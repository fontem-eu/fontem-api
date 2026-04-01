"""
Graph Explorer API — Unit Tests
=================================
Tests for GET /graph/{entity_id} and GET /graph/paths/find.
Mocks the Neo4j client to avoid real database access.

GE-API-01 through GE-API-10 from the test plan.
"""
from __future__ import annotations

# pylint: disable=missing-function-docstring,redefined-outer-name

from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from src.api.app import app
from src.api.dependencies import get_neo4j_client


# ── Fake Neo4j objects ─────────────────────────────────────────


class FakeNode:
    """Mimics a neo4j.graph.Node (supports dict(node) via keys/getitem)."""

    def __init__(self, labels: list[str], props: dict, element_id: str = "0"):
        self._labels = frozenset(labels)
        self._props = dict(props)
        self._element_id = element_id

    @property
    def labels(self):
        return self._labels

    @property
    def element_id(self):
        return self._element_id

    def keys(self):
        return self._props.keys()

    def __getitem__(self, key):
        return self._props[key]

    def __iter__(self):
        return iter(self._props.keys())

    def __len__(self):
        return len(self._props)

    def items(self):
        return self._props.items()

    def get(self, key, default=None):
        return self._props.get(key, default)


class FakeRelationship:
    """Mimics a neo4j.graph.Relationship (supports dict(rel) via keys/getitem)."""

    def __init__(self, start: FakeNode, end: FakeNode, rel_type: str, props=None):
        self._start = start
        self._end = end
        self._type = rel_type
        self._props = props or {}

    @property
    def start_node(self):
        return self._start

    @property
    def end_node(self):
        return self._end

    @property
    def type(self):
        return self._type

    def keys(self):
        return self._props.keys()

    def __getitem__(self, key):
        return self._props[key]

    def __iter__(self):
        return iter(self._props.keys())

    def __len__(self):
        return len(self._props)

    def items(self):
        return self._props.items()


class FakePath:
    """Mimics a neo4j.graph.Path."""

    def __init__(self, nodes: list[FakeNode], rels: list[FakeRelationship]):
        self._nodes = nodes
        self._rels = rels

    @property
    def nodes(self):
        return self._nodes

    @property
    def relationships(self):
        return self._rels


# ── Test data ────────────────────────────────────────────────

COMPANY_A = FakeNode(["Company"], {"gmr_id": "comp-aaa", "name": "Acme Corp", "country": "FRA"})
COMPANY_B = FakeNode(["Company"], {"gmr_id": "comp-bbb", "name": "Beta Ltd", "country": "DEU"})
COMPANY_C = FakeNode(["Company"], {"gmr_id": "comp-ccc", "name": "Gamma Inc", "country": "USA"})
AUTH_X = FakeNode(
    ["Authority"],
    {"authority_id": "auth-xxx", "name": "Ville de Paris", "country": "FRA"},
)
PERSON_P = FakeNode(["Person"], {"person_id": "per-ppp", "name": "Dupont", "first_name": "Jean"})
CONTRACT_1 = FakeNode(["Contract"], {
    "ted_notice_id": "con-111", "title": "Road works", "value_eur": 500000,
})
CONTRACT_2 = FakeNode(["Contract"], {
    "ted_notice_id": "con-222", "title": "IT Services", "value_eur": 200000,
})

REL_AWARDED = FakeRelationship(AUTH_X, CONTRACT_1, "AWARDED", {"date": "2024-06-15"})
REL_AWARDED_TO = FakeRelationship(CONTRACT_1, COMPANY_A, "AWARDED_TO")
REL_DIRECTS = FakeRelationship(PERSON_P, COMPANY_A, "DIRECTS", {"role": "CEO", "current": True})
REL_SUBSIDIARY = FakeRelationship(COMPANY_B, COMPANY_A, "SUBSIDIARY_OF")
REL_AWARDED2 = FakeRelationship(AUTH_X, CONTRACT_2, "AWARDED")
REL_AWARDED_TO2 = FakeRelationship(CONTRACT_2, COMPANY_B, "AWARDED_TO")


class FakeResult:
    """Wraps a single record or None."""

    def __init__(self, record):
        self._record = record

    def single(self):
        return self._record

    def data(self):
        if self._record is None:
            return []
        if isinstance(self._record, list):
            return self._record
        return [self._record]


