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
_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
_P17 = "http://www.wikidata.org/prop/direct/P17"

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
        return via_closure or own

    def _name_query(self, subject: str, closure: str | None = None) -> dict[str, Any]:
        binding = self._closure(closure) if closure else ""
        rows = self._virtuoso.query(f"""
SELECT ?name ?country WHERE {{
  {binding}
  GRAPH <{_G_COMPANY}> {{
    {subject} <{_LABEL}> ?name .
    OPTIONAL {{ {subject} <{_P17}> ?country }}
  }}
}}
LIMIT 1
""")
        return rows[0] if rows else {}

    # ── everything else stays on the graph store ───────────────────

    def get_authority_contracts(self, *a: Any, **k: Any) -> Any:
        return self._fallback.get_authority_contracts(*a, **k)

    def get_contract_detail(self, *a: Any, **k: Any) -> Any:
        return self._fallback.get_contract_detail(*a, **k)

    def get_sector_summary(self, *a: Any, **k: Any) -> Any:
        return self._fallback.get_sector_summary(*a, **k)

    def get_company_cohesion_grants(self, *a: Any, **k: Any) -> Any:
        return self._fallback.get_company_cohesion_grants(*a, **k)

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
        optionals = "\n    ".join(
            f"OPTIONAL {{ ?n <{_ONT}{pred}> ?{key} }}"
            for key, pred in _CONTRACT_FIELDS
        )
        return f"""
SELECT ?n ?title ?auth {" ".join("?" + k for k, _ in _CONTRACT_FIELDS)}
WHERE {{
  {self._closure(gmr_id)}
  GRAPH <{_G_CONTRACT}> {{
    {self._awarded()}
    OPTIONAL {{ ?n <{_LABEL}> ?title }}
    OPTIONAL {{ ?n <{_ONT}awardedBy> ?auth }}
    {optionals}
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
