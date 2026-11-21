"""reddit_request dedupe / poll after Reddit switched RSS off (2026-11-13).

`dedupe` must never report "nobody asked" when it could not look: 404 / 403 / a non-feed 200 /
a network error / a failing dedupe_cmd all raise DedupeUnavailable (CLI: exit 10,
"existing": null, a manual search URL). 429 stays RateLimited (exit 7). `poll` on an
unreadable feed exits 10, emits `poll_blind` once, and `stale_days` does not close a thread it
never read.

No network: state goes to a temp dir and every Reddit call is stubbed.
Run: python -m pytest tests/test_reddit_dedupe.py -q
"""
import json
import pathlib
import sys
import time
import types

import pytest
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import reddit_request as rr  # noqa: E402

DOI = "10.1038/nrd3988"
FEED_HIT = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <entry><id>t3_abc123</id><author><name>/u/someone</name></author>
  <title>[Article] Metabolic disorders</title>
  <link href="https://www.reddit.com/r/Scholar/comments/abc123/x/"/>
  <content type="html">&lt;p&gt;DOI: {DOI}&lt;/p&gt;</content></entry>
 <entry><id>t3_zzz999</id><author><name>/u/other</name></author>
  <title>[Article] Something else</title>
  <link href="https://www.reddit.com/r/Scholar/comments/zzz999/y/"/>
  <content type="html">10.1000/unrelated</content></entry>
