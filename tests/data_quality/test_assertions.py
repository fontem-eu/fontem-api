"""Unit tests for the data-quality assertion catalog + runner.

No live database: the runner takes injected query callables, so we
drive every code path with in-memory fakes.
"""
# Tests deliberately exercise module internals (DSN parsing, runner
# wiring) by name, and use stub callables with fixed signatures.
# pylint: disable=protected-access,unused-argument
from __future__ import annotations

import json
import re

import pytest

from src.data_quality.assertions import catalog
from src.data_quality.assertions.catalog import (
    ASSERTIONS, BLOCK, WARN, KEYS, REFS, VALUES, PIPELINE, FRESHNESS, GOLDEN,
    CONSISTENCY, LINGUISTICS, RESOLUTION,
    COVERAGE, ORACLE, GRAIN, Assertion, by_id, le_threshold, min_coverage,
    no_confident_pair, oracle_band, resolves_confidently, resolves_via_tier,
    zero_violations,
    zero_with_detail,
)
from src.data_quality.assertions import consistency
from src.data_quality.assertions.consistency import cellar_mirror_check
from src.data_quality.assertions.runner import (
    AssertionResult, ERROR, FAIL, PASS, evaluate_assertion, exit_code,
    format_report, run_catalog, summarise,
)
from src.data_quality.assertions import __main__ as cli


# --------------------------------------------------------------------------
# Catalog wellformedness
# --------------------------------------------------------------------------
def test_ids_unique():
    assert len(by_id()) == len(ASSERTIONS)


def test_families_and_severities_valid():
    fams = {KEYS, REFS, VALUES, PIPELINE, FRESHNESS, GOLDEN, COVERAGE, ORACLE,
            CONSISTENCY, GRAIN, LINGUISTICS, RESOLUTION}
    for a in ASSERTIONS:
        assert a.family in fams, a.id
        assert a.severity in (BLOCK, WARN), a.id
        assert a.engine in ("cypher", "sql", "consistency", "prices",
                            "consolidator"), a.id
        assert a.query.strip(), a.id
        assert callable(a.evaluate), a.id


def test_two_tier_severity_mapping():
    # keys/refs always block; pipeline/freshness always warn.
    for a in ASSERTIONS:
        if a.family in (KEYS, REFS):
            assert a.severity == BLOCK, a.id
        if a.family in (PIPELINE, FRESHNESS):
            assert a.severity == WARN, a.id
        # linguistics is enrichment progress (red for weeks until the
        # pipeline first runs) — never a gate; resolution probes service
        # correctness — always a gate.
        if a.family == LINGUISTICS:
            assert a.severity == WARN, a.id
        if a.family == RESOLUTION:
            assert a.severity == BLOCK, a.id


def test_values_block_except_documented_warn():
    warn_values = {a.id for a in ASSERTIONS if a.family == VALUES and a.severity == WARN}
    # The only intentional warn in the value family is the accounting identity.
    assert warn_values == {"values.accounting_identity"}


def test_engine_matches_family():
    # Graph families use cypher; events families use sql.
    for a in ASSERTIONS:
        if a.family in (KEYS, REFS, VALUES, GOLDEN, COVERAGE, ORACLE, GRAIN):
            assert a.engine == "cypher", a.id
        elif a.family == CONSISTENCY:
            assert a.engine == "consistency", a.id
        elif a.family == LINGUISTICS:
            # graph-side coverage is cypher; search-store floors +
            # sink lag are sql — per assertion.
            assert a.engine in ("cypher", "sql"), a.id
        elif a.family == RESOLUTION:
            assert a.engine == "consolidator", a.id
        else:
            # events families are sql; the price-layer freshness pair
            # reads the NFS index via the dedicated prices engine.
            assert a.engine in ("sql", "prices"), a.id


def test_freshness_query_includes_known_cronjobs():
    q = catalog._cadence_freshness_query()
    assert "etl-gleif" in q and "etl-ted-contracts" in q
    assert "events.etl_run" in q


# --------------------------------------------------------------------------
# Evaluator factories
# --------------------------------------------------------------------------
def test_zero_violations_pass_and_fail():
    ev = zero_violations("things", total_key="total")
    ok, obs = ev({"violations": 0, "total": 10})
    assert ok and "0 things of 10" == obs
    ok, obs = ev({"violations": 3, "total": 10})
    assert not ok and "3 things of 10" == obs


