"""
Integration tests for PriceFetcher (yfinance).
These hit the real yfinance API — run with network access.
"""
# pylint: disable=missing-function-docstring,redefined-outer-name,line-too-long
import pytest
import pandas as pd
from src.data.price_fetcher import PriceFetcher

TICKER = "AAPL"

@pytest.fixture(scope="module")
def fetcher():
    return PriceFetcher()

# ── get_history ────────────────────────────────────────────────────────────

def test_get_history_returns_dataframe(fetcher):
    df = fetcher.get_history(TICKER, period="3mo")
    assert isinstance(df, pd.DataFrame), "Expected a DataFrame"

def test_get_history_has_ohlcv_columns(fetcher):
    df = fetcher.get_history(TICKER, period="3mo")
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        assert col in df.columns, f"Missing column: {col}"

def test_get_history_has_rows(fetcher):
    df = fetcher.get_history(TICKER, period="3mo")
    assert len(df) > 10, f"Expected >10 rows, got {len(df)}"

def test_get_history_index_is_tz_naive(fetcher):
    df = fetcher.get_history(TICKER, period="3mo")
    assert df.index.tz is None, "Index should be tz-naive after fetch"

def test_get_history_close_prices_positive(fetcher):
    df = fetcher.get_history(TICKER, period="3mo")
    assert (df["Close"] > 0).all(), "All close prices should be positive"

def test_get_history_invalid_ticker_raises(fetcher):
    with pytest.raises(Exception):
        fetcher.get_history("NOTASTOCK_XYZ_INVALID_999", period="3mo")

# ── get_current_price ──────────────────────────────────────────────────────

def test_get_current_price_returns_positive_float(fetcher):
    price = fetcher.get_current_price(TICKER)
    assert isinstance(price, float), f"Expected float, got {type(price)}"
    assert price > 0, f"Price should be positive, got {price}"

# ── get_shares_outstanding ─────────────────────────────────────────────────

def test_get_shares_outstanding_returns_positive(fetcher):
    shares = fetcher.get_shares_outstanding(TICKER)
    # May be None if yfinance doesn't return it, but if it does it must be positive
    if shares is not None:
        assert shares > 0, f"Shares outstanding should be positive, got {shares}"

# ── get_dividend_yield ─────────────────────────────────────────────────────

def test_get_dividend_yield_returns_float(fetcher):
    dy = fetcher.get_dividend_yield(TICKER)
    assert isinstance(dy, float), f"Expected float, got {type(dy)}"
    assert dy >= 0, f"Dividend yield should be >= 0, got {dy}"

# ── get_avg_volume ─────────────────────────────────────────────────────────

def test_get_avg_volume_returns_positive(fetcher):
    vol = fetcher.get_avg_volume(TICKER)
    if vol is not None:
        assert vol > 0, f"Average volume should be positive, got {vol}"

# ── get_annual_avg_prices ──────────────────────────────────────────────────

def test_annual_avg_prices_is_series(fetcher):
    s = fetcher.get_annual_avg_prices(TICKER, period="5y")
    assert isinstance(s, pd.Series), f"Expected pd.Series, got {type(s)}"

def test_annual_avg_prices_not_empty(fetcher):
    s = fetcher.get_annual_avg_prices(TICKER, period="5y")
    assert not s.empty, "Annual avg prices series is empty"

def test_annual_avg_prices_index_is_integers(fetcher):
    s = fetcher.get_annual_avg_prices(TICKER, period="5y")
    assert s.index.dtype == int or all(isinstance(i, int) for i in s.index)

def test_annual_avg_prices_sorted_descending(fetcher):
    s = fetcher.get_annual_avg_prices(TICKER, period="5y")
    assert list(s.index) == sorted(s.index, reverse=True)

def test_annual_avg_prices_values_positive(fetcher):
    s = fetcher.get_annual_avg_prices(TICKER, period="5y")
    assert (s > 0).all(), "All annual average prices should be positive"

# ── get_annual_dividends ───────────────────────────────────────────────────

