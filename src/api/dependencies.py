"""
FastAPI dependency that provides the application's FinancialDataSource.

Set GMR_DATA_SOURCE=graph to use the Neo4j-backed GraphDataSource.
Any other value (or unset) uses the legacy RoutingDataSource.

In unit tests, override via app.dependency_overrides[get_data_source].
"""
from __future__ import annotations

import os
from functools import lru_cache

from src.analysis.gmr_data_source import FinancialDataSource
from src.data.europe.esef_data_source import EsefDataSource
from src.data.north_america.live_data_source import LiveDataSource
from src.data.routing_data_source import RoutingDataSource


@lru_cache(maxsize=1)
def _routing_source() -> RoutingDataSource:
    local_data_dir = os.environ.get("GMR_EDGAR_LOCAL_DATA_DIR", "/edgar-data/full")
    local_price_data_dir = os.environ.get("GMR_PRICE_DATA_DIR", "/edgar-data/prices")
    esef_data_dir = os.environ.get("GMR_ESEF_DATA_DIR", "/esef-data/esef")
    return RoutingDataSource(
        na_source=LiveDataSource(
            local_data_dir=local_data_dir,
            local_price_data_dir=local_price_data_dir,
        ),
        eu_source=EsefDataSource(esef_data_dir=esef_data_dir),
    )


@lru_cache(maxsize=1)
def _graph_source() -> FinancialDataSource:
    from src.data.graph.graph_data_source import GraphDataSource  # pylint: disable=import-outside-toplevel
    from src.data.graph.neo4j_client import Neo4jClient  # pylint: disable=import-outside-toplevel
    return GraphDataSource(
        neo4j_client=Neo4jClient(),
        price_data_dir=os.environ.get("GMR_PRICE_DATA_DIR", "/edgar-data/prices"),
        edgar_data_dir=os.environ.get("GMR_EDGAR_LOCAL_DATA_DIR", "/edgar-data/full"),
    )


def get_data_source() -> FinancialDataSource:
    """
    Dependency injected into financial endpoints.
    Override in tests via app.dependency_overrides[get_data_source].
    """
    if os.environ.get("GMR_DATA_SOURCE") == "graph":
        return _graph_source()
    return _routing_source()


@lru_cache(maxsize=1)
def _contract_source():
    from src.data.graph.graph_contract_source import GraphContractSource  # pylint: disable=import-outside-toplevel
    from src.data.graph.neo4j_client import Neo4jClient  # pylint: disable=import-outside-toplevel
    return GraphContractSource(neo4j_client=Neo4jClient())


def get_contract_source():
    """Dependency injected into contract endpoints."""
    return _contract_source()


@lru_cache(maxsize=1)
def _data_quality_source():
    from src.data.graph.graph_data_quality import GraphDataQualitySource  # pylint: disable=import-outside-toplevel
    from src.data.graph.neo4j_client import Neo4jClient  # pylint: disable=import-outside-toplevel
    return GraphDataQualitySource(neo4j_client=Neo4jClient())


def get_data_quality_source():
    """Dependency injected into data quality endpoints."""
    return _data_quality_source()


def resolve_company_id(identifier: str) -> dict:
    """Resolve a ticker or gmr_id to company info.

    Returns dict with gmr_id, ticker, name. Works for both listed
    companies (by ticker) and procurement-only companies (by gmr_id).
    """
    import re  # pylint: disable=import-outside-toplevel
    source = _graph_source() if os.environ.get("GMR_DATA_SOURCE") == "graph" else None
    if not source:
        return {"gmr_id": None, "ticker": identifier, "name": None}

    uuid_re = re.compile(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
        re.IGNORECASE,
    )

    with source._neo4j.session() as session:  # pylint: disable=protected-access
        if uuid_re.match(identifier):
            # It's a gmr_id — look up directly
            row = session.run(
                "MATCH (c:Company {gmr_id: $gid}) "
                "OPTIONAL MATCH (c)-[:LISTED_AS]->(l:Listing) "
                "RETURN c.gmr_id AS gmr_id, c.name AS name, "
                "  l.ticker AS ticker LIMIT 1",
                gid=identifier,
            ).single()
        else:
            # It's a ticker — resolve via Listing
            row = session.run(
                "MATCH (l:Listing {ticker: $t})<-[:LISTED_AS]-(c:Company) "
                "RETURN c.gmr_id AS gmr_id, c.name AS name, "
                "  l.ticker AS ticker",
                t=identifier.upper(),
            ).single()

    if row:
        return {
            "gmr_id": row["gmr_id"],
            "ticker": row["ticker"],
            "name": row["name"],
        }
    return {"gmr_id": None, "ticker": identifier, "name": None}
