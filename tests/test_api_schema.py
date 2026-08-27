"""The graph schema endpoint: derived, cached, and honest about direction.

The whole point of this endpoint is that a model should never again have to
guess the edge direction or the country-code convention, so those are what
the tests pin: direction is explicit in every relationship row, the
conventions block names ISO-3, and the answer is cached rather than
re-derived per request.
"""
from __future__ import annotations

# pylint: disable=missing-function-docstring,too-few-public-methods

from unittest.mock import MagicMock

from src.api.routers import schema as schema_mod
from tests.dishka_fixtures import make_test_client, cleanup_dishka


class _Result:
    """Iterable of dict rows, with .single() like a neo4j Result."""

    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def single(self):
        return self._rows[0] if self._rows else None


_ANSWERS = (
    ("db.labels", [{"label": "Company"}, {"label": "Contract"}]),
    ("db.relationshipTypes", [{"relationshipType": "AWARDED_TO"}]),
    ("count(n)", [{"c": 42}]),
    ("count(r)", [{"c": 188}]),
    ("keys(n)", [{"k": ["name", "country"]}, {"k": ["name", "gmr_id"]}]),
    ("labels(a)[0]", [{"f": "Contract", "t": "Company"}]),
)


def _handler(query, **_):
    for marker, rows in _ANSWERS:
        if marker in query:
            return _Result(rows)
    return _Result([])


class FakeNeo4jClient:
    def __init__(self, handler=_handler):
        self.calls = 0
        self._handler = handler

    def session(self):
        self.calls += 1
        sess = MagicMock()
        sess.run = self._handler
        sess.__enter__ = lambda s: s
        sess.__exit__ = lambda s, *a: None
        return sess


def _fresh_cache():
    schema_mod._cache["at"] = 0.0  # pylint: disable=protected-access
    schema_mod._cache["payload"] = None  # pylint: disable=protected-access


def test_direction_is_explicit_in_every_relationship():
    # The bug this endpoint answers: a model wrote Company->Contract and got
    # zero rows where Contract-[:AWARDED_TO]->Company holds the data.
    _fresh_cache()
    client = make_test_client(neo4j_client=FakeNeo4jClient())
    resp = client.get("/schema/graph")
    cleanup_dishka()
    assert resp.status_code == 200
    rels = resp.json()["relationships"]
    assert rels == [
        {"type": "AWARDED_TO", "from": "Contract", "to": "Company",
         "count": 188},
    ]


def test_labels_carry_counts_and_a_key_union():
    _fresh_cache()
    client = make_test_client(neo4j_client=FakeNeo4jClient())
    resp = client.get("/schema/graph")
    cleanup_dishka()
    nodes = {n["label"]: n for n in resp.json()["node_labels"]}
    assert nodes["Company"]["count"] == 42
    # Union across sampled nodes, sorted — not just the first node's keys.
    assert nodes["Company"]["keys"] == ["country", "gmr_id", "name"]


def test_the_conventions_name_the_country_format():
    _fresh_cache()
    client = make_test_client(neo4j_client=FakeNeo4jClient())
    resp = client.get("/schema/graph")
    cleanup_dishka()
    conventions = " ".join(resp.json()["conventions"])
    assert "ISO-3166 alpha-3" in conventions
    assert "'RUS'" in conventions
    assert "AWARDED_TO" in conventions


def test_the_second_request_is_served_from_cache():
    _fresh_cache()
    fake = FakeNeo4jClient()
    client = make_test_client(neo4j_client=fake)
    first = client.get("/schema/graph")
    sessions_after_first = fake.calls
    second = client.get("/schema/graph")
    cleanup_dishka()
    assert first.status_code == second.status_code == 200
    assert fake.calls == sessions_after_first, \
        "the second request must not touch the graph"


def test_a_stale_cache_beats_a_dead_graph():
    # TTL expired AND the graph refuses: keep serving what we had rather
    # than failing the caller — a stale schema is still a correct schema.
    _fresh_cache()
    good = FakeNeo4jClient()
    client = make_test_client(neo4j_client=good)
    assert client.get("/schema/graph").status_code == 200
    cleanup_dishka()

    schema_mod._cache["at"] = 0.0  # pylint: disable=protected-access

    def _explodes(query, **_):
        raise RuntimeError("graph down")

    client = make_test_client(neo4j_client=FakeNeo4jClient(_explodes))
    resp = client.get("/schema/graph")
    cleanup_dishka()
    assert resp.status_code == 200
    assert resp.json()["relationships"]


def test_no_cache_and_a_dead_graph_is_a_503():
    _fresh_cache()

    def _explodes(query, **_):
        raise RuntimeError("graph down")

    client = make_test_client(neo4j_client=FakeNeo4jClient(_explodes))
    resp = client.get("/schema/graph")
    cleanup_dishka()
    assert resp.status_code == 503


def test_the_endpoint_is_annotated_as_an_agent_tool():
    # The community API derives the get_schema tool from this annotation;
    # losing it silently unplugs the tool.
    _fresh_cache()
    client = make_test_client(neo4j_client=FakeNeo4jClient())
    spec = client.get("/openapi.json").json()
    cleanup_dishka()
    op = spec["paths"]["/schema/graph"]["get"]
    tool = op.get("x-agent-tool")
    assert tool and tool["name"] == "get_schema"
    assert tool["core"] is True