def test_zero_violations_handles_missing_keys():
    ok, obs = zero_violations()({})
    assert ok and obs == "0 violations"


def test_le_threshold():
    ev = le_threshold("lag", 1000, "lag")
    assert ev({"lag": 0})[0] is True
    assert ev({"lag": 1000})[0] is True
    assert ev({"lag": 1001})[0] is False
    assert "lag=1001 (limit 1000)" == ev({"lag": 1001})[1]


def test_zero_with_detail():
    ev = zero_with_detail("stale sources")
    ok, obs = ev({"violations": 0, "detail": "ignored"})
    assert ok and obs == "0 stale sources"
    ok, obs = ev({"violations": 2, "detail": "etl-gleif (500h/240h)"})
    assert not ok and "2 stale sources: etl-gleif" in obs


# --------------------------------------------------------------------------
# evaluate_assertion — pass / fail / warn / error classification
# --------------------------------------------------------------------------
def _assertion(severity=BLOCK, engine="cypher", evaluate=None):
    return Assertion(
        "t.x", KEYS, "t", severity, engine,
        "RETURN 1 AS violations", evaluate or zero_violations(),
    )


def test_evaluate_pass():
    r = evaluate_assertion(_assertion(), lambda q: {"violations": 0}, lambda q: {})
    assert r.status == PASS and r.ok


def test_evaluate_block_failure_is_fail():
    r = evaluate_assertion(_assertion(BLOCK), lambda q: {"violations": 5}, lambda q: {})
    assert r.status == FAIL


def test_evaluate_warn_failure_is_warn():
    r = evaluate_assertion(_assertion(WARN), lambda q: {"violations": 5}, lambda q: {})
    assert r.status == WARN


def test_evaluate_block_query_error_is_error():
    def boom(_q):
        raise ValueError("bad cypher")
    r = evaluate_assertion(_assertion(BLOCK), boom, lambda q: {})
    assert r.status == ERROR and "ValueError" in r.observed


def test_evaluate_warn_query_error_is_warn():
    def boom(_q):
        raise RuntimeError("no db")
    r = evaluate_assertion(_assertion(WARN, engine="sql"), lambda q: {}, boom)
    assert r.status == WARN


def test_evaluate_routes_engine():
    seen = {}
    def cy(q):
        seen["cypher"] = True
        return {"violations": 0}
    def sq(q):
        seen["sql"] = True
        return {"violations": 0}
    evaluate_assertion(_assertion(engine="sql"), cy, sq)
    assert seen == {"sql": True}


# --------------------------------------------------------------------------
# run_catalog / summarise / exit_code / report
# --------------------------------------------------------------------------
def _all_clean_cypher(_q):
    # Every catalog cypher query aliases its count to `violations`/`total`;
    # zero violations + a benign total/lag satisfies all evaluators.
    return {"violations": 0, "total": 0, "covered": 0, "lag": 0, "dl": 0,
            "detail": "", "found": 300000}


def _all_clean_sql(_q):
    # `found` clears every sql at_least floor (largest: the 3M company
    # embedding-row floor).
    return {"violations": 0, "lag": 0, "dl": 0, "detail": "", "found": 3_500_000}


def _all_clean_consolidator(query):
    """Dispatch on the parsed JSON spec, the way the real runner does —
    the nonsense-name probe must NOT pair while the known-entity probes
    must, so a single static row cannot satisfy the family."""
    body = json.loads(query)["body"]
    if "Zzqx" in (body.get("name") or ""):
        return {"hint": "no_match", "match_found": 0, "n_candidates": 0,
                "top_confidence": 0.0, "top_tier": ""}
    if body.get("vat"):
        return {"hint": "matched", "match_found": 1, "n_candidates": 0,
                "top_confidence": 0.99, "top_tier": "vat"}
    return {"hint": "matched", "match_found": 1, "n_candidates": 0,
            "top_confidence": 0.95, "top_tier": "name_country"}


