#!/usr/bin/env python3
"""Institutional library auto-session — complete reference implementation.

As of v1.0 this public edition is COMPLETE: every route kind ships working code —
the template routes, the `citation_pdf_url` meta route (headless and headful-nav
variants), and the full LWW/Ovid multi-step signed-URL flow including the Ovid
concurrent-licence-seat (E3) discipline. Nothing is a stub anymore.

What remains yours to supply (because it is inherently yours):
    * config.yaml        — your library's endpoints (remote-auth gate, proxy suffix,
                           link resolver). This repo ships NO institution's access.
    * credentials        — your own account, in a local secret store (never in files).
    * login() specifics  — if your gate is a plain login FORM (EZproxy, Django/NetScaler
                           portals — the two most common families), the generic form
                           login works out of the box once you set the selectors in
                           config.yaml `auth:`. Only SSO families (OpenAthens/Shibboleth
                           redirect chains) still need a custom `login()` — see
                           docs/library-setup.md.

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

  ⚠ Proxy authorization can be granted PER proxy subdomain (a JS-handshake on first visit).
  Being authorized on one publisher's subdomain does NOT authorize another's — and the
  handshake completes only when the login form is submitted ON the bounce page itself
  (its ?next= chain carries the handshake). That is what `_login_submit_here` is for; a
  plain re-login at the gate's own /login page is a NO-OP while the session is still valid.

  Fetching is TWO-PHASE for the best UX:
    Phase 1 (headless, silent): try API/OA/TDM first (paper_fetch.py); else an
      authenticated proxy request reusing any valid Cloudflare clearance in the profile.
    Phase 2 (headful, only where needed): LWW/Ovid's JS interstitial and some publishers'
      Cloudflare challenge (BMJ-class) only complete in a real browser window. The
      clearance persists in the browser profile and is reused silently until it expires.

  Session persistence: the remote-auth session cookie and the proxy-authorization cookie
  are session-only (Chromium won't write them to disk). Save them to your secret store
  after login and re-inject next run, so the session survives browser close/reboot.

  Rate awareness: log every proxy request; `stats` summarizes successes vs Cloudflare /
  rate / auth blocks so you can learn the real throttle ceiling empirically. A courtesy
  delay (rate.min_interval_s) is enforced between papers.
────────────────────────────────────────────────────────────────────────────────────────

Credentials (store once in a DPAPI CurrentUser store; values never touch chat or config):
    powershell -File ~/.secrets/secret.ps1 set LIB_USER
    powershell -File ~/.secrets/secret.ps1 set LIB_PASS

CLI:
    python library_session.py check                    # is the stored session valid?
    python library_session.py login                    # force a fresh login
    python library_session.py fetch <DOI> <out.pdf>    # two-phase full-text download
    python library_session.py stats                    # access-log summary / block analysis
    python library_session.py routes                   # per-route scorecard + holdings gaps

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
import html as _html
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
from urllib.parse import unquote, urlsplit

from paper_config import CFG, require

# --- config ---------------------------------------------------------------
SECRETS_DIR = Path.home() / ".secrets"
PROFILE_DIR = Path.home() / ".paperfetch_profile"   # patchright (stealth) chromium profile
ACCESS_LOG = PROFILE_DIR / "access_log.jsonl"

REMOTE_AUTH = (CFG["institution"]["remote_auth_base"] or "").rstrip("/")
REMOTE_AUTH_DOMAIN = urlsplit(REMOTE_AUTH).netloc if REMOTE_AUTH else ""
PROXY_SUFFIX = CFG["institution"]["proxy_suffix"]
SFX = CFG["institution"]["sfx_base"]

# Form-login knobs (config.yaml `auth:`) — see docs/library-setup.md for the presets.
AUTH = CFG["auth"]
AUTH_FAMILY = (AUTH.get("family") or "form").lower()
LOGIN_PATH = AUTH.get("login_path") or "/login/"
USER_SEL = AUTH.get("user_selector") or "#id_username"
PASS_SEL = AUTH.get("pass_selector") or "#id_password"
SUBMIT_SEL = AUTH.get("submit_selector") or \
    "form button[type='submit'], form input[type='submit']"
CAPTCHA_SEL = AUTH.get("captcha_selector") or ""
CAPTCHA_HASHKEY_SEL = AUTH.get("captcha_hashkey_selector") or ""
CAPTCHA_IMAGE_PATH = AUTH.get("captcha_image_path") or ""

CAPTCHA_MAX_TRIES = 6
NAV_TIMEOUT_MS = 30000
LAUNCH_TIMEOUT_S = int(os.environ.get("PAPERFETCH_LAUNCH_TIMEOUT_S", "90"))
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

# Ovid licence-seat back-off. E3 ("License Service Failure") means a *concurrent seat* is
# taken, not that we're rate-limited — and it is raised above the proxy, so _classify()
# never sees it. On E3 we stop touching Ovid until this cools down.
OVID_COOLDOWN_FILE = PROFILE_DIR / ".ovid_e3_until"
OVID_E3_COOLDOWN_S = int(os.environ.get("PAPERFETCH_OVID_COOLDOWN_S", "1800"))
OVID_VIEWER_WAIT_S = int(os.environ.get("PAPERFETCH_OVID_VIEWER_WAIT_S", "30"))  # viewer mounts async
_E3_MARKERS = ("License Service Failure", "Code: E3", "licence service failure")

if MIN_REQUEST_INTERVAL_S <= 0:
    sys.stderr.write(
        "⚠⚠ rate.min_interval_s=0 — courtesy delay DISABLED. Systematic/bulk download can\n"
        "   get your entire institution's IP blocked by the publisher (everyone loses access,\n"
        "   not just you). Only do this for a handful of papers, and stop if `stats` shows a\n"
        "   block. You own the consequences.\n")

# proxy-authorization cookie is typically wildcarded on the remote-auth parent domain
_PARENT_DOMAIN = "." + ".".join(REMOTE_AUTH_DOMAIN.split(".")[-3:]) if REMOTE_AUTH_DOMAIN else ""
# name -> exact cookie domain. Override in config.yaml `auth.persist_cookies` with your
# proxy's actual cookies (find them in devtools after logging in): the session cookie on
# the remote-auth host, and the proxy-authorization cookie wildcarded on the parent domain.
PERSIST_COOKIES = AUTH.get("persist_cookies") or {
    "sessionid": REMOTE_AUTH_DOMAIN,
    "proxy-auth": _PARENT_DOMAIN,
}

# DOI prefix -> route. Three kinds (every entry below was verified end-to-end against a
# real library's subscriptions — the publisher hosts and PDF path templates are PUBLIC and
# the same for everyone; only the proxy suffix appended at runtime is yours):
#
#   tpl  — one-step PDF URL template: host + path.format(doi=...), headless request.get.
#   meta — multi-step: resolver → article HTML → `<meta name="citation_pdf_url">` →
#          fetch with Referer. `host` omitted → the generic doi-org proxy resolver;
#          `host` given → that site's own `/lookup/doi/{doi}` (Highwire sites send the
#          doi-org resolver into an infinite redirect loop). `nav=True` → the resolver
#          step must be a REAL headful navigation (the site's Cloudflare challenge blocks
#          headless, including request.get from a headful context).
#   lww  — LWW/Ovid multi-step signed-URL flow (headful): resolver → viewer → signed
#          pdfUrl, with an Ovid-OCE fallback. See _lww_ovid_pdf.
#
# ==Before adding a publisher, your test article MUST be one your library actually holds
# (subscribed AND the year inside coverage — holdings.check).== Probing with an
# unentitled article makes a GOOD route return reader HTML/403, indistinguishable from a
# broken one — four publishers (Sage, T&F, OUP, BMJ) were each wrongly written off that
# way in this project's history. The reverse trap exists too: some journals genuinely
# have no online entitlement (the full-text page is an abstract + paywall) — that is not
# a route bug either. `routes` prints the per-prefix scorecard with entitlement attached.
ROUTES: dict[str, dict] = {
    # --- tpl (one-step template) ---
    "10.1002": {"kind": "tpl", "host": "onlinelibrary.wiley.com", "path": "/doi/pdfdirect/{doi}?download=true"},  # Wiley (incl. Cochrane 10.1002/14651858)
    "10.1111": {"kind": "tpl", "host": "onlinelibrary.wiley.com", "path": "/doi/pdfdirect/{doi}?download=true"},  # Wiley
    "10.1007": {"kind": "tpl", "host": "link.springer.com",       "path": "/content/pdf/{doi}.pdf"},              # Springer
    "10.1186": {"kind": "tpl", "host": "link.springer.com",       "path": "/content/pdf/{doi}.pdf"},              # BMC ⚠ real PDF lives on per-journal *.biomedcentral.com, often 404
    "10.1056": {"kind": "tpl", "host": "www.nejm.org",            "path": "/doi/pdf/{doi}"},                      # NEJM ✅
    "10.1177": {"kind": "tpl", "host": "journals.sagepub.com",    "path": "/doi/pdf/{doi}?download=true"},        # Sage ✅ (OnlineFirst may sit outside coverage → 403, not a route bug)
    "10.1080": {"kind": "tpl", "host": "www.tandfonline.com",     "path": "/doi/pdf/{doi}?download=true"},        # Taylor & Francis ✅
    "10.2214": {"kind": "tpl", "host": "www.ajronline.org",       "path": "/doi/pdf/{doi}?download=true"},        # AJR (Atypon) ✅ (meta route has no citation_pdf_url)
    "10.1148": {"kind": "tpl", "host": "pubs.rsna.org",           "path": "/doi/pdf/{doi}?download=true"},        # Radiology / RSNA ✅
    "10.1142": {"kind": "tpl", "host": "www.worldscientific.com", "path": "/doi/pdf/{doi}?download=true"},        # World Scientific ✅
    # --- meta (resolver → citation_pdf_url) ---
    "10.1001": {"kind": "meta"},                                 # JAMA Network ✅
    "10.1093": {"kind": "meta"},                                 # Oxford (OUP) ✅
    "10.1542": {"kind": "meta"},                                 # Pediatrics ✅
    "10.1183": {"kind": "meta"},                                 # European Respiratory J ✅
    "10.3171": {"kind": "meta"},                                 # J Neurosurg ✅
    "10.1038": {"kind": "meta"},                                 # Nature portfolio ✅
    # --- meta + headful nav (Cloudflare blocks headless) ---
    "10.1136": {"kind": "meta", "nav": True},                    # BMJ ✅ — the earlier "WAF dead end"
    #                                                              verdict was a headless-only artifact;
    #                                                              a headful navigation passes first try.
    "10.3174": {"kind": "meta", "nav": True, "host": "www.ajnr.org"},          # AJNR — CF "Just a moment"; doi-org resolver loops → /lookup/doi/
    "10.2967": {"kind": "meta", "nav": True, "host": "jnm.snmjournals.org"},   # J Nucl Med — same
    # --- lww (Ovid multi-step, headful; concurrent-licence seats apply) ---
    "10.1097": {"kind": "lww"},   # most LWW journals
    "10.1161": {"kind": "lww"},   # AHA (Circulation / Stroke)
    "10.1213": {"kind": "lww"},   # A&A / A&A Practice
    "10.2215": {"kind": "lww"},   # CJASN (moved to LWW; goes through the Ovid OCE branch)
    # --- no route, with the reason established (don't re-probe blindly) ---
    # 10.1016 Elsevier → paper_fetch.py's TDM API takes it first (no proxy involved).
    # Genuine dead ends at the reference library, kept as examples of WHY a prefix can be
    # absent (check your own holdings before copying these verdicts):
    #   10.2519 JOSPT — no online entitlement (full-text page = abstract + paywall).
    #   10.1055 Thieme / 10.1200 JCO / 10.1089 Liebert — the LIBRARY's proxy had those
    #     subdomains unregistered ("Host does not match" / error page) → report to the
    #     library; not fixable client-side. `_classify` flags this as
    #     `proxy_host_unregistered` so you can tell it apart.
}


# --- DPAPI (pure python, CurrentUser, no entropy) ---------------------------
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
_T0 = time.time()


def _mark(msg: str) -> None:
    """Timestamped startup/progress trace. Headful chromium launches can intermittently
    hang for minutes with an empty access log — these lines pin down WHICH call blocks."""
    print(f"[t+{time.time() - _T0:6.1f}s] {msg}", file=sys.stderr, flush=True)


def _now() -> datetime:
    return datetime.now()


# This fetch's holdings entitlement, set by run_fetch; _log stamps it onto every proxy
# record. ==Why==: an isolated `no_pdf_meta` is unreadable — it could be "route broken"
# or "no access to this article", and the two call for OPPOSITE responses (reverse-
# engineer vs. do nothing). Pinning subscribed/covered to the same record is what lets
# `routes` answer "should this failure be fixed" automatically.
_CUR_ENT: dict = {}


def _log(rec: dict) -> None:
    rec.setdefault("ts", _now().isoformat(timespec="seconds"))
    if rec.get("kind") == "proxy" and _CUR_ENT:
        rec.setdefault("subscribed", _CUR_ENT.get("subscribed"))
        rec.setdefault("covered", _CUR_ENT.get("covered"))
        rec.setdefault("journal", _CUR_ENT.get("journal"))
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


_CHAIN_STARTED = False


def _throttle() -> None:
    """Courtesy delay BETWEEN papers, not within one paper's multi-step chain.

    One fetch process = one paper (the profile lock enforces serial runs), so only the
    first proxy request of the process waits against the access log; later steps of the
    same chain (resolver → viewer → PDF) use a small jitter. Applying the full gap at
    every step adds 30-45 s per paper for nothing — watch `stats`, and revisit if it
    starts showing real blocks."""
    global _CHAIN_STARTED
    if _CHAIN_STARTED:
        time.sleep(random.uniform(1.0, 2.5))
        return
    _CHAIN_STARTED = True
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
    parent only.

    ==Nothing in here may raise.== This runs on the watchdog thread; an escaping
    exception would kill only that thread, silently disarming the watchdog and leaving
    the process hung forever. So every step is individually guarded and `os._exit` is
    in a `finally`.
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
def _kill_chromium_children() -> int:
    """Kill chrome/chromium children only — NOT the patchright node driver (killing the
    driver would break the whole sync_playwright session, making a relaunch impossible)."""
    n = 0
    try:
        import psutil
        for ch in psutil.Process().children(recursive=True):
            try:
                if "chrom" in ch.name().lower():
                    ch.kill()
                    n += 1
            except Exception:
                pass
    except Exception:
        pass
    return n


def _new_context(pw, headless: bool = True):
    # patchright (stealth Playwright fork) — do NOT set a custom user_agent or inject init
    # scripts; that de-anonymizes the browser and re-triggers Cloudflare. The defaults are
    # what let headless pass the CF challenge. (This is the single most important gotcha.)
    #
    # Headful launches can intermittently hang for minutes (headless is always instant).
    # Rather than only diagnosing the heisenbug, self-heal: if the launch exceeds
    # LAUNCH_TIMEOUT_S, kill the half-started chromium (which makes the pending launch
    # call raise) and retry once with the same profile.
    PROFILE_DIR.mkdir(exist_ok=True)
    last_err = None
    for attempt in (1, 2):
        _mark(f"launch chromium headless={headless} attempt={attempt}")
        fired = threading.Event()

        def _bail():
            fired.set()
            _mark(f"launch watchdog: >{LAUNCH_TIMEOUT_S}s — killing chromium children "
                  f"({_kill_chromium_children()} killed) and retrying")
        t = threading.Timer(LAUNCH_TIMEOUT_S, _bail)
        t.daemon = True
        t.start()
        try:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=headless,
                viewport={"width": 1280, "height": 900},
                accept_downloads=True,
            )
            t.cancel()
            if fired.is_set():
                # Watchdog killed chromium *while* the context came up — it is unusable.
                try:
                    ctx.close()
                except Exception:
                    pass
                raise RuntimeError("launch exceeded watchdog")
            _mark("launch OK")
            ctx.set_default_navigation_timeout(NAV_TIMEOUT_MS)
            return ctx
        except Exception as e:
            t.cancel()
            last_err = e
            _mark(f"launch attempt {attempt} failed: {repr(e)[:120]}")
            if attempt == 1:
                time.sleep(2)
    raise last_err


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


# --- login (generic form family; SSO families → docs/library-setup.md) -----
def is_logged_in(page) -> bool:
    """Judge by the LOGIN page itself, NOT the gate's home page.

    A gate's home page often loads fine without a session, so "landed on home, not
    redirected" can report VALID seconds before the login page presents a full form. On
    the login page the signal is unambiguous: a valid session shows no password field; an
    expired one shows the form. (This still says nothing about per-subdomain proxy
    authorization — only the proxy attempt itself tests that.)"""
    if not REMOTE_AUTH:
        sys.exit("config.yaml institution.remote_auth_base is blank — set it to your library's "
                 "remote-auth URL to use the proxy path.")
    _mark(f"is_logged_in: goto {LOGIN_PATH}")
    page.goto(f"{REMOTE_AUTH}{LOGIN_PATH}", wait_until="domcontentloaded")
    _mark(f"is_logged_in: landed on {page.url[:80]}")
    return page.locator(PASS_SEL).count() == 0


def _login_submit_here(page) -> bool:
    """Fill + submit the login form on the CURRENT page (config-driven selectors).

    This exists because the form is not always at the gate's own login URL: on proxies
    that authorize PER subdomain, the first navigation to a not-yet-authorized publisher
    subdomain bounces to a login page carrying a `?next=` chain — and ==submitting the
    form THERE is what completes the per-subdomain handshake==. A plain `login()` re-visit
    of the gate's login page sees the still-valid session ("no password field → already
    logged in"), returns True as a NO-OP, and the subdomain stays unauthorized forever.

    The CAPTCHA image (if configured) is fetched from the current page's own origin so it
    works on both the gate and proxy-subdomain bounce pages. Gates without a CAPTCHA:
    leave the `auth.captcha_*` config keys blank and this degrades to plain fill+submit —
    which is exactly the EZproxy flow.
    """
    user = dpapi_get("LIB_USER")
    pw = dpapi_get("LIB_PASS")
    for attempt in range(1, CAPTCHA_MAX_TRIES + 1):
        if page.locator(PASS_SEL).count() == 0:
            return True
        sp = urlsplit(page.url)   # recompute — a failed submit may have moved hosts
        origin = f"{sp.scheme}://{sp.netloc}"
        if CAPTCHA_SEL:
            hashkey = ""
            if CAPTCHA_HASHKEY_SEL:
                hashkey = page.locator(CAPTCHA_HASHKEY_SEL).get_attribute("value") or ""
            img = page.request.get(f"{origin}{CAPTCHA_IMAGE_PATH.format(hashkey=hashkey)}")
            code = solve_captcha(img.body())
            print(f"[login] attempt {attempt} on {sp.netloc}: captcha -> {code!r}",
                  file=sys.stderr)
            if len(code) < 4:
                page.reload(wait_until="domcontentloaded")
                continue
            page.fill(CAPTCHA_SEL, code)
        page.fill(USER_SEL, user)
        page.fill(PASS_SEL, pw)
        page.click(SUBMIT_SEL)
        page.wait_for_load_state("domcontentloaded")
        if page.locator(PASS_SEL).count() == 0 and "/login" not in page.url:
            print(f"[login] success on attempt {attempt}", file=sys.stderr)
            save_session(page.context)
            return True
        time.sleep(1)
    print("[login] FAILED after retries (check creds / selectors / captcha style change)",
          file=sys.stderr)
    return False


def login(page) -> bool:
    """Log in at the gate's own login page.

    `auth.family: form` (default) drives the generic form flow above — it covers EZproxy
    and Django/NetScaler-style portals once the selectors in config.yaml match your gate
    (inspect its login page in devtools). SSO redirect chains (OpenAthens / Shibboleth)
    don't reduce to one form; set `auth.family: custom` and implement this function for
    your IdP — everything else in this file works unchanged once `login()` leaves a valid
    session in the browser context.
    """
    if AUTH_FAMILY != "form":
        raise NotImplementedError(
            "auth.family is not 'form'. Implement login() for your SSO flow (OpenAthens/"
            "Shibboleth), or use only the API/OA routes in paper_fetch.py. See "
            "docs/library-setup.md.")
    if not REMOTE_AUTH:
        sys.exit("config.yaml institution.remote_auth_base is blank — set it to your library's "
                 "remote-auth URL to use the proxy path.")
    _mark(f"login: goto {LOGIN_PATH}")
    page.goto(f"{REMOTE_AUTH}{LOGIN_PATH}", wait_until="domcontentloaded")
    _mark("login: page loaded")
    return _login_submit_here(page)


def ensure_login(page) -> bool:
    # login() is already idempotent — it opens the login page and only submits if a form
    # is there — so a separate is_logged_in() probe would just be a second navigation.
    return login(page)


# --- download -------------------------------------------------------------
def _entitlement(doi: str) -> dict:
    """This article's holdings entitlement (`holdings.py`: DOI → ISSN/journal → your
    library's A-Z e-journal table).

    ==Why not a per-article link-resolver query==: SFX-style `getFullTxt` responses are
    UNSTABLE — the same DOI can return a full-text target on one call and nothing minutes
    later, manufacturing false negatives. The holdings table is journal-level, stable,
    offline, reproducible (docs/holdings.md).

    Returns holdings.check()'s dict; an empty dict when the holdings module/table is
    unavailable (never blocks the fetch).

    ⚠ Two semantic traps (both observed in practice; see docs/holdings.md):
    - `subscribed=None` (not in the table) ≠ no access — some platforms are licensed at a
      "database" level and never appear in the e-journal list, yet the proxy serves them.
      Not-found only warns; the proxy is still tried.
    - `covered=False` (journal subscribed, but this article's YEAR is outside coverage)
      is the usual reason a proxy returns reader HTML. Don't blame the route."""
    try:
        import holdings
        return holdings.check(doi)
    except Exception as e:
        print(f"[holdings] skipped ({repr(e)[:60]})", file=sys.stderr)
        return {}


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
    if cfm == "challenge" or b"just a moment" in head or b"attention required" in head:
        return "cf_challenge"
    if cfm == "block" or resp.status == 1020 or b"you have been blocked" in head:
        return "cf_block"
    if resp.status == 429 or b"too many requests" in head or b"rate limit" in head:
        return "rate_limited"
    if "/login" in (resp.url or ""):
        return "auth_expired"
    # The library's proxy has no registration for this publisher's subdomain → report it
    # to the library; it is NOT a route bug on our side.
    if b"host does not match" in head or b"oh noes" in head:
        return "proxy_host_unregistered"
    return f"http_{resp.status}"


def _classify_exc(e: Exception) -> str:
    """Classify exceptions too — lumping everything into `request_error` hides the
    difference between "the doi-org resolver redirect-loops on this site" (fixable by
    switching to a host resolver) and a genuine network error."""
    s = repr(e)
    if "TOO_MANY_REDIRECTS" in s or "Max redirect count exceeded" in s:
        return "redirect_loop"
    if "Timeout" in s or "timeout" in s:
        return "timeout"
    return "request_error"


def _try_paper_fetch(doi: str, out: Path) -> bool:
    """Layer 1: API/OA/TDM (Elsevier TDM, Springer OA, Unpaywall) — no proxy, no CF."""
    if not PAPER_FETCH.exists():
        return False
    try:
        r = subprocess.run([sys.executable, str(PAPER_FETCH), doi, str(out)],
                           timeout=120, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except Exception:
        return False
    ok = _is_pdf(out)
    if ok:
        _log({"kind": "api", "doi": doi, "status": "pdf", "bytes": out.stat().st_size})
    else:
        # Surface WHY the OA/TDM ladder came back empty — swallowing it hides route
        # diagnostics (which OA candidates were tried, missing-token hints, resolver link).
        tail = [ln for ln in (r.stdout or "").splitlines() if ln.strip()][-6:]
        for ln in tail:
            print(f"[paper_fetch] {ln}", file=sys.stderr)
    return ok


def _proxy_pdf(page, doi: str, out: Path, allow_nav: bool) -> bool:
    """tpl route: authenticated proxy. allow_nav=False → silent request.get (phase 1);
    allow_nav=True → run CF challenge in a real browser first (phase 2)."""
    prefix = doi.split("/")[0]
    route = ROUTES.get(prefix)
    if not route or route.get("kind") != "tpl":
        _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "n/a",
              "status": "unknown_prefix"})
        return False
    if not PROXY_SUFFIX:
        sys.exit("config.yaml institution.proxy_suffix is blank — set it to your library's "
                 "proxy suffix to use the proxy path.")
    url = f"https://{_proxy_host(route['host'])}{route['path'].format(doi=doi)}"
    _throttle()
    phase = "headful" if allow_nav else "headless"

    if allow_nav:
        # Real navigation runs the CF JS challenge (refreshing proxy-domain cf_clearance)
        # and fires a download event if authorized.
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
                page.wait_for_timeout(2500)  # let a non-download CF challenge finish
            except Exception:
                pass

    try:
        resp = page.request.get(url, timeout=NAV_TIMEOUT_MS)
        body = resp.body()
    except Exception as e:
        _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": phase,
              "status": _classify_exc(e), "note": repr(e)[:100]})
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


