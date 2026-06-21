"""Unit tests for the data-quality assertion catalog + runner.

No live database: the runner takes injected query callables, so we
drive every code path with in-memory fakes.
"""
# Tests deliberately exercise module internals (DSN parsing, runner
# wiring) by name, and use stub callables with fixed signatures.
# pylint: disable=protected-access,unused-argument
from __future__ import annotations

import pytest

from src.data_quality.assertions import catalog
from src.data_quality.assertions.catalog import (
    ASSERTIONS, BLOCK, WARN, KEYS, REFS, VALUES, PIPELINE, FRESHNESS, GOLDEN,
    COVERAGE, ORACLE, Assertion, by_id, le_threshold, min_coverage, oracle_band,
    zero_violations,
    zero_with_detail,
)
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
    fams = {KEYS, REFS, VALUES, PIPELINE, FRESHNESS, GOLDEN, COVERAGE, ORACLE}
    for a in ASSERTIONS:
        assert a.family in fams, a.id
        assert a.severity in (BLOCK, WARN), a.id
        assert a.engine in ("cypher", "sql"), a.id
        assert a.query.strip(), a.id
        assert callable(a.evaluate), a.id


def test_two_tier_severity_mapping():
    # keys/refs always block; pipeline/freshness always warn.
    for a in ASSERTIONS:
        if a.family in (KEYS, REFS):
            assert a.severity == BLOCK, a.id
        if a.family in (PIPELINE, FRESHNESS):
            assert a.severity == WARN, a.id


def test_values_block_except_documented_warn():
    warn_values = {a.id for a in ASSERTIONS if a.family == VALUES and a.severity == WARN}
    # The only intentional warn in the value family is the accounting identity.
    assert warn_values == {"values.accounting_identity"}


def test_engine_matches_family():
    # Graph families use cypher; events families use sql.
    for a in ASSERTIONS:
        if a.family in (KEYS, REFS, VALUES, GOLDEN, COVERAGE, ORACLE):
            assert a.engine == "cypher", a.id
        else:
            assert a.engine == "sql", a.id


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
            "detail": "", "found": 1000}


def _all_clean_sql(_q):
    return {"violations": 0, "lag": 0, "dl": 0, "detail": "", "found": 1000}


def test_run_catalog_all_pass():
    results = run_catalog(_all_clean_cypher, _all_clean_sql)
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
    results = run_catalog(_all_clean_cypher, _all_clean_sql)
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
