"""Graph Data Quality Source — Neo4j-backed data quality metrics."""
# The implementation mirrors the wide DataQualitySource protocol — one method
# per dashboard panel. That gives one large module with one method per panel
# (R0904 too-many-public-methods, C0302 too-many-lines). The override of
# `get_triples_stats` keyword-onlies the limits the base method advertises as
# variadic kwargs (W0221 arguments-differ) — that's an explicit narrowing,
# safe to do once we know the concrete impl never accepts more kwargs.
# pylint: disable=too-many-lines,too-many-public-methods,arguments-differ
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from ...analysis.data_quality_source import DataQualitySource
from ..sparql.virtuoso_client import SparqlTimeout, VirtuosoClient
from ._value_quality import trusted_value_sum
from .neo4j_client import Neo4jClient

logger = logging.getLogger(__name__)

# Contract-value aggregation guard — see ._value_quality. Excludes
# confidence-flagged values from headline totals (the contract stays
# counted; only its money is dropped). ``ct`` is the Contract binding.
_TRUSTED_VALUE_SUM = trusted_value_sum("ct")

# Labels the connectedness dashboard reports on. Curated rather than
# `MATCH (n)` so each per-label query uses the label-scan index and
# the result ordering is deterministic.
#
# SanctionedEntity (Phase 2) and FinancialYear (Phase 3) were
# removed from Neo4j; their counts come from Virtuoso, reported
# in their own panels. Connectedness is a Neo4j-only metric — it
# measures node degree inside the Cypher graph — so labels that
# no longer live in Neo4j are dropped from this list.
_CONNECTEDNESS_LABELS = (
    "Company", "Listing",
    "Contract", "Authority", "CPV",
    "Lobbyist", "LobbyInterest",
    "NUTSRegion",
    "CohesionProject", "Person",
)

# Sanctions live in this named graph in Virtuoso. The DQ dashboard
# queries this graph for entity totals + regime breakdown; the
# matched-companies count still comes from Neo4j (the SANCTIONED
# edge stayed Neo4j-side because it's a Company-relationship and
# Companies haven't migrated yet).
SANCTIONS_GRAPH_IRI = "http://data.fontem.eu/graph/sanctions"

# fontem:Filing nodes for the FinancialYear domain. One named
# graph per source so the loaders can PUT-replace independently.
FILINGS_EDGAR_GRAPH_IRI = "http://data.fontem.eu/graph/financials/edgar"
FILINGS_ESEF_GRAPH_IRI = "http://data.fontem.eu/graph/financials/esef"

# Property URIs we ask the SPARQL endpoint to filter on. Picking
# them up as constants here so a typo in `revenue` is caught at
# the boundary rather than silently producing a 0%.
_FONTEM = "http://data.fontem.eu/ontology#"
_FILING_FIELD_URIS = {
    "revenue":           _FONTEM + "revenue",
    "net_income":        _FONTEM + "netIncome",
    "total_assets":      _FONTEM + "totalAssets",
    "equity":            _FONTEM + "equity",
    "operating_cashflow": _FONTEM + "operatingCashflow",
}

_CONNECTEDNESS_TTL_SECONDS = 3600


