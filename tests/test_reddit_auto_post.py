"""reddit.auto_post: off by default, carried on every draft so a posting integration can check it.

This repo never posts; the field is the user's standing permission for an agent that was
fetching the paper for them. No network: metadata and the OA check are stubbed.
Run: python -m pytest tests/test_reddit_auto_post.py -q
"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import reddit_request as rr  # noqa: E402

META = {"title": "A test article", "first_author": "Someone", "n_authors": 2,
        "journal": "J Test", "year": 2021, "doi": "10.1/x", "pmid": "1"}


@pytest.fixture
def stubbed(tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "STATE_DIR", tmp_path)
    monkeypatch.setattr(rr, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(rr, "EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setitem(rr._R, "auto_post", False)
    monkeypatch.setattr(rr, "metadata", lambda ident: dict(META))
    monkeypatch.setattr(rr, "oa_status", lambda doi: {"is_oa": False, "checked": True})
    return tmp_path


def test_default_is_off_everywhere(stubbed, capsys):
    assert rr.build_draft(META)["auto_post"] is False
    assert rr.draft_for_envelope("10.1/x")["auto_post"] is False
    assert rr.main(["draft", "10.1/x", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["auto_post"] is False


def test_on_when_configured(stubbed, monkeypatch):
    monkeypatch.setitem(rr._R, "auto_post", True)
    assert rr.draft_for_envelope("10.1/x")["auto_post"] is True
    rr.main(["draft", "10.1/x", "--json"])
    assert rr.load_state()["drafts"]["10.1/x"]["auto_post"] is True


def test_refused_envelope_has_nothing_to_post(stubbed, monkeypatch):
    monkeypatch.setitem(rr._R, "auto_post", True)
    monkeypatch.setattr(rr, "oa_status", lambda doi: {"is_oa": True, "url": "https://oa.example/x", "checked": True})
    d = rr.draft_for_envelope("10.1/x")
    assert d["refused"] == "open_access" and "title" not in d and "auto_post" not in d
