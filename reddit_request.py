#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r/Scholar requests — draft, dedupe, track, poll, verify. Posting stays with a human.

When every route of the ladder has come back empty (paper_fetch exit 2, library_session exit
2 or 7 after the direct/nav retries), the remaining finishes are interlibrary loan, the
author, or asking on r/Scholar. This script does the bookkeeping for the last one: it writes
the request in the subreddit's own format, checks nobody already asked, then watches the
thread so you hear about a reply instead of re-opening Reddit every few hours. (Since Reddit
switched RSS off — announced for 2026-11-13 — checking needs `reddit.dedupe_cmd` and watching
needs `reddit.poll_cmd`; see the section on that below.)

It never posts. Writing to someone else's community is a human act; the draft is printed
for you to paste. (An integration that posts *as you, from your own logged-in browser, only
when you say so* can sit on top of `track` — this repo does not ship one.)

    python reddit_request.py draft <DOI|PMID> [--dest P] [--json]   # OA gate → title/body → state
    python reddit_request.py dedupe <DOI>                  # already requested on r/Scholar?
    python reddit_request.py track <post_url> [--doi D] [--record-id R] [--title T] [--dest P]
    python reddit_request.py poll [--json]                 # one pass over tracked threads
    python reddit_request.py verify <post> <pdf> [--registry NCT…]   # or --manual
    python reddit_request.py close <post>                  # done by hand / give up
    python reddit_request.py status [--json]
    python reddit_request.py notify-test [--only SINK]     # test event → OK/FAIL per sink

Exit codes: 0 ok · 1 usage · 2 nothing found (no metadata / PDF not verified) ·
7 rate-limited by Reddit (backoff recorded in the state file — wait, do not loop) ·
8 refused: the article is open access — fetch it, do not ask strangers for it ·
9 notify-test: a sink failed, or no sink is usable ·
10 Reddit could not be read: `dedupe` could not check (stdout says `"existing": null` and
   prints the search page to check by hand), or `poll` could not read a thread.

## Reddit switched RSS off on 2026-11-13 — dedupe and poll need a browser

Measured 2026-11-21: every `*.rss` URL (`/r/Scholar/search.rss`, `/r/Scholar/new.rss`,
`/comments/<id>/.rss`, other subreddits too) answers HTTP 404 with an Atom feed titled
"Not Found" — for this script's User-Agent, python-requests' and curl's defaults, and from
inside a real browser, logged out and logged in alike. Scripts get 403 from `search.json` and
from `/comments/<id>.json`; old.reddit redirects to a login page. Reddit had announced the end
of RSS for 13 November 2026. What still answers is a logged-in browser: there,
`fetch("/comments/<id>.json")` from a reddit.com page returns the whole comment tree. So:

- `dedupe` cannot use the search feed. Point `reddit.dedupe_cmd` at something that can search
  Reddit (a real browser); without it, `dedupe` says "unavailable" — it never reports
  "nobody asked" when it could not look. A caller must not post on an unavailable dedupe
  unless a person checked the printed search page.
- `poll` reads threads through `reddit.poll_cmd`: argv, `{id}` substituted, that prints the
  JSON of `https://www.reddit.com/comments/{id}.json` (two Listings: the post, its comments)
  — e.g. from a logged-in browser. This script parses it (`parse_listing`) the way it used to
  parse the feed. Without the hook, or when it fails or prints anything else (an HTML page, a
  different thread), `poll` is blind for that thread: exit 10, the thread URL to open by hand,
  one `poll_blind` event, and `stale_days` does not close a thread it could not read.

## Why the OA gate refuses instead of warning (2026-10-24)

A Karger gold-OA article (CC BY-NC-ND printed on the PDF) was logged "no full-text route"
because the publisher blocks scripts, and was then requested on r/Scholar. A bot wall is not
the absence of an open copy. So `draft` asks Unpaywall first and, if `is_oa`, prints the OA
location and stops: open it in a real browser (`library_session.py fetch --direct`).

## Reddit's unauthenticated RSS budget is about one request a minute

Measured 2026-10-24: a single `/comments/<id>/.rss` GET answers
`x-ratelimit-remaining: 0.0, x-ratelimit-reset: 56` — two requests in a row are a 429.
Every Reddit request here goes through one gate that honours those headers (it sleeps
`reset` + jitter before the next request, and persists the earliest-next time in the state
file so a second process waits too). A 429 records a backoff and ends the run with exit 7;
there is no retry loop. Polling five threads therefore takes about five minutes — run `poll`
from a scheduler every 30 minutes, not in a tight loop.

## What polling can and cannot see

Each tracked thread's comments are read; a comment by anyone other than the poster and
AutoModerator moves the request to `replied`, a comment carrying an off-Reddit link to
`fulfilled` (a *candidate*: links are untrusted file hosts and are never downloaded here).
Reddit chat is not in any RSS feed — a fulfiller who answers by chat is invisible unless you
add your private inbox feed (reddit.com/prefs/feeds → "your inbox", store the full URL in
the secret store and name it in `reddit.inbox_feed_secret`); even then chat stays dark.

State machine: drafted → posted → replied → fulfilled → verified → closed (`close` any time;
automatic after `stale_days` with no reply). `on_idle` / `on_active` hooks let the scheduler
sleep while nothing is open.
The poster's own "thanks solution verified" comment moves a request to `verified` too.
`status` prints the tracking table (posted / replies / link offered / status / thanked);
`<state_dir>/status.md` holds the same table and is rewritten on every state change.
Notifications carry title, DOI, record id, status, comment count, the replying comment's
Reddit permalink, the host the offered link is on, the destination path and what to do —
never comment text or the offered link itself. A sink that missed a reply (added later, or
failing at the time) gets it on the next `poll` or `notify-test`, marked `catch_up`.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
import os
import pathlib
import random
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import requests

from paper_config import CFG

VERSION = "1.13.0"
_R = {
    "subreddit": "Scholar",
    "state_dir": "",
    "body_note": "Needed for a systematic review. Thank you!",
    "ignore_authors": ["AutoModerator"],
    "inbox_feed_secret": "",
    "max_wait_s": 180,
    "stale_days": 21,       # no reply at all after this long → closed (stops polling it)
    "on_active": [],        # argv run when a request starts being tracked (e.g. enable the scheduler)
    "on_idle": [],          # argv run when poll finds nothing left to watch (e.g. disable it)
    "notify": [],
    "dedupe_cmd": [],       # argv, "{doi}" substituted; prints {"existing": [...]} (see dedupe())
    "poll_cmd": [],         # argv, "{id}" substituted; prints /comments/{id}.json (see _poll_cmd())
    **(CFG.get("reddit") or {}),
}
STATE_DIR = pathlib.Path(os.path.expanduser(os.environ.get("PAPERFETCH_REDDIT_STATE")
                                             or _R["state_dir"] or "~/.paper-fetch/reddit"))
STATE_FILE = STATE_DIR / "state.json"
EVENTS_FILE = STATE_DIR / "events.jsonl"
_CONTACT = CFG["rate"]["contact"]
REDDIT_UA = (f"paper-fetch/{VERSION} (r/{_R['subreddit']} request tracker; "
             "+https://github.com/drpwchen/paper-fetch)" + (f" contact {_CONTACT}" if _CONTACT else ""))
API_UA = f"paper-fetch/{VERSION}" + (f" (mailto:{_CONTACT})" if _CONTACT else "")
ATOM = "{http://www.w3.org/2005/Atom}"
OPEN_STATES = ("posted", "replied", "fulfilled")
ORDER = ("drafted", "posted", "replied", "fulfilled", "verified", "closed")


class RateLimited(Exception):
    pass


class DedupeUnavailable(Exception):
    """dedupe could not look — which is NOT "nobody asked". `manual_url` is the search page
    a person can open to check by hand."""

    def __init__(self, why: str, manual_url: str):
        super().__init__(why)
        self.manual_url = manual_url


