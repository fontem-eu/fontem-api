"""Tests for the /sparql proxy router."""
from __future__ import annotations

from src.data.sparql.virtuoso_client import SparqlTimeout

from tests.dishka_fixtures import make_test_client


class _RecorderVirtuoso:
    """In-process Virtuoso stub. ``responses`` is consumed FIFO; each
    item is either a list of bindings (returned) or an Exception (raised)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[str] = []

    def query(self, q: str):
        self.calls.append(q)
        next_resp = self._responses.pop(0)
        if isinstance(next_resp, Exception):
            raise next_resp
        return next_resp


# ── GET /sparql ─────────────────────────────────────────────────


def test_get_sparql_returns_endpoint_metadata():
    client = make_test_client()
    r = client.get("/sparql")
    assert r.status_code == 200
    body = r.json()
    assert body["endpoint"] == "/api/sparql"
    assert body["limits"]["read_only"] is True
    assert body["limits"]["max_query_bytes"] >= 1024
    assert body["examples"]
    assert "SELECT" in body["examples"][0]["query"]


# ── POST /sparql happy path ─────────────────────────────────────


def test_post_sparql_returns_sparql_json_envelope():
    virtuoso = _RecorderVirtuoso([
        [{"s": "http://example.com/x", "n": 42}],
    ])
    client = make_test_client(virtuoso=virtuoso)
    r = client.post("/sparql", json={"query": "SELECT ?s ?n WHERE { ?s ?p ?n }"})
    assert r.status_code == 200, r.text
    body = r.json()
    # Envelope shape: SPARQL 1.1 JSON results format.
    assert set(body.keys()) == {"head", "results"}
    assert body["head"]["vars"] == ["s", "n"]
    [row] = body["results"]["bindings"]
    assert row["s"] == {"type": "uri", "value": "http://example.com/x"}
    assert row["n"] == {
        "type": "literal", "value": "42",
        "datatype": "http://www.w3.org/2001/XMLSchema#integer",
    }


def test_post_sparql_empty_result_set_returns_head_with_no_vars():
    virtuoso = _RecorderVirtuoso([[]])
    client = make_test_client(virtuoso=virtuoso)
    r = client.post("/sparql", json={"query": "SELECT * WHERE { ?s ?p ?o } LIMIT 0"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["head"]["vars"] == []
    assert body["results"]["bindings"] == []


# ── POST /sparql guardrails ─────────────────────────────────────


def test_post_sparql_rejects_empty_body():
    client = make_test_client()
    r = client.post("/sparql", json={"query": ""})
    assert r.status_code == 400
    assert "non-empty" in r.json()["detail"]


def test_post_sparql_rejects_missing_query_key():
    client = make_test_client()
    r = client.post("/sparql", json={})
    assert r.status_code == 400


def test_post_sparql_rejects_oversized_query():
    client = make_test_client()
    # 4097-byte query (default limit is 4096).
    payload = "SELECT * WHERE { ?s ?p ?o } " + "# " + ("x" * 5000)
    r = client.post("/sparql", json={"query": payload})
    assert r.status_code == 400
    assert "limit" in r.json()["detail"].lower()


def test_post_sparql_rejects_update_keywords():
    client = make_test_client()
    for verb in ("INSERT", "DELETE", "DROP", "CLEAR"):
        r = client.post(
            "/sparql",
            json={"query": f"{verb} DATA {{ <a> <b> <c> }}"},
        )
        assert r.status_code == 400, f"{verb} should be rejected"


# ── POST /sparql infrastructure failures ─────────────────────────


def test_post_sparql_503_when_virtuoso_unconfigured():
    # No virtuoso passed → provider returns None.
    client = make_test_client()
    r = client.post("/sparql", json={"query": "SELECT * WHERE { ?s ?p ?o } LIMIT 1"})
    assert r.status_code == 503
    assert "Virtuoso" in r.json()["detail"]


def test_post_sparql_504_when_query_times_out():
    virtuoso = _RecorderVirtuoso([
        SparqlTimeout("query exceeded 60.0s"),
    ])
    client = make_test_client(virtuoso=virtuoso)
    r = client.post("/sparql", json={"query": "SELECT * WHERE { ?s ?p ?o }"})
    assert r.status_code == 504
    assert "60.0s" in r.json()["detail"]
