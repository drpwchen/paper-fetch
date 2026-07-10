#!/usr/bin/env python3
"""Institutional library auto-session — SKELETON / reference implementation.

⚠ This public edition ships the *architecture* and the generic scaffolding (config,
secret store, access log, throttle, rate stats, proxy host-rewrite, response classifier,
publisher route map, two-phase orchestration). The two pieces that are inherently
specific to YOUR institution / a given publisher are left as documented stubs for you to
implement against your own library:

    * login()          — your remote-auth login form + CAPTCHA handling
    * _lww_ovid_pdf()  — the LWW/Ovid multi-step signed-URL flow (one worked example,
                         described in full in its docstring so you can reproduce it)

It ships NO institution's endpoints (fill config.yaml) and NO credentials (use your own
in a local secret store). Nothing here works until you supply your own library access.
This is a tool for people who ALREADY have legitimate subscription access — it automates
your own authenticated session, it does not bypass a paywall or share anyone's account.

────────────────────────────────────────────────────────────────────────────────────────
How institutional off-campus full text works (the pattern most EZproxy / NetScaler +
rewriting-proxy libraries follow), so an agent/reader can adapt this to any library:

  A library usually has TWO separate remote systems:
    - an e-resource / SFX link-resolver portal (lookup only), and
    - a remote-reader authentication gate (often Citrix NetScaler + a URL-rewriting proxy)
      that actually authorizes publisher sites when you're off-campus.

  Off-campus, the publisher host is rewritten to a proxy domain: dots become dashes and
  your proxy suffix is appended —
        onlinelibrary.wiley.com  →  onlinelibrary-wiley-com.<proxy_suffix>
  The lookup portal does NOT authorize publisher sites; the remote-auth gate does, via a
  wildcard proxy-authorization cookie set on login.

  Fetching is TWO-PHASE for the best UX:
    Phase 1 (headless, silent): try API/OA/TDM first (paper_fetch.py); else an
      authenticated proxy request reusing any valid Cloudflare clearance in the profile.
    Phase 2 (headful, only if phase 1 hit a Cloudflare challenge): open a real browser
      window, let the CF JS challenge refresh the proxy-domain clearance, then request the
      PDF. The clearance persists in the browser profile and is reused silently by phase 1
      until it expires — so a window only appears occasionally.

  Session persistence: the remote-auth session cookie and the proxy-authorization cookie
  are session-only (Chromium won't write them to disk). Save them to your secret store
  after login and re-inject next run, so the session survives browser close/reboot.

  Rate awareness: log every proxy request; `stats` summarizes successes vs Cloudflare /
  rate / auth blocks so you can learn the real throttle ceiling empirically. A courtesy
  delay (rate.min_interval_s) is enforced between proxy requests.
────────────────────────────────────────────────────────────────────────────────────────

Credentials (store once in a DPAPI CurrentUser store; values never touch chat or config):
    powershell -File ~/.secrets/secret.ps1 set LIB_USER
    powershell -File ~/.secrets/secret.ps1 set LIB_PASS

CLI:
    python library_session.py check                    # is the stored session valid?
    python library_session.py login                    # force a fresh login
    python library_session.py fetch <DOI> <out.pdf>    # two-phase full-text download
    python library_session.py stats                    # access-log summary / block analysis

Windows-only as written (DPAPI is user-bound).

SERIAL BY DESIGN. The chromium profile is exclusive, so two concurrent `fetch`/`login`/
  `check` runs cannot both drive it. This is enforced by a cross-process lock: a second
  caller queues, then gives up with exit 4 instead of hanging. Do not fan `fetch` out
  across parallel workers/agents — acquire full text in a serial pre-pass, then hand the
  resulting PDF paths to whatever consumes them.

Bounded failure. Login/CAPTCHA and proxy interstitials can hang indefinitely; a watchdog
  (PAPERFETCH_TIMEOUT_S, default 240s) aborts and tree-kills its own chromium so no browser
  is orphaned. Never wrap this script in a bare `timeout` — that kills the parent only and
  leaks the browser.

Exit codes: 0 ok · 1 usage · 2 fetch/auth failed · 4 profile busy (lock) · 5 watchdog abort.
  4 and 5 mean "retry serially", NOT "no full text available".
"""
from __future__ import annotations