class PollUnavailable(Exception):
    """poll_cmd could not read a thread — which is NOT "no new comments"."""


# ── state ────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"schema": 1, "drafts": {}, "posts": {}, "reddit_next_ok": 0, "backoff_until": 0}


def save_state(st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)
    write_status_md(st)                     # the human-readable tracking table, always current


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _advance(rec: dict, status: str) -> bool:
    """Move forward only — a late `replied` must not undo `fulfilled`."""
    if ORDER.index(status) > ORDER.index(rec["status"]):
        rec["status"] = status
        rec["updated"] = _now()
        return True
    return False


# ── notifications ────────────────────────────────────────────────────────────
# Every event goes to events.jsonl. Each sink in `reddit.notify` is one way to reach a person:
# ntfy · discord · telegram · email · webhook (raw event JSON) · command (event JSON on stdin).
# A sink never stops the run; its last success/failure is kept in notify_status.json so a dead
# sink shows up at the top of status.md instead of only in a log nobody reads.
NOTIFY_EVENTS = ("replied", "fulfilled", "verified", "stale", "inbox")   # + "test", always sent
NOTIFY_EXIT = 9                     # notify-test: at least one sink failed, or none is usable
_HEAD = {"test": "test event", "replied": "new reply", "fulfilled": "reply with a link",
         "verified": "PDF verified", "inbox": "inbox message", "stale": "closed — no reply"}
# type → (required fields, {field_secret: what it holds}); "a|b" = one of the two
_SINKS = {
    "ntfy": (["topic"], {"token_secret": "access token (optional)"}),
    "discord": (["url|url_secret"], {"url_secret": "webhook URL"}),
    "telegram": (["token_secret", "chat_id"], {"token_secret": "bot token"}),
    "email": (["host", "from", "to"], {"password_secret": "SMTP password"}),
    "webhook": (["url|url_secret"], {"url_secret": "webhook URL"}),
    "command": (["argv"], {}),
}


class NotifyError(Exception):
    """A delivery failure whose message is safe to print: no URL, no token, no password."""


def _secret(name: str) -> str | None:
    if not name:
        return None
    import library_session            # one secret store for the whole tool (dpapi/keychain/env)
    return library_session.secret_get_opt(name)


def _store_hint(name: str) -> str:
    import library_session
    return library_session._store_hint(name)


def _label(i: int, sink: dict) -> str:
    return (sink or {}).get("name") or f"{i + 1}:{(sink or {}).get('type', '?')}"


def sink_problems(sink: dict) -> list[str]:
    """Why this sink cannot deliver, found before an event needs it (empty = looks usable)."""
    sink = sink or {}
    kind = sink.get("type")
    if kind not in _SINKS:
        return [f"unknown type {kind!r} (expected one of: {', '.join(_SINKS)})"]
    need, secrets = _SINKS[kind]
    out = []
    for f in need:
        if not any(sink.get(alt) for alt in f.split("|")):
            out.append(f"missing `{f.replace('|', '` or `')}`")
    for f, what in secrets.items():
        name = sink.get(f)
        if name and not _secret(name):
            out.append(f"secret {name!r} ({what}) is not stored — {_store_hint(name)}")
    if kind == "email" and sink.get("security", "starttls") not in ("starttls", "ssl", "none"):
        out.append(f"`security` must be starttls, ssl or none (got {sink['security']!r})")
    bad = [e for e in sink.get("events") or [] if e not in NOTIFY_EVENTS]
    if bad:
        out.append(f"unknown event(s) {bad} in `events` (expected: {', '.join(NOTIFY_EVENTS)})")
    return out


def has_sink() -> bool:
    """True when at least one sink can actually reach a person (`jsonl` is the events file itself)."""
    return any((s or {}).get("type") not in (None, "jsonl") and not sink_problems(s)
               for s in _R["notify"] or [])


NO_SINK = ("NO WORKING NOTIFY SINK — replies are only written to events.jsonl and nobody "
           "will be told. Add reddit.notify (ntfy / discord / telegram / email / webhook / "
           "command) to config.yaml, then run `python reddit_request.py notify-test`.")


def warn_no_sink() -> None:
    """Shout on stderr: a tracked request with no sink is a request whose answer goes unseen."""
    for i, s in enumerate(_R["notify"] or []):
        if (s or {}).get("type") != "jsonl":
            for p in sink_problems(s):
                print(f"[notify] {_label(i, s)}: {p}", file=sys.stderr)
    if not has_sink():
        print(f"\n!!! [reddit] {NO_SINK}\n", file=sys.stderr)


def _render(ev: dict) -> tuple[str, str]:
    """(title, body) shared by every human-facing sink. Never carries comment text."""
    title = f"r/{_R['subreddit']}: {_HEAD.get(ev['event'], ev['event'])}"
    if ev.get("catch_up"):
        title += " (delivered late)"
    hosts = ", ".join(ev.get("link_hosts") or [])
    body = "\n".join(x for x in (
        ev.get("title"), ev.get("doi") and f"DOI {ev['doi']}",
        ev.get("record_id") and f"record {ev['record_id']}",
        ev.get("messages") and f"{ev['messages']} new private message(s)",
        ev.get("status") and f"status {ev.get('status')} · {ev.get('comments', 0)} comment(s)",
        hosts and f"link offered on: {hosts} (untrusted — open it yourself)",
        *(f"reply: {u}" for u in ev.get("reply_urls") or []),
        ev.get("dest") and f"save to: {ev['dest']}",
        ev.get("action") and f"to do: {ev['action']}",
        ev.get("url")) if x)
    return title, body


# What a person has to do about each event — notifications must say it, not just "something happened".
_ACTION = {
    "fulfilled": ("Open the offered link yourself in a browser (never fetched automatically). "
                  "If it is a Reddit chat/DM, log in as the poster. Save the PDF{dest}, then run "
                  "`verify {id} <file>`."),
    "replied": ("Read the reply on Reddit — no link yet; it may point you to Reddit chat or your "
                "DMs (log in as the poster)."),
    "stale": "Closed with no reply. Interlibrary loan or writing to the author are the next steps.",
    "inbox": "Log in to Reddit and read your inbox (private messages are not in any feed).",
    "poll_blind": ("This thread could not be read (Reddit switched RSS off; reddit.poll_cmd "
                   "unset or failing), so replies are not watched. Open the thread yourself to "
                   "check; `close {id}` once you are done."),
}


def _host(u: str) -> str:
    h = re.sub(r"^[a-z]+://", "", u, flags=re.I).split("/", 1)[0].split("@")[-1].split(":")[0].lower()
    return h.removeprefix("www.")


def event_detail(event: str, rec: dict | None) -> dict:
    """The actionable part of an event: who answered where, what the link is on, where the PDF
    goes, and what the person must do. Reddit permalinks and host names only — never the offered
    link itself or comment text (both are untrusted)."""
    rec = rec or {}
    out = {}
    links = rec.get("links") or []
    if links:
        out["reply_urls"] = [x["comment"] for x in links if x.get("comment")][-3:]
        out["link_hosts"] = sorted({h for x in links for h in x.get("hosts") or []})
    if rec.get("dest"):
        out["dest"] = rec["dest"]
    if event in _ACTION:
        out["action"] = _ACTION[event].format(
            id=rec.get("id") or "<id>", dest=f" to {rec['dest']}" if rec.get("dest") else "")
    return out


def _redact(msg: str, *secrets: str | None) -> str:
    for s in secrets:
        if s:
            msg = msg.replace(s, "<secret>")
    return re.sub(r"https?://\S+", "<url>", msg)     # request errors quote the full URL


def _post(url: str, *, secrets: tuple = (), **kw) -> requests.Response:
    """requests.post whose exceptions never leak the URL (a Discord URL or bot token is a key)."""
    try:
        return requests.post(url, timeout=15, **kw)
    except requests.RequestException as e:
        raise NotifyError(f"{type(e).__name__}: {_redact(str(e), url, *secrets)[:160]}") from None


