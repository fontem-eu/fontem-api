"""
Graph Contract Source
======================
ContractDataSource backed by Neo4j. Queries Contract, Authority,
and CPV nodes via the graph.
"""
from __future__ import annotations

import logging

from fontem_event_schemas.integrity import contract_red_flags

from ...analysis.contract_data_source import ContractDataSource
from ...api.lang import authority_name_expr, contract_title_expr
from ._value_quality import trusted_value_sum
from .neo4j_client import Neo4jClient

logger = logging.getLogger(__name__)


class GraphContractSource(ContractDataSource):
    """Production contract data source backed by Neo4j."""

    def __init__(self, neo4j_client: Neo4jClient) -> None:
        self._neo4j = neo4j_client

    def get_company_contracts(
        self, gmr_id: str, years: int = 5, limit: int = 50,
        lang: str | None = None,
    ) -> dict:
        """Return contracts awarded to a company."""
        auth_name = authority_name_expr("a", lang)
        title_expr = contract_title_expr("ct", lang)
        with self._neo4j.session() as session:
            company = session.run(
                "MATCH (c:Company {gmr_id: $gid}) "
                "RETURN c.name AS name, c.country AS country",
                gid=gmr_id,
            ).single()
            if not company:
                return {"gmr_id": gmr_id, "contracts": [], "contract_count": 0}

            # TED awards land with the publication date (from the TED
            # XML <issue_date> field) — there is no separate "award date"
            # in the source, so the panel's `award_date` column reads
            # from `publication_date` under the hood. Likewise `cpv` is
            # written by the loader as `cpv` (the earlier `cpv_main`
            # name never existed on the nodes — the dashboard pre-fix
            # reported all 56k contracts as "missing cpv_main").
            rows = session.run(
                "MATCH (a:Authority)-[:AWARDED]->(ct:Contract)"
                "-[:AWARDED_TO]->(c:Company {gmr_id: $gid}) "
                "OPTIONAL MATCH (ct)-[:CATEGORIZED_AS]->(cpv:CPV) "
                "RETURN ct.ted_notice_id AS notice_id, "
                # ted_publication_number is the human-readable TED ID
                # ("295342-2026"); the UI uses it to short-circuit the
                # /api/contracts/<id>/ted-link redirector and link
                # straight to TED. May be null on rows ingested before
                # the publication-number capture landed (see backfill).
                "  ct.ted_publication_number AS publication_number, "
                f"  {title_expr} AS title, ct.value_eur AS value_eur, "
                "  ct.publication_date AS award_date, ct.cpv AS cpv, "
                "  ct.value_low_confidence AS value_low_confidence, "
                "  ct.value_quality_flag AS value_quality_flag, "
                "  ct.value_payable_discrepancy AS value_payable_discrepancy, "
                "  ct.estimated_value_eur AS estimated_value_eur, "
                "  ct.procedure_type AS procedure_type, "
                "  ct.ted_url AS ted_url, "
                f"  {auth_name} AS authority, a.country AS authority_country, "
                # `authority_id` lets the contracts UI link each row's
                # authority cell back to the authority profile. Without
                # this the panel could only render the name as plain
                # text — there was no path from "X awarded by Y" back
                # to Y's profile page.
                "  a.authority_id AS authority_id, "
                "  cpv.description AS cpv_description "
                "ORDER BY ct.publication_date DESC LIMIT $limit",
                gid=gmr_id, limit=limit,
            ).data()

            total_value = session.run(
                "MATCH (ct:Contract)-[:AWARDED_TO]->"
                "(c:Company {gmr_id: $gid}) "
                "RETURN " + trusted_value_sum("ct") + " AS total, count(ct) AS cnt",
                gid=gmr_id,
            ).single()

        contracts = []
        for r in rows:
            cpv_label = r["cpv"] or ""
            if r.get("cpv_description"):
                cpv_label = f"{r['cpv']} - {r['cpv_description']}"
            contracts.append({
                "ted_notice_id": r["notice_id"],
                "ted_publication_number": r.get("publication_number"),
                "title": r["title"],
                "value_eur": r["value_eur"],
                "estimated_value_eur": r.get("estimated_value_eur"),
                "value_low_confidence": r.get("value_low_confidence"),
                "value_quality_flag": r.get("value_quality_flag"),
                "value_payable_discrepancy": r.get("value_payable_discrepancy"),
                "award_date": r["award_date"],
                "cpv": cpv_label,
                "procedure_type": r["procedure_type"],
                "ted_url": r["ted_url"],
                "authority": r["authority"],
                "authority_id": r["authority_id"],
                "authority_country": r["authority_country"],
            })

        return {
            "gmr_id": gmr_id,
            "company_name": company["name"],
            "country": company["country"],
            "total_contract_value_eur": total_value["total"] if total_value else 0,
            "contract_count": total_value["cnt"] if total_value else 0,
            "contracts": contracts,
        }

    def get_authority_contracts(
        self, authority_id: str, years: int = 5, limit: int = 50,
        lang: str | None = None,
    ) -> dict:
        """Return contracts issued by an authority."""
        auth_name = authority_name_expr("a", lang)
        title_expr = contract_title_expr("ct", lang)
        with self._neo4j.session() as session:
            authority = session.run(
                "MATCH (a:Authority {authority_id: $aid}) "
                f"RETURN {auth_name} AS name, a.country AS country",
                aid=authority_id,
            ).single()
            if not authority:
                return {"authority_id": authority_id, "contracts": [], "contract_count": 0}

            # Same property-name remap as get_company_contracts —
            # award_date / cpv aliases read from publication_date / cpv.
            rows = session.run(
                "MATCH (a:Authority {authority_id: $aid})"
                "-[:AWARDED]->(ct:Contract)-[:AWARDED_TO]->(c:Company) "
                "OPTIONAL MATCH (ct)-[:CATEGORIZED_AS]->(cpv:CPV) "
                "RETURN ct.ted_notice_id AS notice_id, "
                "  ct.ted_publication_number AS publication_number, "
                f"  {title_expr} AS title, ct.value_eur AS value_eur, "
                "  ct.publication_date AS award_date, ct.cpv AS cpv, "
                "  ct.value_low_confidence AS value_low_confidence, "
                "  ct.value_quality_flag AS value_quality_flag, "
                "  ct.value_payable_discrepancy AS value_payable_discrepancy, "
                "  ct.estimated_value_eur AS estimated_value_eur, "
                "  ct.procedure_type AS procedure_type, "
                "  ct.ted_url AS ted_url, "
                "  c.name AS contractor, c.country AS contractor_country, "
                "  c.gmr_id AS contractor_gmr_id, "
                "  cpv.description AS cpv_description "
                "ORDER BY ct.publication_date DESC LIMIT $limit",
                aid=authority_id, limit=limit,
            ).data()

            total = session.run(
                "MATCH (a:Authority {authority_id: $aid})"
                "-[:AWARDED]->(ct:Contract) "
                "RETURN " + trusted_value_sum("ct") + " AS total, count(ct) AS cnt",
                aid=authority_id,
            ).single()

        contracts = []
        for r in rows:
            cpv_label = r["cpv"] or ""
            if r.get("cpv_description"):
                cpv_label = f"{r['cpv']} - {r['cpv_description']}"
            contracts.append({
                "ted_notice_id": r["notice_id"],
                "ted_publication_number": r.get("publication_number"),
                "title": r["title"],
                "value_eur": r["value_eur"],
                "estimated_value_eur": r.get("estimated_value_eur"),
                "value_low_confidence": r.get("value_low_confidence"),
                "value_quality_flag": r.get("value_quality_flag"),
                "value_payable_discrepancy": r.get("value_payable_discrepancy"),
                "award_date": r["award_date"],
                "cpv": cpv_label,
                "procedure_type": r["procedure_type"],
                "ted_url": r["ted_url"],
                "contractor": r["contractor"],
                "contractor_country": r["contractor_country"],
                "contractor_gmr_id": r["contractor_gmr_id"],
            })

        return {
            "authority_id": authority_id,
            "authority_name": authority["name"],
            "country": authority["country"],
            "total_spend_eur": total["total"] if total else 0,
            "contract_count": total["cnt"] if total else 0,
            "contracts": contracts,
        }

    def get_contract_detail(
        self, notice_id: str, lang: str | None = None,
    ) -> dict | None:
        """Return full detail for a single contract."""
        with self._neo4j.session() as session:
            row = session.run(
                "MATCH (a:Authority)-[:AWARDED]->(ct:Contract "
                "{ted_notice_id: $nid})-[:AWARDED_TO]->(c:Company) "
                "OPTIONAL MATCH (ct)-[:CATEGORIZED_AS]->(cpv:CPV) "
                "RETURN ct, a, c, cpv",
                nid=notice_id,
            ).single()
        if not row:
            return None
        ct = row["ct"]
        auth_node = row["a"]
        # Full-node projection — coalesce in Python. `lang` is already
        # whitelisted by the handler via safe_lang(), so the dynamic key
        # lookup is safe.
        auth_name = (
            auth_node.get(f"name_{lang}") if lang else None
        ) or auth_node["name"]
        title = (
            ct.get(f"title_{lang}") if lang else None
        ) or ct.get("title")
        # API output keys are kept stable for the frontend; the source
        # property names are the storage ones (see render_upsert_contract
        # in fontem-neo4j-sink). Notes:
        #   - `description` doesn't exist on the Contract node at all;
        #     the TED loader doesn't carry one, so this stays None until
        #     a future loader version adds it.
        #   - `cpv_main` reads from `cpv`, `award_date` reads from
        #     `publication_date` (no separate award date in the TED XML).
        return {
            "ted_notice_id": ct["ted_notice_id"],
            "ted_publication_number": ct.get("ted_publication_number"),
            "ted_url": ct.get("ted_url"),
            "title": title,
            "description": None,
            "value_eur": ct.get("value_eur"),
            "cpv_main": ct.get("cpv"),
            "procedure_type": ct.get("procedure_type"),
            "award_date": ct.get("publication_date"),
            "authority": {
                "name": auth_name,
                "country": auth_node.get("country"),
            },
            "contractor": {
                "gmr_id": row["c"]["gmr_id"],
                "name": row["c"]["name"],
                "country": row["c"].get("country"),
            },
            # Tender-integrity: the raw eForms fields + the shared keystone's
            # derived red flags (single-bidder etc.), computed on the fly so
            # the detail page works even before the sink re-materialises them.
            "integrity": self._integrity_block(ct),
        }

    @staticmethod
    def _integrity_block(ct) -> dict:
        fields = {
            "procedure_type": ct.get("procedure_type"),
            "tenders_received": ct.get("tenders_received"),
            "award_criterion_type": ct.get("award_criterion_type"),
            "submission_deadline": ct.get("submission_deadline"),
            "is_framework": ct.get("is_framework"),
            "eu_funded": ct.get("eu_funded"),
            "funding_programme": ct.get("funding_programme"),
        }
        return {**fields, **contract_red_flags(fields)}

    def get_single_bidder_stats(
        self, country: str | None = None, cpv: str | None = None,
    ) -> dict:
        """Single-bidder rate over contracts with a known bidder count,
        optionally scoped by authority country and/or CPV prefix. The
        EC Single Market Scoreboard headline indicator."""
        where = ["c.tenders_received IS NOT NULL"]
        params: dict = {}
        if country:
            where.append("c.country = $country")
            params["country"] = country
        if cpv:
            where.append("c.cpv STARTS WITH $cpv")
            params["cpv"] = cpv
        clause = " AND ".join(where)
        with self._neo4j.session() as session:
            row = session.run(
                f"MATCH (c:Contract) WHERE {clause} "
                "RETURN count(*) AS total, "
                "count(CASE WHEN c.tenders_received = 1 THEN 1 END) AS single",
                **params,
            ).single()
        total = (row["total"] if row else 0) or 0
        single = (row["single"] if row else 0) or 0
        return {
            "scope": {"country": country, "cpv": cpv},
            "total": total,
            "single_bidder": single,
            "single_bidder_rate": (single / total) if total else None,
        }

    def get_single_bidder_by_country(self, min_sample: int = 20,
                                     limit: int = 40) -> list[dict]:
        """Single-bidder rate per authority country (min_sample contracts
        for a meaningful rate), highest first — the cross-country
        benchmark that keeps any one figure honest."""
        with self._neo4j.session() as session:
            return session.run(
                "MATCH (c:Contract) WHERE c.tenders_received IS NOT NULL "
                "AND c.country IS NOT NULL "
                "WITH c.country AS country, count(*) AS total, "
                "count(CASE WHEN c.tenders_received = 1 THEN 1 END) AS single "
                "WHERE total >= $min_sample "
                "RETURN country, total, single, "
                "toFloat(single) / total AS single_bidder_rate "
                "ORDER BY single_bidder_rate DESC LIMIT $limit",
                min_sample=min_sample, limit=limit,
            ).data()

    def get_sector_summary(
        self, country: str | None = None, year: int | None = None,
    ) -> list[dict]:
        """Aggregated contract values by CPV division."""
        where_parts = []
        params: dict = {}
        if country:
            where_parts.append("ct.country = $country")
            params["country"] = country
        if year:
            where_parts.append(
                "ct.publication_date STARTS WITH $year_prefix"
            )
            params["year_prefix"] = str(year)

        where_clause = "WHERE " + " AND ".join(where_parts) if where_parts else ""

        with self._neo4j.session() as session:
            rows = session.run(
                f"MATCH (ct:Contract)-[:CATEGORIZED_AS]->(cpv:CPV) "
                f"{where_clause} "
                f"RETURN cpv.division AS division, "
                f"  cpv.description AS description, "
                f"  {trusted_value_sum('ct')} AS total_value, "
                f"  count(ct) AS contract_count "
                f"ORDER BY total_value DESC LIMIT 20",
                **params,
            ).data()
        return rows


    def get_stored_publication_number(self, notice_id: str) -> str | None:
        """Look up just the pub-num for a contract row, no joins.

        Used by the /ted-link redirector to skip the TED v3 search call
        when the ETL has already resolved + persisted the value. Returns
        ``None`` for contracts whose ted_publication_number is null,
        not yet ingested, or whose ted_notice_id doesn't exist — the
        caller falls back to the live search lookup in all three cases.

        Tight single-row read because this is on the click-through hot
        path; the LRU cache in src/services/ted_lookup.py wins again
        after one hit per pod, but this query keeps cold hits off TED.
        """
        with self._neo4j.session() as session:
            row = session.run(
                "MATCH (ct:Contract {ted_notice_id: $nid}) "
                "RETURN ct.ted_publication_number AS pub_num "
                "LIMIT 1",
                nid=notice_id,
            ).single()
        if row is None:
            return None
        pub_num = row["pub_num"]
        return str(pub_num) if pub_num else None
