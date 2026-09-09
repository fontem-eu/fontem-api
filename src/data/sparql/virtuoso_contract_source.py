"""Company contracts read from Virtuoso, aggregated across owl:sameAs.

Why this exists
---------------
Virtuoso is where identity lives: two records the consolidator approved
as the same company are one entity there, and their contracts are split
across both subjects. Neo4j has no such notion — a :SAME_AS edge was
removed from it precisely because nothing followed it — so a company
page built on Neo4j shows one record's contracts and silently omits its
duplicates'. Measured on prod: a company whose bare subject has 3
contracts has 566 across its closure.

Only get_company_contracts moves. Everything else on the interface
delegates to the Neo4j-backed source, including anything that needs real
graph traversal (the corporate group walks SUBSIDIARY_OF*1..5, which is
what Neo4j is for and what it keeps).

Two queries, not one
--------------------
The obvious single query joins the authority graph in an OPTIONAL to get
each contract's authority name. Virtuoso's planner costs that at ~10,000
seconds and refuses it outright (the 60s estimate limit). Fetching the
rows first and resolving the authority IRIs in a second VALUES-bound
query runs in 0.016s. One extra round trip is a fair price for a query
the store will actually execute.
"""

from __future__ import annotations

import logging
from typing import Any

from src.analysis.contract_data_source import ContractDataSource
from src.data.sparql.same_as import OWL_SAME_AS
from src.data.sparql.virtuoso_client import SparqlTimeout, VirtuosoClient

logger = logging.getLogger(__name__)

_ID = "http://data.fontem.eu/id"
_ONT = "http://data.fontem.eu/ontology#"
_G_COMPANY = "http://data.fontem.eu/graph/company"
_G_CONTRACT = "http://data.fontem.eu/graph/contract"
_G_AUTHORITY = "http://data.fontem.eu/graph/authority"
_G_COHESION = "http://data.fontem.eu/graph/eu_cohesion"
_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
_P17 = "http://www.wikidata.org/prop/direct/P17"

#: Indent between OPTIONAL lines inside a generated WHERE block. The
#: three builders that assemble one share it so a change to the query
#: layout cannot leave them formatted differently.
_OPTIONAL_SEP = "\n    "

# Fields read straight off the notice/contract subject. Name here maps to
# the ontology predicate; the row key is what the API already returns, so
# the wire shape does not change when the backing store does.
_CONTRACT_FIELDS: tuple[tuple[str, str], ...] = (
    ("notice_id", "tedNoticeId"),
    ("publication_number", "tedPublicationNumber"),
    ("value_eur", "valueEur"),
    ("award_date", "publicationDate"),
    ("cpv", "cpv"),
    ("notice_type", "noticeType"),
    ("value_currency", "valueCurrency"),
    ("value_original", "valueOriginal"),
    ("value_before_eur", "valueBeforeEur"),
    ("value_before_original", "valueBeforeOriginal"),
    ("value_confidence", "valueConfidence"),
    ("value_low_confidence", "valueLowConfidence"),
    ("value_quality_flag", "valueQualityFlag"),
    ("value_payable_discrepancy", "valuePayableDiscrepancy"),
    ("estimated_value_eur", "estimatedValueEur"),
    ("value_quarantined", "valueQuarantined"),
    ("value_quarantine_reason", "valueQuarantineReason"),
    ("modifies_publication_number", "modifiesPublicationNumber"),
    ("procedure_type", "procedureType"),
    ("current_value", "currentValue"),
    ("is_current", "isCurrent"),
)