def _make_detect_handler(known_entities: dict):
    """Build a session.run handler that detects entities and traverses.

    known_entities maps entity_id → (FakeNode, label, id_prop).
    """
    def handler(query, **kwargs):  # pylint: disable=too-many-return-statements
        # Entity detection queries
        if "labels(n)[0]" in query:
            eid = kwargs.get("eid")
            if eid in known_entities:
                node, label, _ = known_entities[eid]
                return FakeResult({"label": label})
            return FakeResult(None)

        # Single node fetch
        if "RETURN n LIMIT 1" in query:
            eid = kwargs.get("eid")
            if eid in known_entities:
                node = known_entities[eid][0]
                return FakeResult({"n": node})
            return FakeResult(None)

        return FakeResult(None)

    return handler


# ── Fixtures ──────────────────────────────────────────────────


class FakeNeo4jClient:  # pylint: disable=too-few-public-methods
    """Mock Neo4j client whose session.run is programmable."""

    def __init__(self, run_handler):
        self._handler = run_handler

    def session(self):
        sess = MagicMock()
        sess.run = self._handler
        sess.__enter__ = lambda s: s
        sess.__exit__ = lambda s, *a: None
        return sess


@pytest.fixture()
def _override_cleanup():
    yield
    app.dependency_overrides.clear()


# ── GE-API-01: Depth 0 returns only center node ──────────────


def test_depth_0_returns_center_only(_override_cleanup):
    entities = {"comp-aaa": (COMPANY_A, "Company", "gmr_id")}

    def handler(query, **kwargs):
        if "labels(n)[0]" in query:
            eid = kwargs.get("eid")
            if eid in entities:
                return FakeResult({"label": entities[eid][1]})
            return FakeResult(None)
        if "RETURN n LIMIT 1" in query:
            eid = kwargs.get("eid")
            if eid in entities:
                return FakeResult({"n": entities[eid][0]})
            return FakeResult(None)
        return FakeResult(None)

    app.dependency_overrides[get_neo4j_client] = lambda: FakeNeo4jClient(handler)
    with TestClient(app) as client:
        resp = client.get("/graph/comp-aaa?depth=0")
    assert resp.status_code == 200
    body = resp.json()
    assert body["center"]["id"] == "comp-aaa"
    assert len(body["nodes"]) == 1
    assert len(body["edges"]) == 0
    assert body["truncated"] is False


# ── GE-API-02: Depth 1 returns direct relationships ──────────


def test_depth_1_returns_neighbors(_override_cleanup):
    entities = {
        "comp-aaa": (COMPANY_A, "Company", "gmr_id"),
    }

    traversal_records = [
        {"n": COMPANY_A, "rel": REL_AWARDED_TO},
        {"n": CONTRACT_1, "rel": REL_AWARDED_TO},
        {"n": AUTH_X, "rel": REL_AWARDED},
    ]

    def handler(query, **kwargs):
        if "labels(n)[0]" in query:
            eid = kwargs.get("eid")
            if eid in entities:
                return FakeResult({"label": entities[eid][1]})
            return FakeResult(None)
        if "RETURN n LIMIT 1" in query:
            eid = kwargs.get("eid")
            if eid in entities:
                return FakeResult({"n": entities[eid][0]})
            return FakeResult(None)
        if "UNWIND" in query:
            return FakeResult(traversal_records)
        return FakeResult(None)

    app.dependency_overrides[get_neo4j_client] = lambda: FakeNeo4jClient(handler)
    with TestClient(app) as client:
        resp = client.get("/graph/comp-aaa?depth=1")
    assert resp.status_code == 200
    body = resp.json()
    node_ids = {n["id"] for n in body["nodes"]}
    assert "comp-aaa" in node_ids
    assert "con-111" in node_ids
    assert "auth-xxx" in node_ids
    assert len(body["edges"]) >= 1


# ── GE-API-03: Type filter excludes unwanted types ────────────


