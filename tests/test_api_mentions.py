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


class _PropertyAwareSession(_FakeNeo4jSession):
    """A session that answers like the real store: a node is found only
    if the query filters on a property that node actually carries.

    The stub above returns its node for ANY query, so it cannot tell an
    indexed `gmr_id` seek apart from the legacy `id`/`nuts_code`
    fallback — it would stay green if either path were dropped.

    No nuts_code case here on purpose: _IRI_RE only admits a UUID, so a
    real NUTS code like "PT16" is rejected as a 400 long before the
    query runs. That leg is unreachable in practice.
    """

    def __init__(self, node):
        self._node = node

    def run(self, query, **kwargs):  # type: ignore[override]
        uid = kwargs.get("uid")
        for prop in ("gmr_id", "id", "nuts_code"):  # nuts_code: see note below
            if f"n.{prop} = $uid" in query and self._node.get(prop) == uid:
                return _MatchOneResult(self._node)
        return _MatchOneResult(None)


class _PropertyAwareNeo4j(_FakeNeo4jClient):
    def __init__(self, node):
        self._node = node

    def session(self):
        return _PropertyAwareSession(self._node)


# ── lookup keys ─────────────────────────────────────────────


def test_resolve_finds_node_by_gmr_id():
    uid = "11111111-2222-3333-4444-555555555555"
    node = {"gmr_id": uid, "name": "Siemens AG", "country": "DEU"}
    client = make_test_client(neo4j_client=_PropertyAwareNeo4j(node))
    try:
        r = client.get(
            f"/mentions/resolve?iri=http://data.fontem.eu/id/Company/{uid}")
        assert r.status_code == 200
        assert r.json()["label"] == "Siemens AG"
    finally:
        cleanup_dishka()


def test_resolve_still_finds_legacy_node_without_a_gmr_id():
    """Nodes predating the gmr_id stamp are reachable only by `id`.

    The fast path seeks gmr_id; this pins that the fallback still runs
    when that misses, which is the whole reason the fallback exists.
    """
    uid = "22222222-3333-4444-5555-666666666666"
    node = {"id": uid, "name": "Legacy Co", "country": "PRT"}
    client = make_test_client(neo4j_client=_PropertyAwareNeo4j(node))
    try:
        r = client.get(
            f"/mentions/resolve?iri=http://data.fontem.eu/id/Company/{uid}")
        assert r.status_code == 200
        assert r.json()["label"] == "Legacy Co"
    finally:
        cleanup_dishka()


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
