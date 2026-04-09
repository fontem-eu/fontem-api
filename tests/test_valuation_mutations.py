"""
Mutation-killing tests for Valuation — targets surviving mutants in valuation.py.

Covers: _DEFAULT_TAX_RATE, EBITDA/ROIC/EV calculations, NaN guards,
boundary conditions, empty result structure, dataclass defaults.
"""
# pylint: disable=missing-function-docstring,redefined-outer-name,missing-class-docstring,unused-argument,multiple-statements,too-few-public-methods,unused-import
from __future__ import annotations

import math

import numpy as np
import pytest
import pandas as pd

from src.analysis.gmr_data_source import GMRDataSource, MarketSnapshot
from src.analysis.valuation import Valuation, ValuationResult, _DEFAULT_TAX_RATE


YEARS = [2023, 2022, 2021]


def _series(vals):
    return pd.Series(dict(zip(YEARS, vals)))


class MockDS(GMRDataSource):
    def __init__(self, fundamentals, prices, snapshot):
        self._f = fundamentals
        self._p = prices
        self._s = snapshot
    def get_annual_fundamentals(self, ticker, years): return self._f
    def get_annual_avg_prices(self, ticker, years): return self._p
    def get_annual_dividends(self, ticker): return pd.Series(dtype=float)
    def get_price_history(self, ticker, period="1y"): return pd.DataFrame()
    def get_market_snapshot(self, ticker): return self._s
    def get_available_tickers(self): return []
    def search_tickers(self, query, limit=10): return []
    def get_data_source_name(self, ticker): return "mock"


def _make_ds(**overrides):
    n = len(YEARS)
    defaults = {
        "revenue": _series([100_000] * n),
        "operating_income": _series([25_000] * n),
        "net_income": _series([18_000] * n),
        "equity": _series([80_000] * n),
        "long_term_debt": _series([40_000] * n),
        "cash_and_equivalents": _series([15_000] * n),
        "depreciation_amortization": _series([4_000] * n),
        "interest_expense": _series([2_000] * n),
        "income_tax_expense": _series([5_000] * n),
        "free_cashflow": _series([17_000] * n),
    }
    defaults.update(overrides)
    snapshot = MarketSnapshot(
        current_price=22.0, avg_volume=500_000,
        shares_outstanding=10_000, last_dividend_date="2023-12-15",
        last_dividend_amount=0.10,
    )
    return MockDS(defaults, _series([20.0] * n), snapshot)


@pytest.fixture
def result():
    return Valuation(_make_ds()).compute("TEST", years=3)


# ── Constants ──────────────────────────────────────────────────

class TestConstants:
    def test_default_tax_rate_is_0_21(self):
        assert _DEFAULT_TAX_RATE == 0.21


# ── Result structure ───────────────────────────────────────────

class TestResultStructure:
    def test_ticker_uppercased(self, result):
        assert result.ticker == "TEST"

    def test_per_year_is_dataframe(self, result):
        assert isinstance(result.per_year, pd.DataFrame)

    def test_per_year_has_ebitda_column(self, result):
        assert "ebitda" in result.per_year.columns

    def test_per_year_has_roic_column(self, result):
        assert "roic" in result.per_year.columns

    def test_per_year_has_effective_tax_rate(self, result):
        assert "effective_tax_rate" in result.per_year.columns

    def test_per_year_has_nopat(self, result):
        assert "nopat" in result.per_year.columns

    def test_per_year_has_invested_capital(self, result):
        assert "invested_capital" in result.per_year.columns

    def test_last_dividend_has_date_and_amount(self, result):
        assert "date" in result.last_dividend
        assert "amount" in result.last_dividend
        assert result.last_dividend["date"] == "2023-12-15"
        assert result.last_dividend["amount"] == 0.10


# ── EBITDA calculation ─────────────────────────────────────────

class TestEBITDA:
    def test_ebitda_is_operating_income_plus_da(self, result):
        # operating_income=25000, da=4000 => EBITDA=29000
        assert result.per_year.at[2023, "ebitda"] == pytest.approx(29_000)

    def test_ebitda_margin(self, result):
        # EBITDA/Revenue*100 = 29000/100000*100 = 29%
        assert result.per_year.at[2023, "ebitda_margin"] == pytest.approx(29.0)

    def test_da_column_shows_nonzero_values(self, result):
        assert result.per_year.at[2023, "da"] == pytest.approx(4000)

    def test_da_zero_shows_nan(self):
        ds = _make_ds(depreciation_amortization=_series([0.0] * 3))
        r = Valuation(ds).compute("X", years=3)
        assert math.isnan(r.per_year.at[2023, "da"])


# ── Net Debt / Interest Coverage ───────────────────────────────

