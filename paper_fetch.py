#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""統一論文 PDF 抓取 dispatcher — 依 DOI 前綴選全文繞道路線。

用法:
    python paper_fetch.py <DOI> <out.pdf>
    python paper_fetch.py <DOI>            # 不給輸出檔 → 只試抓並回報狀態，不存檔
    python paper_fetch.py --json <DOI> [out.pdf]   # agent 模式：stdout 只出一行 JSON envelope
                                                   # （診斷全走 stderr），typed exit codes
    python paper_fetch.py --title "<文章標題>" <DOI> <out.pdf>
                                                   # ==批次取件請一律加 --title==：驗證抓到的
                                                   # 是不是「那一篇」，整本增刊會自動切出該篇

Exit codes（與 library_session.py 同一張表）: 0=拿到有效 PDF · 1=用法錯誤 · 2=自動路線全敗
JSON envelope: {schema, doi, ok, route, tried[], bytes, sha256, path, resolver_url?,
                partial_path?, reject_reason?, oa_claimed?, blocked_by?[], verify?,
                verify_detail?, volume_path?, extracted_pages?, elapsed_s}
`blocked_by` 列出被 bot 牆（js_challenge / recaptcha / cf_*）擋掉的候選 URL——有它就代表
全文在牆後面，下一步是 layer 2 的真瀏覽器，不是「無全文」。
「有效 PDF」＝ magic bytes ＋內容驗證（頁數/抽出字量；擋 in-press 的 1 頁 pre-proof 封面）；
Elsevier TDM 另看 `X-ELS-Status` 回應 header——未授權的 DOI 會回 HTTP 200 ＋只有第一頁的 PDF。
未過驗證的回應存 <out>.partial、reject_reason 進 envelope，階梯繼續往下走。

==給了 --title 才有第二層（內容層）驗證==：位元組層分不出「這篇文章」與「這篇文章所在的
那一本」。會議摘要常掛增刊的 DOI，於是 Unpaywall／TDM／機構 proxy 三條路都會誠實地回整本
——一個 563 頁的會議摘要集完全合乎 `%PDF-` ＋大小檢查（2026-08，一批 49 篇裡 20% 中招）。
細節與判準見 `pdf_verify.py`。

路線（依 DOI 前綴自動選，失敗逐階 fallback，最後印機構 SFX 連結）:
    10.1016            → Elsevier TDM Article Retrieval API   (key: ELSEVIER_TDM_KEY)
    10.1002 / 10.1111  → Wiley TDM API                        (key: WILEY_TDM_TOKEN)
    10.1007 / 10.1186  → Springer/BMC OpenAccess（直連 content/pdf；API key 選用）
                         ⚠ 2026-09 起 link.springer.com 對所有腳本請求回 JS「Client Challenge」
                         （HTTP 200、3 KB HTML）；此路線只負責把它認出來，OA 全文改靠 PMC
    10.3390            → MDPI：mdpi-res.com CDN 直連（www.mdpi.com 本站是 Cloudflare 403）
    其他 / 上述失敗    → Unpaywall 直連 OA PDF
    全部失敗           → 印機構 SFX 連結，機構登入手動下載

KEY 安全：所有 token 從 PC DPAPI secret store 讀（secret.ps1 get），只放記憶體、塞 header，
全程不列印、不寫檔。缺 token → 印一行 `secret.ps1 set <NAME>` 指令，不洩漏既有值。
機構端點與個人 email 從 config.yaml 讀（見 config.example.yaml），不寫死在原始碼。

PDF 一旦存到磁碟，把它拖進對應的 Zotero item；ZotMoov 會自動搬到你的 linked-files 資料夾並轉 linked file。
"""
import contextlib
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import pathlib
import time

import requests

from paper_config import CFG, require

SECRET_PS1 = pathlib.Path.home() / ".secrets" / "secret.ps1"
UNPAYWALL_EMAIL = CFG["unpaywall_email"] or None
SFX_BASE = CFG["institution"]["sfx_base"]
_CONTACT = CFG["rate"]["contact"]
# contact-identifying UA on direct API/OA routes so publishers can tell "individual
# research use" from "systematic download". (Does NOT apply to the browser/proxy path.)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) paper_fetch/1.0" + (
    f" (mailto:{_CONTACT})" if _CONTACT else "")
# 抓 OA 全文用 browser-like UA：PMC/repository/publisher 靜態 PDF 常擋非瀏覽器 UA
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
# paper-radar 本地 db（選用）：Unpaywall 沒給 PDF 時，用它記錄的 oa_pdf_url 兜底
PAPER_RADAR_DB = pathlib.Path(CFG["paper_radar_db"]) if CFG["paper_radar_db"] else None


def get_secret(name):
    """從 DPAPI 取一個 secret；失敗回 None。stdout 只被本函式吃掉，不外流。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-File", str(SECRET_PS1), "get", name],
            capture_output=True, text=True, timeout=20)
    except Exception:
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout.strip()


