"""Tests for the graph → Yahoo price-universe exporter."""
import json
from collections import Counter
from unittest.mock import MagicMock

from src.etl import export_price_universe as ex
from src.etl.export_price_universe import _suffix_for, pick_symbol


def test_suffix_prefers_mic_over_exchange_code():
    # MIC is definitive; exchange code only fills in when MIC is absent.
    assert _suffix_for({"mic": "XPAR", "exchange": "LN"}) == "PA"
    assert _suffix_for({"mic": None, "exchange": "LN"}) == "L"
    assert _suffix_for({"mic": "", "exchange": "US"}) == ""


def test_unknown_venue_returns_none_and_is_counted():
    unmapped = Counter()
    got = pick_symbol(
        [{"ticker": "ABC", "exchange": "ZZ", "mic": None}], unmapped)
    assert got is None
    assert unmapped == {"ZZ": 1}


def test_pick_symbol_prefers_main_market_over_german_regional():
    listings = [
        {"ticker": "SAP", "mic": "STUB", "exchange": None},   # Stuttgart tape
        {"ticker": "SAP", "mic": "XPAR", "exchange": None},   # main market
    ]
    assert pick_symbol(listings, Counter()) == "SAP.PA"


def test_pick_symbol_falls_back_to_regional_when_alone():
    listings = [{"ticker": "SAP", "mic": "MUND", "exchange": None}]
    assert pick_symbol(listings, Counter()) == "SAP.MU"


def test_us_share_class_dots_become_dashes():
    listings = [{"ticker": "BRK.B", "exchange": "US", "mic": None}]
    assert pick_symbol(listings, Counter()) == "BRK-B"


def _driver_with_rows(rows):
    """Fake driver whose session.run yields dict-convertible records."""
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    session.run.return_value = [dict(r) for r in rows]
    return driver


def test_export_orders_contract_winners_first_and_dedupes(tmp_path):
    rows = [
        {"gmr_id": "g-late", "name": "NoContracts", "country": "FR",
         "is_fund": False, "has_contracts": False,
         "listings": [{"ticker": "AAA", "mic": "XPAR", "exchange": None}]},
        {"gmr_id": "g-first", "name": "Winner", "country": "FR",
         "is_fund": False, "has_contracts": True,
         "listings": [{"ticker": "WIN", "mic": "XPAR", "exchange": None}]},
        {"gmr_id": "g-dup", "name": "SameSymbol", "country": "FR",
         "is_fund": False, "has_contracts": False,
         "listings": [{"ticker": "AAA", "mic": "XPAR", "exchange": None}]},
        {"gmr_id": "g-fund", "name": "A Fund", "country": "LU",
         "is_fund": True, "has_contracts": False,
         "listings": [{"ticker": "FND", "mic": "XLUX", "exchange": None}]},
        {"gmr_id": "g-skip", "name": "Unmappable", "country": "XX",
         "is_fund": False, "has_contracts": False,
         "listings": [{"ticker": "ZZZ", "exchange": "ZZ", "mic": None}]},
    ]
    out_path = tmp_path / "universe.json"
    summary = ex.export_universe(_driver_with_rows(rows), str(out_path))

    data = json.loads(out_path.read_text())
    # contract winner ordered first (fetcher starts NEW tickers in order)
    assert list(data)[0] == "g-first"
    # duplicate symbol exported once (tiebreak by gmr_id decides which
    # entity keeps it); unmappable entity skipped
    assert ("g-dup" in data) != ("g-late" in data)
    assert "g-skip" not in data
    assert data["g-fund"]["ticker"] == "FND.LU"
    assert summary["symbols"] == 3
    assert summary["funds"] == 1
    assert summary["skipped_no_mappable_venue"] == 1
    assert summary["unmapped_venues"] == {"ZZ": 1}


def test_export_output_is_fetcher_compatible(tmp_path):
    """The fetcher's load_eu_tickers reads {key: {"ticker": ...}} and
    skips null tickers — our shape must satisfy that contract."""
    rows = [{"gmr_id": "g1", "name": "X", "country": "FR",
             "is_fund": False, "has_contracts": False,
             "listings": [{"ticker": "ABC", "mic": "XPAR",
                           "exchange": None}]}]
    out_path = tmp_path / "u.json"
    ex.export_universe(_driver_with_rows(rows), str(out_path))
    data = json.loads(out_path.read_text())
    for entry in data.values():
        assert isinstance(entry, dict) and entry.get("ticker")
