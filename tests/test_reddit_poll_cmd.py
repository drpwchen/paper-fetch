"""reddit_request poll through reddit.poll_cmd (Reddit switched RSS off on 2026-11-13).

The hook prints /comments/<id>.json; `parse_listing` turns it into the same entries the feed
gave, so `_ingest` decides replied / fulfilled / verified_by_op exactly as before. A failing
hook, an HTML page, or a different thread is blind (exit 10, one `poll_blind`), never "no new
comments", and never marks the thread as read for `stale_days`.

The fixture has the shape of a real thread (2026-11-21) with made-up names and links.
No network: state goes to a temp dir; the hook is a tiny python -c script.
Run: python -m pytest tests/test_reddit_poll_cmd.py -q
"""
import html
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import reddit_request as rr  # noqa: E402

PID = "abc123"
URL = f"https://www.reddit.com/r/Scholar/comments/{PID}/x/"


def _c(cid, author, body_html, utc=1792934324.0, replies=""):
    return {"kind": "t1", "data": {
        "name": f"t1_{cid}", "id": cid, "author": author, "created_utc": utc,
        "permalink": f"/r/Scholar/comments/{PID}/x/{cid}/", "body": "md",
        "body_html": body_html, "replies": replies}}


def listing(comments, pid=PID, op="poster1"):
    return [{"kind": "Listing", "data": {"children": [{"kind": "t3", "data": {
                "name": f"t3_{pid}", "id": pid, "author": op, "created_utc": 1792840612.0,
                "title": "[Article] A test article + Someone (J Test, 2021)",
                "permalink": f"/r/Scholar/comments/{pid}/x/", "selftext_html": "<p>DOI</p>"}}]}},
            {"kind": "Listing", "data": {"children": comments}}]


AUTOMOD = _c("am1", "AutoModerator", '<div class="md"><p>Dear <a href="/u/poster1">/u/poster1</a>, '
                                      'reply "solution verified"</p></div>', utc=1792840613.0)
LINK = _c("ff1", "helper9", '<div class="md"><p><a href="https://files.example.com/f/1.pdf">'
                            'https://files.example.com/f/1.pdf</a></p></div>')
THANKS = _c("op1", "poster1", '<div class="md"><p>thanks solution verified</p></div>', utc=1794701324.0)
CHAT = _c("q1", "asker2", '<div class="md"><p>did you get it? see <a href="/r/Scholar/wiki">wiki</a></p></div>')


