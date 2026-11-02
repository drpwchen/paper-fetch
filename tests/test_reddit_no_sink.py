"""reddit_request: a tracked request with no notify sink must say so loudly (stderr + status.md).

No network: state goes to a temp dir, hooks are empty, and the Reddit fetch is stubbed to 304.
Run: python -m pytest tests/test_reddit_no_sink.py -q
"""
import pathlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import reddit_request as rr  # noqa: E402


@pytest.fixture
def rr_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "STATE_DIR", tmp_path)
    monkeypatch.setattr(rr, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(rr, "EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setitem(rr._R, "on_active", [])
    monkeypatch.setitem(rr._R, "on_idle", [])
    monkeypatch.setitem(rr._R, "inbox_feed_secret", "")
    monkeypatch.setitem(rr._R, "stale_days", 0)
    monkeypatch.setattr(rr, "reddit_get",
                        lambda st, url, headers=None: types.SimpleNamespace(status_code=304, headers={}))
    return tmp_path


def _track():
    return rr.main(["track", "https://www.reddit.com/r/Scholar/comments/abc123/x/",
                    "--doi", "10.1/x", "--title", "A test article"])


@pytest.mark.parametrize("sinks", [[], None, [{"type": "jsonl"}]])
def test_no_sink_warns_on_track_poll_and_status(rr_tmp, monkeypatch, capsys, sinks):
    monkeypatch.setitem(rr._R, "notify", sinks)
    assert not rr.has_sink()
    assert _track() == 0
    assert "NO NOTIFY SINK CONFIGURED" in capsys.readouterr().err
    assert rr.main(["poll"]) == 0
    assert "NO NOTIFY SINK CONFIGURED" in capsys.readouterr().err
    md = (rr_tmp / "status.md").read_text(encoding="utf-8")
    first = md.split("\n\n")[1]          # right under the heading, above the table
    assert "NO NOTIFY SINK CONFIGURED" in first


def test_configured_sink_is_quiet(rr_tmp, monkeypatch, capsys):
    monkeypatch.setitem(rr._R, "notify", [{"type": "command", "argv": ["python", "-c", "pass"]}])
    assert rr.has_sink()
    assert _track() == 0
    assert rr.main(["poll"]) == 0
    assert "NO NOTIFY SINK" not in capsys.readouterr().err
    assert "NO NOTIFY SINK" not in (rr_tmp / "status.md").read_text(encoding="utf-8")


def test_nothing_open_does_not_warn(rr_tmp, monkeypatch, capsys):
    monkeypatch.setitem(rr._R, "notify", [])
    assert rr.main(["poll"]) == 0
    assert "NO NOTIFY SINK" not in capsys.readouterr().err