class VirtuosoContractSource(ContractDataSource):
    """get_company_contracts from Virtuoso; everything else delegates."""

    def __init__(self, fallback: ContractDataSource, virtuoso: VirtuosoClient | None):
        self._fallback = fallback
        self._virtuoso = virtuoso

    # ── the migrated read ──────────────────────────────────────────

    def get_company_contracts(
        self, gmr_id: str, years: int = 5, limit: int = 50,
        lang: str | None = None,
    ) -> dict:
        if self._virtuoso is None:
            return self._fallback.get_company_contracts(
                gmr_id, years=years, limit=limit, lang=lang,
            )
        try:
            rows = self._virtuoso.query(self._rows_query(gmr_id, limit))
            counts = self._virtuoso.query(self._count_query(gmr_id))
            totals = self._virtuoso.query(self._total_query(gmr_id))
        except SparqlTimeout:
            # A slow store is not a reason to show a blank page when the
            # other one can still answer.
            logger.warning(
                "virtuoso contract read timed out for %s; falling back", gmr_id,
            )
            return self._fallback.get_company_contracts(
                gmr_id, years=years, limit=limit, lang=lang,
            )

        contracts = [self._row(r) for r in rows]
        self._attach_authorities(contracts)
        identity = self._identity(gmr_id)
        # Exactly the keys GraphContractSource returns. The router reads
        # company_name / country / total_contract_value_eur straight off
        # this dict, so a renamed or missing key renders a nameless page
        # rather than raising — which is how it reached the e2e gate.
        return {
            "gmr_id": gmr_id,
            "company_name": identity.get("name"),
            "country": identity.get("country"),
            "total_contract_value_eur": _num(
                (totals[0] if totals else {}).get("total")
            ) or 0,
            "contract_count": _int((counts[0] if counts else {}).get("cnt")),
            "contracts": contracts,
        }

    def _identity(self, gmr_id: str) -> dict[str, Any]:
        """The company's name and country.

        Prefer the record the visitor actually asked for. Fall back to
        its sameAs closure only when that record carries no name — which
        happens, because historical sink bugs stripped subjects down to
        a bare owl:sameAs. Verified on prod: company fb2107f4 has ONLY
        the sameAs triple, while its approved twin 984840bd holds
        "Salus International Sp. z o.o." and POL.

        The fallback is not a bait-and-switch: the closure is, by
        construction, the same entity. A nameless page would be worse
        and would tell the visitor nothing.
        """
        own = self._name_query(f"<{_ID}/Company/{gmr_id}>")
        if own.get("name"):
            return own
        via_closure = self._name_query("?me", closure=gmr_id)
        if not via_closure:
            return own
        # Merge rather than replace. The closure is consulted for the
        # name the visitor's own record lacks; it must not drop a
        # country that record does have.
        merged = dict(own)
        for key, value in via_closure.items():
            if value and not merged.get(key):
                merged[key] = value
        return merged

    def _name_query(self, subject: str, closure: str | None = None) -> dict[str, Any]:
        """Name and country for a subject, either of which may be absent.

        The name is OPTIONAL, not required. Making it mandatory meant a
        company with no rdfs:label matched nothing, so its country was
        thrown away with it and the page rendered entirely blank —
        aef601e8 has rdf:type Company and P17 "DEU" in Virtuoso (and
        country DEU in Neo4j) but the profile showed null for both.
        Nameless companies are not rare: they arrive from procurement
        notices that identify a winner by registration number alone.

        rdf:type is the anchor instead, so "exists but unnamed" still
        returns a row while a gmr_id that resolves to nothing still
        returns none — which the caller needs in order to 404 rather
        than serve an empty page.
        """
        binding = self._closure(closure) if closure else ""
        rows = self._virtuoso.query(f"""
SELECT ?name ?country WHERE {{
  {binding}
  GRAPH <{_G_COMPANY}> {{
    {subject} a <{_ONT}Company> .
    OPTIONAL {{ {subject} <{_LABEL}> ?name }}
    OPTIONAL {{ {subject} <{_P17}> ?country }}
  }}
}}
LIMIT 1
""")
        return rows[0] if rows else {}

    # ── everything else stays on the graph store ───────────────────

    def get_authority_contracts(
        self, authority_id: str, years: int = 5, limit: int = 50,
        lang: str | None = None,
    ) -> dict:
        """Contracts issued by an authority, across its sameAs closure.

        Authorities duplicate for the same reasons companies do — the
        same buyer appears under slightly different names across notices
        — and the consolidator has already linked 623 of them. Measured
        on shared, authority 0817e807 issues 1 contract from its own
        subject and 12 across its closure: a 12x difference on a page
        that is supposed to show what a public body spends.

        `years` is accepted for interface compatibility and unused, as
        on the company side: the rows query orders by award date and
        takes `limit`, and no caller passes a narrowed window.

        Falls back to the graph store when Virtuoso is not configured or
        the query times out.
        """
        if self._virtuoso is None:
            return self._fallback.get_authority_contracts(
                authority_id, years, limit, lang)
        try:
            identity = self._authority_identity(authority_id)
            if identity is None:
                return {"authority_id": authority_id, "contracts": [],
                        "contract_count": 0}
            rows = self._virtuoso.query(
                self._authority_rows_query(authority_id, limit))
            contracts = []
            for raw in rows:
                contract = self._row(raw)
                # _row carries the awarding authority for the company
                # view; on this page the authority IS the subject, and
                # the column the reader wants is who won.
                contract.pop("_auth_iri", None)
                contract["_contractor_iri"] = raw.get("awardee")
                contracts.append(contract)
            self._attach_contractors(contracts)
            totals = self._authority_totals(authority_id)
        except SparqlTimeout:
            logger.warning(
                "authority contracts for %s timed out in Virtuoso; "
                "falling back to the graph store", authority_id,
            )
            return self._fallback.get_authority_contracts(
                authority_id, years, limit, lang)
        return {
            "authority_id": authority_id,
            "authority_name": identity.get("name"),
            "country": identity.get("country"),
            "total_spend_eur": totals["total"],
            "contract_count": totals["count"],
            "contracts": contracts,
        }

    @staticmethod
    def _authority_closure(authority_id: str) -> str:
        return (
            f"GRAPH <{_G_AUTHORITY}> {{ <{_ID}/Authority/{authority_id}> "
            f"(<{OWL_SAME_AS}>|^<{OWL_SAME_AS}>)* ?me . }}"
        )

    def _authority_identity(self, authority_id: str) -> dict | None:
        """Name and country for the authority, or None if it resolves to
        nothing — the caller needs that to return an empty payload
        rather than one with null fields."""
        rows = self._virtuoso.query(f"""
SELECT ?name ?country WHERE {{
  GRAPH <{_G_AUTHORITY}> {{
    <{_ID}/Authority/{authority_id}> a <{_ONT}Authority> .
    OPTIONAL {{ <{_ID}/Authority/{authority_id}> <{_LABEL}> ?name }}
    OPTIONAL {{ <{_ID}/Authority/{authority_id}> <{_P17}> ?country }}
  }}
}}
LIMIT 1
""")
        return rows[0] if rows else None

    def _authority_rows_query(self, authority_id: str, limit: int) -> str:
        optionals = _OPTIONAL_SEP.join(
            f"OPTIONAL {{ ?n <{_ONT}{pred}> ?{key} }}"
            for key, pred in _CONTRACT_FIELDS
        )
        # DISTINCT for the same reason as the company rows query — see
        # the note there. Authority 0817e807 returned 20 rows of a
        # single contract without it.
        return f"""
SELECT DISTINCT ?n ?title ?awardee {" ".join("?" + k for k, _ in _CONTRACT_FIELDS)}
WHERE {{
  {self._authority_closure(authority_id)}
  GRAPH <{_G_CONTRACT}> {{
    ?n <{_ONT}awardedBy> ?me .
    OPTIONAL {{ ?n <{_LABEL}> ?title }}
    OPTIONAL {{ ?n <{_ONT}awardedTo> ?awardee }}
    {optionals}
    {self._CANONICAL}
  }}
}}
ORDER BY DESC(?award_date)
LIMIT {int(limit)}
"""

    def _authority_totals(self, authority_id: str) -> dict:
        crows = self._virtuoso.query(f"""
SELECT (COUNT(DISTINCT ?n) AS ?cnt) WHERE {{
  {self._authority_closure(authority_id)}
  GRAPH <{_G_CONTRACT}> {{ ?n <{_ONT}awardedBy> ?me . {self._CANONICAL} }}
}}
""")
        count = int(crows[0]["cnt"]) if crows and crows[0].get("cnt") else 0
        # Separate query for the same reason as the company side: an
        # aggregate over an OPTIONAL alongside a COUNT makes Virtuoso
        # evaluate the unbound branch as 0 for every row.
        srows = self._virtuoso.query(f"""
SELECT (SUM(?v) AS ?total) WHERE {{
  SELECT DISTINCT ?n ?v WHERE {{
    {self._authority_closure(authority_id)}
    GRAPH <{_G_CONTRACT}> {{
      ?n <{_ONT}awardedBy> ?me .
      ?n <{_ONT}value_eur> ?v .
      {self._CANONICAL}
    }}
  }}
}}
""")
        total = 0
        if srows and srows[0].get("total") not in (None, ""):
            total = float(srows[0]["total"])
        return {"count": count, "total": total}

    def _attach_contractors(self, contracts: list[dict]) -> None:
        """Resolve awardee IRIs to company names in ONE batched query.

        Mirror of _attach_authorities, and batched for the same reason:
        joining the company graph inside the rows query is costed by
        Virtuoso at thousands of seconds and refused.
        """
        iris = {c.get("_contractor_iri") for c in contracts}
        iris = {i for i in iris if i}
        names: dict[str, dict] = {}
        if iris:
            values = " ".join(f"<{i}>" for i in iris)
            # Same shape as _attach_authorities: one required pattern in
            # the GRAPH block with VALUES after it. A block of nothing
            # but OPTIONALs is a 500 from Virtuoso, not an empty result.
            rows = self._virtuoso.query(f"""
SELECT ?c ?name ?country WHERE {{
  GRAPH <{_G_COMPANY}> {{
    ?c <{_LABEL}> ?name .
    OPTIONAL {{ ?c <{_P17}> ?country }}
  }}
  VALUES ?c {{ {values} }}
}}
""")
            names = {r["c"]: r for r in rows if r.get("c")}
        for contract in contracts:
            iri = contract.pop("_contractor_iri", None)
            row = names.get(iri, {}) if iri else {}
            contract["contractor"] = row.get("name")
            contract["contractor_country"] = row.get("country")
            contract["contractor_gmr_id"] = (
                iri.rsplit("/", 1)[-1] if iri else None
            )

    def get_contract_detail(self, *a: Any, **k: Any) -> Any:
        return self._fallback.get_contract_detail(*a, **k)

    def get_sector_summary(self, *a: Any, **k: Any) -> Any:
        return self._fallback.get_sector_summary(*a, **k)

    #: Grant fields, in the response's own naming. Same shape the Neo4j
    #: source returns, so the router and the web app see no difference.
    _GRANT_FIELDS: tuple[tuple[str, str], ...] = (
        ("eu_contribution", "detail_eu_contribution"),
        ("total_budget", "detail_total_budget"),
        ("fund", "detail_fund"),
        ("programme", "detail_programme"),
        ("start_date", "detail_start_date"),
        ("end_date", "detail_end_date"),
        ("nuts", "detail_nuts_code"),
        ("year", "year"),
    )

    def get_company_cohesion_grants(self, gmr_id: str, limit: int = 50) -> dict:
        """EU cohesion grants, aggregated across the company's closure.

        The funding side had exactly the problem the contracts side had:
        a company whose duplicates hold the grants showed none of them.
        Measured on shared, Siemens b559559e returns 21 grants from its
        own subject and 22 across its 22-member closure.

        Falls back to the graph store when Virtuoso is not configured,
        so an environment without it behaves as before.
        """
        if self._virtuoso is None:
            return self._fallback.get_company_cohesion_grants(gmr_id, limit)
        try:
            identity = self._identity(gmr_id)
            grants = self._grant_rows(gmr_id, limit)
            totals = self._grant_totals(gmr_id)
        except SparqlTimeout:
            logger.warning(
                "cohesion grants for %s timed out in Virtuoso; "
                "falling back to the graph store", gmr_id,
            )
            return self._fallback.get_company_cohesion_grants(gmr_id, limit)
        return {
            "gmr_id": gmr_id,
            "name": identity.get("name"),
            "country": identity.get("country"),
            "grants": grants,
            "grant_count": totals["count"],
            "total_eu_contribution": totals["total"],
        }

    def _grant_rows(self, gmr_id: str, limit: int) -> list[dict]:
        optionals = _OPTIONAL_SEP.join(
            f"OPTIONAL {{ ?d <{_ONT}{pred}> ?{key} }}"
            for key, pred in self._GRANT_FIELDS
        )
        rows = self._virtuoso.query(f"""
SELECT DISTINCT ?d ?title {" ".join("?" + k for k, _ in self._GRANT_FIELDS)}
WHERE {{
  {self._closure(gmr_id)}
  GRAPH <{_G_COHESION}> {{
    ?d <{_ONT}filedBy> ?me .
    ?d <{_ONT}disclosureSystem> "eu-cohesion" .
    OPTIONAL {{ ?d <{_LABEL}> ?title }}
    {optionals}
  }}
}}
ORDER BY DESC(?start_date) DESC(?year)
LIMIT {int(limit)}
""")
        return [
            {
                "title": r.get("title"),
                **{key: r.get(key) for key, _ in self._GRANT_FIELDS},
            }
            for r in rows
        ]

    def _grant_totals(self, gmr_id: str) -> dict:
        """Count and EU-contribution sum over the closure.

        COUNT(DISTINCT ?d) rather than COUNT(*): a disclosure filed by
        two companies that later turn out to be the same entity would
        otherwise be counted once per closure member.
        """
        rows = self._virtuoso.query(f"""
SELECT (COUNT(DISTINCT ?d) AS ?cnt) WHERE {{
  {self._closure(gmr_id)}
  GRAPH <{_G_COHESION}> {{
    ?d <{_ONT}filedBy> ?me .
    ?d <{_ONT}disclosureSystem> "eu-cohesion" .
  }}
}}
""")
        count = int(rows[0]["cnt"]) if rows and rows[0].get("cnt") else 0
        # Summed in a separate query for the same reason _total_query is
        # separate on the contracts side: mixing an aggregate over an
        # OPTIONAL with a COUNT in one SELECT makes Virtuoso evaluate the
        # unbound branch as 0 for every row.
        srows = self._virtuoso.query(f"""
SELECT (SUM(?v) AS ?total) WHERE {{
  SELECT DISTINCT ?d ?v WHERE {{
    {self._closure(gmr_id)}
    GRAPH <{_G_COHESION}> {{
      ?d <{_ONT}filedBy> ?me .
      ?d <{_ONT}disclosureSystem> "eu-cohesion" .
      ?d <{_ONT}detail_eu_contribution> ?v .
    }}
  }}
}}
""")
        total = 0
        if srows and srows[0].get("total") not in (None, ""):
            total = float(srows[0]["total"])
        return {"count": count, "total": total}

    def get_single_bidder_stats(self, *a: Any, **k: Any) -> Any:
        return self._fallback.get_single_bidder_stats(*a, **k)

    def get_single_bidder_by_country(self, *a: Any, **k: Any) -> Any:
        return self._fallback.get_single_bidder_by_country(*a, **k)

    def get_stored_publication_number(self, *a: Any, **k: Any) -> Any:
        return self._fallback.get_stored_publication_number(*a, **k)

    # ── queries ────────────────────────────────────────────────────

    @staticmethod
    def _closure(gmr_id: str) -> str:
        """Bind ?me to every company in this one's sameAs closure.

        Zero-or-more so a company with no duplicates still matches
        itself; the inverse leg because which side the consolidator
        recorded as source is arbitrary.
        """
        return (
            f"GRAPH <{_G_COMPANY}> {{ <{_ID}/Company/{gmr_id}> "
            f"(<{OWL_SAME_AS}>|^<{OWL_SAME_AS}>)* ?me . }}"
        )

    @staticmethod
    def _awarded() -> str:
        """A company is on a notice either as the resolved awardee or as
        a winner in parties[]; both mean it won the contract."""
        return (
            f"{{ ?n <{_ONT}awardedTo> ?me }} UNION "
            f"{{ ?n <{_ONT}winner> ?me }}"
        )

    def _rows_query(self, gmr_id: str, limit: int) -> str:
        """The listed contracts — the same population the count counts.

        _CANONICAL belongs here as much as in _count_query. Without it
        the rows included modification restatements that the count and
        total deliberately exclude, so a company whose contracts are all
        amendments rendered a table of rows above the words "0
        contracts" — Siemens AG c0b601df returned contract_count 0,
        total 0, and five can-modif rows. A row list that disagrees with
        its own total is worse than either number alone, because there
        is no way for a reader to tell which one is lying.
        """
        optionals = _OPTIONAL_SEP.join(
            f"OPTIONAL {{ ?n <{_ONT}{pred}> ?{key} }}"
            for key, pred in _CONTRACT_FIELDS
        )
        # DISTINCT is load-bearing, not tidiness. The closure is a
        # property path, and Virtuoso yields ?me once per PATH, not once
        # per member — a 22-member closure with several sameAs routes
        # between its records returns the same contract many times over.
        # Measured on shared before this: company c0b601df asked for 20
        # rows and got 20, of which 2 were distinct contracts. The page
        # rendered the same two awards ten times each while the count
        # beside it (COUNT(DISTINCT ?n)) correctly said 29.
        return f"""
SELECT DISTINCT ?n ?title ?auth {" ".join("?" + k for k, _ in _CONTRACT_FIELDS)}
WHERE {{
  {self._closure(gmr_id)}
  GRAPH <{_G_CONTRACT}> {{
    {self._awarded()}
    OPTIONAL {{ ?n <{_LABEL}> ?title }}
    OPTIONAL {{ ?n <{_ONT}awardedBy> ?auth }}
    {optionals}
    {self._CANONICAL}
  }}
}}
ORDER BY DESC(?award_date)
LIMIT {int(limit)}
"""

    _CANONICAL = (
        f'OPTIONAL {{ ?n <{_ONT}isCurrent> ?is_current }} '
        f'OPTIONAL {{ ?n <{_ONT}noticeType> ?nt }} '
        # is_current when the collapse pass has spoken, else "not a
        # modification restatement" — so a contract amended three times
        # counts once, not four times.
        #
        # Written as an explicit disjunction because Virtuoso rejects
        # COALESCE returning a boolean inside FILTER with
        # `ssg_print_bop_bool_expn(): unsupported mode`.
        'FILTER( (BOUND(?is_current) && ?is_current) || '
        '(!BOUND(?is_current) && (!BOUND(?nt) || ?nt != "can-modif")) )'
    )

    def _count_query(self, gmr_id: str) -> str:
        return f"""
SELECT (COUNT(DISTINCT ?n) AS ?cnt)
WHERE {{
  {self._closure(gmr_id)}
  GRAPH <{_G_CONTRACT}> {{
    {self._awarded()}
    {self._CANONICAL}
  }}
}}
"""

    def _total_query(self, gmr_id: str) -> str:
        """Trusted value: canonical rows only, low-confidence excluded.

        Two queries rather than one because Virtuoso's
        `IF(BOUND(?x), 0, ...)` silently evaluates to 0 for every row —
        verified on prod: the same aggregate returns 367,721,491.42 with
        a plain COALESCE and 0 with the IF wrapped around it. Excluding
        the flagged rows with a FILTER gives the right number, but it
        also drops them from any COUNT in the same query, and the Cypher
        this replaces counts them while contributing 0 to the value. So
        the count gets its own query.
        """
        return f"""
SELECT (SUM(?v) AS ?total)
WHERE {{
  {self._closure(gmr_id)}
  GRAPH <{_G_CONTRACT}> {{
    {self._awarded()}
    {self._CANONICAL}
    OPTIONAL {{ ?n <{_ONT}valueLowConfidence> ?low }}
    FILTER( !BOUND(?low) )
    OPTIONAL {{ ?n <{_ONT}currentValue> ?cv }}
    OPTIONAL {{ ?n <{_ONT}valueEur> ?ve }}
    BIND( COALESCE(?cv, ?ve, 0) AS ?v )
  }}
}}
"""

    def _attach_authorities(self, contracts: list[dict]) -> None:
        """Resolve authority IRIs to names in ONE batched query.

        Joining the authority graph inside the rows query costs Virtuoso
        ~10,000s by its own estimate and is refused; this runs in ~0.016s.
        """
        iris = {c["_auth_iri"] for c in contracts if c.get("_auth_iri")}
        if not iris:
            for c in contracts:
                c.pop("_auth_iri", None)
                c["authority"] = None
                c["authority_id"] = None
                c["authority_country"] = None
            return
        values = " ".join(f"<{i}>" for i in iris)
        rows = self._virtuoso.query(f"""
SELECT ?a ?label ?country WHERE {{
  GRAPH <{_G_AUTHORITY}> {{
    ?a <{_LABEL}> ?label .
    OPTIONAL {{ ?a <{_P17}> ?country }}
  }}
  VALUES ?a {{ {values} }}
}}
""")
        by_iri = {r["a"]: r for r in rows}
        for c in contracts:
            iri = c.pop("_auth_iri", None)
            meta = by_iri.get(iri) if iri else None
            c["authority"] = (meta or {}).get("label")
            c["authority_country"] = (meta or {}).get("country")
            c["authority_id"] = iri.rsplit("/", 1)[-1] if iri else None

    @staticmethod
    def _row(r: dict) -> dict:
        out: dict[str, Any] = {}
        for key, _pred in _CONTRACT_FIELDS:
            out[key] = r.get(key)
        out["ted_notice_id"] = out.pop("notice_id")
        out["ted_publication_number"] = out.pop("publication_number")
        out["title"] = r.get("title")
        # The API has always returned ted_url; it is null on every Neo4j
        # node and absent from the event schema, so it stays null rather
        # than disappearing from the wire shape.
        out["ted_url"] = None
        auth = r.get("auth")
        out["_auth_iri"] = auth
        return out


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0