def test_run_catalog_all_pass():
    results = run_catalog(
        _all_clean_cypher, _all_clean_sql,
        consistency=lambda et: {"violations": 0, "total": 12, "detail": ""},
        prices=lambda q: {"index_present": True, "universe_present": True,
                          "fresh_ratio": 1.0, "fresh_7d": 1, "with_data": 1},
        consolidator=_all_clean_consolidator)
    assert len(results) == len(ASSERTIONS)
    assert all(r.status == PASS for r in results)
    assert exit_code(results) == 0


def test_exit_code_fails_on_block_fail():
    results = [
        AssertionResult("a", KEYS, "a", BLOCK, FAIL, "1 bad"),
        AssertionResult("b", PIPELINE, "b", WARN, WARN, "lagging"),
    ]
    assert exit_code(results) == 1


def test_exit_code_zero_on_warn_only():
    results = [AssertionResult("b", PIPELINE, "b", WARN, WARN, "lagging")]
    assert exit_code(results) == 0


def test_summarise_counts():
    results = [
        AssertionResult("a", KEYS, "a", BLOCK, PASS, ""),
        AssertionResult("b", KEYS, "b", BLOCK, FAIL, ""),
        AssertionResult("c", PIPELINE, "c", WARN, WARN, ""),
    ]
    c = summarise(results)
    assert c[PASS] == 1 and c[FAIL] == 1 and c[WARN] == 1


def test_format_report_contains_verdict_and_families():
    results = run_catalog(_all_clean_cypher, _all_clean_sql,
                          consolidator=_all_clean_consolidator)
    text = format_report(results, "staging")
    assert "staging" in text
    assert "[keys]" in text and "[freshness]" in text
    assert "Gate: OK" in text


def test_format_report_failed_verdict():
    results = [AssertionResult("a", KEYS, "a", BLOCK, FAIL, "1 bad")]
    assert "Gate: FAILED" in format_report(results)


# --------------------------------------------------------------------------
# CLI helpers
# --------------------------------------------------------------------------
def test_normalise_dsn():
    assert cli._normalise_dsn(None) is None
    assert cli._normalise_dsn("postgresql+asyncpg://u:p@h/db") == "postgresql://u:p@h/db"
    assert cli._normalise_dsn("postgresql://u:$(PW)@h/db") is None
    assert cli._normalise_dsn("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


def test_select_families():
    assert cli._select(None) == ASSERTIONS
    keys_only = cli._select("keys")
    assert keys_only and all(a.family == KEYS for a in keys_only)
    two = cli._select("keys, refs")
    assert {a.family for a in two} == {KEYS, REFS}


def test_cypher_runner_shapes_row():
    class _Rec(dict):
        pass
    class _Result:
        def single(self):
            return _Rec({"violations": 2})
    class _Session:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def run(self, _q):
            return _Result()
    class _Client:
        def session(self):
            return _Session()
    run = cli._build_cypher_runner(_Client())
    assert run("RETURN 1") == {"violations": 2}


def test_cypher_runner_empty_on_no_record():
    class _Result:
        def single(self):
            return None
    class _Session:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def run(self, _q):
            return _Result()
    class _Client:
        def session(self):
            return _Session()
    out = cli._build_cypher_runner(_Client())("RETURN 1")
    assert isinstance(out, dict) and not out


def test_sql_runner_raises_without_dsn():
    run = cli._build_sql_runner(None)
    with pytest.raises(RuntimeError):
        run("SELECT 1")


def test_gdpr_and_coverage_assertions_present_and_blocking():
    cat = by_id()
    # Privacy guard + coverage check for the lobbying dereg invariant.
    for aid in ("values.deregistered_lobbyist_name_redacted",
                "values.active_lobbyist_has_name"):
        assert aid in cat, aid
        assert cat[aid].severity == BLOCK, aid
        assert cat[aid].engine == "cypher", aid
        assert cat[aid].family == VALUES, aid
    # The redaction guard must key off the deregistered marker + the
    # redaction sentinel so it actually catches a leaked name.
    q = cat["values.deregistered_lobbyist_name_redacted"].query
    assert "detail_active = false" in q and "[deregistered]" in q


def test_min_coverage_evaluator():
    ev = min_coverage(0.80, "pt")
    assert ev({"total": 0, "covered": 0})[0] is True          # no rows → ok
    assert ev({"total": 10, "covered": 9})[0] is True          # 90% >= 80%
    assert ev({"total": 10, "covered": 5})[0] is False         # 50% < 80%


def test_oracle_band_in_range_passes():
    ev = oracle_band(0.20, 0.60, 100, "HU single-bidder")
    ok, obs = ev({"sample": 500, "rate": 0.42})
    assert ok and "0.420" in obs


def test_oracle_band_out_of_range_fails():
    ev = oracle_band(0.20, 0.60, 100, "HU single-bidder")
    ok, obs = ev({"sample": 500, "rate": 0.05})
    assert not ok and "0.050" in obs


def test_oracle_band_thin_sample_passes_with_note():
    # Too few rows to judge — we validate computation, not manufacture a verdict.
    ev = oracle_band(0.20, 0.60, 100, "HU single-bidder")
    ok, obs = ev({"sample": 12, "rate": 0.99})
    assert ok and "too thin" in obs


def test_critical_indexes_count_matches_pairs():
    """The generalized index assertion uses `RETURN N - count(DISTINCT ...)`;
    N must equal the number of (label, key) pairs it lists, or a pair added
    without bumping N would be silently unchecked."""
    a = next(x for x in ASSERTIONS if x.id == "keys.critical_indexes_present")
    pairs = re.findall(r"\['[A-Za-z]+', ?'[A-Za-z_]+'\]", a.query)
    m = re.search(r"RETURN (\d+) - count\(DISTINCT", a.query)
    assert m, "expected `RETURN N - count(DISTINCT ...)` form"
    assert int(m.group(1)) == len(pairs) > 0, (
        f"index assertion lists {len(pairs)} pairs but subtracts {m.group(1)}"
    )


# ── cross-store consistency engine ────────────────────────────────────────
_ONT = "http://data.fontem.eu/ontology#"


class _FakeNeoSession:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query):  # noqa: ARG002 - fake ignores the query
        return iter(self._rows)


