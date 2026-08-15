#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""NDLTD — Taiwan's national thesis system (臺灣博碩士論文知識加值系統, ndltd.ncl.edu.tw).

Theses have no DOI, so they never fit `library_session.py`'s DOI route table. This module
is the separate entry point: search by title/author/keyword, then pull the authorised
full text as ONE merged PDF.

    python ndltd.py login                        # sign in (or confirm the cookie is alive)
    python ndltd.py check                        # is the stored session still signed in?
    python ndltd.py search "居家復能" --field ti  # list records + their thesis IDs
    python ndltd.py fetch 109CGU05712004 out.pdf # by thesis ID (stable, preferred)
    python ndltd.py fetch --title "長照2.0居家復能…" out.pdf

What this site does that nothing else in this repo does (all four are load-bearing):

1. A load-shedding gate. Under load ANY navigation can turn into a "驗證碼檢查機制" page.
   It is not a session expiry — but it looks exactly like one, because failing it bounces
   you back to the homepage. Every navigation therefore re-checks for the gate.
2. A path-borne session: `/cgi-bin/gs32/gsweb.cgi/ccd=<TOKEN>/…`. Landing on the homepage
   mints a NEW ccd, and hand-built ccd URLs are dead on arrival. Drive the site's own
   links instead.
3. The login is bound to that ccd unless 保持我的登入狀態 is ticked — tick it, so the
   login rides a cookie and survives the next ccd.
4. The download sits behind a copyright declaration popup with its OWN captcha. Clicking
   我同意 without solving it fires a JS alert, which automation sees as silence.

The payload is normally a WinZip of per-chapter PDFs (01.pdf, 02.pdf …), which this
merges back into one document in filename order.

Credentials come from the same secret store as the rest of the repo (see README):
`NDLTD_USER` / `NDLTD_PASS` — a free member account at ndltd.ncl.edu.tw.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import random
import re
import sys
import time
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin

from library_session import secret_get                     # pluggable secret store
from paper_config import CFG

SITE = "https://ndltd.ncl.edu.tw/"
PROFILE_DIR = Path.home() / ".ndltd_profile"
LOCK_FILE = PROFILE_DIR / ".session.lock"
LOCK_WAIT_S = int(os.environ.get("NDLTD_LOCK_WAIT_S", "600"))
LOCK_STALE_S = 1800
NAV_TIMEOUT_MS = 45000
GATE_TRIES = 6
LOGIN_TRIES = 4
DECLARE_TRIES = 5
MIN_REQUEST_INTERVAL_S = int(CFG["rate"]["min_interval_s"])

_ocr = None


# --- small helpers --------------------------------------------------------
def _log(msg: str) -> None:
    print(f"[ndltd] {msg}", file=sys.stderr, flush=True)


def ocr(png: bytes) -> str:
    """Offline captcha OCR. NDLTD's codes are alphanumeric and case-insensitive, so
    (unlike library_session.solve_captcha) do NOT strip letters."""
    global _ocr
    if _ocr is None:
        import ddddocr
        _ocr = ddddocr.DdddOcr(show_ad=False)
    return _ocr.classification(png).strip()


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError) as e:
        return isinstance(e, PermissionError)


class profile_lock:
    """The chromium profile is exclusive — serialize runs instead of racing them."""

    def __init__(self, label: str = ""):
        self.label = label

    def __enter__(self):
        PROFILE_DIR.mkdir(exist_ok=True)
        deadline = time.time() + LOCK_WAIT_S
        while True:
            try:
                fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, json.dumps({"pid": os.getpid(), "ts": time.time(),
                                         "label": self.label}).encode())
                os.close(fd)
                return self
            except FileExistsError:
                holder = {}
                try:
                    holder = json.loads(LOCK_FILE.read_text() or "{}")
                except Exception:
                    pass
                stale = (time.time() - holder.get("ts", 0) > LOCK_STALE_S
                         or not _pid_alive(int(holder.get("pid", -1))))
                if stale:
                    _log(f"stale lock from pid {holder.get('pid')} — taking it")
                    LOCK_FILE.unlink(missing_ok=True)
                    continue
                if time.time() > deadline:
                    sys.exit(4)     # same contract as library_session: 4 = profile busy
                time.sleep(3)

    def __exit__(self, *exc):
        LOCK_FILE.unlink(missing_ok=True)
        return False


def _throttle() -> None:
    if MIN_REQUEST_INTERVAL_S > 0:
        time.sleep(random.uniform(1.0, 2.5))


