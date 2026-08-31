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
    python library_session.py fetch <DOI> <out.pdf> --title "<article title>"
                                                       # ...plus content verification:
                                                       # is this the article, or the whole
                                                       # supplement it sits in? (pdf_verify)
    python library_session.py fetch <DOI> <out.pdf> --skip-layer1
                                                       # go straight to the proxy route —
                                                       # for when layer 1 (OA/TDM) returned
                                                       # something the gate let through but
                                                       # a human can see is not the article
                                                       # (env: PAPERFETCH_SKIP_LAYER1=1)
    python library_session.py stats                    # access-log summary / block analysis
    python library_session.py routes                   # per-route scorecard + holdings gaps

BATCH DISCIPLINE (learned the expensive way, 2026-08-21):
    Run `check` BEFORE a batch, and ABORT THE WHOLE BATCH if the session is invalid.
    A batch that starts with a dead session produces one `fetch` failure per paper, and
    every one of them reads as "this paper has no route" — 9 papers were written off as
    paywalled that way, when a single `login` (which is fully automatic) fixed all of them.
    ==Any "no full text available" conclusion is only valid if the session was valid at
    the time.== That is why auth now has its own exit code instead of sharing 2.

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

Exit codes: 0 ok · 1 usage · 2 a route ran and came back empty · 3 AUTH (login failed /
  session expired) · 4 profile busy (lock) · 5 watchdog abort · 6 no route for this
  publisher prefix.
  ==Only 2 is evidence about the PAPER.== 3 says nothing about it (fix the session and
  retry); 6 says the publisher has no route yet (holdings may still say subscribed);
  4 and 5 mean "retry serially". Before 2026-08-22 codes 2/3/6 were one code, and a batch
  run with an expired session logged nine "no full text" verdicts that were all wrong.
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
import signal
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

