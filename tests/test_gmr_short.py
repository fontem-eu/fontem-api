"""
Unit tests for GMRShort — no network, no I/O, sub-second execution.

Test data design
----------------
"Full" scenario  – 130 business days ending 2024-12-31.
                   65 % positive days, intraday range ±40 %.
                   All filters pass.

"Micro" datasets – tiny deterministic DataFrames for exact metric math.

VUp / VDown formulas (from GMRShort.cs)
  VUp(day)   = max_high_same_month_from_today  / low_today  − 1
  VDown(day) = 1 − high_today / min_low_same_month_from_today

Win probability  = rank_in_descending_change_order / n_days for the
                   boundary positive day  ≡  fraction of non-negative days.

MAT = mean of the 43 most-recent closing prices within the 6-month window.
diffMAT = (MAT − current_price) / current_price.
"""
from __future__ import annotations
# pylint: disable=missing-function-docstring,redefined-outer-name,missing-class-docstring,multiple-statements,too-many-arguments,too-many-positional-arguments

import numpy as np
import pytest
import pandas as pd

from src.analysis.gmr_data_source import GMRDataSource, GMRSettings
from src.analysis.gmr_short import GMRShort, GMRShortResult


# ---------------------------------------------------------------------------
# MockDataSource
# ---------------------------------------------------------------------------

class MockDataSource(GMRDataSource):
    def __init__(self, history: pd.DataFrame, snapshot: dict):
        self._history  = history
        self._snapshot = snapshot

    def get_annual_fundamentals(self, ticker, years):  return {}
    def get_annual_avg_prices(self, ticker, years):    return pd.Series(dtype=float)
    def get_annual_dividends(self, ticker):            return pd.Series(dtype=float)
    def get_price_history(self, ticker, period="1y"):  return self._history
    def get_market_snapshot(self, ticker):             return self._snapshot


# ---------------------------------------------------------------------------
# OHLCV factories
# ---------------------------------------------------------------------------

def _make_history(
    n_days: int = 130,
    close: float = 1.00,
    high_factor: float = 1.40,
    low_factor:  float = 0.70,
    positive_fraction: float = 0.65,
    volume: float = 5_000_000,
    seed: int = 42,
) -> pd.DataFrame:
    """
    Deterministic OHLCV.  Intraday high/low are fixed factors of close so
    VUp/VDown calculations are highly predictable.
    """
    rng   = np.random.default_rng(seed)
    dates = pd.bdate_range(end="2024-12-31", periods=n_days)
    closes = [close]
    for _ in range(n_days - 1):
        move = rng.uniform(0.005, 0.020)
        closes.append(
            closes[-1] * (1 + move if rng.random() < positive_fraction else 1 - move)
        )
    closes = np.array(closes)
    return pd.DataFrame({
        "Open":   closes,
        "High":   closes * high_factor,
        "Low":    closes * low_factor,
        "Close":  closes,
        "Volume": np.full(n_days, volume, dtype=float),
    }, index=dates)


def _micro_history(rows: list[dict]) -> pd.DataFrame:
    """Build a tiny OHLCV DataFrame from a list of dicts."""
    df = pd.DataFrame(rows)
    df.index = pd.DatetimeIndex(df.pop("date"))
    return df[["Open", "High", "Low", "Close", "Volume"]]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PASS_SNAPSHOT = {
    "current_price": 1.00,
    "avg_volume":    5_000_000,
    "last_dividend": {"date": None, "amount": 0.0},
    "splits":        pd.Series(dtype=float),
}


@pytest.fixture
def passing_history():
    return _make_history(n_days=130, close=1.00, high_factor=1.40,
                         low_factor=0.70, positive_fraction=0.65, volume=5_000_000)


@pytest.fixture
def passing_ds(passing_history):
    return MockDataSource(history=passing_history, snapshot=PASS_SNAPSHOT)


@pytest.fixture
def passing_result(passing_ds):
    return GMRShort(passing_ds).compute("VOLT")


# ---------------------------------------------------------------------------
# Structure tests
# ---------------------------------------------------------------------------

def test_result_type(passing_result):
    assert isinstance(passing_result, GMRShortResult)


