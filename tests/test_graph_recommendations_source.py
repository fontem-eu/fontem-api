"""Tests for GraphRecommendationsSource — the country-scoped top-N
queries powering the Public Spending landing."""
from unittest.mock import MagicMock

from src.data.graph.graph_recommendations_source import (
    GraphRecommendationsSource,
)


def _fake_neo4j(rows):
    """Return a Neo4jClient stub whose session.run returns ``rows``."""
    session = MagicMock()
    result = MagicMock()
    result.data.return_value = rows
    session.run.return_value = result
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    client = MagicMock()
    client.session.return_value = session
    return client, session


def test_top_companies_in_country_passes_alpha2_to_cypher():
    """`country_alpha3` is converted to alpha-2 for the NUTSRegion
    `code` lookup. Greece + UK use the EU/NUTS quirks (EL, UK)."""
    client, session = _fake_neo4j([])
    src = GraphRecommendationsSource(neo4j_client=client)

    src.top_companies_in_country("PRT", limit=10)
    assert session.run.call_args.args[1] == {"alpha2": "PT", "limit": 10}

    src.top_companies_in_country("GRC", limit=10)
    # Greece's NUTS code is "EL", not "GR".
    assert session.run.call_args.args[1] == {"alpha2": "EL", "limit": 10}

    src.top_companies_in_country("GBR", limit=10)
    # UK uses "UK" in NUTS contexts.
    assert session.run.call_args.args[1] == {"alpha2": "UK", "limit": 10}


def test_top_companies_returns_empty_for_unknown_alpha3():
    """If LocationService can't map the alpha-3 to alpha-2 we bail
    out early without touching Neo4j — protects the driver from
    nonsense queries."""
    client, _ = _fake_neo4j([])
    src = GraphRecommendationsSource(neo4j_client=client)
    assert src.top_companies_in_country("ZZZ", limit=5) == []
    client.session.assert_not_called()


def test_top_companies_shape():
    rows = [
        {"id": "abc", "name": "Foo Lda", "total_value": 100_000.0, "contract_count": 12},
        {"id": "def", "name": "Bar SA",  "total_value":  50_000.0, "contract_count":  3},
    ]
    client, _ = _fake_neo4j(rows)
    src = GraphRecommendationsSource(neo4j_client=client)
    out = src.top_companies_in_country("PRT", limit=10)
    assert out == [
        {"id": "abc", "name": "Foo Lda", "total_value_eur": 100_000.0, "contract_count": 12},
        {"id": "def", "name": "Bar SA",  "total_value_eur":  50_000.0, "contract_count":  3},
    ]


def test_top_authorities_passes_alpha3_to_cypher():
    """Authorities are stored with alpha-3 directly on `Authority.country`,
    so the query parameter is the raw alpha-3 — no mapping."""
    client, session = _fake_neo4j([])
    src = GraphRecommendationsSource(neo4j_client=client)
    src.top_authorities_in_country("PRT", limit=5)
    assert session.run.call_args.args[1] == {"country": "PRT", "limit": 5}


def test_top_authorities_shape():
    rows = [
        {"id": "auth1", "name": "Município X", "total_value": 8_000_000.0, "contract_count": 240},
    ]
    client, _ = _fake_neo4j(rows)
    src = GraphRecommendationsSource(neo4j_client=client)
    out = src.top_authorities_in_country("PRT", limit=10)
    assert out == [
        {
            "id": "auth1",
            "name": "Município X",
            "total_value_eur": 8_000_000.0,
            "contract_count": 240,
        },
    ]