def _http_fail(r: requests.Response, hints: dict) -> None:
    if r.ok:
        return
    try:
        detail = r.json()
        detail = detail.get("description") or detail.get("message") or detail.get("error") or ""
    except ValueError:
        detail = ""
    raise NotifyError(f"HTTP {r.status_code}" + (f" {str(detail)[:120]}" if detail else "")
                      + (f" — {hints[r.status_code]}" if r.status_code in hints else ""))


def _send_ntfy(sink: dict, ev: dict) -> None:
    title, msg = _render(ev)
    body = {"topic": sink["topic"], "title": title, "message": msg, "tags": ["books"]}
    if ev.get("url"):
        body["click"] = ev["url"]
    # JSON publish to the server root carries UTF-8 titles safely (header Title: cannot);
    # a browser-like UA because Cloudflare-fronted ntfy servers 403 bare python UAs.
    headers = {"Content-Type": "application/json", "User-Agent": f"Mozilla/5.0 ({API_UA})"}
    token = _secret(sink.get("token_secret", ""))
    if token:
        headers["Authorization"] = f"Bearer {token}"
    server = (sink.get("server") or "https://ntfy.sh").rstrip("/")
    r = _post(server, secrets=(token,), data=json.dumps(body).encode("utf-8"), headers=headers)
    _http_fail(r, {401: "token wrong or missing for a protected topic",
                   403: "topic reserved by someone else, or the token lacks write access",
                   429: "ntfy rate limit — wait"})


def _send_discord(sink: dict, ev: dict) -> None:
    url = _secret(sink.get("url_secret", "")) or sink.get("url")
    title, msg = _render(ev)
    payload = {"content": f"**{title}**\n{msg}"[:2000], "allowed_mentions": {"parse": []}}
    if sink.get("username"):
        payload["username"] = sink["username"]
    for attempt in (1, 2):
        r = _post(url, json=payload, headers={"User-Agent": API_UA})
        if r.status_code == 429 and attempt == 1:
            try:
                wait = float(r.json().get("retry_after", 99))
            except ValueError:
                wait = 99
            if wait <= 15:
                time.sleep(wait)
                continue
        break
    _http_fail(r, {401: "webhook token invalid — copy the webhook URL again",
                   404: "webhook deleted or URL wrong — copy it again from the channel settings",
                   429: "Discord rate limit — wait"})


def _send_telegram(sink: dict, ev: dict) -> None:
    token = _secret(sink["token_secret"])
    title, msg = _render(ev)
    r = _post(f"https://api.telegram.org/bot{token}/sendMessage", secrets=(token,),
              json={"chat_id": sink["chat_id"], "text": f"{title}\n{msg}",
                    "disable_web_page_preview": True})
    _http_fail(r, {401: "bot token wrong (copy it again from @BotFather)",
                   400: "chat_id wrong, or you have not sent your bot a message yet",
                   403: "the bot was blocked or removed from that chat"})


def _send_email(sink: dict, ev: dict) -> None:
    import smtplib
    from email.message import EmailMessage
    title, msg = _render(ev)
    to = sink["to"] if isinstance(sink["to"], list) else [sink["to"]]
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = title, sink["from"], ", ".join(to)
    m.set_content(msg + "\n\n— paper-fetch reddit_request.py")
    security = sink.get("security", "starttls")
    port = int(sink.get("port") or (465 if security == "ssl" else 587))
    pw = _secret(sink.get("password_secret", ""))
    try:
        cls = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
        with cls(sink["host"], port, timeout=15) as s:
            if security == "starttls":
                s.starttls()
            if pw:
                s.login(sink.get("user") or sink["from"], pw)
            s.send_message(m)
    except smtplib.SMTPAuthenticationError as e:
        raise NotifyError(f"login refused ({e.smtp_code}) — Gmail/Outlook need an app password, "
                          "not your normal one") from None
    except (smtplib.SMTPException, OSError) as e:
        raise NotifyError(f"{type(e).__name__}: {_redact(str(e), pw)[:120]} — check host/port "
                          "(587 + starttls, or 465 + ssl); some networks block outgoing SMTP") from None


def _send_webhook(sink: dict, ev: dict) -> None:
    url = _secret(sink.get("url_secret", "")) or sink.get("url")
    r = _post(url, json=ev, headers={"User-Agent": API_UA})
    _http_fail(r, {400: "the receiver wants its own format — use type discord/telegram, "
                        "or put a converter behind type command"})


def _send_command(sink: dict, ev: dict) -> None:
    try:
        subprocess.run(sink["argv"], input=json.dumps(ev, ensure_ascii=False),
                       text=True, encoding="utf-8", timeout=30, check=True,
                       capture_output=True)
    except FileNotFoundError:
        raise NotifyError(f"{sink['argv'][0]!r} not found — use absolute paths: a scheduler "
                          "runs with a different PATH and working directory") from None
    except subprocess.CalledProcessError as e:
        raise NotifyError(f"exit {e.returncode}: {(e.stderr or '').strip()[-160:]}") from None
    except subprocess.TimeoutExpired:
        raise NotifyError("timed out after 30 s") from None


_SEND = {"ntfy": _send_ntfy, "discord": _send_discord, "telegram": _send_telegram,
         "email": _send_email, "webhook": _send_webhook, "command": _send_command}


def _load_notify_status() -> dict:
    try:
        return json.loads((STATE_DIR / "notify_status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _record(label: str, kind: str, error: str | None) -> None:
    st = _load_notify_status()
    row = st.setdefault(label, {})
    row["type"] = kind
    if error:
        row.update(failed=_now(), error=error)
    else:
        row["ok"] = _now()
        row.pop("error", None)
        row.pop("failed", None)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "notify_status.json").write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def deliver(i: int, sink: dict, ev: dict) -> str | None:
    """Send one event through one sink. Returns None on success, else a printable reason."""
    label, kind = _label(i, sink), (sink or {}).get("type")
    problems = sink_problems(sink)
    try:
        if problems:
            raise NotifyError("; ".join(problems))
        _SEND[kind](sink, ev)
        err = None
    except NotifyError as e:
        err = str(e)
    except Exception as e:                  # a dead sink must not stop the poll
        err = f"{type(e).__name__}: {_redact(str(e))[:120]}"
    _record(label, kind, err)
    if err:
        print(f"[notify] {label} failed: {err}", file=sys.stderr)
        with EVENTS_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": _now(), "event": "notify_failed", "sink": label,
                                 "for": ev.get("event"), "error": err}, ensure_ascii=False) + "\n")
    return err


