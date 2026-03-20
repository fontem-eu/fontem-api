"""
Unit tests for GMRLong — no network, no I/O, sub-second execution.

Mock data is based on realistic magnitudes:
  PASSING stock ("XYZ")  — small-cap value / dividend payer, all ratios pass.
  FAILING stock ("BIG")  — expensive mega-cap growth, high debt, low yield.

Expected ratio values (XYZ, identical across 5 years for easy arithmetic):
  EPS   = 100M / 60M      = 1.6̄    →  P/E  = 13 / 1.6̄  ≈  7.80
  BVPS  = 600M / 60M      = 10.00  →  P/B  = 13 / 10    =  1.30
  ROE   = 100M / 600M     = 16.7 %
  NPM   = 100M / 500M     = 20.0 %
  D/E   = 600M / 600M     =  1.00
  DivY  = 0.50 / 13 * 100 =  3.85 %
  QR    = (200M-30M-10M)/150M = 1.0̄7
"""
from __future__ import annotations
# pylint: disable=missing-function-docstring,redefined-outer-name,missing-class-docstring,multiple-statements,unused-argument

import math
import pytest
import pandas as pd

from src.analysis.gmr_data_source import GMRDataSource, GMRSettings
from src.analysis.gmr_long import GMRLong, GMRLongResult


# ---------------------------------------------------------------------------
# MockDataSource
# ---------------------------------------------------------------------------

class MockDataSource(GMRDataSource):
    def __init__(self, fundamentals, prices, dividends, snapshot):
        self._fundamentals = fundamentals
        self._prices = prices
        self._dividends = dividends
        self._snapshot = snapshot

    def get_annual_fundamentals(self, ticker, years):  return self._fundamentals
    def get_annual_avg_prices(self, ticker, years):    return self._prices
    def get_annual_dividends(self, ticker):            return self._dividends
    def get_price_history(self, ticker, period="1y"):  return pd.DataFrame()
    def get_market_snapshot(self, ticker):             return self._snapshot


# ---------------------------------------------------------------------------
# Helpers — build realistic fixed annual series
# ---------------------------------------------------------------------------

YEARS = [2024, 2023, 2022, 2021, 2020]


def _series(values: list) -> pd.Series:
    return pd.Series(dict(zip(YEARS, values)))


def _passing_fundamentals() -> dict:
    """XYZ Corp — small-cap value stock that passes all GMR thresholds."""
    n = len(YEARS)
    return {
        "revenue":             _series([500e6] * n),
        "net_income":          _series([100e6] * n),
        "equity":              _series([600e6] * n),
        "total_liabilities":   _series([600e6] * n),   # D/E = 1.0
        "shares_outstanding":  _series([60e6]  * n),
        "current_assets":      _series([200e6] * n),
        "current_liabilities": _series([150e6] * n),
        "inventory":           _series([30e6]  * n),
        "prepaid_expenses":    _series([10e6]  * n),
        "free_cashflow":       _series([100e6] * n),
        # optional — GMRLong uses them if present
        "total_assets":        _series([1200e6] * n),
        "operating_cashflow":  _series([120e6] * n),
        "capex":               _series([20e6]  * n),
        "eps":                 _series([100e6 / 60e6] * n),
    }


def _failing_fundamentals() -> dict:
    """BIG Corp — expensive mega-cap; fails P/E, P/B, D/E, dividend yield."""
    n = len(YEARS)
    return {
        "revenue":             _series([400e9] * n),
        "net_income":          _series([94e9]  * n),
        "equity":              _series([57e9]  * n),
        "total_liabilities":   _series([308e9] * n),   # D/E ≈ 5.4
        "shares_outstanding":  _series([15.3e9] * n),
        "current_assets":      _series([143e9] * n),
        "current_liabilities": _series([131e9] * n),
        "inventory":           _series([7e9]   * n),
        "prepaid_expenses":    _series([0.0]   * n),
        "free_cashflow":       _series([96e9]  * n),
        "total_assets":        _series([365e9] * n),
        "operating_cashflow":  _series([105e9] * n),
        "capex":               _series([9e9]   * n),
        "eps":                 _series([94e9 / 15.3e9] * n),
    }


