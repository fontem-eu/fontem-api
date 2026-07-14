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
ORACLE = "oracle"        # computed indicators validated vs published external figures
CONSISTENCY = "consistency"  # Neo4j <-> Virtuoso cross-store agreement (sampled)

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


def oracle_band(low: float, high: float, min_sample: int,
                label: str) -> Evaluator:
    """OK when an independently-computed rate lands inside the band an
    external authority publishes for the same indicator. Passes (with a
    note) when the sample is too thin to judge — we validate the
    computation, we don't manufacture a verdict from a handful of rows."""
    def _ev(row: Mapping[str, Any]) -> tuple[bool, str]:
        sample = int(row.get("sample") or 0)
        if sample < min_sample:
            return True, (f"{label}: n={sample} < {min_sample} — too thin to "
                          f"validate against the published band [{low}, {high}]")
        rate = float(row.get("rate") or 0.0)
        ok = low <= rate <= high
        return ok, (f"{label}: rate={rate:.3f} vs published band "
                    f"[{low}, {high}] (n={sample})")
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
    "etl-cellar-mirror": 48,          # daily EUR-Lex delta (gitops#290)
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
        "ted_notice_id is the merge key; a contract without one can never be updated and "
        "duplicates on re-ingest.",
    ),
    Assertion(
        "keys.contract_id_unique", KEYS,
        "Contract.ted_notice_id is unique", BLOCK, "cypher",
        "MATCH (c:Contract) WHERE c.ted_notice_id IS NOT NULL "
        "WITH c.ted_notice_id AS k, count(*) AS n WHERE n > 1 RETURN count(*) AS violations",
        zero_violations("duplicate ted_notice_ids"),
        "Two nodes sharing a ted_notice_id would double-count the award in every aggregate.",
    ),
    Assertion(
        "keys.disclosure_id_unique", KEYS,
        "Disclosure (disclosure_id, system) is unique", BLOCK, "cypher",
        "MATCH (d:Disclosure) WHERE d.disclosure_id IS NOT NULL "
        "WITH d.disclosure_id AS id, d.system AS s, count(*) AS n "
        "WHERE n > 1 RETURN count(*) AS violations",
        zero_violations("duplicate disclosure composite keys"),
        "The (disclosure_id, system) pair is the idempotency key for every disclosure loader.",
    ),
    Assertion(
        "keys.authority_id_present", KEYS,
        "Every Authority has an authority_id", BLOCK, "cypher",
        "MATCH (a:Authority) "
        "RETURN count(*) AS total, count(*) - count(a.authority_id) AS violations",
        zero_violations("authorities missing authority_id", "total"),
        "authority_id is the merge key; an authority without one strands its AWARDED edges on "
        "re-ingest.",
    ),
    Assertion(
        "keys.financialyear_unique", KEYS,
        "FinancialYear (gmr_id, year, source) is unique", BLOCK, "cypher",
        "MATCH (f:FinancialYear) WITH f.gmr_id AS g, f.year AS y, f.source AS s, "
        "count(*) AS n WHERE n > 1 RETURN count(*) AS violations",
        zero_violations("duplicate financial-year keys"),
        "One row per (gmr_id, year, source); duplicates double financial aggregates silently.",
    ),
    Assertion(
        "keys.sanctioned_id_present", KEYS,
        "Every SanctionedEntity has an entity_id", BLOCK, "cypher",
        "MATCH (s:SanctionedEntity) "
        "RETURN count(*) AS total, count(*) - count(s.entity_id) AS violations",
        zero_violations("sanctioned entities missing entity_id", "total"),
        "entity_id is the merge key for sanctions re-ingests; without it re-runs duplicate "
        "entries.",
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
        "Every TED award names its contracting authority; a contract without one lost its buyer "
        "side in ingest.",
    ),
    Assertion(
        "refs.contract_has_company", REFS,
        "Every Contract is AWARDED_TO a Company or InvestmentFund",
        BLOCK, "cypher",
        "MATCH (c:Contract) WHERE NOT (c)-[:AWARDED_TO]->(:Company) "
        "AND NOT (c)-[:AWARDED_TO]->(:InvestmentFund) "
        "RETURN count(*) AS violations",
        zero_violations("contracts not awarded to a company or fund"),
        "The awardee may be relabeled :InvestmentFund once GLEIF confirms "
        "its category is FUND (an :InvestmentFund can win a contract), so "
        "both labels are valid targets. Guarding only :Company made the "
        "gate fail the moment a fund awardee was relabeled (#270).",
    ),
    Assertion(
        "refs.financialyear_has_company", REFS,
        "Every FinancialYear is REPORTED by a Company or InvestmentFund",
        BLOCK, "cypher",
        "MATCH (f:FinancialYear) WHERE NOT EXISTS { (:Company)-[:REPORTED]->(f) } "
        "AND NOT EXISTS { (:InvestmentFund)-[:REPORTED]->(f) } "
        "RETURN count(*) AS violations",
        zero_violations("financial years with no reporting entity"),
        "The reporter may be relabeled :InvestmentFund when GLEIF's "
        "category is FUND; both labels are valid (#270 alignment).",
    ),
    Assertion(
        "refs.subsidiary_no_selfloop", REFS,
        "SUBSIDIARY_OF has no self-loops", BLOCK, "cypher",
        "MATCH (a)-[:SUBSIDIARY_OF]->(a) RETURN count(*) AS violations",
        zero_violations("self-referential SUBSIDIARY_OF edges"),
        "GLEIF self-consolidation rows must never materialise as a company owning itself.",
    ),
    Assertion(
        "refs.sameas_no_selfloop", REFS,
        "SAME_AS has no self-loops", BLOCK, "cypher",
        "MATCH (a)-[:SAME_AS]->(a) RETURN count(*) AS violations",
        zero_violations("self-referential SAME_AS edges"),
        "A SAME_AS self-loop is a dedup-rule bug; it would let the merge engine collapse a node "
        "into itself.",
    ),
    Assertion(
        "refs.sameas_confidence_range", REFS,
        "SAME_AS.confidence is within [0,1]", BLOCK, "cypher",
        "MATCH ()-[r:SAME_AS]->() WHERE r.confidence IS NOT NULL "
        "AND (r.confidence < 0 OR r.confidence > 1) RETURN count(*) AS violations",
        zero_violations("out-of-range SAME_AS confidences"),
        "Consolidator confidences are probabilities; out-of-range values mean a broken rule, not "
        "a strong match.",
    ),
    Assertion(
        "refs.lobbying_filedby_when_matched", REFS,
        "Lobbying disclosure with company_gmr_id has a FILED_BY edge", BLOCK, "cypher",
        "MATCH (d:Disclosure {system:'eu-lobbying'}) WHERE d.company_gmr_id IS NOT NULL "
        "AND NOT (d)-[:FILED_BY]->(:Company) "
        "AND NOT (d)-[:FILED_BY]->(:InvestmentFund) "
        "RETURN count(*) AS violations",
        zero_violations("matched disclosures with dropped FILED_BY"),
        "Silent relationship-drop guard (backlog #7): a set "
        "company_gmr_id must materialise an edge.",
    ),
    Assertion(
        "refs.disclosure_company_resolves", REFS,
        "Disclosure.company_gmr_id resolves to a real Company", BLOCK, "cypher",
        "MATCH (d:Disclosure) WHERE d.company_gmr_id IS NOT NULL "
        "AND NOT EXISTS { (c:Company {gmr_id: d.company_gmr_id}) } "
        "AND NOT EXISTS { (f:InvestmentFund {gmr_id: d.company_gmr_id}) } "
        "RETURN count(*) AS violations",
        zero_violations("dangling company_gmr_id references"),
        "A company_gmr_id that resolves to no node is a dangling attribution — the UI would "
        "render a dead link.",
    ),

    # ---- Family C: value sanity (BLOCK, except the accounting identity) ---
    Assertion(
        "values.contract_value_nonneg", VALUES,
        "Contract.value_eur is never negative", BLOCK, "cypher",
        "MATCH (c:Contract) WHERE c.value_eur < 0 "
        "AND coalesce(c.value_quality_flag, '') <> 'concession_negative' "
        "RETURN count(*) AS violations",
        zero_violations("negative contract values"),
        "Negative award totals are ingest artifacts (sign errors, corrigenda mis-parses), never "
        "real prices.",
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
        "value_confidence is a probability; outside [0,1] the scorer misfired.",
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
        "confidence must equal consistency*plausibility; drift means the scorer and its stored "
        "parts disagree.",
    ),
    Assertion(
        "values.currency_iso", VALUES,
        "Contract.value_currency is a known ISO-4217 code", BLOCK, "cypher",
        f"MATCH (c:Contract) WHERE c.value_currency IS NOT NULL "
        f"AND NOT c.value_currency IN {_ISO_LIST_CYPHER} RETURN count(*) AS violations",
        zero_violations("contracts with an unrecognised currency"),
        "A currency outside ISO-4217 cannot be FX-converted; those values silently drop out of "
        "EUR aggregates.",
    ),
    Assertion(
        "values.contract_pubdate_not_future", VALUES,
        "Contract.publication_date is not in the future", BLOCK, "cypher",
        "MATCH (c:Contract) WHERE c.publication_date IS NOT NULL "
        "AND c.publication_date > toString(date()) RETURN count(*) AS violations",
        zero_violations("future-dated contracts"),
        "A publication date in the future is a parser artifact and corrupts time-series panels.",
    ),
    Assertion(
        "values.financialyear_year_range", VALUES,
        "FinancialYear.year is within [1990, current+1]", BLOCK, "cypher",
        "MATCH (f:FinancialYear) WHERE f.year IS NOT NULL "
        "AND (f.year < 1990 OR f.year > date().year + 1) RETURN count(*) AS violations",
        zero_violations("financial years out of plausible range"),
        "Financial years outside a plausible range are XBRL parsing artifacts.",
    ),
    Assertion(
        "values.lobby_cost_band", VALUES,
        "Lobbying detail_cost_max >= detail_cost_min", BLOCK, "cypher",
        "MATCH (d:Disclosure) WHERE d.detail_cost_min IS NOT NULL "
        "AND d.detail_cost_max IS NOT NULL "
        "AND d.detail_cost_max < d.detail_cost_min RETURN count(*) AS violations",
        zero_violations("inverted lobby spend bands"),
        "Lobby-spend bands are (low<=high) ranges by definition; inversions are register parsing "
        "bugs.",
    ),
    Assertion(
        "values.nuts_level", VALUES,
        "NUTSRegion.level is in {0,1,2,3}", BLOCK, "cypher",
        "MATCH (n:NUTSRegion) WHERE NOT n.level IN [0, 1, 2, 3] RETURN count(*) AS violations",
        zero_violations("NUTS regions with an invalid level"),
        "NUTS levels are 0-3 by the Eurostat standard; anything else corrupts geographic rollups.",
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
        "The RDF mirror trails the event log when the sink stalls; sustained lag means SPARQL "
        "surfaces serve stale data.",
    ),
    Assertion(
        "pipeline.neo4j_deadletter", PIPELINE,
        "neo4j_sink has no dead-lettered events", WARN, "sql",
        "SELECT count(*) AS violations FROM events.dead_letter WHERE consumer = 'neo4j_sink'",
        zero_violations("neo4j_sink dead-letters"),
        "Dead-lettered events are recorded-but-unapplied graph writes; growth means a live "
        "rendering bug.",
    ),
    Assertion(
        "pipeline.deadletter_total", PIPELINE,
        "Total dead-letter backlog is small", WARN, "sql",
        "SELECT count(*) AS dl FROM events.dead_letter",
        le_threshold("dl", 100, "dead-letters"),
        "Total parked events across consumers; the threshold flags a new failure class, the bulk "
        "is triaged history.",
    ),
    Assertion(
        "pipeline.stuck_runs", PIPELINE,
        "No etl_run stuck 'running' beyond 6h", WARN, "sql",
        "SELECT count(*) AS violations FROM events.etl_run "
        "WHERE status = 'running' AND started_at < now() - interval '6 hours'",
        zero_violations("ETL runs stuck in 'running'"),
        "Runs stuck in 'running' are crashed pods (OOM/SIGKILL) that never closed their row; "
        "they hide real failures.",
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
    Assertion(
        "coverage.cohesion_min_projects", COVERAGE,
        "EU cohesion (Kohesio) carries a meaningful project count (>=150k)",
        WARN, "cypher",
        "MATCH (d:Disclosure {system:'eu-cohesion'}) RETURN count(d) AS found",
        at_least("found", 150000, "cohesion disclosures"),
        "Kohesio 2021-27 across all 27 members is far more than the broken "
        "partial load. A low count means countries failed to download and "
        "were silently skipped (load_eu_knowledge_graph.py swallows the "
        "per-country HTTPError and still exits success).",
    ),
    Assertion(
        "coverage.cohesion_country_coverage", COVERAGE,
        "EU cohesion spans >=20 of 27 members (>=50 beneficiaries each)",
        WARN, "cypher",
        "MATCH (d:Disclosure {system:'eu-cohesion'})-[:FILED_BY]->(c:Company) "
        "WHERE c.country IS NOT NULL "
        "WITH c.country AS country, count(*) AS n WHERE n >= 50 "
        "RETURN count(country) AS found",
        at_least("found", 20, "EU members with >=50 cohesion beneficiaries"),
        "Silent per-country download failures drop big members (Italy is "
        "absent; AUT/DNK/HRV/LUX/ROU/SWE land ~1 record). A real load "
        "reaches most of the 27.",
    ),
    Assertion(
        "coverage.cohesion_beneficiary_linkage", COVERAGE,
        "EU cohesion beneficiaries link into the company graph (>=20%)",
        WARN, "cypher",
        "MATCH (d:Disclosure {system:'eu-cohesion'})-[:FILED_BY]->(c:Company) "
        "WITH DISTINCT c, size([(c)--() | 1]) AS degree "
        "WITH count(c) AS total, "
        "sum(CASE WHEN degree > 1 THEN 1 ELSE 0 END) AS covered "
        "RETURN total, covered",
        min_coverage(0.20, "cohesion beneficiaries connected to the graph"),
        "Beneficiary gmr_id is minted via a bespoke kohesio_ben:Q<qid> scheme "
        "no other loader uses, so beneficiaries are isolated twins with no "
        "link to the canonical company graph - cohesion spend can't be joined "
        "to TED / GLEIF / financials.",
    ),
    Assertion(
        "values.cohesion_no_unnamed_collapse", VALUES,
        "No cohesion beneficiary collapsed into a 'nan'/empty-name company",
        BLOCK, "cypher",
        "MATCH (c:Company)<-[:FILED_BY]-(d:Disclosure {system:'eu-cohesion'}) "
        "WHERE toLower(trim(coalesce(c.name, ''))) IN "
        "['nan', '', 'n/a', 'none', 'null', '-'] "
        "WITH c, count(DISTINCT d.detail_beneficiary_qid) AS qids "
        "WHERE qids > 1 "
        "RETURN count(DISTINCT c) AS violations",
        zero_violations("distinct cohesion beneficiaries collapsed under one "
                        "missing-name node"),
        "Kohesio writes missing names as 'nan'; minting from_name('nan') merges "
        "distinct beneficiaries (different QIDs) into one node. The loader keys "
        "the unnamed by their QID so they stay separate -- this flags a "
        "missing-name node only when it still spans >1 distinct beneficiary "
        "QID (the collapse signature), not the legitimately-unnamed-but-"
        "distinct nodes that mere emptiness would over-count.",
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

    # ----- Oracle: our computed indicators vs externally-published figures -----
    Assertion(
        "oracle.hungary_single_bidder_rate", ORACLE,
        "Hungary single-bidder rate matches the EC Single Market Scoreboard band",
        WARN, "cypher",
        "MATCH (c:Contract) WHERE c.country IN ['HUN', 'HU'] "
        "AND c.tenders_received IS NOT NULL "
        "AND coalesce(c.is_current, (c.notice_type IS NULL OR c.notice_type <> 'can-modif')) "
        "WITH count(*) AS sample, "
        "count(CASE WHEN c.tenders_received = 1 THEN 1 END) AS single "
        "RETURN sample, CASE WHEN sample > 0 THEN toFloat(single) / sample "
        "ELSE 0.0 END AS rate",
        oracle_band(0.20, 0.60, 100, "HU single-bidder"),
        "Oracle. The EC Single Market Scoreboard reports Hungary's "
        "single-bidder share among the highest in the EU (~30-45% across "
        "recent editions). Our independently-computed rate must land in that "
        "published band — if it drifts out, the suspect is our ingestion or "
        "the indicator logic, not Hungary. WARN until the historical TED "
        "backfill aligns our window with the EC's published years; the band "
        "is deliberately wide to tolerate the period gap.",
    ),
    Assertion(
        "oracle.eu_single_bidder_rate", ORACLE,
        "EU-wide single-bidder rate is in the EC-published range", WARN, "cypher",
        "MATCH (c:Contract) WHERE c.tenders_received IS NOT NULL "
        "AND coalesce(c.is_current, (c.notice_type IS NULL OR c.notice_type <> 'can-modif')) "
        "WITH count(*) AS sample, "
        "count(CASE WHEN c.tenders_received = 1 THEN 1 END) AS single "
        "RETURN sample, CASE WHEN sample > 0 THEN toFloat(single) / sample "
        "ELSE 0.0 END AS rate",
        oracle_band(0.05, 0.45, 1000, "EU single-bidder"),
        "Oracle. The EC reports the EU aggregate single-bidder share in the "
        "low-to-mid tens of percent. A rate outside this wide band means our "
        "bidder-count capture is systematically wrong (e.g. the 0-as-1 bug we "
        "fixed), not that the single market changed overnight.",
    ),
    # ---- FX exchange-rate health + new graph-integrity checks -------------
    Assertion(
        "keys.critical_indexes_present", KEYS,
        "Required Neo4j indexes exist for every sink-matched (label, key)",
        BLOCK, "cypher",
        # Every (label, property) the neo4j-sink MATCHes/MERGEs on must have a
        # covering index, else the MERGE degrades to an O(n) label scan. The
        # required set mirrors neo4j_sink._KEY_FIELD_BY_LABEL + the
        # extra_relationship targets (Listing.ticker, Authority.authority_id,
        # ...). Checked by (label, first-property), not index name, so a
        # differently-named index still satisfies it. Keep REQUIRED_INDEXES in
        # sync with the sink when it learns a new label; the count literal
        # below must equal the number of pairs listed.
        "SHOW INDEXES YIELD labelsOrTypes, properties "
        "WHERE labelsOrTypes IS NOT NULL AND [labelsOrTypes[0], properties[0]] IN "
        "[['Company','gmr_id'],['Contract','ted_notice_id'],"
        "['Authority','authority_id'],['Listing','ticker'],"
        "['SanctionedEntity','entity_id'],['Cpv','code'],"
        "['Disclosure','system'],['Programme','code'],"
        "['Fund','code'],['NUTSRegion','code']] "
        "RETURN 10 - count(DISTINCT [labelsOrTypes[0], properties[0]]) AS violations",
        zero_violations("missing required indexes"),
        "Every (label, key) the sink MATCHes/MERGEs on must be indexed. A "
        "missing index turns each MERGE into an O(n) label scan and the sink "
        "crawls — observed twice in prod (:Disclosure ~0/sec; "
        ":Authority(authority_id) a 13s relationship batch, ~150x slower "
        "drain). Derived from the sink key-field map + edge targets; this is a "
        "prod incident, not a nicety.",
    ),
    Assertion(
        "coverage.cohesion_programme_financed", COVERAGE,
        "Cohesion programmes are financed by a fund", WARN, "cypher",
        "MATCH (p:Programme) WHERE NOT (p)-[:FINANCED_BY]->(:Fund) "
        "RETURN count(*) AS violations",
        zero_violations("programmes with no FINANCED_BY fund"),
        "The cohesion model is (:CohesionProject)-[:UNDER_PROGRAMME]->"
        "(:Programme)-[:FINANCED_BY]->(:Fund). A programme with no fund means "
        "the per-system taxonomy label or the relationship emit regressed.",
    ),
    Assertion(
        "coverage.contract_currency_convertible", COVERAGE,
        "Contracts valued in a real currency convert to EUR", WARN, "cypher",
        "MATCH (c:Contract) WHERE c.value_currency =~ '[A-Z]{3}' "
        "AND c.value_original IS NOT NULL AND c.value_eur IS NULL "
        "RETURN count(*) AS violations",
        le_threshold("violations", 500, "contracts with an unconverted value"),
        "A real-currency value that didn't convert to EUR is invisible to "
        "every aggregate. MDL/MKD/UAH/RSD have no free rate source (a known "
        "gap); the threshold surfaces growth beyond the current ~300.",
    ),
    Assertion(
        "consistency.cellar_mirror_parity", CONSISTENCY,
        "Sampled works in the CELLAR mirror match the source term-for-term",
        WARN, "consistency", "CellarMirror",
        zero_with_detail("works differing from CELLAR (of 8 sampled)"),
        "The legislative mirror (graph mirror/cellar/eu) is a VERBATIM "
        "CDM copy — for random works the full work/expression/"
        "manifestation closure must match CELLAR exactly. This is the "
        "permanent form of the parity check that caught the export bug "
        "which silently dropped every work-level triple (gitops#290). "
        "A mismatch is mirror loss or source-side drift since the "
        "snapshot; the detail names the record either way.",
    ),
    Assertion(
        "consistency.contract_neo4j_virtuoso", CONSISTENCY,
        "Random contracts render identically in Neo4j + Virtuoso", WARN,
        "consistency", "Contract",
        zero_with_detail("inconsistent contracts (of 12 sampled)"),
        "Both sinks project the same events.entity_events stream, so a "
        "sampled contract whose value/currency/procedure/bidders/cpv differ "
        "across stores means a sink dropped, lagged, or mis-rendered an event. "
        "Sampling spot-check (random dozen) -> WARN, not BLOCK.",
    ),
    Assertion(
        "consistency.company_neo4j_virtuoso", CONSISTENCY,
        "Random companies render identically in Neo4j + Virtuoso", WARN,
        "consistency", "Company",
        zero_with_detail("inconsistent companies (of 12 sampled)"),
        "Sampled companies must agree on name + country across stores. LEI and "
        "other GLEIF enrichment are intentionally excluded -- that is a load-"
        "coverage question (Neo4j leads Virtuoso by ~3.8% on LEI), not a sink-"
        "render inconsistency.",
    ),
    # ── Value quarantine (withheld bad values stay withheld) ──────────
    Assertion(
        "values.quarantined_carries_no_value", VALUES,
        "Quarantined contracts carry no monetary props", BLOCK,
        "cypher",
        "MATCH (ct:Contract) WHERE ct.value_quarantined = true "
        "AND (ct.value_eur IS NOT NULL OR ct.value_original IS NOT NULL) "
        "RETURN count(ct) AS violations",
        zero_violations(),
        "The whole point of quarantine is that nobody downstream needs "
        "to remember a flag exists — a quarantined contract with a "
        "rendered value means the sink clear-path or the loader strip "
        "broke.",
    ),
    Assertion(
        "values.hard_flags_are_quarantined", VALUES,
        "Hard-flagged values are actually quarantined", BLOCK,
        "cypher",
        "MATCH (ct:Contract) WHERE ct.value_eur IS NOT NULL "
        "AND coalesce(ct.value_quarantined, false) = false "
        "AND (ct.value_quality_flag IN "
        "['concession_negative','unverified_single_signal','zero_value'] "
        "OR (ct.value_quality_flag = 'implausible_magnitude' "
        "AND ct.value_confidence < 0.05)) "
        "RETURN count(ct) AS violations",
        zero_violations(),
        "A quarantine-tier value (categorical flags, or implausible "
        "below the confidence floor) still rendered means a "
        "pre-quarantine rendering escaped the backfill or a new emit "
        "path skipped the scorer. implausible_magnitude with "
        "confidence >= 0.05 legitimately keeps its value (mega-"
        "contracts) — that band is excluded here.",
    ),
    # ── InvestmentFund model (funds are not companies) ────────────────
    Assertion(
        "keys.investmentfund_gmr_id", KEYS,
        "Every InvestmentFund has a gmr_id", BLOCK,
        "cypher",
        "MATCH (f:InvestmentFund) WHERE f.gmr_id IS NULL "
        "RETURN count(f) AS violations",
        zero_violations(),
        "gmr_id is the merge key; a fund without one can never be "
        "updated or relabeled again.",
    ),
    Assertion(
        "refs.no_dual_company_fund_label", REFS,
        "No node is both :Company and :InvestmentFund", BLOCK,
        "cypher",
        "MATCH (n) WHERE n:Company AND n:InvestmentFund "
        "RETURN count(n) AS violations",
        zero_violations(),
        "The neo4j-sink relabels in place (SET :InvestmentFund REMOVE "
        ":Company, and UpsertCompany refreshes never relabel back). A "
        "dual-labeled node means that invariant broke and queries "
        "would double-count the entity on both surfaces.",
    ),
    # ── #270: GLEIF entity.category is the SOLE authority for the
    # Company/InvestmentFund label, and match provenance rides the
    # award edge. ────────────────────────────────────────────────────
    Assertion(
        "refs.company_not_gleif_fund", REFS,
        "No :Company is a GLEIF fund (entity_kind='FUND')", BLOCK,
        "cypher",
        "MATCH (c:Company) WHERE c.entity_kind = 'FUND' "
        "RETURN count(*) AS violations",
        zero_violations("companies GLEIF records as FUND but not relabeled"),
        "GLEIF entity.category drives the label. A Company GLEIF calls a "
        "FUND should have been relabeled :InvestmentFund in the same sink "
        "write; a residual here means the relabel/reprocess has not "
        "caught up.",
    ),
    Assertion(
        "refs.fund_matches_gleif_category", REFS,
        "Every :InvestmentFund GLEIF knows is category FUND", BLOCK,
        "cypher",
        "MATCH (f:InvestmentFund) WHERE f.entity_kind IS NOT NULL "
        "AND f.entity_kind <> 'FUND' RETURN count(*) AS violations",
        zero_violations("funds GLEIF records as a non-FUND category"),
        "The inverse guard and the #270 headline: asset managers "
        "(BNP Paribas AM, Ostrum AM) were promoted to :InvestmentFund "
        "from the securityType of instruments they ISSUE, though GLEIF "
        "records them GENERAL. entity.category, not FIGI, decides; the "
        "sink reverts them to :Company on the next GLEIF reprocess.",
    ),
    Assertion(
        "coverage.graph_stub_nodes", COVERAGE,
        "Stub placeholder nodes stay near zero", WARN, "cypher",
        "MATCH (n) WHERE n._stub RETURN count(n) AS stubs",
        le_threshold("stubs", 100, "stub placeholder nodes"),
        "The neo4j sink MERGEs a {_stub: true} placeholder when a "
        "relationship's endpoint hasn't arrived yet (instead of silently "
        "dropping the edge); the entity's own upsert clears the flag. A "
        "persistent stub population means a source is referencing "
        "entities nothing ever loads — visible debt, not silent loss.",
    ),
    Assertion(
        "coverage.fund_label_sourced", COVERAGE,
        "InvestmentFund labels are GLEIF-sourced (entity_kind='FUND')",
        WARN, "cypher",
        "MATCH (f:InvestmentFund) RETURN count(f) AS total, "
        "count(CASE WHEN f.entity_kind = 'FUND' THEN 1 END) AS covered",
        min_coverage(0.90, "GLEIF-sourced fund labels"),
        "Surfaces :InvestmentFund nodes with no GLEIF FUND category "
        "backing — historical instrument-inferred labels with no LEI in "
        "GLEIF to confirm them. WARN not BLOCK: there is no authority to "
        "revert them against, so they are cleaned up out-of-band rather "
        "than gated on.",
    ),
    Assertion(
        "values.awarded_to_match_confidence_range", VALUES,
        "AWARDED_TO.match_confidence is within [0,1]", BLOCK, "cypher",
        "MATCH ()-[r:AWARDED_TO]->() WHERE r.match_confidence IS NOT NULL "
        "AND (r.match_confidence < 0 OR r.match_confidence > 1) "
        "RETURN count(*) AS violations",
        zero_violations("out-of-range match_confidence values"),
        "Match provenance on the award edge must be sane: a confidence "
        "outside [0,1] is a producer bug, not a real attribution.",
    ),
    Assertion(
        "values.awarded_to_match_tier_known", VALUES,
        "AWARDED_TO.match_tier is a known tier", BLOCK, "cypher",
        "MATCH ()-[r:AWARDED_TO]->() WHERE r.match_tier IS NOT NULL "
        "AND NOT r.match_tier IN "
        "['lei', 'vat', 'cik', 'registered_as', 'name_country', 'fuzzy'] "
        "RETURN count(*) AS violations",
        zero_violations("unknown match_tier values"),
        "The edge tier separates exact (lei/vat/cik) from name-based "
        "(name_country/fuzzy) attributions; an unknown value would break "
        "that read in queries and the UI.",
    ),
    Assertion(
        "coverage.fund_unit_security_type", COVERAGE,
        "Fund unit listings carry security_type", WARN,
        "cypher",
        "MATCH (:InvestmentFund)-[:LISTED_AS]->(l:Listing) "
        "RETURN sum(CASE WHEN l.security_type IS NULL THEN 1 ELSE 0 "
        "END) AS violations, count(l) AS total",
        zero_violations(total_key="total"),
        "Fund units are only routed at the fund BECAUSE of "
        "security_type; a unit without it was attached by some other "
        "path and deserves a look.",
    ),
    # ── Price layer (NFS index vs graph universe) ─────────────────────
    Assertion(
        "freshness.price_index_present", FRESHNESS,
        "Price index + graph universe exist on the NFS", WARN,
        "prices",
        "index_present,universe_present",
        lambda row: (
            bool(row.get("index_present")) and bool(row.get("universe_present")),
            f"index_present={row.get('index_present')} "
            f"universe_present={row.get('universe_present')}",
        ),
        "Both files are produced nightly (etl-price-universe 02:30, "
        "usa-stock-price-fetcher 03:00). Either missing means the "
        "price pipeline is stranded again — exactly the 2026-03..07 "
        "outage this cron chain fixed.",
    ),
    Assertion(
        "freshness.price_data_fresh", FRESHNESS,
        "Tracked tickers are mostly fresh (7d)", WARN,
        "prices",
        "fresh_ratio",
        lambda row: (
            float(row.get("fresh_ratio") or 0) >= 0.5,
            f"fresh_ratio={row.get('fresh_ratio')} "
            f"(fresh={row.get('fresh_7d')}/with_data={row.get('with_data')})",
        ),
        "Yahoo throttling or a dead cron shows up here first. 0.5 is "
        "deliberately loose while the initial multi-night backlog "
        "clears; tighten once universe_backlog approaches zero.",
    ),
]


def by_id() -> dict[str, Assertion]:
    return {a.id: a for a in ASSERTIONS}


# Cheap import-time guard: assertion ids must be unique.
if len(by_id()) != len(ASSERTIONS):  # pragma: no cover - guard
    raise RuntimeError("duplicate assertion id in catalog")
