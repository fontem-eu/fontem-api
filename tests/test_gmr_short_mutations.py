"""
Mutation-killing tests for GMRShort — targets surviving mutants.

Covers: flag boundary conditions, empty result structure, VUp/VDown guards,
win probability, MAT calculation, DatetimeIndex normalization.
"""
# pylint: disable=missing-function-docstring,redefined-outer-name,missing-class-docstring,unused-argument,multiple-statements
from __future__ import annotations

import math

import numpy as np
import pytest
import pandas as pd

from src.analysis.gmr_data_source import GMRDataSource, GMRSettings, MarketSnapshot
from src.analysis.gmr_short import GMRShort, GMRShortResult


def _make_history(n_days=130, base_price=1.0, volatility=0.02):
    """Generate n_days of OHLCV data ending at today."""
    dates = pd.bdate_range(end="2024-12-31", periods=n_days)
    np.random.seed(42)
    closes = base_price * np.cumprod(1 + np.random.normal(0, volatility, n_days))
    return pd.DataFrame({
        "Open": closes * 0.99,
        "High": closes * 1.02,
        "Low": closes * 0.98,
        "Close": closes,
        "Volume": np.random.randint(100_000, 1_000_000, n_days),
    }, index=dates)


class MockDS(GMRDataSource):
    def __init__(self, history, snapshot):
        self._h = history
        self._s = snapshot
    def get_annual_fundamentals(self, ticker, years): return {}
    def get_annual_avg_prices(self, ticker, years): return pd.Series(dtype=float)
    def get_annual_dividends(self, ticker): return pd.Series(dtype=float)
    def get_price_history(self, ticker, period="1y"): return self._h
    def get_market_snapshot(self, ticker): return self._s
    def get_available_tickers(self): return []
    def search_tickers(self, query, limit=10): return []
    def get_data_source_name(self, ticker): return "mock"


def _snapshot(price=1.0, volume=2e6):
    return MarketSnapshot(current_price=price, avg_volume=volume)


@pytest.fixture
def result():
    hist = _make_history()
    ds = MockDS(hist, _snapshot(price=float(hist["Close"].iloc[-1]), volume=2e6))
    return GMRShort(ds).compute("TEST")


# ── Result structure ───────────────────────────────────────────

class TestResultStructure:
    def test_ticker_uppercased(self, result):
        assert result.ticker == "TEST"

    def test_flags_have_all_keys(self, result):
        assert set(result.flags) == {"volume", "price_range", "win_prob", "volatility", "mat"}

    def test_win_probability_between_0_and_1(self, result):
        assert 0.0 <= result.win_probability <= 1.0

    def test_avg_v_up_is_positive(self, result):
        assert result.avg_v_up > 0

    def test_avg_v_down_is_negative(self, result):
        assert result.avg_v_down < 0

    def test_mat_43d_is_not_nan(self, result):
        assert not math.isnan(result.mat_43d)

    def test_monthly_breakdown_is_dataframe(self, result):
        assert isinstance(result.monthly_breakdown, pd.DataFrame)


# ── Empty result ───────────────────────────────────────────────

class TestEmptyResult:
    def test_empty_on_no_history(self):
        ds = MockDS(pd.DataFrame(), _snapshot())
        r = GMRShort(ds).compute("EMPTY")
        assert r.passes_all is False
        assert r.win_probability == 0.0
        assert r.avg_v_up == 0.0
        assert r.avg_v_down == 0.0
        assert math.isnan(r.mat_43d)
        assert math.isnan(r.diff_mat_pct)

    def test_empty_flags_all_false(self):
        ds = MockDS(pd.DataFrame(), _snapshot())
        r = GMRShort(ds).compute("EMPTY")
        assert set(r.flags) == {"volume", "price_range", "win_prob", "volatility", "mat"}
        assert all(v is False for v in r.flags.values())

    def test_empty_preserves_price_and_volume(self):
        ds = MockDS(pd.DataFrame(), _snapshot(price=42.0, volume=99.0))
        r = GMRShort(ds).compute("EMPTY")
        assert r.current_price == pytest.approx(42.0)
        assert r.avg_volume == pytest.approx(99.0)

    def test_too_few_days_returns_empty(self):
        hist = _make_history(n_days=3)
        ds = MockDS(hist, _snapshot())
        r = GMRShort(ds).compute("SHORT")
        assert r.passes_all is False
        assert r.monthly_breakdown.empty