def test_type_filter_excludes_types(_override_cleanup):
    entities = {
        "comp-aaa": (COMPANY_A, "Company", "gmr_id"),
    }

    traversal_records = [
        {"n": COMPANY_A, "rel": REL_AWARDED_TO},
        {"n": CONTRACT_1, "rel": REL_AWARDED_TO},
        {"n": AUTH_X, "rel": REL_AWARDED},
        {"n": PERSON_P, "rel": REL_DIRECTS},
    ]

    def handler(query, **kwargs):
        if "labels(n)[0]" in query:
            eid = kwargs.get("eid")
            if eid in entities:
                return FakeResult({"label": entities[eid][1]})
            return FakeResult(None)
        if "RETURN n LIMIT 1" in query:
            eid = kwargs.get("eid")
            if eid in entities:
                return FakeResult({"n": entities[eid][0]})
            return FakeResult(None)
        if "UNWIND" in query:
            return FakeResult(traversal_records)
        return FakeResult(None)

    app.dependency_overrides[get_neo4j_client] = lambda: FakeNeo4jClient(handler)
    with TestClient(app) as client:
        resp = client.get("/graph/comp-aaa?depth=1&types=Company,Contract")
    body = resp.json()
    types_in_result = {n["type"] for n in body["nodes"]}
    assert "Company" in types_in_result
    assert "Contract" in types_in_result
    assert "Authority" not in types_in_result
    assert "Person" not in types_in_result


# ── GE-API-04: Response capped at 500 nodes ──────────────────


def test_response_capped_at_500(_override_cleanup):
    entities = {
        "comp-aaa": (COMPANY_A, "Company", "gmr_id"),
    }

    # Generate 600 fake contract nodes
    big_records = []
    for i in range(600):
        cid = f"con-{i:04d}"
        node = FakeNode(["Contract"], {"ted_notice_id": cid, "title": f"C{i}"})
        rel = FakeRelationship(node, COMPANY_A, "AWARDED_TO")
        big_records.append({"n": node, "rel": rel})

    def handler(query, **kwargs):
        if "labels(n)[0]" in query:
            eid = kwargs.get("eid")
            if eid in entities:
                return FakeResult({"label": entities[eid][1]})
            return FakeResult(None)
        if "RETURN n LIMIT 1" in query:
            eid = kwargs.get("eid")
            if eid in entities:
                return FakeResult({"n": entities[eid][0]})
            return FakeResult(None)
        if "UNWIND" in query:
            return FakeResult(big_records)
        return FakeResult(None)

    app.dependency_overrides[get_neo4j_client] = lambda: FakeNeo4jClient(handler)
    with TestClient(app) as client:
        resp = client.get("/graph/comp-aaa?depth=1")
    body = resp.json()
    assert body["truncated"] is True
    assert len(body["nodes"]) <= 500
    assert body["total_available"] > 500
    # Center node is always included
    assert any(n["id"] == "comp-aaa" for n in body["nodes"])


# ── GE-API-05: Unknown entity returns empty graph ─────────────