def is_pdf(content: bytes) -> bool:
    return len(content) > 1000 and content[:4] == b"%PDF"


# Content-level gate (issue #2): for in-press DOIs the Elsevier TDM API answers with a
# 1-page "Journal Pre-proof" cover sheet — a valid, sometimes large %PDF (876 KB observed,
# bigger than plenty of genuine papers) that has no article body. File size cannot
# discriminate; only page count + extracted-text volume can. Page count is checked first
# so scanned/image-only PDFs (multi-page, little extractable text) still pass.
_LAST_REJECT = None  # (content, reason) of the most recent gate rejection → .partial
_OA_CLAIMED = False  # Unpaywall said is_oa=true — if every route still fails, say so:
                     # "full text likely exists, routes are broken/in-press" is different
                     # follow-up from "paywalled" (issue #2, second observation)
_BLOCKED = []        # (kind, url) of every candidate a bot wall answered instead of a PDF
                     # (issue #3). "Blocked by a JS challenge" and "no OA copy" call for
                     # different next steps — a headful browser passes the former.


# Bot walls that answer a PDF URL with HTTP 200 + a small HTML page (issue #3). Each is a
# JavaScript challenge: no header trick, cookie or TLS impersonation gets past it (curl_cffi
# with a Chrome fingerprint gets the same stub from Springer) — only a real browser does.
_CHALLENGE_MARKERS = (
    (b"_fs-ch-", "js_challenge"),               # Springer Nature "Client Challenge" (2026-09)
    (b"<title>client challenge", "js_challenge"),
    (b"recaptcha", "recaptcha"),                # pmc.ncbi.nlm.nih.gov PDF path (two variants seen)
    (b"just a moment", "cf_challenge"),         # Cloudflare
    (b"attention required", "cf_block"),
)


def challenge_kind(content: bytes):
    """Name the bot wall in an HTML response, or None when it is not one."""
    head = content[:4000].lower()
    for marker, kind in _CHALLENGE_MARKERS:
        if marker in head:
            return kind
    return None


def pdf_gate(content: bytes):
    """回 (ok, reason)。多頁一律放行；單頁才看 pre-proof 標記與抽出字量。"""
    try:
        import fitz  # PyMuPDF — requirements 有列，缺了不擋路只警告
    except ImportError:
        print("  ⚠ PyMuPDF 未安裝 → 跳過內容驗證（pip install pymupdf）")
        return True, None
    try:
        with fitz.open(stream=content, filetype="pdf") as doc:
            pages = doc.page_count
            if pages >= 2:
                # 多頁一律放行，但厚到不像單篇時要出聲：會議摘要掛增刊 DOI 時，每一條
                # 路線都會誠實地回整本（pdf_verify.VOLUME_PAGES 用的是同一個門檻）。
                if pages >= 60:
                    print(f"  ⚠ {pages} 頁 — 不像單篇文章，可能是整本期刊／增刊。"
                          "加 --title \"<標題>\" 可自動驗證並切出該篇")
                return True, None
            text = "".join(p.get_text() for p in doc)
    except Exception as e:
        return False, f"PDF 解析失敗（{e}）"
    low = text.lower()
    # Two in-press cover variants observed from the Elsevier TDM API (issue #2):
    # a "Journal Pre-proof" disclaimer sheet (~2k chars) and an "ARTICLE IN PRESS"
    # first-page preview (~4k chars — title/authors/abstract, so a bare char
    # threshold misses it). Either marker on a single-page doc = not the article.
    if "journal pre-proof" in low:
        return False, f"1 頁 Journal Pre-proof 封面（{len(text)} 字元）— 出版社尚無全文可給"
    if "article in press" in low:
        return False, f"1 頁 ARTICLE IN PRESS 預覽（{len(text)} 字元）— 出版社尚無全文可給"
    if len(text) < 3000:
        return False, f"單頁且僅 {len(text)} 字元 — 疑似封面/摘要頁"
    # A single page with no reference section is what a first-page-only preview looks like
    # (the Elsevier TDM API serves exactly that for DOIs the key is not entitled to — the
    # real first page, cut off mid-Introduction, title intact, 5k+ chars, so everything
    # above passes). The bytes alone cannot settle it: a genuine one-page news item or
    # editorial looks the same (Lancet "In Focus", 2026-08-31 corpus). So this is a WARNING,
    # not a rejection — route_elsevier has the authoritative signal (the X-ELS-Status
    # response header) and rejects there; other routes' one-pagers stay accepted.
    if not _has_reference_section(text):
        print(f"  ⚠ 單頁且無參考文獻段（{len(text)} 字元）— 若不是 letter/news 類短文，"
              "多半是只給第一頁的預覽，請人工確認")
    return True, None