# --- Ovid licence-seat discipline ------------------------------------------
def _ovid_e3_cooldown_left() -> int:
    """Seconds remaining on the Ovid licence-seat back-off, 0 if clear."""
    try:
        until = datetime.fromisoformat(OVID_COOLDOWN_FILE.read_text().strip())
    except Exception:
        return 0
    return max(0, int((until - _now()).total_seconds()))


def _ovid_trip_e3(doi: str, an: str) -> None:
    """Record an E3 licence-seat failure and back off hard."""
    OVID_COOLDOWN_FILE.write_text((_now() + timedelta(seconds=OVID_E3_COOLDOWN_S)).isoformat())
    _log({"kind": "proxy", "doi": doi, "prefix": doi.split("/")[0], "phase": "ovid",
          "status": "license_seat_e3", "an": an})
    print(f"[ovid] ⚠ License Service Failure (E3) — a concurrent-licence SEAT is occupied, "
          f"not a rate limit.\n"
          f"[ovid] Backing off for {OVID_E3_COOLDOWN_S // 60} min. Close any Ovid tab you have "
          f"open (that holds a seat), then retry.", file=sys.stderr)


def _ovid_pick_pdf_url(seen_pdf: str | None, seen_viewer: str | None,
                       html: str, article: str) -> tuple[str | None, str | None]:
    """Choose the real PDF URL from what the Ovid article page produced.

    ==The trap this exists to prevent==: the pdf.js *viewer* URL embeds the literal string
    `application-pdf` inside its own `file=` query. Regex-matching URLs for `application-pdf`
    therefore selects the viewer, and you silently save ~88 KB of HTML as if it were a PDF.
    Selection order: (1) a response actually served as `content-type: application/pdf`;
    (2) the `file=` payload of the viewer URL; (3) the same, scraped from the page HTML.

    Returns `(pdf_url, referer)`; `pdf_url` is None when nothing usable was found.
    """
    if seen_pdf:
        return seen_pdf, (seen_viewer or article)
    if seen_viewer and "file=" in seen_viewer:
        return unquote(seen_viewer.split("file=", 1)[1].split("#")[0]), seen_viewer
    m = re.search(r'/pdfviewer/[^"\'<> ]*file=([^"\'<>\s]+)', html or "")
    if m:
        return unquote(m.group(1)), article
    return None, None