class _FakeNeoClient:
    def __init__(self, rows):
        self._rows = rows

    def session(self):
        return _FakeNeoSession(self._rows)


class _FakeVirtuoso:
    def __init__(self, by_iri):
        self._by_iri = by_iri

    def query(self, q):
        m = re.search(r"<([^>]+)>", q)
        return self._by_iri.get(m.group(1) if m else "", [])


def test_consistency_check_passes_when_aligned_flags_when_not():
    neo = _FakeNeoClient([
        {"_key": "n1", "value_eur": 100.0, "procedure_type": "open"},   # aligned
        {"_key": "n2", "value_eur": 200.0, "procedure_type": "open"},   # proc mismatch
        {"_key": "n3", "value_eur": 300.0, "procedure_type": "open"},   # absent
    ])
    virt = _FakeVirtuoso({
        "http://data.fontem.eu/id/Contract/n1": [
            {"p": _ONT + "valueEur", "o": 100.0}, {"p": _ONT + "procedureType", "o": "open"}],
        "http://data.fontem.eu/id/Contract/n2": [
            {"p": _ONT + "valueEur", "o": 200.0}, {"p": _ONT + "procedureType", "o": "restricted"}],
        # n3 has no triples -> absent
    })
    res = consistency.check(neo, virt, "Contract", n=3)
    assert res["total"] == 3
    assert res["violations"] == 2          # n2 (mismatch) + n3 (absent)
    assert "n2.procedure_type" in res["detail"]


def test_consistency_engine_dispatch_and_missing_runner():
    a = next(x for x in ASSERTIONS if x.engine == "consistency")
    # wired runner -> evaluates the returned row
    ok = evaluate_assertion(
        a, None, None,
        consistency=lambda et: {"violations": 0, "total": 12, "detail": ""})
    assert ok.status == PASS
    # no runner wired -> WARN (not a crash)
    miss = evaluate_assertion(a, None, None, consistency=None)
    assert miss.status == WARN


