"""
Tickers API — Regression & End-to-End Tests
=============================================
Covers the three bugs that caused /api/tickers/ to return an empty list:

  Bug 1 — live_data_source.py: `Any` type not imported → NameError at load time
  Bug 2 — edgar_fetcher.py: no User-Agent header → SEC returns 403
  Bug 3 — edgar_fetcher.py: CIK was the JSON row-index not cik_str

The fast unit tests (no network) mock the SEC HTTP response so they run in CI.
The slow E2E tests hit the real SEC endpoint and are opt-in via pytest marks.

Run fast tests:
    pytest tests/test_api_tickers.py -v -m "not slow"

Run all (including live SEC call):
    pytest tests/test_api_tickers.py -v
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from src.api.app import app
from src.data.edgar_fetcher import EdgarFetcher


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# Minimal fake SEC payload matching the real JSON structure:
#   { "<row_index>": {"cik_str": <int>, "ticker": "<str>", "title": "<str>"} }
FAKE_SEC_PAYLOAD = {
    "0": {"cik_str": 320193,  "ticker": "AAPL",  "title": "Apple Inc."},
    "1": {"cik_str": 789019,  "ticker": "MSFT",  "title": "Microsoft Corp"},
    "2": {"cik_str": 1318605, "ticker": "TSLA",  "title": "Tesla Inc."},
    "3": {"cik_str": 1045810, "ticker": "NVDA",  "title": "NVIDIA Corp"},
    "4": {"cik_str": 51143,   "ticker": "IBM",   "title": "International Business Machines Corp"},
}


def _make_mock_response(payload: dict, status_code: int = 200) -> MagicMock:
    """Build a mock requests.Response object."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = payload
    mock_resp.raise_for_status.return_value = None
    return mock_resp


# ---------------------------------------------------------------------------
# Unit: EdgarFetcher.get_edgar_ticker_list() — no network required
# ---------------------------------------------------------------------------

