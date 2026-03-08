"""
Unit tests for GET /{ticker}/gmr_short
========================================
All tests use a MockDataSource injected via FastAPI dependency overrides.
No network calls — sub-second execution.

Scenarios covered
-----------------
• 200 full response  (default, summarize=false)
• 200 summarised     (summarize=true)
• Response structure — top-level keys, monthly_breakdown shape
• Metric values      — win_probability, v_up, v_down, mat, diff_mat
• 404 ticker missing — ValueError from data source → 404
• 404 unknown ticker — empty current_price + empty breakdown → 404
• Ticker uppercasing — lowercase ticker in URL normalised in response
• monthly breakdown  — month field is a string, v_up/v_down are floats
"""
from __future__ import annotations

import numpy as np
import pytest
from starlette.testclient import TestClient

import pandas as pd

from src.analysis.gmr_data_source import GMRDataSource
from src.api.app import app
from src.api.dependencies import get_data_source

# ---------------------------------------------------------------------------
# OHLCV helper (reused from test_gmr_short.py)
# ---------------------------------------------------------------------------

def _make_history(
    n_days: int = 130,
    close: float = 1.00,
    high_factor: float = 1.40,
    low_factor: float = 0.70,
    positive_fraction: float = 0.65,
    volume: float = 5_000_000,
    seed: int = 42,
) -> pd.DataFrame:
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


# ---------------------------------------------------------------------------
# Mock data sources
# ---------------------------------------------------------------------------

class _GoodMock(GMRDataSource):
    """Volatile penny stock — passes all GMR Short thresholds."""
    _history = _make_history()

    def get_annual_fundamentals(self, t, y): return {}
    def get_annual_avg_prices(self, t, y):   return pd.Series(dtype=float)
    def get_annual_dividends(self, t):        return pd.Series(dtype=float)
    def get_price_history(self, t, period="1y"): return self._history
    def get_market_snapshot(self, t):
        return {"current_price": 1.00, "avg_volume": 5_000_000}


class _NotFoundMock(GMRDataSource):
    """Raises ValueError — unknown ticker."""
    def get_annual_fundamentals(self, t, y): return {}
    def get_annual_avg_prices(self, t, y):   return pd.Series(dtype=float)
    def get_annual_dividends(self, t):        return pd.Series(dtype=float)
    def get_price_history(self, t, period="1y"):
        raise ValueError(f"No price history for '{t}'")
    def get_market_snapshot(self, t):
        raise ValueError(f"Unknown ticker '{t}'")


class _EmptyMock(GMRDataSource):
    """Returns empty history + NaN price — triggers 404."""
    def get_annual_fundamentals(self, t, y): return {}
    def get_annual_avg_prices(self, t, y):   return pd.Series(dtype=float)
    def get_annual_dividends(self, t):        return pd.Series(dtype=float)
    def get_price_history(self, t, period="1y"):
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    def get_market_snapshot(self, t):
        return {"current_price": float("nan"), "avg_volume": 0}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client_good():
    app.dependency_overrides[get_data_source] = lambda: _GoodMock()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def client_not_found():
    app.dependency_overrides[get_data_source] = lambda: _NotFoundMock()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def client_empty():
    app.dependency_overrides[get_data_source] = lambda: _EmptyMock()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def full_resp(client_good):
    return client_good.get("/VOLT/gmr_short")


@pytest.fixture
def full_json(full_resp):
    return full_resp.json()


@pytest.fixture
def summary_json(client_good):
    return client_good.get("/VOLT/gmr_short?summarize=true").json()


# ---------------------------------------------------------------------------
# HTTP status codes
# ---------------------------------------------------------------------------

def test_full_returns_200(full_resp):
    assert full_resp.status_code == 200


def test_summary_returns_200(client_good):
    assert client_good.get("/VOLT/gmr_short?summarize=true").status_code == 200


def test_unknown_ticker_returns_404(client_not_found):
    resp = client_not_found.get("/FAKE999/gmr_short")
    assert resp.status_code == 404


def test_empty_history_returns_404(client_empty):
    resp = client_empty.get("/GHOST/gmr_short")
    assert resp.status_code == 404