_REF_HEADING = re.compile(r"\b(references?|reference list|bibliography|literature cited|"
                          r"works cited)\b", re.I)
_REF_NUMBERED = re.compile(r"^\s*\[?\d{1,3}[\].)]\s+\S", re.M)  # "1. Smith…" / "[1] Smith…"


def _has_reference_section(text: str) -> bool:
    """A reference-list heading, or ≥3 numbered-citation lines (letters often skip the heading)."""
    if _REF_HEADING.search(text):
        return True
    return len(_REF_NUMBERED.findall(text)) >= 3


def valid_pdf(content: bytes) -> bool:
    """magic bytes ＋內容驗證。未過驗證＝當作沒抓到，讓階梯繼續走，退件留給 .partial。"""
    global _LAST_REJECT
    if not is_pdf(content):
        return False
    ok, reason = pdf_gate(content)
    if not ok:
        print(f"  ✗ 內容驗證未過：{reason} → 續走下一路線")
        _LAST_REJECT = (content, reason)
    return ok


# ── 各路線：成功回 bytes(PDF)，失敗回 None（並印診斷，絕不印 key/header）──────────

def route_elsevier(doi):
    key = get_secret("ELSEVIER_TDM_KEY")
    if not key:
        print("⚠ 無 ELSEVIER_TDM_KEY → 先存：powershell -File ~/.secrets/secret.ps1 set ELSEVIER_TDM_KEY")
        return None
    headers = {"X-ELS-APIKey": key, "Accept": "application/pdf", "User-Agent": UA}
    insttoken = get_secret("ELSEVIER_INSTTOKEN")  # 選用；校外解付費才需
    if insttoken:
        headers["X-ELS-Insttoken"] = insttoken
    url = f"https://api.elsevier.com/content/article/doi/{doi}"
    # No "view" param: with Accept: application/pdf it is unnecessary, and view=FULL
    # gets rejected (HTTP 400 INVALID_INPUT "View parameter ... not valid") for a
    # subset of articles (observed on several Archives of PMR DOIs) while the same
    # request without it returns the PDF fine.
    try:
        r = requests.get(url, headers=headers, timeout=90)
    except Exception as e:
        print(f"  Elsevier 連線失敗: {e}")
        return None
    els_status = r.headers.get("X-ELS-Status", "")
    print(f"  Elsevier TDM: HTTP {r.status_code} · {len(r.content)} bytes · X-ELS-Status: "
          f"{els_status or '-'}")
    # Entitlement is announced in a response header, not in the status code (2026-08-31,
    # 10.1016/j.jdiacomp.2024.108718): for a DOI the key has no full-text rights to, the API
    # still answers HTTP 200 + a real %PDF — but only the FIRST PAGE, and it says so:
    #   X-ELS-Status: WARNING - Response limited to first page because requestor not entitled to resource
    # (an entitled article gets `X-ELS-Status: OK`). That first page has the title, authors,
    # abstract and the start of the Introduction — enough to pass every content heuristic
    # and a title match — so without reading the header it was returned as the article and
    # the institutional route (which may well be entitled) never ran. The Article
    # Entitlement API would be the cleaner check, but it is not enabled for TDM keys
    # (403 AUTHENTICATION_ERROR), so the header is the signal.
    if r.status_code == 200 and "not entitled" in els_status.lower():
        global _LAST_REJECT
        reason = ("Elsevier TDM 只給第一頁（X-ELS-Status: not entitled — 這把 key 對此篇無全文"
                  "授權）— 非全文；機構訂閱（ClinicalKey）可能仍拿得到")
        print(f"  ✗ {reason} → 續走下一路線")
        if is_pdf(r.content):
            _LAST_REJECT = (r.content, reason)
        return None
    return r.content if r.status_code == 200 and valid_pdf(r.content) else None


