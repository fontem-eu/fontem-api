"""Declarative data-quality assertion catalog.

Each :class:`Assertion` is one invariant we expect the served graph
(Neo4j) or the events store (Postgres) to satisfy. An assertion's
``query`` returns exactly one row; ``evaluate`` maps that row to
``(ok, observed)``.

Two severities, matching the agreed gate model:
  * ``block`` — keys / referential integrity / value sanity. A failure
    here means corrupt data; the runner exits non-zero (fails the Job).
  * ``warn``  — pipeline lag / freshness. A failure is an ops signal,
    not corruption; the runner reports it but still exits zero.

The catalog is plain Python (not YAML) so each ``evaluate`` is a real,
unit-testable callable. Every query is grounded in the live schema:
see ``apoc.meta.nodeTypeProperties`` for the property set per label.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

BLOCK = "block"
WARN = "warn"

# Families. The first three block; the last two warn.
KEYS = "keys"            # primary-key presence + uniqueness
REFS = "refs"            # referential integrity (relationships)
VALUES = "values"        # value sanity / domain ranges
PIPELINE = "pipeline"    # sink lag + dead-letter (events store)
FRESHNESS = "freshness"  # source recency (events store)
GOLDEN = "golden"        # known-true ground-truth facts (graph)
COVERAGE = "coverage"    # field-population coverage (graph)

Evaluator = Callable[[Mapping[str, Any]], "tuple[bool, str]"]


@dataclass(frozen=True)
class Assertion:  # pylint: disable=too-many-instance-attributes
    id: str
    family: str
    title: str
    severity: str
    engine: str           # "cypher" | "sql"
    query: str
    evaluate: Evaluator
    rationale: str = ""


# --------------------------------------------------------------------------
# Evaluator factories — keep the catalog terse and the logic testable.
# Every query aliases its offending-row count to `violations` unless noted.
# --------------------------------------------------------------------------
def zero_violations(label: str = "violations", total_key: str | None = None) -> Evaluator:
    """OK when row['violations'] == 0."""
    def _ev(row: Mapping[str, Any]) -> tuple[bool, str]:
        v = int(row.get("violations") or 0)
        obs = f"{v} {label}"
        if total_key and row.get(total_key) is not None:
            obs += f" of {row[total_key]}"
        return v == 0, obs
    return _ev


def le_threshold(key: str, threshold: int, label: str) -> Evaluator:
    """OK when row[key] <= threshold (used for lag / dead-letter counts)."""
    def _ev(row: Mapping[str, Any]) -> tuple[bool, str]:
        v = int(row.get(key) or 0)
        return v <= threshold, f"{label}={v} (limit {threshold})"
    return _ev


def at_least(key: str, minimum: int, label: str) -> Evaluator:
    """OK when row[key] >= minimum (presence of a known-true fact)."""
    def _ev(row: Mapping[str, Any]) -> tuple[bool, str]:
        v = int(row.get(key) or 0)
        return v >= minimum, f"{label}={v} (expected >= {minimum})"
    return _ev


def min_coverage(min_ratio: float, label: str) -> Evaluator:
    """OK when row['covered']/row['total'] >= min_ratio. Empty population
    (total 0) passes — nothing to cover yet."""
    def _ev(row: Mapping[str, Any]) -> tuple[bool, str]:
        total = int(row.get("total") or 0)
        covered = int(row.get("covered") or 0)
        if total == 0:
            return True, f"{label}: no rows yet"
        ratio = covered / total
        return ratio >= min_ratio, f"{label}: {covered}/{total} ({ratio:.0%}, min {min_ratio:.0%})"
    return _ev


def zero_with_detail(label: str = "stale") -> Evaluator:
    """OK when row['violations'] == 0; surfaces row['detail'] when not."""
    def _ev(row: Mapping[str, Any]) -> tuple[bool, str]:
        v = int(row.get("violations") or 0)
        detail = row.get("detail") or ""
        obs = f"{v} {label}" + (f": {detail}" if v and detail else "")
        return v == 0, obs
    return _ev


# ISO-4217 codes we expect in TED contract values (EU + common global).
_ISO_4217 = [
    # Active ISO-4217 codes. TED contracts in third countries legitimately
    # carry non-EU currencies (e.g. XOF for West-African co-operation, AMD),
    # so this is the full active set rather than an EU-only allowlist.
    "AED", "AFN", "ALL", "AMD", "ANG", "AOA", "ARS", "AUD", "AWG", "AZN",
    "BAM", "BBD", "BDT", "BGN", "BHD", "BIF", "BMD", "BND", "BOB", "BRL",
    "BSD", "BTN", "BWP", "BYN", "BZD", "CAD", "CDF", "CHF", "CLP", "CNY",
    "COP", "CRC", "CUP", "CVE", "CZK", "DJF", "DKK", "DOP", "DZD", "EGP",
    "ERN", "ETB", "EUR", "FJD", "FKP", "GBP", "GEL", "GHS", "GIP", "GMD",
    "GNF", "GTQ", "GYD", "HKD", "HNL", "HRK", "HTG", "HUF", "IDR", "ILS",
    "INR", "IQD", "IRR", "ISK", "JMD", "JOD", "JPY", "KES", "KGS", "KHR",
    "KMF", "KPW", "KRW", "KWD", "KYD", "KZT", "LAK", "LBP", "LKR", "LRD",
    "LSL", "LYD", "MAD", "MDL", "MGA", "MKD", "MMK", "MNT", "MOP", "MRU",
    "MUR", "MVR", "MWK", "MXN", "MYR", "MZN", "NAD", "NGN", "NIO", "NOK",
    "NPR", "NZD", "OMR", "PAB", "PEN", "PGK", "PHP", "PKR", "PLN", "PYG",
    "QAR", "RON", "RSD", "RUB", "RWF", "SAR", "SBD", "SCR", "SDG", "SEK",
    "SGD", "SHP", "SLE", "SOS", "SRD", "SSP", "STN", "SVC", "SYP", "SZL",
    "THB", "TJS", "TMT", "TND", "TOP", "TRY", "TTD", "TWD", "TZS", "UAH",
    "UGX", "USD", "UYU", "UZS", "VED", "VES", "VND", "VUV", "WST", "XAF",
    "XCD", "XOF", "XPF", "YER", "ZAR", "ZMW", "ZWL",
]
_ISO_LIST_CYPHER = "[" + ", ".join(f"'{c}'" for c in _ISO_4217) + "]"

# Expected max age (hours) between successful runs, per ETL cronjob.
# Tunable — these are the cadences a healthy staging/prod should hold.
SOURCE_CADENCE_HOURS: dict[str, int] = {
    "etl-gleif": 240,                 # weekly full dump + slack
    "etl-gleif-relationships": 336,
    "etl-ted-contracts": 48,          # near-daily procurement feed
    "etl-eu-lobbying": 168,
    "etl-us-financials": 168,
    "etl-eu-listings": 168,
    "etl-firds": 168,
    "etl-openfigi": 168,
    "etl-us-companies": 720,
    "etl-eu-knowledge-graph": 720,
    "etl-eu-sanctions": 168,
    "etl-cdp": 2160,
    "etl-nuts": 8760,                 # NUTS revisions are ~3-yearly
}


def _cadence_freshness_query() -> str:
    rows = ",\n        ".join(
        f"('{job}', {hrs})" for job, hrs in SOURCE_CADENCE_HOURS.items()
    )
    return f"""
    WITH cadence(cronjob, max_hours) AS (VALUES
        {rows}
    ),
    last AS (
        SELECT cronjob_name,
               max(started_at) FILTER (WHERE status = 'success') AS last_ok
        FROM events.etl_run
        GROUP BY cronjob_name
    )
    SELECT count(*) AS violations,
           coalesce(string_agg(
               c.cronjob || ' (' ||
               coalesce(round(extract(epoch FROM (now() - l.last_ok)) / 3600)::text,
                        'never') || 'h/' || c.max_hours || 'h)',
               ', ' ORDER BY c.cronjob), '') AS detail
    FROM cadence c
    LEFT JOIN last l ON l.cronjob_name = c.cronjob
    WHERE l.last_ok IS NULL
       OR l.last_ok < now() - (c.max_hours || ' hours')::interval
    """


# --------------------------------------------------------------------------
# The catalog.
# --------------------------------------------------------------------------
ASSERTIONS: list[Assertion] = [
    # ---- Family A: primary keys (BLOCK) -----------------------------------
    Assertion(
        "keys.company_gmr_id_present", KEYS,
        "Every Company has a gmr_id", BLOCK, "cypher",
        "MATCH (c:Company) RETURN count(*) AS total, count(*) - count(c.gmr_id) AS violations",
        zero_violations("companies missing gmr_id", "total"),
        "gmr_id is the canonical join key; a null one orphans the node.",
    ),
    Assertion(
        "keys.company_gmr_id_unique", KEYS,
        "Company.gmr_id is unique", BLOCK, "cypher",
        "MATCH (c:Company) WHERE c.gmr_id IS NOT NULL "
        "WITH c.gmr_id AS k, count(*) AS n WHERE n > 1 RETURN count(*) AS violations",
        zero_violations("duplicate gmr_ids"),
        "Two nodes sharing a gmr_id means the UUID5 derivation collided or a merge missed.",
    ),
    Assertion(
        "keys.contract_id_present", KEYS,
        "Every Contract has a ted_notice_id", BLOCK, "cypher",
        "MATCH (c:Contract) "
        "RETURN count(*) AS total, count(*) - count(c.ted_notice_id) AS violations",
        zero_violations("contracts missing ted_notice_id", "total"),
    ),
    Assertion(
        "keys.contract_id_unique", KEYS,
        "Contract.ted_notice_id is unique", BLOCK, "cypher",
        "MATCH (c:Contract) WHERE c.ted_notice_id IS NOT NULL "
        "WITH c.ted_notice_id AS k, count(*) AS n WHERE n > 1 RETURN count(*) AS violations",
        zero_violations("duplicate ted_notice_ids"),
    ),
    Assertion(
        "keys.disclosure_id_unique", KEYS,
        "Disclosure (disclosure_id, system) is unique", BLOCK, "cypher",
        "MATCH (d:Disclosure) WHERE d.disclosure_id IS NOT NULL "
        "WITH d.disclosure_id AS id, d.system AS s, count(*) AS n "
        "WHERE n > 1 RETURN count(*) AS violations",
        zero_violations("duplicate disclosure composite keys"),
    ),
    Assertion(
        "keys.authority_id_present", KEYS,
        "Every Authority has an authority_id", BLOCK, "cypher",
        "MATCH (a:Authority) "
        "RETURN count(*) AS total, count(*) - count(a.authority_id) AS violations",
        zero_violations("authorities missing authority_id", "total"),
    ),
    Assertion(
        "keys.financialyear_unique", KEYS,
        "FinancialYear (gmr_id, year, source) is unique", BLOCK, "cypher",
        "MATCH (f:FinancialYear) WITH f.gmr_id AS g, f.year AS y, f.source AS s, "
        "count(*) AS n WHERE n > 1 RETURN count(*) AS violations",
        zero_violations("duplicate financial-year keys"),
    ),
    Assertion(
        "keys.sanctioned_id_present", KEYS,
        "Every SanctionedEntity has an entity_id", BLOCK, "cypher",
        "MATCH (s:SanctionedEntity) "
        "RETURN count(*) AS total, count(*) - count(s.entity_id) AS violations",
        zero_violations("sanctioned entities missing entity_id", "total"),
    ),
    Assertion(
        "keys.lobbyist_label_present", KEYS,
        "Every eu-lobbying Disclosure carries the :Lobbyist label", BLOCK, "cypher",
        "MATCH (d:Disclosure {system:'eu-lobbying'}) "
        "RETURN count(*) AS total, "
        "count(*) - count(CASE WHEN 'Lobbyist' IN labels(d) THEN 1 END) AS violations",
        zero_violations("lobbying disclosures missing :Lobbyist", "total"),
        "Regression guard for the sink label-promotion fix (backlog #23).",
    ),

    # ---- Family B: referential integrity (BLOCK) --------------------------
    Assertion(
        "refs.contract_has_authority", REFS,
        "Every Contract is AWARDED by an Authority", BLOCK, "cypher",
        "MATCH (c:Contract) WHERE NOT (c)<-[:AWARDED]-(:Authority) RETURN count(*) AS violations",
        zero_violations("contracts with no awarding authority"),
    ),
    Assertion(
        "refs.contract_has_company", REFS,
        "Every Contract is AWARDED_TO a Company", BLOCK, "cypher",
        "MATCH (c:Contract) WHERE NOT (c)-[:AWARDED_TO]->(:Company) RETURN count(*) AS violations",
        zero_violations("contracts with no awarded company"),
    ),
    Assertion(
        "refs.financialyear_has_company", REFS,
        "Every FinancialYear is REPORTED by a Company", BLOCK, "cypher",
        "MATCH (f:FinancialYear) WHERE NOT EXISTS { (:Company)-[:REPORTED]->(f) } "
        "RETURN count(*) AS violations",
        zero_violations("financial years with no reporting company"),
    ),
    Assertion(
        "refs.subsidiary_no_selfloop", REFS,
        "SUBSIDIARY_OF has no self-loops", BLOCK, "cypher",
        "MATCH (a)-[:SUBSIDIARY_OF]->(a) RETURN count(*) AS violations",
        zero_violations("self-referential SUBSIDIARY_OF edges"),
    ),
    Assertion(
        "refs.sameas_no_selfloop", REFS,
        "SAME_AS has no self-loops", BLOCK, "cypher",
        "MATCH (a)-[:SAME_AS]->(a) RETURN count(*) AS violations",
        zero_violations("self-referential SAME_AS edges"),
    ),
    Assertion(
        "refs.sameas_confidence_range", REFS,
        "SAME_AS.confidence is within [0,1]", BLOCK, "cypher",
        "MATCH ()-[r:SAME_AS]->() WHERE r.confidence IS NOT NULL "
        "AND (r.confidence < 0 OR r.confidence > 1) RETURN count(*) AS violations",
        zero_violations("out-of-range SAME_AS confidences"),
    ),
    Assertion(
        "refs.lobbying_filedby_when_matched", REFS,
        "Lobbying disclosure with company_gmr_id has a FILED_BY edge", BLOCK, "cypher",
        "MATCH (d:Disclosure {system:'eu-lobbying'}) WHERE d.company_gmr_id IS NOT NULL "
        "AND NOT (d)-[:FILED_BY]->(:Company) RETURN count(*) AS violations",
        zero_violations("matched disclosures with dropped FILED_BY"),
        "Silent relationship-drop guard (backlog #7): a set "
        "company_gmr_id must materialise an edge.",
    ),
    Assertion(
        "refs.disclosure_company_resolves", REFS,
        "Disclosure.company_gmr_id resolves to a real Company", BLOCK, "cypher",
        "MATCH (d:Disclosure) WHERE d.company_gmr_id IS NOT NULL "
        "AND NOT EXISTS { (c:Company {gmr_id: d.company_gmr_id}) } RETURN count(*) AS violations",
        zero_violations("dangling company_gmr_id references"),
    ),

    # ---- Family C: value sanity (BLOCK, except the accounting identity) ---
    Assertion(
        "values.contract_value_nonneg", VALUES,
        "Contract.value_eur is never negative", BLOCK, "cypher",
        "MATCH (c:Contract) WHERE c.value_eur < 0 "
        "AND coalesce(c.value_quality_flag, '') <> 'concession_negative' "
        "RETURN count(*) AS violations",
        zero_violations("negative contract values"),
    ),
    Assertion(
        "values.contract_implausible_guard", VALUES,
        "Contracts above €50B are flagged value_low_confidence", BLOCK, "cypher",
        "MATCH (c:Contract) WHERE c.value_eur > 50000000000 "
        "AND coalesce(c.value_low_confidence, false) = false RETURN count(*) AS violations",
        zero_violations("implausibly large contracts not low-confidence-flagged"),
        "The €7M→€7B aircraft class of bug: an outlier magnitude must carry a confidence flag.",
    ),
    Assertion(
        "values.confidence_range", VALUES,
        "value_confidence is within [0,1]", BLOCK, "cypher",
        "MATCH (c:Contract) WHERE c.value_confidence IS NOT NULL "
        "AND (c.value_confidence < 0 OR c.value_confidence > 1) RETURN count(*) AS violations",
        zero_violations("out-of-range value_confidence"),
    ),
    Assertion(
        "values.confidence_formula", VALUES,
        "value_confidence = consistency × plausibility", BLOCK, "cypher",
        "MATCH (c:Contract) WHERE c.value_confidence IS NOT NULL "
        "AND c.value_confidence_consistency IS NOT NULL "
        "AND c.value_confidence_plausibility IS NOT NULL "
        "AND abs(c.value_confidence - "
        "c.value_confidence_consistency * c.value_confidence_plausibility) > 0.011 "
        "AND coalesce(c.value_quality_flag, '') <> 'concession_negative' "
        "RETURN count(*) AS violations",
        zero_violations("contracts whose confidence breaks its own formula"),
    ),
    Assertion(
        "values.currency_iso", VALUES,
        "Contract.value_currency is a known ISO-4217 code", BLOCK, "cypher",
        f"MATCH (c:Contract) WHERE c.value_currency IS NOT NULL "
        f"AND NOT c.value_currency IN {_ISO_LIST_CYPHER} RETURN count(*) AS violations",
        zero_violations("contracts with an unrecognised currency"),
    ),
    Assertion(
        "values.contract_pubdate_not_future", VALUES,
        "Contract.publication_date is not in the future", BLOCK, "cypher",
        "MATCH (c:Contract) WHERE c.publication_date IS NOT NULL "
        "AND c.publication_date > toString(date()) RETURN count(*) AS violations",
        zero_violations("future-dated contracts"),
    ),
    Assertion(
        "values.financialyear_year_range", VALUES,
        "FinancialYear.year is within [1990, current+1]", BLOCK, "cypher",
        "MATCH (f:FinancialYear) WHERE f.year IS NOT NULL "
        "AND (f.year < 1990 OR f.year > date().year + 1) RETURN count(*) AS violations",
        zero_violations("financial years out of plausible range"),
    ),
    Assertion(
        "values.lobby_cost_band", VALUES,
        "Lobbying detail_cost_max >= detail_cost_min", BLOCK, "cypher",
        "MATCH (d:Disclosure) WHERE d.detail_cost_min IS NOT NULL "
        "AND d.detail_cost_max IS NOT NULL "
        "AND d.detail_cost_max < d.detail_cost_min RETURN count(*) AS violations",
        zero_violations("inverted lobby spend bands"),
    ),
    Assertion(
        "values.nuts_level", VALUES,
        "NUTSRegion.level is in {0,1,2,3}", BLOCK, "cypher",
        "MATCH (n:NUTSRegion) WHERE NOT n.level IN [0, 1, 2, 3] RETURN count(*) AS violations",
        zero_violations("NUTS regions with an invalid level"),
    ),
    Assertion(
        "values.accounting_identity", VALUES,
        "FinancialYear assets ≈ liabilities + equity (±2%)", WARN, "cypher",
        "MATCH (f:FinancialYear) WHERE f.total_assets IS NOT NULL "
        "AND f.total_liabilities IS NOT NULL AND f.equity IS NOT NULL AND f.total_assets > 0 "
        "AND abs(f.total_assets - (f.total_liabilities + f.equity)) / f.total_assets > 0.02 "
        "RETURN count(*) AS violations",
        zero_violations("financial years that break the balance-sheet identity"),
        "Filings legitimately restate; warn (investigative signal) rather than block.",
    ),
    Assertion(
        "values.deregistered_lobbyist_name_redacted", VALUES,
        "Deregistered lobbyists carry no name (GDPR redaction)", BLOCK, "cypher",
        "MATCH (d:Disclosure {system:'eu-lobbying'}) WHERE d.detail_active = false "
        "AND ((d.detail_name IS NOT NULL AND d.detail_name <> '[deregistered]') "
        "OR (d.title IS NOT NULL AND d.title <> '[deregistered]')) "
        "RETURN count(*) AS violations",
        zero_violations("deregistered lobbyists still carrying a name"),
        "Privacy guard: once a registrant drops off the upstream lawful basis we "
        "keep trends, not identities. A real name on a tombstoned record is a "
        "GDPR leak — the dereg path must redact name + title.",
    ),
    Assertion(
        "values.active_lobbyist_has_name", VALUES,
        "Active lobbyists have a real (non-redacted) name", BLOCK, "cypher",
        "MATCH (d:Disclosure {system:'eu-lobbying'}) "
        "WHERE coalesce(d.detail_active, true) = true "
        "AND (d.detail_name IS NULL OR d.detail_name = '[deregistered]') "
        "RETURN count(*) AS violations",
        zero_violations("active lobbyists missing a name"),
        "Coverage: a currently-registered lobbyist must be identifiable. A missing "
        "or already-redacted name on an active record means dropped or "
        "wrongly-tombstoned data.",
    ),

    # ---- Family D: pipeline integrity (WARN, events store) ----------------
    Assertion(
        "pipeline.neo4j_sink_lag", PIPELINE,
        "neo4j_sink is caught up to the events head", WARN, "sql",
        "SELECT (SELECT max(seq) FROM events.entity_events) - last_seq AS lag "
        "FROM events.consumer_offsets WHERE consumer_name = 'neo4j_sink'",
        le_threshold("lag", 1000, "lag"),
        "The served graph is the neo4j_sink projection; large lag means the graph is stale.",
    ),
    Assertion(
        "pipeline.virtuoso_sink_lag", PIPELINE,
        "virtuoso_sink is caught up to the events head", WARN, "sql",
        "SELECT (SELECT max(seq) FROM events.entity_events) - last_seq AS lag "
        "FROM events.consumer_offsets WHERE consumer_name = 'virtuoso_sink'",
        le_threshold("lag", 10000, "lag"),
    ),
    Assertion(
        "pipeline.neo4j_deadletter", PIPELINE,
        "neo4j_sink has no dead-lettered events", WARN, "sql",
        "SELECT count(*) AS violations FROM events.dead_letter WHERE consumer = 'neo4j_sink'",
        zero_violations("neo4j_sink dead-letters"),
    ),
    Assertion(
        "pipeline.deadletter_total", PIPELINE,
        "Total dead-letter backlog is small", WARN, "sql",
        "SELECT count(*) AS dl FROM events.dead_letter",
        le_threshold("dl", 100, "dead-letters"),
    ),
    Assertion(
        "pipeline.stuck_runs", PIPELINE,
        "No etl_run stuck 'running' beyond 6h", WARN, "sql",
        "SELECT count(*) AS violations FROM events.etl_run "
        "WHERE status = 'running' AND started_at < now() - interval '6 hours'",
        zero_violations("ETL runs stuck in 'running'"),
    ),

    # ---- Family E: freshness (WARN, events store) -------------------------
    Assertion(
        "freshness.sources_within_cadence", FRESHNESS,
        "Each source ran successfully within its cadence", WARN, "sql",
        _cadence_freshness_query(),
        zero_with_detail("stale sources"),
        "Makes 'great data quality in staging' enforceable: a source past its cadence is flagged.",
    ),

    # ---- TED tender-integrity (procurement-integrity initiative) ----------
    Assertion(
        "values.contract_bidder_count_positive", VALUES,
        "Awarded contracts have tenders_received >= 1", BLOCK, "cypher",
        "MATCH (c:Contract) WHERE c.tenders_received IS NOT NULL "
        "AND c.tenders_received < 1 RETURN count(*) AS violations",
        zero_violations("contracts with a non-positive bidder count"),
        "An awarded contract had at least one bidder; 0 is corrupt parsing.",
    ),
    Assertion(
        "coverage.contract_procedure_type_2026", COVERAGE,
        "2026 contracts carry procedure_type (>=80%)", WARN, "cypher",
        "MATCH (c:Contract) WHERE c.publication_date >= '2026-01-01' "
        "RETURN count(*) AS total, count(c.procedure_type) AS covered",
        min_coverage(0.80, "procedure_type"),
        "procedure_type is always in eForms; low coverage means the loader "
        "or re-ingest hasn't populated it yet.",
    ),
    Assertion(
        "coverage.contract_bidder_count_2026", COVERAGE,
        "2026 contracts carry tenders_received (>=40%)", WARN, "cypher",
        "MATCH (c:Contract) WHERE c.publication_date >= '2026-01-01' "
        "RETURN count(*) AS total, count(c.tenders_received) AS covered",
        min_coverage(0.40, "bidder count"),
        "Bidder count drives the single-bidder indicator; some notices omit "
        "the submission statistics, so the bar is lower than procedure_type.",
    ),

    # ---- Family G: golden facts (BLOCK, known-true ground truth) -----------
    # Relations/entities we KNOW are true and must exist. A missing one means
    # data loss (a silent drop), not just drift — so they block.
    Assertion(
        "golden.nuts_germany_exists", GOLDEN,
        "Germany (NUTS 'DE') is present", BLOCK, "cypher",
        "MATCH (n:NUTSRegion {code:'DE'}) RETURN count(*) AS found",
        at_least("found", 1, "DE regions"),
        "The NUTS classification is fixed; Germany must always resolve.",
    ),
    Assertion(
        "golden.nuts_germany_subdivided", GOLDEN,
        "Germany's NUTS-1 subdivisions are materialized (>=10)", BLOCK, "cypher",
        "MATCH (:NUTSRegion)-[:PART_OF]->(:NUTSRegion {code:'DE'}) "
        "RETURN count(*) AS found",
        at_least("found", 10, "DE children"),
        "Silent-drop canary for the NUTS hierarchy: Germany has 16 NUTS-1 regions.",
    ),
    Assertion(
        "golden.apple_company_exists", GOLDEN,
        "Apple Inc. (LEI HWUPKR0MPOU8FGXBT394) exists", BLOCK, "cypher",
        "MATCH (c:Company {lei:'HWUPKR0MPOU8FGXBT394'}) RETURN count(*) AS found",
        at_least("found", 1, "Apple nodes"),
        "A stable, well-known GLEIF entity must resolve to a Company.",
    ),
    Assertion(
        "golden.volkswagen_group_materialized", GOLDEN,
        "Volkswagen AG's subsidiary group is materialized (>=50)", BLOCK, "cypher",
        "MATCH (:Company)-[:SUBSIDIARY_OF]->(:Company {lei:'529900NNUPAGGOMPXZ31'}) "
        "RETURN count(*) AS found",
        at_least("found", 50, "VW subsidiaries"),
        "Silent-drop canary for GLEIF SUBSIDIARY_OF: VW AG had 207 subsidiaries.",
    ),
]


def by_id() -> dict[str, Assertion]:
    return {a.id: a for a in ASSERTIONS}


# Cheap import-time guard: assertion ids must be unique.
if len(by_id()) != len(ASSERTIONS):  # pragma: no cover - guard
    raise RuntimeError("duplicate assertion id in catalog")