def test_ticker_uppercased(passing_result):
    assert passing_result.ticker == "VOLT"


def test_monthly_breakdown_is_dataframe(passing_result):
    assert isinstance(passing_result.monthly_breakdown, pd.DataFrame)


def test_monthly_breakdown_columns(passing_result):
    cols = set(passing_result.monthly_breakdown.columns)
    assert {"v_up", "v_down"}.issubset(cols)


def test_monthly_breakdown_has_six_months(passing_result):
    # 130 bdays ending Dec → 6 active months (Jul–Dec)
    assert len(passing_result.monthly_breakdown) == 6


def test_flags_keys_present(passing_result):
    assert set(passing_result.flags) == {
        "volume", "price_range", "win_prob", "volatility", "mat"
    }


# ---------------------------------------------------------------------------
# Passing scenario — all flags True
# ---------------------------------------------------------------------------

def test_passes_all_for_volatile_penny_stock(passing_result):
    assert passing_result.passes_all is True, passing_result.flags


def test_win_probability_above_half(passing_result):
    assert passing_result.win_probability > 0.5


def test_avg_v_up_above_threshold(passing_result):
    assert passing_result.avg_v_up > 0.30


def test_avg_v_down_below_threshold(passing_result):
    assert passing_result.avg_v_down < -0.30


def test_current_price_from_snapshot(passing_result):
    assert passing_result.current_price == pytest.approx(1.00)


def test_avg_volume_from_snapshot(passing_result):
    assert passing_result.avg_volume == pytest.approx(5_000_000)


# ---------------------------------------------------------------------------
# Win probability exact calculation
# ---------------------------------------------------------------------------

def test_win_probability_equals_positive_day_fraction():
    """
    Build a 10-day window with exactly 7 non-negative changes.
    Expected win probability = 7/10 = 0.70.
    """
    # 10 contiguous bdays in the same 6-month window
    dates = pd.bdate_range("2024-07-01", periods=10)
    # Construct closes so that exactly 7 days have non-negative change:
    # day 0 baseline=1.0, then +,+,+,+,+,+,+,-,-  (changes from day 1 onward)
    closes = [1.00, 1.02, 1.04, 1.06, 1.08, 1.10, 1.12, 1.14, 1.12, 1.10]
    # changes (day 1..9): +0.02,+0.02,...,+0.02,-0.02,-0.02 → 7 positives, 2 negatives
    # (day 0 change = 0 → counts as non-negative → 8 non-neg out of 10)
    df = pd.DataFrame({
        "Open": closes, "High": [c * 1.1 for c in closes],
        "Low":  [c * 0.9 for c in closes], "Close": closes,
        "Volume": [5_000_000] * 10,
    }, index=dates)
    ds = MockDataSource(history=df, snapshot={
        "current_price": 1.10, "avg_volume": 5_000_000,
    })
    result = GMRShort(ds).compute("TEST")
    # 8 non-negative out of 10 → boundary positive day rank = 8 → 8/10 = 0.80
    assert result.win_probability == pytest.approx(0.80, rel=1e-3)


# ---------------------------------------------------------------------------
# VUp / VDown formula validation
# ---------------------------------------------------------------------------

