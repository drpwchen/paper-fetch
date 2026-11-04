"""1.9.0: a preprint / accepted manuscript is labelled, not passed off as the version of record.

Shape of the case that prompted it (smoke DOI 10.1186/s12984-023-01168-x, 2026-11-04): the
published PDF sits on PMC and Europe PMC, both walled; the ladder lands on the Research Square
copy, which Unpaywall calls `acceptedVersion`. No network."""
import importlib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
pf = importlib.import_module("paper" + "_fetch")

PDF = b"%PDF-1.7\n" + b"x" * 5000
DOI = "10.1186/s12984-023-01168-x"
RS_PDF = "https://www.researchsquare.com/article/rs-1592253/latest.pdf"
RS_DOI = "https://doi.org/10.21203/rs.3.rs-1592253/v1"
PMC_PDF = "https://pmc.ncbi.nlm.nih.gov/articles/PMC10148551/pdf/12984_2023_Article_1168.pdf"
EPMC_PDF = "https://europepmc.org/articles/PMC10148551?pdf=render"
BMC_PDF = "https://jneuroengrehab.biomedcentral.com/counter/pdf/10.1186/s12984-023-01168-x"


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    for name, val in (("_BLOCKED", []), ("_LAST_REJECT", None), ("_SOURCE", None),
                      ("_LAST_GRAB_URL", None), ("_OA_CLAIMED", False),
                      ("_FREE_AT_PUBLISHER", None), ("SFX_BASE", None)):
        monkeypatch.setattr(pf, name, val)


# --- classification ------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    (RS_PDF, "researchsquare.com"),
    (RS_DOI, "doi:10.21203"),
    ("https://www.medrxiv.org/content/10.1101/2021.01.01.21249000v1.full.pdf", "medrxiv.org"),
    ("https://arxiv.org/pdf/2101.00001", "arxiv.org"),
    ("https://papers.ssrn.com/sol3/Delivery.cfm?abstractid=1", "ssrn.com"),
    ("https://doi.org/10.1101/2020.03.05.20031815", "doi:10.1101"),
    (PMC_PDF, None),
    (BMC_PDF, None),
    ("https://doi.org/10.1101/gr.123456.111", None),     # Genome Research, not bioRxiv
    ("https://notarxiv.org.example.com/a.pdf", None),
    (None, None),
])
def test_preprint_host(url, expected):
    assert pf.preprint_host(url) == expected


def test_preprint_host_beats_unpaywall_accepted():
    # Unpaywall really does call the Research Square copy acceptedVersion.
    assert pf.classify_version([RS_PDF], "acceptedVersion") == ("preprint", "host:researchsquare.com")


@pytest.mark.parametrize("upw,expected", [
    ("publishedVersion", "published"), ("acceptedVersion", "accepted"),
    ("submittedVersion", "preprint"), (None, None), ("weird", None),
])
def test_unpaywall_version_mapping(upw, expected):
    assert pf.classify_version(["https://repo.example.edu/a.pdf"], upw)[0] == expected


def test_publisher_routes_are_vor_unknown_oa_is_unknown():
    assert pf.version_of("springer") == ("published", "route:springer")
    assert pf.version_of("pubmed_free", None, "https://journal.example.org/a.pdf")[0] == "published"
    assert pf.version_of("unpaywall", (EPMC_PDF, None)) == (None, None)
    assert pf.version_of("unpaywall", (PMC_PDF, "publishedVersion"))[0] == "published"


# --- run_ladder: labelling ------------------------------------------------------------------

def fake_oa(url, upw, walls=()):
    def route_unpaywall(doi):
        pf._BLOCKED.extend(walls)
        pf._SOURCE, pf._LAST_GRAB_URL = (url, upw), url
        return PDF
    return route_unpaywall


