"""reddit_request: a reply must reach a person even if no sink could deliver it when it arrived.

Reproduces the 2026-10-25 case: two r/Scholar requests were answered with a file-host link,
`poll` logged `fulfilled`, but the only sink then was a phone push; the sink that reaches the
orchestrator was added a week later and never heard about those replies. Also: a sink that
failed at reply time used to lose that event for good (only a ⚠️ in status.md).

No network: the Reddit feed is a canned Atom document, sinks are local commands.
Run: python -m pytest tests/test_reddit_catchup.py -q
"""
import json
import pathlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import reddit_request as rr  # noqa: E402

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <entry><id>t3_abc123</id><author><name>/u/me_op</name></author>
  <title>[Article] A test article (J Test, 2024)</title><published>2026-10-24T11:16:00+00:00</published>
  <link href="https://www.reddit.com/r/Scholar/comments/abc123/x/"/>
  <content type="html">&lt;a href="https://doi.org/10.1/x"&gt;doi&lt;/a&gt;</content></entry>
 <entry><id>t1_c1</id><author><name>/u/helper</name></author><title>/u/helper on x</title>
  <link href="https://www.reddit.com/r/Scholar/comments/abc123/x/c1/"/>
  <content type="html">here &lt;a href="https://www.mediafire.com/file/zzz/a.pdf/file"&gt;pdf&lt;/a&gt;</content></entry>
</feed>"""


@pytest.fixture
def rr_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "STATE_DIR", tmp_path)
    monkeypatch.setattr(rr, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(rr, "EVENTS_FILE", tmp_path / "events.jsonl")
    for k, v in (("on_active", []), ("on_idle", []), ("inbox_feed_secret", ""), ("stale_days", 0),
                 ("notify", [])):
        monkeypatch.setitem(rr._R, k, v)
    feed = {"status": 200}
    monkeypatch.setattr(rr, "reddit_get", lambda st, url, headers=None: types.SimpleNamespace(
        status_code=feed["status"], headers={}, text=FEED))
    return tmp_path, feed


def _sink(tmp_path, name="orchestrator", fail=False):
    """A command sink that appends each event to <name>.jsonl (or exits 1 when fail=True)."""
    out = tmp_path / f"{name}.jsonl"
    code = ("import sys; sys.exit(1)" if fail else
            f"import sys; open(r'{out}', 'a', encoding='utf-8').write(sys.stdin.read() + '\\n')")
    return {"type": "command", "argv": [sys.executable, "-c", code]}, out


def _got(out):
    if not out.exists():
        return []
    return [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines() if x.strip()]


def _track(tmp_path):
    return rr.main(["track", "https://www.reddit.com/r/Scholar/comments/abc123/x/",
                    "--doi", "10.1/x", "--title", "A test article", "--record-id", "CDX-1",
                    "--dest", str(tmp_path / "pdfs" / "x.pdf")])


def test_sink_added_after_reply_gets_it_on_next_poll(rr_tmp, monkeypatch):
    tmp, feed = rr_tmp
    assert _track(tmp) == 0
    assert rr.main(["poll"]) == 0                      # reply ingested, nobody to tell
    st = rr.load_state()
    assert st["posts"]["abc123"]["status"] == "fulfilled"

    sink, out = _sink(tmp)
    monkeypatch.setitem(rr._R, "notify", [sink])        # the orchestrator sink, a week later
    feed["status"] = 304                                # nothing new on Reddit
    assert rr.main(["poll"]) == 0
    evs = _got(out)
    assert [e["event"] for e in evs] == ["fulfilled"]
    ev = evs[0]
    assert ev["catch_up"] is True
    assert ev["doi"] == "10.1/x" and ev["record_id"] == "CDX-1"
    assert ev["link_hosts"] == ["mediafire.com"]
    assert ev["reply_urls"] == ["https://www.reddit.com/r/Scholar/comments/abc123/x/c1/"]
    assert ev["dest"].endswith("x.pdf") and "verify abc123" in ev["action"]
    assert "mediafire.com/file" not in json.dumps(ev)  # the offered link itself never travels

    assert rr.main(["poll"]) == 0                       # delivered once, not every poll
    assert len(_got(out)) == 1


def test_failed_delivery_is_retried(rr_tmp, monkeypatch):
    tmp, feed = rr_tmp
    bad, _ = _sink(tmp, fail=True)
    monkeypatch.setitem(rr._R, "notify", [bad])
    assert _track(tmp) == 0
    assert rr.main(["poll"]) == 0                       # reply arrives, sink fails
    good, out = _sink(tmp, name="fixed")
    good["name"] = "orchestrator"
    bad["name"] = "orchestrator"                        # same sink, now fixed
    monkeypatch.setitem(rr._R, "notify", [good])
    feed["status"] = 304
    assert rr.main(["poll"]) == 0
    assert [e["event"] for e in _got(out)] == ["fulfilled"]


def test_on_time_delivery_is_not_repeated(rr_tmp, monkeypatch):
    tmp, feed = rr_tmp
    sink, out = _sink(tmp)
    monkeypatch.setitem(rr._R, "notify", [sink])
    assert _track(tmp) == 0
    assert rr.main(["poll"]) == 0
    feed["status"] = 304
    assert rr.main(["poll"]) == 0
    evs = _got(out)
    assert [e["event"] for e in evs] == ["fulfilled"] and not evs[0].get("catch_up")


def test_notify_test_delivers_missed_replies(rr_tmp, monkeypatch, capsys):
    tmp, _ = rr_tmp
    assert _track(tmp) == 0
    assert rr.main(["poll"]) == 0
    sink, out = _sink(tmp)
    monkeypatch.setitem(rr._R, "notify", [sink])
    assert rr.main(["notify-test"]) == 0
    assert [e["event"] for e in _got(out)] == ["test", "fulfilled"]
    assert "delivered late" in capsys.readouterr().out


def test_verified_request_is_not_caught_up(rr_tmp, monkeypatch):
    tmp, feed = rr_tmp
    assert _track(tmp) == 0
    assert rr.main(["poll"]) == 0
    assert rr.main(["verify", "abc123", "--manual"]) == 0     # already got the PDF
    sink, out = _sink(tmp)
    monkeypatch.setitem(rr._R, "notify", [sink])
    assert rr.main(["poll"]) == 0
    assert _got(out) == []


def test_render_says_what_to_do(rr_tmp):
    rec = {"id": "abc123", "url": "u", "title": "T", "doi": "10.1/x", "record_id": "CDX-1",
           "status": "fulfilled", "comments": 1, "dest": "/pdfs/x.pdf",
           "links": [{"comment": "https://www.reddit.com/c1/", "hosts": ["mediafire.com"]}]}
    title, body = rr._render(rr._event("fulfilled", rec, catch_up=True))
    assert "delivered late" in title
    for want in ("record CDX-1", "mediafire.com", "reply: https://www.reddit.com/c1/",
                 "save to: /pdfs/x.pdf", "to do: Open the offered link yourself"):
        assert want in body