import base64
import contextlib
import ctypes
import ctypes.wintypes as wt
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from paper_config import CFG, require

# --- config ---------------------------------------------------------------
SECRETS_DIR = Path.home() / ".secrets"
PROFILE_DIR = Path.home() / ".paperfetch_profile"   # patchright (stealth) chromium profile
ACCESS_LOG = PROFILE_DIR / "access_log.jsonl"

REMOTE_AUTH = (CFG["institution"]["remote_auth_base"] or "").rstrip("/")
REMOTE_AUTH_DOMAIN = urlsplit(REMOTE_AUTH).netloc if REMOTE_AUTH else ""
PROXY_SUFFIX = CFG["institution"]["proxy_suffix"]
SFX = CFG["institution"]["sfx_base"]

CAPTCHA_MAX_TRIES = 6
NAV_TIMEOUT_MS = 30000
MIN_REQUEST_INTERVAL_S = int(CFG["rate"]["min_interval_s"])
FEEDBACK_CONTACT = CFG["rate"]["contact"]
PAPER_FETCH = Path(__file__).with_name("paper_fetch.py")

# Cross-process lock: the chromium profile is exclusive (a second launch throws
# TargetClosedError / hangs on the proxy interstitial). Serialize instead of racing.
LOCK_FILE = PROFILE_DIR / ".session.lock"
LOCK_WAIT_S = int(os.environ.get("PAPERFETCH_LOCK_WAIT_S", "900"))   # queue this long, then bail
LOCK_STALE_S = 1800                                                  # holder older than this = crashed

# Hard wall-clock ceiling: an unbounded hang can burn a caller's whole budget.
WATCHDOG_S = int(os.environ.get("PAPERFETCH_TIMEOUT_S", "240"))

if MIN_REQUEST_INTERVAL_S <= 0:
    sys.stderr.write(
        "⚠⚠ rate.min_interval_s=0 — courtesy delay DISABLED. Systematic/bulk download can\n"
        "   get your entire institution's IP blocked by the publisher (everyone loses access,\n"
        "   not just you). Only do this for a handful of papers, and stop if `stats` shows a\n"
        "   block. You own the consequences.\n")

# proxy-authorization cookie is typically wildcarded on the remote-auth parent domain
_PARENT_DOMAIN = "." + ".".join(REMOTE_AUTH_DOMAIN.split(".")[-3:]) if REMOTE_AUTH_DOMAIN else ""
PERSIST_COOKIES = {
    # name -> exact cookie domain. Adjust BOTH names to your proxy's actual cookies (find
    # them in devtools after logging in): the session cookie on the remote-auth host, and
    # the proxy-authorization cookie wildcarded on the parent domain.
    "sessionid": REMOTE_AUTH_DOMAIN,
    "proxy-auth": _PARENT_DOMAIN,
}

# DOI-prefix -> (publisher host, pdf path template) | None (no simple template → resolver).
# The publisher hosts + PDF path templates below are PUBLIC (same for everyone); only the
# proxy suffix that gets appended is institution-specific (from config.yaml). This map is
# the reusable, valuable part — extend it as you verify more publishers.
PROVIDER_ROUTES = {
    "10.1002": ("onlinelibrary.wiley.com", "/doi/pdfdirect/{doi}?download=true"),   # Wiley
    "10.1111": ("onlinelibrary.wiley.com", "/doi/pdfdirect/{doi}?download=true"),   # Wiley
    "10.1007": ("link.springer.com", "/content/pdf/{doi}.pdf"),                     # Springer
    "10.1186": ("link.springer.com", "/content/pdf/{doi}.pdf"),                     # BMC (per-journal host; often 404)
    "10.1056": ("www.nejm.org", "/doi/pdf/{doi}"),                                  # NEJM (unverified template)
    "10.1177": ("journals.sagepub.com", "/doi/pdf/{doi}?download=true"),            # Sage (often returns HTML)
    "10.1136": ("www.bmj.com", "/content/{doi}.full.pdf"),                          # BMJ (path may vary)
    "10.1001": None,                                                                # JAMA — needs articleId scrape (LWW-style)
    # 10.1016 Elsevier → paper_fetch.py TDM (API, no proxy) handles it first.
    # 10.1097/10.1161/10.1213 (LWW/Ovid) → _LWW_PREFIXES + _lww_ovid_pdf (see stub).
}

