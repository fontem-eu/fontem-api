"""Dishka dependency injection providers for the GMR ETL API.

All data sources are APP-scoped singletons sharing a single Neo4jClient.
No per-request scoping is needed — the Neo4j driver manages its own
connection pool internally.
"""
# Each @provide here imports the concrete GraphXSource *inside* the factory:
# this keeps `from src.api.di import resolve_company_id` cheap (a number of
# tests do this) and avoids loading the heavy Neo4j adapters during the
# pre-fork import phase of uvicorn. The recommendations_source and
# ip_to_country forward-ref-then-import dance is the standard dishka pattern
# for breaking a circular import.
# pylint: disable=import-outside-toplevel,redefined-outer-name,reimported
from __future__ import annotations

import os
import re

from dishka import Provider, Scope, provide, make_async_container, AsyncContainer

from src.analysis.contract_data_source import ContractDataSource
from src.analysis.data_quality_source import DataQualitySource
from src.analysis.geo_source import GeoSource
from src.analysis.gmr_data_source import FinancialDataSource
from src.analysis.person_data_source import PersonDataSource
from src.data.graph.neo4j_client import Neo4jClient
from src.data.graph.graph_recommendations_source import GraphRecommendationsSource
from src.data.linguistics.client import LinguisticsClient
from src.data.sparql.virtuoso_client import VirtuosoClient
from src.services.ip_to_country import IpToCountryService


_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


class Neo4jProvider(Provider):
    """Single Neo4jClient shared by all data sources."""

    @provide(scope=Scope.APP)
    def neo4j_client(self) -> Neo4jClient:
        return Neo4jClient(
            uri=os.environ.get("NEO4J_URI"),
            user=os.environ.get("NEO4J_USER"),
            password=os.environ.get("NEO4J_PASSWORD"),
        )


class VirtuosoProvider(Provider):
    """Optional read-only SPARQL client for Virtuoso. Returns
    None when VIRTUOSO_SPARQL_URL is unset — callers (data-
    quality source today, future cross-store joins) handle the
    missing-client case as ‘feature unavailable’ rather than a
    boot-time failure.
    """

    @provide(scope=Scope.APP)
    def virtuoso_client(self) -> VirtuosoClient | None:
        return VirtuosoClient.from_env()


class LinguisticsProvider(Provider):
    """Optional keyword-extraction client. None when LINGUISTICS_URL is
    unset — search degrades to naive tokenization instead of failing."""

    @provide(scope=Scope.APP)
    def linguistics_client(self) -> LinguisticsClient | None:
        return LinguisticsClient.from_env()


class DataSourceProvider(Provider):
    """All data source singletons, sharing the single Neo4jClient."""

    @provide(scope=Scope.APP)
    def financial_data_source(self, neo4j: Neo4jClient) -> FinancialDataSource:
        from src.data.graph.graph_data_source import GraphDataSource
        return GraphDataSource(
            neo4j_client=neo4j,
            price_data_dir=os.environ.get("GMR_PRICE_DATA_DIR", "/edgar-data/prices"),
            edgar_data_dir=os.environ.get("GMR_EDGAR_LOCAL_DATA_DIR", "/edgar-data/full"),
        )

    @provide(scope=Scope.APP)
    def contract_data_source(
        self, neo4j: Neo4jClient, virtuoso: VirtuosoClient | None,
    ) -> ContractDataSource:
        """Company contracts come from Virtuoso so they aggregate across
        owl:sameAs; everything else still reads the graph store.

        A company page built on Neo4j alone shows one record's contracts
        and silently omits its duplicates', because Neo4j holds no
        equivalences — measured on prod, 3 contracts against 566 across
        the closure. Wrapping rather than replacing keeps every other
        read (and the SUBSIDIARY_OF walk the corporate group needs) on
        the store that is actually good at it, and makes the swap
        reversible by deleting one line.

        With no Virtuoso configured the wrapper delegates everything, so
        an environment that has not enabled it behaves exactly as before.
        """
        from src.data.graph.graph_contract_source import GraphContractSource
        from src.data.sparql.virtuoso_contract_source import VirtuosoContractSource
        return VirtuosoContractSource(
            fallback=GraphContractSource(neo4j_client=neo4j),
            virtuoso=virtuoso,
        )

    @provide(scope=Scope.APP)
    def data_quality_source(
        self,
        neo4j: Neo4jClient,
        virtuoso: VirtuosoClient | None,
    ) -> DataQualitySource:
        from src.data.graph.graph_data_quality import GraphDataQualitySource
        return GraphDataQualitySource(
            neo4j_client=neo4j, virtuoso_client=virtuoso,
        )

    @provide(scope=Scope.APP)
    def person_data_source(self, neo4j: Neo4jClient) -> PersonDataSource:
        from src.data.graph.graph_person_source import GraphPersonSource
        return GraphPersonSource(neo4j_client=neo4j)

    @provide(scope=Scope.APP)
    def geo_source(self, neo4j: Neo4jClient) -> GeoSource:
        from src.data.graph.graph_geo_source import GraphGeoSource
        return GraphGeoSource(neo4j_client=neo4j)

    @provide(scope=Scope.APP)
    def recommendations_source(
        self, neo4j: Neo4jClient,
    ) -> "GraphRecommendationsSource":  # noqa: F821 — forward ref imported below
        from src.data.graph.graph_recommendations_source import (
            GraphRecommendationsSource,
        )
        return GraphRecommendationsSource(neo4j_client=neo4j)

    @provide(scope=Scope.APP)
    def ip_to_country(self) -> "IpToCountryService":  # noqa: F821
        from src.services.ip_to_country import IpToCountryService
        return IpToCountryService()


def resolve_company_id(identifier: str, neo4j: Neo4jClient) -> dict:
    """Resolve a ticker or gmr_id to company info.

    Accepts the injected Neo4jClient instead of reaching into a global.
    """
    fallback = {"gmr_id": None, "ticker": identifier, "name": None}
    try:
        with neo4j.session() as session:
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


def make_container() -> AsyncContainer:
    """Build the full DI container for the GMR ETL API."""
    return make_async_container(
        Neo4jProvider(), VirtuosoProvider(), LinguisticsProvider(),
        DataSourceProvider(),
    )
