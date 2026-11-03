"""paper_fetch.get_secret reads the cross-platform secret store first (what README tells users
to fill), and only then the legacy ~/.secrets/secret.ps1 — which this repo does not ship."""
import importlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
pf = importlib.import_module("paper" + "_fetch")
import library_session as ls  # noqa: E402


def test_store_first(monkeypatch):
    monkeypatch.setattr(ls, "secret_get_opt", lambda name: "v-" + name)
    assert pf.get_secret("ELSEVIER_TDM_KEY") == "v-ELSEVIER_TDM_KEY"


def test_missing_everywhere_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(ls, "secret_get_opt", lambda name: None)
    monkeypatch.setattr(pf, "SECRET_PS1", tmp_path / "absent.ps1")
    assert pf.get_secret("ELSEVIER_TDM_KEY") is None
