"""
Boundary tests for GMRLong — kills mutmut mutations on comparison operators.

Tests values exactly at each threshold to distinguish <= from < and >= from >.
Also verifies _empty_result structure and years_for_avg default usage.
"""
# MockDS below stubs the GMRDataSource protocol on one line per method — same
# pattern as test_data_source_mutations. _make_ds takes one positional per
# ratio because each ratio is tested at its threshold independently — a kwargs
# dict would erase the column header that makes the test table readable. Both
# shapes are intentional, so:
#   multiple-statements (C0321) — one-line `def m(...): return ...` stubs
#   line-too-long (C0301) — parametrize tables read wider than 100 chars
#   too-many-arguments / too-many-positional-arguments (R0913/R0917) — one arg
#       per ratio is the boundary-test design
# pylint: disable=missing-function-docstring,redefined-outer-name,missing-class-docstring,unused-argument,multiple-statements,line-too-long,too-many-arguments,too-many-positional-arguments
from __future__ import annotations

import math
import pytest
import pandas as pd

from src.analysis.gmr_data_source import GMRDataSource, GMRSettings, MarketSnapshot
from src.analysis.gmr_long import GMRLong


# ---------------------------------------------------------------------------
# MockDataSource
# ---------------------------------------------------------------------------

YEARS = [2024, 2023, 2022, 2021, 2020]


def _series(values):
    return pd.Series(dict(zip(YEARS, values)))


class MockDS(GMRDataSource):
    """Minimal mock data source for boundary testing."""
    def __init__(self, fundamentals, prices, dividends, snapshot):
        self._f = fundamentals
        self._p = prices
        self._d = dividends
        self._s = snapshot

    def get_annual_fundamentals(self, ticker, years): return self._f
    def get_annual_avg_prices(self, ticker, years): return self._p
    def get_annual_dividends(self, ticker): return self._d
    def get_price_history(self, ticker, period="1y"): return pd.DataFrame()
    def get_market_snapshot(self, ticker): return self._s
    def get_available_tickers(self): return []
    def search_tickers(self, query, limit=10): return []  # pylint: disable=unused-argument
    def get_data_source_name(self, ticker): return "mock"


def _make_ds(pe_ratio, pb_ratio, roe_pct, npm_pct, de_ratio, div_yield_pct):
    """Build a MockDS where each ratio hits exact target values.

    The ratios are inter-dependent (PE/PB/ROE all depend on net_income, equity, price).
    We choose net_income and equity from pe_ratio and roe_pct, then derive price from
    pe_ratio and pb_ratio independently via the avg annual price.

    pe_ratio: target avg P/E
    pb_ratio: target avg P/B (may require relaxed settings threshold)
    roe_pct:  target avg ROE %
    npm_pct:  target avg NPM %
    de_ratio: target avg D/E
    div_yield_pct: target avg dividend yield %
    """
    n = len(YEARS)
    shares = 10e6

    # Fix net_income and equity from roe_pct, then derive price from pe_ratio.
    net_income = 100e6  # arbitrary anchor
    equity = net_income / (roe_pct / 100) if roe_pct != 0 else 1e9
    # P/E = price / EPS => price = pe_ratio * (net_income / shares)
    price = pe_ratio * (net_income / shares)
    # NPM = net_income / revenue * 100 => revenue = net_income / (npm/100)
    revenue = net_income / (npm_pct / 100) if npm_pct != 0 else 1
    # D/E = total_liabilities / equity
    total_liabilities = de_ratio * equity
    # DivY = div_per_share / price * 100 => div_per_share = price * div_yield / 100
    div_per_share = price * div_yield_pct / 100

    return MockDS(
        fundamentals={
            "revenue": _series([revenue] * n),
            "net_income": _series([net_income] * n),
            "equity": _series([equity] * n),
            "total_liabilities": _series([total_liabilities] * n),
            "shares_outstanding": _series([shares] * n),
            "current_assets": _series([200e6] * n),
            "current_liabilities": _series([150e6] * n),
            "inventory": _series([30e6] * n),
            "prepaid_expenses": _series([10e6] * n),
            "free_cashflow": _series([50e6] * n),
        },
        prices=_series([price] * n),
        dividends=_series([div_per_share] * n),
        snapshot=MarketSnapshot(current_price=price, avg_volume=2e6),
    )


# ---------------------------------------------------------------------------
# Boundary tests: value exactly AT threshold should pass
# ---------------------------------------------------------------------------

class TestPEBoundary:
    """P/E uses <= : value exactly at threshold must pass."""

    def test_pe_exactly_at_threshold_passes(self):
        # Default pe threshold = 15. Build stock with avg P/E = 15.
        ds = _make_ds(pe_ratio=15, pb_ratio=1.0, roe_pct=20, npm_pct=20, de_ratio=1.0, div_yield_pct=5)
        result = GMRLong(ds).compute("BOUND", years=5)
        assert result.flags["pe"] is True

    def test_pe_just_above_threshold_fails(self):
        ds = _make_ds(pe_ratio=15.01, pb_ratio=1.0, roe_pct=20, npm_pct=20, de_ratio=1.0, div_yield_pct=5)
        result = GMRLong(ds).compute("BOUND", years=5)
        assert result.flags["pe"] is False