def route_wiley(doi):
    token = get_secret("WILEY_TDM_TOKEN")
    if not token:
        print("⚠ 無 WILEY_TDM_TOKEN → 接受 click-through (https://static.wiley.com/tdm/) 後存：")
        print("   powershell -File ~/.secrets/secret.ps1 set WILEY_TDM_TOKEN")
        return None
    headers = {"Wiley-TDM-Client-Token": token, "Accept": "application/pdf", "User-Agent": UA}
    # TDM v1 端點依 DOI 回 PDF（會 302 到實際 PDF；requests 預設跟隨）
    from urllib.parse import quote
    url = f"https://api.wiley.com/onlinelibrary/tdm/v1/articles/{quote(doi, safe='')}"
    try:
        r = requests.get(url, headers=headers, timeout=90)
    except Exception as e:
        print(f"  Wiley 連線失敗: {e}")
        return None
    print(f"  Wiley TDM: HTTP {r.status_code} · {len(r.content)} bytes")
    return r.content if r.status_code == 200 and valid_pdf(r.content) else None


def route_springer(doi):
    # OA Springer/BMC 的 PDF 直連最穩；API key 僅用於確認 OA/配額，非必要
    key = get_secret("SPRINGER_API_KEY")
    if key:
        try:
            r = requests.get("https://api.springernature.com/openaccess/json",
                             params={"q": f"doi:{doi}", "api_key": key},
                             timeout=30, headers={"User-Agent": UA})
            if r.status_code == 200:
                recs = r.json().get("records", [])
                if recs:
                    for u in recs[0].get("url", []):
                        if u.get("format") == "pdf" and u.get("value"):
                            pdf = _grab(u["value"])
                            if pdf:
                                return pdf
        except Exception as e:
            print(f"  Springer API 查詢失敗（改直連）: {e}")
    else:
        print("⚠ 無 SPRINGER_API_KEY（OA 直連通常仍可）→ 需要時存：")
        print("   powershell -File ~/.secrets/secret.ps1 set SPRINGER_API_KEY")
    # 直連 OA content/pdf。2026-09-05 起 link.springer.com 對每個腳本請求（含舊 OA 文章、含
    # 先抓落地頁拿 cookie、含 curl_cffi 模仿 Chrome）都回 HTTP 200 + 3 KB「Client Challenge」
    # JS 頁；*.biomedcentral.com 的 /counter/pdf 也 302 回同一面牆。這裡只做一次，認出牆就
    # 說清楚，剩下交給 PMC 候選（route_unpaywall）與 layer 2 的真瀏覽器。
    pdf = _grab(f"https://link.springer.com/content/pdf/{doi}.pdf")
    if pdf is None and _BLOCKED and _BLOCKED[-1][0] == "js_challenge":
        print("  Springer: 直連被 JS Client Challenge 擋（不是 cookie gate、不是無 OA）→ "
              "改靠 PMC 候選；都沒有時 layer 2 的 headful 瀏覽器可過此牆")
    return pdf


def _mdpi_cdn_urls(doi):
    """10.3390 → mdpi-res.com CDN 上的 PDF 候選（issue #3）。

    www.mdpi.com 本站對腳本一律 Cloudflare 403（含 Semantic Scholar 給的 /pdf 連結），但
    文章 PDF 同時放在 CDN：
      https://mdpi-res.com/d_attachment/{j}/{j}-{vol}-{art:05d}/article_deploy/{j}-{vol}-{art:05d}.pdf
    `{j}` 有兩種：DOI 裡的期刊代碼（life、ijerph、jcm…）或期刊全名去空白（nutrients、
    sensors、antioxidants…）——DOI 代碼 nu/s/antiox 的檔名用全名。2026-09-05 抽 17 個 DOI
    兩種擇一命中 16 個。DOI 尾碼＝代碼＋卷＋兩位期號＋文章號；卷號長度從 Crossref 拿。"""
    m = re.match(r"^10\.3390/([a-z]+)(\d+)$", doi.lower())
    if not m:
        return []
    code, digits = m.group(1), m.group(2)
    vol = title = None
    try:
        r = requests.get(f"https://api.crossref.org/works/{doi}", timeout=20,
                         headers={"User-Agent": UA})
        if r.status_code == 200:
            msg = (r.json() or {}).get("message") or {}
            vol = msg.get("volume")
            title = (msg.get("container-title") or [None])[0]
    except Exception as e:
        print(f"  Crossref 查詢略過: {e}")
    if vol and digits.startswith(vol) and len(digits) > len(vol) + 2:
        art = digits[len(vol) + 2:]
    else:  # Crossref 沒答：假設四位文章號、兩位期號
        art, vol = digits[-4:], digits[:-6] or None
    if not vol:
        return []
    art = art.zfill(5)
    slugs = [code]
    if title:
        slug = re.sub(r"[^a-z0-9]", "", title.lower())
        if slug and slug != code:
            slugs.append(slug)
    return [f"https://mdpi-res.com/d_attachment/{j}/{j}-{vol}-{art}/article_deploy/{j}-{vol}-{art}.pdf"
            for j in slugs]


