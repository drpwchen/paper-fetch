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


# Synthetic pages shaped like what the two hosts served on 2026-11-03 (markers only — no real
# captured HTML, no tokens): PMC = HTTP 200 proof-of-work page, Europe PMC = HTTP 403 Cloudflare.
POW_PAGE = (b"\n\n<!DOCTYPE html><html><head><title>Preparing to download ...</title>"
            b"<script>window.POW_CHALLENGE='x';window.POW_COOKIE_NAME='cloudpmc-viewer-pow';"
            b"</script></head><body><main id=\"main\"></main></body></html>")
CF_PAGE = (b"<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>"
           b"<noscript><span id=\"challenge-error-text\">Enable JavaScript and cookies to "
           b"continue</span></noscript><script>window._cf_chl_opt={};</script></body></html>")


def serve_page(monkeypatch, page, status, has_pdf="Y"):
    def get(url, **kw):
        if "webservices/rest/search" in url:
            return FakeResp(json_data=epmc_flag(has_pdf))
        return FakeResp(status=status, content=page, url=url)
    monkeypatch.setattr(pf.requests, "get", get)


def test_pmc_pow_page_is_pmc_challenge(monkeypatch):
    serve_page(monkeypatch, POW_PAGE, 200)
    assert pf._grab(PMC_PDF) is None
    assert pf._BLOCKED == [("pmc_challenge", PMC_PDF)]


def test_pow_marker_counts_even_on_a_big_page():
    big = POW_PAGE + b"<!--" + b"x" * 30_000 + b"-->"
    assert pf.pmc_stub_kind(PMC_PDF, 200, big) == "pmc_challenge"


def test_europepmc_cloudflare_403_is_pmc_challenge(monkeypatch):
    serve_page(monkeypatch, CF_PAGE, 403)
    assert pf._grab(EPMC_PDF) is None
    assert pf._BLOCKED == [("pmc_challenge", EPMC_PDF)]


def test_europepmc_cloudflare_403_with_haspdf_n_is_not_a_wall(monkeypatch):
    serve_page(monkeypatch, CF_PAGE, 403, has_pdf="N")
    assert pf._grab(EPMC_PDF) is None
    assert pf._BLOCKED == []


def test_cloudflare_off_pmc_keeps_generic_kind(monkeypatch):
    serve_page(monkeypatch, CF_PAGE, 403)
    assert pf._grab("https://journal.example.org/a.pdf") is None
    assert pf._BLOCKED == [("cf_challenge", "https://journal.example.org/a.pdf")]


def test_pmc_404_is_not_a_stub():
    assert pf.pmc_stub_kind(PMC_PDF, 404, b"<html>" + b"x" * 40_000) is None


def test_pmc_recaptcha_variant_over_size_limit_is_pmc_challenge(monkeypatch):
    page = (b"<!DOCTYPE html><html><head><title>Checking your browser - reCAPTCHA</title>"
            b"<script src=\"https://www.google.com/recaptcha/api.js\"></script></head><body>"
            + b"<!--" + b"x" * 20_500 + b"--></body></html>")             # > _PMC_STUB_MAX
    serve_page(monkeypatch, page, 200)
    assert pf._grab(PMC_PDF) is None
    assert pf._BLOCKED == [("pmc_challenge", PMC_PDF)]


def test_pmc_host_challenge_with_haspdf_n_is_not_a_wall(monkeypatch):
    serve_page(monkeypatch, POW_PAGE, 200, has_pdf="N")
    assert pf._grab(PMC_PDF) is None
    assert pf._BLOCKED == []