def _ovid_oce_pdf(page, doi: str, an: str, out: Path, viewer_url: str | None = None) -> str:
    """Ovid pdf.js-viewer route. Two entry points share this listener logic:
      * `oce-ovid-com/article/{an}/HTML` (default; ahead-of-print articles) — pass `an`.
      * `www-ovid-com/jnls/{journal}/pdf/{doi}~{slug}` (classic reader) — pass `viewer_url`.
    Both mount a pdf.js viewer that pulls the real PDF from assets.ovid.com with
    `content-type: application/pdf`; only the viewer page URL differs.

    ==This is the correct route for publish-ahead-of-print articles==, whose LWW-platform
    downloadpdf viewer carries no signed `pdfUrl`. The PDF exists; only the LWW platform
    hides it. ==A route returning nothing never proves the file is absent — check the
    publisher's other platform.==

    Network-traced findings: `/article/{an}/HTML` mounts the pdf.js viewer **by itself**
    (there is no PDF button in the DOM to click). The viewer is requested as
    `/pdfviewer/web/viewer.html?file=<signed assets.ovid.com URL>` and the PDF comes back
    with `content-type: application/pdf`, served **directly from assets.ovid.com** — the
    signature carries the authorization, no proxy rewrite needed.
    ⚠ Match on **content-type**, not on the URL: the viewer's own URL contains the string
    `application-pdf` inside its `file=` query, so a naive regex fetches 88 KB of viewer
    HTML instead of the PDF. Headful — same proxy interstitial as the LWW route.

    ⚠ ==Ovid enforces concurrent-licence SEATS, not just rate.== Opening the article page
    takes a seat; a human with the same article open already holds one, and seats are
    released only after a delay — so back-to-back experiments E3 themselves. Exceeding them
    yields **"License Service Failure (Code: E3)"**, raised *above* the proxy and therefore
    invisible to `_classify` (naive code mislabels it `no_pdfurl`). Discipline enforced here:
      * one attempt, no retry storm (only a short 503 retry while the proxy warms);
      * `about:blank` immediately after the fetch, to release the seat;
      * on E3: log `license_seat_e3` (so `stats` finally sees it), write a cooldown to
        `.ovid_e3_until`, and skip Ovid entirely until it expires.
    Callers must also not re-run the whole chain on a non-auth failure — that costs a
    second seat. See `run_fetch`: only an `"auth"` result triggers a re-login + retry.
    Set `PAPERFETCH_OVID_FALLBACK=0` to disable the route entirely.

    ==Returns a REASON==: `"pdf"` | `"auth"` (this proxy subdomain has no auth handshake
    yet — proxy authorization is granted PER subdomain, so being authorized on the LWW
    subdomain does NOT authorize the OCE one) | `"fail"`.
    """
    prefix = doi.split("/")[0]
    if os.environ.get("PAPERFETCH_OVID_FALLBACK") == "0":
        print("[ovid] route disabled by PAPERFETCH_OVID_FALLBACK=0", file=sys.stderr)
        return "fail"
    left = _ovid_e3_cooldown_left()
    if left:
        _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "ovid",
              "status": "cooldown", "an": an, "left_s": left})
        print(f"[ovid] skipping — E3 licence-seat cooldown, {left // 60}m{left % 60}s left.\n"
              f"[ovid] Meanwhile, by hand: https://{_proxy_host('oce.ovid.com')}/article/{an}/HTML"
              f" → click PDF.", file=sys.stderr)
        return "fail"
    article = viewer_url or f"https://{_proxy_host('oce.ovid.com')}/article/{an}/HTML"
    # The article page mounts the pdf.js viewer by itself — nothing to click. Watch the
    # network: the viewer request carries `?file=<signed assets.ovid.com URL>`, and the
    # PDF itself comes back as content-type application/pdf.
    seen = {"pdf": None, "viewer": None}

    def _on_resp(r):
        try:
            ct = (r.headers or {}).get("content-type", "").lower()
        except Exception:
            return
        if "application/pdf" in ct and not seen["pdf"]:
            seen["pdf"] = r.url
        elif "/pdfviewer/" in r.url and "file=" in r.url and not seen["viewer"]:
            seen["viewer"] = r.url

    page.on("response", _on_resp)
    _throttle()
    try:
        _mark(f"ovid: goto article {article[:90]}")
        page.goto(article, wait_until="domcontentloaded")
        # ==Do NOT judge the URL yet.== The first navigation to a *new* proxy subdomain
        # runs the proxy's JS-redirect handshake, and mid-handshake the URL legitimately
        # sits on the login/interstitial page. Checking immediately reports a false "auth"
        # even straight after a successful login.
        page.wait_for_timeout(4000)
        _mark(f"ovid: after handshake wait, url={page.url[:100]}")

        # The viewer then mounts asynchronously (~15 s in the trace), so a fixed short sleep
        # misses it. Poll until the PDF response (or the viewer request) shows up.
        # Time-based, not iteration-based: an inline login mid-poll resets the budget, so
        # a login at second 25 doesn't leave the viewer only 5 s to mount.
        html = ""
        tried_inline_login = False
        deadline = time.time() + OVID_VIEWER_WAIT_S
        parked_since = None            # first moment we saw ourselves on a /login page
        while time.time() < deadline:
            page.wait_for_timeout(1000)
            if seen["pdf"]:
                break
            if "/login" in page.url:
                parked_since = parked_since or time.time()
                # Parked on a login page. If an actual login FORM is present, submit it
                # HERE (its ?next= chain is what completes the per-subdomain proxy
                # handshake). Returning "auth" for the caller to re-run login() is a
                # no-op when the gate session is still valid — see _login_submit_here.
                if (not tried_inline_login and time.time() - parked_since > 4
                        and page.locator(PASS_SEL).count() > 0):
                    tried_inline_login = True
                    _mark(f"ovid: login form on bounce page {page.url[:100]} — inline login")
                    if not _login_submit_here(page):
                        _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "ovid",
                              "status": "auth_expired", "step": "article_inline_login"})
                        return "auth"
                    _mark("ovid: inline login OK — re-goto article")
                    page.goto(article, wait_until="domcontentloaded")
                    page.wait_for_timeout(4000)
                    deadline = time.time() + OVID_VIEWER_WAIT_S   # fresh viewer budget
                    parked_since = None
                    continue
                if time.time() - parked_since > 20:
                    # No form (or already retried) and still parked well past any
                    # plausible handshake — a genuine auth failure.
                    _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "ovid",
                          "status": "auth_expired", "step": "article",
                          "url": page.url[:120]})
                    return "auth"
                continue
            parked_since = None
            html = page.content()
            if any(mk in html for mk in _E3_MARKERS):
                _ovid_trip_e3(doi, an)
                return "fail"
            if seen["viewer"]:
                page.wait_for_timeout(2000)   # give the PDF response a moment to land
                break
        html = html or page.content()
        if any(mk in html for mk in _E3_MARKERS):
            _ovid_trip_e3(doi, an)
            return "fail"
    except Exception as e:
        _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "ovid",
              "status": _classify_exc(e), "step": "article", "note": repr(e)[:100]})
        return "fail"
    finally:
        try:
            page.remove_listener("response", _on_resp)
        except Exception:
            pass

    pdf_url, viewer = _ovid_pick_pdf_url(seen["pdf"], seen["viewer"], html, article)
    if not pdf_url:
        _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "ovid",
              "status": "no_pdfurl", "bytes": len(html)})
        print(f"[ovid] no PDF url on article page (an={an}).{_sfx_hint(doi)}",
              file=sys.stderr)
        return "fail"
    referer = viewer or article

    # The assets URL is already signed, so it serves directly (verified). Keep the
    # proxy-rewritten form as a fallback in case the signature is IP/proxy-bound.
    pu = urlsplit(pdf_url)
    candidates = [pdf_url]
    if pu.netloc and not pu.netloc.endswith(PROXY_SUFFIX):
        candidates.append(pdf_url.replace(pu.netloc, _proxy_host(pu.netloc), 1))

    ok = False
    for cand in candidates:
        for attempt in range(1, 3):
            try:
                rp = page.request.get(cand, headers={"referer": referer},
                                      timeout=NAV_TIMEOUT_MS)
                body = rp.body()
            except Exception as e:
                _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "ovid",
                      "status": _classify_exc(e), "step": "pdf", "note": repr(e)[:100]})
                break
            if any(mk.encode() in body[:4000] for mk in _E3_MARKERS):
                _ovid_trip_e3(doi, an)
                return "fail"
            status = _classify(rp, body)
            _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "ovid",
                  "status": status, "http": rp.status, "bytes": len(body),
                  "attempt": attempt, "direct": cand is candidates[0]})
            if status == "pdf":
                out.write_bytes(body)
                print(f"[ovid] got PDF via Ovid OCE -> {out} ({len(body)}B)", file=sys.stderr)
                ok = True
                break
            if rp.status == 503 and attempt < 2:      # proxy warming the backend
                page.wait_for_timeout(3000)
                continue
            break
        if ok:
            break

    # Release the Ovid licence seat immediately — leaving the viewer open holds it.
    try:
        page.goto("about:blank", wait_until="domcontentloaded")
    except Exception:
        pass
    if not ok:
        print(f"[ovid] PDF fetch failed (an={an}).{_sfx_hint(doi)}", file=sys.stderr)
    return "pdf" if ok else "fail"


