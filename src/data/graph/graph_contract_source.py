"""
Graph Contract Source
======================
ContractDataSource backed by Neo4j. Queries Contract, Authority,
and CPV nodes via the graph.
"""
from __future__ import annotations

import logging
import re
from itertools import zip_longest

from fontem_event_schemas.integrity import contract_red_flags

from ...analysis.contract_data_source import ContractDataSource
from ...api.lang import authority_name_expr, contract_title_expr
from ...services.ted_lookup import detail_url_for
from .identity import identity_class
from ._value_quality import (
    canonical_count,
    canonical_predicate,
    trusted_value_sum,
)
from .neo4j_client import Neo4jClient

logger = logging.getLogger(__name__)

#: How a contract list may be ordered, as ORDER BY clauses over the
#: projected aliases (ordering by an alias is always legal after
#: RETURN DISTINCT; ordering by a bare property is not).
#:
#: Every clause puts missing data LAST — a contract with no value is the
#: least useful row to open the list with — and ends on `notice_id` so
#: equal rows keep a fixed order. Without that tie-break the same page
#: could come back shuffled, which is what made these lists look
#: randomly sorted: most rows carry no value at all (81 of 100 on a
#: typical authority), so "sort by value" left almost everything tied.
CONTRACT_SORTS = {
    "recent": "ORDER BY award_date IS NULL, award_date DESC, notice_id",
    "oldest": "ORDER BY award_date IS NULL, award_date ASC, notice_id",
    "value_desc": (
        "ORDER BY value_eur IS NULL, value_eur DESC, award_date DESC, notice_id"
    ),
    "value_asc": (
        "ORDER BY value_eur IS NULL, value_eur ASC, award_date DESC, notice_id"
    ),
}
DEFAULT_CONTRACT_SORT = "recent"

#: How many of a framework's other award notices the detail page carries.
#: Of 176 real frameworks sampled, 121 (68.8%) have a single award notice
#: and the mean cluster is 6.04, so ten covers all but the long tail — and
#: the tail is what `sibling_count` is for.
FRAMEWORK_SIBLING_LIMIT = 10

#: The publication-number form of the OPT-100 grouping key ("536632-2024").
#: TED's per-notice routes are keyed by publication number and by nothing
#: else: /en/notice/536632-2024/xml answers 200, while the other form the
#: key takes — an eForms notice UUID — 404s there and renders the known
#: blank 202 page under /en/notice/-/detail/ (see services/ted_lookup).
#: So a UUID-form key gets no link at all rather than a broken one.
_PUBLICATION_NUMBER = re.compile(r"\d+-\d{4}")


def framework_ted_url(framework_id: str | None) -> str | None:
    """The TED page for a framework grouping key, or None when the key
    is in the UUID form TED cannot resolve to a page."""
    if framework_id and _PUBLICATION_NUMBER.fullmatch(framework_id):
        return detail_url_for(framework_id)
    return None


def contract_order_by(sort: str | None) -> str:
    """The ORDER BY clause for `sort`, defaulting to most recent first.

    Unknown values fall back rather than raise: the API validates the
    parameter, and a data source should not be the thing that 500s on a
    typo from an internal caller.
    """
    return CONTRACT_SORTS.get(sort or DEFAULT_CONTRACT_SORT,
                              CONTRACT_SORTS[DEFAULT_CONTRACT_SORT])