# --- gate / navigation ----------------------------------------------------
def at_gate(page) -> bool:
    return (page.locator("input[name=check]").count() > 0
            and page.locator("#validinput").count() > 0)


def pass_gate(page, tries: int = GATE_TRIES) -> bool:
    """Clear the load-shedding captcha gate if we landed on it."""
    for i in range(1, tries + 1):
        if not at_gate(page):
            return True
        img = page.locator("img[alt=check_image]").first
        code = ocr(page.request.get(urljoin(page.url, img.get_attribute("src"))).body())
        _log(f"gate try {i}: {code!r}")
        page.fill("#validinput", code)
        page.click("input[name=check]")
        page.wait_for_load_state("domcontentloaded")
        time.sleep(1.5)
    return not at_gate(page)


def go(page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded")
    time.sleep(1.2)
    pass_gate(page)


def logged_in(page) -> bool:
    try:
        return "登出" in page.inner_text("body")
    except Exception:
        return False


def new_context(pw, headless: bool = True):
    PROFILE_DIR.mkdir(exist_ok=True)
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR), headless=headless,
        viewport={"width": 1280, "height": 900}, accept_downloads=True)
    ctx.set_default_navigation_timeout(NAV_TIMEOUT_MS)
    return ctx


# --- login ----------------------------------------------------------------
def login(page) -> bool:
    """Sign in with 保持我的登入狀態 ticked so the session outlives the ccd token."""
    user = secret_get("NDLTD_USER")
    pw_ = secret_get("NDLTD_PASS")
    for attempt in range(1, LOGIN_TRIES + 1):
        go(page, SITE)
        if logged_in(page):
            _log("already signed in (cookie held)")
            return True
        link = page.locator("a[href*='registry']")
        if link.count() == 0:
            _log(f"no 登入 link at {page.url}")
            time.sleep(2)
            continue
        link.first.click()
        page.wait_for_load_state("domcontentloaded")
        time.sleep(1.2)
        pass_gate(page)
        if page.locator("#id").count() == 0:
            continue
        page.fill("#id", user)
        page.fill("#ps", pw_)
        try:
            box = page.locator("#fb32cookieid")
            if box.count() and not box.is_checked():
                box.check(force=True)
        except Exception as e:
            _log(f"keep-signed-in checkbox: {e!r}")
        img = page.locator("img[alt='驗證碼']").first
        if img.count():
            code = ocr(page.request.get(urljoin(page.url, img.get_attribute("src"))).body())
            _log(f"login try {attempt}: captcha {code!r}")
            page.fill("#validinput", code)
        page.click("#button")
        page.wait_for_load_state("domcontentloaded")
        time.sleep(2.0)
        pass_gate(page)
        if logged_in(page):
            _log("signed in")
            return True
        body = page.inner_text("body")
        hint = " | ".join(l.strip() for l in body.splitlines()
                          if any(k in l for k in ("驗證", "錯誤", "失敗", "不正確", "無效")))[:200]
        _log(f"login attempt {attempt} rejected: {hint or 'no message'}")
    return False


def _ccd(url: str) -> str | None:
    m = re.search(r"/ccd=([^/]+)/", url)
    return m.group(1) if m else None


def _in_session(page, path: str) -> str:
    """Build a URL inside the CURRENT ccd session (never hand-build a fresh one)."""
    ccd = _ccd(page.url)
    if not ccd:
        raise RuntimeError(f"no ccd token in {page.url}")
    return f"{SITE.rstrip('/')}/cgi-bin/gs32/gsweb.cgi/ccd={ccd}/{path}"


# --- search ---------------------------------------------------------------
FIELDS = {"ti": "ti_論文名稱", "au": "au_研究生", "ad": "ad_指導教授",
          "kw": "kw_關鍵詞", "ab": "ab_摘要", "all": "ALLFIELD_不限欄位"}


def search(page, term: str, field: str = "ti") -> list[dict]:
    """Run a simple search from inside the logged-in session and parse the hit list."""
    link = page.locator("a:has-text('簡易查詢')")
    if link.count() == 0:
        go(page, SITE)               # not signed in yet — the homepage IS the form
    else:
        link.first.click()
        page.wait_for_load_state("domcontentloaded")
        time.sleep(1.5)
        pass_gate(page)
    if page.locator("#ysearchinput0").count() == 0:
        raise RuntimeError(f"no search form at {page.url}")
    box_id = FIELDS.get(field, FIELDS["ti"])
    try:
        box = page.locator(f"#{box_id}")
        if box.count() and not box.is_checked():
            box.check(force=True)
        allf = page.locator("#ALLFIELD_不限欄位")
        if box_id != "ALLFIELD_不限欄位" and allf.count() and allf.is_checked():
            allf.uncheck(force=True)
    except Exception as e:
        _log(f"field toggle: {e!r}")
    page.fill("#ysearchinput0", term)
    page.click("#gs32search")
    try:
        page.wait_for_load_state("networkidle", timeout=25000)
    except Exception:
        pass
    time.sleep(2.0)
    pass_gate(page)
    return parse_results(page)