_LWW_PREFIXES = {"10.1097", "10.1161", "10.1213"}   # LWW/Ovid journals.lww.com


# --- DPAPI (pure python, secret.ps1-compatible: CurrentUser, no entropy) ---
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_get(name: str) -> str:
    f = SECRETS_DIR / f"{name}.dpapi"
    if not f.exists():
        raise FileNotFoundError(
            f"secret '{name}' not stored. Run: powershell -File ~/.secrets/secret.ps1 set {name}"
        )
    enc = base64.b64decode(f.read_text().strip())
    blob_in = _BLOB(len(enc), ctypes.cast(ctypes.c_char_p(enc), ctypes.POINTER(ctypes.c_char)))
    blob_out = _BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError(f"CryptUnprotectData failed for {name} (err {ctypes.GetLastError()})")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def dpapi_get_opt(name: str) -> str | None:
    try:
        return dpapi_get(name)
    except FileNotFoundError:
        return None


def dpapi_set(name: str, value: str) -> None:
    data = value.encode("utf-8")
    blob_in = _BLOB(len(data), ctypes.cast(ctypes.c_char_p(data), ctypes.POINTER(ctypes.c_char)))
    blob_out = _BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError(f"CryptProtectData failed for {name} (err {ctypes.GetLastError()})")
    try:
        enc = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    (SECRETS_DIR / f"{name}.dpapi").write_text(base64.b64encode(enc).decode("ascii"))


# --- captcha OCR ----------------------------------------------------------
_ocr = None


def solve_captcha(png_bytes: bytes) -> str:
    """Offline numeric-CAPTCHA OCR (ddddocr). Digits only — adjust the filter if your
    library's CAPTCHA uses letters."""
    global _ocr
    if _ocr is None:
        import ddddocr
        _ocr = ddddocr.DdddOcr(show_ad=False)
    return "".join(ch for ch in _ocr.classification(png_bytes) if ch.isdigit())


# --- access log & throttle ------------------------------------------------
def _now() -> datetime:
    return datetime.now()


def _log(rec: dict) -> None:
    rec.setdefault("ts", _now().isoformat(timespec="seconds"))
    try:
        PROFILE_DIR.mkdir(exist_ok=True)
        with ACCESS_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _read_log() -> list[dict]:
    if not ACCESS_LOG.exists():
        return []
    out = []
    for line in ACCESS_LOG.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def _throttle() -> None:
    """Enforce a minimum gap between proxy requests, based on the last logged request."""
    if MIN_REQUEST_INTERVAL_S <= 0:
        return
    recs = [r for r in _read_log() if r.get("kind") == "proxy"]
    if not recs:
        return
    try:
        last = datetime.fromisoformat(recs[-1]["ts"])
    except Exception:
        return
    elapsed = (_now() - last).total_seconds()
    wait = MIN_REQUEST_INTERVAL_S - elapsed
    if wait > 0:
        time.sleep(wait + random.uniform(0.5, 2.5))


def _within(ts: str, hours: int) -> bool:
    try:
        return datetime.fromisoformat(ts) >= _now() - timedelta(hours=hours)
    except Exception:
        return False


def _warn_if_blocked(status: str) -> None:
    """When a real server-side block appears, report the daily volume that triggered it —
    that's how the true rate ceiling gets calibrated. Bulk download blocks the whole
    institution's IP, so this matters for everyone, not just you."""
    if status not in ("cf_challenge", "cf_block", "rate_limited"):
        return
    n24 = sum(1 for r in _read_log() if r.get("kind") == "proxy" and _within(r.get("ts"), 24))
    who = f" (report it to {FEEDBACK_CONTACT})" if FEEDBACK_CONTACT else ""
    sys.stderr.write(
        f"⚠ Publisher blocked you at ~request #{n24} in the last 24h ({status}). This is the\n"
        f"  real rate-ceiling signal{who}. Pause for a while before retrying.\n")


