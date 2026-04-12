"""
Prices API — Unit Tests
========================
Tests for GET /{ticker}/prices using a mocked data source.
No network or filesystem access required.
"""
from __future__ import annotations
# pylint: disable=missing-function-docstring,redefined-outer-name

from unittest.mock import MagicMock

import pandas as pd
import pytest

from tests.dishka_fixtures import make_test_client, cleanup_dishka

# ---------------------------------------------------------------------------
# Shared fake data
# ---------------------------------------------------------------------------

_FAKE_TICKERS = [
    {
        "symbol": "AAPL", "name": "Apple Inc.", "exchange": "NASDAQ",
        "cik": "0000320193", "sic": "", "sic_description": "",
        "sector": "Technology", "industry": "Consumer Electronics",
        "country": "US", "currency": "USD", "is_active": True,
        "last_updated": "", "search_name": "apple inc. aapl",
        "search_keywords": "apple inc. aapl",
    },
]

# 10 OHLCV bars representing ~2 weeks of trading data.
_AAPL_BARS = pd.DataFrame(
    {
        "Open":   [170.0, 171.5, 169.0, 172.0, 173.5,
                   174.0, 172.5, 175.0, 176.0, 177.0],
        "High":   [172.0, 173.0, 171.0, 174.0, 175.0,
                   175.5, 174.0, 177.0, 178.0, 179.0],
        "Low":    [169.0, 170.5, 168.0, 171.0, 172.5,
                   173.0, 171.5, 174.5, 175.0, 176.5],
        "Close":  [171.5, 172.0, 170.0, 173.5, 174.0,
                   173.0, 173.5, 176.0, 177.5, 178.0],
        "Volume": [5e7, 4.5e7, 5.2e7, 4.8e7, 5.1e7,
                   4.7e7, 5.3e7, 4.9e7, 5.0e7, 4.6e7],
    },
    index=pd.to_datetime([
        "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05",
        "2024-01-08", "2024-01-09", "2024-01-10", "2024-01-11",
        "2024-01-12", "2024-01-16",
    ]),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def client():
    mock_ds = MagicMock()
    mock_ds.get_available_tickers.return_value = _FAKE_TICKERS
    mock_ds.get_price_history.return_value = _AAPL_BARS.copy()
    mock_ds.get_data_source_name.return_value = "edgar"
    yield make_test_client(data_source=mock_ds)
    cleanup_dishka()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_prices_aapl_returns_200(client):
    assert client.get("/AAPL/prices").status_code == 200


def test_prices_aapl_ticker_field(client):
    assert client.get("/AAPL/prices").json()["ticker"] == "AAPL"


def test_prices_aapl_name_field(client):
    assert client.get("/AAPL/prices").json()["name"] == "Apple Inc."


def test_prices_aapl_exchange_field(client):
    assert client.get("/AAPL/prices").json()["exchange"] == "NASDAQ"


def test_prices_aapl_period_echoed(client):
    body = client.get("/AAPL/prices?period=6m").json()
    assert body["period"] == "6m"


def test_prices_aapl_bars_non_empty(client):
    assert len(client.get("/AAPL/prices").json()["bars"]) > 0


def test_prices_aapl_bars_count(client):
    # Mock always returns the 10-bar DataFrame regardless of period.
    assert len(client.get("/AAPL/prices").json()["bars"]) == 10


def test_prices_aapl_bar_keys(client):
    first = client.get("/AAPL/prices").json()["bars"][0]
    for key in ("date", "open", "high", "low", "close", "volume"):
        assert key in first, f"Missing key '{key}' in bar"


def test_prices_aapl_bar_values_are_numeric(client):
    first = client.get("/AAPL/prices").json()["bars"][0]
    for key in ("open", "high", "low", "close", "volume"):
        assert isinstance(first[key], (int, float)), f"'{key}' is not numeric"


def test_prices_aapl_bar_dates_are_strings(client):
    for entry in client.get("/AAPL/prices").json()["bars"]:
        assert isinstance(entry["date"], str)
        # Should be YYYY-MM-DD
        assert len(entry["date"]) == 10


def test_prices_aapl_high_gte_low(client):
    for entry in client.get("/AAPL/prices").json()["bars"]:
        assert entry["high"] >= entry["low"], f"high < low on {entry['date']}"


def test_prices_aapl_volume_positive(client):
    for entry in client.get("/AAPL/prices").json()["bars"]:
        assert entry["volume"] > 0


def test_prices_lowercase_ticker_normalised(client):
    assert client.get("/aapl/prices").json()["ticker"] == "AAPL"


# ---------------------------------------------------------------------------
# Period query param validation
# ---------------------------------------------------------------------------

def test_prices_period_1m(client):
    assert client.get("/AAPL/prices?period=1m").json()["period"] == "1m"


def test_prices_period_6m(client):
    assert client.get("/AAPL/prices?period=6m").json()["period"] == "6m"


def test_prices_period_1y(client):
    assert client.get("/AAPL/prices?period=1y").json()["period"] == "1y"


def test_prices_period_3y(client):
    assert client.get("/AAPL/prices?period=3y").json()["period"] == "3y"


def test_prices_period_5y(client):
    assert client.get("/AAPL/prices?period=5y").json()["period"] == "5y"


def test_prices_period_all(client):
    assert client.get("/AAPL/prices?period=all").json()["period"] == "all"


def test_prices_unknown_period_falls_back_to_1y(client):
    body = client.get("/AAPL/prices?period=bogus").json()
    # The endpoint silently normalises unknown periods to 1y.
    assert body["period"] == "bogus"
    assert body["ticker"] == "AAPL"


# ---------------------------------------------------------------------------
# 404 — unknown ticker
# ---------------------------------------------------------------------------

@pytest.fixture()
def empty_client():
    """Client with a source that returns an empty DataFrame for any ticker."""
    mock_ds = MagicMock()
    mock_ds.get_available_tickers.return_value = []
    mock_ds.get_price_history.return_value = pd.DataFrame(
        columns=["Open", "High", "Low", "Close", "Volume"]
    )
    yield make_test_client(data_source=mock_ds)
    cleanup_dishka()


def test_prices_unknown_ticker_returns_404(empty_client):
    resp = empty_client.get("/ZZZNOTTHERE/prices")
    assert resp.status_code == 404


def test_prices_404_body_has_detail(empty_client):
    resp = empty_client.get("/ZZZNOTTHERE/prices")
    assert "detail" in resp.json()
