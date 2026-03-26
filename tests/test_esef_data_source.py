"""
Unit tests for EsefDataSource.
Uses a temporary directory with hand-crafted JSON fixtures.
No network calls.
"""
from __future__ import annotations
# pylint: disable=missing-function-docstring,redefined-outer-name

import json
import math
from pathlib import Path

import pandas as pd
import pytest

from src.data.europe.esef_data_source import EsefDataSource


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def esef_dir(tmp_path: Path) -> Path:
    """Populate a minimal ESEF output directory."""
    summaries_dir = tmp_path / "summaries"
    summaries_dir.mkdir()

    registry = {
        "ASML.AS": {
            "lei": "LEIASML0000000000000",
            "ticker": "ASML.AS",
            "symbol": "ASML",
            "exchange": "AS",
            "name": "ASML Holding N.V.",
            "country": "NL",
            "source": "esef",
        },
        "SAP.DE": {
            "lei": "LEISAP00000000000000",
            "ticker": "SAP.DE",
            "symbol": "SAP",
            "exchange": "DE",
            "name": "SAP SE",
            "country": "DE",
            "source": "esef",
        },
    }
    (tmp_path / "eu_entities.json").write_text(
        json.dumps(registry, ensure_ascii=False), encoding="utf-8"
    )

    asml_summary = {
        "lei": "LEIASML0000000000000",
        "ticker": "ASML.AS",
        "symbol": "ASML",
        "exchange": "AS",
        "name": "ASML Holding N.V.",
        "country": "NL",
        "source": "esef",
        "updated_at": "2024-01-01T00:00:00+00:00",
        "filings": [
            {
                "year": 2023,
                "filing_date": "2023-12-31",
                "filing_index": "idx_2023",
                "revenue": 27_600_000_000,
                "net_income": 7_800_000_000,
                "total_assets": 30_000_000_000,
                "total_liabilities": 12_000_000_000,
                "equity": 18_000_000_000,
                "operating_cashflow": 9_000_000_000,
                "capex": -2_000_000_000,
                "free_cashflow": 7_000_000_000,
                "current_assets": 8_000_000_000,
                "current_liabilities": 4_000_000_000,
                "inventory": 800_000_000,
                "prepaid_expenses": None,
                "shares_outstanding": 400_000_000,
                "eps": 19.5,
                "long_term_debt": 5_000_000_000,
                "cash_and_equivalents": 4_000_000_000,
                "depreciation_amortization": 600_000_000,
                "interest_expense": 200_000_000,
                "income_tax_expense": 1_500_000_000,
            },
            {
                "year": 2022,
                "filing_date": "2022-12-31",
                "filing_index": "idx_2022",
                "revenue": 21_200_000_000,
                "net_income": 5_600_000_000,
                "total_assets": 24_000_000_000,
                "total_liabilities": 10_000_000_000,
                "equity": 14_000_000_000,
                "operating_cashflow": 7_500_000_000,
                "capex": -1_800_000_000,
                "free_cashflow": 5_700_000_000,
                "current_assets": 7_000_000_000,
                "current_liabilities": 3_500_000_000,
                "inventory": 700_000_000,
                "prepaid_expenses": None,
                "shares_outstanding": 402_000_000,
                "eps": 13.9,
                "long_term_debt": 4_500_000_000,
                "cash_and_equivalents": 3_500_000_000,
                "depreciation_amortization": 550_000_000,
                "interest_expense": 180_000_000,
                "income_tax_expense": 1_100_000_000,
            },
        ],
    }
    (summaries_dir / "ASML.AS.json").write_text(
        json.dumps(asml_summary, ensure_ascii=False), encoding="utf-8"
    )

    return tmp_path


@pytest.fixture()
def ds(esef_dir: Path) -> EsefDataSource:
    return EsefDataSource(esef_data_dir=str(esef_dir))


# ---------------------------------------------------------------------------
# get_annual_fundamentals
# ---------------------------------------------------------------------------

def test_fundamentals_returns_all_keys(ds: EsefDataSource):
    result = ds.get_annual_fundamentals("ASML.AS", years=10)
    expected_keys = {
        "revenue", "net_income", "total_assets", "total_liabilities", "equity",
        "operating_cashflow", "capex", "free_cashflow", "current_assets",
        "current_liabilities", "inventory", "prepaid_expenses", "shares_outstanding",
        "eps", "long_term_debt", "cash_and_equivalents",
        "depreciation_amortization", "interest_expense", "income_tax_expense",
    }
    assert expected_keys.issubset(result.keys())


