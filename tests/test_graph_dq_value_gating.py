"""The contract-value DQ aggregations must exclude low-confidence values.

After the value-confidence work, a contract flagged value_low_confidence
is kept in the graph but must not contribute to headline value totals
(country totals, currency breakdown, value timeline, coverage). These
tests pin that the gating predicate is present in those queries — so a
future refactor can't silently let an impossible value back into a
country total — and that contracts stay COUNTED (only their money is
dropped).
"""
# pylint: disable=protected-access
from __future__ import annotations

from unittest.mock import MagicMock

from src.data.graph.graph_data_quality import GraphDataQualitySource


def _capturing_client():
    """Fake Neo4jClient whose session.run() records every Cypher string
    and returns an empty result (the queries' shape is what we assert,
    not their data)."""
    queries: list[str] = []
    session = MagicMock()

    def run(cypher, *_a, **_k):
        queries.append(cypher)
        result = MagicMock()
        result.data.return_value = []
        result.single.return_value = {"n": 0}
        return result

    session.run.side_effect = run
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    client = MagicMock()
    client.session.return_value = session
    return client, queries


_GATE = "value_low_confidence"


def test_by_country_value_is_confidence_gated():
    client, queries = _capturing_client()
    GraphDataQualitySource(client).get_contracts_by_country()
    value_q = next(q for q in queries if "total_eur" in q)
    assert _GATE in value_q
    # contracts are counted once per underlying contract (canonical only,
    # collapse_modifications) and the count itself is not confidence-gated.
    assert "THEN 1 ELSE 0 END)" in value_q
    assert "ct.is_current" in value_q


def test_value_timeline_is_confidence_gated():
    client, queries = _capturing_client()
    GraphDataQualitySource(client).get_contracts_value_timeline()
    assert any(_GATE in q for q in queries)


def test_currency_quality_value_is_confidence_gated():
    client, queries = _capturing_client()
    GraphDataQualitySource(client).get_contracts_currency_quality()
    value_q = next((q for q in queries if "total_eur" in q), "")
    assert _GATE in value_q


def test_coverage_country_and_cpv_values_are_gated():
    client, queries = _capturing_client()
    GraphDataQualitySource(client).get_coverage_stats()
    value_queries = [q for q in queries if "total_value" in q]
    assert value_queries, "coverage must run value-sum queries"
    assert all(_GATE in q for q in value_queries)


def test_value_quality_surface_returns_flagged_breakdown():
    client, queries = _capturing_client()
    out = GraphDataQualitySource(client).get_contracts_value_quality()
    assert set(out) >= {
        "total", "flagged_low_confidence", "with_payable_discrepancy",
        "low_confidence_pct", "by_flag", "top_flagged",
    }
    # The flagged-set query filters on the gate.
    assert any(_GATE in q for q in queries)