def sink_key(sink: dict) -> str:
    """A sink's identity in the delivery ledger: its `name`, else a hash of what it points at.
    Stable when sinks are reordered; a changed target counts as a new sink (it has seen nothing)."""
    sink = sink or {}
    if sink.get("name"):
        return sink["name"]
    import hashlib
    ident = {k: v for k, v in sink.items() if k not in ("events", "name")}
    return f"{sink.get('type', '?')}:" + hashlib.sha1(
        json.dumps(ident, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:10]


# Events that ask a person to act on an open request. Each post keeps, per sink, which of them
# that sink has actually received (`notified`). A reply that arrives while a sink is missing,
# misconfigured or failing is not lost: the next `poll` (or `notify-test`) delivers it late.
CATCH_UP = ("replied", "fulfilled")


def _wants(sink: dict, event: str) -> bool:
    wanted = (sink or {}).get("events")
    return (sink or {}).get("type") != "jsonl" and (not wanted or event == "test" or event in wanted)


def _event(event: str, rec: dict, **extra) -> dict:
    return {"ts": _now(), "event": event, "post_id": rec.get("id"), "url": rec.get("url"),
            "title": rec.get("title"), "doi": rec.get("doi"), "record_id": rec.get("record_id"),
            "status": rec.get("status"), "comments": rec.get("comments", 0),
            **event_detail(event, rec), **extra}


def _fan_out(ev: dict, rec: dict | None, sinks) -> None:
    for i, sink in sinks:
        if deliver(i, sink, ev) is None and rec is not None and rec.get("id") and ev["event"] in CATCH_UP:
            got = rec.setdefault("notified", {}).setdefault(sink_key(sink), [])
            if ev["event"] not in got:
                got.append(ev["event"])


def emit(event: str, rec: dict | None = None, *, notify: bool = True, **extra) -> None:
    """Append to events.jsonl (always), then fan out to the configured sinks.
    The caller saves state afterwards: successful deliveries are recorded on `rec`."""
    ev = _event(event, rec or {}, **extra)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with EVENTS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    if not notify:
        return
    _fan_out(ev, rec, [(i, s) for i, s in enumerate(_R["notify"] or []) if _wants(s, event)])


def catch_up(st: dict, only: set | None = None) -> int:
    """Deliver what each sink missed: for every open request whose status asks for action
    (replied / fulfilled), each usable sink that has not yet received that event gets it now,
    marked `catch_up: true`. Covers a sink added after the reply, and a delivery that failed.
    `only` limits it to these sink keys. Returns how many deliveries were attempted."""
    n = 0
    for rec in st["posts"].values():
        event = rec.get("status")
        if event not in CATCH_UP:
            continue
        missing = [(i, s) for i, s in enumerate(_R["notify"] or [])
                   if _wants(s, event) and not sink_problems(s)
                   and (only is None or sink_key(s) in only)
                   and event not in (rec.get("notified") or {}).get(sink_key(s), [])]
        if not missing:
            continue
        ev = _event(event, rec, catch_up=True)
        with EVENTS_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({**ev, "sinks": [_label(i, s) for i, s in missing]},
                                ensure_ascii=False) + "\n")
        print(f"[notify] {rec['id']}: delivering missed `{event}` to "
              + ", ".join(_label(i, s) for i, s in missing), file=sys.stderr)
        _fan_out(ev, rec, missing)
        n += len(missing)
    return n


def failing_sinks() -> list[tuple[str, dict]]:
    """Sinks whose most recent delivery failed (a later success clears it)."""
    live = {_label(i, s) for i, s in enumerate(_R["notify"] or [])}
    return [(k, v) for k, v in _load_notify_status().items() if v.get("error") and k in live]


# ── metadata ─────────────────────────────────────────────────────────────────
def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", html.unescape(s or ""))
    return " ".join(s.split()).rstrip(".")


def metadata(ident: str) -> dict | None:
    """PubMed first (ISO journal abbreviation, volume/issue/pages — what r/Scholar readers
    search by); Crossref when the DOI is not in PubMed. The DOI keeps the caller's casing."""
    ident = ident.strip().removeprefix("https://doi.org/").removeprefix("doi:")
    if re.fullmatch(r"\d{5,9}", ident):
        return _pubmed(ident)
    pmid = _pmid_for_doi(ident)
    meta = _pubmed(pmid) if pmid else None
    if meta and meta.get("title"):
        return {**meta, "doi": ident}
    try:
        r = requests.get(f"https://api.crossref.org/works/{ident}", timeout=20,
                         headers={"User-Agent": API_UA})
    except requests.RequestException as e:
        print(f"[draft] Crossref: {e}", file=sys.stderr)
        return None
    if r.status_code != 200:
        return None
    m = r.json()["message"]
    authors = m.get("author") or []
    first = authors[0] if authors else {}
    year = None
    for k in ("published-print", "published-online", "issued"):
        parts = (m.get(k) or {}).get("date-parts") or [[None]]
        if parts[0] and parts[0][0]:
            year = parts[0][0]
            break
    initials = "".join(w[0] for w in (first.get("given") or "").replace("-", " ").split())
    return {"doi": ident, "pmid": None, "title": _clean((m.get("title") or [""])[0]),
            "first_author": first.get("family") or first.get("name") or "",
            "first_author_cite": " ".join(x for x in (first.get("family"), initials) if x),
            "n_authors": len(authors),
            "journal": _clean((m.get("short-container-title") or m.get("container-title") or [""])[0]),
            "year": year, "volume": m.get("volume"), "issue": m.get("issue"),
            "pages": m.get("page")}


def _pmid_for_doi(doi: str) -> str | None:
    try:
        r = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                         params={"db": "pubmed", "term": f"{doi}[doi]", "retmode": "json"},
                         timeout=20, headers={"User-Agent": API_UA})
        ids = r.json()["esearchresult"]["idlist"]
        return ids[0] if len(ids) == 1 else None
    except Exception:
        return None


def _pubmed(pmid: str) -> dict | None:
    try:
        r = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                         params={"db": "pubmed", "id": pmid, "retmode": "json"},
                         timeout=20, headers={"User-Agent": API_UA})
        d = r.json()["result"][pmid]
    except Exception as e:
        print(f"[draft] PubMed {pmid}: {type(e).__name__}", file=sys.stderr)
        return None
    if d.get("error"):
        return None
    doi = next((a["value"] for a in d.get("articleids", []) if a.get("idtype") == "doi"), None)
    authors = [a["name"] for a in d.get("authors") or [] if a.get("authtype") == "Author"]
    first = authors[0] if authors else ""
    return {"doi": doi, "pmid": pmid, "title": _clean(d.get("title")),
            "first_author": first.rsplit(" ", 1)[0] if " " in first else first,
            "first_author_cite": first, "n_authors": len(authors),
            "journal": d.get("source") or d.get("fulljournalname") or "",
            "year": (d.get("pubdate") or "")[:4] or None, "volume": d.get("volume"),
            "issue": d.get("issue"), "pages": d.get("pages")}


def oa_status(doi: str) -> dict:
    """{'checked': bool, 'is_oa': bool, 'url': best OA url or None}"""
    email = CFG["unpaywall_email"]
    if not email:
        return {"checked": False, "is_oa": False, "url": None, "why": "unpaywall_email unset"}
    try:
        r = requests.get(f"https://api.unpaywall.org/v2/{doi}", params={"email": email},
                         timeout=20, headers={"User-Agent": API_UA})
    except requests.RequestException as e:
        return {"checked": False, "is_oa": False, "url": None, "why": type(e).__name__}
    if r.status_code == 404:                    # Unpaywall has never seen it → not known OA
        return {"checked": True, "is_oa": False, "url": None}
    if r.status_code != 200:
        return {"checked": False, "is_oa": False, "url": None, "why": f"HTTP {r.status_code}"}
    j = r.json() or {}
    best = j.get("best_oa_location") or {}
    return {"checked": True, "is_oa": bool(j.get("is_oa")), "oa_status": j.get("oa_status"),
            "url": best.get("url_for_pdf") or best.get("url")}


def build_draft(meta: dict) -> dict:
    """The r/Scholar format ([Meta] Updates 2.0): '[Article] <Title> + <Author> et al.
    (<Journal>, <Year>)'. The author is required by the template — omitting it is the
    commonest reason a request gets a "please follow the format" reply."""
    au = meta["first_author"] + (" et al." if meta.get("n_authors", 1) > 1 else "")
    tail = f" + {au} ({meta['journal']}, {meta['year']})"
    title = meta["title"]
    room = 300 - len("[Article] ") - len(tail)          # Reddit's hard title limit
    if len(title) > room:
        title = title[:room - 1].rstrip() + "…"
    cite = f"{meta['journal']}. {meta['year']}"
    if meta.get("volume"):
        cite += f";{meta['volume']}" + (f"({meta['issue']})" if meta.get("issue") else "")
        cite += f":{meta['pages']}" if meta.get("pages") else ""
    lines = [f"{meta.get('first_author_cite') or meta['first_author']}"
             f"{', et al' if meta.get('n_authors', 1) > 1 else ''}. {cite}."]
    if meta.get("doi"):
        lines += [f"DOI: {meta['doi']}", f"https://doi.org/{meta['doi']}"]
    else:
        lines.append("No DOI is registered for this article.")
    if meta.get("pmid"):
        lines.append(f"PMID: {meta['pmid']} — https://pubmed.ncbi.nlm.nih.gov/{meta['pmid']}/")
    lines.append(_R["body_note"])
    return {"subreddit": _R["subreddit"], "flair": "Requesting",
            "title": f"[Article] {title}{tail}", "body": "\n\n".join(lines),
            "doi": meta.get("doi"), "pmid": meta.get("pmid"), "article_title": meta["title"]}