def test_annual_dividends_is_series(fetcher):
    s = fetcher.get_annual_dividends(TICKER)
    assert isinstance(s, pd.Series)

def test_annual_dividends_not_empty_for_aapl(fetcher):
    """AAPL has paid dividends since 2012."""
    s = fetcher.get_annual_dividends(TICKER)
    assert not s.empty, "AAPL annual dividends should not be empty"

def test_annual_dividends_values_positive(fetcher):
    s = fetcher.get_annual_dividends(TICKER)
    if not s.empty:
        assert (s > 0).all(), "Dividend amounts should be positive"

def test_annual_dividends_index_is_integers(fetcher):
    s = fetcher.get_annual_dividends(TICKER)
    if not s.empty:
        assert s.index.dtype == int or all(isinstance(i, int) for i in s.index)

# ── get_last_dividend ──────────────────────────────────────────────────────

def test_last_dividend_returns_dict(fetcher):
    d = fetcher.get_last_dividend(TICKER)
    assert isinstance(d, dict)

def test_last_dividend_has_expected_keys(fetcher):
    d = fetcher.get_last_dividend(TICKER)
    assert "date" in d and "amount" in d

def test_last_dividend_amount_positive_for_aapl(fetcher):
    d = fetcher.get_last_dividend(TICKER)
    assert d["amount"] > 0, f"AAPL last dividend amount should be positive: {d}"

def test_last_dividend_date_is_string(fetcher):
    d = fetcher.get_last_dividend(TICKER)
    if d["date"] is not None:
        assert isinstance(d["date"], str)

# ── get_splits ─────────────────────────────────────────────────────────────

def test_get_splits_returns_series(fetcher):
    s = fetcher.get_splits(TICKER)
    assert isinstance(s, pd.Series)

def test_get_splits_not_empty_for_aapl(fetcher):
    """AAPL has had multiple splits (most recently 4:1 in 2020)."""
    s = fetcher.get_splits(TICKER)
    assert not s.empty, "AAPL should have split history"

def test_get_splits_values_positive(fetcher):
    s = fetcher.get_splits(TICKER)
    if not s.empty:
        assert (s > 0).all(), "Split ratios should be positive"

def test_get_splits_index_is_integers(fetcher):
    s = fetcher.get_splits(TICKER)
    if not s.empty:
        assert s.index.dtype == int or all(isinstance(i, int) for i in s.index)

# ── get_latest_quarter ─────────────────────────────────────────────────────

def test_latest_quarter_returns_dict(fetcher):
    q = fetcher.get_latest_quarter(TICKER)
    assert isinstance(q, dict)

def test_latest_quarter_has_expected_keys(fetcher):
    q = fetcher.get_latest_quarter(TICKER)
    if q:  # may be empty if yfinance returns nothing
        for key in ["as_of", "current_assets", "current_liabilities",
                    "total_liabilities", "equity"]:
            assert key in q, f"Missing key '{key}' in latest_quarter"

def test_latest_quarter_as_of_is_string(fetcher):
    q = fetcher.get_latest_quarter(TICKER)
    if q and q.get("as_of"):
        assert isinstance(q["as_of"], str)

def test_latest_quarter_current_assets_positive(fetcher):
    q = fetcher.get_latest_quarter(TICKER)
    if q and q.get("current_assets") is not None:
        assert q["current_assets"] > 0

def test_latest_quarter_has_total_debt_key(fetcher):
    """total_debt must be present in the quarterly snapshot (may be None for debt-free companies)."""
    q = fetcher.get_latest_quarter(TICKER)
    if q:
        assert "total_debt" in q, "latest_quarter should include 'total_debt' key"

def test_latest_quarter_total_debt_positive_for_aapl(fetcher):
    """AAPL carries long-term debt — total_debt should be a large positive number."""
    q = fetcher.get_latest_quarter(TICKER)
    if q and q.get("total_debt") is not None:
        assert q["total_debt"] > 0, f"AAPL total_debt should be positive: {q['total_debt']}"