# --- watchdog (bounded failure instead of an unbounded hang) --------------
def _kill_own_tree(code: int) -> None:
    """Exit, taking any spawned chromium with us. A bare os._exit orphans the browser
    (leaked RAM); `timeout`/SIGTERM from a caller has the same flaw — it kills the
    parent only. An external `timeout` has the same flaw.

    ==Nothing in here may raise.== This runs on the watchdog thread; an escaping
    exception would kill only that thread, silently disarming the watchdog and leaving
    the process hung forever (observed 2026-07-10: a `login` ran 442 s unprotected).
    So every step is individually guarded and `os._exit` is in a `finally`.
    """
    try:
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            sys.stderr.flush()
        except Exception:
            pass
        try:
            import psutil
            for child in psutil.Process().children(recursive=True):
                try:
                    child.kill()
                except Exception:
                    pass
        except Exception:
            try:
                subprocess.run(["taskkill", "/PID", str(os.getpid()), "/T", "/F"],
                               capture_output=True, timeout=20)
            except Exception:
                pass
    finally:
        os._exit(code)


def _arm_watchdog(label: str, seconds: int = WATCHDOG_S) -> threading.Timer:
    def blow():
        print(f"[watchdog] '{label}' exceeded {seconds}s — aborting.\n"
              f"[watchdog] Usual cause: remote-auth login/CAPTCHA or a proxy interstitial "
              f"hanging. Retry once; if it repeats, run `login` interactively.",
              file=sys.stderr)
        _kill_own_tree(5)
    t = threading.Timer(seconds, blow)
    t.daemon = True
    t.start()
    return t


# --- single-instance lock (the chromium profile is exclusive) -------------
def _pid_alive(pid: int) -> bool:
    SYNCHRONIZE = 0x00100000
    h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if h:
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    return False


def _lock_holder() -> dict | None:
    try:
        return json.loads(LOCK_FILE.read_text())
    except Exception:
        return None            # unreadable / torn write → treat as stale


def _lock_is_stale(holder: dict | None) -> bool:
    if not holder:
        return True
    pid = holder.get("pid")
    if isinstance(pid, int) and not _pid_alive(pid):
        return True
    try:
        age = (_now() - datetime.fromisoformat(holder["started"])).total_seconds()
    except Exception:
        return True
    return age > LOCK_STALE_S


@contextlib.contextmanager
def profile_lock(label: str = "", wait_s: int = LOCK_WAIT_S):
    """Serialize browser-driving commands across processes.

    A second caller queues; on timeout it exits 4 with an actionable message rather than
    hanging inside chromium. A holder whose pid is gone (or older than LOCK_STALE_S) is
    treated as crashed and its lock is stolen.
    """
    PROFILE_DIR.mkdir(exist_ok=True)
    deadline = time.time() + wait_s
    waiting = False
    while True:
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, json.dumps(
                {"pid": os.getpid(), "started": _now().isoformat(timespec="seconds"),
                 "cmd": label}).encode())
            os.close(fd)
            break
        except FileExistsError:
            holder = _lock_holder()
            if _lock_is_stale(holder):
                print(f"[lock] stealing stale lock {holder}", file=sys.stderr)
                LOCK_FILE.unlink(missing_ok=True)
                continue
            if time.time() >= deadline:
                print(f"[lock] profile busy (held by {holder}); gave up after {wait_s}s.\n"
                      f"[lock] This tool is SERIAL — run fetches one at a time, never fanned "
                      f"out across parallel workers.", file=sys.stderr)
                raise SystemExit(4)
            if not waiting:
                print(f"[lock] profile busy (held by {holder}); queueing…", file=sys.stderr)
                waiting = True
            time.sleep(3)
    try:
        yield
    finally:
        LOCK_FILE.unlink(missing_ok=True)


# --- browser context ------------------------------------------------------
def _new_context(pw, headless: bool = True):
    # patchright (stealth Playwright fork) — do NOT set a custom user_agent or inject init
    # scripts; that de-anonymizes the browser and re-triggers Cloudflare. The defaults are
    # what let headless pass the CF challenge. (This is the single most important gotcha.)
    PROFILE_DIR.mkdir(exist_ok=True)
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 1280, "height": 900},
        accept_downloads=True,
    )
    ctx.set_default_navigation_timeout(NAV_TIMEOUT_MS)
    return ctx


# --- session persistence --------------------------------------------------
def save_session(ctx) -> None:
    saved = []
    for c in ctx.cookies():
        want = PERSIST_COOKIES.get(c["name"])
        if want and c["domain"] == want:
            dpapi_set(f"LIB_COOKIE_{c['name'].upper().replace('-', '_')}", c["value"])
            saved.append(c["name"])
    print(f"[session] persisted cookies: {saved}", file=sys.stderr)