def parse_results(page) -> list[dict]:
    return page.evaluate("""() => {
      const out = [];
      for (const td of document.querySelectorAll('td.tdfmt1-content')) {
        const a = td.querySelector('a.slink');
        if (!a) continue;
        const lines = td.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
        const title = (a.innerText || '').trim();
        const meta = lines.find(l => l.includes('／')) || '';
        const p = meta.split('／');
        const author = (lines.find(l => l.startsWith('研究生:')) || '').replace('研究生:', '').trim();
        const advisor = (lines.find(l => l.startsWith('指導教授:')) || '').replace('指導教授:', '').trim();
        // The thesis ID rides in a link's onclick — but only while signed OUT; once signed
        // in the confirm-login wrapper (which carried it) is gone. Best effort here; the
        // record page always has it (see record_meta).
        const m = td.innerHTML.match(/id(?:%3D|=)(?:%22|")([0-9]{3}[A-Za-z0-9_.-]{4,})(?:%22|")/);
        const id = m ? m[1] : null;
        out.push({
          id, title,
          school: p[0] || '', dept: p[1] || '', acad_year: p[2] || '', degree: p[3] || '',
          author, advisor,
          fulltext: !!td.querySelector("img[alt='電子全文']"),
          record_href: a.getAttribute('href'),
        });
      }
      return out;
    }""")


ID_RE = re.compile(r"^[0-9]{3}[A-Za-z0-9_.-]{4,}$")


def record_meta(page) -> dict:
    """Read the bibliography off an open record page, including the two stable IDs
    (thesis ID and the handle.net permanent link)."""
    # ⚠ Do NOT grab the first id=… in the page: a record page is full of 相關論文 links
    # that carry OTHER theses' IDs, and the first match is usually one of those. Only
    # these two anchors belong to the record itself (this record's cite/report buttons and
    # its own permanent-link box).
    return page.evaluate("""() => {
      const html = document.documentElement.innerHTML;
      const idm = html.match(/(?:refformat\\?dbid=|ThesisID=)([0-9]{3}[A-Za-z0-9_.-]{4,})/);
      const perm = document.querySelector('#fe_text1');
      const hm = (perm && perm.value || html).match(/hdl\\.handle\\.net\\/([0-9]+\\/[A-Za-z0-9]+)/);
      const text = document.body.innerText.split('\\n').map(s => s.trim());
      const pick = (label) => {
        for (let i = 0; i < text.length; i++) {
          if (text[i] === label || text[i] === label + ':') {
            for (let j = i + 1; j < Math.min(i + 4, text.length); j++)
              if (text[j]) return text[j];
          }
          if (text[i].startsWith(label + ':')) return text[i].slice(label.length + 1).trim();
        }
        return '';
      };
      return {
        id: idm ? idm[1] : null,
        handle: hm ? 'https://hdl.handle.net/' + hm[1] : null,
        title: pick('論文名稱'), author: pick('研究生'), advisor: pick('指導教授'),
        school: pick('校院名稱'), dept: pick('系所名稱'), acad_year: pick('畢業學年度'),
        year: pick('論文出版年'), pages_declared: pick('論文頁數'),
      };
    }""")


# --- full text ------------------------------------------------------------
def open_by_id(page, thesis_id: str) -> bool:
    """Jump straight to a record by its thesis ID, inside the current ccd session."""
    go(page, _in_session(page, f'search?s=id="{thesis_id}".&openfull=1&setcurrent=0'))
    if page.locator("a:has-text('電子全文')").count() and "/record" in page.url:
        return True
    rec = page.locator("a[href*='/record?']")
    if rec.count():
        rec.first.click()
        page.wait_for_load_state("domcontentloaded")
        time.sleep(1.5)
        pass_gate(page)
        return True
    return False


def open_record(page, rec: dict) -> bool:
    """Open a record page, preferring the stable thesis ID over the positional link."""
    if rec.get("id") and open_by_id(page, rec["id"]):
        return True
    href = rec.get("record_href")
    if not href:
        return False
    go(page, urljoin(page.url, href))
    return True