class TestLeverage:
    def test_net_debt_is_ltd_minus_cash(self, result):
        # 40000 - 15000 = 25000
        assert result.per_year.at[2023, "net_debt"] == pytest.approx(25_000)

    def test_interest_coverage_is_ebit_over_interest(self, result):
        # 25000 / 2000 = 12.5
        assert result.per_year.at[2023, "interest_coverage"] == pytest.approx(12.5)

    def test_zero_interest_gives_nan_coverage(self):
        ds = _make_ds(interest_expense=_series([0.0] * 3))
        r = Valuation(ds).compute("X", years=3)
        assert math.isnan(r.per_year.at[2023, "interest_coverage"])

    def test_cash_column_shows_nonzero(self, result):
        assert result.per_year.at[2023, "cash_and_equivalents"] == pytest.approx(15_000)

    def test_ltd_column_shows_nonzero(self, result):
        assert result.per_year.at[2023, "long_term_debt"] == pytest.approx(40_000)

    def test_interest_expense_passthrough(self, result):
        assert result.per_year.at[2023, "interest_expense"] == pytest.approx(2_000)


# ── Tax rate / ROIC ────────────────────────────────────────────

class TestROIC:
    def test_effective_tax_rate_computed(self, result):
        # tax/(NI+tax) = 5000/(18000+5000) = 21.74%
        assert result.per_year.at[2023, "effective_tax_rate"] == pytest.approx(21.74, rel=0.01)

    def test_nopat_computed(self, result):
        # NOPAT = OpInc * (1 - eff_tax) = 25000 * (1 - 0.2174) = ~19565
        nopat = result.per_year.at[2023, "nopat"]
        assert nopat == pytest.approx(25_000 * (1 - 5000/23000), rel=0.01)

    def test_invested_capital_computed(self, result):
        # equity + ltd - cash = 80000 + 40000 - 15000 = 105000
        assert result.per_year.at[2023, "invested_capital"] == pytest.approx(105_000)

    def test_roic_computed(self, result):
        # ROIC = NOPAT / IC * 100
        roic = result.per_year.at[2023, "roic"]
        assert not math.isnan(roic)
        assert roic > 0

    def test_fallback_to_default_tax_rate_when_missing(self):
        ds = _make_ds(income_tax_expense=pd.Series(dtype=float))
        r = Valuation(ds).compute("X", years=3)
        # Should use _DEFAULT_TAX_RATE (0.21)
        assert r.per_year.at[2023, "effective_tax_rate"] == pytest.approx(21.0)


# ── Enterprise Value ───────────────────────────────────────────

class TestEnterpriseValue:
    def test_market_cap_computed(self, result):
        # price * shares = 22 * 10000 = 220000
        assert result.market_cap == pytest.approx(220_000)

    def test_enterprise_value_computed(self, result):
        # EV = market_cap + net_debt = 220000 + 25000 = 245000
        assert result.enterprise_value == pytest.approx(245_000)

    def test_ev_ebitda_ratio(self, result):
        assert not math.isnan(result.ev_ebitda)

    def test_ev_revenue_ratio(self, result):
        assert not math.isnan(result.ev_revenue)

    def test_ev_fcf_ratio(self, result):
        assert not math.isnan(result.ev_fcf)

    def test_ev_ebit_ratio(self, result):
        assert not math.isnan(result.ev_ebit)


# ── Empty result ───────────────────────────────────────────────

class TestEmptyResult:
    def test_empty_when_no_fundamentals(self):
        ds = MockDS({}, pd.Series(dtype=float),
                    MarketSnapshot(current_price=10.0, avg_volume=1e6, shares_outstanding=1000))
        r = Valuation(ds).compute("EMPTY", years=3)
        assert r.per_year.empty
        assert r.ticker == "EMPTY"
        assert math.isnan(r.avg_roic)
        assert math.isnan(r.avg_ebitda_margin)

    def test_empty_preserves_market_cap(self):
        ds = MockDS({}, pd.Series(dtype=float),
                    MarketSnapshot(current_price=50.0, avg_volume=0, shares_outstanding=100))
        r = Valuation(ds).compute("EMPTY", years=3)
        assert r.current_price == pytest.approx(50.0)
        assert r.market_cap == pytest.approx(5000.0)

    def test_empty_with_no_price_gives_nan_market_cap(self):
        ds = MockDS({}, pd.Series(dtype=float), MarketSnapshot())
        r = Valuation(ds).compute("X", years=3)
        assert math.isnan(r.market_cap)

    def test_empty_with_zero_shares_gives_nan_market_cap(self):
        ds = MockDS({}, pd.Series(dtype=float),
                    MarketSnapshot(current_price=10.0, shares_outstanding=0))
        r = Valuation(ds).compute("X", years=3)
        assert math.isnan(r.market_cap)


# ── ValuationResult defaults ──────────────────────────────────

class TestValuationResultDefaults:
    def test_last_dividend_default_is_empty_dict(self):
        r = ValuationResult(
            ticker="X", per_year=pd.DataFrame(),
            avg_ebitda_margin=0, avg_roic=0, avg_interest_coverage=0,
            avg_net_debt_to_ebitda=0, enterprise_value=0,
            ev_ebitda=0, ev_revenue=0, ev_fcf=0, ev_ebit=0,
            current_price=0, market_cap=0,
        )
        assert not r.last_dividend