def _sfx_lww_target(page, doi: str) -> str | None:
    """SFX detailed XML → the LWW `getFullTxt` target_url.

    Standard ExLibris SFX feature: appending `&sfx.response_type=multi_obj_detailed_xml`
    to the resolver URL returns machine-readable targets. The LWW target is an OvidSP DOI
    query link that 302s to the *subscribed* `oce-ovid-com/article/{an}/HTML` (the AN is
    right there in the URL). Plain GET on the public SFX endpoint — takes no Ovid seat.
    Returns None when SFX is unconfigured, the query fails, or there is no LWW target."""
    if not SFX:
        print("[sfx] institution.sfx_base is blank — cannot resolve the classic-Ovid "
              "platform without it", file=sys.stderr)
        return None
    u = f"{SFX.format(doi=doi)}&sfx.response_type=multi_obj_detailed_xml"
    try:
        raw = page.request.get(u, timeout=NAV_TIMEOUT_MS).body().decode("utf-8", "ignore")
    except Exception as e:
        print(f"[sfx] query failed: {repr(e)[:80]}", file=sys.stderr)
        return None
    dec = _html.unescape(_html.unescape(raw))
    for t in re.findall(r"<target>(.*?)</target>", dec, re.S):
        if (re.search(r"<service_type>\s*getFullTxt\s*</service_type>", t)
                and "LWW" in t):
            m = re.search(r"<target_url>([^<]+)", t)
            if m:
                return m.group(1)
    return None


