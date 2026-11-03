"""Issue #7 point 3: a DOI whose prefix belongs to one publisher but whose doi.org landing is
another's (Ann Rheum Dis 10.1136/annrheumdis-* → linkinghub.elsevier.com/retrieve/pii/…) is
routed by the landing host. No network: requests.get is replaced by a fake redirect chain."""
import importlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
pf = importlib.import_module("paper" + "_fetch")
import library_session as ls  # noqa: E402

ARD = "10.1136/annrheumdis-2021-219846"
PII = "S0003496724205148"
CHAIN = {
    f"https://doi.org/{ARD}": f"https://linkinghub.elsevier.com/retrieve/pii/{PII}",
    "https://doi.org/10.1136/bmj.n71": "https://www.bmj.com/lookup/doi/10.1136/bmj.n71",
}


class FakeResp:
    def __init__(self, status, location=None, content=b"", url=None, json_data=None):
        self.status_code = self.status = status
        self.headers = {"location": location} if location else {}
        self.content, self.url, self._json = content, url, json_data

    def close(self):
        pass

    def json(self):
        return self._json


def fake_get(calls):
    def get(url, **kw):
        calls.append(url)
        if url in CHAIN:
            return FakeResp(302, CHAIN[url])
        return FakeResp(200)                     # final hop: no Location → stop
    return get


def test_paper_fetch_routes_ard_to_elsevier_by_pii(monkeypatch):
    calls = []
    monkeypatch.setattr(pf.requests, "get", fake_get(calls))
    pf._LANDING.clear()
    routes = pf.routes_for(ARD)
    assert routes[0] is pf.route_elsevier
    assert pf.landing_pii(ARD) == PII
    assert routes[-2:] == [pf.route_unpaywall, pf.route_pubmed_free]


def test_route_elsevier_uses_pii_endpoint(monkeypatch):
    pf._LANDING.clear()
    pf._LANDING[ARD] = CHAIN[f"https://doi.org/{ARD}"]
    monkeypatch.setattr(pf, "get_secret", lambda name: "k" if name == "ELSEVIER_TDM_KEY" else None)
    seen = []

    def get(url, **kw):
        seen.append(url)
        return FakeResp(404)
    monkeypatch.setattr(pf.requests, "get", get)
    assert pf.route_elsevier(ARD) is None
    assert seen == [f"https://api.elsevier.com/content/article/pii/{PII}"]


def test_elsevier_doi_keeps_doi_endpoint_and_no_lookup(monkeypatch):
    pf._LANDING.clear()
    calls = []
    monkeypatch.setattr(pf.requests, "get", fake_get(calls))
    assert pf.routes_for("10.1016/j.apmr.2024.01.001")[0] is pf.route_elsevier
    assert calls == []                           # own prefix → no doi.org lookup


def test_unmatched_landing_keeps_prefix_ladder(monkeypatch):
    pf._LANDING.clear()
    monkeypatch.setattr(pf.requests, "get", fake_get([]))
    assert pf.routes_for("10.1136/bmj.n71") == [pf.route_unpaywall, pf.route_pubmed_free]


def test_library_session_moves_known_prefix_to_ck(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", fake_get([]))
    monkeypatch.setattr(ls, "_log", lambda rec: None)
    ls._LANDING.clear()
    route = ls._resolve_route(ARD)
    assert route["kind"] == "ck" and route["aliased_from"] == "10.1016"
    assert ls._landing_pii(ARD) == PII
    assert ls.ROUTES["10.1136"]["kind"] == "meta"          # the BMJ route itself is untouched


def test_library_session_keeps_bmj_route_for_bmj_landing(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", fake_get([]))
    monkeypatch.setattr(ls, "_log", lambda rec: None)
    assert ls._resolve_route("10.1136/bmj.n71") is ls.ROUTES["10.1136"]


def test_library_session_lookup_failure_keeps_prefix_route(monkeypatch):
    import requests

    def boom(url, **kw):
        raise requests.ConnectionError("offline")
    monkeypatch.setattr(requests, "get", boom)
    assert ls._resolve_route(ARD) is ls.ROUTES["10.1136"]