def route_mdpi(doi):
    urls = _mdpi_cdn_urls(doi)
    if not urls:
        print("  MDPI: DOI 不符 <代碼><卷><期><文章號> 型式 → 略過 CDN 直連")
        return None
    for u in urls:
        pdf = _grab(u)
        if pdf:
            return pdf
    return None


def _pmc_render_url(url):
    """PMC 落地頁（reCAPTCHA 擋）→ 轉 Europe PMC 直接出 PDF 的 render 端點。非 PMC 回 None。"""
    if not url:
        return None
    low = url.lower()
    if ("ncbi.nlm.nih.gov" in low or "pmc.ncbi" in low or "europepmc.org" in low):
        m = re.search(r"(PMC\d+)", url, re.I)
        if m:
            return f"https://europepmc.org/articles/{m.group(1).upper()}?pdf=render"
    return None


def _pmcid_lookup(doi):
    """DOI→PMCID：NCBI idconv，查無或出錯再問 Europe PMC search（兩個獨立索引）。查無回 None。"""
    try:
        params = {"ids": doi, "format": "json", "tool": "paper_fetch"}
        if UNPAYWALL_EMAIL:
            params["email"] = UNPAYWALL_EMAIL
        r = requests.get("https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/",
                         params=params, timeout=20, headers={"User-Agent": UA})
        if r.status_code == 200:
            for rec in (r.json() or {}).get("records", []):
                if rec.get("pmcid"):
                    print(f"  idconv: {doi} → {rec['pmcid']}")
                    return rec["pmcid"].upper()
    except Exception as e:
        print(f"  idconv 查詢略過: {e}")
    try:
        r = requests.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                         params={"query": f'DOI:"{doi}"', "format": "json",
                                 "resultType": "lite"},
                         timeout=20, headers={"User-Agent": UA})
        if r.status_code == 200:
            for rec in ((r.json() or {}).get("resultList") or {}).get("result") or []:
                if rec.get("pmcid"):
                    print(f"  Europe PMC search: {doi} → {rec['pmcid']}")
                    return rec["pmcid"].upper()
    except Exception as e:
        print(f"  Europe PMC 查詢略過: {e}")
    return None


def _pmcid_render_url(doi):
    """DOI→PMCID → Europe PMC render 端點。

    `_pmc_render_url` 只在候選 URL 字面帶 PMC 時觸發；NIH author manuscript 常在 PMC
    但 Unpaywall 只給 landing page 或漏索引 → 這裡直接查 PMCID 補一條候選。查無回 None。

    有 PMCID 時腳本能用的 PDF 端點只有 Europe PMC 這一個：pmc.ncbi.nlm.nih.gov 的 /pdf/
    回 reCAPTCHA 頁，Europe PMC REST 的 fullTextXML 是 XML 不是 PDF（issue #3 查證）。
    Europe PMC 偶爾回 HTTP 500 是暫時的（同一 URL 幾小時後 200），由 _grab 重試一次處理。"""
    pmcid = _pmcid_lookup(doi)
    return f"https://europepmc.org/articles/{pmcid}?pdf=render" if pmcid else None


def _semantic_scholar_pdf(doi):
    """Semantic Scholar Graph API 的 openAccessPdf 兜底。

    ==為什麼加（2026-07-14）==：Unpaywall 會漏 index 一部分 OA（尤其 preprint server 版本、
    以及某些出版社的 hybrid OA）；S2 的 openAccessPdf 是另一個獨立的 OA 索引，兩者互補。
    無 API key 也能用（有 rate limit，偶爾 429 → 靜默略過，不影響其他候選）。查無回 None。"""
    try:
        r = requests.get(f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}",
                         params={"fields": "openAccessPdf"}, timeout=20,
                         headers={"User-Agent": UA})
        if r.status_code == 200:
            oa = (r.json() or {}).get("openAccessPdf") or {}
            url = oa.get("url")
            if url:
                print(f"  Semantic Scholar openAccessPdf: {url}")
                return url
        elif r.status_code != 404:
            print(f"  Semantic Scholar: HTTP {r.status_code}（略過）")
    except Exception as e:
        print(f"  Semantic Scholar 查詢略過: {e}")
    return None