def test_404_detail_is_informative(client_not_found):
    resp = client_not_found.get("/FAKE999/gmr_short")
    detail = resp.json()["detail"]
    assert detail  # non-empty


# ---------------------------------------------------------------------------
# Top-level structure
# ---------------------------------------------------------------------------

def test_top_level_keys_full(full_json):
    assert "ticker" in full_json
    assert "gmr_ratio" in full_json
    assert "market_snapshot" in full_json
    assert "monthly_breakdown" in full_json


def test_summary_only_has_ticker_and_ratio(summary_json):
    assert set(summary_json.keys()) == {"ticker", "gmr_ratio"}


def test_summary_missing_market_snapshot(summary_json):
    assert "market_snapshot" not in summary_json


def test_summary_missing_monthly_breakdown(summary_json):
    assert "monthly_breakdown" not in summary_json


# ---------------------------------------------------------------------------
# Ticker field
# ---------------------------------------------------------------------------

def test_ticker_uppercased(full_json):
    assert full_json["ticker"] == "VOLT"


def test_lowercase_url_ticker_uppercased(client_good):
    resp = client_good.get("/volt/gmr_short")
    assert resp.json()["ticker"] == "VOLT"


# ---------------------------------------------------------------------------
# gmr_ratio structure and values
# ---------------------------------------------------------------------------

def test_gmr_ratio_has_passes(full_json):
    assert "passes" in full_json["gmr_ratio"]


def test_gmr_ratio_passes_is_true(full_json):
    assert full_json["gmr_ratio"]["passes"] is True, full_json["gmr_ratio"]


def test_gmr_ratio_flags_keys(full_json):
    flags = set(full_json["gmr_ratio"]["flags"])
    assert flags == {"volume", "price_range", "win_prob", "volatility", "mat"}


def test_gmr_ratio_has_metric_fields(full_json):
    ratio = full_json["gmr_ratio"]
    for field in ("win_probability", "avg_v_up", "avg_v_down", "mat_43d", "diff_mat_pct"):
        assert field in ratio, f"Missing field: {field}"


def test_win_probability_above_half(full_json):
    assert full_json["gmr_ratio"]["win_probability"] > 0.5


def test_avg_v_up_above_threshold(full_json):
    assert full_json["gmr_ratio"]["avg_v_up"] > 0.30


def test_avg_v_down_below_threshold(full_json):
    assert full_json["gmr_ratio"]["avg_v_down"] < -0.30


def test_all_flags_true(full_json):
    for key, val in full_json["gmr_ratio"]["flags"].items():
        assert val is True, f"Flag '{key}' should be True"


# ---------------------------------------------------------------------------
# market_snapshot
# ---------------------------------------------------------------------------

def test_market_snapshot_current_price(full_json):
    assert full_json["market_snapshot"]["current_price"] == pytest.approx(1.00)


def test_market_snapshot_avg_volume(full_json):
    assert full_json["market_snapshot"]["avg_volume"] == pytest.approx(5_000_000)


# ---------------------------------------------------------------------------
# monthly_breakdown
# ---------------------------------------------------------------------------

def test_monthly_breakdown_is_list(full_json):
    assert isinstance(full_json["monthly_breakdown"], list)


def test_monthly_breakdown_has_six_entries(full_json):
    # 130 bdays ending Dec-2024 → 6-month active window → 6 months
    assert len(full_json["monthly_breakdown"]) == 6


def test_monthly_breakdown_entry_has_month_v_up_v_down(full_json):
    entry = full_json["monthly_breakdown"][0]
    assert "month" in entry
    assert "v_up" in entry
    assert "v_down" in entry


def test_monthly_breakdown_month_is_string(full_json):
    for entry in full_json["monthly_breakdown"]:
        assert isinstance(entry["month"], str)
        # Should look like "YYYY-MM"
        assert len(entry["month"]) == 7


def test_monthly_breakdown_v_up_positive(full_json):
    for entry in full_json["monthly_breakdown"]:
        if entry["v_up"] is not None:
            assert entry["v_up"] > 0


def test_monthly_breakdown_v_down_negative(full_json):
    for entry in full_json["monthly_breakdown"]:
        if entry["v_down"] is not None:
            assert entry["v_down"] < 0
