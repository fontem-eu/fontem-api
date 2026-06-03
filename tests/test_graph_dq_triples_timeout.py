"""Regression test for the triples-dashboard SPARQL timeout.

Pre-fix: `get_triples_stats` ran four full triple-store scans against
Virtuoso, each with the default 10s httpx timeout. On the production
Virtuoso the first COUNT(*) reliably blew past 10s and the
`httpx.ReadTimeout` propagated up to the FastAPI handler as a 500 —
making the dashboard panel show "Internal Server Error" indefinitely.

After the fix:
- VirtuosoClient defaults to 60s and surfaces `SparqlTimeout` on the
  ReadTimeout path.
- `get_triples_stats` catches `SparqlTimeout` and returns a graceful
  partial response (`available: True, total_triples: 0, error: ...`).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.data.graph.graph_data_quality import GraphDataQualitySource
from src.data.sparql.virtuoso_client import SparqlTimeout


def _make_dq(virtuoso=None) -> GraphDataQualitySource:
    """Build a GraphDataQualitySource with a stub Neo4j (unused by triples_stats)."""
    fake_neo4j = MagicMock()
    return GraphDataQualitySource(
        neo4j_client=fake_neo4j,
        virtuoso_client=virtuoso,
    )


def test_triples_stats_returns_unavailable_when_virtuoso_unconfigured():
    dq = _make_dq(virtuoso=None)
    result = dq.get_triples_stats()
    assert result["available"] is False
    assert result["total_triples"] == 0
    assert not result["graphs"]


def test_triples_stats_returns_partial_response_on_total_count_timeout():
    """The dashboard renders the graceful state rather than 500."""
    virtuoso = MagicMock()
    virtuoso.query.side_effect = SparqlTimeout("SPARQL query exceeded 60.0s")

    dq = _make_dq(virtuoso=virtuoso)
    result = dq.get_triples_stats()

    assert result["available"] is True
    assert result["total_triples"] == 0
    assert not result["graphs"]
    assert "60.0s" in result["error"]
    assert "generated_at" in result


def test_triples_stats_drops_class_breakdown_on_class_query_timeout():
    """If only the 4th (class) query times out we keep total + graphs
    + predicates and silently empty the class breakdown — partial data
    is more useful than no data."""
    virtuoso = MagicMock()
    virtuoso.query.side_effect = [
        [{"n": 10000}],  # 1. total
        [{"g": "http://data.fontem.eu/graph/sanctions", "n": 5000}],  # 2. per-graph
        [{"g": "http://data.fontem.eu/graph/sanctions",
          "p": "http://example.com/p", "n": 2000}],  # 3. predicates
        SparqlTimeout("classes timed out"),  # 4. class breakdown
    ]

    dq = _make_dq(virtuoso=virtuoso)
    result = dq.get_triples_stats()

    assert result["available"] is True
    assert result["total_triples"] == 10000
    assert len(result["graphs"]) == 1
    graph = result["graphs"][0]
    assert graph["triples"] == 5000
    assert graph["top_predicates"]  # kept
    assert not graph["classes"]   # dropped on timeout


def test_triples_stats_happy_path_aggregates_per_graph_predicates_and_classes():
    virtuoso = MagicMock()
    virtuoso.query.side_effect = [
        [{"n": 12000}],
        [
            {"g": "http://data.fontem.eu/graph/sanctions", "n": 8000},
            {"g": "http://data.fontem.eu/graph/filings", "n": 4000},
        ],
        [
            {"g": "http://data.fontem.eu/graph/sanctions",
             "p": "http://example.com/name", "n": 3000},
            {"g": "http://data.fontem.eu/graph/sanctions",
             "p": "http://example.com/regime", "n": 1500},
            {"g": "http://data.fontem.eu/graph/filings",
             "p": "http://example.com/filer", "n": 2000},
        ],
        [
            {"g": "http://data.fontem.eu/graph/sanctions",
             "type": "https://schema.org/Organization", "n": 2500},
            {"g": "http://data.fontem.eu/graph/filings",
             "type": "https://schema.org/Report", "n": 1200},
        ],
    ]

    dq = _make_dq(virtuoso=virtuoso)
    result = dq.get_triples_stats()

    assert result["available"] is True
    assert result["total_triples"] == 12000
    assert "error" not in result
    by_iri = {g["iri"]: g for g in result["graphs"]}
    sanctions = by_iri["http://data.fontem.eu/graph/sanctions"]
    assert sanctions["triples"] == 8000
    assert sanctions["label"] == "sanctions"
    assert [p["predicate"] for p in sanctions["top_predicates"]] == [
        "http://example.com/name", "http://example.com/regime",
    ]
    assert sanctions["classes"][0] == {
        "class": "https://schema.org/Organization", "n": 2500,
    }


def test_triples_stats_predicate_and_class_limit_kwargs_trim_per_graph_lists():
    virtuoso = MagicMock()
    virtuoso.query.side_effect = [
        [{"n": 100}],
        [{"g": "http://data.fontem.eu/graph/x", "n": 100}],
        [
            {"g": "http://data.fontem.eu/graph/x",
             "p": f"http://example.com/p{i}", "n": 100 - i}
            for i in range(5)
        ],
        [
            {"g": "http://data.fontem.eu/graph/x",
             "type": f"http://example.com/c{i}", "n": 100 - i}
            for i in range(4)
        ],
    ]

    dq = _make_dq(virtuoso=virtuoso)
    result = dq.get_triples_stats(predicate_limit=2, class_limit=1)

    [graph] = result["graphs"]
    assert len(graph["top_predicates"]) == 2
    assert len(graph["classes"]) == 1