def test_prices_engine_unwired_warns_not_crashes():
    """Environments without the price mount leave the prices engine
    unwired — its WARN assertions must degrade to WARN, never ERROR."""
    prices_assertions = [a for a in ASSERTIONS if a.engine == "prices"]
    assert prices_assertions, "expected price-layer assertions"
    for a in prices_assertions:
        res = evaluate_assertion(a, cypher=lambda q: {}, sql=lambda q: {})
        assert res.status == "warn", (a.id, res.status)


def test_price_freshness_evaluators():
    fresh = by_id()["freshness.price_data_fresh"]
    ok, _ = fresh.evaluate({"fresh_ratio": 0.9, "fresh_7d": 9, "with_data": 10})
    assert ok
    bad, _ = fresh.evaluate({"fresh_ratio": 0.1})
    assert not bad
    present = by_id()["freshness.price_index_present"]
    ok, _ = present.evaluate({"index_present": True, "universe_present": True})
    assert ok
    missing, _ = present.evaluate({"index_present": False,
                                   "universe_present": True})
    assert not missing


def test_fund_assertions_shapes():
    dual = by_id()["refs.no_dual_company_fund_label"]
    assert dual.severity == BLOCK
    ok, _ = dual.evaluate({"violations": 0})
    assert ok
    bad, _ = dual.evaluate({"violations": 3})
    assert not bad


# ── #270 acceptance criteria, encoded as catalog guarantees ──────────


def test_contract_awardee_assertion_accepts_investmentfund():
    """The aligned referential guard treats a relabeled fund awardee as a
    valid AWARDED_TO target (the 9 contracts that failed the gate)."""
    a = by_id()["refs.contract_has_company"]
    assert ":InvestmentFund" in a.query
    assert a.severity == BLOCK


def test_270_label_authority_assertions_present():
    """GLEIF entity.category is the sole label authority — both
    directions are BLOCK guards, plus a WARN coverage for unsourced
    fund labels."""
    cat = by_id()
    assert cat["refs.company_not_gleif_fund"].severity == BLOCK
    assert cat["refs.fund_matches_gleif_category"].severity == BLOCK
    assert cat["coverage.fund_label_sourced"].severity == WARN
    # the fund guard keys on GLEIF's entity_kind, not on any FIGI signal
    assert "entity_kind" in cat["refs.fund_matches_gleif_category"].query


def test_270_edge_provenance_validity_assertions_present():
    """match_tier/confidence on the AWARDED_TO edge are domain-checked."""
    cat = by_id()
    assert cat["values.awarded_to_match_confidence_range"].severity == BLOCK
    tier = cat["values.awarded_to_match_tier_known"]
    assert tier.severity == BLOCK
    for expected in ("lei", "name_country", "fuzzy", "registered_as"):
        assert expected in tier.query


def test_270_label_alignment_covers_disclosures_and_financialyears():
    """All entity-referencing assertions accept a relabeled
    :InvestmentFund — the EssilorLuxottica class: node + edge exist,
    only the label moved (#270 follow-up)."""
    cat = by_id()
    for aid in ("refs.disclosure_company_resolves",
                "refs.lobbying_filedby_when_matched",
                "refs.financialyear_has_company"):
        assert "InvestmentFund" in cat[aid].query, aid


def test_stub_visibility_assertion_present():
    """Sink stub-creation is observable, not silent: WARN when the stub
    population grows past the transient level."""
    a = by_id()["coverage.graph_stub_nodes"]
    assert a.severity == WARN
    assert "_stub" in a.query


def test_cellar_mirror_parity_assertion_present():
    a = by_id()["consistency.cellar_mirror_parity"]
    assert a.engine == "consistency" and a.query == "CellarMirror"
    assert a.severity == WARN


