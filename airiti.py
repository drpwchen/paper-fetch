#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Airiti Library (華藝線上圖書館) — Chinese-language journal articles.

Airiti is a database-level subscription, not a per-journal one, so `holdings.py` (built
from the library's A–Z e-journal list) cannot see it: a journal missing from holdings says
nothing about Airiti access. Articles are addressed by Airiti's own DocID, and most also
carry a DOI, so both work here:

    python airiti.py fetch 10232141-202204-202205090002-202205090002-170-188 out.pdf
    python airiti.py fetch 10.6288/TJPH.202204_41(2).110135 out.pdf
    python airiti.py search "居家復能" --limit 10

Three traps, each of which silently produces "nothing happened" rather than an error:

1. ==The cookie-consent banner must be dismissed, and it is NOT clickable normally.==
   Playwright reports its 我知道了 span as not visible, so a plain .click() raises and the
   usual try/except swallows it — after which the confirm button does nothing at all, with
   no error and no network call. Dismiss it from inside the page (JS click) instead.
2. ==The PDF arrives as a blob download, not as a readable response body.== The site XHRs
   POST /Article/TextDownloadNew (Application/octet-stream), wraps the bytes in a Blob and
   triggers a browser download. Watching for a %PDF response body sees nothing; you have to
   catch the download event. Allow ~15 s — the server takes several seconds to answer.
3. ==Use the query form of the article URL: /Article/Detail?DocID=…== The path form
   (/Article/Detail/…) renders a page that looks identical but carries a different
   entitlement state.

Reads the institutional session from library_session (same proxy, same cookies).
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from pathlib import Path

import library_session as ls

HOST = "www.airitilibrary.com"
DOI_RE = re.compile(r"^10\.\d{4,9}/")


def _log(msg: str) -> None:
    print(f"[airiti] {msg}", file=sys.stderr, flush=True)


def proxy_host() -> str:
    """Airiti through the library proxy, e.g. www-airitilibrary-com.<proxy suffix>."""
    return ls._proxy_host(HOST)


def article_url(docid: str, direct: bool = False) -> str:
    host = HOST if direct else proxy_host()
    return f"https://{host}/Article/Detail?DocID={docid}"


def doi_url(doi: str, direct: bool = False) -> str:
    if direct:
        return f"https://doi.org/{doi}"
    return f"https://doi-org.{ls.PROXY_SUFFIX}/{doi}"


def open_article(page, url: str) -> bool:
    """Open an Airiti page through the proxy, completing the gate handshake if bounced."""
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    time.sleep(3)
    if "遠端讀者認證" in page.title() or "/login" in page.url:
        _log("bounced to the library gate — submitting the form on that page")
        if not ls._login_submit_here(page):
            return False
        time.sleep(2)
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(3)
    return "遠端讀者認證" not in page.title()


def dismiss_banner(page) -> None:
    """Trap 1. Click it from inside the page — Playwright considers the span invisible."""
    try:
        clicked = page.evaluate("""() => {
          let n = 0;
          for (const e of document.querySelectorAll('span,button,a')) {
            if ((e.innerText || '').trim() === '我知道了') { e.click(); n++; }
          }
          return n; }""")
        if clicked:
            _log(f"cookie banner dismissed ({clicked} control(s))")
            time.sleep(1)
    except Exception as e:                      # never fatal, but say so
        _log(f"cookie banner: {e!r}")


def fetch(page, out: Path, wait_s: int = 40) -> dict:
    """Article page → 全文下載 → confirm → catch the blob download."""
    res = {"ok": False, "reason": "", "bytes": 0, "pages": 0, "suggested": ""}
    dl = page.locator("text=/全文下載/")
    if dl.count() == 0:
        body = page.inner_text("body")
        res["reason"] = ("no 全文下載 control — "
                         + ("looks like a login/entitlement page" if "登入" in body[:200]
                            else "article page may not offer full text"))
        return res
    try:
        dl.first.click(timeout=10000)
        time.sleep(2)
    except Exception as e:
        res["reason"] = f"could not open the download modal: {type(e).__name__}"
        return res
    ok = page.locator("#_TextDownloadWindow_ok")
    if ok.count() == 0:
        res["reason"] = "confirm modal never appeared (is the cookie banner still up?)"
        return res
    try:
        with page.expect_download(timeout=wait_s * 1000) as info:
            ok.first.click(timeout=10000)
        d = info.value
    except Exception as e:
        res["reason"] = f"no download within {wait_s}s ({type(e).__name__})"
        return res
    res["suggested"] = d.suggested_filename
    out.parent.mkdir(parents=True, exist_ok=True)
    d.save_as(str(out))
    head = out.read_bytes()[:4]
    res["bytes"] = out.stat().st_size
    if head != b"%PDF":
        res["reason"] = f"not a PDF (starts {head!r})"
        return res
    import fitz
    doc = fitz.open(str(out))
    res["pages"] = doc.page_count
    doc.close()
    if res["pages"] < 1:
        res["reason"] = "PDF has no pages"
        return res
    res["ok"] = True
    return res


_HIT_RE = re.compile(r"var docID = '([^']+)';\s*\n?\s*var title = '((?:[^'\\]|\\.)*)'")


def search(page, term: str, limit: int = 10) -> list[dict]:
    """Drive Airiti's own search box.

    Two things force this shape: a hand-built `/Search/Index?SearchText=…` URL returns the
    site's "page not found" (the real result page is `/Article/Query?queryString={…JSON…}`,
    built by the page's JS), and the result titles are not plain links — each row's DocID
    lives in an inline script (`var docID = '…'; var title = '…';`), which is what we read.
    """
    if not open_article(page, f"https://{proxy_host()}/"):
        return []
    dismiss_banner(page)
    box = page.locator("input.searchSite, input[placeholder*='ISSN']").first
    if box.count() == 0:
        _log("no search box on the Airiti home page")
        return []
    box.fill(term)
    box.press("Enter")
    for _ in range(12):
        time.sleep(1)
        if "/Article/Query" in page.url:
            break
    time.sleep(3)
    dismiss_banner(page)
    html = page.content()
    out, seen = [], set()
    for docid, title in _HIT_RE.findall(html):
        if docid in seen:
            continue
        seen.add(docid)
        out.append({"docid": docid, "title": title.replace("\\'", "'")})
        if len(out) >= limit:
            break
    return out


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def cmd_fetch(argv) -> int:
    t0 = time.time()
    as_json = "--json" in argv
    direct = "--direct" in argv
    args = [a for a in argv if not a.startswith("--")]
    ident, out = args[0], Path(args[1])
    env = {"schema": "airiti.fetch/1", "id": ident, "ok": False, "path": None,
           "bytes": 0, "pages": 0, "reason": ""}
    url = doi_url(ident, direct) if DOI_RE.match(ident) else article_url(ident, direct)
    _log(f"opening {url}")
    with ls.profile_lock(f"airiti fetch {ident[:30]}"):
        from patchright.sync_api import sync_playwright
        with sync_playwright() as pw:
            ctx = ls._new_context(pw, headless=False)   # the gate interstitial needs a window
            ls.restore_session(ctx)
            page = ctx.new_page()
            page.on("dialog", lambda d: d.dismiss())
            try:
                if not open_article(page, url):
                    env["reason"] = "could not get past the library gate (run: library_session.py login)"
                else:
                    _log(f"page: {page.title()[:70]}")
                    dismiss_banner(page)
                    r = fetch(page, out)
                    env.update(ok=r["ok"], bytes=r["bytes"], pages=r["pages"],
                               reason=r["reason"], suggested=r["suggested"])
                    if r["ok"]:
                        env["path"] = str(out)
                        env["sha256"] = _sha256(out)
            finally:
                ctx.close()
    env["elapsed_s"] = round(time.time() - t0, 1)
    if as_json:
        print(json.dumps(env, ensure_ascii=False))
    elif env["ok"]:
        print(f"OK  {out}  {env['bytes']:,} bytes / {env['pages']} pages")
    else:
        print(f"FAILED: {env['reason']}")
    return 0 if env["ok"] else 2


def cmd_search(argv) -> int:
    as_json = "--json" in argv
    limit = 10
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])
    term = [a for a in argv if not a.startswith("--")][0]
    with ls.profile_lock("airiti search"):
        from patchright.sync_api import sync_playwright
        with sync_playwright() as pw:
            ctx = ls._new_context(pw, headless=False)
            ls.restore_session(ctx)
            page = ctx.new_page()
            try:
                hits = search(page, term, limit)
            finally:
                ctx.close()
    if as_json:
        print(json.dumps({"schema": "airiti.search/1", "term": term, "hits": hits},
                         ensure_ascii=False))
        return 0 if hits else 2
    for h in hits:
        print(f"{h['docid']}\n     {h['title']}")
    print(f"\n{len(hits)} hit(s)")
    return 0 if hits else 2


def main(argv) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, rest = argv[0], argv[1:]
    if cmd == "fetch":
        if len([a for a in rest if not a.startswith("--")]) < 2:
            print("usage: fetch <DocID|DOI> <out.pdf> [--json] [--direct]")
            return 1
        return cmd_fetch(rest)
    if cmd == "search":
        if not rest:
            print('usage: search "<term>" [--limit N] [--json]')
            return 1
        return cmd_search(rest)
    print(f"unknown command: {cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