def test_v_up_formula_single_month():
    """
    5 days, one month.  For the day with the lowest intraday low, VUp should
    equal max_high_in_month / that_low − 1.

    Day data (all in 2024-07):
      Mon  High=1.40  Low=0.70  Close=1.00
      Tue  High=1.30  Low=0.80  Close=1.00
      Wed  High=1.50  Low=0.75  Close=1.00
      Thu  High=1.20  Low=0.90  Close=1.00
      Fri  High=1.10  Low=0.85  Close=1.00

    For Monday (low=0.70): max future high (same month, Mon onwards) = 1.50
      VUp = 1.50 / 0.70 − 1 = 1.1429…
    Monthly VUp = max over all days.
    """
    rows = [
        {"date": "2024-07-01", "Open": 1.0, "High": 1.40, "Low": 0.70, "Close": 1.0, "Volume": 5e6},
        {"date": "2024-07-02", "Open": 1.0, "High": 1.30, "Low": 0.80, "Close": 1.0, "Volume": 5e6},
        {"date": "2024-07-03", "Open": 1.0, "High": 1.50, "Low": 0.75, "Close": 1.0, "Volume": 5e6},
        {"date": "2024-07-04", "Open": 1.0, "High": 1.20, "Low": 0.90, "Close": 1.0, "Volume": 5e6},
        {"date": "2024-07-05", "Open": 1.0, "High": 1.10, "Low": 0.85, "Close": 1.0, "Volume": 5e6},
    ]
    df = _micro_history(rows)
    ds = MockDataSource(history=df, snapshot={
        "current_price": 1.0, "avg_volume": 5e6,
    })
    result = GMRShort(ds).compute("VTEST")
    # Monthly VUp = max VUp across all days:
    # Mon: max_high from Mon-Fri = 1.50, low_Mon = 0.70 → VUp = 1.50/0.70 - 1 = 1.1429
    expected_monthly_v_up = 1.50 / 0.70 - 1.0
    assert result.avg_v_up == pytest.approx(expected_monthly_v_up, rel=1e-3)


def test_v_down_formula_single_month():
    """
    Same 5-day dataset.  VDown formula: 1 − high_i / min_low_forward.

    For Monday (high=1.40): min future low (Mon onward) = min(0.70,0.80,0.75,0.90,0.85) = 0.70
      VDown(Mon) = 1 - 1.40/0.70 = 1 - 2.0 = -1.0

    Monthly VDown = min (most negative) across all days.
    For Friday (high=1.10): min future low = 0.85 → VDown = 1 - 1.10/0.85 ≈ -0.294
    For Tuesday: min future low = min(0.80,0.75,0.90,0.85) = 0.75 → VDown = 1 - 1.30/0.75 ≈ -0.733
    For Monday: VDown = 1 - 1.40/0.70 = -1.0  ← most negative
    """
    rows = [
        {"date": "2024-07-01", "Open": 1.0, "High": 1.40, "Low": 0.70, "Close": 1.0, "Volume": 5e6},
        {"date": "2024-07-02", "Open": 1.0, "High": 1.30, "Low": 0.80, "Close": 1.0, "Volume": 5e6},
        {"date": "2024-07-03", "Open": 1.0, "High": 1.50, "Low": 0.75, "Close": 1.0, "Volume": 5e6},
        {"date": "2024-07-04", "Open": 1.0, "High": 1.20, "Low": 0.90, "Close": 1.0, "Volume": 5e6},
        {"date": "2024-07-05", "Open": 1.0, "High": 1.10, "Low": 0.85, "Close": 1.0, "Volume": 5e6},
    ]
    df = _micro_history(rows)
    ds = MockDataSource(history=df, snapshot={
        "current_price": 1.0, "avg_volume": 5e6,
    })
    result = GMRShort(ds).compute("VDTEST")
    expected_monthly_v_down = 1.0 - 1.40 / 0.70   # = -1.0
    assert result.avg_v_down == pytest.approx(expected_monthly_v_down, rel=1e-3)


# ---------------------------------------------------------------------------
# MAT (43-day moving average) test
# ---------------------------------------------------------------------------

def test_mat_is_mean_of_43_most_recent_closes():
    """
    50 business days (all in 6-month window). Closes = 1.0, 2.0, … 50.0.
    MAT = mean of last 43 = mean(8.0 … 50.0).
    """
    dates  = pd.bdate_range("2024-07-01", periods=50)
    closes = np.arange(1.0, 51.0)
    df = pd.DataFrame({
        "Open": closes, "High": closes * 1.1, "Low": closes * 0.9,
        "Close": closes, "Volume": np.full(50, 5e6),
    }, index=dates)
    ds = MockDataSource(history=df, snapshot={
        "current_price": 50.0, "avg_volume": 5e6,
    })
    result = GMRShort(ds).compute("MAT")
    expected_mat = closes[-43:].mean()
    assert result.mat_43d == pytest.approx(expected_mat, rel=1e-6)