def _declare_url(page) -> str | None:
    return page.evaluate("""() => {
      for (const a of document.querySelectorAll('a')) {
        const m = (a.getAttribute('onclick') || '').match(/window\\.open\\('([^']*fulltextdeclare[^']*)'/);
        if (m) return m[1]; }
      return null; }""")


def fetch_fulltext(ctx, page, out: Path, keep_zip: bool = False) -> dict:
    """Record page → declaration (own captcha) → file. Returns a result dict."""
    res = {"ok": False, "reason": "", "bytes": 0, "pages": 0, "parts": 0, "container": ""}
    href = _declare_url(page)
    if not href:
        body = page.inner_text("body")
        res["reason"] = ("校內電子全文" if "校內" in body else
                         "no authorised electronic full text (paper copy only)")
        return res
    pop = ctx.new_page()
    pop.on("dialog", lambda d: (_log(f"dialog: {d.message[:60]}"), d.dismiss()))
    file_url = None
    try:
        for attempt in range(1, DECLARE_TRIES + 1):
            pop.goto(urljoin(page.url, href), wait_until="domcontentloaded")
            time.sleep(1.5)
            pass_gate(pop)
            img = pop.locator("img[alt='驗證碼'], img[src*='random_validationimgs']").first
            if img.count():
                code = ocr(pop.request.get(urljoin(pop.url, img.get_attribute("src"))).body())
                _log(f"declaration try {attempt}: captcha {code!r}")
                pop.fill("#validinput", code)
            pop.locator("input[name=ok]").click(timeout=10000)
            pop.wait_for_load_state("domcontentloaded")
            time.sleep(2.5)
            links = pop.evaluate("""() => [...document.querySelectorAll('a')]
                .map(a => a.getAttribute('href'))
                .filter(h => h && h.includes('brwfull'))""")
            if links:
                file_url = urljoin(pop.url, links[0])
                break
            _log(f"declaration not accepted: {pop.inner_text('body')[:120]}")
        if not file_url:
            res["reason"] = "declaration step never yielded a download link"
            return res
        _throttle()
        resp = pop.request.get(file_url, headers={"Referer": pop.url}, timeout=180000)
        body = resp.body()
    finally:
        try:
            pop.close()
        except Exception:
            pass
    if resp.status != 200 or not body:
        res["reason"] = f"download HTTP {resp.status}, {len(body or b'')} bytes"
        return res
    return _materialise(body, out, res, keep_zip)


def _materialise(body: bytes, out: Path, res: dict, keep_zip: bool) -> dict:
    """The payload is a WinZip of per-chapter PDFs (or, rarely, a bare PDF)."""
    import fitz
    out.parent.mkdir(parents=True, exist_ok=True)
    if body[:4] == b"%PDF":
        res["container"] = "pdf"
        out.write_bytes(body)
    elif body[:2] == b"PK":
        res["container"] = "zip"
        zf = zipfile.ZipFile(io.BytesIO(body))
        names = sorted(n for n in zf.namelist() if n.lower().endswith(".pdf"))
        if not names:
            res["reason"] = f"zip holds no PDF: {zf.namelist()[:10]}"
            return res
        if keep_zip:
            out.with_suffix(".zip").write_bytes(body)
        merged = fitz.open()
        for n in names:
            part = fitz.open(stream=zf.read(n), filetype="pdf")
            merged.insert_pdf(part)
            part.close()
        merged.save(str(out))
        merged.close()
        res["parts"] = len(names)
    else:
        res["reason"] = f"unexpected payload (first bytes {body[:8]!r})"
        return res
    doc = fitz.open(str(out))
    res["pages"] = doc.page_count
    doc.close()
    res["bytes"] = out.stat().st_size
    if out.read_bytes()[:4] != b"%PDF" or res["pages"] < 2:
        res["reason"] = f"validation failed ({res['bytes']} bytes, {res['pages']} pages)"
        return res
    res["ok"] = True
    return res


