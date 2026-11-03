"""reddit_request notify sinks: discord / telegram / email / ntfy / webhook / command,
`events` filter, failure bookkeeping (status.md), notify-test exit codes, secret redaction.

No network: HTTP goes to a throwaway local server, SMTP to a stub class, secrets to a dict.
Run: python -m pytest tests -q
"""
import http.server
import json
import pathlib
import smtplib
import sys
import threading

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import reddit_request as rr  # noqa: E402

SECRETS = {"DISCORD_URL": None, "TG_TOKEN": "123:SECRET-TOKEN", "SMTP_PW": "hunter2"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "STATE_DIR", tmp_path)
    monkeypatch.setattr(rr, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(rr, "EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(rr, "_secret", lambda name: SECRETS.get(name) if name else None)
    monkeypatch.setattr(rr, "_store_hint", lambda name: f"store {name}")
    monkeypatch.setitem(rr._R, "notify", [])
    return tmp_path


class _Hook(http.server.BaseHTTPRequestHandler):
    status = 204
    body = b""
    seen: list = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        _Hook.seen.append({"path": self.path, "json": json.loads(self.rfile.read(n) or b"null")})
        self.send_response(_Hook.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(_Hook.body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    _Hook.seen, _Hook.status, _Hook.body = [], 204, b""
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


REC = {"id": "abc123", "url": "https://www.reddit.com/r/Scholar/comments/abc123/x/",
       "title": "A test article", "doi": "10.1/x", "status": "fulfilled", "comments": 1}


# ── config checks ────────────────────────────────────────────────────────────
def test_sink_problems_name_the_fix(env):
    assert "unknown type" in rr.sink_problems({"type": "discrod"})[0]
    assert "missing `url` or `url_secret`" in rr.sink_problems({"type": "discord"})[0]
    p = rr.sink_problems({"type": "telegram", "token_secret": "NOPE", "chat_id": 1})
    assert p == ["secret 'NOPE' (bot token) is not stored — store NOPE"]
    assert "unknown event" in rr.sink_problems({"type": "command", "argv": ["x"],
                                                "events": ["replyed"]})[0]
    assert rr.sink_problems({"type": "telegram", "token_secret": "TG_TOKEN", "chat_id": 1}) == []


def test_misconfigured_sink_counts_as_no_sink(env, monkeypatch):
    monkeypatch.setitem(rr._R, "notify", [{"type": "telegram", "token_secret": "NOPE", "chat_id": 1}])
    assert not rr.has_sink()


# ── channels ─────────────────────────────────────────────────────────────────
def test_discord_payload(env, server, monkeypatch):
    monkeypatch.setitem(rr._R, "notify", [{"type": "discord", "url": server + "/wh"}])
    rr.emit("fulfilled", REC)
    (hit,) = _Hook.seen
    assert hit["json"]["content"].startswith("**r/Scholar: reply with a link**")
    assert "DOI 10.1/x" in hit["json"]["content"]
    assert hit["json"]["allowed_mentions"] == {"parse": []}


def test_discord_failure_is_recorded_without_the_url(env, server, monkeypatch, capsys):
    _Hook.status = 404
    url = server + "/api/webhooks/1/SECRETPART"
    monkeypatch.setitem(rr._R, "notify", [{"type": "discord", "url": url}])
    rr.emit("replied", REC)
    err = capsys.readouterr().err
    assert "HTTP 404" in err and "copy it again" in err and "SECRETPART" not in err
    events = [json.loads(x) for x in (env / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event"] == "notify_failed" and "SECRETPART" not in json.dumps(events)
    md = rr.status_markdown({"posts": {}, "drafts": {}})
    assert "Notify sink `1:discord` failing" in md
    _Hook.status = 204                                     # fixed → a success clears it
    rr.emit("replied", REC)
    assert "failing" not in rr.status_markdown({"posts": {}, "drafts": {}})


def test_telegram_sends_and_redacts_token(env, monkeypatch):
    sent = {}

    class R:
        ok, status_code = True, 200

    def fake_post(url, *, secrets=(), **kw):
        sent.update(url=url, **kw)
        return R()

    monkeypatch.setattr(rr, "_post", fake_post)
    rr._send_telegram({"type": "telegram", "token_secret": "TG_TOKEN", "chat_id": 42},
                      {**REC, "event": "replied"})
    assert sent["url"].endswith("/bot123:SECRET-TOKEN/sendMessage")
    assert sent["json"]["chat_id"] == 42 and "new reply" in sent["json"]["text"]


def test_post_errors_never_carry_url_or_token():
    with pytest.raises(rr.NotifyError) as e:
        rr._post("http://127.0.0.1:9/bot123:SECRET-TOKEN/sendMessage", secrets=("123:SECRET-TOKEN",))
    assert "SECRET-TOKEN" not in str(e.value) and "127.0.0.1:9/bot" not in str(e.value)


class FakeSMTP:
    sent, fail_login = [], False

    def __init__(self, host, port, timeout):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.tls = True

    def login(self, user, pw):
        if FakeSMTP.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials hunter2")
        self.user = user

    def send_message(self, m):
        FakeSMTP.sent.append((self.host, self.port, getattr(self, "user", None), m))


def test_email(env, monkeypatch):
    FakeSMTP.sent, FakeSMTP.fail_login = [], False
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    sink = {"type": "email", "host": "smtp.example.org", "from": "me@example.org",
            "to": ["a@example.org", "b@example.org"], "password_secret": "SMTP_PW"}
    assert rr.deliver(0, sink, {**REC, "event": "stale"}) is None
    host, port, user, m = FakeSMTP.sent[0]
    assert (host, port, user) == ("smtp.example.org", 587, "me@example.org")
    assert m["Subject"] == "r/Scholar: closed — no reply" and m["To"] == "a@example.org, b@example.org"
    FakeSMTP.fail_login = True
    err = rr.deliver(0, sink, {**REC, "event": "stale"})
    assert "app password" in err and "hunter2" not in err


def test_ntfy_still_works(env, server):
    rr._send_ntfy({"type": "ntfy", "server": server, "topic": "t"}, {**REC, "event": "fulfilled"})
    (hit,) = _Hook.seen
    assert hit["json"]["topic"] == "t" and hit["json"]["click"] == REC["url"]


def test_command_missing_binary_hint(env):
    err = rr.deliver(0, {"type": "command", "argv": ["no-such-binary-xyz"]}, {**REC, "event": "test"})
    assert "absolute paths" in err


# ── events filter ────────────────────────────────────────────────────────────
def test_events_filter(env, server, monkeypatch):
    monkeypatch.setitem(rr._R, "notify", [{"type": "webhook", "url": server, "events": ["fulfilled"]}])
    rr.emit("replied", REC)
    assert _Hook.seen == []
    rr.emit("fulfilled", REC)
    rr.emit("test", REC)                                   # a test always goes through
    assert [h["json"]["event"] for h in _Hook.seen] == ["fulfilled", "test"]


# ── notify-test ──────────────────────────────────────────────────────────────
def test_notify_test_exit_codes(env, server, monkeypatch, capsys):
    assert rr.main(["notify-test"]) == rr.NOTIFY_EXIT        # nothing configured
    monkeypatch.setitem(rr._R, "notify", [{"type": "webhook", "url": server, "name": "hook"},
                                          {"type": "discord"}])
    assert rr.main(["notify-test"]) == rr.NOTIFY_EXIT        # the discord one has no url
    out = capsys.readouterr().out
    assert "OK    hook" in out and "FAIL  2:discord" in out and "1/2 sink(s)" in out
    assert rr.main(["notify-test", "--only", "hook"]) == 0
    assert rr.main(["notify-test", "--only", "nope"]) == 1


# ── secret CLI ───────────────────────────────────────────────────────────────
@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI round trip is Windows-only")
def test_secret_cli_roundtrip(tmp_path, monkeypatch, capsys):
    import io

    import library_session as ls
    monkeypatch.setattr(ls, "SECRETS_DIR", tmp_path)
    monkeypatch.setattr(ls, "SECRETS_BACKEND", "dpapi")
    assert ls.main(["secret", "check", "PF_TEST"]) == 2
    monkeypatch.setattr(sys, "stdin", io.StringIO("s3cret value\n"))
    assert ls.main(["secret", "set", "PF_TEST"]) == 0
    assert ls.secret_get_opt("PF_TEST") == "s3cret value"
    assert ls.main(["secret", "check", "PF_TEST"]) == 0
    assert "s3cret" not in capsys.readouterr().out
