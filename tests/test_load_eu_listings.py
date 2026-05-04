"""Tests for the EU listings and financials loader.

Listings still go to Neo4j (Companies haven't migrated). The
financials side switched to a Virtuoso-backed RdfFilingsWriter
in the FinancialYear cutover; the tests below mock that writer
so they don't need a live Virtuoso to run.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.etl.load_eu_listings import (
    COUNTRY_CURRENCY,
    load_financials,
    load_listings,
)


def _mock_driver(lei_to_gmr: dict[str, str] | None = None):
    """Mock Neo4j driver. When ``lei_to_gmr`` is supplied, the
    first session.run() returns those rows so the LEI→gmr_id
    index pre-pass succeeds.
    """
    driver = MagicMock()
    session = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)
    if lei_to_gmr is not None:
        rows = [
            {"lei": lei, "gmr_id": gid}
            for lei, gid in lei_to_gmr.items()
        ]
        session.run.return_value = iter(rows)
    return driver, session


def _mock_writer():
    """Stand-in for RdfFilingsWriter that records what it was
    asked to push and returns the standard WriteResult shape."""
    captured: list[dict] = []

    def _write(records):
        records = list(records)
        captured.extend(records)
        return SimpleNamespace(written=len(records),
                               triples_pushed=len(records) * 4)

    writer = MagicMock()
    writer.write.side_effect = _write
    writer._captured = captured
    return writer


# ── load_listings ────────────────────────────────────────────────────

def test_listings_creates_constraint_and_merges():
    """Loader creates a Listing constraint then MERGEs each entity."""
    driver, session = _mock_driver()
    entities = {
        "ADYEN.AS": {
            "lei": "724500973ODKK3IFQ447",
            "ticker": "ADYEN.AS",
            "exchange": "AS",
            "name": "Adyen N.V.",
            "country": "NL",
        },
    }
    total = load_listings(driver, entities)
    assert total == 1
    calls = session.run.call_args_list
    assert "CONSTRAINT" in calls[0].args[0]
    assert "MERGE" in calls[1].args[0]


def test_listings_batches_multiple_entities():
    """Multiple entities are batched correctly."""
    driver, session = _mock_driver()
    entities = {
        f"T{i}.XX": {
            "lei": f"{'A' * 18}{i:02d}",
            "ticker": f"T{i}.XX",
            "exchange": "XX",
            "name": f"Co {i}",
            "country": "DE",
        }
        for i in range(3)
    }
    total = load_listings(driver, entities)
    assert total == 3


def test_listings_short_lei_uses_name_fallback():
    """Entities with non-standard LEIs fall back to name-based gmr_id."""
    driver, session = _mock_driver()
    entities = {
        "BAD.UA": {
            "lei": "12345",
            "ticker": "BAD.UA",
            "exchange": "PFTS",
            "name": "Bad Corp",
            "country": "UA",
        },
    }
    total = load_listings(driver, entities)
    assert total == 1
    batch = session.run.call_args_list[1].kwargs["batch"]
    assert batch[0]["lei"] is None  # short LEI not stored


def test_listings_derives_currency_from_country():
    """Currency is derived from the country code."""
    assert COUNTRY_CURRENCY["NL"] == "EUR"
    assert COUNTRY_CURRENCY["GB"] == "GBP"
    assert COUNTRY_CURRENCY["SE"] == "SEK"


# ── load_financials ──────────────────────────────────────────────────

_LEI = "7Z46009YA1DNEUQBVR42"
_GMR_ID = "00000000-1111-5111-9111-000000000001"


def test_financials_reads_summary_and_writes_filings(tmp_path):
    """Filings from summary JSON become Virtuoso-bound records
    keyed by the LEI→gmr_id index pulled from Neo4j first."""
    driver, _ = _mock_driver(lei_to_gmr={_LEI: _GMR_ID})
    writer = _mock_writer()
    summaries = tmp_path / "summaries"
    summaries.mkdir()
    (summaries / "ADYEN.AS.json").write_text(json.dumps({
        "lei": _LEI,
        "ticker": "ADYEN.AS",
        "filings": [
            {"year": 2024, "revenue": 2225601000.0, "net_income": 925163000.0,
             "filing_date": "2024-12-31"},
            {"year": 2023, "revenue": 1863406000.0, "net_income": 698322000.0,
             "filing_date": "2023-12-31"},
        ],
    }))

    written = load_financials(driver, summaries, writer)
    assert written == 2
    assert writer.write.called
    # gmr_id was resolved from the index, not echoed from the file.
    assert writer._captured[0]["gmr_id"] == _GMR_ID
    assert writer._captured[0]["year"] == 2024
    assert writer._captured[0]["revenue"] == 2225601000.0
    assert writer._captured[0]["filing_date"] == "2024-12-31"


def test_financials_skips_filing_without_year(tmp_path):
    """Filings missing a year field are skipped."""
    driver, _ = _mock_driver(lei_to_gmr={_LEI: _GMR_ID})
    writer = _mock_writer()
    summaries = tmp_path / "summaries"
    summaries.mkdir()
    (summaries / "BAD.XX.json").write_text(json.dumps({
        "lei": _LEI,
        "ticker": "BAD.XX",
        "filings": [{"revenue": 100}],
    }))

    written = load_financials(driver, summaries, writer)
    assert written == 0
    # Empty batch — writer never called.
    assert writer.write.call_count == 0


def test_financials_handles_missing_dir(tmp_path):
    """Returns 0 when summaries directory doesn't exist."""
    driver, _ = _mock_driver(lei_to_gmr={})
    writer = _mock_writer()
    total = load_financials(driver, tmp_path / "nonexistent", writer)
    assert total == 0


def test_financials_skips_bad_json(tmp_path):
    """Malformed JSON files are skipped without crashing."""
    driver, _ = _mock_driver(lei_to_gmr={_LEI: _GMR_ID})
    writer = _mock_writer()
    summaries = tmp_path / "summaries"
    summaries.mkdir()
    (summaries / "BAD.XX.json").write_text("not json{{{")

    written = load_financials(driver, summaries, writer)
    assert written == 0
    assert writer.write.call_count == 0


def test_financials_skips_company_without_resolvable_lei(tmp_path):
    """If the LEI isn't in the Neo4j Company index, the filings
    for that company are dropped — no orphan Filings."""
    driver, _ = _mock_driver(lei_to_gmr={})  # empty index
    writer = _mock_writer()
    summaries = tmp_path / "summaries"
    summaries.mkdir()
    (summaries / "X.json").write_text(json.dumps({
        "lei": _LEI,
        "filings": [{"year": 2024, "revenue": 100}],
    }))

    written = load_financials(driver, summaries, writer)
    assert written == 0