# ClinicalKey knobs (config.yaml `clinicalkey:`) — used by the `ck` route (_ck_pdf).
# institution_match: regex picked against the "Choose organization" modal's button texts
# the FIRST time this profile enters CK (the choice is then remembered by the profile).
CK_INSTITUTION = ((CFG.get("clinicalkey") or {}).get("institution_match") or "")
CK_BOOT_TIMEOUT_S = int(os.environ.get("PAPERFETCH_CK_BOOT_S", "120"))  # SPA bootstrap wait
CK_POLL_S = 10

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
    # Springer/BMC: the canonical /content/pdf URL 404s while an article is IN PRESS,
    # but the landing page's Download button serves an Article-in-Press proof at
    # {doi}_reference.pdf (observed 2026-08-19: canonical 404, _reference.pdf = 70-page
    # proof). citation_pdf_url still advertises the 404 canonical URL, so a meta route
    # would not find it either — hence the explicit alt path.
    "10.1007": {"kind": "tpl", "host": "link.springer.com",       "path": "/content/pdf/{doi}.pdf",
                "alt_paths": ["/content/pdf/{doi}_reference.pdf"]},                                               # Springer
    "10.1186": {"kind": "tpl", "host": "link.springer.com",       "path": "/content/pdf/{doi}.pdf",
                "alt_paths": ["/content/pdf/{doi}_reference.pdf"]},                                               # BMC ⚠ real PDF lives on per-journal *.biomedcentral.com, often 404
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
    # Nature portfolio: news/comment items (and some older articles) carry NO
    # citation_pdf_url — the meta route then reports `no_pdf_meta` on a perfectly
    # entitled article (observed 3× on 10.1038/466914a, 215 KB article page, 2026-08-21).
    # Their PDF URL is derivable from the landing URL, so give the route a fallback.
    "10.1038": {"kind": "meta", "pdf_from_landing": "{landing}.pdf"},
    # Endocrine Society (JCEM / Endocrine Reviews / JES) — same Silverchair platform as
    # OUP 10.1093. Added 2026-08-22 after two JCEM papers logged `no_route` and had to be
    # fetched by hand. ==Silverchair specifics (verified end-to-end in a real browser)==:
    # doi.org → article page → citation_pdf_url → that URL 302s to a
    # watermarkNN.silverchair.com token URL whose body is the PDF and which needs NO
    # cookie; a non-navigation fetch of the article-pdf URL is 403 (CF allows navigations
    # only), and the FIRST navigation sometimes bounces back to the article page — the
    # second succeeds. If this route logs cf_block, add `"nav": True` (and keep the
    # one built-in retry in mind — it is what covers the bounce).
    "10.1210": {"kind": "meta"},                                 # Endocrine Society
    # Bioscientifica (J Endocrinol / J Mol Endocrinol / Endocrine Connections / JOP ...) -
    # ==the same Silverchair platform as 10.1093 and 10.1210==: doi.org lands on
    # journals.bioscientifica.com/joe/article/<vol>/<issue>/<eid>/<id>/<slug>, which is the
    # Silverchair article-URL shape, so the citation_pdf_url route applies unchanged. Added
    # 2026-08-29: 10.1530/joe-25-0081 had been logged `not_retrieved` with exit 6, and exit 6
    # is "no route for this prefix", never evidence about the paper. A plain request to the
    # landing page answers HTTP 403 with a Cloudflare "Just a moment" body. Headless was tried
    # first and the resolver logged `cf_challenge (http 403, nav=False)`, so this route carries
    # `nav: True` like BMJ/AJNR/JNM - their WAF passes a real navigation and refuses everything
    # else. Not in the A-Z holdings table -> entitlement UNKNOWN, not no-go; a `fail` here means
    # "a route ran and came back empty", which is the first honest evidence this article has
    # ever had (it was previously logged not_retrieved on exit 6 = no route for this prefix).
    # 2026-08-31 outcome: even headful nav got 403 (publisher 24h request-cap notice) and SFX
    # lists no full-text target → the reference library has no subscription; not a tool bug.
    "10.1530": {"kind": "meta", "nav": True},                    # Bioscientifica
    # --- meta + headful nav (Cloudflare blocks headless) ---
    # BMJ: headless is definitely blocked; headful `nav` passed on the first try when the
    # route was added (2026-07-14). ==It is not passing reliably any more== — 2026-08-21/22
    # logged cf_block×2 and one HTTP 403 with nav=True in effect. Treat a BMJ failure as
    # "Cloudflare, retry later / SFX by hand", NOT as evidence that `nav` is broken or that
    # the library lacks a subscription (holdings says BMJ Journals, covered=True).
    "10.1136": {"kind": "meta", "nav": True},                    # BMJ ⚠ CF-flaky since 2026-08
    "10.3174": {"kind": "meta", "nav": True, "host": "www.ajnr.org"},          # AJNR — CF "Just a moment"; doi-org resolver loops → /lookup/doi/
    "10.2967": {"kind": "meta", "nav": True, "host": "jnm.snmjournals.org"},   # J Nucl Med — same
    # --- lww (Ovid multi-step, headful; concurrent-licence seats apply) ---
    "10.1097": {"kind": "lww"},   # most LWW journals
    "10.1161": {"kind": "lww"},   # AHA (Circulation / Stroke)
    "10.1213": {"kind": "lww"},   # A&A / A&A Practice
    "10.2215": {"kind": "lww"},   # CJASN (moved to LWW; goes through the Ovid OCE branch)
    # --- ck (ClinicalKey, headful — see _ck_pdf for why) ---
    # 10.1016 Elsevier: paper_fetch's TDM API still goes first (layer 1) and covers most
    # articles. This route is the fallback for what TDM cannot serve: in-press articles
    # (TDM returns a cover sheet that pdf_gate rejects), published articles the TDM key has
    # no entitlement for (TDM returns HTTP 200 + the FIRST PAGE ONLY, flagged by the
    # `X-ELS-Status: WARNING … not entitled` response header — route_elsevier rejects on
    # that header since 1.5.1), and ClinicalKey-only titles. `fetch --skip-layer1` forces
    # this route when layer 1 is wrong in a way no check catches.
    # ScienceDirect-via-proxy is NOT an equivalent fallback — SD and CK are separate
    # subscriptions (the reference library holds 5 SD journals vs 953 CK journals).
    "10.1016": {"kind": "ck"},
    # --- no route, with the reason established (don't re-probe blindly) ---
    # Genuine dead ends at the reference library, kept as examples of WHY a prefix can be
    # absent (check your own holdings before copying these verdicts):
    #   10.2519 JOSPT — no online entitlement (full-text page = abstract + paywall).
    #   10.1055 Thieme / 10.1200 JCO / 10.1089 Liebert — the LIBRARY's proxy had those
    #     subdomains unregistered ("Host does not match" / error page) → report to the
    #     library; not fixable client-side. `_classify` flags this as
    #     `proxy_host_unregistered` so you can tell it apart.
}


# --- secret store ----------------------------------------------------------
# Three backends behind one interface, so the same code path works on every OS:
#   dpapi     — Windows DPAPI (CurrentUser, no entropy), the original store
#   keychain  — macOS login Keychain via `security`
#   env       — plain environment variables (documented in config.example.yaml)
# `auto` (the default) picks dpapi on Windows, keychain on macOS, env elsewhere.
# Precedence: SECRETS_BACKEND env var > config.yaml `secrets.backend` > "auto".
KEYCHAIN_SERVICE = os.environ.get("PAPERFETCH_KEYCHAIN_SERVICE", "paper-fetch")


_BACKENDS = ("dpapi", "keychain", "env", "none")


def _resolve_backend() -> str:
    choice = (os.environ.get("SECRETS_BACKEND")
              or (CFG.get("secrets") or {}).get("backend")
              or "auto").lower()
    if choice == "auto":
        if sys.platform == "win32":
            return "dpapi"
        if sys.platform == "darwin":
            return "keychain"
        return "env"
    if choice not in _BACKENDS:
        # Die at import, not at the first credential read: a typo'd backend name otherwise
        # surfaces as a stored secret gone missing (and `secret_set` would silently skip).
        sys.exit(f"secrets backend {choice!r} is not one of: auto, {', '.join(_BACKENDS)}. "
                 "Set `secrets.backend` in config.yaml or the SECRETS_BACKEND env var.")
    return choice


SECRETS_BACKEND = _resolve_backend()