# --- commands -------------------------------------------------------------
def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def cmd_search(argv) -> int:
    term = argv[0]
    field = "ti"
    as_json = "--json" in argv
    if "--field" in argv:
        field = argv[argv.index("--field") + 1]
    with profile_lock("search"):
        from patchright.sync_api import sync_playwright
        with sync_playwright() as pw:
            ctx = new_context(pw)
            page = ctx.new_page()
            try:
                login(page)          # search works signed out; full text does not
                hits = search(page, term, field)
            finally:
                ctx.close()
    if as_json:
        print(json.dumps({"schema": "ndltd.search/1", "term": term, "field": field,
                          "count": len(hits), "hits": hits}, ensure_ascii=False))
        return 0 if hits else 2
    if not hits:
        print("no hits")
        return 2
    for h in hits:
        mark = "📄" if h["fulltext"] else "  "
        print(f"{mark} {h['id'] or '?'}  {h['title']}")
        print(f"     {h['author']} / {h['school']} {h['dept']} / {h['acad_year']} "
              f"/ 指導教授 {h['advisor']}")
    print(f"\n{len(hits)} hit(s); 📄 = authorised electronic full text")
    return 0


def cmd_fetch(argv) -> int:
    t0 = time.time()
    as_json = "--json" in argv
    keep_zip = "--keep-zip" in argv
    args = [a for a in argv if not a.startswith("--")]
    by_title = "--title" in argv
    term, out = args[0], Path(args[1])
    env = {"schema": "ndltd.fetch/1", "query": term, "ok": False, "path": None,
           "bytes": 0, "pages": 0, "record": None, "reason": ""}
    with profile_lock("fetch"):
        from patchright.sync_api import sync_playwright
        with sync_playwright() as pw:
            ctx = new_context(pw)
            page = ctx.new_page()
            try:
                if not login(page):
                    env["reason"] = "login failed (check NDLTD_USER / NDLTD_PASS)"
                else:
                    opened = False
                    if not by_title and ID_RE.match(term):
                        go(page, SITE)                     # need a live ccd to build on
                        opened = open_by_id(page, term)
                        if not opened:
                            env["reason"] = f"no record for thesis ID {term}"
                    else:
                        hits = search(page, term, "ti")
                        if not hits:
                            env["reason"] = "no record matched that title"
                        else:
                            if len(hits) > 1:
                                _log(f"⚠ {len(hits)} hits — taking the first; "
                                     f"pass a thesis ID to be exact")
                            if not hits[0]["fulltext"]:
                                env["reason"] = ("record has no authorised electronic "
                                                 "full text (paper copy only)")
                            else:
                                opened = open_record(page, hits[0])
                                if not opened:
                                    env["reason"] = "could not open the record page"
                    if opened:
                        env["record"] = record_meta(page)
                        _log(f"record: {env['record'].get('title', '')[:60]} "
                             f"({env['record'].get('id')})")
                        r = fetch_fulltext(ctx, page, out, keep_zip)
                        env.update(ok=r["ok"], bytes=r["bytes"], pages=r["pages"],
                                   reason=r["reason"], parts=r["parts"],
                                   container=r["container"])
                        if r["ok"]:
                            env["path"] = str(out)
                            env["sha256"] = _sha256(out)
            finally:
                ctx.close()
    env["elapsed_s"] = round(time.time() - t0, 1)
    if as_json:
        print(json.dumps(env, ensure_ascii=False))
    elif env["ok"]:
        how = (f"{env['parts']} chapter files merged" if env.get("parts")
               else "single PDF, no merge needed")
        print(f"OK  {out}  {env['bytes']:,} bytes / {env['pages']} pages ({how})")
    else:
        print(f"FAILED: {env['reason']}")
    return 0 if env["ok"] else 2


def cmd_session(cmd: str) -> int:
    with profile_lock(cmd):
        from patchright.sync_api import sync_playwright
        with sync_playwright() as pw:
            ctx = new_context(pw)
            page = ctx.new_page()
            try:
                if cmd == "check":
                    go(page, SITE)
                    ok = logged_in(page)
                    print("session: VALID" if ok else "session: EXPIRED (run: login)")
                    return 0 if ok else 2
                ok = login(page)
                print("login: OK" if ok else "login: FAILED")
                return 0 if ok else 2
            finally:
                ctx.close()


def main(argv) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, rest = argv[0], argv[1:]
    if cmd in ("login", "check"):
        return cmd_session(cmd)
    if cmd == "search":
        if not rest:
            print('usage: search "<term>" [--field ti|au|ad|kw|ab|all] [--json]')
            return 1
        return cmd_search(rest)
    if cmd == "fetch":
        if len([a for a in rest if not a.startswith("--")]) < 2:
            print('usage: fetch <thesis-id> <out.pdf> [--json] [--keep-zip]\n'
                  '       fetch --title "<title>" <out.pdf>')
            return 1
        return cmd_fetch(rest)
    print(f"unknown command: {cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
