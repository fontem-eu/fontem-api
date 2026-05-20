"""Regression tests for the event-log CDP loader.

The fuzzy match path was removed long ago — see the prior
load_cdp comment block. This test file pins the new event-emit
shape and re-asserts the no-fuzzy-matching invariant.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.etl import load_cdp
from src.etl.load_cdp import (
    RESOLVE_COMPANY,
    _disclosure_id,
    emit_disclosure_events,
)


def _mock_log():
    log = MagicMock()
    emit = MagicMock()
    log.batch.return_value.__enter__ = MagicMock(return_value=emit)
    log.batch.return_value.__exit__ = MagicMock(return_value=False)
    return log, emit


def test_no_fuzzy_match_cypher_exists():
    """The fuzzy path was the source of the misattribution risk. It
    must not exist as a callable cypher anywhere in this module."""
    assert not hasattr(load_cdp, "MATCH_COMPANY_FUZZY"), (
        "MATCH_COMPANY_FUZZY must remain removed."
    )
    assert not hasattr(load_cdp, "CREATE_FT_INDEX"), (
        "CDP loader must not create a fulltext index."
    )


def test_resolve_requires_non_empty_country():
    """Same-name companies in different jurisdictions must not get
    cross-pollinated CDP attribution. Empty/NULL country on the
    CDP row must short-circuit the match in Cypher."""
    assert "coalesce(row.country, '') <> ''" in RESOLVE_COMPANY


def test_resolve_uses_country_equality():
    assert "c.country = row.country" in RESOLVE_COMPANY


def test_disclosure_id_is_deterministic():
    rec = {"reporting_year": 2025, "company_name": "Foo", "country": "DEU"}
    assert _disclosure_id(rec) == _disclosure_id(rec)


def test_disclosure_id_changes_per_company():
    a = _disclosure_id({"reporting_year": 2025, "company_name": "Foo", "country": "DEU"})
    b = _disclosure_id({"reporting_year": 2025, "company_name": "Bar", "country": "DEU"})
    assert a != b


def test_emit_skips_records_without_company_match():
    log, emit = _mock_log()
    records = [
        {"company_name": "MatchedCo", "country": "DEU",
         "cdp_score": "A", "scope1_emissions": 1.0, "scope2_emissions": 2.0,
         "reporting_year": 2025},
        {"company_name": "UnmatchedCo", "country": "DEU",
         "cdp_score": "B", "scope1_emissions": None, "scope2_emissions": None,
         "reporting_year": 2025},
    ]
    company_index = {("MatchedCo", "DEU"): "00040372-dad6-5d34-882c-8b8624b4e734"}
    summary = emit_disclosure_events(log, records, company_index)
    assert summary["total"] == 2
    assert summary["emitted"] == 1
    assert summary["skipped"] == 1
    assert emit.upsert.call_count == 1
    payload = emit.upsert.call_args.kwargs["payload"]
    assert payload["system"] == "cdp"
    assert payload["company_gmr_id"] == "00040372-dad6-5d34-882c-8b8624b4e734"
    assert payload["details"]["cdp_score"] == "A"
    assert payload["details"]["scope1_emissions"] == 1.0


def test_emit_drops_null_detail_fields():
    """details should only carry non-null fields; the schema's
    additionalProperties:true accepts whatever, but down-stream
    queries are easier with no nulls."""
    log, emit = _mock_log()
    records = [{
        "company_name": "X", "country": "FR",
        "cdp_score": "", "scope1_emissions": None, "scope2_emissions": None,
        "reporting_year": 2024,
    }]
    company_index = {("X", "FR"): "00040372-dad6-5d34-882c-8b8624b4e734"}
    emit_disclosure_events(log, records, company_index)
    payload = emit.upsert.call_args.kwargs["payload"]
    # Empty score string + None scopes → no details at all.
    assert "details" not in payload or payload["details"] in (None, {})


def test_emit_no_records_skips_batch():
    log, _emit = _mock_log()
    summary = emit_disclosure_events(log, [], {})
    assert summary == {"total": 0, "emitted": 0, "skipped": 0}
    log.batch.assert_not_called()