def test_cellar_mirror_check_flags_missing_terms():
    """A work whose closure or term-set differs from CELLAR is a
    violation; identical stores pass."""
    work = "http://publications.europa.eu/resource/cellar/w1"
    expr = "http://publications.europa.eu/resource/cellar/w1.0001"
    cdm = "http://publications.europa.eu/ontology/cdm#"

    def bindings(rows):
        return {"results": {"bindings": rows}}

    def make_get(mirror_terms):
        def _get(url, params):
            q = params["query"]
            if "work_date_document" in q and "ORDER BY RAND()" in q:
                return bindings([{"w": {"type": "uri", "value": work}}])
            if "SELECT DISTINCT ?s" in q:
                return bindings([{"s": {"type": "uri", "value": work}},
                                 {"s": {"type": "uri", "value": expr}}])
            # per-subject terms: mirror queries carry FROM <...mirror...>
            terms = mirror_terms if "mirror/cellar" in q else [
                {"p": {"value": cdm + "work_date_document"},
                 "o": {"type": "literal", "value": "2024-05-14",
                       "datatype": "http://www.w3.org/2001/XMLSchema#date"}}]
            return bindings(terms)
        return _get

    # mirror side uses Virtuoso-7 "typed-literal"; CELLAR side (the
    # make_get fallback) uses SPARQL-1.1 "literal" — must compare equal.
    full = [{"p": {"value": cdm + "work_date_document"},
             "o": {"type": "typed-literal", "value": "2024-05-14",
                   "datatype": "http://www.w3.org/2001/XMLSchema#date"}}]
    ok = cellar_mirror_check("http://ours/sparql?mirror/cellar", make_get(full))
    assert ok == {"violations": 0, "total": 1, "detail": ""}

    lossy = cellar_mirror_check("http://ours/sparql?mirror/cellar", make_get([]))
    assert lossy["violations"] == 1
    assert "missing" in lossy["detail"]


def test_every_assertion_has_a_description():
    """The assertion monitor renders rationale as the user-facing
    description — every assertion must carry one."""
    empty = [a.id for a in ASSERTIONS if not a.rationale.strip()]
    assert not empty, f"assertions without a description: {empty}"


# ── petitions + legislative spine checks (2026-07-15) ──────────────────


class _PetFakeSession:
    def __init__(self, n):
        self._n = n

    def run(self, _query, **_params):
        n = self._n

        class _R:
            def single(self):
                return {"n": n}
        return _R()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _PetFakeNeo4j:
    def __init__(self, n):
        self._n = n

    def session(self):
        return _PetFakeSession(self._n)


class _PetFakeVirtuoso:
    def __init__(self, n):
        self._n = n

    def query(self, _q):
        return [{"n": str(self._n)}]


def test_petition_parity_check():
    ok = consistency.petition_parity_check(_PetFakeNeo4j(132), _PetFakeVirtuoso(132))
    assert ok["violations"] == 0
    drift = consistency.petition_parity_check(_PetFakeNeo4j(132), _PetFakeVirtuoso(120))
    assert drift["violations"] == 12
    assert "neo4j=132" in drift["detail"]


def test_legal_act_spine_check_tolerates_5pct_lag():
    # graph trails the mirror by <5% — daily sweep hasn't run yet: OK
    lag = consistency.legal_act_spine_check(_PetFakeNeo4j(9700), _PetFakeVirtuoso(10000))
    assert lag["violations"] == 0
    # >5% shortfall — materializer broken
    broken = consistency.legal_act_spine_check(_PetFakeNeo4j(5000), _PetFakeVirtuoso(10000))
    assert broken["violations"] > 0
    # graph excess is never tolerated (spine outlived a mirror wipe)
    excess = consistency.legal_act_spine_check(_PetFakeNeo4j(10100), _PetFakeVirtuoso(10000))
    assert excess["violations"] == 100


def test_cellar_ft_index_check():
    built = consistency.cellar_ft_index_check(_PetFakeVirtuoso(48000))
    assert built["violations"] == 0
    # a sliver of matches means the build is in progress, not done
    sliver = consistency.cellar_ft_index_check(_PetFakeVirtuoso(1))
    assert sliver["violations"] == 1
    unbuilt = consistency.cellar_ft_index_check(_PetFakeVirtuoso(0))
    assert unbuilt["violations"] == 1
    assert "canary" in unbuilt["detail"]


# ── linguistics + resolution families (embeddings/translations/consolidator) ──


def test_linguistics_assertions_present_and_warn():
    cat = by_id()
    for aid in ("linguistics.authority_embedding_coverage",
                "linguistics.authority_translation_coverage",
                "linguistics.search_embedding_company_floor",
                "linguistics.search_embedding_authority_floor",
                "linguistics.search_embedding_single_encoder",
                "linguistics.embedding_sink_lag"):
        assert aid in cat, aid
        assert cat[aid].family == LINGUISTICS, aid
        assert cat[aid].severity == WARN, aid


