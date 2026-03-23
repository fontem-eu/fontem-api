"""
Unit tests for LocalPriceFetcher
==================================
Uses committed fixture CSVs under tests/fixtures/prices/daily/ so that these
tests run without any network access or NFS volume.

Fixture tickers:
  AAPL — 24 monthly rows spanning 2023-01 to 2024-12
  MSFT — 24 monthly rows spanning 2023-01 to 2024-12
"""
from __future__ import annotations
# pylint: disable=missing-function-docstring,redefined-outer-name,protected-access

from pathlib import Path

import pandas as pd
import pytest

from src.data.local_price_fetcher import LocalPriceFetcher

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "prices"


@pytest.fixture(scope="module")
def fetcher():
    return LocalPriceFetcher(price_data_dir=str(_FIXTURES_DIR))


# ---------------------------------------------------------------------------
# _has_local / _load_history
# ---------------------------------------------------------------------------

def test_has_local_aapl(fetcher):
    assert fetcher._has_local("AAPL") is True


def test_has_local_msft(fetcher):
    assert fetcher._has_local("MSFT") is True


def test_has_local_unknown_ticker(fetcher):
    assert fetcher._has_local("ZZZNOTREAL") is False


def test_load_history_returns_dataframe(fetcher):
    df = fetcher._load_history("AAPL")
    assert df is not None
    assert isinstance(df, pd.DataFrame)
    assert "Close" in df.columns
    assert len(df) == 24


def test_load_history_index_is_datetime(fetcher):
    df = fetcher._load_history("AAPL")
    assert pd.api.types.is_datetime64_any_dtype(df.index)


def test_load_history_sorted_ascending(fetcher):
    df = fetcher._load_history("AAPL")
    assert df.index.is_monotonic_increasing


def test_load_history_missing_ticker_returns_none(fetcher):
    assert fetcher._load_history("ZZZNOTREAL") is None


# ---------------------------------------------------------------------------
# get_current_price
# ---------------------------------------------------------------------------

def test_get_current_price_aapl(fetcher):
    price = fetcher.get_current_price("AAPL")
    assert isinstance(price, float)
    assert price > 0
    # Last row of fixture: 2024-12-02, Close=247.96
    assert abs(price - 247.96) < 0.01


def test_get_current_price_msft(fetcher):
    price = fetcher.get_current_price("MSFT")
    assert isinstance(price, float)
    # Last row of fixture: 2024-12-02, Close=381.87
    assert abs(price - 381.87) < 0.01


def test_get_current_price_unknown_returns_zero(fetcher):
    assert fetcher.get_current_price("ZZZNOTREAL") == 0.0


# ---------------------------------------------------------------------------
# get_snapshot
# ---------------------------------------------------------------------------

def test_get_snapshot_aapl_has_current_price(fetcher):
    assert fetcher.get_snapshot("AAPL")["current_price"] > 0


def test_get_snapshot_aapl_price_matches_last_close(fetcher):
    assert abs(fetcher.get_snapshot("AAPL")["current_price"] - 247.96) < 0.01


def test_get_snapshot_has_52_week_range(fetcher):
    snap = fetcher.get_snapshot("AAPL")
    assert snap["week_52_high"] is not None
    assert snap["week_52_low"] is not None
    assert snap["week_52_high"] >= snap["week_52_low"]


def test_get_snapshot_high_low_sensible(fetcher):
    snap = fetcher.get_snapshot("AAPL")
    assert snap["week_52_high"] > 100
    assert snap["week_52_low"] > 0


def test_get_snapshot_avg_volume_positive(fetcher):
    assert fetcher.get_snapshot("AAPL")["avg_volume"] > 0


def test_get_snapshot_unavailable_fields_are_none(fetcher):
    snap = fetcher.get_snapshot("AAPL")
    assert snap["shares_outstanding"] is None
    assert snap["beta"] is None
    assert snap["market_cap"] is None


def test_get_snapshot_msft_price(fetcher):
    assert abs(fetcher.get_snapshot("MSFT")["current_price"] - 381.87) < 0.01


def test_get_snapshot_unknown_returns_zero_price(fetcher):
    snap = fetcher.get_snapshot("ZZZNOTREAL")
    assert snap["current_price"] == 0.0
    assert snap["week_52_high"] is None
    assert snap["week_52_low"] is None


# ---------------------------------------------------------------------------
# get_annual_avg_prices
# ---------------------------------------------------------------------------

def test_annual_avg_prices_aapl_returns_series(fetcher):
    prices = fetcher.get_annual_avg_prices("AAPL", period="3y")
    assert isinstance(prices, pd.Series)
    assert len(prices) > 0


def test_annual_avg_prices_aapl_covers_both_years(fetcher):
    prices = fetcher.get_annual_avg_prices("AAPL", period="3y")
    assert 2023 in prices.index
    assert 2024 in prices.index


def test_annual_avg_prices_aapl_descending_index(fetcher):
    assert fetcher.get_annual_avg_prices("AAPL", period="3y").index.is_monotonic_decreasing


def test_annual_avg_prices_aapl_positive(fetcher):
    assert all(p > 0 for p in fetcher.get_annual_avg_prices("AAPL", period="3y"))


def test_annual_avg_prices_msft_positive(fetcher):
    assert all(p > 0 for p in fetcher.get_annual_avg_prices("MSFT", period="3y"))


def test_annual_avg_prices_unknown_returns_empty(fetcher):
    result = fetcher.get_annual_avg_prices("ZZZNOTREAL", period="3y")
    assert isinstance(result, pd.Series)
    assert result.empty


# ---------------------------------------------------------------------------
# get_annual_dividends — always returns empty Series, no network
# ---------------------------------------------------------------------------

def test_annual_dividends_returns_empty_series(fetcher):
    divs = fetcher.get_annual_dividends("AAPL")
    assert isinstance(divs, pd.Series)
    assert divs.empty


def test_annual_dividends_unknown_ticker_returns_empty(fetcher):
    assert fetcher.get_annual_dividends("ZZZNOTREAL").empty


# ---------------------------------------------------------------------------
# get_history
# ---------------------------------------------------------------------------

def test_get_history_aapl_returns_ohlcv(fetcher):
    df = fetcher.get_history("AAPL", period="3y")
    assert not df.empty
    for col in ("Open", "High", "Low", "Close", "Volume"):
        assert col in df.columns


def test_get_history_period_filter(fetcher):
    df_1y = fetcher.get_history("AAPL", period="1y")
    df_3y = fetcher.get_history("AAPL", period="3y")
    assert len(df_1y) <= len(df_3y)


def test_get_history_unknown_returns_empty_dataframe(fetcher):
    df = fetcher.get_history("ZZZNOTREAL", period="3y")
    assert isinstance(df, pd.DataFrame)
    assert df.empty
