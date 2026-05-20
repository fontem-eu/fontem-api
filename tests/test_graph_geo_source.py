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


# ── aggregate_entity_by_nuts ─────────────────────────────────────────────


def test_entity_rejects_invalid_level():
    source = GraphGeoSource(neo4j_client=MagicMock())
    with pytest.raises(ValueError, match="level"):
        source.aggregate_entity_by_nuts(entity_id="abc", level=9, metric="contracts")


def test_entity_rejects_invalid_metric():
    source = GraphGeoSource(neo4j_client=MagicMock())
    with pytest.raises(ValueError, match="metric"):
        source.aggregate_entity_by_nuts(entity_id="abc", level=0, metric="companies")


def _fake_neo4j_multi(side_effects):
    """Return a Neo4j stub whose session.run returns successive result sets."""
    session = MagicMock()
    results = []
    for rows in side_effects:
        r = MagicMock()
        r.data.return_value = rows
        results.append(r)
    session.run.side_effect = results
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    client = MagicMock()
    client.session.return_value = session
    return client, session


def test_entity_contracts_count_query():
    """metric=contracts must use count(DISTINCT ct) in the country aggregation query."""
    client, session = _fake_neo4j_multi([[], []])  # country query → empty → authority fallback
    source = GraphGeoSource(neo4j_client=client)
    source.aggregate_entity_by_nuts(entity_id="abc-123", level=0, metric="contracts")
    first_query = session.run.call_args_list[0].args[0]
    assert "count(DISTINCT ct)" in first_query
    assert "Company" in first_query


def test_entity_contracts_eur_query():
    """metric=contracts_eur must use sum(toFloat(ct.value_eur))."""
    client, session = _fake_neo4j_multi([[], []])
    source = GraphGeoSource(neo4j_client=client)
    source.aggregate_entity_by_nuts(entity_id="abc-123", level=0, metric="contracts_eur")
    first_query = session.run.call_args_list[0].args[0]
    assert "sum(toFloat(ct.value_eur))" in first_query


def test_entity_scope_filter_converts_nuts_to_alpha3():
    """For level > 0 with scope_nuts='DE', scope_a3 passed is 'DEU'."""
    client, session = _fake_neo4j_multi([[], []])
    source = GraphGeoSource(neo4j_client=client)
    source.aggregate_entity_by_nuts(
        entity_id="abc", level=1, metric="contracts", scope_nuts="DE"
    )
    # Country query receives scope_a3 = "DEU" (alpha-3 for Germany)
    params = session.run.call_args_list[0].kwargs
    assert params["scope_a3"] == "DEU"


def test_entity_falls_back_to_authority_query_when_company_empty():
    """If the company-country query returns no rows, tries the authority path."""
    client, session = _fake_neo4j_multi([
        [],  # company country query → no rows
        [{"code": "DE", "name": "Deutschland", "level": 0, "value": 5}],  # authority
    ])
    source = GraphGeoSource(neo4j_client=client)
    result = source.aggregate_entity_by_nuts(
        entity_id="ORG-001", level=0, metric="contracts"
    )
    assert len(result) == 1
    assert result[0]["nuts_code"] == "DE"
    authority_query = session.run.call_args_list[1].args[0]
    assert "authority_id" in authority_query


def test_entity_returns_normalized_rows():
    """Company path: alpha-3 country rows mapped to normalized NUTS output."""
    client, _session = _fake_neo4j_multi([
        [{"country_a3": "DEU", "value": 42}],               # country agg query
        [{"code": "DE", "name": "Deutschland", "level": 0}],  # region name lookup
    ])
    source = GraphGeoSource(neo4j_client=client)
    result = source.aggregate_entity_by_nuts(
        entity_id="some-gmr-id", level=0, metric="contracts"
    )
    assert result == [
        {"nuts_code": "DE", "label": "Deutschland", "level": 0, "value": 42}
    ]


def test_entity_grc_mapped_to_el():
    """GRC (Greece) authority country must be mapped to NUTS code 'EL'."""
    client, _session = _fake_neo4j_multi([
        [{"country_a3": "GRC", "value": 3}],
        [{"code": "EL", "name": "Ellada", "level": 0}],
    ])
    source = GraphGeoSource(neo4j_client=client)
    result = source.aggregate_entity_by_nuts(
        entity_id="xyz", level=0, metric="contracts"
    )
    assert result[0]["nuts_code"] == "EL"


def test_entity_gbr_mapped_to_uk():
    """GBR (UK) authority country must be mapped to NUTS code 'UK', not 'GB'."""
    client, _session = _fake_neo4j_multi([
        [{"country_a3": "GBR", "value": 7}],
        [{"code": "UK", "name": "United Kingdom", "level": 0}],
    ])
    source = GraphGeoSource(neo4j_client=client)
    result = source.aggregate_entity_by_nuts(
        entity_id="xyz", level=0, metric="contracts"
    )
    assert result[0]["nuts_code"] == "UK"


def test_entity_level_gt_0_returns_empty_for_company_entity():
    """For level > 0, company path returns empty (no sub-national authority data)."""
    client, _session = _fake_neo4j_multi([
        [{"country_a3": "DEU", "value": 10}],  # has country data but level=1
    ])
    source = GraphGeoSource(neo4j_client=client)
    result = source.aggregate_entity_by_nuts(
        entity_id="comp-1", level=1, metric="contracts", scope_nuts="DE"
    )
    assert result == []  # no NUTS 1 breakdown without authority LOCATED_IN edges
