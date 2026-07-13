#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""統一論文 PDF 抓取 dispatcher — 依 DOI 前綴選全文繞道路線。

用法:
    python paper_fetch.py <DOI> <out.pdf>
    python paper_fetch.py <DOI>            # 不給輸出檔 → 只試抓並回報狀態，不存檔

路線（依 DOI 前綴自動選，失敗逐階 fallback，最後印機構 SFX 連結）:
    10.1016            → Elsevier TDM Article Retrieval API   (key: ELSEVIER_TDM_KEY)
    10.1002 / 10.1111  → Wiley TDM API                        (key: WILEY_TDM_TOKEN)
    10.1007 / 10.1186  → Springer/BMC OpenAccess（直連 content/pdf；API key 選用）
    其他 / 上述失敗    → Unpaywall 直連 OA PDF
    全部失敗           → 印機構 SFX 連結，機構登入手動下載

KEY 安全：所有 token 從 PC DPAPI secret store 讀（secret.ps1 get），只放記憶體、塞 header，
全程不列印、不寫檔。缺 token → 印一行 `secret.ps1 set <NAME>` 指令，不洩漏既有值。
機構端點與個人 email 從 config.yaml 讀（見 config.example.yaml），不寫死在原始碼。

PDF 一旦存到磁碟，把它拖進對應的 Zotero item；ZotMoov 會自動搬到你的 linked-files 資料夾並轉 linked file。
"""
import re
import sqlite3
import subprocess
import sys
import pathlib

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
    try:
        r = requests.get(url, headers=headers, params={"view": "FULL"}, timeout=90)
    except Exception as e:
        print(f"  Elsevier 連線失敗: {e}")
        return None
    print(f"  Elsevier TDM: HTTP {r.status_code} · {len(r.content)} bytes")
    return r.content if r.status_code == 200 and is_pdf(r.content) else None


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
    return r.content if r.status_code == 200 and is_pdf(r.content) else None


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
    # 直連 OA content/pdf
    return _grab(f"https://link.springer.com/content/pdf/{doi}.pdf")


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


def _pmcid_render_url(doi):
    """DOI→PMCID 直查（NCBI idconv）→ Europe PMC render 端點。

    `_pmc_render_url` 只在候選 URL 字面帶 PMC 時觸發；NIH author manuscript 常在 PMC
    但 Unpaywall 只給 landing page 或漏索引 → 這裡直接問 idconv 補一條候選。查無回 None。"""
    try:
        params = {"ids": doi, "format": "json", "tool": "paper_fetch"}
        if UNPAYWALL_EMAIL:
            params["email"] = UNPAYWALL_EMAIL
        r = requests.get("https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/",
                         params=params, timeout=20, headers={"User-Agent": UA})
        if r.status_code != 200:
            return None
        for rec in (r.json() or {}).get("records", []):
            pmcid = rec.get("pmcid")
            if pmcid:
                print(f"  idconv: {doi} → {pmcid}")
                return f"https://europepmc.org/articles/{pmcid.upper()}?pdf=render"
    except Exception as e:
        print(f"  idconv 查詢略過: {e}")
    return None


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


def _grab(url, referer=None):
    """通用 GET → 驗證 %PDF。用 browser UA。Cloudflare/reCAPTCHA 擋（HTML/403）回 None。"""
    headers = {"User-Agent": BROWSER_UA, "Accept": "application/pdf,*/*"}
    if referer:
        headers["Referer"] = referer
    try:
        r = requests.get(url, timeout=90, headers=headers, allow_redirects=True)
    except Exception as e:
        print(f"  下載失敗 {url}: {e}")
        return None
    print(f"  GET {url} → HTTP {r.status_code} · {len(r.content)} bytes")
    return r.content if r.status_code == 200 and is_pdf(r.content) else None


def _grab_via_landing(url):
    """落地頁：本身若是 PDF 直接收；否則抓 citation_pdf_url meta 再抓一層。涵蓋機構 repository。"""
    try:
        r = requests.get(url, timeout=60, headers={"User-Agent": BROWSER_UA},
                         allow_redirects=True)
    except Exception as e:
        print(f"  落地頁失敗 {url}: {e}")
        return None
    if r.status_code == 200 and is_pdf(r.content):
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
    else:
        primary = []
    return primary + [route_unpaywall]


def main():
    if len(sys.argv) < 2:
        sys.exit("用法: python paper_fetch.py <DOI> [out.pdf]")
    doi = sys.argv[1].strip().removeprefix("https://doi.org/").removeprefix("doi:")
    out = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else None

    print(f"DOI: {doi}")
    for route in routes_for(doi):
        print(f"→ 試 {route.__name__.replace('route_', '')}")
        pdf = route(doi)
        if pdf:
            if out:
                out.write_bytes(pdf)
                print(f"✓ PDF 已存 → {out} ({len(pdf)} bytes)")
                print("  下一步：把此 PDF 拖進對應 Zotero item，ZotMoov 會自動搬到 linked-files 資料夾並轉 linked。")
            else:
                print(f"✓ 抓到有效 PDF（{len(pdf)} bytes）；未指定輸出檔，未存。")
            return

    # 全部失敗 → 機構 SFX 連結
    print("\n✗ 自動路線皆未取得 PDF（可能付費牆或 Cloudflare）。")
    if SFX_BASE:
        print("  改走機構圖書館（已登入機構 session 後）:")
        print(f"  {SFX_BASE.format(doi=doi)}")
    else:
        print("  設定 config.yaml 的 institution.sfx_base 可在此印出你機構的 SFX 連結。")
    print("  Wiley 付費可直接: https://onlinelibrary.wiley.com/doi/pdfdirect/"
          f"{doi}?download=true（機構 IP/session 下）")
    sys.exit(2)


if __name__ == "__main__":
    main()
