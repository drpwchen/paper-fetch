# Chinese-language sources — NDLTD theses and Airiti journal articles

The DOI route ladder in `library_session.py` covers international publishers. Two Taiwanese
platforms fall outside it and each gets its own module:

| Module | Platform | Addressed by | Needs |
|---|---|---|---|
| `ndltd.py` | 臺灣博碩士論文知識加值系統 (ndltd.ncl.edu.tw) | title / author / thesis ID | a free member account |
| `airiti.py` | 華藝線上圖書館 Airiti Library | Airiti DocID or DOI | your library's Airiti subscription |

Both drive the same stealth-chromium stack as `library_session.py` and use the same secret
store, so nothing new to install beyond `pymupdf`.

---

## `ndltd.py` — Taiwanese master's and doctoral theses

Theses have no DOI, and NDLTD's authorised full texts are free — the blocker is never
entitlement, it is the site's machinery.

```bash
python ndltd.py login                          # sign in once; the cookie persists
python ndltd.py search "居家復能" --field ti     # 📄 marks records with full text
python ndltd.py fetch 109CGU05712004 out.pdf   # by thesis ID (stable — prefer this)
python ndltd.py fetch --title "長照2.0居家復能與常規照顧服務…" out.pdf
```

Store the credentials for a free account at ndltd.ncl.edu.tw under `NDLTD_USER` /
`NDLTD_PASS` (same store as every other secret here — see the README).

### The four things that break naive automation

1. **A load-shedding captcha gate.** Under load, *any* navigation can return a 驗證碼檢查機制
   page instead of what you asked for. It is not a session expiry, but failing it dumps you
   back on the homepage, so it reads exactly like one. Every navigation re-checks for it
   (`pass_gate`), and ddddocr clears it first try in practice.
2. **A path-borne session token**: `/cgi-bin/gs32/gsweb.cgi/ccd=<TOKEN>/…`. Landing on the
   homepage mints a *new* ccd; hand-built ccd URLs are dead on arrival. Always follow the
   site's own links (`_in_session` builds URLs from the live token).
3. **Login is bound to that ccd** unless 保持我的登入狀態 is ticked. Tick it — otherwise the
   next search silently runs as an anonymous user, and the full-text link turns into a
   "please log in first" JS confirm rather than a download.
4. **The download sits behind a copyright declaration popup with its own captcha.** Clicking
   我同意 without solving it fires a JS alert, which automation experiences as silence.

### What you get back

The payload is normally a WinZip of per-chapter PDFs (`01.pdf`, `02.pdf`, …), occasionally a
single PDF. `ndltd.py` merges the chapters in filename order into one document, verifies the
`%PDF` header and the page count, and reports both. `--keep-zip` also writes the original
container next to the output.

Records with `校內電子全文` (campus-only) or paper-only records have no declaration link;
the tool says so rather than writing a broken file.

---

## `airiti.py` — Chinese journal articles

```bash
python airiti.py fetch 10232141-202204-202205090002-202205090002-170-188 out.pdf
python airiti.py fetch "10.6288/TJPH.202204_41(2).110135" out.pdf   # DOI works too
python airiti.py search "居家復能" --limit 10
```

### Entitlement — do not use `holdings.py` here

Airiti is usually a **database-level** subscription, so it does not appear in the A–Z
e-journal list that `holdings.sqlite` is built from. A journal missing from holdings tells
you nothing about Airiti access. Check your library's *database* list instead.

### Three silent failure modes

1. **The cookie-consent banner must be dismissed, and a normal click cannot do it.**
   Playwright reports its 我知道了 span as not visible, so `.click()` raises; wrap that in the
   usual try/except and you now have an invisible banner swallowing every later click — the
   confirm button does nothing, with no error and no network request. Dismiss it with an
   in-page JS click (`dismiss_banner`).
2. **The PDF arrives as a blob download, not a readable response body.** The site XHRs
   `POST /Article/TextDownloadNew` (`Application/octet-stream`), wraps the bytes in a Blob
   and triggers a browser download. Watching for a `%PDF` response body sees nothing at all;
   catch the download event, and allow ~15 s for the server to answer.
3. **Use the query form of the article URL** — `/Article/Detail?DocID=…`. The path form
   `/Article/Detail/…` renders a page that looks identical but carries a different
   entitlement state.

### A lesson worth stating plainly

An earlier attempt at this route concluded "the library must not subscribe to these
journals" because four articles failed under automation while the user downloaded the same
four by hand, minutes apart, on the same machine. That conclusion was wrong: the cause was
trap 1 plus trap 2, and the same four articles now download byte-for-byte identically to the
manual copies. **"My automation cannot get it" is not evidence about your institution's
entitlements.** Confirm subscriptions against the library's own database list, never by
inference from a failed script.
