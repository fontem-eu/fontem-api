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
                "Person", "Lobbyist", "LobbyInterest",
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

    # ── Per-pipeline queries ──────────────────────────────────────

    def get_contracts_timeline(self) -> list[dict]:
        """Daily contract counts by publication_date."""
        with self._neo4j.session() as session:
            return session.run(
                "MATCH (ct:Contract) "
                "WHERE ct.publication_date IS NOT NULL "
                "RETURN left(ct.publication_date, 10) AS date, count(ct) AS value "
                "ORDER BY date"
            ).data()

    def get_contracts_by_country(self) -> list[dict]:
        """Contract count and total EUR per country."""
        with self._neo4j.session() as session:
            return session.run(
                "MATCH (ct:Contract) "
                "WHERE ct.country IS NOT NULL "
                "RETURN ct.country AS country, count(ct) AS contracts, "
                "  sum(ct.value_eur) AS total_eur "
                "ORDER BY contracts DESC"
            ).data()

    def get_contracts_currency_quality(self) -> dict:
        """Currency-related data quality metrics."""
        with self._neo4j.session() as session:
            total = session.run(
                "MATCH (ct:Contract) RETURN count(ct) AS n"
            ).single()["n"]
            undisclosed = session.run(
                "MATCH (ct:Contract) WHERE ct.value_undisclosed = true RETURN count(ct) AS n"
            ).single()["n"]
            inferred = session.run(
                "MATCH (ct:Contract) WHERE ct.currency_inferred = true RETURN count(ct) AS n"
            ).single()["n"]
            converted = session.run(
                "MATCH (ct:Contract) WHERE ct.value_eur IS NOT NULL RETURN count(ct) AS n"
            ).single()["n"]
            with_currency = session.run(
                "MATCH (ct:Contract) WHERE ct.value_currency IS NOT NULL RETURN count(ct) AS n"
            ).single()["n"]
            by_currency = session.run(
                "MATCH (ct:Contract) WHERE ct.value_currency IS NOT NULL "
                "RETURN ct.value_currency AS currency, count(ct) AS contracts, "
                "  sum(ct.value_eur) AS total_eur "
                "ORDER BY contracts DESC LIMIT 25"
            ).data()
            return {
                "total": total,
                "value_undisclosed": undisclosed,
                "currency_inferred": inferred,
                "converted_to_eur": converted,
                "with_currency": with_currency,
                "by_currency": by_currency,
            }

    def get_contracts_nulls(self) -> dict:
        """Count of contracts missing key fields."""
        with self._neo4j.session() as session:
            total = session.run(
                "MATCH (ct:Contract) RETURN count(ct) AS n"
            ).single()["n"]
            fields = {}
            for field in ["value_eur", "cpv_main", "award_date", "description", "country"]:
                n = session.run(
                    f"MATCH (ct:Contract) WHERE ct.{field} IS NULL "
                    "RETURN count(ct) AS n"
                ).single()["n"]
                fields[field] = n
            return {"total": total, "missing": fields}

    def get_contracts_value_timeline(self) -> list[dict]:
        """Daily total EUR value of contracts."""
        with self._neo4j.session() as session:
            return session.run(
                "MATCH (ct:Contract) "
                "WHERE ct.publication_date IS NOT NULL AND ct.value_eur IS NOT NULL "
                "RETURN left(ct.publication_date, 10) AS date, sum(ct.value_eur) AS value "
                "ORDER BY date"
            ).data()

    def get_gleif_stats(self) -> dict:
        """GLEIF-specific stats: active/inactive, LEI coverage, relationships."""
        with self._neo4j.session() as session:
            total = session.run(
                "MATCH (c:Company) RETURN count(c) AS n"
            ).single()["n"]
            with_lei = session.run(
                "MATCH (c:Company) WHERE c.lei IS NOT NULL RETURN count(c) AS n"
            ).single()["n"]
            active = session.run(
                "MATCH (c:Company {active: true}) RETURN count(c) AS n"
            ).single()["n"]
            subsidiaries = session.run(
                "MATCH ()-[r:SUBSIDIARY_OF]->() RETURN count(r) AS n"
            ).single()["n"]
            orphan_subs = session.run(
                "MATCH (c:Company)-[r:SUBSIDIARY_OF]->(p) "
                "WHERE NOT exists((p)-[:LISTED_AS]->()) AND p.lei IS NULL "
                "RETURN count(r) AS n"
            ).single()["n"]
            by_country = session.run(
                "MATCH (c:Company) WHERE c.country IS NOT NULL "
                "RETURN c.country AS country, count(c) AS count "
                "ORDER BY count DESC LIMIT 30"
            ).data()
            return {
                "total": total, "with_lei": with_lei, "active": active,
                "inactive": total - active,
                "subsidiary_links": subsidiaries,
                "orphan_subsidiaries": orphan_subs,
                "by_country": by_country,
            }

    def get_edgar_stats(self) -> dict:
        """US EDGAR financial data stats."""
        with self._neo4j.session() as session:
            companies = session.run(
                "MATCH (c:Company)-[:LISTED_AS]->(l:Listing {exchange: 'US'}) "
                "RETURN count(DISTINCT c) AS n"
            ).single()["n"]
            fin_years = session.run(
                "MATCH (f:FinancialYear {source: 'EDGAR'}) RETURN count(f) AS n"
            ).single()["n"]
            by_year = session.run(
                "MATCH (c:Company)-[:REPORTED]->(f:FinancialYear {source: 'EDGAR'}) "
                "WHERE f.year >= 1990 AND f.year <= 2030 "
                "RETURN toString(f.year) + '-01-01' AS date, count(f) AS value ORDER BY date"
            ).data()
            # Field coverage
            fields_coverage = {}
            for field in ["revenue", "net_income", "total_assets", "equity", "operating_cashflow"]:
                n = session.run(
                    f"MATCH (f:FinancialYear {{source: 'EDGAR'}}) "
                    f"WHERE f.{field} IS NOT NULL RETURN count(f) AS n"
                ).single()["n"]
                fields_coverage[field] = round(n / max(fin_years, 1) * 100, 1)
            return {
                "companies": companies, "financial_years": fin_years,
                "by_year": by_year, "field_coverage": fields_coverage,
            }

    _EU_MEMBERS = (
        "'AT','BE','BG','HR','CY','CZ','DK','EE','FI','FR','DE','GR',"
        "'HU','IE','IT','LV','LT','LU','MT','NL','PL','PT','RO','SK',"
        "'SI','ES','SE'"
    )

    def get_esef_stats(self) -> dict:
        """EU ESEF financial data stats (EU members only)."""
        eu_filter = f"WHERE c.country IN [{self._EU_MEMBERS}]"
        with self._neo4j.session() as session:
            companies = session.run(
                f"MATCH (c:Company)-[:REPORTED]->(f:FinancialYear {{source: 'ESEF'}}) "
                f"{eu_filter} RETURN count(DISTINCT c) AS n"
            ).single()["n"]
            fin_years = session.run(
                f"MATCH (c:Company)-[:REPORTED]->(f:FinancialYear {{source: 'ESEF'}}) "
                f"{eu_filter} RETURN count(f) AS n"
            ).single()["n"]
            by_year = session.run(
                f"MATCH (c:Company)-[:REPORTED]->(f:FinancialYear {{source: 'ESEF'}}) "
                f"{eu_filter} AND f.year >= 1990 AND f.year <= 2030 "
                "RETURN toString(f.year) + '-01-01' AS date, count(f) AS value ORDER BY date"
            ).data()
            by_country = session.run(
                f"MATCH (c:Company)-[:REPORTED]->(f:FinancialYear {{source: 'ESEF'}}) "
                f"{eu_filter} "
                "RETURN c.country AS country, count(f) AS count "
                "ORDER BY count DESC LIMIT 20"
            ).data()
            fields_coverage = {}
            for field in ["revenue", "net_income", "total_assets", "equity", "operating_cashflow"]:
                n = session.run(
                    f"MATCH (c:Company)-[:REPORTED]->(f:FinancialYear {{source: 'ESEF'}}) "
                    f"{eu_filter} AND f.{field} IS NOT NULL RETURN count(f) AS n"
                ).single()["n"]
                fields_coverage[field] = round(n / max(fin_years, 1) * 100, 1)
            return {
                "companies": companies, "financial_years": fin_years,
                "by_year": by_year, "by_country": by_country,
                "field_coverage": fields_coverage,
            }

    def get_lobbying_stats(self) -> dict:
        """EU Transparency Register stats."""
        with self._neo4j.session() as session:
            total = session.run("MATCH (l:Lobbyist) RETURN count(l) AS n").single()["n"]
            with_ep = session.run(
                "MATCH (l:Lobbyist) WHERE l.ep_passes > 0 RETURN count(l) AS n"
            ).single()["n"]
            matched = session.run(
                "MATCH (l:Lobbyist)-[:REPRESENTS]->() RETURN count(DISTINCT l) AS n"
            ).single()["n"]
            by_country = session.run(
                "MATCH (l:Lobbyist) WHERE l.country IS NOT NULL "
                "RETURN l.country AS country, count(l) AS count "
                "ORDER BY count DESC LIMIT 20"
            ).data()
            registrations = session.run(
                "MATCH (l:Lobbyist) "
                "WHERE l.registration_date IS NOT NULL AND size(l.registration_date) >= 7 "
                "RETURN left(l.registration_date, 7) + '-01' AS date, count(l) AS value "
                "ORDER BY date"
            ).data()
            cost_ranges = session.run(
                "MATCH (l:Lobbyist) WHERE l.cost_max IS NOT NULL "
                "RETURN CASE "
                "  WHEN l.cost_max < 10000 THEN '<10K' "
                "  WHEN l.cost_max < 100000 THEN '10K-100K' "
                "  WHEN l.cost_max < 1000000 THEN '100K-1M' "
                "  ELSE '>1M' END AS bucket, count(l) AS count "
                "ORDER BY count DESC"
            ).data()
            return {
                "total": total, "with_ep_passes": with_ep,
                "matched_to_company": matched,
                "match_rate": round(matched / max(total, 1) * 100, 1),
                "by_country": by_country,
                "registrations_timeline": registrations,
                "cost_distribution": cost_ranges,
            }

    def get_directors_stats(self) -> dict:
        """French directors / person data stats."""
        with self._neo4j.session() as session:
            persons = session.run("MATCH (p:Person) RETURN count(p) AS n").single()["n"]
            links = session.run(
                "MATCH ()-[r:DIRECTS]->() RETURN count(r) AS n"
            ).single()["n"]
            companies_with = session.run(
                "MATCH (p:Person)-[:DIRECTS]->(c:Company) "
                "RETURN count(DISTINCT c) AS n"
            ).single()["n"]
            roles = session.run(
                "MATCH ()-[r:DIRECTS]->() WHERE r.role IS NOT NULL "
                "RETURN r.role AS label, count(r) AS value "
                "ORDER BY value DESC LIMIT 10"
            ).data()
            with_birth = session.run(
                "MATCH (p:Person) WHERE p.birth_year IS NOT NULL RETURN count(p) AS n"
            ).single()["n"]
            return {
                "persons": persons, "director_links": links,
                "companies_with_directors": companies_with,
                "birth_year_coverage": round(with_birth / max(persons, 1) * 100, 1),
                "roles": roles,
            }

    def get_trade_edges_stats(self) -> dict:
        """Materialized trade edge stats."""
        with self._neo4j.session() as session:
            client_of = session.run(
                "MATCH ()-[r:CLIENT_OF]->() "
                "RETURN count(r) AS pairs, sum(r.total_eur) AS total_eur, "
                "  sum(r.contracts) AS total_contracts"
            ).single()
            return {
                "trade_pairs": client_of["pairs"],
                "total_eur": client_of["total_eur"],
                "total_contracts": client_of["total_contracts"],
            }

    def get_dedup_stats(self) -> dict:
        """Deduplication queue stats."""
        with self._neo4j.session() as session:
            pending = session.run(
                "MATCH ()-[r:SAME_AS {reviewed: false}]->() RETURN count(r) AS n"
            ).single()["n"]
            reviewed = session.run(
                "MATCH ()-[r:SAME_AS {reviewed: true}]->() RETURN count(r) AS n"
            ).single()["n"]
            return {"pending": pending, "reviewed": reviewed, "total": pending + reviewed}

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

            # Lobbying stats
            lobbyist_count = session.run(
                "MATCH (l:Lobbyist) RETURN count(l) AS n"
            ).single()["n"]

            lobbyists_with_ep = session.run(
                "MATCH (l:Lobbyist) WHERE l.ep_passes > 0 "
                "RETURN count(l) AS n"
            ).single()["n"]

            lobby_interests = session.run(
                "MATCH (i:LobbyInterest)<-[:INTERESTED_IN]-(l) "
                "RETURN i.name AS topic, count(l) AS lobbyists "
                "ORDER BY lobbyists DESC LIMIT 10"
            ).data()

            # Person/director stats
            person_count = session.run(
                "MATCH (p:Person) RETURN count(p) AS n"
            ).single()["n"]

        return {
            "companies_with_contracts": companies_with_contracts,
            "contracts_by_country": by_country,
            "top_cpv_sectors": top_cpv,
            "authority_count": authority_count,
            "lobbyist_count": lobbyist_count,
            "lobbyists_with_ep_passes": lobbyists_with_ep,
            "top_lobby_interests": lobby_interests,
            "person_count": person_count,
        }
