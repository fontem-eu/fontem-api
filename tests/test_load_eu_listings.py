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
            "isin": "NL0012969182",
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
            "ticker": "BRIT", "exchange": "LSE", "isin": "GB0030913577",
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
    # Normalised on the way out — this asserted "FR" before, which
    # pinned the alpha-2 drift rather than the fallback this test is
    # actually about.
    assert payload["country"] == "FRA"
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


def test_emit_financials_one_upsert_per_filing(tmp_path: Path):
    summaries_dir = _make_summaries(
        tmp_path, lei="724500973ODKK3IFQ447", years=[2022, 2023],
    )
    _log, emit = _mock_log()
    n = emit_financials(emit, summaries_dir)
    assert n == 2
    # No Begin/End — only UpsertFiling events.
    assert emit.control.call_count == 0
    assert emit.upsert.call_count == 2
    assert all(c.args[0] == "UpsertFiling" for c in emit.upsert.call_args_list)


def test_emit_financials_noop_when_dir_missing(tmp_path: Path):
    """Missing summaries dir → no events emitted, not a crash."""
    _log, emit = _mock_log()
    n = emit_financials(emit, tmp_path / "does-not-exist")
    assert n == 0
    assert emit.control.call_count == 0
    assert emit.upsert.call_count == 0


def test_emit_financials_skips_summary_with_invalid_lei(tmp_path: Path):
    summaries_dir = tmp_path / "summaries"
    summaries_dir.mkdir()
    (summaries_dir / "broken.json").write_text(
        json.dumps({"lei": "TOOSHORT", "filings": [{"year": 2023, "revenue": 1}]})
    )
    _log, emit = _mock_log()
    n = emit_financials(emit, summaries_dir)
    assert n == 0
    assert emit.control.call_count == 0
    assert emit.upsert.call_count == 0


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
            "ticker": "ADYEN", "exchange": "AMS", "isin": "NL0012969182",
        },
    }
    (esef_dir / "eu_entities.json").write_text(json.dumps(entities))
    _make_summaries(esef_dir, lei="724500973ODKK3IFQ447", years=[2023])

    log, emit = _mock_log()
    res = load_eu_listings(log, esef_dir)
    assert res == {"companies": 1, "listings": 1, "filings": 1}
    # Exactly one batch context for the entire run.
    assert log.batch.call_count == 1
    # No control events — only upserts (Company, Listing, Filing).
    assert emit.control.call_count == 0


def test_emit_listings_skips_ticker_without_isin():
    """Pin the new contract: an entity carrying a ticker but no ISIN
    must NOT produce an ``UpsertListing``. Such Listings used to land
    as suspect (``isin=NULL`` on EU exchanges, ticker shape matching
    the legacy fabricator), and the OpenFIGI ``lei-reeval`` pass had
    to retire them on every cron run. The ``Company`` row still gets
    emitted so a later ISIN-carrying loader (OpenFIGI ``lei`` mode
    via the GLEIF bulk file) can attach the canonical Listing."""
    _log, emit = _mock_log()
    entities = {
        "ABC": {
            "lei": "LEI" + "0" * 17,
            "name": "Some Holding",
            "country": "DE",
            "ticker": "ABC",
            "exchange": "DE",
            "isin": None,        # ← the previously-buggy case
        },
    }
    companies, listings = emit_listings(emit, entities)
    assert companies == 1, "Company must still be emitted"
    assert listings == 0, (
        "Listing must NOT be emitted without an ISIN"
    )
    # Sanity: only the UpsertCompany call landed, no UpsertListing.
    call_types = [c.args[0] for c in emit.upsert.call_args_list]
    assert call_types == ["UpsertCompany"]


def test_emit_listings_isin_lands_on_the_payload():
    """When ISIN IS present, it must be passed through to the
    ``UpsertListing`` payload so the consolidator can key the
    Listing by ISIN downstream and ``lei-reeval`` doesn't flag it
    as a suspect ticker."""
    _log, emit = _mock_log()
    entities = {
        "ADYEN": {
            "lei": "724500973ODKK3IFQ447",
            "name": "Adyen N.V.",
            "country": "NL",
            "ticker": "ADYEN",
            "exchange": "AS",
            "isin": "NL0012969182",
        },
    }
    emit_listings(emit, entities)
    listing_call = next(
        c for c in emit.upsert.call_args_list
        if c.args[0] == "UpsertListing"
    )
    payload = listing_call.kwargs["payload"]
    assert payload["isin"] == "NL0012969182"
    assert payload["ticker"] == "ADYEN"


def test_esef_plausible_filing_year_bounds():
    import datetime  # pylint: disable=import-outside-toplevel
    from src.etl.load_eu_listings import _plausible_filing_year  # pylint: disable=import-outside-toplevel
    now = datetime.date.today().year
    assert _plausible_filing_year(2021) is True
    assert _plausible_filing_year(now + 2) is False
    assert _plausible_filing_year(1989) is False


# ── country codes are alpha-3 on the way out ─────────────────────────
#
# The graph's convention is alpha-3 everywhere — Authority, Contract,
# Lobbyist and the NUTS links all use it, and load_gleif normalises on
# write. This loader passed the upstream alpha-2 straight through, which
# made it the live source of a drift that three separate backfill
# scripts have been written to undo (backfill_country_alpha3,
# normalize_countries, normalize_country_codes) and which was still
# producing new alpha-2 rows the day this was fixed. A country code that
# does not match the convention silently misses every country join.

def _emitted_countries(entities):
    _log, emit = _mock_log()
    emit_listings(emit, entities)
    return [
        c.kwargs["payload"].get("country")
        for c in emit.upsert.call_args_list
        if c.args[0] == "UpsertCompany"
    ]


def test_alpha2_country_is_normalised_to_alpha3():
    assert _emitted_countries({
        "A": {"lei": "724500973ODKK3IFQ447", "name": "Adyen N.V.", "country": "NL"},
    }) == ["NLD"]


def test_alpha3_input_passes_through_unchanged():
    """Idempotent: the upstream feed may already be normalised, and a
    re-run must not mangle what it wrote last time."""
    assert _emitted_countries({
        "A": {"lei": "724500973ODKK3IFQ447", "name": "X", "country": "NLD"},
    }) == ["NLD"]


def test_an_unrecognised_country_becomes_null_not_junk():
    """Better an absent country than a code no join will match."""
    assert _emitted_countries({
        "A": {"lei": "724500973ODKK3IFQ447", "name": "X", "country": "ZZ"},
    }) == [None]
    assert _emitted_countries({
        "A": {"lei": "724500973ODKK3IFQ447", "name": "X"},
    }) == [None]
