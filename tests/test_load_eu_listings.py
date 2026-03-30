"""Tests for the EU listings and financials loader."""
import json
from unittest.mock import MagicMock

from src.etl.load_eu_listings import (
    COUNTRY_CURRENCY,
    load_financials,
    load_listings,
)


def _mock_driver():
    """Create a mock Neo4j driver with a usable session context manager."""
    driver = MagicMock()
    session = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return driver, session


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

def test_financials_reads_summary_and_merges(tmp_path):
    """Filings from summary JSON become MERGE calls with correct fields."""
    driver, session = _mock_driver()
    summaries = tmp_path / "summaries"
    summaries.mkdir()
    (summaries / "ADYEN.AS.json").write_text(json.dumps({
        "ticker": "ADYEN.AS",
        "filings": [
            {"year": 2024, "revenue": 2225601000.0, "net_income": 925163000.0,
             "filing_date": "2024-12-31"},
            {"year": 2023, "revenue": 1863406000.0, "net_income": 698322000.0,
             "filing_date": "2023-12-31"},
        ],
    }))

    total = load_financials(driver, summaries.parent / "summaries")
    assert total == 2

    merge_call = session.run.call_args_list[0]
    assert "FinancialYear" in merge_call.args[0]
    batch = merge_call.kwargs["batch"]
    assert batch[0]["ticker"] == "ADYEN.AS"
    assert batch[0]["year"] == 2024
    assert batch[0]["revenue"] == 2225601000.0


def test_financials_skips_filing_without_year(tmp_path):
    """Filings missing a year field are skipped."""
    driver, session = _mock_driver()
    summaries = tmp_path / "summaries"
    summaries.mkdir()
    (summaries / "BAD.XX.json").write_text(json.dumps({
        "ticker": "BAD.XX",
        "filings": [{"revenue": 100}],
    }))

    total = load_financials(driver, summaries.parent / "summaries")
    assert total == 0


def test_financials_handles_missing_dir(tmp_path):
    """Returns 0 when summaries directory doesn't exist."""
    driver, _ = _mock_driver()
    total = load_financials(driver, tmp_path / "nonexistent")
    assert total == 0


def test_financials_skips_bad_json(tmp_path):
    """Malformed JSON files are skipped without crashing."""
    driver, session = _mock_driver()
    summaries = tmp_path / "summaries"
    summaries.mkdir()
    (summaries / "BAD.XX.json").write_text("not json{{{")

    total = load_financials(driver, summaries.parent / "summaries")
    assert total == 0