def _local_oa_url(doi):
    """paper-radar 本地 db 的 oa_pdf_url 兜底（唯讀，選用）。未設定/查不到/出錯回 None。"""
    if not PAPER_RADAR_DB or not PAPER_RADAR_DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{PAPER_RADAR_DB}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute("select oa_pdf_url from papers where doi=?", (doi,)).fetchone()
        finally:
            con.close()
        return row[0] if row and row[0] else None
    except Exception as e:
        print(f"  本地 db 查詢略過: {e}")
        return None


def route_unpaywall(doi):
    """OA 兜底：彙整 Unpaywall 所有 oa_locations + 本地 db 的候選 PDF，逐一試抓。

    修正點：①不再只看 best_oa_location.url_for_pdf，遍歷所有 location；
    ②PMC 落地頁轉 Europe PMC render 端點（PMC 已上 reCAPTCHA 擋 bot）；
    ③本地 db oa_pdf_url 兜底；④落地頁抓 citation_pdf_url meta（涵蓋機構 repository）。
    """
    candidates = []   # 直連 PDF 候選（依序、去重）
    landings = []     # 落地頁候選（最後才試 meta / 直接是 PDF）

    def add_pdf(u):
        if u and u not in candidates:
            candidates.append(u)

    def add_landing(u):
        if u and u not in landings:
            landings.append(u)

    if not UNPAYWALL_EMAIL:
        print("  Unpaywall: 未設定 unpaywall_email（config.yaml）→ 略過 OA 查詢")
    else:
        try:
            r = requests.get(f"https://api.unpaywall.org/v2/{doi}",
                             params={"email": UNPAYWALL_EMAIL}, timeout=20,
                             headers={"User-Agent": UA})
            if r.status_code == 200:
                j = r.json() or {}
                if j.get("is_oa"):
                    global _OA_CLAIMED
                    _OA_CLAIMED = True
                locs = []
                if j.get("best_oa_location"):
                    locs.append(j["best_oa_location"])
                locs += (j.get("oa_locations") or [])
                for loc in locs:
                    if not loc:
                        continue
                    add_pdf(loc.get("url_for_pdf"))
                    add_pdf(_pmc_render_url(loc.get("url_for_pdf")))
                    add_pdf(_pmc_render_url(loc.get("url")))
                    add_landing(loc.get("url"))
                if not locs:
                    print(f"  Unpaywall: is_oa={j.get('is_oa')} oa_status={j.get('oa_status')} · 無 OA location")
            else:
                print(f"  Unpaywall: HTTP {r.status_code}")
        except Exception as e:
            print(f"  Unpaywall 查詢失敗: {e}")

    # DOI→PMCID 直查兜底（author manuscript 在 PMC 但 Unpaywall 漏列/只給 landing）
    add_pdf(_pmcid_render_url(doi))

    # Semantic Scholar openAccessPdf 兜底（獨立於 Unpaywall 的 OA 索引，互補）
    s2 = _semantic_scholar_pdf(doi)
    if s2:
        add_pdf(s2)
        add_pdf(_pmc_render_url(s2))
        add_landing(s2)

    # 本地 db 兜底（Unpaywall 未 index 或未給 PDF 時）
    local = _local_oa_url(doi)
    if local:
        add_pdf(_pmc_render_url(local))
        add_landing(local)

    if not candidates and not landings:
        print("  OA: 無任何候選 URL")
        return None

    for u in candidates:
        print(f"  OA 候選 PDF: {u}")
        pdf = _grab(u)
        if pdf:
            return pdf
    for u in landings:
        pdf = _grab_via_landing(u)
        if pdf:
            return pdf
    print("  OA: 所有候選皆未取得有效 PDF")
    return None


_RETRY_5XX_WAIT_S = 3


def _grab(url, referer=None):
    """通用 GET → 驗證 %PDF。用 browser UA。

    5xx 重試一次（Europe PMC render 對同一 PMCID 先 500、稍後 200——issue #3）；HTTP 200
    卻是 bot 牆的 HTML（Springer Client Challenge、reCAPTCHA、Cloudflare）認出來、記進
    _BLOCKED 並印名字，讓「被牆擋」和「沒有 OA 副本」在 log 與 envelope 裡分得開。"""
    headers = {"User-Agent": BROWSER_UA, "Accept": "application/pdf,*/*"}
    if referer:
        headers["Referer"] = referer
    r = None
    for attempt in (1, 2):
        try:
            r = requests.get(url, timeout=90, headers=headers, allow_redirects=True)
        except Exception as e:
            print(f"  下載失敗 {url}: {e}")
            return None
        print(f"  GET {url} → HTTP {r.status_code} · {len(r.content)} bytes")
        if r.status_code < 500 or attempt == 2:
            break
        print(f"  HTTP {r.status_code} 多半是暫時的 → {_RETRY_5XX_WAIT_S} s 後重試一次")
        time.sleep(_RETRY_5XX_WAIT_S)
    if r.status_code == 200 and valid_pdf(r.content):
        return r.content
    kind = challenge_kind(r.content) if r.content[:4] != b"%PDF" else None
    if kind:
        _BLOCKED.append((kind, url))
        print(f"  ✗ 回應是 bot 牆（{kind}）而非 PDF —— 腳本過不了，真瀏覽器（layer 2 headful）可以")
    return None


