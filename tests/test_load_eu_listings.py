"""Tests for the EU listings + ESEF financials loader.

Post-event-log: the loader emits one event-log batch covering
both listing/company upserts and a Begin/End-bracketed run of
UpsertFiling events for ESEF.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock

from src.etl.load_eu_listings import (
    COUNTRY_CURRENCY,
    ESEF_FINANCIALS_GRAPH,
    _filing_uuid,
    emit_financials,
    emit_listings,
    load_eu_listings,
)


def _mock_log():
    log = MagicMock()
    emit = MagicMock()
    log.batch.return_value.__enter__ = MagicMock(return_value=emit)
    log.batch.return_value.__exit__ = MagicMock(return_value=False)
    return log, emit


# ── emit_listings ────────────────────────────────────────────────────

def test_emit_listings_companies_and_listings():
    _log, emit = _mock_log()
    entities = {
        "ADYEN": {
            "lei": "724500973ODKK3IFQ447",
            "name": "Adyen N.V.",
            "country": "NL",
            "ticker": "ADYEN",
            "exchange": "AMS",
        },
        "NOTICKER": {
            "lei": "5493006IQ6OL2D9TZD89",
            "name": "Some Holding",
            "country": "NL",
            "ticker": None,
        },
    }
    companies, listings = emit_listings(emit, entities)
    assert companies == 2
    assert listings == 1
    types = [c.args[0] for c in emit.upsert.call_args_list]
    # Adyen company, Adyen listing, NoTicker company.
    assert types == ["UpsertCompany", "UpsertListing", "UpsertCompany"]


def test_emit_listings_currency_from_country():
    _log, emit = _mock_log()
    entities = {
        "GB1": {
            "lei": "724500973ODKK3IFQ44A",
            "name": "Brit Plc", "country": "GB",
            "ticker": "BRIT", "exchange": "LSE",
        },
    }
    emit_listings(emit, entities)
    listing_payload = emit.upsert.call_args_list[1].kwargs["payload"]
    assert listing_payload["currency"] == COUNTRY_CURRENCY["GB"] == "GBP"


def test_emit_listings_falls_back_to_name_country_when_no_lei():
    """No-LEI entries get a deterministic gmr_id from name+country
    so re-runs land on the same Company."""
    _log, emit = _mock_log()
    entities = {
        "X": {"lei": "", "name": "No-LEI Co", "country": "FR"},
    }
    emit_listings(emit, entities)
    payload = emit.upsert.call_args.kwargs["payload"]
    assert payload["country"] == "FR"
    assert "lei" not in payload  # builder drops Nones
    assert payload["gmr_id"]


# ── emit_financials ──────────────────────────────────────────────────

def _make_summaries(tmp_path: Path, *, lei: str, years: list[int]) -> Path:
    summaries_dir = tmp_path / "summaries"
    summaries_dir.mkdir()
    doc = {
        "lei": lei,
        "filings": [
            {"year": y, "revenue": 1000 * y, "net_income": 50 * y}
            for y in years
        ],
    }
    (summaries_dir / f"{lei}.json").write_text(json.dumps(doc))
    return summaries_dir


def test_emit_financials_brackets_with_begin_and_end(tmp_path: Path):
    summaries_dir = _make_summaries(
        tmp_path, lei="724500973ODKK3IFQ447", years=[2022, 2023],
    )
    _log, emit = _mock_log()
    n = emit_financials(emit, summaries_dir)
    assert n == 2
    assert emit.control.call_count == 2
    begin = emit.control.call_args_list[0]
    end = emit.control.call_args_list[1]
    assert begin.args[0] == "BeginGraphReplace"
    assert end.args[0] == "EndGraphReplace"
    assert begin.args[1]["graph_iri"] == ESEF_FINANCIALS_GRAPH


def test_emit_financials_brackets_even_when_dir_missing(tmp_path: Path):
    """Missing summaries dir still emits Begin/End so the ESEF graph
    becomes empty rather than retaining stale Filings."""
    _log, emit = _mock_log()
    n = emit_financials(emit, tmp_path / "does-not-exist")
    assert n == 0
    assert emit.control.call_count == 2


def test_emit_financials_skips_summary_with_invalid_lei(tmp_path: Path):
    summaries_dir = tmp_path / "summaries"
    summaries_dir.mkdir()
    (summaries_dir / "broken.json").write_text(
        json.dumps({"lei": "TOOSHORT", "filings": [{"year": 2023, "revenue": 1}]})
    )
    _log, emit = _mock_log()
    n = emit_financials(emit, summaries_dir)
    assert n == 0
    # Bracket still emits.
    assert emit.control.call_count == 2


def test_filing_iri_deterministic():
    a = _filing_uuid("abc-123", 2023, "esef")
    b = _filing_uuid("abc-123", 2023, "esef")
    assert a == b


# ── load_eu_listings (end-to-end) ────────────────────────────────────

def test_load_eu_listings_one_batch_for_listings_and_filings(tmp_path: Path):
    """Listings and filings share a single log.batch() context — one
    event-log transaction, all-or-nothing."""
    esef_dir = tmp_path / "esef"
    esef_dir.mkdir()
    entities = {
        "X": {
            "lei": "724500973ODKK3IFQ447",
            "name": "Adyen N.V.", "country": "NL",
            "ticker": "ADYEN", "exchange": "AMS",
        },
    }
    (esef_dir / "eu_entities.json").write_text(json.dumps(entities))
    _make_summaries(esef_dir, lei="724500973ODKK3IFQ447", years=[2023])

    log, emit = _mock_log()
    res = load_eu_listings(log, esef_dir)
    assert res == {"companies": 1, "listings": 1, "filings": 1}
    # Exactly one batch context for the entire run.
    assert log.batch.call_count == 1
    # Begin + End around the financials block.
    assert emit.control.call_count == 2