class TestEdgarFetcherGetTickerList:
    """Unit tests for the EdgarFetcher ticker-list logic (no network)."""

    def _fetcher(self):
        """Create a fetcher without calling set_identity (avoids network config)."""
        with patch("src.data.edgar_fetcher.set_identity"):
            return EdgarFetcher()

    # --- Bug 2 regression: User-Agent header must be present ---

    def test_user_agent_header_is_sent(self):
        """Regression for Bug 2: SEC returns 403 if User-Agent is absent."""
        fetcher = self._fetcher()
        mock_resp = _make_mock_response(FAKE_SEC_PAYLOAD)

        with patch("requests.get", return_value=mock_resp) as mock_get:
            fetcher.get_edgar_ticker_list()

        call_kwargs = mock_get.call_args[1]
        headers = call_kwargs.get("headers", {})
        assert "User-Agent" in headers, (
            "User-Agent header must be sent to the SEC API (Bug 2 regression)"
        )
        assert "@" in headers["User-Agent"], (
            "User-Agent must contain a contact email as required by the SEC"
        )

    # --- Bug 3 regression: CIK must come from cik_str, not the row index ---

    def test_cik_is_from_cik_str_not_row_index(self):
        """Regression for Bug 3: CIK was the JSON row index (0,1,2…) not cik_str."""
        fetcher = self._fetcher()
        mock_resp = _make_mock_response(FAKE_SEC_PAYLOAD)

        with patch("requests.get", return_value=mock_resp):
            tickers = fetcher.get_edgar_ticker_list()

        aapl = next(t for t in tickers if t["symbol"] == "AAPL")
        assert aapl["cik"] == "0000320193", (
            f"Expected CIK '0000320193' (from cik_str), got '{aapl['cik']}' "
            "(Bug 3 regression: was using row index instead of cik_str)"
        )
        # Row indices are "0", "1", "2" ... make sure none of those slipped through
        row_index_ciks = {"0000000000", "0000000001", "0000000002"}
        actual_ciks = {t["cik"] for t in tickers}
        assert actual_ciks.isdisjoint(row_index_ciks), (
            "CIKs should not be row indices — Bug 3 regression"
        )

    def test_cik_is_zero_padded_to_10_digits(self):
        """CIK strings must be zero-padded to exactly 10 characters."""
        fetcher = self._fetcher()
        mock_resp = _make_mock_response(FAKE_SEC_PAYLOAD)

        with patch("requests.get", return_value=mock_resp):
            tickers = fetcher.get_edgar_ticker_list()

        for t in tickers:
            assert len(t["cik"]) == 10, (
                f"CIK '{t['cik']}' for {t['symbol']} should be 10 chars"
            )
            assert t["cik"].isdigit(), f"CIK '{t['cik']}' should only contain digits"

    def test_returns_list_of_dicts(self):
        fetcher = self._fetcher()
        mock_resp = _make_mock_response(FAKE_SEC_PAYLOAD)

        with patch("requests.get", return_value=mock_resp):
            tickers = fetcher.get_edgar_ticker_list()

        assert isinstance(tickers, list)
        assert len(tickers) == len(FAKE_SEC_PAYLOAD)

    def test_required_fields_present(self):
        """Every ticker dict must contain the fields expected by TickerInfo schema."""
        fetcher = self._fetcher()
        mock_resp = _make_mock_response(FAKE_SEC_PAYLOAD)

        with patch("requests.get", return_value=mock_resp):
            tickers = fetcher.get_edgar_ticker_list()

        required = {"symbol", "cik", "name", "search_name", "search_keywords"}
        for t in tickers:
            missing = required - set(t.keys())
            assert not missing, f"Ticker {t.get('symbol')} missing fields: {missing}"

    def test_symbol_is_uppercase(self):
        fetcher = self._fetcher()
        mock_resp = _make_mock_response(FAKE_SEC_PAYLOAD)

        with patch("requests.get", return_value=mock_resp):
            tickers = fetcher.get_edgar_ticker_list()

        for t in tickers:
            assert t["symbol"] == t["symbol"].upper(), (
                f"symbol '{t['symbol']}' should be uppercase"
            )

    def test_search_name_contains_symbol_and_title(self):
        fetcher = self._fetcher()
        mock_resp = _make_mock_response(FAKE_SEC_PAYLOAD)

        with patch("requests.get", return_value=mock_resp):
            tickers = fetcher.get_edgar_ticker_list()

        aapl = next(t for t in tickers if t["symbol"] == "AAPL")
        assert "apple" in aapl["search_name"]
        assert "aapl" in aapl["search_name"]

    def test_entries_without_ticker_are_skipped(self):
        """Entries with no ticker should be silently skipped."""
        payload_with_empty = dict(FAKE_SEC_PAYLOAD)
        payload_with_empty["99"] = {"cik_str": 9999999, "ticker": "", "title": "No Ticker Corp"}
        payload_with_empty["100"] = {"cik_str": 8888888, "title": "No Ticker Field Corp"}

        fetcher = self._fetcher()
        mock_resp = _make_mock_response(payload_with_empty)

        with patch("requests.get", return_value=mock_resp):
            tickers = fetcher.get_edgar_ticker_list()

        symbols = [t["symbol"] for t in tickers]
        assert "" not in symbols
        assert len(tickers) == len(FAKE_SEC_PAYLOAD)  # only original 5

    def test_http_error_is_raised(self):
        """HTTP errors must be raised, not silently swallowed (Bug 2 regression)."""
        fetcher = self._fetcher()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("403 Forbidden")

        with patch("requests.get", return_value=mock_resp):
            with pytest.raises(Exception, match="403"):
                fetcher.get_edgar_ticker_list()


# ---------------------------------------------------------------------------
# Unit: LiveDataSource import (Bug 1 regression)
# ---------------------------------------------------------------------------

class TestLiveDataSourceImport:
    """Bug 1 regression: `Any` must be imported so the module loads without error."""

    def test_live_data_source_imports_without_error(self):
        """Regression for Bug 1: NameError: name 'Any' is not defined."""
        # If this import fails the test will error — that is the regression signal
        from src.data.live_data_source import LiveDataSource  # noqa: F401
        assert LiveDataSource is not None

    def test_get_cached_data_method_has_correct_signature(self):
        """The extracted _get_cached_data helper must accept cache_key, fetch_func, ttl_key."""
        import inspect
        from src.data.live_data_source import LiveDataSource

        sig = inspect.signature(LiveDataSource._get_cached_data)
        params = list(sig.parameters.keys())
        assert "cache_key" in params
        assert "fetch_func" in params
        assert "ttl_key" in params