def _snapshot(price: float = 13.50, volume: float = 2_500_000) -> dict:
    return {
        "current_price": price,
        "avg_volume": volume,
        "last_dividend": {"date": "2024-11-15", "amount": 0.13},
        "splits": pd.Series(dtype=float),
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def passing_ds():
    return MockDataSource(
        fundamentals=_passing_fundamentals(),
        prices=_series([13.0] * len(YEARS)),
        dividends=_series([0.50] * len(YEARS)),
        snapshot=_snapshot(price=13.50),
    )


@pytest.fixture
def failing_ds():
    return MockDataSource(
        fundamentals=_failing_fundamentals(),
        prices=_series([180.0] * len(YEARS)),
        dividends=_series([0.97] * len(YEARS)),
        snapshot=_snapshot(price=182.0, volume=80_000_000),
    )


@pytest.fixture
def passing_result(passing_ds):
    return GMRLong(passing_ds).compute("XYZ", years=5)


@pytest.fixture
def failing_result(failing_ds):
    return GMRLong(failing_ds).compute("BIG", years=5)


# ---------------------------------------------------------------------------
# Result structure
# ---------------------------------------------------------------------------

def test_result_is_gmrlongresult(passing_result):
    assert isinstance(passing_result, GMRLongResult)


def test_ticker_is_uppercased(passing_result):
    assert passing_result.ticker == "XYZ"


def test_per_year_is_dataframe(passing_result):
    assert isinstance(passing_result.per_year, pd.DataFrame)


def test_per_year_index_contains_years(passing_result):
    assert set(YEARS).issubset(set(passing_result.per_year.index))


def test_per_year_has_all_ratio_columns(passing_result):
    expected_cols = {"pe", "pb", "roe", "npm", "debt_equity",
                     "dividend_yield", "quick_ratio", "free_cashflow"}
    assert expected_cols.issubset(set(passing_result.per_year.columns))


def test_flags_keys_present(passing_result):
    assert set(passing_result.flags) == {"pe", "pb", "roe", "npm",
                                          "debt_equity", "dividend_yield"}


def test_current_price_from_snapshot(passing_result):
    assert passing_result.current_price == pytest.approx(13.50)


def test_avg_volume_from_snapshot(passing_result):
    assert passing_result.avg_volume == pytest.approx(2_500_000)


# ---------------------------------------------------------------------------
# Ratio calculations — XYZ: same values every year so avg = single-year value
# ---------------------------------------------------------------------------

def test_pe_calculation(passing_result):
    # P/E = 13 / (100e6 / 60e6) = 13 / 1.6667 ≈ 7.80
    expected = 13.0 / (100e6 / 60e6)
    assert passing_result.avg_pe == pytest.approx(expected, rel=1e-4)


def test_pb_calculation(passing_result):
    # P/B = 13 / (600e6 / 60e6) = 13 / 10 = 1.30
    assert passing_result.avg_pb == pytest.approx(1.30, rel=1e-4)


def test_roe_calculation(passing_result):
    # ROE = 100M / 600M * 100 = 16.667 %
    assert passing_result.avg_roe == pytest.approx(100 / 6, rel=1e-4)


def test_npm_calculation(passing_result):
    # NPM = 100M / 500M * 100 = 20.0 %
    assert passing_result.avg_npm == pytest.approx(20.0, rel=1e-4)


def test_debt_equity_calculation(passing_result):
    # D/E = 600M / 600M = 1.0
    assert passing_result.avg_debt_equity == pytest.approx(1.0, rel=1e-4)


def test_dividend_yield_calculation(passing_result):
    # DivY = 0.50 / 13 * 100 = 3.846 %
    assert passing_result.avg_dividend_yield == pytest.approx(0.50 / 13 * 100, rel=1e-3)


def test_quick_ratio_calculation(passing_result):
    # QR = (200M - 30M - 10M) / 150M = 160/150 ≈ 1.067
    assert passing_result.avg_quick_ratio == pytest.approx(160 / 150, rel=1e-4)


def test_fcf_pass_through(passing_result):
    assert passing_result.avg_fcf == pytest.approx(100e6, rel=1e-4)


# ---------------------------------------------------------------------------
# Pass/fail verdict — XYZ should pass all
# ---------------------------------------------------------------------------

def test_passing_stock_passes_all(passing_result):
    assert passing_result.passes_all is True


def test_passing_stock_all_flags_true(passing_result):
    assert all(passing_result.flags.values()), passing_result.flags


# ---------------------------------------------------------------------------
# Pass/fail verdict — BIG should fail several flags
# ---------------------------------------------------------------------------

def test_failing_stock_does_not_pass_all(failing_result):
    assert failing_result.passes_all is False


def test_failing_stock_fails_pe(failing_result):
    # P/E ≈ 180 / (94e9/15.3e9) ≈ 29.3 > 15
    assert failing_result.flags["pe"] is False


def test_failing_stock_fails_pb(failing_result):
    # P/B ≈ 180 / (57e9/15.3e9) ≈ 48 > 1.5
    assert failing_result.flags["pb"] is False


def test_failing_stock_fails_debt_equity(failing_result):
    # D/E = 308/57 ≈ 5.4 > 1.5
    assert failing_result.flags["debt_equity"] is False


def test_failing_stock_fails_dividend_yield(failing_result):
    # DivY = 0.97/180 ≈ 0.54 % < 3.5 %
    assert failing_result.flags["dividend_yield"] is False


def test_failing_stock_passes_roe(failing_result):
    # ROE = 94e9/57e9 ≈ 165 % > 15 %
    assert failing_result.flags["roe"] is True


def test_failing_stock_passes_npm(failing_result):
    # NPM = 94/400 = 23.5 % > 15 %
    assert failing_result.flags["npm"] is True


# ---------------------------------------------------------------------------
# Custom settings override
# ---------------------------------------------------------------------------

def test_custom_pe_threshold_relaxed(passing_ds):
    """Relaxing PE to 50 still passes."""
    s = GMRSettings(pe=50)
    result = GMRLong(passing_ds, settings=s).compute("XYZ", years=5)
    assert result.flags["pe"] is True


def test_custom_pe_threshold_strict(passing_ds):
    """Setting PE=5 makes XYZ fail."""
    s = GMRSettings(pe=5)
    result = GMRLong(passing_ds, settings=s).compute("XYZ", years=5)
    assert result.flags["pe"] is False


def test_custom_dividend_yield_zero_threshold(passing_ds):
    """Any dividend yield passes when threshold is 0."""
    s = GMRSettings(dividend_yield=0.0)
    result = GMRLong(passing_ds, settings=s).compute("XYZ", years=5)
    assert result.flags["dividend_yield"] is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_fundamentals_returns_failed_result():
    ds = MockDataSource(
        fundamentals={
            "revenue": pd.Series(dtype=float),
            "net_income": pd.Series(dtype=float),
            "equity": pd.Series(dtype=float),
            "total_liabilities": pd.Series(dtype=float),
            "shares_outstanding": pd.Series(dtype=float),
        },
        prices=pd.Series(dtype=float),
        dividends=pd.Series(dtype=float),
        snapshot={"current_price": 10.0, "avg_volume": 1e6},
    )
    result = GMRLong(ds).compute("EMPTY", years=5)
    assert result.passes_all is False
    assert result.per_year.empty


def test_missing_optional_series_does_not_crash(passing_ds):
    """Inventory and prepaid may be missing — result should still compute."""
    f = _passing_fundamentals()
    del f["inventory"]
    del f["prepaid_expenses"]
    ds = MockDataSource(
        fundamentals=f,
        prices=_series([13.0] * len(YEARS)),
        dividends=_series([0.50] * len(YEARS)),
        snapshot=_snapshot(),
    )
    result = GMRLong(ds).compute("NOINV", years=5)
    # Quick ratio with zero inventory/prepaid = 200/150 ≈ 1.333
    assert result.avg_quick_ratio == pytest.approx(200 / 150, rel=1e-4)
    assert not math.isnan(result.avg_pe)


def test_years_kwarg_limits_look_back(passing_ds):
    """Requesting only 2 years produces a 2-row per_year table."""
    result = GMRLong(passing_ds).compute("XYZ", years=2)
    assert len(result.per_year) == 2


def test_no_dividends_gives_zero_yield():
    """Stock that never paid a dividend should get 0 % yield (not NaN)."""
    ds = MockDataSource(
        fundamentals=_passing_fundamentals(),
        prices=_series([13.0] * len(YEARS)),
        dividends=pd.Series(dtype=float),           # no dividends at all
        snapshot=_snapshot(),
    )
    result = GMRLong(ds).compute("NODIV", years=5)
    assert result.avg_dividend_yield == pytest.approx(0.0)
    assert result.flags["dividend_yield"] is False
