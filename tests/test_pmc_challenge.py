"""Issue #7 point 2: PMC / Europe PMC answering a PDF URL with a small JS-challenge HTML page is
a bot wall (exit 7, `blocked_by` kind `pmc_challenge`), not "no PDF". No network."""
import importlib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
pf = importlib.import_module("paper" + "_fetch")

STUB = (b"<!DOCTYPE html><html><head><title>PMC</title><script>"
        + b"var c=1;" * 150 + b"</script></head><body></body></html>")       # ~1.8 KB
PMC_PDF = "https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/pdf/"
EPMC_PDF = "https://europepmc.org/articles/PMC1234567?pdf=render"


class FakeResp:
    def __init__(self, status=200, content=b"", url=None, json_data=None):
        self.status_code = status
        self.content, self.url, self._json = content, url, json_data
        self.headers = {"content-type": "text/html"}

    def json(self):
        return self._json


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(pf, "_BLOCKED", [])
    monkeypatch.setattr(pf, "_LAST_REJECT", None)


def epmc_flag(flag):
    return {"resultList": {"result": [{"pmcid": "PMC1234567", "hasPDF": flag}]}}


def serve(monkeypatch, has_pdf="Y"):
    def get(url, **kw):
        if "webservices/rest/search" in url:
            return FakeResp(json_data=epmc_flag(has_pdf))
        return FakeResp(content=STUB, url=url)
    monkeypatch.setattr(pf.requests, "get", get)


@pytest.mark.parametrize("url", [PMC_PDF, EPMC_PDF])
def test_pmc_stub_is_a_wall(monkeypatch, url):
    serve(monkeypatch)
    assert pf._grab(url) is None
    assert pf._BLOCKED == [("pmc_challenge", url)]


def test_europepmc_haspdf_n_is_honest_no_pdf(monkeypatch):
    serve(monkeypatch, has_pdf="N")
    assert pf._grab(EPMC_PDF) is None
    assert pf._BLOCKED == []


def test_non_pmc_small_html_is_not_classified(monkeypatch):
    serve(monkeypatch)
    assert pf._grab("https://repo.example.edu/paper.pdf") is None
    assert pf._BLOCKED == []


def test_big_pmc_html_is_not_a_stub():
    assert pf.pmc_stub_kind(PMC_PDF, 200, b"<html>" + b"x" * 50_000) is None


def test_walls_only_exit_7_and_message(monkeypatch, capsys):
    monkeypatch.setattr(pf, "routes_for", lambda doi: [lambda d: pf._BLOCKED.append(
        ("pmc_challenge", EPMC_PDF))])
    monkeypatch.setattr(pf, "SFX_BASE", None)
    env = pf.run_ladder("10.1/x", None)
    assert env["blocked"] is True and pf.exit_code(env) == 7
    assert env["blocked_by"] == [{"kind": "pmc_challenge", "url": EPMC_PDF}]
    out = capsys.readouterr().out
    assert "exit 7" in out and EPMC_PDF in out


def test_no_wall_stays_exit_2(monkeypatch):
    monkeypatch.setattr(pf, "routes_for", lambda doi: [lambda d: None])
    monkeypatch.setattr(pf, "SFX_BASE", None)
    env = pf.run_ladder("10.1/x", None)
    assert env["blocked"] is False and pf.exit_code(env) == 2


def test_content_reject_beats_wall(monkeypatch):
    def route(d):
        pf._BLOCKED.append(("pmc_challenge", EPMC_PDF))
        pf._LAST_REJECT = (b"%PDF-cover", "1-page cover")
    monkeypatch.setattr(pf, "routes_for", lambda doi: [route])
    monkeypatch.setattr(pf, "SFX_BASE", None)
    env = pf.run_ladder("10.1/x", None)
    assert pf.exit_code(env) == 2