def _grab_via_landing(url):
    """落地頁：本身若是 PDF 直接收；否則抓 citation_pdf_url meta 再抓一層。涵蓋機構 repository。"""
    try:
        r = requests.get(url, timeout=60, headers={"User-Agent": BROWSER_UA},
                         allow_redirects=True)
    except Exception as e:
        print(f"  落地頁失敗 {url}: {e}")
        return None
    if r.status_code == 200 and valid_pdf(r.content):
        print(f"  落地頁本身即 PDF: {url} · {len(r.content)} bytes")
        return r.content
    if "html" not in r.headers.get("content-type", "").lower():
        return None
    m = re.search(r'citation_pdf_url"\s+content="([^"]+)"', r.text)
    if not m:
        return None
    pdf_url = m.group(1)
    if pdf_url == url:
        return None
    print(f"  落地頁 meta citation_pdf_url: {pdf_url}")
    return _grab(pdf_url, referer=r.url)


def routes_for(doi):
    """依 DOI 前綴排序路線，永遠以 Unpaywall 兜底。"""
    d = doi.lower()
    if d.startswith("10.1016"):
        primary = [route_elsevier]
    elif d.startswith("10.1002") or d.startswith("10.1111"):
        primary = [route_wiley]
    elif d.startswith("10.1007") or d.startswith("10.1186"):
        primary = [route_springer]
    elif d.startswith("10.3390"):
        primary = [route_mdpi]
    else:
        primary = []
    return primary + [route_unpaywall]


def content_check(pdf: bytes, title, out):
    """第二層驗證：這份 PDF 是不是「那一篇」。沒給 title 就回空 dict（不做）。

    整本增刊（volume_like）會就地切出該篇，==整本保留成 <stem>_volume.pdf 不刪==：
    它是複查「切的那頁對不對」唯一的依據。title_absent／no_text 不擋收件（掃描版式、標題
    在圖片裡都會這樣），但一定要留在 envelope 裡讓呼叫端看見——拿錯文件比缺全文更糟。"""
    if not title:
        return {}
    try:
        import pdf_verify
    except ImportError:
        print("  ⚠ pdf_verify 不可用 → 跳過內容驗證")
        return {}
    res = pdf_verify.verify(pdf, title)
    info = {"verify": res["verdict"], "verify_detail": res["detail"]}
    if res["verdict"] == "match":
        print(f"  ✓ 內容驗證：{res['detail']}")
        return info
    print(f"  ⚠ 內容驗證：{res['verdict']} — {res['detail']}")
    if res["verdict"] == "volume_like" and out:
        cut = pdf_verify.extract(out, title)
        if cut["extracted"]:
            print(f"  ✂ 從整本切出 p{cut['first_page']}-{cut['last_page']}／{cut['pages']} 頁"
                  f"（連續命中 {cut['ratio']:.0%}，第二名 {cut['runner']:.0%}）")
            print(f"    整本保留 → {cut['volume_path']}")
            info.update({"verify": "extracted_from_volume",
                         "volume_path": cut["volume_path"],
                         "extracted_pages": [cut["first_page"], cut["last_page"]]})
        else:
            print(f"  ✗ 整本裡定位不到這篇（{cut['reason']}）→ 檔案原樣留著，人工確認")
    return info


