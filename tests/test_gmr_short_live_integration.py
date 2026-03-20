"""
Integration tests that reproduce the exact inner flow the e2e test exercises.

The e2e failure is:
  test_e2e_aapl_gmr_long_* (9 tests) PASS  →  test_e2e_aapl_gmr_short_* FAIL

The failing tests share one module-scoped TestClient, meaning the LiveDataSource
singleton (via @lru_cache) is shared and the FakeRedisCache accumulates state from
the GMR Long requests BEFORE the first GMR Short request arrives.

These tests drill into each layer individually to find where the KeyError:'Close'
actually originates.
"""
from __future__ import annotations
# pylint: disable=missing-function-docstring,redefined-outer-name,line-too-long

import pytest
import pandas as pd
from starlette.testclient import TestClient

from src.data.price_fetcher import PriceFetcher
from src.data.live_data_source import LiveDataSource
from src.analysis.gmr_short import GMRShort
from src.api.app import app


# ---------------------------------------------------------------------------
# Layer 1 — PriceFetcher.get_history() column stability
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def fetcher():
    return PriceFetcher()


def test_get_history_columns_after_10y_download(fetcher):
    """
    Reproduce the sequence GMR Long uses before GMR Short runs:
    first download 10y (annual avg prices), then download 1y (GMR Short).
    Both must return Title-Case columns.
    """
    df_10y = fetcher.get_history("AAPL", period="10y")
    assert "Close" in df_10y.columns, f"10y download missing 'Close'; got: {df_10y.columns.tolist()}"

    df_1y = fetcher.get_history("AAPL", period="1y")
    assert "Close" in df_1y.columns, f"1y download (after 10y) missing 'Close'; got: {df_1y.columns.tolist()}"
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        assert col in df_1y.columns, f"Missing '{col}' after sequence download; got: {df_1y.columns.tolist()}"


def test_get_history_columns_after_5d_download(fetcher):
    """
    After a 5d download (market snapshot current_price), a 1y download
    must still return Title-Case columns.
    """
    fetcher.get_history("AAPL", period="5d")   # warm/prime like market snapshot
    df_1y = fetcher.get_history("AAPL", period="1y")
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        assert col in df_1y.columns, f"Missing '{col}'; got: {df_1y.columns.tolist()}"


def test_get_history_columns_full_gmr_long_then_short_sequence(fetcher):
    """
    Full sequence that happens when the module-scoped client runs:
      1. get_history 10y  (annual avg prices for GMR Long)
      2. get_history 5d   (current_price inside market snapshot for GMR Long)
      3. get_history 1y   (price_history for GMR Short)
    The 1y result must always have Title-Case OHLCV columns.
    """
    fetcher.get_history("AAPL", period="10y")
    fetcher.get_history("AAPL", period="5d")
    df_1y = fetcher.get_history("AAPL", period="1y")
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        assert col in df_1y.columns, (
            f"Missing '{col}' in 1y result after full sequence; "
            f"got columns: {df_1y.columns.tolist()}"
        )


# ---------------------------------------------------------------------------
# Layer 2 — LiveDataSource.get_price_history() after get_market_snapshot()
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def live_ds():
    """Fresh LiveDataSource with its own FakeRedisCache."""
    return LiveDataSource()


def test_live_ds_price_history_after_market_snapshot(live_ds):
    """
    Mimics the EXACT call order GMRLong.compute() uses:
      get_annual_fundamentals → get_annual_avg_prices → get_annual_dividends → get_market_snapshot
    Then what GMRShort does: get_price_history(period="1y")
    """
    # Exactly what GMRLong.compute() calls (in order):
    live_ds.get_annual_fundamentals("AAPL")    # EDGAR — no yfinance
    live_ds.get_annual_avg_prices("AAPL")      # yf.download(period="10y")
    live_ds.get_annual_dividends("AAPL")       # yf.Ticker().dividends  ← THIS WAS MISSING
    live_ds.get_market_snapshot("AAPL")        # yf.Ticker().info + dividends + splits etc.

    # Now do what GMR Short does
    hist = live_ds.get_price_history("AAPL", period="1y")
    assert isinstance(hist, pd.DataFrame), "get_price_history should return a DataFrame"
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        assert col in hist.columns, (
            f"Missing '{col}' in LiveDataSource.get_price_history result "
            f"after full GMR Long call sequence; got: {hist.columns.tolist()}"
        )


def test_live_ds_price_history_second_call_uses_cache(live_ds):
    """
    A second call to get_price_history with the same key returns the cached
    DataFrame intact — columns must still be Title-Case.
    """
    _hist1 = live_ds.get_price_history("AAPL", period="1y")
    hist2 = live_ds.get_price_history("AAPL", period="1y")  # should be cache hit
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        assert col in hist2.columns, (
            f"Cached DataFrame missing '{col}'; got: {hist2.columns.tolist()}"
        )


# ---------------------------------------------------------------------------
# Layer 3 — GMRShort.compute() with real LiveDataSource
# ---------------------------------------------------------------------------

def test_gmr_short_compute_after_gmr_long_cache_population(live_ds):
    """
    Runs GMRShort.compute() on a LiveDataSource that already has the market
    snapshot and annual prices cached (exactly as happens in the e2e scenario).
    Must not raise KeyError and must return a valid result.
    """
    result = GMRShort(live_ds).compute("AAPL")
    assert result.ticker == "AAPL"
    assert not result.monthly_breakdown.empty, "monthly_breakdown should not be empty"
    assert 0.0 <= result.win_probability <= 1.0, f"win_probability out of range: {result.win_probability}"


# ---------------------------------------------------------------------------
# Layer 4 — Exact e2e scenario: TestClient(app) GMR Long then GMR Short
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def shared_client():
    """
    Module-scoped client — exactly like test_api_e2e.py — so the
    _live_source() singleton is shared across all tests in this module.
    """
    with TestClient(app) as c:
        yield c


def test_app_gmr_long_aapl_returns_200(shared_client):
    """Hit GMR Long first to populate the _live_source() cache."""
    resp = shared_client.get("/AAPL/gmr_long")
    assert resp.status_code == 200, f"GMR Long failed: {resp.text}"


def test_app_gmr_long_aapl_hit_multiple_times(shared_client):
    """Hit it several more times (like the 9 e2e GMR Long tests)."""
    for _ in range(4):
        resp = shared_client.get("/AAPL/gmr_long")
        assert resp.status_code == 200


def test_app_gmr_short_aapl_returns_200_after_gmr_long(shared_client):
    """
    GMR Short must succeed even after the GMR Long endpoint has already
    been called and populated the _live_source() cache.
    This is the exact scenario that fails in test_api_e2e.py.
    """
    resp = shared_client.get("/AAPL/gmr_short")
    assert resp.status_code == 200, f"GMR Short failed after GMR Long: {resp.text}"


def test_app_gmr_short_aapl_has_expected_keys(shared_client):
    body = shared_client.get("/AAPL/gmr_short").json()
    assert "ticker" in body
    assert "gmr_ratio" in body
    assert "monthly_breakdown" in body