def test_fundamentals_revenue_values(ds: EsefDataSource):
    result = ds.get_annual_fundamentals("ASML.AS", years=10)
    rev = result["revenue"]
    assert isinstance(rev, pd.Series)
    assert rev[2023] == 27_600_000_000
    assert rev[2022] == 21_200_000_000


def test_fundamentals_sorted_descending(ds: EsefDataSource):
    result = ds.get_annual_fundamentals("ASML.AS", years=10)
    years = list(result["revenue"].index)
    assert years == sorted(years, reverse=True)


def test_fundamentals_years_limit(ds: EsefDataSource):
    result = ds.get_annual_fundamentals("ASML.AS", years=1)
    assert len(result["revenue"]) == 1
    assert list(result["revenue"].index) == [2023]


def test_fundamentals_none_for_missing_field(ds: EsefDataSource):
    result = ds.get_annual_fundamentals("ASML.AS", years=10)
    # prepaid_expenses is None in both filings
    prepaid = result["prepaid_expenses"]
    assert prepaid[2023] is None


def test_fundamentals_unknown_ticker_returns_empty(ds: EsefDataSource):
    result = ds.get_annual_fundamentals("UNKNOWN.XX", years=10)
    assert all(s.empty for s in result.values())


# ---------------------------------------------------------------------------
# Price stubs
# ---------------------------------------------------------------------------

def test_annual_avg_prices_empty(ds: EsefDataSource):
    prices = ds.get_annual_avg_prices("ASML.AS", years=5)
    assert isinstance(prices, pd.Series)
    assert prices.empty


def test_annual_dividends_empty(ds: EsefDataSource):
    divs = ds.get_annual_dividends("ASML.AS")
    assert isinstance(divs, pd.Series)
    assert divs.empty


def test_price_history_empty_df(ds: EsefDataSource):
    df = ds.get_price_history("ASML.AS", period="1y")
    assert isinstance(df, pd.DataFrame)
    assert df.empty


def test_market_snapshot_stub(ds: EsefDataSource):
    snap = ds.get_market_snapshot("ASML.AS")
    assert snap.current_price is None
    assert snap.avg_volume is None
    assert isinstance(snap.splits, pd.Series)


# ---------------------------------------------------------------------------
# Ticker discovery
# ---------------------------------------------------------------------------

def test_get_available_tickers_returns_list(ds: EsefDataSource):
    tickers = ds.get_available_tickers()
    assert isinstance(tickers, list)
    assert len(tickers) == 2


def test_get_available_tickers_has_search_fields(ds: EsefDataSource):
    tickers = ds.get_available_tickers()
    for t in tickers:
        assert "search_name" in t
        assert "search_keywords" in t
        assert "data_source" in t
        assert t["data_source"] == "esef"


def test_search_tickers_by_symbol(ds: EsefDataSource):
    results = ds.search_tickers("ASML", limit=10)
    assert len(results) == 1
    assert results[0]["symbol"] == "ASML"


def test_search_tickers_by_name(ds: EsefDataSource):
    results = ds.search_tickers("SAP", limit=10)
    assert any(r["symbol"] == "SAP" for r in results)


def test_search_tickers_no_match(ds: EsefDataSource):
    results = ds.search_tickers("XYZNOTEXIST", limit=10)
    assert results == []


def test_search_tickers_empty_query_returns_up_to_limit(ds: EsefDataSource):
    results = ds.search_tickers("", limit=1)
    assert len(results) == 1


def test_search_tickers_limit_respected(ds: EsefDataSource):
    results = ds.search_tickers("", limit=1)
    assert len(results) <= 1


# ---------------------------------------------------------------------------
# Missing registry
# ---------------------------------------------------------------------------

def test_missing_registry_returns_empty(tmp_path: Path):
    ds_empty = EsefDataSource(esef_data_dir=str(tmp_path))
    assert not ds_empty.get_available_tickers()


def test_missing_registry_search_returns_empty(tmp_path: Path):
    ds_empty = EsefDataSource(esef_data_dir=str(tmp_path))
    assert not ds_empty.search_tickers("ASML", limit=5)


# ---------------------------------------------------------------------------
# Numeric integrity
# ---------------------------------------------------------------------------

def test_fundamentals_no_nan_for_known_fields(ds: EsefDataSource):
    result = ds.get_annual_fundamentals("ASML.AS", years=10)
    for key in ("revenue", "net_income", "total_assets", "equity"):
        for val in result[key]:
            if val is not None:
                assert not (isinstance(val, float) and math.isnan(val))
