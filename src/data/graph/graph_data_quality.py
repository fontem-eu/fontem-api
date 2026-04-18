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
                "Lobbyist", "LobbyInterest",
                "SanctionedEntity",
                "NUTSRegion", "CohesionProject",
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

    def get_sanctions_stats(self) -> dict:
        """Sanctions list stats."""
        with self._neo4j.session() as session:
            total = session.run(
                "MATCH (s:SanctionedEntity) RETURN count(s) AS n"
            ).single()["n"]
            persons = session.run(
                "MATCH (s:SanctionedEntity {entity_type: 'person'}) "
                "RETURN count(s) AS n"
            ).single()["n"]
            entities = total - persons
            matched = session.run(
                "MATCH (s:SanctionedEntity)-[:SANCTIONED]->(:Company) "
                "RETURN count(DISTINCT s) AS n"
            ).single()["n"]
            regimes = session.run(
                "MATCH (s:SanctionedEntity) "
                "RETURN s.sanction_regime AS regime, count(s) AS n "
                "ORDER BY n DESC LIMIT 10"
            ).data()
        return {
            "total": total, "persons": persons, "entities": entities,
            "matched_to_companies": matched, "top_regimes": regimes,
        }

    def get_firds_stats(self) -> dict:
        """FIRDS instrument data stats."""
        with self._neo4j.session() as session:
            total = session.run(
                "MATCH (l:Listing) WHERE l.isin IS NOT NULL "
                "RETURN count(l) AS n"
            ).single()["n"]
            with_ticker = session.run(
                "MATCH (l:Listing) WHERE l.isin IS NOT NULL "
                "AND l.ticker IS NOT NULL RETURN count(l) AS n"
            ).single()["n"]
            without_ticker = total - with_ticker
            by_type = session.run(
                "MATCH (l:Listing) WHERE l.instrument_type IS NOT NULL "
                "RETURN l.instrument_type AS type, count(l) AS count "
                "ORDER BY count DESC LIMIT 15"
            ).data()
            by_venue = session.run(
                "MATCH (l:Listing) WHERE l.trading_venue_mic IS NOT NULL "
                "RETURN l.trading_venue_mic AS venue, count(l) AS count "
                "ORDER BY count DESC LIMIT 10"
            ).data()
        return {
            "total": total, "with_ticker": with_ticker,
            "without_ticker": without_ticker,
            "ticker_rate": round(with_ticker / max(total, 1) * 100, 1),
            "by_instrument_type": by_type, "by_venue": by_venue,
        }

    def get_openfigi_stats(self) -> dict:
        """OpenFIGI enrichment stats."""
        with self._neo4j.session() as session:
            total = session.run(
                "MATCH (l:Listing) RETURN count(l) AS n"
            ).single()["n"]
            with_ticker = session.run(
                "MATCH (l:Listing) WHERE l.ticker IS NOT NULL "
                "RETURN count(l) AS n"
            ).single()["n"]
            without_ticker = total - with_ticker
        return {
            "total_listings": total, "with_ticker": with_ticker,
            "without_ticker": without_ticker,
            "enrichment_rate": round(with_ticker / max(total, 1) * 100, 1),
        }

    def get_cdp_stats(self) -> dict:
        """CDP climate disclosure stats."""
        with self._neo4j.session() as session:
            with_score = session.run(
                "MATCH (c:Company) WHERE c.cdp_score IS NOT NULL "
                "RETURN count(c) AS n"
            ).single()["n"]
            distribution = session.run(
                "MATCH (c:Company) WHERE c.cdp_score IS NOT NULL "
                "RETURN c.cdp_score AS score, count(c) AS count "
                "ORDER BY score"
            ).data()
            by_year = session.run(
                "MATCH (c:Company) WHERE c.cdp_reporting_year IS NOT NULL "
                "RETURN toString(c.cdp_reporting_year) AS year, count(c) AS count "
                "ORDER BY year DESC LIMIT 10"
            ).data()
        return {
            "companies_with_score": with_score,
            "score_distribution": distribution,
            "by_reporting_year": by_year,
        }

    def get_nuts_stats(self) -> dict:
        """NUTS region stats: hierarchy levels, company/authority coverage."""
        with self._neo4j.session() as session:
            total = session.run(
                "MATCH (n:NUTSRegion) RETURN count(n) AS n"
            ).single()["n"]
            by_level = session.run(
                "MATCH (n:NUTSRegion) "
                "RETURN n.level AS level, count(n) AS n ORDER BY n.level"
            ).data()
            companies_linked = session.run(
                "MATCH (:Company)-[:LOCATED_IN]->(:NUTSRegion) "
                "RETURN count(*) AS n"
            ).single()["n"]
            total_companies = session.run(
                "MATCH (c:Company) RETURN count(c) AS n"
            ).single()["n"]
            authorities_linked = session.run(
                "MATCH (:Authority)-[:LOCATED_IN]->(:NUTSRegion) "
                "RETURN count(*) AS n"
            ).single()["n"]
            top_regions = session.run(
                "MATCH (c:Company)-[:LOCATED_IN]->(n:NUTSRegion {level: 0}) "
                "RETURN n.code AS code, n.name AS name, count(c) AS companies "
                "ORDER BY companies DESC LIMIT 15"
            ).data()
        return {
            "total_regions": total,
            "by_level": by_level,
            "companies_linked": companies_linked,
            "total_companies": total_companies,
            "company_coverage_pct": round(
                companies_linked / max(total_companies, 1) * 100, 1
            ),
            "authorities_linked": authorities_linked,
            "top_regions": top_regions,
        }

    def get_eu_knowledge_graph_stats(self) -> dict:
        """EU Knowledge Graph cohesion project stats."""
        with self._neo4j.session() as session:
            projects = session.run(
                "MATCH (p:CohesionProject) RETURN count(p) AS n"
            ).single()["n"]
            with_budget = session.run(
                "MATCH (p:CohesionProject) "
                "WHERE p.total_budget IS NOT NULL "
                "RETURN count(p) AS n"
            ).single()["n"]
            total_eu_contribution = session.run(
                "MATCH (p:CohesionProject) "
                "WHERE p.eu_contribution IS NOT NULL "
                "RETURN sum(p.eu_contribution) AS total"
            ).single()["total"] or 0
            beneficiary_links = session.run(
                "MATCH (:Company)-[:BENEFICIARY_OF]->(:CohesionProject) "
                "RETURN count(*) AS n"
            ).single()["n"]
            by_fund = session.run(
                "MATCH (p:CohesionProject) WHERE p.fund IS NOT NULL "
                "RETURN p.fund AS fund, count(p) AS n "
                "ORDER BY n DESC LIMIT 10"
            ).data()
            by_country = session.run(
                "MATCH (p:CohesionProject) WHERE p.country IS NOT NULL "
                "RETURN p.country AS country, count(p) AS n "
                "ORDER BY n DESC LIMIT 15"
            ).data()
            with_nuts = session.run(
                "MATCH (p:CohesionProject)-[:LOCATED_IN]->(:NUTSRegion) "
                "RETURN count(p) AS n"
            ).single()["n"]
        return {
            "total_projects": projects,
            "with_budget": with_budget,
            "total_eu_contribution": total_eu_contribution,
            "beneficiary_links": beneficiary_links,
            "by_fund": by_fund,
            "by_country": by_country,
            "with_nuts_region": with_nuts,
        }

    def get_cross_source_overlap(self) -> dict:
        """Count entities shared between data sources."""
        with self._neo4j.session() as session:
            contracts_and_cohesion = session.run(
                "MATCH (c:Company)-[:AWARDED_TO]-(:Contract) "
                "WHERE (c)-[:BENEFICIARY_OF]->(:CohesionProject) "
                "RETURN count(DISTINCT c) AS n"
            ).single()["n"]

            contracts_and_lobby = session.run(
                "MATCH (c:Company)<-[:REPRESENTS]-(:Lobbyist) "
                "WHERE (c)-[:AWARDED_TO]-(:Contract) "
                "RETURN count(DISTINCT c) AS n"
            ).single()["n"]

            listed_and_contracts = session.run(
                "MATCH (c:Company)-[:LISTED_AS]->(:Listing) "
                "WHERE (c)-[:AWARDED_TO]-(:Contract) "
                "RETURN count(DISTINCT c) AS n"
            ).single()["n"]

            sanctions_matched = session.run(
                "MATCH (se:SanctionedEntity)-[:SANCTIONED]->(:Company) "
                "RETURN count(DISTINCT se) AS n"
            ).single()["n"]

        return {
            "contracts_and_cohesion": contracts_and_cohesion,
            "contracts_and_lobby": contracts_and_lobby,
            "listed_and_contracts": listed_and_contracts,
            "sanctions_matched": sanctions_matched,
        }

    def get_country_code_consistency(self) -> dict:
        """Check country code format consistency across companies."""
        with self._neo4j.session() as session:
            alpha2 = session.run(
                "MATCH (c:Company) WHERE size(c.country) = 2 "
                "RETURN count(c) AS n"
            ).single()["n"]
            alpha3 = session.run(
                "MATCH (c:Company) WHERE size(c.country) = 3 "
                "RETURN count(c) AS n"
            ).single()["n"]
            other = session.run(
                "MATCH (c:Company) "
                "WHERE c.country IS NOT NULL AND size(c.country) NOT IN [2, 3] "
                "RETURN count(c) AS n"
            ).single()["n"]
            no_country = session.run(
                "MATCH (c:Company) WHERE c.country IS NULL "
                "RETURN count(c) AS n"
            ).single()["n"]

            top_alpha2 = session.run(
                "MATCH (c:Company) WHERE size(c.country) = 2 "
                "RETURN c.country AS code, count(c) AS n "
                "ORDER BY n DESC LIMIT 10"
            ).data()

        return {
            "alpha2_count": alpha2,
            "alpha3_count": alpha3,
            "other_count": other,
            "no_country_count": no_country,
            "top_alpha2_codes": top_alpha2,
        }

    def get_field_completeness(self) -> dict:
        """Per-source field completeness percentages."""
        with self._neo4j.session() as session:
            # Sanctions completeness
            se_total = session.run(
                "MATCH (s:SanctionedEntity) RETURN count(s) AS n"
            ).single()["n"]
            se_name = session.run(
                "MATCH (s:SanctionedEntity) "
                "WHERE s.name IS NOT NULL AND s.name <> '' "
                "RETURN count(s) AS n"
            ).single()["n"]
            se_regime = session.run(
                "MATCH (s:SanctionedEntity) "
                "WHERE s.sanction_regime IS NOT NULL "
                "AND s.sanction_regime <> '' "
                "RETURN count(s) AS n"
            ).single()["n"]

            # Cohesion completeness
            cp_total = session.run(
                "MATCH (p:CohesionProject) RETURN count(p) AS n"
            ).single()["n"]
            cp_start = session.run(
                "MATCH (p:CohesionProject) "
                "WHERE p.start_date IS NOT NULL "
                "RETURN count(p) AS n"
            ).single()["n"]
            cp_nuts = session.run(
                "MATCH (p:CohesionProject) "
                "WHERE p.nuts_code IS NOT NULL AND p.nuts_code <> '' "
                "RETURN count(p) AS n"
            ).single()["n"]
            cp_linked_nuts = session.run(
                "MATCH (p:CohesionProject)-[:LOCATED_IN]->(:NUTSRegion) "
                "RETURN count(p) AS n"
            ).single()["n"]

            # Company completeness
            c_total = session.run(
                "MATCH (c:Company) RETURN count(c) AS n"
            ).single()["n"]
            c_lei = session.run(
                "MATCH (c:Company) WHERE c.lei IS NOT NULL "
                "RETURN count(c) AS n"
            ).single()["n"]
            c_country = session.run(
                "MATCH (c:Company) WHERE c.country IS NOT NULL "
                "RETURN count(c) AS n"
            ).single()["n"]
            c_nuts_linked = session.run(
                "MATCH (c:Company)-[:LOCATED_IN]->(:NUTSRegion) "
                "RETURN count(c) AS n"
            ).single()["n"]

        return {
            "sanctions": {
                "total": se_total,
                "name_pct": round(
                    se_name / max(se_total, 1) * 100, 1
                ),
                "regime_pct": round(
                    se_regime / max(se_total, 1) * 100, 1
                ),
            },
            "cohesion": {
                "total": cp_total,
                "start_date_pct": round(
                    cp_start / max(cp_total, 1) * 100, 1
                ),
                "nuts_code_pct": round(
                    cp_nuts / max(cp_total, 1) * 100, 1
                ),
                "nuts_linked_pct": round(
                    cp_linked_nuts / max(cp_total, 1) * 100, 1
                ),
            },
            "companies": {
                "total": c_total,
                "lei_pct": round(
                    c_lei / max(c_total, 1) * 100, 1
                ),
                "country_pct": round(
                    c_country / max(c_total, 1) * 100, 1
                ),
                "nuts_linked_pct": round(
                    c_nuts_linked / max(c_total, 1) * 100, 1
                ),
            },
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

            # New data sources
            sanctioned = session.run(
                "MATCH (s:SanctionedEntity) RETURN count(s) AS n"
            ).single()["n"]
            nuts_count = session.run(
                "MATCH (n:NUTSRegion) RETURN count(n) AS n"
            ).single()["n"]
            cohesion_count = session.run(
                "MATCH (p:CohesionProject) RETURN count(p) AS n"
            ).single()["n"]

        return {
            "companies_with_contracts": companies_with_contracts,
            "contracts_by_country": by_country,
            "top_cpv_sectors": top_cpv,
            "authority_count": authority_count,
            "lobbyist_count": lobbyist_count,
            "lobbyists_with_ep_passes": lobbyists_with_ep,
            "top_lobby_interests": lobby_interests,
            "sanctioned_entity_count": sanctioned,
            "nuts_region_count": nuts_count,
            "cohesion_project_count": cohesion_count,
        }

    # Degree-bucket boundaries for the connectedness histogram. Picked to
    # give meaningful resolution at the low end (where the mass is) and
    # handle the long tail (some hub nodes have 200k+ edges).
    _DEGREE_BUCKETS: list[tuple[int, str]] = [
        (0, "0"),
        (1, "1"),
        (3, "2-3"),
        (10, "4-10"),
        (30, "11-30"),
        (100, "31-100"),
        (300, "101-300"),
        (1000, "301-1000"),
        (10000, "1001-10000"),
        (999999, "10000+"),
    ]

    def get_connectedness(self) -> dict:
        """Return degree distribution, summary stats, and top hubs.

        Whole-graph scan. Runs in ~2s on the current 4M-node graph via
        Cypher's ``COUNT { (n)--() }`` subquery, which is backed by Neo4j's
        relationship counts per node. Well below the 30s transaction
        timeout. Three focused queries — aggregation happens server-side,
        so we never ship the per-node degree list back.
        """
        bucket_labels = {b[0]: b[1] for b in self._DEGREE_BUCKETS}

        with self._neo4j.session() as session:
            # 1. Bucketed distribution.
            distribution_rows = session.run(
                """
                MATCH (n)
                WITH COUNT { (n)--() } AS deg
                WITH
                  CASE
                    WHEN deg = 0      THEN 0
                    WHEN deg = 1      THEN 1
                    WHEN deg <= 3     THEN 3
                    WHEN deg <= 10    THEN 10
                    WHEN deg <= 30    THEN 30
                    WHEN deg <= 100   THEN 100
                    WHEN deg <= 300   THEN 300
                    WHEN deg <= 1000  THEN 1000
                    WHEN deg <= 10000 THEN 10000
                    ELSE 999999
                  END AS bucket
                RETURN bucket, count(*) AS nodes
                """
            ).data()
            counts = {row["bucket"]: row["nodes"] for row in distribution_rows}

            # 2. Summary stats.
            stats_row = session.run(
                """
                MATCH (n)
                WITH COUNT { (n)--() } AS deg
                RETURN
                  count(*) AS total_nodes,
                  avg(deg) AS mean_degree,
                  percentileCont(deg, 0.5) AS median_degree,
                  max(deg) AS max_degree
                """
            ).single()
            total_edges = session.run(
                "MATCH ()-[r]->() RETURN count(r) AS n"
            ).single()["n"]

            # 3. Top 10 hubs.
            hubs_rows = session.run(
                """
                MATCH (n)
                WITH n, COUNT { (n)--() } AS deg
                ORDER BY deg DESC
                LIMIT 10
                RETURN
                  labels(n) AS labels,
                  coalesce(n.name, n.code, n.id, n.gmr_id, '<unnamed>') AS id,
                  deg AS degree
                """
            ).data()

        distribution = [
            {"bucket": edge, "label": bucket_labels[edge], "nodes": counts.get(edge, 0)}
            for edge, _ in self._DEGREE_BUCKETS
        ]

        return {
            "stats": {
                "total_nodes": stats_row["total_nodes"],
                "total_edges": total_edges,
                "orphan_count": counts.get(0, 0),
                "mean_degree": round(float(stats_row["mean_degree"] or 0), 4),
                "median_degree": float(stats_row["median_degree"] or 0),
                "max_degree": stats_row["max_degree"] or 0,
            },
            "distribution": distribution,
            "hubs": hubs_rows,
        }
