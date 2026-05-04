"""Tests for /mentions/resolve."""
from __future__ import annotations

# pylint: disable=missing-function-docstring

from tests.dishka_fixtures import (
    _FakeNeo4jClient,
    _FakeNeo4jSession,
    _FakeResult,
    cleanup_dishka,
    make_test_client,
)


class _MatchOneResult(_FakeResult):
    """Single() returns a single dict-like row stub."""

    def __init__(self, node):
        self._node = node

    def single(self):
        if self._node is None:
            return None
        return {"n": self._node}


class _StubNeo4jSession(_FakeNeo4jSession):
    """Returns the stubbed node for any MATCH query."""

    def __init__(self, node):
        self._node = node

    def run(self, query, **kwargs):  # type: ignore[override]
        return _MatchOneResult(self._node)


class _StubNeo4j(_FakeNeo4jClient):
    def __init__(self, node):
        self._node = node

    def session(self):
        return _StubNeo4jSession(self._node)


# ── happy path ──────────────────────────────────────────────


def test_resolve_company_iri_returns_panel_payload():
    node = {"gmr_id": "11111111-2222-3333-4444-555555555555",
            "name": "Siemens AG",
            "country": "DE",
            "lei": "W38RGI023J3WT1HWY074"}
    client = make_test_client(neo4j_client=_StubNeo4j(node))
    try:
        iri = "http://data.fontem.eu/id/Company/11111111-2222-3333-4444-555555555555"
        r = client.get(f"/mentions/resolve?iri={iri}")
        assert r.status_code == 200
        data = r.json()
        assert data["class"] == "Company"
        assert data["label"] == "Siemens AG"
        assert data["iri"] == iri
        # Country fact present, LEI fact present.
        keys = {f["key"] for f in data["facts"]}
        assert "country" in keys
        assert "LEI" in keys
        # Profile link is the existing /company/<gmr_id> route.
        assert data["links"]["profile"] == f"/company/{node['gmr_id']}"
    finally:
        cleanup_dishka()


def test_resolve_authority_iri():
    node = {"gmr_id": "11111111-2222-3333-4444-555555555555",
            "authority_id": "78d8b920-1a05-56f2-a84b-6a5e5afe8a59",
            "name": "eu-LISA",
            "country": "EU"}
    client = make_test_client(neo4j_client=_StubNeo4j(node))
    try:
        iri = "http://data.fontem.eu/id/Authority/11111111-2222-3333-4444-555555555555"
        r = client.get(f"/mentions/resolve?iri={iri}")
        assert r.status_code == 200
        data = r.json()
        assert data["class"] == "Authority"
        assert data["label"] == "eu-LISA"
    finally:
        cleanup_dishka()


# ── 400 / 404 surface ───────────────────────────────────────


def test_malformed_iri_returns_400():
    client = make_test_client(neo4j_client=_StubNeo4j(None))
    try:
        r = client.get("/mentions/resolve?iri=not-a-valid-iri")
        assert r.status_code == 400
        assert "iri must look like" in r.json()["detail"]
    finally:
        cleanup_dishka()


def test_unsupported_class_returns_400():
    client = make_test_client(neo4j_client=_StubNeo4j(None))
    try:
        # Listing is in the ontology but not in the Phase B1 resolvable
        # set — IRI is well-formed, class is just not surfaced yet.
        iri = "http://data.fontem.eu/id/Listing/11111111-2222-3333-4444-555555555555"
        r = client.get(f"/mentions/resolve?iri={iri}")
        assert r.status_code == 400
        assert "not resolvable" in r.json()["detail"]
    finally:
        cleanup_dishka()


def test_unknown_iri_returns_404():
    client = make_test_client(neo4j_client=_StubNeo4j(None))
    try:
        iri = "http://data.fontem.eu/id/Company/11111111-2222-3333-4444-555555555555"
        r = client.get(f"/mentions/resolve?iri={iri}")
        assert r.status_code == 404
    finally:
        cleanup_dishka()
