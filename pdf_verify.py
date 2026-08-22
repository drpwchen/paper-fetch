#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""內容層驗證：抓回來的 PDF 到底是不是「那一篇」——以及從整本裡切出那一篇。

==為什麼需要這支==（2026-08-22，一次 147 筆的系統性回顧全文取件）：
取件腳本一路只用 magic bytes（`%PDF-` ＋ >20 KB）當合格條件，於是一個 48 MB／563 頁的
會議摘要集被標成 `ok`，而我們要的其實是裡面某一頁的一篇 research letter。
全掃 49 篇 article PDF 後發現 ==10 篇（20%）拿到的是整本會議摘要集或增刊==，而且全是核心
研究。位元組層的檢查分不出「這篇文章」與「這篇文章所在的那一本」，而拿錯文件去跑全文篩選
比缺全文更糟——它會對錯的研究產出一個有信心的答案。

成因是常態不是意外：**會議摘要常掛增刊的 DOI 而不是自己的**，於是 Unpaywall、出版社 TDM
API、機構 proxy 三條路都會誠實地把整本交給你。

## 兩個判準上的教訓（都是踩過才知道的）

1. ==用「連續子字串」比對，不要用 bag-of-words。== 摘要集一頁塞好幾篇，用關鍵詞集合去比，
   隔壁兩篇各出現一個詞就會湊出一個假命中。要求標題**連續**出現，整個失效類別就消失了。
2. ==驗證方法選錯，會製造出比原始錯誤更難察覺的假警報。== 第一版複驗只印每頁**前 230 字**
   來人工抽查，據此判定「4 筆有 3 筆切錯」——那個判定本身才是錯的：摘要集的標題在頁面下半。
   改成「整頁正規化文字是否含連續標題字串」重驗，10 個候選頁 100% 命中、第二名只有 20–45%。
   檢查的粒度要對得上文件的排版，否則你會回頭去「修」一個沒壞的東西。

## 判定

    match         標題出現在開頭幾頁 → 就是這篇
    volume_like   標題不在開頭幾頁，而且頁數多 → 多半是整本／增刊，可用 --extract 切
    title_absent  標題不在開頭幾頁，但頁數不多 → 可疑（抓到別篇？勘誤？只有摘要？）
    no_text       抽不出文字（掃描件或加密）→ 無法判斷，人工看
    unreadable    檔案根本解析不開
    no_title      沒給標題 → 不做內容驗證（呼叫端自己知道就好）

## 用法

    python pdf_verify.py <pdf> --title "<文章標題>"              # 只驗證
    python pdf_verify.py <pdf> --title "<標題>" --extract        # volume → 切出該篇
    python pdf_verify.py <pdf> --title "<標題>" --json           # 一行 JSON，給 agent 用

`--extract` 會把原檔改名成 `<stem>_volume.pdf` 保留（==整本不刪==：它是花了一次機構 session
才拿到的東西，而且是「這頁到底對不對」唯一的複查依據），再把切出來的頁面寫回原路徑。

模組介面：`verify(source, title)` / `locate(source, title)` / `extract(path, title)`。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

PAGES_TO_READ = 4      # 一篇文章的前置頁；也足以掃到整本的目次
VOLUME_PAGES = 60      # 超過這個頁數的「單篇文章」幾乎一定是整本或增刊
ACCEPT = 0.60          # 一頁至少要連續帶到標題的這個比例才算命中
STRONG = 0.90          # 到這個比例以上直接接受，不必贏第二名
MARGIN = 0.15          # 否則要領先第二名這麼多，否則寧可不切
MIN_TITLE_CHARS = 12   # 太短的標題（"Old Bones" 之類）沒有鑑別力，不做判定


def _open(source):
    """source 可以是 bytes / 路徑字串 / Path。回 fitz.Document。"""
    try:
        import fitz  # PyMuPDF
    except ImportError:  # pragma: no cover - 環境問題，訊息比堆疊有用
        sys.exit("需要 PyMuPDF：pip install pymupdf")
    if isinstance(source, (bytes, bytearray)):
        return fitz.open(stream=bytes(source), filetype="pdf")
    return fitz.open(str(source))


def norm(s: str) -> str:
    """正規化：先接回跨行斷字（"alen- dronate" → "alendronate"），再折疊大小寫與標點。

    斷字是排版造成的，不是內容差異；不接回來的話，真命中會平白掉掉一截長度。"""
    s = re.sub(r"-\s+", "", s or "")
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split())


def contiguous_ratio(title: str, page: str) -> float:
    """`title` 能在 `page` 裡逐字連續找到的最長片段，佔標題長度的比例。"""
    if not title or not page:
        return 0.0
    best = 0
    for i in range(len(title)):
        if len(title) - i <= best:
            break
        for j in range(len(title), i + best, -1):
            if title[i:j] in page:
                best = j - i
                break
    return best / len(title)


def _page_texts(doc, limit: int | None = None) -> list[str]:
    out = []
    for i, page in enumerate(doc):
        if limit is not None and i >= limit:
            break
        try:
            out.append(norm(page.get_text()))
        except Exception:
            out.append("")
    return out