def test_preprint_is_renamed_and_labelled(monkeypatch, tmp_path, capsys):
    walls = [("pmc_challenge", PMC_PDF), ("pmc_challenge", EPMC_PDF),
             ("js_challenge", RS_DOI)]          # a walled preprint copy is not a VoR lead
    monkeypatch.setattr(pf, "routes_for", lambda d: [fake_oa(RS_PDF, "acceptedVersion", walls)])
    out = tmp_path / "paper.pdf"
    env = pf.run_ladder(DOI, out)
    labeled = tmp_path / "paper.preprint.pdf"
    assert not out.exists() and labeled.read_bytes() == PDF
    assert env["ok"] and pf.exit_code(env) == 0
    assert env["path"] == str(labeled)
    assert env["version"] == "preprint" and env["version_basis"] == "host:researchsquare.com"
    assert env["source_url"] == RS_PDF
    assert env["vor_url"] == f"https://doi.org/{DOI}"
    assert env["vor_blocked"] == [{"kind": "pmc_challenge", "url": PMC_PDF},
                                  {"kind": "pmc_challenge", "url": EPMC_PDF}]
    assert env["zotero_attachment_title"].startswith("Preprint PDF")
    log = capsys.readouterr().out
    assert "預印本" in log and PMC_PDF in log and "paper.preprint.pdf" in log


def test_accepted_manuscript_gets_its_own_label(monkeypatch, tmp_path):
    src = "https://repo.example.edu/aam.pdf"
    monkeypatch.setattr(pf, "routes_for", lambda d: [fake_oa(src, "acceptedVersion")])
    env = pf.run_ladder(DOI, tmp_path / "p.pdf")
    assert env["version"] == "accepted" and pf.exit_code(env) == 0
    assert env["path"] == str(tmp_path / "p.accepted.pdf")
    assert env["zotero_attachment_title"].startswith("Accepted Manuscript PDF")
    assert env["vor_blocked"] == []


def test_vor_download_is_unchanged(monkeypatch, tmp_path, capsys):
    def route_springer(doi):
        return PDF
    monkeypatch.setattr(pf, "routes_for", lambda d: [route_springer])
    out = tmp_path / "p.pdf"
    env = pf.run_ladder(DOI, out)
    assert out.read_bytes() == PDF and env["path"] == str(out)
    assert env["version"] == "published"
    assert not {"vor_url", "vor_blocked", "zotero_attachment_title"} & env.keys()
    assert "預印本" not in capsys.readouterr().out


def test_unknown_version_keeps_requested_name(monkeypatch, tmp_path):
    monkeypatch.setattr(pf, "routes_for", lambda d: [fake_oa(EPMC_PDF, None)])
    out = tmp_path / "p.pdf"
    env = pf.run_ladder(DOI, out)
    assert out.exists() and env["version"] is None and "vor_url" not in env


def test_asking_for_a_preprint_doi_is_not_a_warning(monkeypatch, tmp_path):
    monkeypatch.setattr(pf, "routes_for", lambda d: [fake_oa(RS_PDF, "submittedVersion")])
    out = tmp_path / "p.pdf"
    env = pf.run_ladder("10.21203/rs.3.rs-1592253/v1", out)
    assert out.exists() and env["version"] == "preprint" and "vor_url" not in env


def test_no_output_path_still_labels_envelope(monkeypatch):
    monkeypatch.setattr(pf, "routes_for", lambda d: [fake_oa(RS_PDF, None)])
    env = pf.run_ladder(DOI, None)
    assert env["version"] == "preprint" and env["path"] is None and env["vor_url"]


# --- route_unpaywall end to end (mocked HTTP): which candidate won decides the label --------

class Resp:
    def __init__(self, status=200, content=b"", url=None, js=None):
        self.status_code, self.content, self.url, self._js = status, content, url, js
        self.headers = {"content-type": "text/html"}
        self.text = content.decode("latin-1")

    def json(self):
        return self._js