</feed>"""
FEED_404 = ('<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">'
            '<title>Not Found</title></feed>')


def _resp(status, text="", headers=None):
    return types.SimpleNamespace(status_code=status, text=text, headers=headers or {})


@pytest.fixture
def rr_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "STATE_DIR", tmp_path)
    monkeypatch.setattr(rr, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(rr, "EVENTS_FILE", tmp_path / "events.jsonl")
    for k, v in (("on_active", []), ("on_idle", []), ("inbox_feed_secret", ""),
                 ("notify", []), ("dedupe_cmd", []), ("stale_days", 21)):
        monkeypatch.setitem(rr._R, k, v)
    return tmp_path


def _stub_get(monkeypatch, resp):
    calls = []

    def fake(st, url, headers=None):
        calls.append(url)
        if isinstance(resp, Exception):
            raise resp
        return resp
    monkeypatch.setattr(rr, "reddit_get", fake)
    return calls


# ── dedupe() ──────────────────────────────────────────────────────────────────
def test_200_feed_returns_only_matching_posts(rr_tmp, monkeypatch):
    _stub_get(monkeypatch, _resp(200, FEED_HIT))
    hits = rr.dedupe(rr.load_state(), DOI)
    assert [h["id"] for h in hits] == ["abc123"]


def test_200_feed_with_no_match_is_an_empty_list(rr_tmp, monkeypatch):
    _stub_get(monkeypatch, _resp(200, FEED_HIT))
    assert rr.dedupe(rr.load_state(), "10.9999/nobody") == []


@pytest.mark.parametrize("resp", [_resp(404, FEED_404), _resp(403, "<html>"), _resp(500, ""),
                                  _resp(200, "<html>not a feed</html>"),
                                  requests.ConnectionError("boom")])
def test_could_not_look_is_unavailable_not_empty(rr_tmp, monkeypatch, resp):
    _stub_get(monkeypatch, resp)
    with pytest.raises(rr.DedupeUnavailable) as e:
        rr.dedupe(rr.load_state(), DOI)
    assert e.value.manual_url.startswith("https://www.reddit.com/r/Scholar/search/?q=%2210.1038%2Fnrd3988%22")


def test_404_names_the_rss_shutdown(rr_tmp, monkeypatch):
    _stub_get(monkeypatch, _resp(404, FEED_404))
    with pytest.raises(rr.DedupeUnavailable, match="RSS off"):
        rr.dedupe(rr.load_state(), DOI)


def test_429_stays_rate_limited(rr_tmp, monkeypatch):
    _stub_get(monkeypatch, rr.RateLimited("HTTP 429"))
    with pytest.raises(rr.RateLimited):
        rr.dedupe(rr.load_state(), DOI)


def test_real_reddit_get_429_records_backoff_in_tmp_state(rr_tmp, monkeypatch):
    monkeypatch.setattr(rr.requests, "get",
                        lambda *a, **k: _resp(429, "", {"retry-after": "60", "x-ratelimit-remaining": "0"}))
    st = rr.load_state()
    with pytest.raises(rr.RateLimited):
        rr.dedupe(st, DOI)
    saved = json.loads((rr_tmp / "state.json").read_text(encoding="utf-8"))
    assert saved["backoff_until"] > time.time()


# ── dedupe_cmd hook ──────────────────────────────────────────────────────────
def _cmd(monkeypatch, code):
    monkeypatch.setitem(rr._R, "dedupe_cmd", [sys.executable, "-c", code, "{doi}"])


def test_dedupe_cmd_is_used_and_gets_the_doi(rr_tmp, monkeypatch):
    calls = _stub_get(monkeypatch, _resp(500))
    _cmd(monkeypatch, "import sys, json; print('noise'); "
                      "print(json.dumps({'existing': [{'id': 'x1', 'url': 'u', 'author': 'a', "
                      "'title': sys.argv[1]}]}))")
    hits = rr.dedupe(rr.load_state(), DOI)
    assert hits[0]["title"] == DOI and not calls          # no RSS request when the hook is set


def test_dedupe_cmd_empty_list_is_looked_nobody_asked(rr_tmp, monkeypatch):
    _cmd(monkeypatch, "print('{\"existing\": []}')")
    assert rr.dedupe(rr.load_state(), DOI) == []


@pytest.mark.parametrize("code", ["import sys; print('{\"existing\": []}'); sys.exit(3)",
                                  "print('no json')", "print('{\"existing\": null}')"])
def test_dedupe_cmd_failure_is_unavailable(rr_tmp, monkeypatch, code):
    _cmd(monkeypatch, code)
    with pytest.raises(rr.DedupeUnavailable):
        rr.dedupe(rr.load_state(), DOI)


# ── CLI ──────────────────────────────────────────────────────────────────────
def test_cli_unavailable_exit_10_existing_null(rr_tmp, monkeypatch, capsys):
    _stub_get(monkeypatch, _resp(404, FEED_404))
    assert rr.main(["dedupe", DOI]) == 10
    out = capsys.readouterr()
    j = json.loads(out.out)
    assert j["existing"] is None and "search/?q=" in j["manual_url"]
    assert "check by hand" in out.err


def test_cli_rate_limited_exit_7_existing_null(rr_tmp, monkeypatch, capsys):
    _stub_get(monkeypatch, rr.RateLimited("HTTP 429"))
    assert rr.main(["dedupe", DOI]) == 7
    assert json.loads(capsys.readouterr().out)["existing"] is None


def test_cli_ok_exit_0(rr_tmp, monkeypatch, capsys):
    _stub_get(monkeypatch, _resp(200, FEED_HIT))
    assert rr.main(["dedupe", DOI]) == 0
    assert json.loads(capsys.readouterr().out)["existing"][0]["id"] == "abc123"


# ── poll on a dead feed ──────────────────────────────────────────────────────
def _track(rr_tmp, posted_at=None):
    assert rr.main(["track", "https://www.reddit.com/r/Scholar/comments/abc123/x/",
                    "--doi", DOI, "--title", "A test article"]) == 0
    if posted_at:
        st = rr.load_state()
        st["posts"]["abc123"]["posted_at"] = posted_at
        rr.save_state(st)


def test_poll_blind_exits_10_and_notifies_once(rr_tmp, monkeypatch, capsys):
    _track(rr_tmp)
    _stub_get(monkeypatch, _resp(404, FEED_404))
    assert rr.main(["poll"]) == 10
    assert "BLIND" in capsys.readouterr().err
    assert rr.main(["poll"]) == 10
    evs = [json.loads(x)["event"] for x in (rr_tmp / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert evs.count("poll_blind") == 1
    assert rr.load_state()["posts"]["abc123"]["poll_http"] == 404


def test_stale_does_not_close_a_thread_it_could_not_read(rr_tmp, monkeypatch):
    _track(rr_tmp, posted_at="2026-01-01T00:00:00+00:00")
    _stub_get(monkeypatch, _resp(404, FEED_404))
    rr.main(["poll"])
    rr.main(["poll"])
    assert rr.load_state()["posts"]["abc123"]["status"] == "posted"


def test_stale_still_closes_a_thread_read_with_no_reply(rr_tmp, monkeypatch):
    _track(rr_tmp, posted_at="2026-01-01T00:00:00+00:00")
    _stub_get(monkeypatch, _resp(304))
    assert rr.main(["poll"]) == 0                 # first poll reads it (304)
    rr.main(["poll"])                             # next run: read, old, no reply → closed
    assert rr.load_state()["posts"]["abc123"]["status"] == "closed"


def test_feed_back_clears_blind_flag(rr_tmp, monkeypatch):
    _track(rr_tmp)
    _stub_get(monkeypatch, _resp(404, FEED_404))
    rr.main(["poll"])
    _stub_get(monkeypatch, _resp(304))
    assert rr.main(["poll"]) == 0
    assert "blind_notified" not in rr.load_state()["posts"]["abc123"]