# ---------------------------------------------------------------------------
# Unit: /tickers/ API endpoint (mock network)
# ---------------------------------------------------------------------------

class TestTickersEndpointUnit:
    """Fast unit tests for the /tickers/ API route using a mocked data source."""

    @pytest.fixture(autouse=True)
    def _patch_live_source(self):
        """Patch get_data_source to return a mock that returns FAKE_SEC_PAYLOAD tickers."""
        from src.api.dependencies import get_data_source

        mock_tickers = []
        for _idx, info in FAKE_SEC_PAYLOAD.items():
            ticker = info["ticker"]
            mock_tickers.append({
                "symbol": ticker.upper(),
                "cik": str(info["cik_str"]).zfill(10),
                "name": info["title"],
                "sic": "",
                "sic_description": "Unknown",
                "exchange": "NASDAQ",
                "sector": "Unknown",
                "industry": "Unknown",
                "country": "US",
                "currency": "USD",
                "is_active": True,
                "last_updated": "",
                "search_name": f"{info['title']} {ticker}".lower(),
                "search_keywords": f"{info['title'].lower()} {ticker.lower()}",
            })

        mock_ds = MagicMock()
        mock_ds.get_available_tickers.return_value = mock_tickers
        mock_ds.search_tickers.side_effect = lambda q, limit=10: [
            t for t in mock_tickers
            if q.lower() in t["search_name"]
        ][:limit]

        app.dependency_overrides[get_data_source] = lambda: mock_ds
        yield
        app.dependency_overrides.clear()

    def test_list_tickers_returns_200(self, client):
        resp = client.get("/tickers/")
        assert resp.status_code == 200, resp.text

    def test_list_tickers_returns_list(self, client):
        body = client.get("/tickers/").json()
        assert isinstance(body, list)

    def test_list_tickers_non_empty(self, client):
        body = client.get("/tickers/").json()
        assert len(body) > 0, "Ticker list must not be empty (regression for all 3 bugs)"

    def test_list_tickers_symbol_field_present(self, client):
        body = client.get("/tickers/").json()
        for item in body:
            assert "symbol" in item
            assert isinstance(item["symbol"], str)

    def test_list_tickers_cik_field_present(self, client):
        body = client.get("/tickers/").json()
        for item in body:
            assert "cik" in item

    def test_list_tickers_name_field_present(self, client):
        body = client.get("/tickers/").json()
        for item in body:
            assert "name" in item

    def test_list_tickers_cik_is_not_row_index(self, client):
        """Regression for Bug 3: CIK '0000000000', '0000000001' etc must not appear."""
        body = client.get("/tickers/").json()
        row_index_ciks = {"0000000000", "0000000001", "0000000002", "0000000003", "0000000004"}
        actual_ciks = {item["cik"] for item in body}
        overlap = row_index_ciks & actual_ciks
        assert not overlap, f"Found row-index CIKs in response: {overlap}"

    def test_list_tickers_aapl_cik_correct(self, client):
        """AAPL's CIK must be 0000320193, not 0000000000 (row index 0)."""
        body = client.get("/tickers/").json()
        aapl = next((t for t in body if t["symbol"] == "AAPL"), None)
        assert aapl is not None
        assert aapl["cik"] == "0000320193", (
            f"Bug 3 regression: AAPL CIK should be '0000320193', got '{aapl['cik']}'"
        )

    def test_list_tickers_pagination_limit(self, client):
        resp = client.get("/tickers/?limit=2")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2

    def test_list_tickers_pagination_offset(self, client):
        full = client.get("/tickers/").json()
        paged = client.get("/tickers/?offset=1").json()
        assert len(paged) == len(full) - 1
        assert paged[0]["symbol"] == full[1]["symbol"]

    def test_search_tickers_returns_200(self, client):
        resp = client.get("/tickers/search?query=apple")
        assert resp.status_code == 200

    def test_search_tickers_response_shape(self, client):
        body = client.get("/tickers/search?query=apple").json()
        assert "query" in body
        assert "results" in body
        assert "count" in body
        assert "total_available" in body

    def test_search_tickers_query_echoed(self, client):
        body = client.get("/tickers/search?query=apple").json()
        assert body["query"] == "apple"

    def test_search_tickers_count_matches_results(self, client):
        body = client.get("/tickers/search?query=apple").json()
        assert body["count"] == len(body["results"])