class TestPBBoundary:
    """P/B uses <= : value exactly at threshold must pass.
    Since PB is derived (price / BVPS), we use custom settings to set the threshold
    to match the actual PB computed from our test data.
    """

    def test_pb_exactly_at_threshold_passes(self):
        ds = _make_ds(pe_ratio=10, pb_ratio=1.5, roe_pct=20, npm_pct=20, de_ratio=1.0, div_yield_pct=5)
        result = GMRLong(ds).compute("BOUND", years=5)
        # Set pb_value threshold to the actual computed PB
        s = GMRSettings(pb_value=result.avg_pb)
        result2 = GMRLong(ds, settings=s).compute("BOUND", years=5)
        assert result2.flags["pb"] is True

    def test_pb_just_below_threshold_fails(self):
        ds = _make_ds(pe_ratio=10, pb_ratio=1.5, roe_pct=20, npm_pct=20, de_ratio=1.0, div_yield_pct=5)
        result = GMRLong(ds).compute("BOUND", years=5)
        # Set threshold slightly below actual PB — should fail
        s = GMRSettings(pb_value=result.avg_pb - 0.01)
        result2 = GMRLong(ds, settings=s).compute("BOUND", years=5)
        assert result2.flags["pb"] is False


class TestROEBoundary:
    """ROE uses >= : value exactly at threshold must pass."""

    def test_roe_exactly_at_threshold_passes(self):
        ds = _make_ds(pe_ratio=10, pb_ratio=1.5, roe_pct=15, npm_pct=20, de_ratio=1.0, div_yield_pct=5)
        # Relax pb_value threshold so PB doesn't interfere
        s = GMRSettings(pb_value=100)
        result = GMRLong(ds, settings=s).compute("BOUND", years=5)
        assert result.avg_roe == pytest.approx(15.0, rel=1e-2)
        assert result.flags["roe"] is True

    def test_roe_just_below_threshold_fails(self):
        ds = _make_ds(pe_ratio=10, pb_ratio=1.5, roe_pct=14.9, npm_pct=20, de_ratio=1.0, div_yield_pct=5)
        s = GMRSettings(pb_value=100)
        result = GMRLong(ds, settings=s).compute("BOUND", years=5)
        assert result.flags["roe"] is False


class TestNPMBoundary:
    """NPM uses >= : value exactly at threshold must pass."""

    def test_npm_exactly_at_threshold_passes(self):
        ds = _make_ds(pe_ratio=10, pb_ratio=1.0, roe_pct=20, npm_pct=15, de_ratio=1.0, div_yield_pct=5)
        result = GMRLong(ds).compute("BOUND", years=5)
        assert result.avg_npm == pytest.approx(15.0, rel=1e-2)
        assert result.flags["npm"] is True

    def test_npm_just_below_threshold_fails(self):
        ds = _make_ds(pe_ratio=10, pb_ratio=1.0, roe_pct=20, npm_pct=14.9, de_ratio=1.0, div_yield_pct=5)
        result = GMRLong(ds).compute("BOUND", years=5)
        assert result.flags["npm"] is False


class TestDebtEquityBoundary:
    """D/E uses <= : value exactly at threshold must pass."""

    def test_de_exactly_at_threshold_passes(self):
        ds = _make_ds(pe_ratio=10, pb_ratio=1.0, roe_pct=20, npm_pct=20, de_ratio=1.5, div_yield_pct=5)
        result = GMRLong(ds).compute("BOUND", years=5)
        assert result.avg_debt_equity == pytest.approx(1.5, rel=1e-2)
        assert result.flags["debt_equity"] is True

    def test_de_just_above_threshold_fails(self):
        ds = _make_ds(pe_ratio=10, pb_ratio=1.0, roe_pct=20, npm_pct=20, de_ratio=1.51, div_yield_pct=5)
        result = GMRLong(ds).compute("BOUND", years=5)
        assert result.flags["debt_equity"] is False


class TestDividendYieldBoundary:
    """DivY uses >= : value exactly at threshold must pass.
    Threshold is dividend_yield=0.035 (3.5%), compared as >= s.dividend_yield * 100."""

    def test_divy_exactly_at_threshold_passes(self):
        ds = _make_ds(pe_ratio=10, pb_ratio=1.0, roe_pct=20, npm_pct=20, de_ratio=1.0, div_yield_pct=3.5)
        result = GMRLong(ds).compute("BOUND", years=5)
        assert result.avg_dividend_yield == pytest.approx(3.5, rel=1e-2)
        assert result.flags["dividend_yield"] is True

    def test_divy_just_below_threshold_fails(self):
        ds = _make_ds(pe_ratio=10, pb_ratio=1.0, roe_pct=20, npm_pct=20, de_ratio=1.0, div_yield_pct=3.49)
        result = GMRLong(ds).compute("BOUND", years=5)
        assert result.flags["dividend_yield"] is False