def _store_hint(name: str) -> str:
    """How to store `name` under the active backend — printed on a miss."""
    if SECRETS_BACKEND == "dpapi":
        return f"powershell -File ~/.secrets/secret.ps1 set {name}"
    if SECRETS_BACKEND == "keychain":
        # -w LAST so `security` prompts, keeping the value out of shell history and `ps`.
        return f"security add-generic-password -s {KEYCHAIN_SERVICE} -a {name} -U -w"
    return f"export {name}=... (backend 'env')"


# --- DPAPI (pure python, CurrentUser, no entropy) ---------------------------
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_get(name: str) -> str | None:
    f = SECRETS_DIR / f"{name}.dpapi"
    if not f.exists():
        return None
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


def _dpapi_set(name: str, value: str) -> None:
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
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    (SECRETS_DIR / f"{name}.dpapi").write_text(base64.b64encode(enc).decode("ascii"))


# --- macOS Keychain ---------------------------------------------------------
def _keychain_run(*args: str) -> subprocess.CompletedProcess:
    """Run `security`, naming the cause when the binary isn't there.

    A bare FileNotFoundError from subprocess would be indistinguishable from the one
    `secret_get` raises for "secret not stored" — sending the user to look for a missing
    credential when the real problem is the backend choice.
    """
    try:
        return subprocess.run(["security", *args], capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError(
            "secrets backend 'keychain' needs macOS's security(1), which is not on PATH. "
            "Set `secrets.backend` (config.yaml) or SECRETS_BACKEND to 'dpapi' on Windows, "
            "'env' elsewhere.") from None


def _keychain_get(name: str) -> str | None:
    r = _keychain_run("find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name, "-w")
    if r.returncode != 0:
        return None
    # Strip only the newline `security` appends — a password may legitimately begin
    # or end with a space.
    return r.stdout.rstrip("\n") or None


def _keychain_set(name: str, value: str) -> None:
    # The value rides in argv, where `ps` can see it. macOS hides other users' arguments
    # from unprivileged `ps` and `security` offers no stdin path for -w, so this is the
    # available trade-off; the form documented for humans prompts instead.
    r = _keychain_run("add-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name,
                      "-U", "-w", value)
    if r.returncode != 0:
        # A locked Keychain (ssh session, no GUI) must not abort a fetch that already
        # succeeded. Not persisting costs one extra login — the same trade-off as 'env'.
        print(f"[secrets] keychain could not store '{name}' (security exit {r.returncode}: "
              f"{(r.stderr or '').strip()[:120]}); continuing", file=sys.stderr)


# --- public interface -------------------------------------------------------
def secret_get_opt(name: str) -> str | None:
    """Read a secret from the active backend; None if it is not stored."""
    if SECRETS_BACKEND == "dpapi":
        return _dpapi_get(name)
    if SECRETS_BACKEND == "keychain":
        return _keychain_get(name)
    if SECRETS_BACKEND == "env":
        return os.environ.get(name) or None
    if SECRETS_BACKEND == "none":
        return None
    raise ValueError(f"unknown secrets backend {SECRETS_BACKEND!r} "
                     "(expected dpapi, keychain, env, none, or auto)")


def secret_get(name: str) -> str:
    v = secret_get_opt(name)
    if v is None:
        raise FileNotFoundError(
            f"secret '{name}' not stored (backend '{SECRETS_BACKEND}'). "
            f"Store it with: {_store_hint(name)}")
    return v


def secret_set(name: str, value: str) -> None:
    if SECRETS_BACKEND == "dpapi":
        return _dpapi_set(name, value)
    if SECRETS_BACKEND == "keychain":
        return _keychain_set(name, value)
    # 'env' and 'none' have nowhere to persist to. Session cookies simply do not
    # survive across runs, which costs an extra login rather than failing the run.
    print(f"[secrets] backend '{SECRETS_BACKEND}' cannot persist '{name}'; skipping",
          file=sys.stderr)


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
def _posix_kill_descendants(pid: int) -> None:
    """Depth-first SIGKILL of everything below `pid` — the POSIX analogue of
    `taskkill /T /F`.

    Deliberately NOT os.killpg: this process usually shares its process group with the
    interactive shell that launched it, so killing the group would take the user's
    terminal down with it.
    """
    try:
        out = subprocess.run(["pgrep", "-P", str(pid)],
                             capture_output=True, text=True, timeout=10).stdout.split()
    except FileNotFoundError:
        # psutil is the preferred path and this is the fallback; with neither, a spawned
        # chromium outlives us. Say so — a silent return looks like a clean teardown.
        print("[watchdog] pgrep not found: cannot walk the process tree, a spawned chromium "
              "may be left running. `pip install psutil` to avoid this path.", file=sys.stderr)
        return
    except Exception:
        return
    for child in out:
        try:
            cpid = int(child)
        except ValueError:
            continue
        _posix_kill_descendants(cpid)
        try:
            os.kill(cpid, signal.SIGKILL)
        except Exception:
            pass


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
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/PID", str(os.getpid()), "/T", "/F"],
                                   capture_output=True, timeout=20)
                else:
                    _posix_kill_descendants(os.getpid())
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
    if sys.platform == "win32":
        SYNCHRONIZE = 0x00100000
        h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    try:
        os.kill(pid, 0)          # signal 0 = existence check, no signal delivered
    except ProcessLookupError:
        return False
    except PermissionError:
        return True              # alive, just owned by another user
    return True


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
            secret_set(f"LIB_COOKIE_{c['name'].upper().replace('-', '_')}", c["value"])
            saved.append(c["name"])
    print(f"[session] persisted cookies: {saved}", file=sys.stderr)


def restore_session(ctx) -> bool:
    cookies = []
    for name, domain in PERSIST_COOKIES.items():
        if not domain:
            continue
        val = secret_get_opt(f"LIB_COOKIE_{name.upper().replace('-', '_')}")
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


def _dismiss_overlays(page) -> None:
    """Dismiss a SweetAlert2 announcement modal if one covers the page.

    Some gates decorate their login page with a swal2 popup (site news, maintenance
    notices) whose backdrop intercepts every pointer event — the submit click then
    retries until the watchdog kills the run, before anything reaches the access log
    (first seen 2026-08-19 on a login-form submit). The modal is informational only:
    confirm-click it when a button exists, otherwise remove it outright."""
    try:
        if page.locator(".swal2-container").count() == 0:
            return
        btn = page.locator(".swal2-confirm")
        if btn.count():
            btn.first.click(timeout=3000)
            page.wait_for_timeout(300)
        if page.locator(".swal2-container").count():
            page.evaluate(
                "document.querySelectorAll('.swal2-container').forEach(e => e.remove());"
                "document.body.classList.remove('swal2-shown', 'swal2-height-auto')")
        print("[login] dismissed a swal2 overlay", file=sys.stderr)
    except Exception as e:
        print(f"[login] overlay dismiss skipped: {e}", file=sys.stderr)


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
    user = secret_get("LIB_USER")
    pw = secret_get("LIB_PASS")
    for attempt in range(1, CAPTCHA_MAX_TRIES + 1):
        if page.locator(PASS_SEL).count() == 0:
            return True
        _dismiss_overlays(page)
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


def _try_paper_fetch(doi: str, out: Path, title: str | None = None) -> bool:
    """Layer 1: API/OA/TDM (Elsevier TDM, Springer OA, Unpaywall) — no proxy, no CF."""
    if not PAPER_FETCH.exists():
        return False
    cmd = [sys.executable, str(PAPER_FETCH)]
    if title:                      # → content verification + volume extraction downstream
        cmd += ["--title", title]
    cmd += [doi, str(out)]
    try:
        r = subprocess.run(cmd, timeout=120, capture_output=True, text=True,
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

    # Primary URL first, then any alt_paths (e.g. Springer's in-press _reference.pdf).
    urls = [url] + [f"https://{_proxy_host(route['host'])}{p.format(doi=doi)}"
                    for p in route.get("alt_paths", [])]
    status = None
    for u in urls:
        try:
            resp = page.request.get(u, timeout=NAV_TIMEOUT_MS)
            body = resp.body()
        except Exception as e:
            _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": phase,
                  "status": _classify_exc(e), "note": repr(e)[:100]})
            return False
        status = _classify(resp, body)
        _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": phase, "status": status,
              "http": resp.status, "bytes": len(body),
              "alt": (u != url) or None,
              "cf_ray": resp.headers.get("cf-ray"), "cf_mitigated": resp.headers.get("cf-mitigated")})
        if status == "pdf":
            out.write_bytes(body)
            print(f"[proxy] OK ({phase}) -> {out} ({len(body)} bytes)"
                  + ("（alt path — 可能是 in-press proof 版）" if u != url else ""),
                  file=sys.stderr)
            return True
        _warn_if_blocked(status)
        print(f"[proxy] {status} ({phase}, http {resp.status}, {len(body)}B)"
              + (f" — trying alt path" if u != urls[-1] else ""), file=sys.stderr)
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
                       host: str | None = None,
                       pdf_from_landing: str | None = None) -> str:
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
    pdf_from_landing="{landing}.pdf" → when the article page carries no
      `citation_pdf_url`, derive the PDF URL from the landing URL instead of giving up.
      ==Not every entitled article advertises the meta tag== (Nature news/comment items
      don't), and `no_pdf_meta` on a subscribed article reads like a dead publisher when
      it is really one missing tag.
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
        # ==Classify the resolver response before looking for the meta tag.== A CF 403 is
        # an HTML page with no `citation_pdf_url` in it, so the old code fell through and
        # logged `no_pdf_meta` — which reads as "this publisher has no PDF link" when it
        # actually means "we never got the article page" (BMJ, 2026-08-21: one run logged
        # cf_block, the next logged no_pdf_meta on http 403 — same wall, two names).
        cur_url = (page.url if nav else r.url) if r is not None or nav else ""
        st = _classify(r, body) if r is not None else "no_response"
        if st == "pdf":            # some resolvers land straight on the PDF
            out.write_bytes(body)
            _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "meta",
                  "status": "pdf", "step": "doi_resolve", "bytes": len(body)})
            print(f"[meta] resolver went straight to the PDF -> {out} ({len(body)} bytes)",
                  file=sys.stderr)
            return "pdf"
        if st in ("cf_challenge", "cf_block", "rate_limited") or (
                r is not None and r.status >= 400):
            _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "meta",
                  "status": st, "step": "doi_resolve", "nav": nav,
                  "http": (r.status if r is not None else None),
                  "bytes": len(body), "url": cur_url[:120]})
            _warn_if_blocked(st)
            print(f"[meta] resolver blocked: {st} "
                  f"(http {r.status if r is not None else '?'}, {len(body)}B, nav={nav})"
                  + ("; nav=True usually passes" if not nav else ""), file=sys.stderr)
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
    landing = (page.url if nav else r.url).split("?")[0].rstrip("/")
    m = re.search(r'citation_pdf_url"\s+content="([^"]+)"', html)
    if m:
        pdf_url = m.group(1)
    elif pdf_from_landing:
        pdf_url = pdf_from_landing.format(landing=landing)
        print(f"[meta] no citation_pdf_url → derived from the landing URL: {pdf_url[:100]}",
              file=sys.stderr)
    else:
        _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "meta",
              "status": "no_pdf_meta", "http": (r.status if r is not None else None),
              "bytes": len(body), "url": landing[:120]})
        print(f"[meta] no citation_pdf_url on {landing[:100]}", file=sys.stderr)
        return "fail"
    host = urlsplit(pdf_url).netloc
    if PROXY_SUFFIX.split(":")[0] not in host:   # meta gave the public host → rewrite
        pdf_url = pdf_url.replace(host, _proxy_host(host), 1)
    try:
        rp = page.request.get(pdf_url, headers={"referer": landing}, timeout=NAV_TIMEOUT_MS)
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