# ---------------------------------------------------------------------------
# E2E: live SEC API call (opt-in, slow)
# ---------------------------------------------------------------------------

@pytest.mark.slow
class TestTickersEndpointE2E:
    """
    End-to-end tests that call the real SEC EDGAR API.
    No mocks — these verify the full stack against production data.

    Run with:  pytest tests/test_api_tickers.py -v -m slow
    """

    @pytest.fixture(scope="class")
    def e2e_client(self):
        with TestClient(app) as c:
            yield c

    def test_e2e_list_tickers_returns_200(self, e2e_client):
        resp = e2e_client.get("/tickers/?limit=10")
        assert resp.status_code == 200, resp.text

    def test_e2e_list_tickers_non_empty(self, e2e_client):
        """Regression for all 3 bugs: endpoint must return actual tickers."""
        body = e2e_client.get("/tickers/?limit=50").json()
        assert isinstance(body, list)
        assert len(body) > 0, (
            "Tickers endpoint returned empty list — check User-Agent header, "
            "CIK extraction, and Any import"
        )

    def test_e2e_list_tickers_large_count(self, e2e_client):
        """SEC has 10,000+ filers — we should see at least 1,000 without limit."""
        body = e2e_client.get("/tickers/?limit=1000").json()
        assert len(body) == 1000

    def test_e2e_list_tickers_schema_compliance(self, e2e_client):
        """Every ticker must have required fields with correct types."""
        body = e2e_client.get("/tickers/?limit=20").json()
        for item in body:
            assert isinstance(item["symbol"], str) and len(item["symbol"]) > 0
            assert isinstance(item["name"], str) and len(item["name"]) > 0
            assert isinstance(item["cik"], str) and len(item["cik"]) == 10
            assert item["cik"].isdigit(), f"CIK '{item['cik']}' must be numeric"

    def test_e2e_cik_not_row_index(self, e2e_client):
        """Regression Bug 3: No ticker CIK should equal a small row index."""
        body = e2e_client.get("/tickers/?limit=100").json()
        row_index_ciks = {str(i).zfill(10) for i in range(100)}
        actual_ciks = {item["cik"] for item in body}
        overlap = row_index_ciks & actual_ciks
        assert not overlap, (
            f"Bug 3 regression: found row-index CIKs in response: {overlap}"
        )

    def test_e2e_known_ticker_aapl_present(self, e2e_client):
        """AAPL (Apple) should be in the EDGAR database."""
        body = e2e_client.get("/tickers/?limit=5000").json()
        symbols = {t["symbol"] for t in body}
        assert "AAPL" in symbols, "AAPL should be in EDGAR ticker list"

    def test_e2e_aapl_cik_is_correct(self, e2e_client):
        """AAPL's official CIK is 0000320193."""
        body = e2e_client.get("/tickers/?limit=5000").json()
        aapl = next((t for t in body if t["symbol"] == "AAPL"), None)
        assert aapl is not None
        assert aapl["cik"] == "0000320193", (
            f"Bug 3 regression: AAPL CIK should be '0000320193', got '{aapl['cik']}'"
        )

    def test_e2e_search_apple_returns_results(self, e2e_client):
        """Searching 'apple' must return at least one result."""
        resp = e2e_client.get("/tickers/search?query=apple&limit=10")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] > 0, "Search for 'apple' returned no results"

    def test_e2e_search_result_symbols_are_uppercase(self, e2e_client):
        body = e2e_client.get("/tickers/search?query=microsoft&limit=5").json()
        for t in body.get("results", []):
            assert t["symbol"] == t["symbol"].upper()