def draft_for_envelope(doi: str, env: dict | None = None) -> dict:
    """What paper_fetch puts in `reddit_draft` when its ladder ends empty."""
    env = env or {}
    if env.get("blocked_by") or env.get("free_at_publisher"):
        return {"refused": "try_browser_first",
                "next": "a bot wall or a free-at-publisher flag means a real browser can likely "
                        "get it: library_session.py fetch <DOI> <out.pdf> (direct route)"}
    oa = oa_status(doi)
    if oa["is_oa"]:
        return {"refused": "open_access", "oa_url": oa["url"],
                "next": "open the OA copy in a real browser: library_session.py fetch --direct"}
    meta = metadata(doi)
    if not meta or not meta.get("title"):
        return {"refused": "no_metadata"}
    d = build_draft(meta)
    d["oa_checked"] = oa["checked"]
    d["precondition"] = ("post only after library_session.py fetch also ended 2/7 — "
                         "the library layer has not been tried by this script")
    return d


# ── Reddit transport (one gate for every request) ────────────────────────────
def reddit_get(st: dict, url: str, headers: dict | None = None) -> requests.Response:
    now = time.time()
    if st.get("backoff_until", 0) > now:
        raise RateLimited(f"backoff until {_dt.datetime.fromtimestamp(st['backoff_until']):%H:%M:%S}")
    wait = st.get("reddit_next_ok", 0) - now
    if wait > _R["max_wait_s"]:
        raise RateLimited(f"next Reddit slot in {wait:.0f}s")
    if wait > 0:
        print(f"[reddit] rate budget: waiting {wait:.0f}s", file=sys.stderr)
        time.sleep(wait)
    r = requests.get(url, timeout=30, headers={"User-Agent": REDDIT_UA, **(headers or {})})
    remaining = float(r.headers.get("x-ratelimit-remaining") or 1)
    reset = float(r.headers.get("x-ratelimit-reset") or 0)
    st["reddit_next_ok"] = time.time() + (reset + random.uniform(3, 9) if remaining < 1
                                          else random.uniform(4, 10))
    if r.status_code == 429:
        back = max(float(r.headers.get("retry-after") or 0), reset, 600)
        st["backoff_until"] = time.time() + back
        save_state(st)
        raise RateLimited(f"HTTP 429 — backing off {back / 60:.0f} min")
    save_state(st)
    return r


def parse_feed(xml: str) -> list[dict]:
    root = ET.fromstring(xml)
    out = []
    for e in root.findall(f"{ATOM}entry"):
        content = html.unescape(e.findtext(f"{ATOM}content") or "")
        links = re.findall(r'href="([^"]+)"', content)
        link_el = e.find(f"{ATOM}link")
        out.append({"id": e.findtext(f"{ATOM}id") or "",
                    "author": (e.findtext(f"{ATOM}author/{ATOM}name") or "").removeprefix("/u/"),
                    "title": e.findtext(f"{ATOM}title") or "",
                    "published": (e.findtext(f"{ATOM}published") or "")[:16].replace("T", " "),
                    "link": link_el.get("href") if link_el is not None else "",
                    "text": re.sub(r"<[^>]+>", " ", content),
                    "external": _external(links)})
    return out


def _external(urls: list[str]) -> list[str]:
    return [u for u in urls if u.startswith("http")
            and not re.match(r"https?://([a-z0-9-]+\.)?(reddit\.com|redd\.it)/", u)]


def parse_listing(data) -> list[dict]:
    """Reddit's /comments/<id>.json (a list of two Listings: the post, then its comment tree)
    → the same entries parse_feed() makes, so _ingest() is unchanged. Nested replies are
    flattened; "load more" stubs (kind `more`) are skipped. Raises ValueError on any other shape."""
    if not (isinstance(data, list) and len(data) == 2
            and all(isinstance(x, dict) and x.get("kind") == "Listing" for x in data)):
        raise ValueError("not a Reddit thread listing")

    def when(utc) -> str:
        try:
            return _dt.datetime.fromtimestamp(float(utc), _dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            return ""

    def entry(kind: str, d: dict) -> dict:
        # body_html is escaped HTML unless the URL asked for raw_json=1 — unescape either way
        page = html.unescape(d.get("body_html") or d.get("selftext_html") or "")
        text = re.sub(r"<[^>]+>", " ", page) if page else (d.get("body") or d.get("selftext") or "")
        urls = ([html.unescape(u) for u in re.findall(r'href="([^"]+)"', page)]
                or re.findall(r"https?://[^\s)\]]+", text))
        return {"id": d.get("name") or f"{kind}_{d.get('id', '')}",
                "author": d.get("author") or "",
                "title": d.get("title", "") if kind == "t3" else "",
                "published": when(d.get("created_utc")),
                "link": "https://www.reddit.com" + (d.get("permalink") or ""),
                "text": text,
                "external": _external(urls)}

    out = [entry("t3", c["data"]) for c in data[0]["data"].get("children", []) if c.get("kind") == "t3"]

    def walk(children):
        for c in children or []:
            if c.get("kind") != "t1":
                continue
            out.append(entry("t1", c["data"]))
            rep_ = c["data"].get("replies")
            if isinstance(rep_, dict):
                walk(rep_.get("data", {}).get("children"))
    walk(data[1]["data"].get("children"))
    return out


def post_id(ref: str) -> str:
    m = re.search(r"comments/([a-z0-9]+)", ref)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-z0-9]{5,10}", ref):
        return ref
    sys.exit(f"not a Reddit post URL or id: {ref!r}")


# ── commands ─────────────────────────────────────────────────────────────────
def cmd_draft(a) -> int:
    meta = metadata(a.ident)
    if not meta or not meta.get("title"):
        print(f"no metadata for {a.ident} (Crossref and PubMed both empty)", file=sys.stderr)
        return 2
    if meta.get("doi"):
        oa = oa_status(meta["doi"])
        if oa["is_oa"]:
            print(f"✗ refused: Unpaywall says this is open access ({oa.get('oa_status')}).\n"
                  f"  OA copy: {oa['url']}\n"
                  f"  A bot wall is not a paywall — open it in a real browser "
                  f"(library_session.py fetch {meta['doi']} out.pdf --direct).", file=sys.stderr)
            return 8
        if not oa["checked"] and not a.skip_oa_check:
            print(f"✗ could not check Unpaywall ({oa.get('why')}); fix that or pass "
                  "--skip-oa-check after checking by hand.", file=sys.stderr)
            return 2
    d = build_draft(meta)
    st = load_state()
    key = (meta.get("doi") or f"pmid:{meta['pmid']}").lower()
    st["drafts"][key] = {**d, "status": "drafted", "created": _now(),
                         "record_id": a.record_id, "dest": _dest(a.dest)}
    live = [p for p in st["posts"].values() if (p.get("doi") or "").lower() == key]
    if live:                                   # already out there — this draft is a preview only
        st["drafts"][key].update(status="posted", post_id=live[0]["id"])
        print(f"note: already tracked as {live[0]['url']} ({live[0]['status']})", file=sys.stderr)
    save_state(st)
    if a.json:
        print(json.dumps(d, ensure_ascii=False))
    else:
        print(f"Title:\n{d['title']}\n\nBody:\n{d['body']}\n\nFlair: {d['flair']}"
              f"   → https://www.reddit.com/r/{_R['subreddit']}/submit")
        print(f"\nRun `dedupe {key}` before posting; after posting: track <post_url> --doi {key}",
              file=sys.stderr)
    return 0