def verify(source, title: str, *, pages_to_read: int = PAGES_TO_READ,
           volume_pages: int = VOLUME_PAGES, accept: float = ACCEPT) -> dict:
    """開頭幾頁裡找得到這個標題嗎？回 {verdict, detail, pages, ratio}。"""
    t = norm(title)
    if len(t) < MIN_TITLE_CHARS:
        return {"verdict": "no_title", "detail": "標題太短或未提供，跳過內容驗證",
                "pages": 0, "ratio": 0.0}
    try:
        doc = _open(source)
    except Exception as e:
        return {"verdict": "unreadable", "detail": f"{type(e).__name__}: {e}",
                "pages": 0, "ratio": 0.0}
    with doc:
        pages = doc.page_count
        texts = _page_texts(doc, limit=pages_to_read)
    head = " ".join(texts)
    if not head.strip():
        return {"verdict": "no_text", "detail": "抽不出文字（掃描件或加密？）",
                "pages": pages, "ratio": 0.0}
    ratio = contiguous_ratio(t, head)
    if ratio >= accept:
        return {"verdict": "match", "detail": f"標題連續命中 {ratio:.0%}",
                "pages": pages, "ratio": ratio}
    if pages >= volume_pages:
        return {"verdict": "volume_like",
                "detail": f"前 {pages_to_read} 頁只連續帶到標題的 {ratio:.0%}，共 {pages} 頁"
                          "（整本／增刊？可用 --extract 切）",
                "pages": pages, "ratio": ratio}
    return {"verdict": "title_absent",
            "detail": f"前 {pages_to_read} 頁只連續帶到標題的 {ratio:.0%}",
            "pages": pages, "ratio": ratio}


def locate(source, title: str, *, accept: float = ACCEPT, strong: float = STRONG,
           margin: float = MARGIN) -> dict:
    """在整本裡定位這篇。回 {ok, page(0-based), ratio, runner, pages, reason}。

    接受條件：①該頁連續帶到標題的比例 ≥ accept，且 ②要嘛近乎完全命中（≥ strong），
    要嘛領先第二名 margin 以上。==模稜兩可時寧可不切==——切錯比不切糟：它會餵給全文
    篩選一份別人的研究，而且看起來完全正常。"""
    t = norm(title)
    if len(t) < MIN_TITLE_CHARS:
        return {"ok": False, "reason": "no_title", "page": None, "ratio": 0.0,
                "runner": 0.0, "pages": 0}
    with _open(source) as doc:
        texts = _page_texts(doc)
    scored = sorted(((contiguous_ratio(t, pg), i) for i, pg in enumerate(texts)),
                    key=lambda x: (-x[0], x[1]))
    if not scored:
        return {"ok": False, "reason": "empty", "page": None, "ratio": 0.0,
                "runner": 0.0, "pages": 0}
    top, best_page = scored[0]
    runner = scored[1][0] if len(scored) > 1 else 0.0
    base = {"page": best_page, "ratio": top, "runner": runner, "pages": len(texts)}
    if top < accept:
        return {**base, "ok": False, "reason": "not_found"}
    if top < strong and top - runner < margin:
        return {**base, "ok": False, "reason": "ambiguous"}
    return {**base, "ok": True, "reason": "found"}


def extract(path, title: str, *, out=None, pages_after: int = 1,
            keep_volume: bool = True) -> dict:
    """在整本裡定位這篇並切出它所在的頁（＋後一頁，摘要常跨頁）。

    回 locate() 的結果再加 {extracted, path, volume_path}。原檔改名保留成
    `<stem>_volume.pdf`（keep_volume=False 才會覆寫掉）。"""
    src = pathlib.Path(path)
    dst = pathlib.Path(out) if out else src
    hit = locate(src, title)
    if not hit["ok"]:
        return {**hit, "extracted": False, "path": None, "volume_path": None}

    import fitz
    vol = src.with_name(src.stem + "_volume.pdf")
    if keep_volume:
        if not vol.exists():
            src.rename(vol)
        source_path = vol
    else:
        source_path = src
    first = hit["page"]
    last = min(first + pages_after, hit["pages"] - 1)
    with fitz.open(str(source_path)) as doc:
        with fitz.open() as cut:
            cut.insert_pdf(doc, from_page=first, to_page=last)
            cut.save(str(dst))
    return {**hit, "extracted": True, "first_page": first + 1, "last_page": last + 1,
            "path": str(dst), "volume_path": str(vol) if keep_volume else None}


def main() -> int:
    ap = argparse.ArgumentParser(description="PDF 內容驗證／整本切頁")
    ap.add_argument("pdf")
    ap.add_argument("--title", required=True, help="這筆記錄的文章標題")
    ap.add_argument("--extract", action="store_true",
                    help="判定為整本時，切出該篇（原檔保留為 <stem>_volume.pdf）")
    ap.add_argument("--out", help="切出來要寫到哪（預設就地覆寫）")
    ap.add_argument("--json", action="store_true", help="只輸出一行 JSON")
    a = ap.parse_args()

    res = verify(a.pdf, a.title)
    if a.extract and res["verdict"] in ("volume_like", "title_absent"):
        res = {**res, **extract(a.pdf, a.title, out=a.out)}

    if a.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        print(f"{res['verdict']}: {res['detail']}")
        if res.get("extracted"):
            print(f"  切出 p{res['first_page']}-{res['last_page']}／共 {res['pages']} 頁 "
                  f"(連續命中 {res['ratio']:.0%}，第二名 {res['runner']:.0%})")
            print(f"  → {res['path']}（整本保留：{res['volume_path']}）")
        elif a.extract and res.get("reason") in ("not_found", "ambiguous"):
            print(f"  未切：{res['reason']}（最佳頁 {(res.get('page') or 0) + 1} "
                  f"命中 {res.get('ratio', 0):.0%}，第二名 {res.get('runner', 0):.0%}）")
    # 0=就是這篇 · 3=可疑（volume/title_absent/no_text）· 1=解析不開／沒判
    return {"match": 0, "no_title": 1, "unreadable": 1}.get(res["verdict"], 3)


if __name__ == "__main__":
    sys.exit(main())
