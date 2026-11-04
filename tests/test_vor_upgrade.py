"""1.10.0: layer 1 found only a preprint / accepted manuscript → library_session goes on to the
direct and institutional routes for the version of record (user ruling 2026-11-04). No network,
no browser."""
import importlib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
importlib.import_module("paper" + "_fetch")
import library_session as ls  # noqa: E402

PDF = b"%PDF-1.7\n" + b"x" * 5000
DOI = "10.1186/s12984-023-01168-x"
PMC_PDF = "https://pmc.ncbi.nlm.nih.gov/articles/PMC10148551/pdf/x.pdf"


def l1_env(path, version="preprint"):
    return {"ok": True, "route": "unpaywall", "path": str(path), "version": version,
            "version_basis": "host:researchsquare.com", "vor_url": f"https://doi.org/{DOI}",
            "vor_blocked": [{"kind": "pmc_challenge", "url": PMC_PDF}],
            "zotero_attachment_title": "Preprint PDF — not the version of record"}


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(ls, "_log", lambda rec: None)
    monkeypatch.setattr(ls, "_L1_NON_VOR", None)


def fake_ladder(monkeypatch, pre, reason, write_vor):
    def ladder(pw, doi, out, *a):
        ls._L1_NON_VOR = pre
        if write_vor:
            out.write_bytes(PDF)
        return reason
    monkeypatch.setattr(ls, "_fetch_ladder", ladder)
    monkeypatch.setattr(ls, "_L1_ENV", l1_env(pre))


def test_vor_found_replaces_preprint(monkeypatch, tmp_path, capsys):
    out, pre = tmp_path / "p.pdf", tmp_path / "p.preprint.pdf"
    pre.write_bytes(PDF)
    fake_ladder(monkeypatch, pre, "ok", write_vor=True)
    assert ls.run_fetch(None, DOI, out) == "ok"
    assert out.exists() and not pre.exists()
    assert "version of record obtained" in capsys.readouterr().err


@pytest.mark.parametrize("reason", ["blocked", "auth", "fail", "no_route"])
def test_vor_not_found_keeps_labelled_preprint(monkeypatch, tmp_path, capsys, reason):
    out, pre = tmp_path / "p.pdf", tmp_path / "p.preprint.pdf"
    pre.write_bytes(PDF)
    fake_ladder(monkeypatch, pre, reason, write_vor=False)
    assert ls.run_fetch(None, DOI, out) == "ok"          # the 1.9.0 result stands → exit 0
    assert pre.exists() and not out.exists()
    err = capsys.readouterr().err
    assert f"({reason})" in err and "NOT the version of record" in err and PMC_PDF in err


def test_no_preprint_reason_passes_through(monkeypatch, tmp_path):
    monkeypatch.setattr(ls, "_fetch_ladder", lambda *a: "blocked")
    assert ls.run_fetch(None, DOI, tmp_path / "p.pdf") == "blocked"


class Stop(Exception):
    pass


def stub_layer1(monkeypatch, tmp_path, env):
    calls = {}
    monkeypatch.setattr(ls, "_resolve_route", lambda doi: {"kind": "tpl"})

    def l1(doi, out, title=None):
        ls._L1_ENV = env
        return True
    monkeypatch.setattr(ls, "_try_paper_fetch", l1)

    def new_context(pw, headless=True):
        calls["headless"] = headless
        raise Stop()
    monkeypatch.setattr(ls, "_new_context", new_context)
    return calls


def test_ladder_goes_on_after_a_preprint(monkeypatch, tmp_path):
    pre = tmp_path / "p.preprint.pdf"
    calls = stub_layer1(monkeypatch, tmp_path, l1_env(pre))
    with pytest.raises(Stop):                       # reached the browser = did not stop at L1
        ls._fetch_ladder(None, DOI, tmp_path / "p.pdf")
    assert ls._L1_NON_VOR == pre
    assert calls["headless"] is False               # VoR behind a wall → the direct route


def test_ladder_stops_at_a_vor(monkeypatch, tmp_path):
    out = tmp_path / "p.pdf"
    calls = stub_layer1(monkeypatch, tmp_path, {"ok": True, "path": str(out),
                                                "version": "published"})
    assert ls._fetch_ladder(None, DOI, out) == "ok"
    assert calls == {} and ls._L1_NON_VOR is None   # no browser opened