def search_url(doi: str) -> str:
    """The r/<sub> search page for this DOI — what a person opens to check by hand."""
    q = requests.utils.quote(f'"{doi}"', safe="")
    return f"https://www.reddit.com/r/{_R['subreddit']}/search/?q={q}&type=posts&sort=new"


def _dedupe_cmd(doi: str, manual: str) -> list[dict]:
    """Run reddit.dedupe_cmd ("{doi}" substituted). It must exit 0 and print one JSON object
    {"existing": [{"id", "url", "author", "title"}, ...]} on stdout; anything else means it
    could not look."""
    argv = [str(x).replace("{doi}", doi) for x in _R["dedupe_cmd"]]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DedupeUnavailable(f"dedupe_cmd: {type(e).__name__}: {e}"[:200], manual) from None
    tail = (p.stderr or "").strip().splitlines()[-1:] or [""]
    if p.returncode != 0:
        raise DedupeUnavailable(f"dedupe_cmd exit {p.returncode}: {tail[0][:160]}", manual)
    try:
        hits = json.loads(p.stdout.strip().splitlines()[-1])["existing"]
        assert isinstance(hits, list)
    except Exception:
        raise DedupeUnavailable(f"dedupe_cmd printed no {{\"existing\": [...]}} JSON "
                                f"({tail[0][:120]})", manual) from None
    return hits


def dedupe(st: dict, doi: str) -> list[dict]:
    """Requests for this DOI already on r/<sub>: a list (empty = looked, nobody asked).
    Raises DedupeUnavailable when it could not look and RateLimited on Reddit's 429.
    `reddit.dedupe_cmd` is used when set — since Reddit switched RSS off (2026-11-13) the
    search feed below answers 404, so a real search needs a browser behind that hook."""
    manual = search_url(doi)
    if _R.get("dedupe_cmd"):
        return _dedupe_cmd(doi, manual)
    q = requests.utils.quote(f'"{doi}"')
    try:
        r = reddit_get(st, f"https://www.reddit.com/r/{_R['subreddit']}/search.rss"
                           f"?q={q}&restrict_sr=1&sort=new")
    except requests.RequestException as e:
        raise DedupeUnavailable(f"search feed: {type(e).__name__}", manual) from None
    if r.status_code != 200:
        why = f"search feed HTTP {r.status_code}"
        if r.status_code == 404:
            why += " (Reddit switched RSS off on 2026-11-13; set reddit.dedupe_cmd)"
        raise DedupeUnavailable(why, manual)
    try:                                  # an HTML page parses as XML too — demand an Atom feed
        if ET.fromstring(r.text).tag != f"{ATOM}feed":
            raise ET.ParseError("not an Atom feed")
        feed = parse_feed(r.text)
    except ET.ParseError:
        raise DedupeUnavailable("search feed: HTTP 200 but not a feed", manual) from None
    hits = [e for e in feed if e["id"].startswith("t3_")
            and doi.lower() in (e["title"] + " " + e["text"]).lower()]
    return [{"id": e["id"][3:], "url": e["link"], "author": e["author"], "title": e["title"]}
            for e in hits]


def cmd_dedupe(a) -> int:
    st = load_state()
    doi = a.doi.strip().removeprefix("https://doi.org/").lower()
    try:
        hits = dedupe(st, doi)
    except DedupeUnavailable as e:
        print(json.dumps({"doi": doi, "existing": None, "unavailable": str(e),
                          "manual_url": e.manual_url}, ensure_ascii=False))
        print(f"dedupe unavailable: {e}\n  check by hand before posting: {e.manual_url}",
              file=sys.stderr)
        return 10
    except RateLimited as e:
        print(json.dumps({"doi": doi, "existing": None, "rate_limited": str(e),
                          "manual_url": search_url(doi)}, ensure_ascii=False))
        print(f"dedupe rate-limited: {e} — wait, do not loop", file=sys.stderr)
        return 7
    print(json.dumps({"doi": doi, "existing": hits}, ensure_ascii=False))
    if hits:
        print(f"already requested {len(hits)}× — track that thread instead of posting again: "
              f"track {hits[0]['url']} --doi {doi}", file=sys.stderr)
    return 0


def _dest(path: str | None) -> str | None:
    """Where the PDF should end up once someone sends it — carried by every notification."""
    return str(pathlib.Path(os.path.expanduser(path)).resolve()) if path else None


def cmd_track(a) -> int:
    st = load_state()
    pid = post_id(a.url)
    doi = a.doi.strip().lower() if a.doi else None
    d = st["drafts"].get(doi or "", {})
    title = a.title or d.get("article_title")
    if not title and doi:
        meta = metadata(doi)
        title = meta and meta["title"]
    rec = st["posts"].get(pid) or {"id": pid, "status": "posted", "created": _now(),
                                   "seen": [], "comments": 0, "links": []}
    rec.update({k: v for k, v in {"url": f"https://www.reddit.com/comments/{pid}/",
                                  "doi": doi, "title": title,
                                  "record_id": a.record_id or d.get("record_id"),
                                  "dest": _dest(a.dest) or d.get("dest")}.items() if v})
    rec.setdefault("updated", rec["created"])
    st["posts"][pid] = rec
    if d:
        d["status"], d["post_id"] = "posted", pid
    save_state(st)
    emit("tracked", rec, notify=False)
    if rec["status"] in OPEN_STATES:
        _hook("on_active")
        warn_no_sink()
    print(f"tracking {pid} ({rec['status']}): {title or '(title from first poll)'}")
    return 0


def _ingest(st: dict, rec: dict, entries: list[dict]) -> list[str]:
    """Fold feed entries into one post record; returns the events it caused."""
    me = {x.lower() for x in _R["ignore_authors"]}
    events = []
    for e in entries:
        if e["id"].startswith("t3_"):
            rec["op"] = e["author"]
            rec.setdefault("post_title", e["title"])
            if e["published"]:
                rec["posted_at"] = e["published"]          # UTC, from Reddit
            # no DOI given at track time → best guess from the post title, for `verify`
            rec["title"] = rec.get("title") or re.sub(
                r"^\[[^\]]*\]\s*|\s*(\+[^()]*)?\([^()]*\)\s*$", "", e["title"]).strip()
    for e in entries:
        # The poster's own "thanks solution verified" (the subreddit's scoring phrase) closes
        # the loop even when the PDF was checked by hand. Re-read every poll, not only new
        # comments: a thank-you posted before this check existed is still in the feed.
        if (e["id"].startswith("t1_") and e["author"] == rec.get("op")
                and re.search(r"solution\s+verified", e["text"], re.I)):
            rec.setdefault("thanked", e["id"])
            if _advance(rec, "verified"):
                events.append("verified_by_op")
    for e in entries:
        if not e["id"].startswith("t1_") or e["id"] in rec["seen"]:
            continue
        rec["seen"].append(e["id"])
        if e["author"].lower() in me or e["author"] == rec.get("op"):
            continue
        rec["comments"] = rec.get("comments", 0) + 1
        if e["external"]:
            rec["links"].append({"comment": e["link"], "author": e["author"], "n": len(e["external"]),
                                 "hosts": sorted({_host(u) for u in e["external"]})})
            if _advance(rec, "fulfilled"):
                events.append("fulfilled")
        elif _advance(rec, "replied"):
            events.append("replied")
    return events


def _poll_cmd(pid: str) -> list[dict]:
    """Run reddit.poll_cmd ("{id}" substituted). It must exit 0 and print the JSON of
    /comments/<id>.json on stdout — for THIS thread; anything else is PollUnavailable."""
    argv = [str(x).replace("{id}", pid) for x in _R["poll_cmd"]]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PollUnavailable(f"poll_cmd: {type(e).__name__}: {e}"[:200]) from None
    tail = (p.stderr or "").strip().splitlines()[-1:] or [""]
    if p.returncode != 0:
        raise PollUnavailable(f"poll_cmd exit {p.returncode}: {tail[0][:160]}")
    try:
        entries = parse_listing(json.loads(p.stdout))
    except (ValueError, KeyError, TypeError, AttributeError):
        raise PollUnavailable(f"poll_cmd printed no thread JSON ({tail[0][:120]})") from None
    if not any(e["id"] == f"t3_{pid}" for e in entries):
        raise PollUnavailable(f"poll_cmd printed a different thread, not t3_{pid}")
    return entries