def _citation_meta_pdf(page, doi: str, out: Path, nav: bool = False,
                       host: str | None = None) -> str:
    """Generic multi-step route: resolver → article HTML → `citation_pdf_url` meta →
    PDF fetched with `Referer`. Returns "pdf"|"auth"|"fail" (same convention as
    _lww_ovid_pdf).

    Many publishers that have no DOI→PDF *template* still advertise the exact PDF URL in
    a `<meta name="citation_pdf_url">` tag on the article page (Google Scholar relies on
    it). That's a whole class of "hard" publishers solved without any reverse-engineering.

    nav=False (JAMA/OUP/Pediatrics/ERJ/JNS/Nature): fully headless request.get.
    nav=True (BMJ/AJNR/JNM): the resolver step runs as a REAL headful `page.goto`, and
      the context must be headful — these sites' Cloudflare returns "Attention Required!"
      or "Just a moment..." to headless (including request.get from a headful context);
      a headful real navigation passes on the first try. ==A "WAF dead end" verdict
      reached headless is only valid for headless.==
    host=None → the generic doi-org proxy resolver; a host string → that site's own
      `/lookup/doi/{doi}` (Highwire sites redirect-loop on doi-org).
    The PDF step is always request.get + Referer (same context, not re-blocked).

    Add a prefix to ROUTES once you've confirmed the publisher's PDF endpoint really
    returns bytes **for an article your library holds** — an unentitled article returns
    reader HTML even when the meta is present, which means no entitlement, not a broken
    route."""
    prefix = doi.split("/")[0]
    _throttle()
    resolver = (f"https://{_proxy_host(host)}/lookup/doi/{doi}" if host
                else f"https://doi-org.{PROXY_SUFFIX}/{doi}")
    for attempt in (1, 2):
        try:
            if nav:
                r = page.goto(resolver, wait_until="domcontentloaded",
                              timeout=NAV_TIMEOUT_MS)
                body = page.content().encode("utf-8", "ignore")
            else:
                r = page.request.get(resolver, timeout=NAV_TIMEOUT_MS)
                body = r.body()
        except Exception as e:
            _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "meta",
                  "status": _classify_exc(e), "step": "doi_resolve", "note": repr(e)[:100]})
            return "fail"
        if nav and page.locator(PASS_SEL).count() > 0:
            # Bounced to a login page → submit the form THERE (per-subdomain proxy
            # handshake), then retry.
            if attempt == 2 or not _login_submit_here(page):
                _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "meta",
                      "status": "auth_expired", "step": "doi_resolve"})
                return "auth"
            continue
        head = body[:3000].lower()
        if b"attention required" in head or b"just a moment" in head:
            _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "meta",
                  "status": "cf_block", "step": "doi_resolve", "nav": nav,
                  "url": (page.url if nav else r.url)[:120]})
            print(f"[meta] Cloudflare blocked the resolver (nav={nav}); "
                  f"if nav=False, nav=True usually passes", file=sys.stderr)
            return "fail"
        if "/login" not in (r.url if not nav else page.url):
            break
        if attempt == 2:
            _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "meta",
                  "status": "auth_expired", "step": "doi_resolve"})
            return "auth"
        # Bounced to login → open the bounce page and submit the form there (completes
        # the per-subdomain proxy handshake), then retry once.
        page.goto(r.url, wait_until="domcontentloaded")
        if not _login_submit_here(page):
            return "auth"
    html = body.decode("utf-8", "ignore")
    m = re.search(r'citation_pdf_url"\s+content="([^"]+)"', html)
    if not m:
        _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "meta",
              "status": "no_pdf_meta", "http": r.status, "bytes": len(body),
              "url": r.url[:120]})
        print(f"[meta] no citation_pdf_url on {r.url[:100]}", file=sys.stderr)
        return "fail"
    pdf_url = m.group(1)
    host = urlsplit(pdf_url).netloc
    if PROXY_SUFFIX.split(":")[0] not in host:   # meta gave the public host → rewrite
        pdf_url = pdf_url.replace(host, _proxy_host(host), 1)
    try:
        rp = page.request.get(pdf_url, headers={"referer": r.url}, timeout=NAV_TIMEOUT_MS)
        pb = rp.body()
    except Exception as e:
        _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "meta",
              "status": _classify_exc(e), "step": "pdf", "note": repr(e)[:100]})
        return "fail"
    status = _classify(rp, pb)
    _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "meta", "status": status,
          "http": rp.status, "bytes": len(pb),
          "cf_mitigated": rp.headers.get("cf-mitigated")})
    if status == "pdf":
        out.write_bytes(pb)
        print(f"[meta] OK -> {out} ({len(pb)} bytes)", file=sys.stderr)
        return "pdf"
    _warn_if_blocked(status)
    print(f"[meta] {status} (http {rp.status}, {len(pb)}B)", file=sys.stderr)
    return "fail"