class GraphContractSource(ContractDataSource):
    """Production contract data source backed by Neo4j."""

    def __init__(self, neo4j_client: Neo4jClient) -> None:
        self._neo4j = neo4j_client

    def get_company_contracts(
        self, gmr_id: str, years: int = 5, limit: int = 50,
        lang: str | None = None, sort: str | None = None,
    ) -> dict:
        """Return contracts awarded to a company, most recent first
        unless `sort` says otherwise (see CONTRACT_SORTS)."""
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
                # Across the identity class, not just this record: the
                # consolidator's approved merges are what make a company
                # page show the entity's contracts rather than one
                # duplicate's. DISTINCT because a contract awarded to
                # two members of the class is reachable twice.
                identity_class("Company", "gmr_id")
                + "MATCH (a:Authority)-[:AWARDED]->(ct:Contract)"
                "-[:AWARDED_TO]->(me) "
                "OPTIONAL MATCH (ct)-[:CATEGORIZED_AS]->(cpv:CPV) "
                "RETURN DISTINCT ct.ted_notice_id AS notice_id, "
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
                # Modification (errata) fields: a can-modif contract
                # self-contains the value change (value_before_eur ->
                # value_eur). notice_type flags it; modifies_publication_
                # number points at the original notice it amends.
                "  ct.notice_type AS notice_type, "
                "  ct.value_currency AS value_currency, "
                "  ct.value_original AS value_original, "
                "  ct.value_before_eur AS value_before_eur, "
                "  ct.value_confidence AS value_confidence, "
                "  ct.value_quarantined AS value_quarantined, "
                "  ct.value_quarantine_reason AS value_quarantine_reason, "
                "  ct.value_before_original AS value_before_original, "
                "  ct.modifies_publication_number AS modifies_publication_number, "
                "  ct.procedure_type AS procedure_type, "
                "  ct.ted_url AS ted_url, "
                # "Part of a framework agreement" tag on the row. Never
                # coalesced to false: pre-eForms notices carry no
                # ContractingSystemTypeCode at all, and "we don't know"
                # is not "not a framework".
                "  ct.is_framework AS is_framework, "
                f"  {auth_name} AS authority, a.country AS authority_country, "
                # `authority_id` lets the contracts UI link each row's
                # authority cell back to the authority profile. Without
                # this the panel could only render the name as plain
                # text — there was no path from "X awarded by Y" back
                # to Y's profile page.
                "  a.authority_id AS authority_id, "
                "  cpv.description AS cpv_description "
                + f" {contract_order_by(sort)} LIMIT $limit",
                gid=gmr_id, limit=limit,
            ).data()

            total_value = session.run(
                identity_class("Company", "gmr_id")
                + "MATCH (ct:Contract)-[:AWARDED_TO]->(me) "
                # DISTINCT before the aggregates, not inside them:
                # canonical_count sums over ROWS, so a contract reachable
                # through two class members would be counted twice and
                # its value added twice.
                "WITH DISTINCT ct "
                "RETURN " + trusted_value_sum("ct") + " AS total, "
                + canonical_count("ct") + " AS cnt",
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
                "notice_type": r.get("notice_type"),
                "value_currency": r.get("value_currency"),
                "value_original": r.get("value_original"),
                "value_before_eur": r.get("value_before_eur"),
                # data-confidence surface (quarantine + low-confidence
                # marks rendered by DataConfidenceIcon in the web app)
                "value_confidence": r.get("value_confidence"),
                "value_quarantined": r.get("value_quarantined"),
                "value_quarantine_reason": r.get("value_quarantine_reason"),
                "value_before_original": r.get("value_before_original"),
                "modifies_publication_number": r.get("modifies_publication_number"),
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
                "is_framework": r.get("is_framework"),
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
        lang: str | None = None, sort: str | None = None,
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
                # Across the identity class — eu-LISA is two Authority
                # nodes the consolidator merged, with three contracts on
                # one and one on the other, so an unresolved read shows
                # whichever the caller happened to name. `a` is rebound
                # to the class member that actually awarded each
                # contract, so the row's authority fields stay truthful.
                identity_class("Authority", "authority_id", param="aid",
                               out="a")
                + "MATCH (a)-[:AWARDED]->(ct:Contract) "
                # The supplier is optional, as on the detail page: a
                # contract whose only supplier the cleaning stage
                # withheld (data-backlog Part 5, C2 - the name field
                # held a sentence, a URL, a placeholder) has no
                # AWARDED_TO edge, and an inner MATCH dropped it from
                # the rows while the totals below still counted it, so
                # a buyer's page summed contracts the list never showed.
                # The row keeps contractor null and carries the withheld
                # count, which is what the UI labels.
                "OPTIONAL MATCH (ct)-[:AWARDED_TO]->(c:Company) "
                "OPTIONAL MATCH (ct)-[:CATEGORIZED_AS]->(cpv:CPV) "
                "RETURN DISTINCT ct.ted_notice_id AS notice_id, "
                "  ct.ted_publication_number AS publication_number, "
                f"  {title_expr} AS title, ct.value_eur AS value_eur, "
                "  ct.publication_date AS award_date, ct.cpv AS cpv, "
                "  ct.value_low_confidence AS value_low_confidence, "
                "  ct.value_quality_flag AS value_quality_flag, "
                "  ct.value_payable_discrepancy AS value_payable_discrepancy, "
                "  ct.estimated_value_eur AS estimated_value_eur, "
                # Modification (errata) fields: a can-modif contract
                # self-contains the value change (value_before_eur ->
                # value_eur). notice_type flags it; modifies_publication_
                # number points at the original notice it amends.
                "  ct.notice_type AS notice_type, "
                "  ct.value_currency AS value_currency, "
                "  ct.value_original AS value_original, "
                "  ct.value_before_eur AS value_before_eur, "
                "  ct.value_confidence AS value_confidence, "
                "  ct.value_quarantined AS value_quarantined, "
                "  ct.value_quarantine_reason AS value_quarantine_reason, "
                "  ct.value_before_original AS value_before_original, "
                "  ct.modifies_publication_number AS modifies_publication_number, "
                "  ct.procedure_type AS procedure_type, "
                "  ct.ted_url AS ted_url, "
                # "Part of a framework agreement" tag on the row. Never
                # coalesced to false: pre-eForms notices carry no
                # ContractingSystemTypeCode at all, and "we don't know"
                # is not "not a framework".
                "  ct.is_framework AS is_framework, "
                "  c.name AS contractor, c.country AS contractor_country, "
                "  c.gmr_id AS contractor_gmr_id, "
                "  ct.suppliers_withheld_count AS supplier_withheld_count, "
                "  cpv.description AS cpv_description "
                + f" {contract_order_by(sort)} LIMIT $limit",
                aid=authority_id, limit=limit,
            ).data()

            total = session.run(
                identity_class("Authority", "authority_id", param="aid",
                               out="a")
                + "MATCH (a)-[:AWARDED]->(ct:Contract) "
                # DISTINCT before the aggregates: canonical_count sums
                # over ROWS, so a contract reachable through two class
                # members would count twice and add its value twice.
                "WITH DISTINCT ct "
                "RETURN " + trusted_value_sum("ct") + " AS total, "
                + canonical_count("ct") + " AS cnt",
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
                "notice_type": r.get("notice_type"),
                "value_currency": r.get("value_currency"),
                "value_original": r.get("value_original"),
                "value_before_eur": r.get("value_before_eur"),
                # data-confidence surface (quarantine + low-confidence
                # marks rendered by DataConfidenceIcon in the web app)
                "value_confidence": r.get("value_confidence"),
                "value_quarantined": r.get("value_quarantined"),
                "value_quarantine_reason": r.get("value_quarantine_reason"),
                "value_before_original": r.get("value_before_original"),
                "modifies_publication_number": r.get("modifies_publication_number"),
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
                # How many suppliers the notice named that the cleaning
                # stage refused to mint a company for. Non-zero with a
                # null contractor is "supplier not disclosed in the
                # notice"; a row written before the cleaning stage
                # carries no count at all, which reads as zero.
                "supplier_withheld_count": r.get("supplier_withheld_count") or 0,
                "is_framework": r.get("is_framework"),
            })

        return {
            "authority_id": authority_id,
            "authority_name": authority["name"],
            "country": authority["country"],
            "total_spend_eur": total["total"] if total else 0,
            "contract_count": total["cnt"] if total else 0,
            "contracts": contracts,
        }

    def get_company_cohesion_grants(
        self, gmr_id: str, limit: int = 50,
    ) -> dict:
        """EU cohesion (Kohesio) grants attained by a company — the
        eu-cohesion disclosures FILED_BY it, with the EU contribution, fund,
        programme and dates. Mirrors get_company_contracts on the funding
        side. Unnamed-beneficiary 'nan' nodes are excluded."""
        with self._neo4j.session() as session:
            company = session.run(
                "MATCH (c:Company {gmr_id: $gid}) "
                "RETURN c.name AS name, c.country AS country",
                gid=gmr_id,
            ).single()
            if not company:
                return {"gmr_id": gmr_id, "grants": [], "grant_count": 0,
                        "total_eu_contribution": 0}
            rows = session.run(
                "MATCH (:Company {gmr_id: $gid})<-[:FILED_BY]-"
                "(d:Disclosure {system:'eu-cohesion'}) "
                "RETURN d.title AS title, "
                "  d.detail_eu_contribution AS eu_contribution, "
                "  d.detail_total_budget AS total_budget, "
                "  d.detail_fund AS fund, d.detail_programme AS programme, "
                "  d.detail_start_date AS start_date, "
                "  d.detail_end_date AS end_date, "
                "  d.detail_nuts_code AS nuts, d.year AS year "
                "ORDER BY coalesce(d.detail_start_date, toString(d.year)) DESC "
                "LIMIT $limit",
                gid=gmr_id, limit=limit,
            ).data()
            summary = session.run(
                "MATCH (:Company {gmr_id: $gid})<-[:FILED_BY]-"
                "(d:Disclosure {system:'eu-cohesion'}) "
                "RETURN count(d) AS grant_count, "
                "  sum(coalesce(d.detail_eu_contribution, 0)) AS total_eu",
                gid=gmr_id,
            ).single()
        return {
            "gmr_id": gmr_id, "name": company["name"],
            "country": company["country"], "grants": rows,
            "grant_count": summary["grant_count"],
            "total_eu_contribution": summary["total_eu"],
        }

    def get_contract_detail(
        self, notice_id: str, lang: str | None = None,
    ) -> dict | None:
        """Return full detail for a single contract.

        ``notice_id`` may name any notice of the contract, not only the
        current one. A :Contract carries the ted_notice_id of its current
        notice, and that id moves whenever a newer notice (a modification,
        a republication) joins the chain — so a link made earlier, in a
        feed card, a bookmark or a search index, would 404 on a contract
        that is still there. The superseded :Notice keeps its own id and
        its NOTICE_OF edge, which is what resolves it; both lookups are
        index seeks. The page is always the contract's current state.

        The awardee is optional: a contract with no AWARDED_TO edge is
        still a contract, and the briefing feed lists those as awarded to
        "an undisclosed supplier". Since the cleaning stage (data-backlog
        Part 5, C2) the contract also says WHY there is nobody to link:
        ``suppliers_withheld`` lists the names the notice published that
        the cleaner refused to mint a company for (a sentence, a URL, a
        placeholder in the name field), each with the rule that withheld
        it, and ``supplier_not_disclosed`` is true when that list is the
        only trace of a supplier - no company was named, so the honest
        reading is "not disclosed in the notice", raw text kept as a
        pointer to where the real award was published.
        """
        with self._neo4j.session() as session:
            row = session.run(
                "CALL { "
                "  MATCH (ct:Contract {ted_notice_id: $nid}) RETURN ct "
                "  UNION "
                "  MATCH (:Notice {ted_notice_id: $nid})"
                "-[:NOTICE_OF]->(ct:Contract) RETURN ct "
                "} "
                "WITH ct LIMIT 1 "
                "MATCH (a:Authority)-[:AWARDED]->(ct) "
                "OPTIONAL MATCH (ct)-[:AWARDED_TO]->(c:Company) "
                "OPTIONAL MATCH (ct)-[:CATEGORIZED_AS]->(cpv:CPV) "
                "RETURN ct, a, c, cpv",
                nid=notice_id,
            ).single()
            if not row:
                return None
            framework = self._framework_block(row["ct"], session, lang)
        ct = row["ct"]
        auth_node = row["a"]
        contractor = {
            "gmr_id": row["c"]["gmr_id"],
            "name": row["c"]["name"],
            "country": row["c"].get("country"),
        } if row["c"] is not None else None
        suppliers_withheld = self._withheld_suppliers(ct)
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
                # authority_id, not gmr_id: Authority nodes carry the
                # former on all 207,002 of them and the latter on none.
                # Without it the detail page can name the buyer but not
                # link to it, while the supplier beside it is clickable.
                "authority_id": auth_node.get("authority_id"),
                "name": auth_name,
                "country": auth_node.get("country"),
            },
            "contractor": contractor,
            "suppliers_withheld": suppliers_withheld,
            "supplier_not_disclosed": contractor is None and bool(suppliers_withheld),
            # Cleaning-stage marks (Part 5): the ids of the rules that
            # fired on this notice, and why a value was quarantined
            # rather than read. Empty / null when the contract predates
            # the cleaning stage or nothing fired.
            "cleaning_rules": list(ct.get("cleaning_rules") or []),
            "value_quarantine_reason": ct.get("value_quarantine_reason"),
            # Tender-integrity: the raw eForms fields + the shared keystone's
            # derived red flags (single-bidder etc.), computed on the fly so
            # the detail page works even before the sink re-materialises them.
            "integrity": self._integrity_block(ct),
            # Framework agreement: null unless there is something to say.
            "framework": framework,
        }

    def _framework_block(self, ct, session, lang: str | None) -> dict | None:
        """What this contract says about the framework agreement it is
        part of, or None when it says nothing.

        Null, not an empty object: a contract with no grouping key is not
        a contract with no framework. Coverage of OPT-100 on
        framework-flagged award notices runs 28.8% (2024), 89.4% (2025),
        98.3% (2026), and pre-eForms notices carry neither the key nor
        ``is_framework``, so "absent" has to render as nothing rather
        than as "no framework".

        Every term here is what THIS notice published. ``is_framework``
        (the lot's ContractingSystemTypeCode starting `fa`) means the
        notice belongs to a framework procedure — 344 of 351 sampled
        CALL-OFFS carry it too — so nothing may be read out of this block
        as "this contract IS the framework", and the ceiling is the
        procedure's capacity, never money paid.
        """
        framework_id = ct.get("framework_id")
        if not framework_id and not ct.get("is_framework"):
            return None
        siblings, sibling_count = (
            self._framework_siblings(
                session, framework_id, ct["ted_notice_id"], lang)
            if framework_id else ([], 0)
        )
        return {
            "framework_id": framework_id,
            "ted_url": framework_ted_url(framework_id),
            "max_value_eur": ct.get("framework_max_value_eur"),
            "reestimated_value_eur": ct.get("framework_reestimated_value_eur"),
            "duration_months": ct.get("framework_duration_months"),
            "max_operators": ct.get("framework_max_operators"),
            "sibling_count": sibling_count,
            "siblings": siblings,
        }

    @staticmethod
    def _framework_siblings(
        session, framework_id: str, notice_id: str, lang: str | None,
    ) -> tuple[list[dict], int]:
        """The other award notices carrying the same grouping key, newest
        first, capped at FRAMEWORK_SIBLING_LIMIT, with the total.

        Both statements are equality seeks on the `contract_framework_id`
        index (fontem-neo4j-sink#162). Without it the planner falls back
        to scanning every :Contract: PROFILEd on fontem-prod on 2026-09-23
        (3,606,471 contracts) the page read cost 7,212,943 DbHits / 3.9 s
        and the count 7,212,943 DbHits / 3.8–52.8 s across runs, i.e. past
        the 8 s transaction budget on a cold page cache. With the index
        the same shape is a NodeIndexSeek: 20 DbHits / 1 ms for a one-row
        cluster and 2,387 DbHits / 5 ms for a 1,136-row one, ~7x the
        largest framework observed (148 award notices).

        LIMIT lands before the supplier expansion so the OPTIONAL MATCH
        only ever touches ten contracts, and the suppliers are ordered
        before they are collected so a multi-supplier award always shows
        the same name rather than reshuffling between requests.

        Self-exclusion is on the contract's OWN ted_notice_id, not the id
        the caller asked for: a detail page reached through a superseded
        notice would otherwise list the contract as its own sibling.
        """
        title_expr = contract_title_expr("s", lang)
        rows = session.run(
            "MATCH (s:Contract {framework_id: $fid}) "
            "WHERE s.ted_notice_id <> $nid "
            "WITH s ORDER BY s.publication_date IS NULL, "
            "  s.publication_date DESC, s.ted_notice_id "
            "LIMIT $limit "
            "OPTIONAL MATCH (s)-[:AWARDED_TO]->(co:Company) "
            "WITH s, co ORDER BY co.name "
            "WITH s, head(collect(co.name)) AS supplier "
            "RETURN s.ted_notice_id AS ted_notice_id, "
            f"  {title_expr} AS title, s.country AS country, "
            "  s.value_eur AS value_eur, "
            "  s.publication_date AS publication_date, supplier "
            "ORDER BY publication_date IS NULL, publication_date DESC, "
            "  ted_notice_id",
            fid=framework_id, nid=notice_id, limit=FRAMEWORK_SIBLING_LIMIT,
        ).data()
        total = session.run(
            "MATCH (s:Contract {framework_id: $fid}) "
            "WHERE s.ted_notice_id <> $nid "
            "RETURN count(s) AS sibling_count",
            fid=framework_id, nid=notice_id,
        ).single()
        siblings = [{
            "ted_notice_id": r["ted_notice_id"],
            "title": r.get("title"),
            "country": r.get("country"),
            "value_eur": r.get("value_eur"),
            "publication_date": r.get("publication_date"),
            "supplier": r.get("supplier"),
        } for r in rows]
        return siblings, (total["sibling_count"] if total else len(siblings))

    @staticmethod
    def _withheld_suppliers(ct) -> list[dict]:
        """The names the notice published that the cleaning stage
        withheld, as ``[{name_raw, reason}]``.

        The sink stores them as two parallel lists in payload order
        (Neo4j holds no list of maps): ``suppliers_withheld_names[i]``
        was withheld for ``suppliers_withheld_reasons[i]``, a rule id
        such as ``it.notice_text_in_supplier_name``. Absent lists read
        as nothing withheld; a reason the sink wrote as '' (or a list
        that fell short) reads as an unknown reason rather than a
        misaligned one, and a nameless entry has nothing to show.
        """
        names = ct.get("suppliers_withheld_names") or []
        reasons = ct.get("suppliers_withheld_reasons") or []
        return [
            {"name_raw": name, "reason": reason or None}
            for name, reason in zip_longest(names, reasons)
            if name
        ]

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
        where = ["c.tenders_received IS NOT NULL", canonical_predicate("c")]
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
                f"AND {canonical_predicate('c')} "
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
                f"  {canonical_count('ct')} AS contract_count "
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