@pytest.fixture
def rr_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "STATE_DIR", tmp_path)
    monkeypatch.setattr(rr, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(rr, "EVENTS_FILE", tmp_path / "events.jsonl")
    for k, v in (("on_active", []), ("on_idle", []), ("inbox_feed_secret", ""),
                 ("notify", []), ("dedupe_cmd", []), ("poll_cmd", []), ("stale_days", 21)):
        monkeypatch.setitem(rr._R, k, v)
    monkeypatch.setattr(rr.time, "sleep", lambda s: None)
    monkeypatch.setattr(rr, "reddit_get", lambda *a, **k: pytest.fail("RSS must not be read"))
    assert rr.main(["track", URL, "--doi", "10.1/x", "--title", "A test article"]) == 0
    return tmp_path


def hook(monkeypatch, tmp_path, payload=None, code=None):
    """poll_cmd = python -c that prints `payload` (JSON) and records the id it was given."""
    if code is None:
        f = tmp_path / "payload.json"
        f.write_text(json.dumps(payload), encoding="utf-8")
        code = (f"import sys; open({str(tmp_path / 'argv.txt')!r}, 'a').write(sys.argv[1] + '\\n'); "
                f"sys.stdout.write(open({str(f)!r}, encoding='utf-8').read())")
    monkeypatch.setitem(rr._R, "poll_cmd", [sys.executable, "-c", code, "{id}"])


def events(tmp_path):
    p = tmp_path / "events.jsonl"
    return [json.loads(x)["event"] for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


# ── parse_listing ────────────────────────────────────────────────────────────
def test_parse_listing_matches_feed_entry_shape():
    es = rr.parse_listing(listing([AUTOMOD, LINK]))
    assert [e["id"] for e in es] == [f"t3_{PID}", "t1_am1", "t1_ff1"]
    post, _, ff = es
    assert post["author"] == "poster1" and post["title"].startswith("[Article]")
    assert ff["published"] == "2026-10-25 13:18"                     # UTC, like the feed's
    assert ff["link"] == f"https://www.reddit.com/r/Scholar/comments/{PID}/x/ff1/"
    assert ff["external"] == ["https://files.example.com/f/1.pdf"]
    assert es[1]["external"] == []                                    # /u/ link is Reddit's own
    assert set(ff) == {"id", "author", "title", "published", "link", "text", "external"}


def test_parse_listing_escaped_body_html_too():
    esc = _c("ff2", "helper9", html.escape('<div class="md"><p><a href="https://h.example.org/a?b=1&amp;c=2">x</a></p></div>'))
    (e,) = [x for x in rr.parse_listing(listing([esc])) if x["id"] == "t1_ff2"]
    assert e["external"] == ["https://h.example.org/a?b=1&c=2"]


def test_parse_listing_flattens_replies_and_skips_more():
    nested = _c("q1", "asker2", "<p>q</p>", replies={"kind": "Listing", "data": {"children": [
        THANKS, {"kind": "more", "data": {"count": 3, "children": ["z1", "z2"]}}]}})
    ids = [e["id"] for e in rr.parse_listing(listing([nested, {"kind": "more", "data": {}}]))]
    assert ids == [f"t3_{PID}", "t1_q1", "t1_op1"]


@pytest.mark.parametrize("bad", [{"kind": "Listing"}, [], [{"kind": "Listing", "data": {}}],
                                 ["<html>", "x"]])
def test_parse_listing_rejects_other_shapes(bad):
    with pytest.raises(ValueError):
        rr.parse_listing(bad)


# ── poll through the hook ────────────────────────────────────────────────────
def test_poll_cmd_link_is_fulfilled_and_gets_the_id(rr_tmp, monkeypatch, capsys):
    hook(monkeypatch, rr_tmp, listing([AUTOMOD, LINK]))
    assert rr.main(["poll"]) == 0
    assert (rr_tmp / "argv.txt").read_text().split() == [PID]
    rec = rr.load_state()["posts"][PID]
    assert rec["status"] == "fulfilled" and rec["poll_http"] == 200
    assert rec["links"][0]["author"] == "helper9" and rec["links"][0]["hosts"] == ["files.example.com"]
    assert rec["op"] == "poster1"
    assert events(rr_tmp).count("fulfilled") == 1


def test_poll_cmd_seen_comment_gives_no_second_event(rr_tmp, monkeypatch):
    hook(monkeypatch, rr_tmp, listing([AUTOMOD, LINK]))
    rr.main(["poll"])
    assert rr.main(["poll"]) == 0
    assert events(rr_tmp).count("fulfilled") == 1


def test_poll_cmd_plain_reply_is_replied(rr_tmp, monkeypatch):
    hook(monkeypatch, rr_tmp, listing([AUTOMOD, CHAT]))
    rr.main(["poll"])
    assert rr.load_state()["posts"][PID]["status"] == "replied"


def test_poll_cmd_op_thanks_is_verified_by_op(rr_tmp, monkeypatch):
    hook(monkeypatch, rr_tmp, listing([AUTOMOD, LINK, THANKS]))
    rr.main(["poll"])
    rec = rr.load_state()["posts"][PID]
    assert rec["status"] == "verified" and rec["thanked"] == "t1_op1"
    assert "verified_by_op" in events(rr_tmp)


def test_poll_cmd_clears_blind_flag(rr_tmp, monkeypatch):
    hook(monkeypatch, rr_tmp, code="import sys; sys.exit(10)")
    assert rr.main(["poll"]) == 10
    assert rr.load_state()["posts"][PID]["blind_notified"]
    hook(monkeypatch, rr_tmp, listing([AUTOMOD]))
    assert rr.main(["poll"]) == 0
    assert "blind_notified" not in rr.load_state()["posts"][PID]


@pytest.mark.parametrize("code", [
    "import sys; sys.exit(10)",                                   # Edge down / logged out
    "print('<html><body>blocked</body></html>')",                 # a 403 page passed through
    "print('{\"kind\": \"Listing\"}')",                           # JSON, wrong shape
])
def test_poll_cmd_failure_is_blind_once_not_read(rr_tmp, monkeypatch, capsys, code):
    hook(monkeypatch, rr_tmp, code=code)
    assert rr.main(["poll"]) == 10
    assert "BLIND" in capsys.readouterr().err
    assert rr.main(["poll"]) == 10
    assert events(rr_tmp).count("poll_blind") == 1
    rec = rr.load_state()["posts"][PID]
    assert rec["poll_http"] == "cmd_failed" and rec["status"] == "posted"


def test_poll_cmd_other_thread_is_blind(rr_tmp, monkeypatch, capsys):
    hook(monkeypatch, rr_tmp, listing([AUTOMOD, LINK], pid="zzz999"))
    assert rr.main(["poll"]) == 10
    assert "different thread" in capsys.readouterr().err
    assert rr.load_state()["posts"][PID]["status"] == "posted"


def test_stale_waits_for_a_read_then_closes(rr_tmp, monkeypatch):
    st = rr.load_state()
    st["posts"][PID]["posted_at"] = "2026-01-01T00:00:00+00:00"
    rr.save_state(st)
    hook(monkeypatch, rr_tmp, code="import sys; sys.exit(10)")
    rr.main(["poll"])
    rr.main(["poll"])
    assert rr.load_state()["posts"][PID]["status"] == "posted"     # never read → kept open
    hook(monkeypatch, rr_tmp, listing([AUTOMOD]))
    rr.main(["poll"])                                                # read: nobody but AutoMod
    rr.main(["poll"])
    assert rr.load_state()["posts"][PID]["status"] == "closed"