def _lww_ovid_pdf(page, doi: str, out: Path) -> str:
    """LWW/Ovid (journals.lww.com) full text via the institutional proxy.
    Falls back to `_ovid_oce_pdf` when the viewer carries no signed pdfUrl (AOP articles).

    ==Returns a REASON, not a bool==: `"pdf"` (written) | `"auth"` (session/proxy lapsed —
    caller may re-login and retry once) | `"fail"` (no route / no PDF — retrying is useless
    and would hit Ovid again, costing another concurrent licence seat).

    ==REQUIRES a headful context== — the proxy's "please wait" JS-redirect interstitial on
    the proxy doi-resolver (and the first hit to the pdfs-* subdomain) only completes in
    a headed browser (headless chromium AND headless real Chrome both hang on it).

    The reverse-engineered flow (network trace + patchright verify):
      1. `doi-org.<proxy>/{doi}`  → article page on `journals-lww-com.<proxy>`
         (the real LWW URL is not DOI-based; the proxy resolver lands us on it).
      2. Scrape the article number `an` (########-#########-##### in the PDF button's
         `PDFDownloadInit` config) + the journal slug (first path segment).
      3. GET `/{journal}/_layouts/15/oaks.journals/downloadpdf.aspx?trckng_src_pg=
         ArticleViewer&an={an}` with `Referer: <article>` → a ~80 KB *viewer* HTML
         (NOT the PDF) whose inline JSON `pdfDownloadDetails.pdfUrl` is the real,
         signed PDF URL on `pdfs-journals-lww-com.<proxy>` (token=method|ExpireAbsolute;
         source|Journals;ttl|<ms>;payload|<b64>;hash|<b64>).
      4. GET that signed pdfUrl ==with `Referer: <downloadpdf.aspx>`== → PDF bytes.
         The Referer MUST be the downloadpdf viewer; an article/empty Referer → HTTP 503
         (that 503 is what makes this look like a dead end). The pdfs backend may also
         503 briefly while the proxy warms it → retry a few times.

    The technique (resolve → scrape id → viewer → signed URL with the right Referer
    chain) generalizes to other signed-URL publishers.
    """
    prefix = doi.split("/")[0]
    _throttle()
    # 1) resolve DOI → article page (headful passes the "please wait" interstitial)
    try:
        _mark("lww: goto doi-org proxy resolver")
        page.goto(f"https://doi-org.{PROXY_SUFFIX}/{doi}", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)   # let the proxy redirect + article render
        art_url = page.url
        _mark(f"lww: resolver landed on {art_url[:100]}")
        if "/login" in art_url and page.locator(PASS_SEL).count() > 0:
            # Log in on the bounce page ITSELF (its ?next=/?url= chain completes the
            # proxy handshake). Returning "auth" for the caller's login() is a no-op
            # whenever the gate session is still valid — see _login_submit_here.
            _mark("lww: login form on bounce page — inline login")
            if _login_submit_here(page):
                page.goto(f"https://doi-org.{PROXY_SUFFIX}/{doi}",
                          wait_until="domcontentloaded")
                page.wait_for_timeout(4000)
                art_url = page.url
                _mark(f"lww: resolver retry landed on {art_url[:100]}")
        if "/login" in art_url:
            _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "lww",
                  "status": "auth_expired", "step": "doi_resolve"})
            return "auth"
        # LWW DOIs land on one of two Ovid platforms; log the landing so the
        # www-ovid-com branch accrues end-to-end verification during daily use.
        platform = ("www-ovid-com" if "www-ovid-com" in art_url
                    else "journals-lww-com" if "journals-lww-com" in art_url
                    else "other")
        _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "lww",
              "status": "landing", "platform": platform, "url": art_url[:120]})
        html = page.content()
    except Exception as e:
        _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "lww",
              "status": _classify_exc(e), "step": "doi_resolve", "note": repr(e)[:100]})
        return "fail"

    # 1b) SECOND Ovid platform. The proxy DOI resolver lands LWW DOIs on EITHER
    # `journals-lww-com` (new LWW Journals, AN + downloadpdf.aspx — handled below) OR the
    # classic `www-ovid-com/jnls/{journal}/fulltext/{doi}~{slug}` reader.
    if "www-ovid-com" in art_url:
        # ⚠ An "LWW Total Access"-style licence may live ONLY on the OCE/journals-lww
        # platforms — on classic www-ovid the same subscribed article shows /abstract/
        # and bounces /pdf/ back to /fulltext/. Neither proves "not subscribed"; it means
        # THIS platform isn't licensed. The reliable move: ask SFX for the LWW getFullTxt
        # target — its OvidSP DOI query link 302s to the *licensed*
        # oce-ovid-com/article/{an}/HTML (the AN is in the URL), which the existing
        # _ovid_oce_pdf handles.
        tgt = _sfx_lww_target(page, doi)
        an2 = None
        if tgt:
            try:
                _mark("ovid-www: goto SFX OvidSP target")
                page.goto(tgt, wait_until="domcontentloaded")
                for _ in range(30):
                    page.wait_for_timeout(1000)
                    m2 = re.search(r"/article/(\d{8}-\d{9}-\d{5})/", page.url)
                    if m2:
                        an2 = m2.group(1)
                        break
            except Exception as e:
                _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "ovid_www",
                      "status": _classify_exc(e), "step": "sfx_target", "note": repr(e)[:100]})
                return "fail"
        if not an2:
            _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "ovid_www",
                  "status": "no_sfx_ovid_an", "url": page.url[:120]})
            print(f"[ovid-www] SFX/OvidSP did not land on an OCE article.{_sfx_hint(doi)}",
                  file=sys.stderr)
            return "fail"
        _mark(f"ovid-www: OCE an={an2} via SFX — handing to _ovid_oce_pdf")
        return _ovid_oce_pdf(page, doi, an2, out)

    # 2) scrape article-number + journal slug
    m = re.search(r'an=?["\']?\s*(\d{8}-\d{9}-\d{5})', html) or re.search(r'\b(\d{8}-\d{9}-\d{5})\b', html)
    jm = re.search(r'//journals-lww-com\.[^/]+/([^/]+)/', art_url)
    if not m or not jm:
        _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "lww",
              "status": "no_an", "url": art_url[:120]})
        print(f"[lww] could not scrape AN/journal from {art_url}", file=sys.stderr)
        return "fail"
    an, journal = m.group(1), jm.group(1)

    # 3) fetch the viewer HTML (Referer = article) → extract signed pdfUrl
    dlpdf = (f"https://journals-lww-com.{PROXY_SUFFIX}/{journal}"
             f"/_layouts/15/oaks.journals/downloadpdf.aspx?trckng_src_pg=ArticleViewer&an={an}")
    try:
        vhtml = page.request.get(dlpdf, headers={"referer": art_url},
                                 timeout=NAV_TIMEOUT_MS).body().decode("utf-8", "ignore")
    except Exception as e:
        _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "lww",
              "status": _classify_exc(e), "step": "viewer", "note": repr(e)[:100]})
        return "fail"
    vd = vhtml.replace("&quot;", '"').replace("&amp;", "&")
    pm = re.search(r'"pdfUrl"\s*:\s*"([^"]+)"', vd)
    if not pm:
        # No signed pdfUrl in the journals.lww.com viewer. This is COMMON for
        # publish-ahead-of-print (`an` volume `990000000`) and does NOT mean the PDF
        # doesn't exist — Ovid (oce-ovid-com) still serves it. Fall through to Ovid.
        aop = "990000000" in (an or "")
        _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "lww",
              "status": "no_pdfurl", "aop": aop, "bytes": len(vhtml)})
        print(f"[lww] no pdfUrl in viewer (an={an}{', ahead-of-print' if aop else ''}) "
              f"→ trying Ovid OCE", file=sys.stderr)
        return _ovid_oce_pdf(page, doi, an, out)   # propagates "auth" so caller re-logins
    pdf_url = pm.group(1)

    # 4) fetch the signed PDF (Referer = downloadpdf viewer; retry on proxy 503)
    for attempt in range(1, 7):
        try:
            rp = page.request.get(pdf_url, headers={"referer": dlpdf}, timeout=NAV_TIMEOUT_MS)
            body = rp.body()
        except Exception as e:
            _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "lww",
                  "status": _classify_exc(e), "step": "pdf", "note": repr(e)[:100]})
            return "fail"
        if body[:4] == b"%PDF":
            out.write_bytes(body)
            _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "lww",
                  "status": "pdf", "bytes": len(body), "attempt": attempt, "an": an})
            print(f"[lww] OK -> {out} ({len(body)} bytes, attempt {attempt})", file=sys.stderr)
            return "pdf"
        if rp.status != 503:
            _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "lww",
                  "status": _classify(rp, body), "http": rp.status, "bytes": len(body),
                  "cf_mitigated": rp.headers.get("cf-mitigated")})
            print(f"[lww] non-PDF http {rp.status} ({len(body)}B)", file=sys.stderr)
            return "fail"
        page.wait_for_timeout(3000)   # proxy warming the pdfs backend → retry
    _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "lww",
          "status": "pdf_503_exhausted", "an": an})
    print("[lww] pdfs backend kept 503-ing after retries", file=sys.stderr)
    return "fail"