def test_authority_translation_query_lists_all_24_langs():
    """The coverage query must enumerate every EU official language —
    a dropped property silently shrinks the natural-full count."""
    q = by_id()["linguistics.authority_translation_coverage"].query
    langs = ("bg", "cs", "da", "de", "el", "en", "es", "et", "fi", "fr",
             "ga", "hr", "hu", "it", "lt", "lv", "mt", "nl", "pl", "pt",
             "ro", "sk", "sl", "sv")
    assert len(langs) == 24
    for code in langs:
        assert f"a.name_{code}" in q, code
    assert ">= 20" in q


def test_authority_embedding_query_requires_768d():
    q = by_id()["linguistics.authority_embedding_coverage"].query
    assert "name_embedding_dim = 768" in q


def test_linguistics_floor_and_encoder_evaluators():
    company = by_id()["linguistics.search_embedding_company_floor"]
    assert company.evaluate({"found": 3_824_256})[0] is True
    assert company.evaluate({"found": 2_999_999})[0] is False
    authority = by_id()["linguistics.search_embedding_authority_floor"]
    assert authority.evaluate({"found": 78_398})[0] is True
    assert authority.evaluate({"found": 59_999})[0] is False
    encoder = by_id()["linguistics.search_embedding_single_encoder"]
    assert encoder.evaluate({"n_encoders": 1})[0] is True
    assert encoder.evaluate({"n_encoders": 0})[0] is True   # empty table
    ok, obs = encoder.evaluate({"n_encoders": 2})
    assert not ok and "distinct encoder_ids=2 (limit 1)" == obs


def test_embedding_sink_lag_evaluator_generous_ceiling():
    lag = by_id()["linguistics.embedding_sink_lag"]
    # 10-22M is the current backfill-flood reality: must WARN-pass.
    assert lag.evaluate({"lag": 22_000_000})[0] is True
    assert lag.evaluate({"lag": 30_000_001})[0] is False
    # the ceiling is documented as temporary
    assert "tighten" in lag.rationale.lower()


def test_resolution_assertions_present_and_blocking():
    cat = by_id()
    for aid in ("resolution.known_company_resolves",
                "resolution.nonsense_name_no_pair",
                "resolution.vat_exact_wins",
                "resolution.known_authority_resolves"):
        assert aid in cat, aid
        assert cat[aid].family == RESOLUTION, aid
        assert cat[aid].severity == BLOCK, aid
        assert cat[aid].engine == "consolidator", aid


def test_resolution_query_specs_are_valid_json():
    """Every consolidator query is a JSON spec the runner can POST."""
    for a in ASSERTIONS:
        if a.engine != "consolidator":
            continue
        spec = json.loads(a.query)
        assert spec["path"] == "/resolve", a.id
        assert spec["body"]["entity_type"] in ("Company", "Authority"), a.id
        assert spec["body"].get("name") or spec["body"].get("vat"), a.id


def test_resolves_confidently_accepts_match_or_candidates():
    ev = resolves_confidently(0.9, "probe")
    # today's ambiguous-duplicate state: candidates only
    ok, obs = ev({"hint": "ambiguous", "match_found": 0, "n_candidates": 2,
                  "top_confidence": 0.95, "top_tier": "name_country"})
    assert ok and "candidates=2" in obs
    # future post-merge state: direct match
    assert ev({"hint": "matched", "match_found": 1, "n_candidates": 0,
               "top_confidence": 0.95, "top_tier": "name_country"})[0] is True
    # broken: nothing came back
    assert ev({"hint": "no_match", "match_found": 0, "n_candidates": 0,
               "top_confidence": 0.0, "top_tier": ""})[0] is False
    # broken: only low-confidence fuzz
    assert ev({"hint": "ambiguous", "match_found": 0, "n_candidates": 3,
               "top_confidence": 0.4, "top_tier": "fuzzy"})[0] is False