def _crossref_pii(doi: str) -> str | None:
    """DOI → Elsevier PII via CrossRef `alternative-id` (no browser, no auth needed).

    ClinicalKey's PDF endpoint is keyed by PII, not DOI. CrossRef carries the PII of an
    Elsevier DOI in `alternative-id` (verified on in-press APMR articles 2026-08-19), so
    the URL can be built without scraping the CK SPA."""
    import urllib.request
    from urllib.parse import quote
    mail = CFG.get("unpaywall_email") or "unknown"
    req = urllib.request.Request(
        f"https://api.crossref.org/works/{quote(doi, safe='')}",
        headers={"User-Agent": f"paper-fetch (mailto:{mail})"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            msg = json.loads(r.read())["message"]
    except Exception as e:
        print(f"[ck] CrossRef lookup failed ({repr(e)[:80]})", file=sys.stderr)
        return None
    for aid in msg.get("alternative-id") or []:
        cand = re.sub(r"[^A-Za-z0-9X]", "", aid)
        # PII = S + ISSN (8 chars, check digit may be X) + YY + NNNNN + check digit (may be X);
        # e.g. S1530891X26010232 (Endocr Pract, ISSN 1530-891X) — the X can sit at position 9.
        if re.fullmatch(r"[SB][0-9]{7}[0-9X][0-9]{7}[0-9X]", cand):
            return cand
    return None


def _ck_pdf(page, doi: str, out: Path) -> str:
    """ClinicalKey route (HEADFUL): gate login → CK `playBy/doi` → watermarked-PDF service.
    Returns "pdf" / "auth" / "fail".

    Shape of the route (established 2026-08-19, issue #2 postmortem):
    - The CK SPA DOES load through the rewriting proxy — but only in a headful context.
      Headless hangs on the「页面加载中」splash forever; the earlier "CK over the proxy is
      a dead end" verdict was a headless artifact (same class as the LWW 請稍候 case).
    - The PDF endpoint needs CK's own session cookies, which only the SPA bootstrap sets;
      with gate cookies alone it answers HTTP 902 + JSON. So: navigate once, let the SPA
      boot, and poll the PDF endpoint from the same context (500s while booting are
      normal — the second poll typically succeeds).
    - Entering CK shows a "选择机构 / Choose organization" modal (csas status=PATH_CHOICE).
      Its DOM has changed once already (`button.pseudo-label` → radio group
      `input[name=path_choice_select]`, 2026-08-31); the poll loop handles both and picks
      by config `clinicalkey.institution_match`. ==If the route ever logs boot_timeout with
      the PDF endpoint at HTTP 500 and no "institution picked" line, suspect the modal
      selector first== — dump the DOM (see `_scratch/ck_diag4.py`), don't assume CK is down.
      The "remember" box is ticked; whether it persists is profile-dependent.
    - A synthetic click on the SPA's own download link does NOT trigger a download —
      fetch the PDF URL directly instead. PII for the URL comes from _crossref_pii.
    """
    prefix = doi.split("/")[0]
    pii = _crossref_pii(doi)
    if not pii:
        print(f"[ck] no PII for {doi} (CrossRef alternative-id empty) — cannot build the "
              "ClinicalKey PDF URL", file=sys.stderr)
        _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "ck",
              "status": "no_pii"})
        return "fail"
    # Query string exactly as the CK UI's own download link builds it (verified working).
    pdf_url = (f"https://{_proxy_host('www.clinicalkey.com')}"
               f"/service/content/pdf/watermarked/1-s2.0-{pii}.pdf?locale=zh_CN&searchIndex=")
    play = f"{REMOTE_AUTH}{LOGIN_PATH}?url=https://www.clinicalkey.com/content/playBy/doi/?v={doi}"
    _throttle()
    _mark(f"ck: goto playBy {doi}")
    try:
        page.goto(play, wait_until="commit", timeout=NAV_TIMEOUT_MS)
    except Exception as e:
        _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "ck",
              "status": _classify_exc(e), "note": repr(e)[:100]})
        return "fail"
    # Bounced onto the gate's login form? Complete it HERE — submitting on the bounced
    # page is what performs the per-subdomain handshake (re-running login() is a no-op).
    try:
        page.wait_for_timeout(1500)
        if page.locator(PASS_SEL).count() > 0 and not _login_submit_here(page):
            return "auth"
    except Exception:
        pass
    deadline = time.time() + CK_BOOT_TIMEOUT_S
    tries = 0
    while time.time() < deadline:
        page.wait_for_timeout(CK_POLL_S * 1000)
        tries += 1
        # Institution-choice modal. The SPA's /auth/csas/session answers status=PATH_CHOICE
        # with N path_choices and renders a "选择机构 / Choose organization" modal; until one
        # is submitted, every /service/* call (the PDF endpoint included) is a Tomcat 500 and
        # the header shows「访问验证…」forever. Two DOM generations seen:
        #   · ≤2026-08-19: options are `button.pseudo-label`
        #   · master-248835a (2026-08-31): a radio group `input[name=path_choice_select]`
        #     with the label text in `.path-choice-text`, a "记住这个机构" checkbox and a
        #     `button.submit-button` (继续). The old selector matched nothing, so the route
        #     polled 12× into boot_timeout while the modal sat there the whole time —
        #     the user's own browser (remember-me token → status=INITIALIZED) sailed through.
        # The remember box is ticked so later runs skip the choice like that browser does.
        try:
            if page.evaluate("!!document.querySelector("
                             "'button.pseudo-label, input[name=path_choice_select]')"):
                res = json.loads(page.evaluate(
                    """(inst) => {
                        const rx = inst ? new RegExp(inst,'i') : null;
                        const radios=[...document.querySelectorAll('input[name=path_choice_select]')];
                        if (radios.length) {
                            const opts = radios.map(r => {
                                const box = r.closest('.c-els-field') || r.parentElement;
                                const t = ((box && box.querySelector('.path-choice-text, label')) || box || r)
                                          .textContent.trim();
                                return {r, t};
                            });
                            const pick = rx ? opts.find(o=>rx.test(o.t))
                                            : (opts.length===1 ? opts[0] : null);
                            if(!pick) return JSON.stringify(
                                {picked:null, options:opts.map(o=>o.t.slice(0,90))});
                            pick.r.click();
                            if(!pick.r.checked){ pick.r.checked=true;
                                pick.r.dispatchEvent(new Event('change',{bubbles:true})); }
                            const form = pick.r.closest('form') || document;
                            const remember = form.querySelector('input[type=checkbox]');
                            if (remember && !remember.checked) remember.click();
                            const cont = form.querySelector('button.submit-button, button[type=submit]')
                                || [...form.querySelectorAll('button')]
                                     .find(b=>/继续|繼續|continue/i.test(b.textContent));
                            if(cont) cont.click();
                            return JSON.stringify({picked:pick.t.slice(0,90), cont:!!cont,
                                                   ui:'radio', remember:!!remember});
                        }
                        const btns=[...document.querySelectorAll('button.pseudo-label')];
                        const pick=rx?btns.find(b=>rx.test(b.textContent))
                                     :(btns.length===1?btns[0]:null);
                        if(!pick) return JSON.stringify(
                            {picked:null,options:btns.map(b=>b.textContent.trim().slice(0,90))});
                        pick.click();
                        const form=pick.closest('form')||document;
                        const cont=[...form.querySelectorAll('button')]
                            .find(b=>!b.classList.contains('pseudo-label')
                                     &&/继续|繼續|continue/i.test(b.textContent))
                            ||form.querySelector('button[type=submit]');
                        if(cont)cont.click();
                        return JSON.stringify({picked:pick.textContent.trim().slice(0,90),
                                               cont:!!cont, ui:'pseudo-label'});
                    }""", CK_INSTITUTION))
                if res.get("picked"):
                    print(f"[ck] institution picked ({res.get('ui')}): {res['picked']}",
                          file=sys.stderr)
                elif res.get("options"):
                    print("[ck] institution modal has several options and "
                          "`clinicalkey.institution_match` (config.yaml) matched none:",
                          file=sys.stderr)
                    for o in res["options"]:
                        print(f"[ck]   · {o}", file=sys.stderr)
                    _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "ck",
                          "status": "institution_unmatched"})
                    return "fail"
        except Exception:
            pass
        try:
            resp = page.request.get(pdf_url, timeout=45000)
            body = resp.body()
        except Exception as e:
            _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "ck",
                  "status": _classify_exc(e), "note": repr(e)[:100]})
            continue
        status = _classify(resp, body)
        if status == "pdf":
            try:
                from paper_fetch import pdf_gate
                ok, reason = pdf_gate(body)
            except ImportError:
                ok, reason = True, None
            if not ok:
                partial = out.with_suffix(out.suffix + ".partial")
                partial.write_bytes(body)
                print(f"[ck] 內容驗證未過：{reason} → 退件存 {partial}", file=sys.stderr)
                _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "ck",
                      "status": "gate_reject", "note": str(reason)[:100]})
                return "fail"
            out.write_bytes(body)
            _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "ck",
                  "status": "pdf", "http": resp.status, "bytes": len(body), "tries": tries})
            print(f"[ck] OK -> {out} ({len(body)} bytes, probe {tries})", file=sys.stderr)
            return "pdf"
        if status == "auth_expired":
            return "auth"
        _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "ck",
              "status": status, "http": resp.status, "bytes": len(body), "tries": tries})
        # HTTP 500 / 902-JSON while the SPA bootstraps — keep polling until the deadline.
    print(f"[ck] CK session not ready within {CK_BOOT_TIMEOUT_S}s — the SPA only boots "
          "HEADFUL; if this run was headless, that is the bug", file=sys.stderr)
    _log({"kind": "proxy", "doi": doi, "prefix": prefix, "phase": "ck",
          "status": "boot_timeout"})
    return "fail"