def _sfx_hint(doi: str) -> str:
    return f" SFX: {SFX.format(doi=doi)}" if SFX else ""


def run_fetch(pw, doi: str, out: Path) -> bool:
    """Three-layer ladder: ① paper_fetch's API/OA/TDM (no proxy) → ② holdings entitlement
    pre-check → ③ dispatch the proxy route by ROUTES[prefix]["kind"] (tpl / meta / lww).

    Headless is the default (patchright clears most CF). Two cases MUST be headful:
      · lww  — the proxy's "please wait" JS interstitial hangs headless
      · meta with nav=True (BMJ-class) — their CF only passes a real navigation
    `is_logged_in` (the gate page) can report VALID while the per-subdomain proxy
    authorization has separately expired → the proxy returns an auth page. So any proxy
    "auth" failure forces one fresh login + retry."""
    prefix = doi.split("/")[0]
    route = ROUTES.get(prefix, {})
    kind = route.get("kind")
    needs_headful = kind == "lww" or (kind == "meta" and route.get("nav"))
    ctx = _new_context(pw, headless=not needs_headful)
    _mark("restore_session")
    restore_session(ctx)
    page = ctx.new_page()
    _mark("new_page OK")
    try:
        # Layer 1 — API/OA/TDM (no proxy, no CF, no login). Works out of the box.
        if _try_paper_fetch(doi, out):
            print(f"[fetch] got via API/OA/TDM route -> {out}", file=sys.stderr)
            return True
        if not ensure_login(page):
            print("[fetch] login failed", file=sys.stderr)
            return False
        # Layer 2 — holdings entitlement pre-check (see _entitlement's two traps)
        global _CUR_ENT
        ent = _CUR_ENT = _entitlement(doi)
        sub, covered = ent.get("subscribed"), ent.get("covered")
        _log({"kind": "holdings", "doi": doi, "prefix": prefix, "subscribed": sub,
              "covered": covered, "platform": ent.get("platform")})
        if sub:
            print(f"[holdings] {ent.get('platform')} · {ent.get('coverage')}", file=sys.stderr)
            if covered is False:
                # Journal subscribed, but this article's year is outside coverage → the
                # proxy will likely return reader HTML. Still try (coverage strings can
                # lag), but say so up front, so a failure isn't misread as a broken route.
                print(f"[holdings] ⚠ article year ({ent.get('year')}) outside coverage → "
                      f"the proxy will likely return reader HTML (NOT a broken route)",
                      file=sys.stderr)
        elif sub is None:
            print("[holdings] journal not in the holdings table → entitlement unknown, "
                  "trying the proxy anyway (database-level platforms look like this)",
                  file=sys.stderr)
        if (sub is False or covered is False) and os.environ.get("PAPERFETCH_SKIP_UNSUB") == "1":
            print(f"[fetch] PAPERFETCH_SKIP_UNSUB=1 → skipping the proxy.{_sfx_hint(doi)}",
                  file=sys.stderr)
            _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "n/a",
                  "status": "skip_no_entitlement"})
            return False
        # Layer 3 — proxy route by kind. Each is "run once → re-login+retry ONLY on auth
        # expiry": a re-login fixes nothing else, and re-running LWW costs another Ovid
        # concurrent-licence seat (self-inflicted E3).
        if kind == "lww":
            attempt = lambda: _lww_ovid_pdf(page, doi, out)
        elif kind == "meta":
            attempt = lambda: _citation_meta_pdf(page, doi, out, nav=bool(route.get("nav")),
                                                 host=route.get("host"))
        elif kind == "tpl":
            # _proxy_pdf returns bool; wrap as "pdf"/"auth". It cannot distinguish auth
            # expiry, so a failure always gets one fresh-login retry.
            attempt = lambda: "pdf" if _proxy_pdf(page, doi, out, allow_nav=False) else "auth"
        else:
            _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "n/a",
                  "status": "no_route", "subscribed": sub})
            print(f"[fetch] no proxy route for this publisher ({prefix}).{_sfx_hint(doi)}",
                  file=sys.stderr)
            if sub:
                print("[fetch] ⚠ but holdings says SUBSCRIBED → worth adding a route (first "
                      "check the 'no route, reason established' list at the bottom of ROUTES "
                      "— it may be a known library-side proxy issue)", file=sys.stderr)
            return False

        st = attempt()
        if st == "pdf":
            return True
        if st == "auth":
            print("[fetch] auth expired → fresh login (refreshes proxy authorization), "
                  "then one retry", file=sys.stderr)
            if login(page) and attempt() == "pdf":
                return True
        print(f"[fetch] {kind} route could not fetch {doi}.{_sfx_hint(doi)}", file=sys.stderr)
        return False
    finally:
        ctx.close()


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
    # Real rate/anti-bot blocks — the signal for the daily ceiling.
    blocks = [r for r in recs if r.get("status") in ("cf_challenge", "cf_block", "rate_limited")]
    # Ovid licence-seat failures are raised ABOVE the proxy, so _classify never sees them.
    # They are a different signal from rate/CF blocks: a seat is occupied, not a ban.
    e3 = [r for r in recs if r.get("status") == "license_seat_e3"]
    if e3:
        print(f"  🎟 Ovid licence-seat failures (E3): {len(e3)} — a concurrent seat was taken "
              f"(close your own Ovid tabs). Most recent: {e3[-1]['ts']}")
        left = _ovid_e3_cooldown_left()
        if left:
            print(f"     cooldown active: {left // 60}m{left % 60}s remaining")
    # auth_expired = our session lapsed (fixed by re-login), NOT a server-side block.
    auth = [r for r in recs if r.get("status") == "auth_expired"]
    pdfs = [r for r in recs if r.get("status") == "pdf"]
    print(f"access log: {ACCESS_LOG}  ({len(recs)} events)")
    print(f"  requests last 1h / 24h : {within(1)} / {within(24)}")
    print(f"  PDF successes total    : {len(pdfs)}")
    print(f"  session re-auths (not blocks): {len(auth)}")
    print("  status breakdown       :", dict(by_status))
    if blocks:
        print(f"  ⚠ REAL blocks ({len(blocks)}) — ceiling signal, most recent:")
        for r in blocks[-5:]:
            print(f"    {r.get('ts')}  {r.get('status')}  {r.get('prefix','')}  {r.get('doi','')}")
        if FEEDBACK_CONTACT:
            print(f"  → report the daily request count at the block to {FEEDBACK_CONTACT} "
                  "to help calibrate the real ceiling.")
    else:
        print("  ✅ no rate/CF blocks ever — ceiling not hit")


