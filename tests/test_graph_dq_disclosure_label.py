"""Regression tests for the Disclosure-label DQ stats functions.

The audit on 2026-06-11 found that `get_lobbying_stats`,
`get_eu_knowledge_graph_stats`, and `get_cdp_stats` queried the
wrong Neo4j labels (`:Lobbyist`, `:CohesionProject`) and the wrong
property names (`l.country`, `c.cdp_score`, etc.) — none of which
exist in the graph, because the loaders standardised on
`(:Disclosure {system: 'X'})` with `detail_*`-prefixed properties.

Result: valid-200 JSON full of zeros while 17,313 eu-lobbying +
43,091 eu-cohesion disclosures sat in the graph. Smoke tests pass,
page renders, no alarm — the worst possible failure mode for a
transparency tool.

These tests pin the contract: when the graph has Disclosure nodes
for system X, the corresponding stats function cannot return
all-zeros. Fakes the Neo4j client with a session whose `run()`
inspects the Cypher and returns a non-empty row whenever the
expected MATCH shape is present; the stats function must read that
non-empty row.
"""
# pylint: disable=protected-access,missing-function-docstring
from __future__ import annotations

from unittest.mock import MagicMock

from src.data.graph.graph_data_quality import GraphDataQualitySource


def _client_for_system(system: str, *, count: int = 100) -> MagicMock:
    """Build a fake Neo4jClient whose session.run() returns rows that
    match the new ``(:Disclosure {system: 'X'})`` shape. Any query
    that doesn't reference that shape (e.g. the legacy
    ``:Lobbyist`` / ``:CohesionProject``) gets a zero row — so a
    regression that reverts to the old labels will fail loudly.
    """
    session = MagicMock()

    def run(cypher: str, *_args, **_kwargs):
        result = MagicMock()
        match_phrase = f"Disclosure {{system: '{system}'}}"
        if match_phrase not in cypher:
            # Legacy-label query path — return zero so the test fails.
            result.single.return_value = {
                "n": 0, "total": 0, "score": 0,
            }
            result.data.return_value = []
            return result
        # Default non-zero responses for the four shapes used by
        # the stats functions: scalar counts (`n`), scalar sums
        # (`total`), and tabular `data()` returns.
        result.single.return_value = {
            "n": count, "total": count * 1000, "score": 1,
        }
        result.data.return_value = [
            {"country": "DEU", "count": count, "fund": "ERDF",
             "n": count, "score": 1, "value": count, "date": "2024-01-01",
             "bucket": "10K-100K", "year": "2024"},
        ]
        return result

    session.run.side_effect = run
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)

    client = MagicMock()
    client.session.return_value = session
    return client


def test_lobbying_stats_reads_disclosure_with_eu_lobbying_system():
    source = GraphDataQualitySource(_client_for_system("eu-lobbying"))
    result = source.get_lobbying_stats()
    # The old code returned all-zeros against the populated graph.
    # The contract: with non-zero disclosure rows present, the
    # endpoint surfaces them — anything else is the regression.
    assert result["total"] > 0
    assert result["with_ep_passes"] > 0
    assert result["by_country"], "by_country must include rows"
    assert result["registrations_timeline"]
    assert result["cost_distribution"]


def test_eu_knowledge_graph_stats_reads_disclosure_with_eu_cohesion_system():
    source = GraphDataQualitySource(_client_for_system("eu-cohesion"))
    result = source.get_eu_knowledge_graph_stats()
    assert result["total_projects"] > 0
    assert result["with_budget"] > 0
    assert result["total_eu_contribution"] > 0
    assert result["by_fund"]
    assert result["by_country"]


def test_cdp_stats_reads_disclosure_with_cdp_system():
    # CDP has zero events emitted today (item #19) — the stats fn
    # will still legitimately return zeros in prod. Here we feed a
    # populated graph for system='cdp' just to verify the QUERY
    # shape matches; a non-zero graph must produce non-zero output.
    source = GraphDataQualitySource(_client_for_system("cdp"))
    result = source.get_cdp_stats()
    assert result["companies_with_score"] > 0
    assert result["score_distribution"]
    assert result["by_reporting_year"]


def test_lobbying_stats_returns_zeros_when_legacy_label_only():
    """If the queries had been left on `:Lobbyist`, the fake would
    return zero for every call (since it only populates rows for
    the new `Disclosure {system: 'eu-lobbying'}` shape). This test
    documents the failure mode the fix prevents — flip-of-the-coin
    canary that the regression test above is actually load-bearing.
    """
    # Build a client whose run() ALWAYS returns zero rows, mimicking
    # what the old `:Lobbyist`-based queries hit in prod.
    session = MagicMock()

    def run(_cypher: str, *_args, **_kwargs):
        result = MagicMock()
        result.single.return_value = {
            "n": 0, "total": 0, "score": 0,
        }
        result.data.return_value = []
        return result

    session.run.side_effect = run
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    client = MagicMock()
    client.session.return_value = session

    source = GraphDataQualitySource(client)
    result = source.get_lobbying_stats()
    assert result["total"] == 0
    assert result["match_rate"] == 0.0
