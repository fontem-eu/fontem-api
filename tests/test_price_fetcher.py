"""
Integration tests for PriceFetcher (yfinance).
These hit the real yfinance API — run with network access.
"""
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