def print_routes() -> None:
    """Route health check — answers "which publisher isn't automated yet" without a
    manual holdings audit.

    ① Per-prefix scorecard from the access log. Failures print subscribed/covered
      alongside — that is the dividing line between "route broken" and "this article was
      never entitled", the single most common misdiagnosis in this problem space.
    ② Holdings gaps: subscribed articles hit a prefix that ROUTES doesn't know → a route
      worth adding."""
    from collections import Counter, defaultdict
    recs = [r for r in _read_log() if r.get("kind") == "proxy" and r.get("prefix")]
    per = defaultdict(Counter)
    for r in recs:
        per[r["prefix"]][r.get("status", "?")] += 1

    print("=== ROUTES table vs. real-world scorecard ===")
    for prefix in sorted(ROUTES):
        kind = ROUTES[prefix]["kind"]
        c = per.get(prefix, Counter())
        ok = c["pdf"]
        bad = sum(v for k, v in c.items() if k != "pdf")
        tag = "✅" if ok else ("⚠" if bad else "·")
        detail = "" if not bad else "  failures: " + ", ".join(
            f"{k}×{v}" for k, v in c.most_common() if k != "pdf")
        print(f" {tag} {prefix:9s} {kind:5s}  pdf×{ok}{detail}")
        hot = [r for r in recs if r["prefix"] == prefix
               and r.get("status") != "pdf" and r.get("subscribed")]
        for r in hot[-2:]:
            print(f"      ↳ subscribed yet failed (worth a look): "
                  f"{r.get('journal') or r.get('doi')}"
                  f" · {r.get('status')} · covered={r.get('covered')}")

    print("\n=== Holdings gaps (subscribed, but no proxy route) ===")
    gaps = sorted({r["prefix"] for r in recs
                   if r["prefix"] not in ROUTES and r.get("subscribed")})
    for p in gaps:
        print(f"  ⚠  {p} — subscribed articles hit it, but ROUTES has no entry → add one")
    if not gaps:
        print("  ✅ no 'subscribed but routeless' prefix in the access log")
    print("\n  Known deliberate absences (see the note at the bottom of ROUTES):")
    print("    prefixes can be missing because there is genuinely no online entitlement,")
    print("    or because the LIBRARY's proxy has the subdomain unregistered "
          "(status `proxy_host_unregistered`) — report those to the library.")
    print("\n  ⚠ These gaps only cover DOIs actually TRIED. For the full holdings picture "
          "run `python holdings.py platforms` (all subscribed platforms + journal counts)")


# --- CLI ------------------------------------------------------------------
def main(argv):
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "stats":
        print_stats()
        return 0
    if cmd == "routes":
        print_routes()
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
        # the playwright context leaves that window unprotected. The lock's own bounded
        # wait covers the queueing phase, so nothing is unguarded.
        wd = _arm_watchdog(label)
        try:
            _mark("starting patchright driver")
            with sync_playwright() as pw:
                _mark("driver up")
                if cmd == "fetch":
                    return 0 if run_fetch(pw, argv[1], Path(argv[2])) else 2

                # `login` needs a real window: the proxy's JS-redirect interstitial
                # never completes headless. `check` only hits the gate's login page → headless.
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