def _hook(name: str) -> None:
    argv = _R.get(name) or []
    if not argv:
        return
    try:
        subprocess.run(argv, capture_output=True, timeout=30, check=True)
    except Exception as e:                  # a scheduler hiccup must not fail the command
        print(f"[{name}] {argv[0]} failed: {type(e).__name__}", file=sys.stderr)


def _close_stale(st: dict) -> None:
    """A request nobody has answered in `stale_days` is not coming back; stop polling it."""
    limit = _R.get("stale_days") or 0
    if not limit:
        return
    now = _dt.datetime.now(_dt.timezone.utc)
    for rec in st["posts"].values():
        if rec["status"] != "posted":
            continue
        if rec.get("poll_http") not in (200, 304):  # never read (or feed gone): "no reply" is
            continue                                # unknown, not observed — keep it open
        when = rec.get("posted_at") or rec.get("created", "")
        try:
            t = _dt.datetime.fromisoformat(when.replace(" ", "T"))
        except ValueError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=_dt.timezone.utc)
        if (now - t).days >= limit:
            _advance(rec, "closed")
            rec["closed_reason"] = f"no reply in {limit} days"
            emit("stale", rec)


def cmd_poll(a) -> int:
    st = load_state()
    _close_stale(st)
    catch_up(st)                        # replies a sink missed (added later, or failing then)
    save_state(st)
    todo = [r for r in st["posts"].values() if r["status"] in OPEN_STATES]
    if not todo:
        print("nothing open to watch" + (" — running on_idle" if _R.get("on_idle") else ""))
        _hook("on_idle")
        return 0
    warn_no_sink()
    report = []
    try:
        if _R["inbox_feed_secret"]:
            feed = _secret(_R["inbox_feed_secret"])
            if feed:
                r = reddit_get(st, feed)
                if r.status_code == 200:
                    inbox = parse_feed(r.text)
                    seen = st.setdefault("inbox_seen", [])
                    fresh = [e for e in inbox if e["id"] not in seen]
                    seen.extend(e["id"] for e in fresh)
                    for rec in todo:
                        mine = [e for e in fresh if f"comments/{rec['id']}/" in e["link"]]
                        for ev in _ingest(st, rec, mine):
                            emit(ev, rec, notify=ev != "verified_by_op")
                    if any(e["id"].startswith("t4_") for e in fresh):
                        emit("inbox", None, messages=sum(e["id"].startswith("t4_") for e in fresh))
            else:
                print(f"[poll] secret {_R['inbox_feed_secret']!r} not stored — inbox skipped",
                      file=sys.stderr)
        for i, rec in enumerate(todo):
            if _R.get("poll_cmd"):
                if i:
                    time.sleep(random.uniform(3, 8))        # a person does not open them at once
                rec["polled"] = _now()
                try:
                    entries = _poll_cmd(rec["id"])
                except PollUnavailable as e:
                    rec["poll_http"] = "cmd_failed"
                    print(f"[poll] {rec['id']}: {e}", file=sys.stderr)
                    report.append({"id": rec["id"], "http": "cmd_failed", "events": []})
                    if not rec.get("blind_notified"):
                        rec["blind_notified"] = _now()
                        emit("poll_blind", rec, http="cmd_failed")
                    continue
                rec["poll_http"] = 200                     # read — stale_days may judge it
                rec.pop("blind_notified", None)
                evs = _ingest(st, rec, entries)
                save_state(st)
                for ev in evs:
                    emit(ev, rec, notify=ev != "verified_by_op")
                report.append({"id": rec["id"], "http": 200, "events": evs, "status": rec["status"]})
                continue
            hdr = {}
            if rec.get("etag"):
                hdr["If-None-Match"] = rec["etag"]
            if rec.get("last_modified"):
                hdr["If-Modified-Since"] = rec["last_modified"]
            r = reddit_get(st, f"https://www.reddit.com/comments/{rec['id']}/.rss", hdr)
            rec["polled"] = _now()
            rec["poll_http"] = r.status_code
            if r.status_code in (200, 304):
                rec.pop("blind_notified", None)
            if r.status_code == 304:
                report.append({"id": rec["id"], "http": 304, "events": []})
                continue
            if r.status_code != 200:
                report.append({"id": rec["id"], "http": r.status_code, "events": []})
                if not rec.get("blind_notified"):
                    rec["blind_notified"] = _now()
                    emit("poll_blind", rec, http=r.status_code)
                continue
            rec["etag"] = r.headers.get("etag") or rec.get("etag")
            rec["last_modified"] = r.headers.get("last-modified") or rec.get("last_modified")
            evs = _ingest(st, rec, parse_feed(r.text))
            save_state(st)
            for ev in evs:
                emit(ev, rec, notify=ev != "verified_by_op")
            report.append({"id": rec["id"], "http": 200, "events": evs, "status": rec["status"]})
    except RateLimited as e:
        save_state(st)
        print(f"[poll] stopped: {e}. Not retrying — the next scheduled run picks up.",
              file=sys.stderr)
        if a.json:
            print(json.dumps({"polled": report, "rate_limited": str(e)}))
        return 7
    save_state(st)
    blind = [x for x in report if x["http"] not in (200, 304)]
    if a.json:
        print(json.dumps({"polled": report, "blind": [x["id"] for x in blind]}, ensure_ascii=False))
    else:
        for x in report:
            print(f"{x['id']}  HTTP {x['http']}  {x.get('status', '')}  "
                  f"{', '.join(x['events']) or 'no new events'}")
    if blind:
        print(f"[poll] BLIND: {len(blind)} thread(s) unreadable (Reddit switched RSS off on "
              "2026-11-13; " + ("reddit.poll_cmd failed" if _R.get("poll_cmd") else
                                "set reddit.poll_cmd") + ") — replies are NOT being watched. "
              "Open by hand:\n  "
              + "\n  ".join(st["posts"][x["id"]].get("url") or x["id"] for x in blind),
              file=sys.stderr)
        return 10
    return 0


def cmd_verify(a) -> int:
    import pdf_verify
    st = load_state()
    rec = st["posts"].get(post_id(a.post))
    if not rec:
        sys.exit(f"{a.post} is not tracked")
    if a.manual:                        # the user opened the file and says it is the article
        _advance(rec, "verified")
        rec["verified_pdf"] = "manual"
        save_state(st)
        emit("verified", rec, notify=False)
        print(f"{rec['id']} marked verified by hand")
        return 0
    if not a.pdf:
        sys.exit("give the PDF, or --manual if you checked it yourself")
    if not rec.get("title"):
        sys.exit("no article title on record — re-run track with --title")
    res = pdf_verify.verify(a.pdf, rec["title"])
    ok = res["verdict"] == "match"
    reg = None
    if a.registry:
        with pdf_verify._open(a.pdf) as doc:
            full = " ".join(pdf_verify._page_texts(doc))
        reg = pdf_verify.norm(a.registry) in full
        ok = ok and reg
    print(f"{res['verdict']}: {res['detail']}"
          + (f" · registry {a.registry} {'found' if reg else 'NOT found'}" if a.registry else ""))
    if not ok:
        return 2
    _advance(rec, "verified")
    rec["verified_pdf"] = str(pathlib.Path(a.pdf).resolve())
    save_state(st)
    emit("verified", rec)
    who = rec["links"][-1]["comment"] if rec.get("links") else rec["url"]
    print(f"✓ verified. Reply under the fulfiller's comment with exactly: thanks solution verified\n"
          f"  {who}")
    if rec.get("dest") and pathlib.Path(rec["dest"]).resolve() != pathlib.Path(a.pdf).resolve():
        print(f"  requested destination: {rec['dest']}")
    return 0