def restore_session(ctx) -> bool:
    cookies = []
    for name, domain in PERSIST_COOKIES.items():
        if not domain:
            continue
        val = dpapi_get_opt(f"LIB_COOKIE_{name.upper().replace('-', '_')}")
        if val:
            cookies.append({
                "name": name, "value": val, "domain": domain, "path": "/",
                "expires": time.time() + 8 * 3600, "httpOnly": True, "secure": True,
            })
    if cookies:
        ctx.add_cookies(cookies)
    return bool(cookies)


def is_logged_in(page) -> bool:
    if not REMOTE_AUTH:
        sys.exit("config.yaml institution.remote_auth_base is blank — set it to your library's "
                 "remote-auth URL to use the proxy path.")
    page.goto(REMOTE_AUTH, wait_until="domcontentloaded")
    if "/login" in page.url:
        return False
    # Heuristic: no password field visible → we're past the login page. Adjust the selector
    # to whatever marks a logged-in state on your gate.
    return page.locator("#id_password").count() == 0


def login(page) -> bool:
    """STUB — implement your library's remote-auth login here.

    The reference flow (a Django-style gate with a numeric-CAPTCHA, which is common) is:
      1. GET  {REMOTE_AUTH}/login/
      2. read the CSRF/hashkey hidden field
      3. GET  the CAPTCHA image endpoint, solve it offline (see solve_captcha)
      4. fill username / password / captcha, submit the form
      5. verify you've left /login, then save_session(page.context)
      6. retry up to CAPTCHA_MAX_TRIES on OCR misreads

    The exact form field ids, the CAPTCHA image URL, and the success check are specific to
    YOUR library's login page — inspect it in your browser's devtools and fill them in.
    Credentials come from your secret store: dpapi_get("LIB_USER") / dpapi_get("LIB_PASS").
    """
    raise NotImplementedError(
        "login() is a stub in the public edition. Implement it for your library's remote-auth "
        "page (see the docstring), or use only the API/OA routes in paper_fetch.py.")


def ensure_login(page) -> bool:
    return True if is_logged_in(page) else login(page)


# --- download -------------------------------------------------------------
def _proxy_host(publisher_host: str) -> str:
    """onlinelibrary.wiley.com -> onlinelibrary-wiley-com.<proxy_suffix>"""
    return f"{publisher_host.replace('.', '-')}.{PROXY_SUFFIX}"


def _is_pdf(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 2048 and path.read_bytes()[:4] == b"%PDF"


def _classify(resp, body: bytes) -> str:
    if body[:4] == b"%PDF":
        return "pdf"
    cfm = (resp.headers.get("cf-mitigated") or "").lower()
    head = body[:2000].lower()
    if cfm == "challenge" or b"just a moment" in head:
        return "cf_challenge"
    if cfm == "block" or resp.status == 1020 or b"you have been blocked" in head:
        return "cf_block"
    if resp.status == 429 or b"too many requests" in head or b"rate limit" in head:
        return "rate_limited"
    if "/login" in (resp.url or ""):
        return "auth_expired"
    return f"http_{resp.status}"


def _try_paper_fetch(doi: str, out: Path) -> bool:
    """Layer 1: API/OA/TDM (Elsevier TDM, Springer OA, Unpaywall) — no proxy, no CF."""
    if not PAPER_FETCH.exists():
        return False
    try:
        subprocess.run([sys.executable, str(PAPER_FETCH), doi, str(out)],
                       timeout=120, capture_output=True)
    except Exception:
        return False
    ok = _is_pdf(out)
    if ok:
        _log({"kind": "api", "doi": doi, "status": "pdf", "bytes": out.stat().st_size})
    return ok


def _proxy_pdf(page, doi: str, out: Path, allow_nav: bool) -> bool:
    """Layer 2: authenticated proxy. allow_nav=False → silent request.get (phase 1);
    allow_nav=True → run CF challenge in a real browser first (phase 2)."""
    prefix = doi.split("/")[0]
    route = PROVIDER_ROUTES.get(prefix)
    if not route:
        _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "n/a",
              "status": "no_route" if prefix in PROVIDER_ROUTES else "unknown_prefix"})
        return False
    if not PROXY_SUFFIX:
        sys.exit("config.yaml institution.proxy_suffix is blank — set it to your library's "
                 "proxy suffix to use the proxy path.")
    host, path = route
    url = f"https://{_proxy_host(host)}{path.format(doi=doi)}"
    _throttle()
    phase = "headful" if allow_nav else "headless"

    if allow_nav:
        print("[proxy] opening browser to clear Cloudflare — solve it if a challenge shows",
              file=sys.stderr)
        try:
            with page.expect_download(timeout=45000) as dl:
                page.goto(url)
            dl.value.save_as(str(out))
            if _is_pdf(out):
                _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": phase,
                      "status": "pdf", "bytes": out.stat().st_size, "via": "download_event"})
                return True
        except Exception:
            try:
                page.wait_for_timeout(2500)
            except Exception:
                pass

    try:
        resp = page.request.get(url, timeout=NAV_TIMEOUT_MS)
        body = resp.body()
    except Exception as e:
        _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": phase,
              "status": "request_error", "note": repr(e)[:100]})
        return False

    status = _classify(resp, body)
    _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": phase, "status": status,
          "http": resp.status, "bytes": len(body),
          "cf_ray": resp.headers.get("cf-ray"), "cf_mitigated": resp.headers.get("cf-mitigated")})
    if status == "pdf":
        out.write_bytes(body)
        print(f"[proxy] OK ({phase}) -> {out} ({len(body)} bytes)", file=sys.stderr)
        return True
    _warn_if_blocked(status)
    print(f"[proxy] {status} ({phase}, http {resp.status}, {len(body)}B)", file=sys.stderr)
    return False


