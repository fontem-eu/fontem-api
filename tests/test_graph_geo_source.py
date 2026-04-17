"""Tests for GraphGeoSource (Neo4j-backed GeoSource)."""
from unittest.mock import MagicMock

import pytest

from src.data.graph.graph_geo_source import GraphGeoSource


def _fake_neo4j(rows):
    """Return a Neo4jClient stub whose session.run returns the given rows."""
    session = MagicMock()
    result = MagicMock()
    result.data.return_value = rows
    session.run.return_value = result
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    client = MagicMock()
    client.session.return_value = session
    return client, session


def test_rejects_invalid_level():
    """Level outside 0..3 must raise ValueError."""
    source = GraphGeoSource(neo4j_client=MagicMock())
    with pytest.raises(ValueError, match="level"):
        source.aggregate_by_nuts(level=5, metric="companies")


def test_rejects_invalid_metric():
    """Unknown metric names must raise ValueError."""
    source = GraphGeoSource(neo4j_client=MagicMock())
    with pytest.raises(ValueError, match="metric"):
        source.aggregate_by_nuts(level=0, metric="bogus")


def test_level_3_requires_scope_nuts():
    """NUTS 3 queries are capped — scope_nuts is mandatory."""
    source = GraphGeoSource(neo4j_client=MagicMock())
    with pytest.raises(ValueError, match="scope_nuts"):
        source.aggregate_by_nuts(level=3, metric="companies")


def test_level_3_with_scope_passes_validation():
    """NUTS 3 with scope_nuts bound is a valid query."""
    client, session = _fake_neo4j([])
    source = GraphGeoSource(neo4j_client=client)
    result = source.aggregate_by_nuts(
        level=3, metric="companies", scope_nuts="DE1",
    )
    assert result == []
    # Query was executed once with scope binding
    assert session.run.call_count == 1
    params = session.run.call_args.kwargs
    assert params["level"] == 3
    assert params["scope"] == "DE1"


def test_connected_to_country_adds_path_filter():
    """Passing connected_to_country adds an EXISTS subquery filtering by country."""
    client, session = _fake_neo4j([])
    source = GraphGeoSource(neo4j_client=client)
    source.aggregate_by_nuts(
        level=0, metric="companies", connected_to_country="RUS",
    )
    query = session.run.call_args.args[0]
    # Query must include the path-existence filter
    assert "other.country = $connected_to" in query
    assert session.run.call_args.kwargs["connected_to"] == "RUS"


def test_query_changes_shape_by_metric():
    """Cypher differs per metric: count(e), count(ct), sum(value_eur)."""
    client, session = _fake_neo4j([])
    source = GraphGeoSource(neo4j_client=client)

    source.aggregate_by_nuts(level=0, metric="companies")
    companies_query = session.run.call_args.args[0]
    assert "count(DISTINCT e)" in companies_query

    source.aggregate_by_nuts(level=0, metric="contracts")
    contracts_query = session.run.call_args.args[0]
    assert "count(DISTINCT ct)" in contracts_query
    assert "Contract" in contracts_query

    source.aggregate_by_nuts(level=0, metric="contracts_eur")
    eur_query = session.run.call_args.args[0]
    assert "sum(toFloat(ct.value_eur))" in eur_query


def test_returns_normalized_rows():
    """Source reshapes Cypher result rows to {nuts_code,label,level,value}."""
    client, _ = _fake_neo4j([
        {"code": "DE", "name": "Deutschland", "level": 0, "value": 123},
    ])
    source = GraphGeoSource(neo4j_client=client)
    result = source.aggregate_by_nuts(level=0, metric="companies")
    assert result == [
        {"nuts_code": "DE", "label": "Deutschland", "level": 0, "value": 123},
    ]
