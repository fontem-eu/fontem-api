"""Graph Data Quality Source — Neo4j-backed data quality metrics."""
from __future__ import annotations

import logging

from ...analysis.data_quality_source import DataQualitySource
from .neo4j_client import Neo4jClient

logger = logging.getLogger(__name__)


class GraphDataQualitySource(DataQualitySource):
    """Production data quality source backed by Neo4j."""

    def __init__(self, neo4j_client: Neo4jClient) -> None:
        self._neo4j = neo4j_client

    def get_graph_stats(self) -> dict:
        """Return node/relationship counts by label."""
        with self._neo4j.session() as session:
            labels = {}
            for label in [
                "Company", "Listing", "FinancialYear",
                "Contract", "Authority", "CPV",
            ]:
                n = session.run(
                    f"MATCH (n:{label}) RETURN count(n) AS n"
                ).single()["n"]
                labels[label] = n

            rels = session.run(
                "MATCH ()-[r]->() RETURN count(r) AS n"
            ).single()["n"]

        return {"nodes": labels, "relationships": rels}

    def get_matching_stats(self) -> dict:
        """Return entity resolution metrics."""
        with self._neo4j.session() as session:
            # SAME_AS queue
            same_as_pending = session.run(
                "MATCH ()-[r:SAME_AS {reviewed: false}]->() "
                "RETURN count(r) AS n"
            ).single()["n"]

            same_as_total = session.run(
                "MATCH ()-[r:SAME_AS]->() RETURN count(r) AS n"
            ).single()["n"]

            # Companies with VAT (matched via procurement)
            with_vat = session.run(
                "MATCH (c:Company) WHERE c.vat IS NOT NULL "
                "RETURN count(c) AS n"
            ).single()["n"]

            # Companies with LEI (from GLEIF)
            with_lei = session.run(
                "MATCH (c:Company) WHERE c.lei IS NOT NULL "
                "RETURN count(c) AS n"
            ).single()["n"]

            # Procurement-only companies (have contracts but no listing)
            procurement_only = session.run(
                "MATCH (ct:Contract)-[:AWARDED_TO]->(c:Company) "
                "WHERE NOT (c)-[:LISTED_AS]->() "
                "RETURN count(DISTINCT c) AS n"
            ).single()["n"]

        return {
            "same_as_pending": same_as_pending,
            "same_as_total": same_as_total,
            "companies_with_vat": with_vat,
            "companies_with_lei": with_lei,
            "procurement_only_companies": procurement_only,
        }

    def get_data_freshness(self) -> dict:
        """Return freshness info."""
        with self._neo4j.session() as session:
            latest_contract = session.run(
                "MATCH (ct:Contract) "
                "WHERE ct.loaded_at IS NOT NULL "
                "RETURN ct.loaded_at AS loaded_at "
                "ORDER BY ct.loaded_at DESC LIMIT 1"
            ).single()

            contract_date_range = session.run(
                "MATCH (ct:Contract) "
                "WHERE ct.publication_date IS NOT NULL "
                "RETURN min(ct.publication_date) AS earliest, "
                "  max(ct.publication_date) AS latest"
            ).single()

            financial_sources = session.run(
                "MATCH (f:FinancialYear) "
                "RETURN f.source AS source, count(f) AS n "
                "ORDER BY n DESC"
            ).data()

        return {
            "latest_contract_load": (
                latest_contract["loaded_at"]
                if latest_contract else None
            ),
            "contract_date_range": {
                "earliest": (
                    contract_date_range["earliest"]
                    if contract_date_range else None
                ),
                "latest": (
                    contract_date_range["latest"]
                    if contract_date_range else None
                ),
            },
            "financial_sources": financial_sources,
        }

    def get_coverage_stats(self) -> dict:
        """Return coverage metrics."""
        with self._neo4j.session() as session:
            # Companies with at least one contract
            companies_with_contracts = session.run(
                "MATCH (ct:Contract)-[:AWARDED_TO]->(c:Company) "
                "RETURN count(DISTINCT c) AS n"
            ).single()["n"]

            # Contracts by country (top 10)
            by_country = session.run(
                "MATCH (ct:Contract) "
                "WHERE ct.country IS NOT NULL "
                "RETURN ct.country AS country, count(ct) AS contracts, "
                "  sum(ct.value_eur) AS total_value "
                "ORDER BY contracts DESC LIMIT 15"
            ).data()

            # Top CPV sectors
            top_cpv = session.run(
                "MATCH (ct:Contract)-[:CATEGORIZED_AS]->(cpv:CPV) "
                "RETURN cpv.code AS code, cpv.description AS description, "
                "  count(ct) AS contracts, sum(ct.value_eur) AS total_value "
                "ORDER BY contracts DESC LIMIT 10"
            ).data()

            # Authorities count
            authority_count = session.run(
                "MATCH (a:Authority) RETURN count(a) AS n"
            ).single()["n"]

        return {
            "companies_with_contracts": companies_with_contracts,
            "contracts_by_country": by_country,
            "top_cpv_sectors": top_cpv,
            "authority_count": authority_count,
        }