def _lww_ovid_pdf(page, doi: str, out: Path) -> bool:
    """STUB — one worked example of a multi-step publisher flow (LWW/Ovid, journals.lww.com).

    Some publishers don't expose a DOI→PDF template; the PDF URL is signed and must be
    discovered by walking the article page. LWW/Ovid via a URL-rewriting proxy is the
    canonical hard case, reverse-engineered as follows (implement against your own proxy):

      ==Needs a HEADFUL browser== — the proxy's "please wait" JS interstitial hangs headless.
      1. GET `doi-org.<proxy_suffix>/{doi}` → redirects to the article page on
         `journals-lww-com.<proxy_suffix>` (the real LWW URL is not DOI-based).
      2. Scrape the article number `an` (########-#########-##### in the PDF button's
         PDFDownloadInit config) and the journal slug (first path segment).
      3. GET `/{journal}/_layouts/15/oaks.journals/downloadpdf.aspx?trckng_src_pg=
         ArticleViewer&an={an}` with `Referer: <article>` → a ~80 KB *viewer* HTML whose
         inline JSON `pdfDownloadDetails.pdfUrl` is the real, signed PDF URL.
      4. GET that signed pdfUrl ==with `Referer: <the downloadpdf.aspx viewer>`== → PDF bytes.
         The Referer MUST be the viewer; an article/empty Referer → HTTP 503. The pdfs
         backend may also 503 briefly while warming → retry a few times.

    The technique (resolve → scrape id → viewer → signed URL with the right Referer chain)
    generalizes to other signed-URL publishers. Implement it for the ones you need.
    """
    raise NotImplementedError(
        "_lww_ovid_pdf() is a stub in the public edition. See the docstring for the full "
        "technique and implement it for your proxy, or rely on the simple-template publishers "
        "in PROVIDER_ROUTES and the API/OA routes.")


def _sfx_hint(doi: str) -> str:
    return f" SFX: {SFX.format(doi=doi)}" if SFX else ""