def test_diff_mat_formula():
    """diffMAT = (MAT − price) / price."""
    # Use 43 days all with close = 1.00 → MAT = 1.00, price = 1.05
    dates  = pd.bdate_range("2024-07-01", periods=43)
    closes = np.ones(43)
    df = pd.DataFrame({
        "Open": closes, "High": closes * 1.4, "Low": closes * 0.7,
        "Close": closes, "Volume": np.full(43, 5e6),
    }, index=dates)
    current_price = 1.05
    ds = MockDataSource(history=df, snapshot={
        "current_price": current_price, "avg_volume": 5e6,
    })
    result = GMRShort(ds).compute("MATF")
    expected_diff = (1.00 - current_price) / current_price  # ≈ -0.0476
    assert result.diff_mat_pct == pytest.approx(expected_diff, rel=1e-4)


# ---------------------------------------------------------------------------
# Failing scenarios — one threshold violated at a time
# ---------------------------------------------------------------------------

def test_fails_volume_below_min(passing_history):
    ds = MockDataSource(
        history=passing_history,
        snapshot={"current_price": 1.00, "avg_volume": 500_000},   # too low
    )
    result = GMRShort(ds).compute("LOWVOL")
    assert result.flags["volume"] is False
    assert result.passes_all is False


def test_fails_price_below_min(passing_history):
    ds = MockDataSource(
        history=passing_history,
        snapshot={"current_price": 0.30, "avg_volume": 5_000_000},  # below $0.40
    )
    result = GMRShort(ds).compute("CHEAP")
    assert result.flags["price_range"] is False
    assert result.passes_all is False


def test_fails_price_above_max(passing_history):
    ds = MockDataSource(
        history=passing_history,
        snapshot={"current_price": 5.00, "avg_volume": 5_000_000},  # above $2.50
    )
    result = GMRShort(ds).compute("EXPENSIVE")
    assert result.flags["price_range"] is False
    assert result.passes_all is False


def test_fails_win_probability_too_low():
    """30 % positive days → win prob ≈ 0.30, fails > 0.50."""
    hist = _make_history(n_days=130, positive_fraction=0.30, seed=7)
    ds   = MockDataSource(history=hist, snapshot=PASS_SNAPSHOT)
    result = GMRShort(ds).compute("LOSER")
    assert result.win_probability <= 0.50
    assert result.flags["win_prob"] is False
    assert result.passes_all is False


def test_fails_volatility_v_up_too_small():
    """Intraday range ±3 % → VUp well below 0.30."""
    hist = _make_history(n_days=130, high_factor=1.03, low_factor=0.97, seed=9)
    ds   = MockDataSource(history=hist, snapshot=PASS_SNAPSHOT)
    result = GMRShort(ds).compute("BORING")
    assert result.avg_v_up < 0.30
    assert result.flags["volatility"] is False
    assert result.passes_all is False


def test_fails_mat_price_too_far_above():
    """
    Price = $2.00 but all closes are $1.00 → MAT ≈ $1.00
    diffMAT = (1.00 - 2.00) / 2.00 = -0.50 << -0.025 → FAIL.
    """
    hist = _make_history(n_days=130, close=1.00)
    ds   = MockDataSource(
        history=hist,
        snapshot={"current_price": 2.00, "avg_volume": 5_000_000},
    )
    result = GMRShort(ds).compute("OVERMAT")
    assert result.diff_mat_pct < -0.025
    assert result.flags["mat"] is False
    assert result.passes_all is False


# ---------------------------------------------------------------------------
# Empty / degenerate input
# ---------------------------------------------------------------------------

def test_empty_history_returns_failed_result():
    ds = MockDataSource(
        history=pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"]),
        snapshot={"current_price": 1.0, "avg_volume": 5e6},
    )
    result = GMRShort(ds).compute("EMPTY")
    assert result.passes_all is False
    assert result.win_probability == 0.0
    assert result.monthly_breakdown.empty


def test_custom_settings_relax_price_range(passing_history):
    """Custom settings that widen the price range allow $5 stocks."""
    s  = GMRSettings(min_price=0.0, max_price=100.0)
    ds = MockDataSource(
        history=passing_history,
        snapshot={"current_price": 5.00, "avg_volume": 5_000_000},
    )
    result = GMRShort(ds, settings=s).compute("WIDEPRICE")
    assert result.flags["price_range"] is True