def cmd_close(a) -> int:
    st = load_state()
    rec = st["posts"].get(post_id(a.post))
    if not rec:
        sys.exit(f"{a.post} is not tracked")
    _advance(rec, "closed")
    save_state(st)
    emit("closed", rec, notify=False)
    print(f"{rec['id']} closed")
    return 0


def status_table(st: dict) -> list[dict]:
    """One row per request: what went out, whether anyone answered, where it stands."""
    rows = []
    for r in sorted(st["posts"].values(), key=lambda r: r.get("posted_at") or r.get("created", "")):
        rows.append({"posted": (r.get("posted_at") or r.get("created", ""))[:10] or "?",
                     "article": (r.get("title") or "?")[:70], "doi": r.get("doi") or "-",
                     "record": r.get("record_id") or "", "replies": r.get("comments", 0),
                     "link": "yes" if r.get("links") else "no", "status": r["status"],
                     "thanked": "yes" if r.get("thanked") else "no", "url": r.get("url", "")})
    return rows


def status_markdown(st: dict) -> str:
    rows = status_table(st)
    head = ("| Posted (UTC) | Article | DOI | Replies | Link offered | Status | Thanked | Thread |\n"
            "|---|---|---|---|---|---|---|---|\n")
    body = "".join(f"| {x['posted']} | {x['article'].replace('|', '/')} | {x['doi']} | {x['replies']} | "
                   f"{x['link']} | {x['status']} | {x['thanked']} | [open]({x['url']}) |\n" for x in rows)
    drafts = [d for d in st["drafts"].values() if d.get("status") == "drafted"]
    tail = ("\nDrafted, not posted: " + ", ".join(d.get("doi") or "?" for d in drafts) + "\n"
            if drafts else "")
    warn = "" if has_sink() else f"> ⚠️ **{NO_SINK}**\n\n"
    warn += "".join(f"> ⚠️ **Notify sink `{k}` failing since {v['failed'][:16]} UTC:** {v['error']} "
                    "— fix it, then `notify-test` clears this.\n\n" for k, v in failing_sinks())
    return f"# r/{_R['subreddit']} requests — {_now()[:16]} UTC\n\n" + warn + head + body + tail


def write_status_md(st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "status.md").write_text(status_markdown(st), encoding="utf-8")


def cmd_status(a) -> int:
    st = load_state()
    if a.json:
        print(json.dumps({"requests": status_table(st),
                          "drafts": [d for d in st["drafts"].values() if d.get("status") == "drafted"],
                          "backoff_until": st.get("backoff_until", 0)}, ensure_ascii=False))
    elif a.md:
        print(status_markdown(st))
    else:
        rows = status_table(st)
        print(f"{'posted':<10}  {'status':<9} {'replies':>7}  link  thanked  article")
        for x in rows:
            print(f"{x['posted']:<10}  {x['status']:<9} {x['replies']:>7}  {x['link']:<4}  "
                  f"{x['thanked']:<7}  {x['article'][:60]}")
        drafts = [d for d in st["drafts"].values() if d.get("status") == "drafted"]
        if drafts:
            print(f"\n{len(drafts)} draft(s) not yet posted: " + ", ".join(d.get("doi") or "?" for d in drafts))
    return 0


def _telegram_chat_ids(sink: dict) -> list[str]:
    """chat_id discovery: the chats that recently messaged the bot (getUpdates)."""
    token = _secret(sink.get("token_secret", ""))
    if not token:
        return []
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15)
        ups = r.json().get("result", []) if r.ok else []
    except (requests.RequestException, ValueError):
        return []
    seen = {}
    for u in ups:
        c = (u.get("message") or u.get("channel_post") or {}).get("chat") or {}
        if "id" in c:
            seen[str(c["id"])] = c.get("title") or c.get("username") or c.get("first_name") or ""
    return [f"{k} ({v})" for k, v in seen.items()]


def cmd_notify_test(a) -> int:
    """Send one test event through every sink (or --only one) and say, per sink, OK or why not."""
    sinks = [(i, s) for i, s in enumerate(_R["notify"] or []) if (s or {}).get("type") != "jsonl"]
    if a.only:
        sinks = [(i, s) for i, s in sinks
                 if a.only in (str(i + 1), (s or {}).get("type"), (s or {}).get("name"), _label(i, s))]
        if not sinks:
            print(f"no sink matches {a.only!r} (give a type, a `name`, or a 1-based index)",
                  file=sys.stderr)
            return 1
    ev = {"ts": _now(), "event": "test", "post_id": None, "url": None,
          "title": "paper-fetch notification test", "doi": None, "record_id": None,
          "status": "test", "comments": 0}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with EVENTS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({**ev, "sinks": [_label(i, s) for i, s in sinks]}) + "\n")
    print(f"test event written to {EVENTS_FILE}")
    if not sinks:
        print(f"\n!!! {NO_SINK}")
        return NOTIFY_EXIT
    failed = 0
    for i, s in sinks:
        err = deliver(i, s, ev)
        print(f"  {'OK  ' if not err else 'FAIL'}  {_label(i, s)}" + (f"  — {err}" if err else ""))
        failed += bool(err)
        if s.get("type") in ("discord", "webhook") and s.get("url") and not s.get("url_secret"):
            print("        note: a webhook URL is a password — anyone holding it can post. "
                  "Store it and use `url_secret` instead of `url`.")
        if s.get("type") == "telegram" and err and "chat_id" in err:
            ids = _telegram_chat_ids(s)
            print("        chats that messaged this bot: " + (", ".join(ids) if ids else
                  "none yet — open your bot in Telegram, press Start, then re-run"))
        if s.get("type") == "ntfy" and not s.get("token_secret") and \
                (s.get("server") or "https://ntfy.sh").rstrip("/") == "https://ntfy.sh":
            print("        note: an open ntfy.sh topic is readable by anyone who guesses its name "
                  "— make it long and random.")
    print(f"{len(sinks) - failed}/{len(sinks)} sink(s) delivered")
    st = load_state()                   # a sink that works now gets the replies it missed
    late = catch_up(st, {sink_key(s) for i, s in sinks})
    if late:
        save_state(st)
        print(f"{late} missed notification(s) delivered late (see events.jsonl)")
    return NOTIFY_EXIT if failed else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="r/Scholar request drafts and tracking")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("draft")
    p.add_argument("ident", help="DOI or PMID")
    p.add_argument("--record-id")
    p.add_argument("--dest", help="where the PDF should go once received (shown in notifications)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--skip-oa-check", action="store_true")
    p = sub.add_parser("dedupe")
    p.add_argument("doi")
    p = sub.add_parser("track")
    p.add_argument("url")
    p.add_argument("--doi")
    p.add_argument("--record-id")
    p.add_argument("--title")
    p.add_argument("--dest", help="where the PDF should go once received (shown in notifications)")
    p = sub.add_parser("poll")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("verify")
    p.add_argument("post")
    p.add_argument("pdf", nargs="?")
    p.add_argument("--registry", help="trial registry id that must appear in the PDF")
    p.add_argument("--manual", action="store_true", help="you checked the file yourself")
    p = sub.add_parser("close")
    p.add_argument("post")
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p.add_argument("--md", action="store_true", help="markdown table (also written by poll)")
    p = sub.add_parser("notify-test")
    p.add_argument("--only", help="one sink: its type, `name`, or 1-based index")
    a = ap.parse_args(argv)
    try:
        return globals()[f"cmd_{a.cmd.replace('-', '_')}"](a)
    except RateLimited as e:
        print(f"Reddit rate limit: {e}. Wait — do not retry in a loop.", file=sys.stderr)
        return 7


if __name__ == "__main__":
    sys.exit(main())