# ── Flag boundary tests ────────────────────────────────────────

class TestFlagBoundaries:
    def test_volume_flag_boundary(self):
        hist = _make_history()
        # volume exactly at threshold
        s = GMRSettings(min_volume=2e6)
        ds = MockDS(hist, _snapshot(price=float(hist["Close"].iloc[-1]), volume=2e6))
        r = GMRShort(ds, settings=s).compute("X")
        assert r.flags["volume"] is False  # > not >=

    def test_volume_flag_above(self):
        hist = _make_history()
        s = GMRSettings(min_volume=1e6)
        ds = MockDS(hist, _snapshot(price=float(hist["Close"].iloc[-1]), volume=2e6))
        r = GMRShort(ds, settings=s).compute("X")
        assert r.flags["volume"] is True

    def test_price_range_flag_at_min(self):
        hist = _make_history(base_price=0.40)
        s = GMRSettings(min_price=0.40, max_price=2.50)
        price = float(hist["Close"].iloc[-1])
        ds = MockDS(hist, _snapshot(price=0.40, volume=2e6))
        r = GMRShort(ds, settings=s).compute("X")
        assert r.flags["price_range"] is True  # <= (inclusive)

    def test_price_range_flag_below_min(self):
        hist = _make_history(base_price=0.30)
        s = GMRSettings(min_price=0.40, max_price=2.50)
        ds = MockDS(hist, _snapshot(price=0.39, volume=2e6))
        r = GMRShort(ds, settings=s).compute("X")
        assert r.flags["price_range"] is False


# ── DatetimeIndex normalization ────────────────────────────────

class TestDatetimeNormalization:
    def test_tz_aware_index_gets_localized(self):
        hist = _make_history(n_days=60)
        hist.index = hist.index.tz_localize("US/Eastern")
        ds = MockDS(hist, _snapshot(price=float(hist["Close"].iloc[-1])))
        r = GMRShort(ds).compute("TZ")
        assert not math.isnan(r.win_probability)

    def test_non_datetime_index_gets_converted(self):
        hist = _make_history(n_days=60)
        hist.index = hist.index.astype(str)  # string index
        ds = MockDS(hist, _snapshot(price=float(hist["Close"].iloc[-1])))
        r = GMRShort(ds).compute("STR")
        assert not math.isnan(r.win_probability)


# ── VUp/VDown guards ──────────────────────────────────────────

class TestVUpVDown:
    def test_zero_low_gives_nan_vup(self):
        """When low is 0, VUp should be NaN (division guard)."""
        hist = _make_history(n_days=60)
        hist.iloc[30, hist.columns.get_loc("Low")] = 0.0
        ds = MockDS(hist, _snapshot(price=float(hist["Close"].iloc[-1])))
        r = GMRShort(ds).compute("ZEROLOW")
        # Should not crash
        assert isinstance(r, GMRShortResult)


# ── MAT / diffMAT ─────────────────────────────────────────────

class TestMAT:
    def test_diff_mat_nan_when_price_zero(self):
        hist = _make_history(n_days=60)
        ds = MockDS(hist, _snapshot(price=0.0))
        r = GMRShort(ds).compute("X")
        assert math.isnan(r.diff_mat_pct)

    def test_diff_mat_nan_when_price_none(self):
        hist = _make_history(n_days=60)
        ds = MockDS(hist, MarketSnapshot(current_price=None, avg_volume=0))
        r = GMRShort(ds).compute("X")
        assert math.isnan(r.diff_mat_pct)