def test_unknown_entity_returns_empty(_override_cleanup):
    def handler(_query, **_kwargs):
        return FakeResult(None)

    app.dependency_overrides[get_neo4j_client] = lambda: FakeNeo4jClient(handler)
    with TestClient(app) as client:
        resp = client.get("/graph/no-such-id?depth=1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["center"]["type"] == "Unknown"
    assert body["nodes"] == []
    assert body["edges"] == []
    assert body["total_available"] == 0


# ── GE-API-06: Depth > 3 returns 422 ─────────────────────────


def test_depth_over_3_returns_422(_override_cleanup):
    def handler(_query, **_kwargs):
        return FakeResult(None)

    app.dependency_overrides[get_neo4j_client] = lambda: FakeNeo4jClient(handler)
    with TestClient(app) as client:
        resp = client.get("/graph/comp-aaa?depth=4")
    assert resp.status_code == 422


# ── GE-API-07: Entry from Authority ───────────────────────────


def test_entry_from_authority(_override_cleanup):
    entities = {
        "auth-xxx": (AUTH_X, "Authority", "authority_id"),
    }

    traversal_records = [
        {"n": AUTH_X, "rel": REL_AWARDED},
        {"n": CONTRACT_1, "rel": REL_AWARDED},
        {"n": COMPANY_A, "rel": REL_AWARDED_TO},
    ]

    def handler(query, **kwargs):
        if "labels(n)[0]" in query:
            eid = kwargs.get("eid")
            if eid in entities:
                return FakeResult({"label": entities[eid][1]})
            return FakeResult(None)
        if "RETURN n LIMIT 1" in query:
            eid = kwargs.get("eid")
            if eid in entities:
                return FakeResult({"n": entities[eid][0]})
            return FakeResult(None)
        if "UNWIND" in query:
            return FakeResult(traversal_records)
        return FakeResult(None)

    app.dependency_overrides[get_neo4j_client] = lambda: FakeNeo4jClient(handler)
    with TestClient(app) as client:
        resp = client.get("/graph/auth-xxx?depth=1")
    body = resp.json()
    assert body["center"]["id"] == "auth-xxx"
    assert body["center"]["type"] == "Authority"
    node_types = {n["type"] for n in body["nodes"]}
    assert "Contract" in node_types
    assert "Company" in node_types


# ── GE-API-08: Entry from Person ──────────────────────────────


def test_entry_from_person(_override_cleanup):
    entities = {
        "per-ppp": (PERSON_P, "Person", "person_id"),
    }

    traversal_records = [
        {"n": PERSON_P, "rel": REL_DIRECTS},
        {"n": COMPANY_A, "rel": REL_DIRECTS},
    ]

    def handler(query, **kwargs):
        if "labels(n)[0]" in query:
            eid = kwargs.get("eid")
            if eid in entities:
                return FakeResult({"label": entities[eid][1]})
            return FakeResult(None)
        if "RETURN n LIMIT 1" in query:
            eid = kwargs.get("eid")
            if eid in entities:
                return FakeResult({"n": entities[eid][0]})
            return FakeResult(None)
        if "UNWIND" in query:
            return FakeResult(traversal_records)
        return FakeResult(None)

    app.dependency_overrides[get_neo4j_client] = lambda: FakeNeo4jClient(handler)
    with TestClient(app) as client:
        resp = client.get("/graph/per-ppp?depth=1")
    body = resp.json()
    assert body["center"]["id"] == "per-ppp"
    assert body["center"]["type"] == "Person"
    node_types = {n["type"] for n in body["nodes"]}
    assert "Company" in node_types


# ── GE-API-09: Path finding — shortest path ──────────────────


def _make_path_handler(entities, shortest, extra_paths=None):
    """Build a handler that supports entity detection + path queries."""
    base = _make_detect_handler(entities)

    def handler(query, **kwargs):
        if "shortestPath" in query:
            return FakeResult({"path": shortest})
        if "LIMIT 9" in query:
            return FakeResult(
                [{"path": p} for p in extra_paths] if extra_paths else [],
            )
        return base(query, **kwargs)

    return handler


def test_path_finding_shortest(_override_cleanup):
    entities = {
        "per-ppp": (PERSON_P, "Person", "person_id"),
        "auth-xxx": (AUTH_X, "Authority", "authority_id"),
    }
    shortest = FakePath(
        [PERSON_P, COMPANY_A, CONTRACT_1, AUTH_X],
        [REL_DIRECTS, REL_AWARDED_TO, REL_AWARDED],
    )

    handler = _make_path_handler(entities, shortest)
    app.dependency_overrides[get_neo4j_client] = lambda: FakeNeo4jClient(handler)
    with TestClient(app) as client:
        resp = client.get("/graph/paths/find?from=per-ppp&to=auth-xxx")
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_node"]["id"] == "per-ppp"
    assert body["to_node"]["id"] == "auth-xxx"
    assert body["shortest_length"] == 3
    assert len(body["paths"]) >= 1
    assert body["paths"][0]["length"] == 3


# ── GE-API-10: Path finding — extra paths within shortest+2 ──


def test_path_finding_extra_paths(_override_cleanup):
    entities = {
        "per-ppp": (PERSON_P, "Person", "person_id"),
        "auth-xxx": (AUTH_X, "Authority", "authority_id"),
    }
    shortest = FakePath(
        [PERSON_P, COMPANY_A, CONTRACT_1, AUTH_X],
        [REL_DIRECTS, REL_AWARDED_TO, REL_AWARDED],
    )
    alt_path = FakePath(
        [PERSON_P, COMPANY_A, COMPANY_B, CONTRACT_2, AUTH_X],
        [REL_DIRECTS, REL_SUBSIDIARY, REL_AWARDED_TO2, REL_AWARDED2],
    )

    handler = _make_path_handler(entities, shortest, extra_paths=[alt_path])
    app.dependency_overrides[get_neo4j_client] = lambda: FakeNeo4jClient(handler)
    with TestClient(app) as client:
        resp = client.get("/graph/paths/find?from=per-ppp&to=auth-xxx&extra=2")
    body = resp.json()
    assert body["shortest_length"] == 3
    assert len(body["paths"]) == 2
    # First path is shortest (3 hops), second is longer (4 hops)
    assert body["paths"][0]["length"] == 3
    assert body["paths"][1]["length"] == 4