def run_fetch(pw, doi: str, out: Path) -> bool:
    """Two-phase orchestration. Layer 1 (API/OA/TDM) needs no login; the proxy layers need
    login() and (for LWW) _lww_ovid_pdf() implemented for your institution."""
    prefix = doi.split("/")[0]
    is_lww = prefix in _LWW_PREFIXES
    ctx = _new_context(pw, headless=not is_lww)
    restore_session(ctx)
    page = ctx.new_page()
    try:
        # Layer 1 — API/OA/TDM (no proxy, no CF, no login). Works out of the box.
        if _try_paper_fetch(doi, out):
            print(f"[fetch] got via API/OA/TDM route -> {out}", file=sys.stderr)
            return True
        if not ensure_login(page):
            print("[fetch] login failed", file=sys.stderr)
            return False
        if is_lww:
            if _lww_ovid_pdf(page, doi, out):
                return True
            print(f"[fetch] no LWW route for {doi}.{_sfx_hint(doi)}", file=sys.stderr)
            return False
        if PROVIDER_ROUTES.get(prefix) is None:
            print(f"[fetch] no proxy route for {doi}.{_sfx_hint(doi)}", file=sys.stderr)
            return False
        if _proxy_pdf(page, doi, out, allow_nav=False):
            return True
        print("[fetch] proxy failed; forcing fresh login + retry", file=sys.stderr)
        if login(page) and _proxy_pdf(page, doi, out, allow_nav=False):
            return True
    finally:
        ctx.close()

    print(f"[fetch] no automated route for {doi}.{_sfx_hint(doi)}", file=sys.stderr)
    return False


# --- stats (rate-ceiling analysis) ----------------------------------------
def print_stats() -> None:
    recs = _read_log()
    if not recs:
        print("no access log yet:", ACCESS_LOG)
        return
    now = _now()
    def within(hours):
        cut = now - timedelta(hours=hours)
        n = 0
        for r in recs:
            try:
                if datetime.fromisoformat(r["ts"]) >= cut:
                    n += 1
            except Exception:
                pass
        return n
    from collections import Counter
    by_status = Counter(r.get("status", "?") for r in recs)
    blocks = [r for r in recs if r.get("status") in ("cf_challenge", "cf_block", "rate_limited")]
    auth = [r for r in recs if r.get("status") == "auth_expired"]
    pdfs = [r for r in recs if r.get("status") == "pdf"]
    print(f"access log: {ACCESS_LOG}  ({len(recs)} events)")
    print(f"  requests last 1h / 24h : {within(1)} / {within(24)}")
    print(f"  PDF successes total    : {len(pdfs)}")
    print(f"  session re-auths (not blocks): {len(auth)}")
    print("  status breakdown       :", dict(by_status))
    if blocks:
        print(f"  ⚠️ REAL blocks ({len(blocks)}) — ceiling signal, most recent:")
        for r in blocks[-5:]:
            print(f"    {r.get('ts')}  {r.get('status')}  {r.get('prefix','')}  {r.get('doi','')}")
        if FEEDBACK_CONTACT:
            print(f"  → report the daily request count at the block to {FEEDBACK_CONTACT} "
                  "to help calibrate the real ceiling.")
    else:
        print("  ✅ no rate/CF blocks ever — ceiling not hit")


# --- CLI ------------------------------------------------------------------
def main(argv):
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "stats":
        print_stats()
        return 0

    if cmd not in ("fetch", "check", "login"):
        print(f"unknown command: {cmd}")
        return 1
    if cmd == "fetch" and len(argv) < 3:
        print("usage: fetch <DOI> <out.pdf>")
        return 1

    from patchright.sync_api import sync_playwright   # stealth fork — passes Cloudflare
    # Every command below drives the exclusive chromium profile → serialize (exit 4 if busy),
    # and bound it with a watchdog (exit 5) so a hung login can't stall the caller forever.
    label = " ".join(argv[:2])
    with profile_lock(label=label):
        # ==Arm BEFORE sync_playwright().== Driver startup can itself hang; arming inside
        # the playwright context left that window unprotected (2026-07-10: 442 s hang).
        # The lock's own bounded wait covers the queueing phase, so nothing is unguarded.
        wd = _arm_watchdog(label)
        try:
            with sync_playwright() as pw:
                if cmd == "fetch":
                    return 0 if run_fetch(pw, argv[1], Path(argv[2])) else 2

                # `login` needs a real window: the proxy's JS-redirect interstitial
                # never completes headless. `check` only hits the remote-auth home page → headless.
                ctx = _new_context(pw, headless=(cmd == "check"))
                restore_session(ctx)
                page = ctx.new_page()
                try:
                    if cmd == "check":
                        ok = is_logged_in(page)
                        print("session: VALID" if ok else "session: EXPIRED (run: login)")
                        return 0 if ok else 2
                    ok = ensure_login(page)     # cmd == "login"
                    print("login: OK" if ok else "login: FAILED")
                    return 0 if ok else 2
                finally:
                    ctx.close()
        finally:
            wd.cancel()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