def _drop_stale_partial(out: Path) -> None:
    """Layer 1 leaves its gate-rejected bytes as `<out>.partial` (cover sheet / TDM preview).
    Once the proxy route has produced the real article that file is only a trap for the next
    reader — a regenerable download, so a direct unlink is fine."""
    partial = out.with_name(out.name + ".partial")
    if partial.exists():
        try:
            partial.unlink()
            print(f"[fetch] removed layer-1 reject {partial.name} (superseded)", file=sys.stderr)
        except OSError:
            pass


def _sfx_hint(doi: str) -> str:
    return f" SFX: {SFX.format(doi=doi)}" if SFX else ""


def _content_check(out: Path, title: str | None) -> None:
    """Is the file we just wrote actually THIS article? (`pdf_verify`, only with a title.)

    ==A byte-level check cannot tell "the article" from "the issue the article is in".==
    Conference abstracts routinely carry the supplement's DOI, so every route — TDM API,
    OA, and this proxy — honestly hands back the whole proceedings volume: a 563-page,
    48 MB file that passes `%PDF` + size and gets logged `ok` (2026-08; 20% of one 49-paper
    batch). A whole-volume PDF fed to a full-text screen produces a confident answer about
    the wrong study, which is worse than a missing file. Volumes are cut down to the
    article here, with the volume kept as `<stem>_volume.pdf`."""
    if not title:
        return
    try:
        import pdf_verify      # same directory, like paper_config
    except ImportError:
        print("[verify] pdf_verify unavailable → skipped", file=sys.stderr)
        return
    res = pdf_verify.verify(out, title)
    if res["verdict"] == "match":
        print(f"[verify] {res['detail']}", file=sys.stderr)
        return
    print(f"[verify] ⚠ {res['verdict']} — {res['detail']}", file=sys.stderr)
    if res["verdict"] == "volume_like":
        cut = pdf_verify.extract(out, title)
        if cut["extracted"]:
            print(f"[verify] ✂ cut p{cut['first_page']}-{cut['last_page']} of {cut['pages']} "
                  f"({cut['ratio']:.0%} contiguous, runner-up {cut['runner']:.0%}); "
                  f"volume kept as {cut['volume_path']}", file=sys.stderr)
        else:
            print(f"[verify] could not locate the article inside the volume "
                  f"({cut['reason']}) — file left as is, check by hand", file=sys.stderr)