class GraphDataQualitySource(DataQualitySource):
    """Production data quality source backed by Neo4j (and, for
    the sanctions panel, by Virtuoso since the Phase 2 cutover).

    Pass an optional ``virtuoso_client`` to enable the
    Virtuoso-backed sanctions reads. When None, sanctions stats
    return zero — the API still boots, but the dashboard panel
    shows an empty state. This is the staging fallback for envs
    that haven't enabled Virtuoso yet.
    """

    def __init__(
        self,
        neo4j_client: Neo4jClient,
        virtuoso_client: VirtuosoClient | None = None,
    ) -> None:
        self._neo4j = neo4j_client
        self._virtuoso = virtuoso_client
        # Connectedness is a full graph scan across every label we
        # care about; cache hot for an hour so the dashboard feels
        # instant while staying fresh enough during active ETL work.
        self._connectedness_cache: tuple[float, dict] | None = None

    def get_graph_stats(self) -> dict:
        """Return node/relationship counts by label.

        SanctionedEntity (Phase 2) and FinancialYear (Phase 3)
        moved to Virtuoso. We keep both keys in the response so
        the UI's grid layout doesn't lose rows; their values come
        from SPARQL queries against the corresponding named
        graphs.
        """
        with self._neo4j.session() as session:
            labels = {}
            for label in [
                "Company", "Listing",
                "Contract", "Authority", "CPV",
                "Lobbyist", "LobbyInterest",
                "NUTSRegion", "CohesionProject",
            ]:
                n = session.run(
                    f"MATCH (n:{label}) RETURN count(n) AS n"
                ).single()["n"]
                labels[label] = n
            labels["SanctionedEntity"] = self._sanctions_count_from_virtuoso()
            labels["FinancialYear"] = (
                self._filings_count(FILINGS_EDGAR_GRAPH_IRI)
                + self._filings_count(FILINGS_ESEF_GRAPH_IRI)
            )

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
                "WHERE NOT EXISTS { (c)-[:LISTED_AS]->() } "
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
                f"  {_TRUSTED_VALUE_SUM} AS total_eur "
                "ORDER BY contracts DESC"
            ).data()

    def get_contracts_currency_quality(self) -> dict:
        """Currency-related data quality metrics."""
        with self._neo4j.session() as session:
            total = session.run(
                "MATCH (ct:Contract) RETURN count(ct) AS n"
            ).single()["n"]
            # "Undisclosed" = no usable awarded value could be derived.
            # The loader never wrote the aspirational `value_undisclosed`
            # flag (so this gauge read a permanent 0 → "100% disclosed");
            # the value-confidence work makes the real signal a plain
            # `value_eur IS NULL`, which is what we count here.
            undisclosed = session.run(
                "MATCH (ct:Contract) WHERE ct.value_eur IS NULL RETURN count(ct) AS n"
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
                f"  {_TRUSTED_VALUE_SUM} AS total_eur "
                "ORDER BY contracts DESC LIMIT 25"
            ).data()
            return {
                "total": total,
                "value_undisclosed": undisclosed,
                "converted_to_eur": converted,
                "with_currency": with_currency,
                "by_currency": by_currency,
            }

    def get_contracts_nulls(self) -> dict:
        """Count of contracts missing key fields.

        The property names here must match what the TED loader actually
        writes (see fontem-api/src/etl/load_ted_contracts.py + the sink
        in fontem-neo4j-sink/neo4j_sink/cypher.py:render_upsert_contract):
        ``cpv`` (not ``cpv_main``), ``publication_date`` (not
        ``award_date``), ``title`` (not ``description``). ``country``
        cascades from the contracting authority and was added in the
        same fix-up — before that, this query reported 100% null on
        every field because the names didn't exist on the nodes.
        """
        with self._neo4j.session() as session:
            total = session.run(
                "MATCH (ct:Contract) RETURN count(ct) AS n"
            ).single()["n"]
            fields = {}
            for field in ["value_eur", "cpv", "publication_date", "title", "country"]:
                n = session.run(
                    f"MATCH (ct:Contract) WHERE ct.{field} IS NULL "
                    "RETURN count(ct) AS n"
                ).single()["n"]
                fields[field] = n
            return {"total": total, "missing": fields}

    def get_contracts_integrity(self) -> dict:
        """Tender-integrity metrics for the DQ dashboard: bidder-count and
        procedure-type coverage, the single-bidder rate (EC SMSB headline),
        the red-flag distribution and per-flag counts. Reads the flags the
        sink materialises via the shared keystone (contract_red_flags)."""
        with self._neo4j.session() as session:
            total = session.run(
                "MATCH (ct:Contract) RETURN count(ct) AS n").single()["n"]
            with_bidders = session.run(
                "MATCH (ct:Contract) WHERE ct.tenders_received IS NOT NULL "
                "RETURN count(ct) AS n").single()["n"]
            single = session.run(
                "MATCH (ct:Contract) WHERE ct.tenders_received = 1 "
                "RETURN count(ct) AS n").single()["n"]
            with_proc = session.run(
                "MATCH (ct:Contract) WHERE ct.procedure_type IS NOT NULL "
                "RETURN count(ct) AS n").single()["n"]
            flag_dist = session.run(
                "MATCH (ct:Contract) WHERE ct.integrity_red_flags IS NOT NULL "
                "RETURN ct.integrity_red_flags AS flags, count(*) AS contracts "
                "ORDER BY flags").data()
            flags = {}
            for prop, key in (
                ("is_single_bidder", "single_bidder"),
                ("is_non_open", "non_open"),
                ("is_no_call", "no_call"),
                ("is_price_only", "price_only"),
            ):
                flags[key] = session.run(
                    f"MATCH (ct:Contract) WHERE ct.{prop} = true "
                    "RETURN count(ct) AS n").single()["n"]
        return {
            "total": total,
            "bidder_count_coverage": (with_bidders / total) if total else None,
            "procedure_type_coverage": (with_proc / total) if total else None,
            "single_bidder": single,
            "single_bidder_rate": (single / with_bidders) if with_bidders else None,
            "red_flag_distribution": flag_dist,
            "flags": flags,
        }

    def get_contracts_value_timeline(self) -> list[dict]:
        """Daily total EUR value of contracts (confidence-gated, so a
        flagged outlier never spikes a day's total)."""
        with self._neo4j.session() as session:
            return session.run(
                "MATCH (ct:Contract) "
                "WHERE ct.publication_date IS NOT NULL AND ct.value_eur IS NOT NULL "
                "RETURN left(ct.publication_date, 10) AS date, "
                f"  {_TRUSTED_VALUE_SUM} AS value "
                "ORDER BY date"
            ).data()

    def get_contracts_value_quality(self) -> dict:
        """Value-confidence overview: how many contracts are excluded from
        value aggregates, the breakdown by quality flag, and the top
        flagged contracts for review. This is the operator surface for
        the values the confidence scorer pulled out of the headline
        totals — they are kept in the graph, just not counted."""
        with self._neo4j.session() as session:
            total = session.run(
                "MATCH (ct:Contract) RETURN count(ct) AS n"
            ).single()["n"]
            flagged = session.run(
                "MATCH (ct:Contract) "
                "WHERE coalesce(ct.value_low_confidence, false) "
                "RETURN count(ct) AS n"
            ).single()["n"]
            with_payable_discrepancy = session.run(
                "MATCH (ct:Contract) "
                "WHERE coalesce(ct.value_payable_discrepancy, false) "
                "RETURN count(ct) AS n"
            ).single()["n"]
            by_flag = session.run(
                "MATCH (ct:Contract) WHERE ct.value_quality_flag IS NOT NULL "
                "RETURN ct.value_quality_flag AS flag, count(ct) AS count "
                "ORDER BY count DESC"
            ).data()
            top_flagged = session.run(
                "MATCH (ct:Contract) "
                "WHERE coalesce(ct.value_low_confidence, false) "
                "  AND ct.value_eur IS NOT NULL "
                "RETURN ct.ted_notice_id AS notice, ct.title AS title, "
                "  ct.country AS country, ct.value_eur AS value_eur, "
                "  ct.estimated_value_eur AS estimated_eur, "
                "  ct.value_quality_flag AS flag, "
                "  ct.value_confidence AS confidence "
                "ORDER BY ct.value_eur DESC LIMIT 25"
            ).data()
            return {
                "total": total,
                "flagged_low_confidence": flagged,
                "with_payable_discrepancy": with_payable_discrepancy,
                "low_confidence_pct": round(flagged / max(total, 1) * 100, 1),
                "by_flag": by_flag,
                "top_flagged": top_flagged,
            }

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
                "WHERE NOT EXISTS { (p)-[:LISTED_AS]->() } AND p.lei IS NULL "
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
        """US EDGAR financial data stats — sourced from Virtuoso
        post Phase 3 cutover.

        ``companies`` still queries Neo4j (Companies haven't
        migrated yet); the rest comes from the EDGAR named graph.
        """
        with self._neo4j.session() as session:
            companies = session.run(
                "MATCH (c:Company)-[:LISTED_AS]->(l:Listing {exchange: 'US'}) "
                "RETURN count(DISTINCT c) AS n"
            ).single()["n"]
        return {
            "companies": companies,
            **self._filings_stats(FILINGS_EDGAR_GRAPH_IRI),
        }

    _EU_MEMBERS = (
        "'AT','BE','BG','HR','CY','CZ','DK','EE','FI','FR','DE','GR',"
        "'HU','IE','IT','LV','LT','LU','MT','NL','PL','PT','RO','SK',"
        "'SI','ES','SE'"
    )

    def get_esef_stats(self) -> dict:
        """EU ESEF financial data stats (EU members only) —
        Virtuoso for the filings, Neo4j for the country
        breakdown.

        Companies still live in Neo4j, so by_country and the
        distinct-companies count run there. Filings (totals,
        year breakdown, field coverage) come from the ESEF
        named graph in Virtuoso. The two halves are joined on
        the Filing's filedBy IRI: ``Company/<gmr_id>``.
        """
        if self._virtuoso is None:
            return {
                "companies": 0, "financial_years": 0,
                "by_year": [], "by_country": [],
                "field_coverage": {f: 0.0 for f in _FILING_FIELD_URIS},
            }
        graph = FILINGS_ESEF_GRAPH_IRI

        # Filings totals + by-year + field coverage from Virtuoso.
        base = self._filings_stats(graph)

        # Companies + by-country: join Virtuoso (which gmr_ids
        # filed ESEF) against Neo4j (country lookup, EU filter).
        # First grab the gmr_id list — small enough to round-trip.
        company_rows = self._virtuoso.query(
            f"""
            PREFIX fontem: <http://data.fontem.eu/ontology#>
            SELECT DISTINCT ?company WHERE {{
                GRAPH <{graph}> {{
                    ?f a fontem:Filing ;
                       fontem:filedBy ?company .
                }}
            }}
            """
        )
        gmr_ids = [
            r["company"].rsplit("/", 1)[-1] for r in company_rows
        ]
        if not gmr_ids:
            return {
                **base, "companies": 0, "by_country": [],
            }

        eu_filter = f"AND c.country IN [{self._EU_MEMBERS}]"
        with self._neo4j.session() as session:
            companies = session.run(
                f"MATCH (c:Company) WHERE c.gmr_id IN $ids {eu_filter} "
                "RETURN count(DISTINCT c) AS n",
                ids=gmr_ids,
            ).single()["n"]
            by_country = session.run(
                f"MATCH (c:Company) WHERE c.gmr_id IN $ids {eu_filter} "
                "RETURN c.country AS country, count(c) AS count "
                "ORDER BY count DESC LIMIT 20",
                ids=gmr_ids,
            ).data()
        return {
            **base,
            "companies": companies,
            "by_country": by_country,
        }

    # ── Filings SPARQL helpers ───────────────────────────────────

    def _filings_stats(self, graph_iri: str) -> dict:
        """Filing-graph stats common to EDGAR + ESEF.

        Returns ``{financial_years, by_year, field_coverage}``.
        Caller layers on the cross-store companies count.
        """
        if self._virtuoso is None:
            return {
                "financial_years": 0, "by_year": [],
                "field_coverage": {f: 0.0 for f in _FILING_FIELD_URIS},
            }
        graph = graph_iri

        rows = self._virtuoso.query(
            f"""
            PREFIX fontem: <http://data.fontem.eu/ontology#>
            SELECT (COUNT(DISTINCT ?f) AS ?n) WHERE {{
                GRAPH <{graph}> {{ ?f a fontem:Filing }}
            }}
            """
        )
        fin_years = int(rows[0]["n"]) if rows else 0

        # by_year — group by fiscalYear, format as "YYYY-01-01" so
        # the dashboard's existing Date axis renders unchanged.
        year_rows = self._virtuoso.query(
            f"""
            PREFIX fontem: <http://data.fontem.eu/ontology#>
            SELECT ?yr (COUNT(DISTINCT ?f) AS ?n) WHERE {{
                GRAPH <{graph}> {{
                    ?f a fontem:Filing ;
                       fontem:fiscalYear ?yr .
                }}
                FILTER (xsd:integer(STR(?yr)) >= 1990 &&
                        xsd:integer(STR(?yr)) <= 2030)
            }}
            GROUP BY ?yr
            ORDER BY ?yr
            """
        )
        by_year = [
            {
                "date": f"{r['yr']}-01-01",
                "value": int(r["n"]),
            }
            for r in year_rows
        ]

        coverage: dict[str, float] = {}
        for field, prop in _FILING_FIELD_URIS.items():
            cov_rows = self._virtuoso.query(
                f"""
                PREFIX fontem: <http://data.fontem.eu/ontology#>
                SELECT (COUNT(DISTINCT ?f) AS ?n) WHERE {{
                    GRAPH <{graph}> {{
                        ?f a fontem:Filing ;
                           <{prop}> ?v .
                    }}
                }}
                """
            )
            n = int(cov_rows[0]["n"]) if cov_rows else 0
            coverage[field] = round(n / max(fin_years, 1) * 100, 1)
        return {
            "financial_years": fin_years,
            "by_year": by_year,
            "field_coverage": coverage,
        }

    def get_lobbying_stats(self) -> dict:
        """EU Transparency Register stats.

        Reads from ``(:Disclosure {system: 'eu-lobbying'})`` nodes
        with ``detail_*``-prefixed properties — the shape the sink
        actually writes. The legacy code queried ``:Lobbyist`` with
        bare property names like ``l.country`` / ``l.cost_max``;
        neither label nor property exists, so the endpoint returned
        valid-200 zeros while 17k+ disclosures sat in the graph.
        """
        with self._neo4j.session() as session:
            total = session.run(
                "MATCH (d:Disclosure {system: 'eu-lobbying'}) "
                "RETURN count(d) AS n"
            ).single()["n"]
            with_ep = session.run(
                "MATCH (d:Disclosure {system: 'eu-lobbying'}) "
                "WHERE d.detail_ep_passes > 0 RETURN count(d) AS n"
            ).single()["n"]
            # A confident consolidator match on the registrant sets the
            # disclosure's company_gmr_id, which the sink materialises as
            # Disclosure-[:FILED_BY]->Company (item #7: the typed
            # REPRESENTS edge can't address a composite-keyed :Disclosure,
            # so the link rides on the disclosure's own upsert instead).
            matched = session.run(
                "MATCH (d:Disclosure {system: 'eu-lobbying'})"
                "-[:FILED_BY]->(:Company) "
                "RETURN count(DISTINCT d) AS n"
            ).single()["n"]
            # The most-represented companies: how many lobbyist
            # registrations resolve to each company.
            top_companies = session.run(
                "MATCH (d:Disclosure {system: 'eu-lobbying'})"
                "-[:FILED_BY]->(c:Company) "
                "RETURN coalesce(c.name, c.gmr_id) AS company, "
                "       count(d) AS lobbyists "
                "ORDER BY lobbyists DESC LIMIT 20"
            ).data()
            # Registrant category mix (companies vs NGOs vs consultancies …).
            by_category = session.run(
                "MATCH (d:Disclosure {system: 'eu-lobbying'}) "
                "WHERE d.detail_category IS NOT NULL "
                "RETURN d.detail_category AS category, count(d) AS count "
                "ORDER BY count DESC LIMIT 15"
            ).data()
            # Top declared spenders (annual lobbying cost ceiling).
            top_spenders = session.run(
                "MATCH (d:Disclosure {system: 'eu-lobbying'}) "
                "WHERE d.detail_cost_max IS NOT NULL "
                "RETURN coalesce(d.title, d.disclosure_id) AS lobbyist, "
                "       d.detail_cost_max AS cost_max "
                "ORDER BY d.detail_cost_max DESC LIMIT 20"
            ).data()
            by_country = session.run(
                "MATCH (d:Disclosure {system: 'eu-lobbying'}) "
                "WHERE d.detail_country IS NOT NULL "
                "RETURN d.detail_country AS country, count(d) AS count "
                "ORDER BY count DESC LIMIT 20"
            ).data()
            registrations = session.run(
                "MATCH (d:Disclosure {system: 'eu-lobbying'}) "
                "WHERE d.detail_registration_date IS NOT NULL "
                "  AND size(d.detail_registration_date) >= 7 "
                "RETURN left(d.detail_registration_date, 7) + '-01' "
                "         AS date, count(d) AS value "
                "ORDER BY date"
            ).data()
            cost_ranges = session.run(
                "MATCH (d:Disclosure {system: 'eu-lobbying'}) "
                "WHERE d.detail_cost_max IS NOT NULL "
                "RETURN CASE "
                "  WHEN d.detail_cost_max < 10000 THEN '<10K' "
                "  WHEN d.detail_cost_max < 100000 THEN '10K-100K' "
                "  WHEN d.detail_cost_max < 1000000 THEN '100K-1M' "
                "  ELSE '>1M' END AS bucket, count(d) AS count "
                "ORDER BY count DESC"
            ).data()
            return {
                "total": total, "with_ep_passes": with_ep,
                "matched_to_company": matched,
                "match_rate": round(matched / max(total, 1) * 100, 1),
                "by_country": by_country,
                "registrations_timeline": registrations,
                "cost_distribution": cost_ranges,
                "top_companies": top_companies,
                "by_category": by_category,
                "top_spenders": top_spenders,
            }

    def get_trade_edges_stats(self) -> dict:
        """Authority↔Company trade aggregates, computed live from the
        per-contract data.

        Used to read pre-materialised ``:CLIENT_OF`` / ``:SUPPLIER_OF``
        edges produced by ``materialize_trade_edges.py``. That cron job
        was deleted because the summary edges (a) had no time
        dimension while every meaningful question about supplier
        relationships is time-scoped, (b) the materialiser ran into
        Neo4j transaction-timeout issues and the data went silently
        invisible when it broke, and (c) the same numbers come out
        of one Cypher query over the per-contract data.

        The current query is intentionally all-time. Add a ``since``
        kwarg the day a UI wants "last N months".
        """
        with self._neo4j.session() as session:
            row = session.run(
                "MATCH (a:Authority)-[:AWARDED]->(ct:Contract)"
                "-[:AWARDED_TO]->(c:Company) "
                "WITH a, c, count(ct) AS n, "
                "     sum(coalesce(ct.value_eur, 0)) AS s "
                "RETURN count(*)   AS pairs, "
                "       sum(s)     AS total_eur, "
                "       sum(n)     AS total_contracts"
            ).single()
            return {
                "trade_pairs": row["pairs"],
                "total_eur": row["total_eur"] or 0,
                "total_contracts": row["total_contracts"] or 0,
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

    def _filings_count(self, graph_iri: str) -> int:
        """Count distinct fontem:Filing in a single named graph."""
        if self._virtuoso is None:
            return 0
        rows = self._virtuoso.query(
            f"""
            PREFIX fontem: <http://data.fontem.eu/ontology#>
            SELECT (COUNT(DISTINCT ?f) AS ?n) WHERE {{
                GRAPH <{graph_iri}> {{ ?f a fontem:Filing }}
            }}
            """
        )
        return int(rows[0]["n"]) if rows else 0

    def _sanctions_count_from_virtuoso(
        self, *, extra_clause: str = ""
    ) -> int:
        """Count fontem:SanctionedEntity instances in the sanctions
        graph, optionally narrowed by an extra clause.

        Used by graph_stats, field_completeness, coverage, and
        sanctions_stats — the same query shape with different
        guards. ``extra_clause`` is interpolated raw into the
        WHERE block; only the DataQuality module composes it
        (no user input ever reaches this).
        """
        if self._virtuoso is None:
            return 0
        graph = SANCTIONS_GRAPH_IRI
        rows = self._virtuoso.query(
            f"""
            PREFIX fontem: <http://data.fontem.eu/ontology#>
            SELECT (COUNT(DISTINCT ?s) AS ?n) WHERE {{
                GRAPH <{graph}> {{
                    ?s a fontem:SanctionedEntity .
                    {extra_clause}
                }}
            }}
            """
        )
        return int(rows[0]["n"]) if rows else 0

    def get_sanctions_stats(self) -> dict:
        """Sanctions list stats — Virtuoso for the entity body,
        Neo4j for the matched-companies count.

        After the Phase 2 cutover the Neo4j SanctionedEntity nodes
        are gone; the only thing left in Neo4j is the SANCTIONED
        edge from Company. The edge endpoint changed from
        ``:SanctionedEntity`` (full body) to a stub ``:SanctionRef``
        (IRI only); the matched-count query is rewritten to that
        new shape.
        """
        if self._virtuoso is None:
            # Boot-time fallback — no Virtuoso configured. Empty
            # sanctions panel rather than a 500 on the dashboard.
            return {
                "total": 0, "persons": 0, "entities": 0,
                "matched_to_companies": 0, "top_regimes": [],
            }

        graph = SANCTIONS_GRAPH_IRI
        rows = self._virtuoso.query(
            f"""
            PREFIX fontem: <http://data.fontem.eu/ontology#>
            SELECT (COUNT(DISTINCT ?s) AS ?n) WHERE {{
                GRAPH <{graph}> {{ ?s a fontem:SanctionedEntity }}
            }}
            """
        )
        entities = int(rows[0]["n"]) if rows else 0

        regime_rows = self._virtuoso.query(
            f"""
            PREFIX fontem: <http://data.fontem.eu/ontology#>
            SELECT ?regime (COUNT(DISTINCT ?s) AS ?n) WHERE {{
                GRAPH <{graph}> {{
                    ?s a fontem:SanctionedEntity ;
                       fontem:sanctionRegime ?regime .
                }}
            }}
            GROUP BY ?regime
            ORDER BY DESC(?n) LIMIT 10
            """
        )
        regimes = [{"regime": r["regime"], "n": r["n"]} for r in regime_rows]

        # Persons are no longer stored anywhere — the GDPR posture
        # filters them out at the loader. Reporting 0 keeps the
        # dashboard column wired without resurrecting deleted data.
        persons = 0

        # Matched-companies still lives in Neo4j on the SANCTIONED
        # edge. After cutover the edge points at a :SanctionRef
        # stub; the count is the same shape either way.
        with self._neo4j.session() as session:
            matched = session.run(
                "MATCH (:Company)-[:SANCTIONED]->(s) "
                "RETURN count(DISTINCT s) AS n"
            ).single()["n"]

        return {
            "total": entities + persons,
            "persons": persons,
            "entities": entities,
            "matched_to_companies": matched,
            "top_regimes": regimes,
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
        """CDP climate disclosure stats.

        Reads from ``(:Disclosure {system: 'cdp'})``. The legacy code
        looked at ``c.cdp_score`` / ``c.cdp_reporting_year`` directly
        on Company nodes — neither property is written by any loader,
        so the endpoint returned valid-200 zeros. The CDP ETL has
        also never emitted any events (item #19), so this still
        returns zeros today — but now honestly, scoped to the right
        node shape so a future cdp loader is wired in correctly.
        """
        with self._neo4j.session() as session:
            with_score = session.run(
                "MATCH (d:Disclosure {system: 'cdp'}) "
                "WHERE d.detail_score IS NOT NULL "
                "RETURN count(d) AS n"
            ).single()["n"]
            distribution = session.run(
                "MATCH (d:Disclosure {system: 'cdp'}) "
                "WHERE d.detail_score IS NOT NULL "
                "RETURN d.detail_score AS score, count(d) AS count "
                "ORDER BY score"
            ).data()
            by_year = session.run(
                "MATCH (d:Disclosure {system: 'cdp'}) "
                "WHERE d.year IS NOT NULL "
                "RETURN toString(d.year) AS year, count(d) AS count "
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
        """EU Knowledge Graph cohesion project stats.

        Reads from ``(:Disclosure {system: 'eu-cohesion'})`` nodes
        with ``detail_*`` properties. The legacy code queried
        ``:CohesionProject`` with bare property names like
        ``p.total_budget``; neither exists in the graph, so the
        endpoint returned zeros while 43k+ disclosures sit there.

        BENEFICIARY_OF / LOCATED_IN edges from Disclosure don't
        materialise today (sink drops typed-relationships when an
        endpoint is missing — item #7). Reflected honestly as 0.
        """
        with self._neo4j.session() as session:
            projects = session.run(
                "MATCH (d:Disclosure {system: 'eu-cohesion'}) "
                "RETURN count(d) AS n"
            ).single()["n"]
            with_budget = session.run(
                "MATCH (d:Disclosure {system: 'eu-cohesion'}) "
                "WHERE d.detail_total_budget IS NOT NULL "
                "RETURN count(d) AS n"
            ).single()["n"]
            total_eu_contribution = session.run(
                "MATCH (d:Disclosure {system: 'eu-cohesion'}) "
                "WHERE d.detail_eu_contribution IS NOT NULL "
                "RETURN sum(d.detail_eu_contribution) AS total"
            ).single()["total"] or 0
            beneficiary_links = session.run(
                "MATCH (:Company)-[:BENEFICIARY_OF]->"
                "(:Disclosure {system: 'eu-cohesion'}) "
                "RETURN count(*) AS n"
            ).single()["n"]
            by_fund = session.run(
                "MATCH (d:Disclosure {system: 'eu-cohesion'}) "
                "WHERE d.detail_fund IS NOT NULL "
                "RETURN d.detail_fund AS fund, count(d) AS n "
                "ORDER BY n DESC LIMIT 10"
            ).data()
            by_country = session.run(
                "MATCH (d:Disclosure {system: 'eu-cohesion'}) "
                "WHERE d.detail_country IS NOT NULL "
                "RETURN d.detail_country AS country, count(d) AS n "
                "ORDER BY n DESC LIMIT 15"
            ).data()
            with_nuts = session.run(
                "MATCH (d:Disclosure {system: 'eu-cohesion'})"
                "-[:LOCATED_IN]->(:NUTSRegion) "
                "RETURN count(d) AS n"
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
                "WHERE (c)-[:BENEFICIARY_OF]->"
                "       (:Disclosure {system: 'eu-cohesion'}) "
                "RETURN count(DISTINCT c) AS n"
            ).single()["n"]

            contracts_and_lobby = session.run(
                "MATCH (c:Company)<-[:REPRESENTS]-"
                "(:Disclosure {system: 'eu-lobbying'}) "
                "WHERE (c)-[:AWARDED_TO]-(:Contract) "
                "RETURN count(DISTINCT c) AS n"
            ).single()["n"]

            listed_and_contracts = session.run(
                "MATCH (c:Company)-[:LISTED_AS]->(:Listing) "
                "WHERE (c)-[:AWARDED_TO]-(:Contract) "
                "RETURN count(DISTINCT c) AS n"
            ).single()["n"]

            # SANCTIONED edges still live in Neo4j; the target
            # changed from :SanctionedEntity (full body) to a
            # :SanctionRef stub holding only the Virtuoso IRI.
            # Count the distinct stubs that are bound to a
            # Company — same metric as before, new shape.
            sanctions_matched = session.run(
                "MATCH (:Company)-[:SANCTIONED]->(s) "
                "RETURN count(DISTINCT s) AS n"
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
        """Per-source field completeness percentages.

        Sanctions completeness is sourced from Virtuoso post-
        cutover. Every entity is required by the SHACL shape to
        carry rdfs:label and fontem:sanctionRegime, so the
        coverage is always 100% — but we emit the metric anyway
        because the dashboard panel expects it, and dropping it
        to zero would look like a regression.
        """
        se_total = self._sanctions_count_from_virtuoso()
        se_name = self._sanctions_count_from_virtuoso(
            extra_clause="?s <http://www.w3.org/2000/01/rdf-schema#label> ?label"
        )
        se_regime = self._sanctions_count_from_virtuoso(
            extra_clause="?s <http://data.fontem.eu/ontology#sanctionRegime> ?regime"
        )

        with self._neo4j.session() as session:

            # Cohesion completeness — same Disclosure-shape fix as
            # get_eu_knowledge_graph_stats.
            cp_total = session.run(
                "MATCH (d:Disclosure {system: 'eu-cohesion'}) "
                "RETURN count(d) AS n"
            ).single()["n"]
            cp_start = session.run(
                "MATCH (d:Disclosure {system: 'eu-cohesion'}) "
                "WHERE d.detail_start_date IS NOT NULL "
                "RETURN count(d) AS n"
            ).single()["n"]
            cp_nuts = session.run(
                "MATCH (d:Disclosure {system: 'eu-cohesion'}) "
                "WHERE d.detail_nuts_code IS NOT NULL "
                "  AND d.detail_nuts_code <> '' "
                "RETURN count(d) AS n"
            ).single()["n"]
            cp_linked_nuts = session.run(
                "MATCH (d:Disclosure {system: 'eu-cohesion'})"
                "-[:LOCATED_IN]->(:NUTSRegion) "
                "RETURN count(d) AS n"
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

            # Contracts by country (top 10) — value confidence-gated
            by_country = session.run(
                "MATCH (ct:Contract) "
                "WHERE ct.country IS NOT NULL "
                "RETURN ct.country AS country, count(ct) AS contracts, "
                f"  {_TRUSTED_VALUE_SUM} AS total_value "
                "ORDER BY contracts DESC LIMIT 15"
            ).data()

            # Top CPV sectors — value confidence-gated
            top_cpv = session.run(
                "MATCH (ct:Contract)-[:CATEGORIZED_AS]->(cpv:CPV) "
                "RETURN cpv.code AS code, cpv.description AS description, "
                f"  count(ct) AS contracts, {_TRUSTED_VALUE_SUM} AS total_value "
                "ORDER BY contracts DESC LIMIT 10"
            ).data()

            # Authorities count
            authority_count = session.run(
                "MATCH (a:Authority) RETURN count(a) AS n"
            ).single()["n"]

            # Lobbying stats — same Disclosure-shape fix as
            # get_lobbying_stats.
            lobbyist_count = session.run(
                "MATCH (d:Disclosure {system: 'eu-lobbying'}) "
                "RETURN count(d) AS n"
            ).single()["n"]

            lobbyists_with_ep = session.run(
                "MATCH (d:Disclosure {system: 'eu-lobbying'}) "
                "WHERE d.detail_ep_passes > 0 "
                "RETURN count(d) AS n"
            ).single()["n"]

            # :LobbyInterest doesn't exist in the graph today — the
            # sink drops list-typed `interests` fields (item #14).
            # Query left in shape that will start returning data once
            # that fix lands; honest 0 until then.
            lobby_interests = session.run(
                "MATCH (i:LobbyInterest)<-[:INTERESTED_IN]-(d) "
                "RETURN i.name AS topic, count(d) AS lobbyists "
                "ORDER BY lobbyists DESC LIMIT 10"
            ).data()

            # SanctionedEntity moved to Virtuoso during Phase 2;
            # the count comes from the SPARQL helper below. The
            # remaining counts stay in Neo4j inside this session.
            nuts_count = session.run(
                "MATCH (n:NUTSRegion) RETURN count(n) AS n"
            ).single()["n"]
            cohesion_count = session.run(
                "MATCH (d:Disclosure {system: 'eu-cohesion'}) "
                "RETURN count(d) AS n"
            ).single()["n"]
        sanctioned = self._sanctions_count_from_virtuoso()

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

    # ── Connectedness ─────────────────────────────────────────────

    def get_graph_connectedness(self) -> dict:
        """Per-label degree stats + histograms, cached for 1h.

        Degree is undirected (size((n)--())). Histograms bucket by
        log-ish ranges so a heavily-connected label (Company with
        contracts + ownership + sanctions) and a sparse one (Person,
        which only DIRECTS) both show useful shape.
        """
        now = time.monotonic()
        if (
            self._connectedness_cache is not None
            and now - self._connectedness_cache[0] < _CONNECTEDNESS_TTL_SECONDS
        ):
            return self._connectedness_cache[1]
        result = self._compute_connectedness()
        self._connectedness_cache = (now, result)
        return result

    # Portable across Neo4j 4.x and 5.x. `size((n)--())` was removed
    # in Neo4j 5 (hence the original prod 500); the list-comprehension
    # form works in both.
    _DEGREE_EXPR = "size([(n)--() | 1])"

    def _connectedness_cypher(self, label: str) -> str:
        # Label is pulled from the whitelisted tuple; Neo4j doesn't
        # support parameterized labels so f-string interpolation is
        # the canonical approach.
        return (
            f"MATCH (n:{label}) "
            f"WITH {self._DEGREE_EXPR} AS degree "
            "RETURN count(*) AS count, "
            "  sum(CASE WHEN degree = 0 THEN 1 ELSE 0 END) AS isolated, "
            "  min(degree) AS min_d, "
            "  max(degree) AS max_d, "
            "  avg(degree) AS mean_d, "
            "  percentileCont(degree, 0.5) AS median_d, "
            "  percentileCont(degree, 0.95) AS p95_d, "
            "  sum(CASE WHEN degree = 1 THEN 1 ELSE 0 END) AS b_1, "
            "  sum(CASE WHEN degree >= 2 AND degree <= 5 THEN 1 ELSE 0 END) AS b_2_5, "
            "  sum(CASE WHEN degree >= 6 AND degree <= 10 THEN 1 ELSE 0 END) AS b_6_10, "
            "  sum(CASE WHEN degree >= 11 AND degree <= 50 THEN 1 ELSE 0 END) AS b_11_50, "
            "  sum(CASE WHEN degree >= 51 AND degree <= 100 THEN 1 ELSE 0 END) AS b_51_100, "
            "  sum(CASE WHEN degree >= 101 AND degree <= 500 THEN 1 ELSE 0 END) AS b_101_500, "
            "  sum(CASE WHEN degree > 500 THEN 1 ELSE 0 END) AS b_500_plus"
        )

    def _compute_connectedness(self) -> dict:
        per_type: list[dict] = []
        errors: list[dict] = []
        with self._neo4j.session() as session:
            for label in _CONNECTEDNESS_LABELS:
                try:
                    row = session.run(self._connectedness_cypher(label)).single()
                except Exception as exc:  # pylint: disable=broad-except
                    # One label failing (missing from schema, edge-case
                    # query rejection, etc.) shouldn't sink the whole
                    # dashboard. Log the traceback, record a stub so
                    # the UI can flag it, and move on.
                    logger.exception(
                        "connectedness: label %s failed", label,
                    )
                    errors.append({"entity_type": label, "error": str(exc)})
                    continue
                count = row["count"] or 0
                if count == 0:
                    continue
                isolated = row["isolated"] or 0
                per_type.append({
                    "entity_type": label,
                    "count": count,
                    "isolated_count": isolated,
                    "isolated_pct": round(isolated / count * 100, 1),
                    "min_degree": row["min_d"],
                    "max_degree": row["max_d"],
                    "mean_degree": round(row["mean_d"] or 0.0, 2),
                    "median_degree": row["median_d"],
                    "p95_degree": row["p95_d"],
                    "histogram": [
                        {"bucket": "0", "count": isolated},
                        {"bucket": "1", "count": row["b_1"] or 0},
                        {"bucket": "2-5", "count": row["b_2_5"] or 0},
                        {"bucket": "6-10", "count": row["b_6_10"] or 0},
                        {"bucket": "11-50", "count": row["b_11_50"] or 0},
                        {"bucket": "51-100", "count": row["b_51_100"] or 0},
                        {"bucket": "101-500", "count": row["b_101_500"] or 0},
                        {"bucket": "500+", "count": row["b_500_plus"] or 0},
                    ],
                })
        return {
            "per_type": per_type,
            "errors": errors,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "cache_ttl_seconds": _CONNECTEDNESS_TTL_SECONDS,
        }

    # ── Virtuoso content stats ────────────────────────────
    #
    # Triple-store view of the data: total triples, per-named-graph
    # counts, and a class/predicate breakdown per graph. Powers the
    # /data-quality/triples view in gmr-web. Costs ~3 SPARQL queries
    # total (not N+1 per graph) so it stays cheap even with a dozen
    # named graphs.

    def get_triples_stats(
        self, *, predicate_limit: int = 20, class_limit: int = 20,
    ) -> dict:
        """Snapshot of the Virtuoso RDF store.

        Returns ``{"available": False, ...}`` when no Virtuoso
        client is configured (e.g. on envs that haven't enabled the
        sanctions cutover) so the frontend can render an explicit
        "Virtuoso not configured" state instead of erroring.
        """
        if self._virtuoso is None:
            return {
                "available": False,
                "total_triples": 0,
                "graphs": [],
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }

        # All four queries below scan the entire data partition. On a
        # full-sized store the first COUNT(*) reliably blows past the
        # default httpx 10s timeout; the client now defaults to 60s
        # but we still catch SparqlTimeout here so the dashboard gets
        # a degraded-but-renderable response instead of a 500.
        try:
            # 1. Total triples across all data graphs (the
            # http://data.fontem.eu/graph/* prefix filter excludes
            # Virtuoso's own system graphs).
            total_rows = self._virtuoso.query(
                """
                SELECT (COUNT(*) AS ?n) WHERE {
                    GRAPH ?g { ?s ?p ?o }
                    FILTER(STRSTARTS(STR(?g), "http://data.fontem.eu/graph/"))
                }
                """
            )
            total = int(total_rows[0]["n"]) if total_rows else 0

            # 2. Per-graph triple counts.
            graph_rows = self._virtuoso.query(
                """
                SELECT ?g (COUNT(*) AS ?n) WHERE {
                    GRAPH ?g { ?s ?p ?o }
                    FILTER(STRSTARTS(STR(?g), "http://data.fontem.eu/graph/"))
                } GROUP BY ?g ORDER BY DESC(?n)
                """
            )

            # 3. Per-graph predicate frequency, then trimmed to top-N
            # per graph in Python (LIMIT inside a GROUP BY-by-graph
            # query is not portable). One query for everything is much
            # cheaper than N queries per-graph.
            pred_rows = self._virtuoso.query(
                """
                SELECT ?g ?p (COUNT(*) AS ?n) WHERE {
                    GRAPH ?g { ?s ?p ?o }
                    FILTER(STRSTARTS(STR(?g), "http://data.fontem.eu/graph/"))
                } GROUP BY ?g ?p ORDER BY ?g DESC(?n)
                """
            )
        except SparqlTimeout as exc:
            logger.warning("triples_stats: SPARQL query timed out: %s", exc)
            return {
                "available": True,
                "total_triples": 0,
                "graphs": [],
                "error": str(exc),
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        preds_by_graph: dict[str, list[dict]] = {}
        for row in pred_rows:
            preds_by_graph.setdefault(row["g"], []).append(
                {"predicate": row["p"], "n": int(row["n"])},
            )

        # 4. Per-graph class counts (rdf:type → counts). Kept in its
        # own try block so a slow `?s a ?type` doesn't take the whole
        # response down — we'd rather render predicates + counts +
        # graphs and silently drop the class breakdown than 500.
        try:
            class_rows = self._virtuoso.query(
                """
                SELECT ?g ?type (COUNT(*) AS ?n) WHERE {
                    GRAPH ?g { ?s a ?type }
                    FILTER(STRSTARTS(STR(?g), "http://data.fontem.eu/graph/"))
                } GROUP BY ?g ?type ORDER BY ?g DESC(?n)
                """
            )
        except SparqlTimeout as exc:
            logger.warning(
                "triples_stats: class breakdown query timed out: %s", exc,
            )
            class_rows = []
        classes_by_graph: dict[str, list[dict]] = {}
        for row in class_rows:
            classes_by_graph.setdefault(row["g"], []).append(
                {"class": row["type"], "n": int(row["n"])},
            )

        graphs = []
        for row in graph_rows:
            iri = row["g"]
            graphs.append({
                "iri": iri,
                "label": iri.rsplit("/", 1)[-1],
                "triples": int(row["n"]),
                "top_predicates": preds_by_graph.get(iri, [])[:predicate_limit],
                "classes": classes_by_graph.get(iri, [])[:class_limit],
            })

        return {
            "available": True,
            "total_triples": total,
            "graphs": graphs,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