# ---------------------------------------------------------------------------
# Empty result structure
# ---------------------------------------------------------------------------

class TestEmptyResult:
    """Verify _empty_result dict keys and structure."""

    def test_empty_result_has_all_flag_keys(self):
        ds = MockDS(
            fundamentals={
                "revenue": pd.Series(dtype=float),
                "net_income": pd.Series(dtype=float),
                "equity": pd.Series(dtype=float),
                "total_liabilities": pd.Series(dtype=float),
                "shares_outstanding": pd.Series(dtype=float),
            },
            prices=pd.Series(dtype=float),
            dividends=pd.Series(dtype=float),
            snapshot=MarketSnapshot(current_price=42.0, avg_volume=1e6),
        )
        result = GMRLong(ds).compute("EMPTY", years=5)
        assert set(result.flags) == {"pe", "pb", "roe", "npm", "debt_equity", "dividend_yield"}
        assert all(v is False for v in result.flags.values())

    def test_empty_result_preserves_snapshot_price(self):
        ds = MockDS(
            fundamentals={
                "revenue": pd.Series(dtype=float),
                "net_income": pd.Series(dtype=float),
                "equity": pd.Series(dtype=float),
                "total_liabilities": pd.Series(dtype=float),
                "shares_outstanding": pd.Series(dtype=float),
            },
            prices=pd.Series(dtype=float),
            dividends=pd.Series(dtype=float),
            snapshot=MarketSnapshot(current_price=42.0, avg_volume=99.0),
        )
        result = GMRLong(ds).compute("EMPTY", years=5)
        assert result.current_price == pytest.approx(42.0)
        assert result.avg_volume == pytest.approx(99.0)

    def test_empty_result_averages_are_nan(self):
        ds = MockDS(
            fundamentals={
                "revenue": pd.Series(dtype=float),
                "net_income": pd.Series(dtype=float),
                "equity": pd.Series(dtype=float),
                "total_liabilities": pd.Series(dtype=float),
                "shares_outstanding": pd.Series(dtype=float),
            },
            prices=pd.Series(dtype=float),
            dividends=pd.Series(dtype=float),
            snapshot=MarketSnapshot(current_price=10.0, avg_volume=0),
        )
        result = GMRLong(ds).compute("EMPTY", years=5)
        assert math.isnan(result.avg_pe)
        assert math.isnan(result.avg_pb)
        assert math.isnan(result.avg_roe)
        assert math.isnan(result.avg_npm)


# ---------------------------------------------------------------------------
# years_for_avg default is used when years kwarg is None
# ---------------------------------------------------------------------------

class TestYearsDefault:
    def test_none_years_uses_settings_default(self):
        """When years is None, GMRLong uses settings.years_for_avg (default 5)."""
        n = 7  # provide more data than default look-back
        years = list(range(2024, 2024 - n, -1))
        s = lambda vals: pd.Series(dict(zip(years, vals)))  # pylint: disable=unnecessary-lambda-assignment

        ds = MockDS(
            fundamentals={
                "revenue": s([500e6] * n),
                "net_income": s([100e6] * n),
                "equity": s([600e6] * n),
                "total_liabilities": s([600e6] * n),
                "shares_outstanding": s([60e6] * n),
                "current_assets": s([200e6] * n),
                "current_liabilities": s([150e6] * n),
                "inventory": s([30e6] * n),
                "prepaid_expenses": s([10e6] * n),
                "free_cashflow": s([100e6] * n),
            },
            prices=s([13.0] * n),
            dividends=s([0.50] * n),
            snapshot=MarketSnapshot(current_price=13.5, avg_volume=2.5e6),
        )
        # Default settings has years_for_avg=5
        result = GMRLong(ds).compute("XYZ")
        assert len(result.per_year) == 5

    def test_custom_years_for_avg_in_settings(self):
        """Custom years_for_avg=3 limits look-back to 3."""
        n = 5
        years = list(range(2024, 2024 - n, -1))
        s = lambda vals: pd.Series(dict(zip(years, vals)))  # pylint: disable=unnecessary-lambda-assignment

        ds = MockDS(
            fundamentals={
                "revenue": s([500e6] * n),
                "net_income": s([100e6] * n),
                "equity": s([600e6] * n),
                "total_liabilities": s([600e6] * n),
                "shares_outstanding": s([60e6] * n),
                "current_assets": s([200e6] * n),
                "current_liabilities": s([150e6] * n),
                "inventory": s([30e6] * n),
                "prepaid_expenses": s([10e6] * n),
                "free_cashflow": s([100e6] * n),
            },
            prices=s([13.0] * n),
            dividends=s([0.50] * n),
            snapshot=MarketSnapshot(current_price=13.5, avg_volume=2.5e6),
        )
        settings = GMRSettings(years_for_avg=3)
        result = GMRLong(ds, settings=settings).compute("XYZ")
        assert len(result.per_year) == 3