def run_ladder(doi, out, title=None):
    """Walk the route ladder. Returns an envelope dict (also the --json payload).

    Exit-code contract — same table as library_session.py so orchestrators need one rule:
      0 = valid PDF obtained (saved if an output path was given)
      1 = usage error
      2 = every automatic route exhausted — envelope carries the resolver link
    """
    started = time.time()
    tried = []
    for route in routes_for(doi):
        name = route.__name__.removeprefix("route_")
        print(f"→ 試 {name}")
        tried.append(name)
        pdf = route(doi)
        if pdf:
            path = None
            if out:
                out.write_bytes(pdf)
                path = str(out)
                print(f"✓ PDF 已存 → {out} ({len(pdf)} bytes)")
                print("  下一步：把此 PDF 拖進對應 Zotero item，ZotMoov 會自動搬到 linked-files 資料夾並轉 linked。")
            else:
                print(f"✓ 抓到有效 PDF（{len(pdf)} bytes）；未指定輸出檔，未存。")
            checked = content_check(pdf, title, out)
            env = {"schema": 1, "doi": doi, "ok": True, "route": name,
                   "tried": tried, "bytes": len(pdf),
                   "sha256": hashlib.sha256(pdf).hexdigest(), "path": path,
                   "elapsed_s": round(time.time() - started, 1)}
            if path and checked.get("extracted_pages"):   # 切過了 → 重算實際檔案的指紋
                data = out.read_bytes()
                env.update({"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            env.update(checked)
            return env

    # 全部失敗 → 機構 SFX 連結
    resolver = SFX_BASE.format(doi=doi) if SFX_BASE else None
    partial_path = reject_reason = None
    if _LAST_REJECT:
        rejected, reject_reason = _LAST_REJECT
        if out:
            partial = out.with_name(out.name + ".partial")
            partial.write_bytes(rejected)
            partial_path = str(partial)
        print(f"\n✗ 有路線回應但內容未過驗證：{reject_reason}")
        if partial_path:
            print(f"  退件已留存（標題/作者 metadata 仍可用）→ {partial_path}")
        print("  多半是文章 in-press、出版社尚未釋出正式全文 → 走機構管道或過幾天重試。")
    elif _BLOCKED:
        kinds = sorted({k for k, _ in _BLOCKED})
        print(f"\n✗ 候選 URL 有 {len(_BLOCKED)} 個被 bot 牆（{', '.join(kinds)}）擋下，其餘皆無效。")
        print("  全文多半存在（牆後面就是 PDF）→ 用 layer 2 的真瀏覽器："
              "library_session.py fetch <DOI> <out.pdf>；或稍後重試。這不是付費牆。")
    elif _OA_CLAIMED:
        print("\n✗ Unpaywall 標記 is_oa=true，但所有自動候選 URL 皆失效（in-press 常見）。")
        print("  全文多半存在 → 走機構 resolver 或稍後重試，而非付費牆。")
    else:
        print("\n✗ 自動路線皆未取得 PDF（可能付費牆或 Cloudflare）。")
    if resolver:
        print("  改走機構圖書館（已登入機構 session 後）:")
        print(f"  {resolver}")
    else:
        print("  設定 config.yaml 的 institution.sfx_base 可在此印出你機構的 SFX 連結。")
    print("  Wiley 付費可直接: https://onlinelibrary.wiley.com/doi/pdfdirect/"
          f"{doi}?download=true（機構 IP/session 下）")
    return {"schema": 1, "doi": doi, "ok": False, "route": None, "tried": tried,
            "bytes": 0, "sha256": None, "path": None, "resolver_url": resolver,
            "partial_path": partial_path, "reject_reason": reject_reason,
            "oa_claimed": _OA_CLAIMED,
            "blocked_by": [{"kind": k, "url": u} for k, u in _BLOCKED],
            "elapsed_s": round(time.time() - started, 1)}


def main():
    argv = sys.argv[1:]
    as_json = "--json" in argv
    title = None
    args = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            pass
        elif a == "--title":
            i += 1
            title = argv[i] if i < len(argv) else None
        elif a.startswith("--title="):
            title = a.split("=", 1)[1]
        else:
            args.append(a)
        i += 1
    if not args:
        print('用法: python paper_fetch.py [--json] [--title "<標題>"] <DOI> [out.pdf]',
              file=sys.stderr)
        sys.exit(1)
    doi = args[0].strip().removeprefix("https://doi.org/").removeprefix("doi:")
    out = pathlib.Path(args[1]) if len(args) > 1 else None

    print(f"DOI: {doi}", file=sys.stderr if as_json else sys.stdout)
    if as_json:
        # --json contract: stdout carries EXACTLY one JSON envelope; every
        # diagnostic line the routes print is rerouted to stderr.
        with contextlib.redirect_stdout(sys.stderr):
            env = run_ladder(doi, out, title)
        print(json.dumps(env, ensure_ascii=False))
    else:
        env = run_ladder(doi, out, title)
    sys.exit(0 if env["ok"] else 2)


if __name__ == "__main__":
    main()