def test_no_confident_pair_evaluator():
    ev = no_confident_pair(0.9, "nonsense")
    assert ev({"hint": "no_match", "match_found": 0, "n_candidates": 0,
               "top_confidence": 0.0, "top_tier": ""})[0] is True
    # low-confidence fuzzy chatter is fine — it is not a pairing
    assert ev({"hint": "ambiguous", "match_found": 0, "n_candidates": 2,
               "top_confidence": 0.5, "top_tier": "fuzzy"})[0] is True
    # a direct match on garbage is the failure mode
    assert ev({"hint": "matched", "match_found": 1, "n_candidates": 0,
               "top_confidence": 0.95, "top_tier": "name_country"})[0] is False
    # so is a near-certain candidate
    assert ev({"hint": "ambiguous", "match_found": 0, "n_candidates": 1,
               "top_confidence": 0.94, "top_tier": "fuzzy"})[0] is False


def test_resolves_via_tier_evaluator():
    ev = resolves_via_tier("vat", 0.99, "vat probe")
    ok, obs = ev({"hint": "matched", "match_found": 1, "n_candidates": 0,
                  "top_confidence": 0.99, "top_tier": "vat"})
    assert ok and "tier=vat" in obs
    # hard IDs must not come back through a name tier...
    assert ev({"hint": "matched", "match_found": 1, "n_candidates": 0,
               "top_confidence": 0.99, "top_tier": "name_country"})[0] is False
    # ...nor as an ambiguous candidate list
    assert ev({"hint": "ambiguous", "match_found": 0, "n_candidates": 2,
               "top_confidence": 0.99, "top_tier": "vat"})[0] is False


def test_consolidator_engine_unwired_blocks_error_not_crash():
    """No consolidator runner wired -> the BLOCK resolution assertions
    surface as ERROR (the existing no-runner path), never a crash."""
    for a in ASSERTIONS:
        if a.engine != "consolidator":
            continue
        res = evaluate_assertion(a, cypher=lambda q: {}, sql=lambda q: {})
        assert res.status == ERROR, (a.id, res.status)
        assert "no runner" in res.observed


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.raised = False

    def raise_for_status(self):
        self.raised = True

    def json(self):
        return self._payload


def test_consolidator_runner_flattens_match(monkeypatch):
    import httpx  # pylint: disable=import-outside-toplevel
    seen = {}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen["json"] = kwargs.get("json")
        return _FakeResponse({
            "hint": "matched",
            "match": {"gmr_id": "x", "name": "Pink Aviation", "country": "AUT",
                      "lei": None, "tier": "vat", "confidence": 0.99},
            "candidates": [], "normalised_country": None,
        })
    monkeypatch.setenv("CONSOLIDATOR_URL", "http://consolidator.test")
    monkeypatch.setattr(httpx, "post", fake_post)
    run = cli._build_consolidator_runner()
    row = run(json.dumps({"path": "/resolve",
                          "body": {"entity_type": "Company", "vat": "ATU13722300"}}))
    assert seen["url"] == "http://consolidator.test/resolve"
    assert seen["json"] == {"entity_type": "Company", "vat": "ATU13722300"}
    assert row == {"hint": "matched", "match_found": 1, "n_candidates": 0,
                   "top_confidence": 0.99, "top_tier": "vat"}


def test_consolidator_runner_flattens_candidates_taking_top(monkeypatch):
    import httpx  # pylint: disable=import-outside-toplevel

    def fake_post(url, **kwargs):
        return _FakeResponse({
            "hint": "ambiguous", "match": None,
            "candidates": [
                {"gmr_id": "a", "tier": "fuzzy", "confidence": 0.61},
                {"gmr_id": "b", "tier": "name_country", "confidence": 0.95},
            ],
            "normalised_country": "AUT",
        })
    monkeypatch.setattr(httpx, "post", fake_post)
    run = cli._build_consolidator_runner()
    row = run(json.dumps({"path": "/resolve",
                          "body": {"entity_type": "Company",
                                   "name": "STRABAG AG", "country": "AUT"}}))
    assert row == {"hint": "ambiguous", "match_found": 0, "n_candidates": 2,
                   "top_confidence": 0.95, "top_tier": "name_country"}


def test_consolidator_runner_unwired_when_url_emptied(monkeypatch):
    monkeypatch.setenv("CONSOLIDATOR_URL", "")
    assert cli._build_consolidator_runner() is None
