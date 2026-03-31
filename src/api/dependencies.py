"""
FastAPI dependencies — provides data sources for all endpoints.

All data flows through Neo4j-backed GraphDataSource. No legacy routing.
In unit tests, override via app.dependency_overrides[get_data_source].
"""
from __future__ import annotations

import os
import re
from functools import lru_cache

from src.analysis.gmr_data_source import FinancialDataSource


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
    """Dependency injected into financial endpoints."""
    return _graph_source()


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


@lru_cache(maxsize=1)
def _person_source():
    from src.data.graph.graph_person_source import GraphPersonSource  # pylint: disable=import-outside-toplevel
    from src.data.graph.neo4j_client import Neo4jClient  # pylint: disable=import-outside-toplevel
    return GraphPersonSource(neo4j_client=Neo4jClient())


def get_person_source():
    """Dependency injected into person endpoints."""
    return _person_source()


_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


def resolve_company_id(identifier: str) -> dict:
    """Resolve a ticker or gmr_id to company info.

    Returns dict with gmr_id, ticker, name. Works for both listed
    companies (by ticker) and procurement-only companies (by gmr_id).
    Falls back gracefully if Neo4j is unavailable (e.g. in tests).
    """
    fallback = {"gmr_id": None, "ticker": identifier, "name": None}
    try:
        source = _graph_source()
    except Exception:  # pylint: disable=broad-exception-caught
        return fallback

    try:
        with source._neo4j.session() as session:  # pylint: disable=protected-access
            if _UUID_RE.match(identifier):
                row = session.run(
                    "MATCH (c:Company {gmr_id: $gid}) "
                    "OPTIONAL MATCH (c)-[:LISTED_AS]->(l:Listing) "
                    "RETURN c.gmr_id AS gmr_id, c.name AS name, "
                    "  l.ticker AS ticker LIMIT 1",
                    gid=identifier,
                ).single()
            else:
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
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    return fallback
