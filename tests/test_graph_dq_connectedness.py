"""Tests for GraphDataQualitySource.get_graph_connectedness.

This is the Python glue around the Cypher — response shape, cache
behaviour, and graceful degradation when one label's query fails. The
Cypher syntax itself is covered by running the endpoint against a real
Neo4j in staging (see follow-up smoke test plan); these tests guard
the class of bugs we CAN catch locally without a Neo4j container.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.data.graph.graph_data_quality import (
    GraphDataQualitySource,
    _CONNECTEDNESS_LABELS,
    _CONNECTEDNESS_TTL_SECONDS,
)


def _row(count: int, **overrides) -> dict:
    """Build a fake Cypher result row with sensible defaults."""
    row = {
        "count": count,
        "isolated": 0,
        "min_d": 0,
        "max_d": 0,
        "mean_d": 0.0,
        "median_d": 0.0,
        "p95_d": 0.0,
        "b_1": 0, "b_2_5": 0, "b_6_10": 0, "b_11_50": 0,
        "b_51_100": 0, "b_101_500": 0, "b_500_plus": 0,
    }
    row.update(overrides)
    return row


def _client_with_rows(rows_by_label: dict) -> MagicMock:
    """Build a fake Neo4jClient whose session.run().single() returns
    the row keyed by the label in the incoming Cypher. Unknown labels
    fall through to a zero-count row."""
    session = MagicMock()

    def run(cypher: str):
        result = MagicMock()
        for label, row in rows_by_label.items():
            if f"(n:{label})" in cypher:
                result.single.return_value = row
                return result
        result.single.return_value = _row(0)
        return result

    session.run.side_effect = run
    # Context manager protocol
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)

    client = MagicMock()
    client.session.return_value = session
    return client


def test_connectedness_happy_path():
    """A populated label yields a per_type entry with a canonical
    8-bucket histogram and computed isolated_pct."""
    client = _client_with_rows({
        "Company": _row(
            count=100, isolated=40,
            min_d=0, max_d=42, mean_d=3.5, median_d=2.0, p95_d=12.0,
            b_1=15, b_2_5=30, b_6_10=10, b_11_50=5,
            b_51_100=0, b_101_500=0, b_500_plus=0,
        ),
    })
    source = GraphDataQualitySource(client)
    result = source.get_graph_connectedness()

    assert result["cache_ttl_seconds"] == _CONNECTEDNESS_TTL_SECONDS
    assert result["generated_at"] is not None
    assert result["errors"] == []

    companies = [t for t in result["per_type"] if t["entity_type"] == "Company"]
    assert len(companies) == 1
    c = companies[0]
    assert c["count"] == 100
    assert c["isolated_count"] == 40
    assert c["isolated_pct"] == 40.0
    assert c["mean_degree"] == 3.5

    buckets = [b["bucket"] for b in c["histogram"]]
    assert buckets == ["0", "1", "2-5", "6-10", "11-50", "51-100", "101-500", "500+"]
    # "0" bucket equals isolated — they're the same thing
    assert c["histogram"][0]["count"] == 40
    assert c["histogram"][1]["count"] == 15


def test_connectedness_skips_empty_labels():
    """Zero-count labels don't show up in per_type — no need to chart a
    table with all zeros."""
    client = _client_with_rows({})  # every label returns count=0
    source = GraphDataQualitySource(client)
    result = source.get_graph_connectedness()
    assert result["per_type"] == []
    assert result["errors"] == []


def test_connectedness_one_label_failure_doesnt_kill_response():
    """If one label's Cypher raises, the endpoint records it in
    `errors` and keeps processing the rest. This is the difference
    between a degraded dashboard and a 500."""
    session = MagicMock()
    populated = _row(
        count=10, isolated=2, min_d=0, max_d=5,
        mean_d=1.5, median_d=1.0, p95_d=4.0,
        b_1=5, b_2_5=3, b_6_10=0, b_11_50=0,
        b_51_100=0, b_101_500=0, b_500_plus=0,
    )

    def run(cypher: str):
        if "(n:Company)" in cypher:
            raise RuntimeError("simulated Cypher syntax error")
        result = MagicMock()
        if "(n:Contract)" in cypher:
            result.single.return_value = populated
        else:
            result.single.return_value = _row(0)
        return result

    session.run.side_effect = run
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    client = MagicMock()
    client.session.return_value = session

    source = GraphDataQualitySource(client)
    result = source.get_graph_connectedness()

    assert len(result["errors"]) == 1
    assert result["errors"][0]["entity_type"] == "Company"
    assert "simulated" in result["errors"][0]["error"]

    types = [t["entity_type"] for t in result["per_type"]]
    assert "Contract" in types
    assert "Company" not in types  # failed, so not in per_type


def test_connectedness_cache_hit_skips_second_query():
    """Second call inside the TTL returns cached dict without
    re-querying. The whole point of the 1h TTL is to make repeated
    dashboard loads instant."""
    client = _client_with_rows({
        "Company": _row(
            count=5, isolated=1, min_d=0, max_d=2,
            mean_d=0.8, median_d=1.0, p95_d=2.0,
            b_1=3, b_2_5=1,
        ),
    })
    source = GraphDataQualitySource(client)

    first = source.get_graph_connectedness()
    second = source.get_graph_connectedness()

    # Same dict object reused from cache
    assert first is second
    # session.run was called exactly once per label on the cold hit
    # and zero additional times on the warm hit.
    assert client.session.return_value.run.call_count == len(_CONNECTEDNESS_LABELS)


def test_connectedness_cypher_uses_portable_degree_expression():
    """The Cypher uses `size([(n)--() | 1])` (list-comprehension)
    instead of the removed-in-Neo4j-5 `size((n)--())`. Guards the
    original prod 500 — if someone changes the degree expression back
    to the broken form, this fails loudly."""
    source = GraphDataQualitySource(MagicMock())
    cypher = source._connectedness_cypher("Company")
    assert "size([(n)--() | 1])" in cypher
    assert "size((n)--())" not in cypher