def run_fetch(pw, doi: str, out: Path, title: str | None = None,
              skip_layer1: bool = False) -> str:
    """Three-layer ladder: ① paper_fetch's API/OA/TDM (no proxy) → ② holdings entitlement
    pre-check → ③ dispatch the proxy route by ROUTES[prefix]["kind"] (tpl / meta / lww).

    `skip_layer1` is the human override for ①: the content gate is heuristic, and when it
    lets a non-article through (a 1-page TDM preview did exactly that on 2026-08-31) the
    only way to reach the proxy route was to edit the code. Now it's a flag.

    ==Returns a REASON, not a bool==: "ok" | "auth" (session/login problem) | "no_route"
    (no route for this publisher) | "fail" (a route ran and came back empty). main() maps
    these onto distinct exit codes, because a caller that cannot tell "the session died"
    from "there is no route" writes down N false "no full text" verdicts for what is one
    expired login — exactly what happened to a 9-paper batch on 2026-08-21.

    Headless is the default (patchright clears most CF). Two cases MUST be headful:
      · lww  — the proxy's "please wait" JS interstitial hangs headless
      · ck   — the ClinicalKey SPA only bootstraps headful (headless hangs on its splash)
      · meta with nav=True (BMJ-class) — their CF only passes a real navigation
    `is_logged_in` (the gate page) can report VALID while the per-subdomain proxy
    authorization has separately expired → the proxy returns an auth page. So any proxy
    "auth" failure forces one fresh login + retry."""
    prefix = doi.split("/")[0]
    route = ROUTES.get(prefix, {})
    kind = route.get("kind")
    needs_headful = kind in ("lww", "ck") or (kind == "meta" and route.get("nav"))
    ctx = _new_context(pw, headless=not needs_headful)
    _mark("restore_session")
    restore_session(ctx)
    page = ctx.new_page()
    _mark("new_page OK")
    try:
        # Layer 1 — API/OA/TDM (no proxy, no CF, no login). Works out of the box.
        if skip_layer1:
            print("[fetch] --skip-layer1 → OA/TDM layer skipped, going to the proxy route",
                  file=sys.stderr)
            _log({"kind": "api", "doi": doi, "status": "skipped"})
        elif _try_paper_fetch(doi, out, title):
            print(f"[fetch] got via API/OA/TDM route -> {out}", file=sys.stderr)
            return "ok"
        if not ensure_login(page):
            print("[fetch] LOGIN FAILED — this is an authentication problem, NOT evidence "
                  "that the paper has no route. Run `library_session.py login` (it opens a "
                  "real window; the proxy's JS interstitial can refuse to complete in the "
                  "headless context a tpl/meta fetch runs in), then retry.", file=sys.stderr)
            return "auth"
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
            return "fail"
        # Layer 3 — proxy route by kind. Each is "run once → re-login+retry ONLY on auth
        # expiry": a re-login fixes nothing else, and re-running LWW costs another Ovid
        # concurrent-licence seat (self-inflicted E3).
        if kind == "lww":
            attempt = lambda: _lww_ovid_pdf(page, doi, out)
        elif kind == "ck":
            attempt = lambda: _ck_pdf(page, doi, out)
        elif kind == "meta":
            attempt = lambda: _citation_meta_pdf(
                page, doi, out, nav=bool(route.get("nav")), host=route.get("host"),
                pdf_from_landing=route.get("pdf_from_landing"))
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
            return "no_route"

        st = attempt()
        if st == "pdf":
            _content_check(out, title)
            _drop_stale_partial(out)
            return "ok"
        if st == "auth":
            print("[fetch] auth expired → fresh login (refreshes proxy authorization), "
                  "then one retry", file=sys.stderr)
            if login(page):
                if attempt() == "pdf":
                    _content_check(out, title)
                    _drop_stale_partial(out)
                    return "ok"
            else:
                print("[fetch] re-login failed → authentication problem, not a missing "
                      "route", file=sys.stderr)
                return "auth"
        print(f"[fetch] {kind} route could not fetch {doi}.{_sfx_hint(doi)}", file=sys.stderr)
        return "fail"
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

    print("\n=== Holdings gaps (no proxy route) ===")
    gaps = sorted({r["prefix"] for r in recs
                   if r["prefix"] not in ROUTES and r.get("subscribed")})
    for p in gaps:
        print(f"  ⚠  {p} — subscribed articles hit it, but ROUTES has no entry → add one")
    if not gaps:
        print("  ✅ no 'subscribed but routeless' prefix in the access log")
    # ==Also list routeless prefixes whose entitlement is UNKNOWN.== `subscribed=None`
    # just means the journal isn't in the A-Z table — database-level platforms look
    # exactly like this and fetch fine. Filtering the gap list on `subscribed` truthiness
    # hid 10.1210 (Endocrine Society) for two runs while those papers were fetched by hand.
    unknown = sorted({r["prefix"] for r in recs
                      if r["prefix"] not in ROUTES and not r.get("subscribed")})
    for p in unknown:
        hits = sum(1 for r in recs if r["prefix"] == p and r.get("status") == "no_route")
        j = next((r.get("journal") for r in recs
                  if r["prefix"] == p and r.get("journal")), "")
        print(f"  ·  {p} — no route, entitlement unknown (×{hits}) {j[:50]}"
              "  → not in the A-Z table ≠ no access; worth one manual probe")
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
    # `--title "<article title>"` (fetch only) turns on content verification: is the PDF
    # we got actually this article, or the whole supplement it sits in? See _content_check.
    title = None
    skip_layer1 = os.environ.get("PAPERFETCH_SKIP_LAYER1") == "1"
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--title":
            i += 1
            title = argv[i] if i < len(argv) else None
        elif a.startswith("--title="):
            title = a.split("=", 1)[1]
        elif a == "--skip-layer1":
            skip_layer1 = True
        else:
            rest.append(a)
        i += 1
    argv = rest
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
        print('usage: fetch <DOI> <out.pdf> [--title "<article title>"] [--skip-layer1]')
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
                    # 0 ok · 2 route ran but came back empty · 3 auth · 6 no route
                    return {"ok": 0, "fail": 2, "auth": 3,
                            "no_route": 6}[run_fetch(pw, argv[1], Path(argv[2]), title,
                                                     skip_layer1=skip_layer1)]

                # `login` needs a real window: the proxy's JS-redirect interstitial
                # never completes headless. `check` only hits the gate's login page → headless.
                ctx = _new_context(pw, headless=(cmd == "check"))
                restore_session(ctx)
                page = ctx.new_page()
                try:
                    if cmd == "check":
                        ok = is_logged_in(page)
                        print("session: VALID" if ok else "session: EXPIRED (run: login)")
                        return 0 if ok else 3
                    ok = ensure_login(page)     # cmd == "login"
                    print("login: OK" if ok else "login: FAILED")
                    return 0 if ok else 3
                finally:
                    ctx.close()
        finally:
            wd.cancel()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
