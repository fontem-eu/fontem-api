"""
Tickers API — Unit Tests
=========================
Tests for the /tickers/ API endpoints using a mocked data source.
No network, no filesystem access.
"""
from __future__ import annotations
# pylint: disable=missing-function-docstring,redefined-outer-name

from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from src.api.app import app
from src.api.dependencies import get_data_source

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
    {"symbol": "NVDA",  "cik": "0001045810", "name": "NVIDIA Corp",
     "sic": "", "sic_description": "", "exchange": "NASDAQ", "sector": "Technology",
     "industry": "Semiconductors", "country": "US", "currency": "USD",
     "is_active": True, "last_updated": "",
     "search_name": "nvidia corp nvda",
     "search_keywords": "nvidia corp nvda"},
    {"symbol": "IBM",   "cik": "0000051143", "name": "Intl Business Machines Corp",
     "sic": "", "sic_description": "", "exchange": "NYSE", "sector": "Technology",
     "industry": "IT Services", "country": "US", "currency": "USD",
     "is_active": True, "last_updated": "",
     "search_name": "intl business machines corp ibm",
     "search_keywords": "ibm intl business machines corp"},
]

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _mock_data_source():
    mock_ds = MagicMock()
    mock_ds.get_available_tickers.return_value = _FAKE_TICKERS
    mock_ds.search_tickers.side_effect = lambda q, limit=10: [
        t for t in _FAKE_TICKERS if q.lower() in t["search_name"]
    ][:limit]
    app.dependency_overrides[get_data_source] = lambda: mock_ds
    yield
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# LiveDataSource import smoke test
# ---------------------------------------------------------------------------

def test_live_data_source_imports_without_error():
    from src.data.live_data_source import LiveDataSource  # noqa: F401  # pylint: disable=import-outside-toplevel
    assert LiveDataSource is not None


# ---------------------------------------------------------------------------
# GET /tickers/
# ---------------------------------------------------------------------------

def test_list_tickers_returns_200(client):
    assert client.get("/tickers/").status_code == 200

def test_list_tickers_returns_list(client):
    assert isinstance(client.get("/tickers/").json(), list)

def test_list_tickers_non_empty(client):
    assert len(client.get("/tickers/").json()) > 0

def test_list_tickers_symbol_field_present(client):
    for item in client.get("/tickers/").json():
        assert "symbol" in item
        assert isinstance(item["symbol"], str)

def test_list_tickers_cik_field_present(client):
    for item in client.get("/tickers/").json():
        assert "cik" in item

def test_list_tickers_name_field_present(client):
    for item in client.get("/tickers/").json():
        assert "name" in item

def test_list_tickers_cik_is_not_row_index(client):
    body = client.get("/tickers/").json()
    row_index_ciks = {str(i).zfill(10) for i in range(5)}
    assert {item["cik"] for item in body}.isdisjoint(row_index_ciks)

def test_list_tickers_aapl_cik_correct(client):
    body = client.get("/tickers/").json()
    aapl = next(t for t in body if t["symbol"] == "AAPL")
    assert aapl["cik"] == "0000320193"

def test_list_tickers_pagination_limit(client):
    assert len(client.get("/tickers/?limit=2").json()) == 2

def test_list_tickers_pagination_offset(client):
    full = client.get("/tickers/").json()
    paged = client.get("/tickers/?offset=1").json()
    assert len(paged) == len(full) - 1
    assert paged[0]["symbol"] == full[1]["symbol"]


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