UNPAYWALL = {"is_oa": True, "best_oa_location": None, "oa_locations": [
    # Unpaywall's own order for the smoke DOI: the preprint comes BEFORE the PMC copy.
    {"url": BMC_PDF, "url_for_pdf": BMC_PDF, "version": "publishedVersion"},
    {"url": RS_PDF, "url_for_pdf": RS_PDF, "version": "acceptedVersion"},
    {"url": PMC_PDF, "url_for_pdf": PMC_PDF, "version": "publishedVersion"},
]}
CHALLENGE = (b"<!DOCTYPE html><html><head><title>Preparing to download ...</title><script>"
             b"window.POW_CHALLENGE='x';</script></head><body></body></html>")


def test_route_unpaywall_tries_vor_before_preprint(monkeypatch):
    calls = []

    def get(url, **kw):
        calls.append(url)
        if "api.unpaywall.org" in url:
            return Resp(js=UNPAYWALL)
        if "webservices/rest/search" in url:
            return Resp(js={"resultList": {"result": [{"pmcid": "PMC10148551", "hasPDF": "Y"}]}})
        if "idconv" in url or "semanticscholar" in url:
            return Resp(status=404, js={})
        if url == RS_PDF:
            return Resp(content=PDF, url=RS_PDF)
        return Resp(content=CHALLENGE, url=url)        # BMC, PMC, Europe PMC: walled
    monkeypatch.setattr(pf.requests, "get", get)
    monkeypatch.setattr(pf, "UNPAYWALL_EMAIL", "test@example.edu")
    monkeypatch.setattr(pf, "PAPER_RADAR_DB", None)
    monkeypatch.setattr(pf, "valid_pdf", lambda c: c[:4] == b"%PDF")
    assert pf.route_unpaywall(DOI) == PDF
    assert pf._SOURCE == (RS_PDF, "acceptedVersion")
    assert pf.version_of("unpaywall", pf._SOURCE, pf._LAST_GRAB_URL)[0] == "preprint"
    walled = [u for _, u in pf._BLOCKED]
    assert PMC_PDF in walled and EPMC_PDF in walled
    assert calls.index(PMC_PDF) < calls.index(RS_PDF)
    assert calls.index(EPMC_PDF) < calls.index(RS_PDF)


def test_version_rank_order():
    ranks = [pf._version_rank(u, v) for u, v in (
        (PMC_PDF, "publishedVersion"), (EPMC_PDF, None),
        ("https://repo.example.edu/a.pdf", "acceptedVersion"), (RS_PDF, "acceptedVersion"))]
    assert ranks == sorted(ranks) and len(set(ranks)) == 4


# --- library_session layer 1 accepts the labelled file (it is not at the requested name) ----

def test_library_session_layer1_accepts_labelled_preprint(monkeypatch, tmp_path, capsys):
    import json
    import library_session as ls
    out = tmp_path / "p.pdf"
    labeled = tmp_path / "p.preprint.pdf"
    labeled.write_bytes(PDF)
    env = {"ok": True, "route": "unpaywall", "path": str(labeled), "version": "preprint",
           "version_basis": "host:researchsquare.com", "vor_url": f"https://doi.org/{DOI}",
           "vor_blocked": [{"kind": "pmc_challenge", "url": PMC_PDF}],
           "zotero_attachment_title": pf.ZOTERO_TITLE["preprint"]}

    class Done:
        stdout, stderr, returncode = json.dumps(env) + "\n", "", 0
    monkeypatch.setattr(ls.subprocess, "run", lambda *a, **k: Done())
    monkeypatch.setattr(ls, "_log", lambda rec: None)
    assert ls._try_paper_fetch(DOI, out) is True
    err = capsys.readouterr().err
    assert "NOT the version of record" in err and PMC_PDF in err


def test_library_session_layer1_failure_unchanged(monkeypatch, tmp_path):
    import json
    import library_session as ls

    class Done:
        stdout, stderr, returncode = json.dumps({"ok": False, "path": None}) + "\n", "x", 2
    monkeypatch.setattr(ls.subprocess, "run", lambda *a, **k: Done())
    monkeypatch.setattr(ls, "_log", lambda rec: None)
    assert ls._try_paper_fetch(DOI, tmp_path / "p.pdf") is False
