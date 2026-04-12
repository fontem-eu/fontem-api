"""
Tickers API — Unit Tests
=========================
Tests for GET /tickers/search using a mocked data source.
No network, no filesystem access.
"""
from __future__ import annotations
# pylint: disable=missing-function-docstring,redefined-outer-name

from unittest.mock import MagicMock

import pytest

from tests.dishka_fixtures import make_test_client, cleanup_dishka

# ---------------------------------------------------------------------------
# Shared fake data
# ---------------------------------------------------------------------------

_FAKE_TICKERS = [
    {"symbol": "AAPL",  "cik": "0000320193", "name": "Apple Inc.",
     "sic": "", "sic_description": "", "exchange": "NASDAQ", "sector": "Technology",
     "industry": "Consumer Electronics", "country": "US", "currency": "USD",
     "is_active": True, "last_updated": "",
     "search_name": "apple inc. aapl",
     "search_keywords": "apple inc. aapl"},
    {"symbol": "MSFT",  "cik": "0000789019", "name": "Microsoft Corp",
     "sic": "", "sic_description": "", "exchange": "NASDAQ", "sector": "Technology",
     "industry": "Software", "country": "US", "currency": "USD",
     "is_active": True, "last_updated": "",
     "search_name": "microsoft corp msft",
     "search_keywords": "microsoft corp msft"},
    {"symbol": "TSLA",  "cik": "0001318605", "name": "Tesla Inc.",
     "sic": "", "sic_description": "", "exchange": "NASDAQ", "sector": "Automotive",
     "industry": "EVs", "country": "US", "currency": "USD",
     "is_active": True, "last_updated": "",
     "search_name": "tesla inc. tsla",
     "search_keywords": "tesla inc. tsla"},
]

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def client():
    mock_ds = MagicMock()
    mock_ds.get_available_tickers.return_value = _FAKE_TICKERS
    mock_ds.search_tickers.side_effect = lambda q, limit=10: [
        t for t in _FAKE_TICKERS if q.lower() in t["search_name"]
    ][:limit]
    yield make_test_client(data_source=mock_ds)
    cleanup_dishka()




# ---------------------------------------------------------------------------
# GET /tickers/search
# ---------------------------------------------------------------------------

def test_search_tickers_returns_200(client):
    assert client.get("/tickers/search?query=apple").status_code == 200

def test_search_tickers_response_shape(client):
    body = client.get("/tickers/search?query=apple").json()
    for field in ("query", "results", "count", "total_available"):
        assert field in body

def test_search_tickers_query_echoed(client):
    body = client.get("/tickers/search?query=apple").json()
    assert body["query"] == "apple"

def test_search_tickers_count_matches_results(client):
    body = client.get("/tickers/search?query=apple").json()
    assert body["count"] == len(body["results"])

def test_search_tickers_returns_matching_ticker(client):
    body = client.get("/tickers/search?query=apple").json()
    assert any(t["symbol"] == "AAPL" for t in body["results"])

def test_search_tickers_total_available_reflects_full_list(client):
    body = client.get("/tickers/search?query=apple").json()
    assert body["total_available"] == len(_FAKE_TICKERS)

def test_search_tickers_missing_query_returns_422(client):
    assert client.get("/tickers/search").status_code == 422

def test_search_tickers_limit_param(client):
    body = client.get("/tickers/search?query=a&limit=1").json()
    assert body["count"] <= 1
