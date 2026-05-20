"""
Data Health Endpoint — Unit Tests
===================================
Tests for GET /v1/health/data using a temporary filesystem (no real data
directories required) and the ``collect_data_health`` helper directly.
"""
from __future__ import annotations
# pylint: disable=missing-function-docstring,redefined-outer-name

import pytest
from starlette.testclient import TestClient

from src.api.app import app
from src.api.routers.health import collect_data_health


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def data_dirs(tmp_path):
    """
    Create a minimal fake data directory tree:

        edgar_dir/
            companyfacts/  CIK000001.json  CIK000002.json
            reference/     company_tickers.json
        price_dir/
            daily/         AAPL.csv  MSFT.csv
    """
    edgar = tmp_path / "edgar"
    cf = edgar / "companyfacts"
    cf.mkdir(parents=True)
    (cf / "CIK000001.json").write_text("{}")
    (cf / "CIK000002.json").write_text("{}")
    ref = edgar / "reference"
    ref.mkdir()
    (ref / "company_tickers.json").write_text("{}")

    prices = tmp_path / "prices"
    daily = prices / "daily"
    daily.mkdir(parents=True)
    aapl_rows = (
        "Date,Open,High,Low,Close,Volume\n"
        "2025-01-10,220,225,219,222,50000000\n"
        "2025-01-13,222,230,221,228,48000000\n"
    )
    msft_rows = (
        "Date,Open,High,Low,Close,Volume\n"
        "2025-01-10,410,415,409,413,20000000\n"
        "2025-01-13,413,420,412,418,18000000\n"
    )
    (daily / "AAPL.csv").write_text(aapl_rows)
    (daily / "MSFT.csv").write_text(msft_rows)

    return str(edgar), str(prices)


# ---------------------------------------------------------------------------
# collect_data_health unit tests (no HTTP, no env vars)
# ---------------------------------------------------------------------------

class TestCollectDataHealth:
    """Unit tests for collect_data_health helper (no HTTP, no env vars)."""

    def test_ok_status_when_both_stores_populated(self, data_dirs):
        edgar_dir, price_dir = data_dirs
        result = collect_data_health(edgar_dir, price_dir)
        assert result["status"] == "ok"

    def test_edgar_companyfacts_count(self, data_dirs):
        edgar_dir, price_dir = data_dirs
        result = collect_data_health(edgar_dir, price_dir)
        assert result["edgar"]["companyfacts_count"] == 2

    def test_edgar_reference_modified_is_iso_string(self, data_dirs):
        edgar_dir, price_dir = data_dirs
        result = collect_data_health(edgar_dir, price_dir)
        ts = result["edgar"]["reference_last_modified"]
        assert ts is not None
        assert "T" in ts and ts.endswith("Z")

    def test_price_csv_count(self, data_dirs):
        edgar_dir, price_dir = data_dirs
        result = collect_data_health(edgar_dir, price_dir)
        assert result["prices"]["csv_count"] == 2

    def test_price_newest_modified_is_iso_string(self, data_dirs):
        edgar_dir, price_dir = data_dirs
        result = collect_data_health(edgar_dir, price_dir)
        ts = result["prices"]["newest_file_modified"]
        assert ts is not None
        assert "T" in ts and ts.endswith("Z")

    def test_price_newest_date_extracted_from_csv(self, data_dirs):
        edgar_dir, price_dir = data_dirs
        result = collect_data_health(edgar_dir, price_dir)
        # one of the two CSVs should be newest; both end on 2025-01-13
        assert result["prices"]["newest_price_date"] == "2025-01-13"

    def test_empty_status_when_edgar_dir_missing(self, tmp_path):
        price_dir = tmp_path / "prices" / "daily"
        price_dir.mkdir(parents=True)
        (price_dir / "AAPL.csv").write_text("Date,Close\n2025-01-10,220\n")
        result = collect_data_health(str(tmp_path / "noedgar"), str(tmp_path / "prices"))
        assert result["status"] == "empty"
        assert result["edgar"]["companyfacts_count"] == 0

    def test_empty_status_when_price_dir_missing(self, tmp_path):
        edgar = tmp_path / "edgar" / "companyfacts"
        edgar.mkdir(parents=True)
        (edgar / "CIK000001.json").write_text("{}")
        result = collect_data_health(str(tmp_path / "edgar"), str(tmp_path / "noprice"))
        assert result["status"] == "empty"
        assert result["prices"]["csv_count"] == 0

    def test_none_values_when_both_dirs_missing(self, tmp_path):
        result = collect_data_health(str(tmp_path / "a"), str(tmp_path / "b"))
        assert result["edgar"]["reference_last_modified"] is None
        assert result["prices"]["newest_file_modified"] is None
        assert result["prices"]["newest_price_date"] is None


# ---------------------------------------------------------------------------
# HTTP endpoint tests
# ---------------------------------------------------------------------------

class TestDataHealthEndpoint:
    """HTTP endpoint tests for GET /v1/health/data."""

    def test_returns_200(self, client, data_dirs, monkeypatch):
        edgar_dir, price_dir = data_dirs
        monkeypatch.setenv("GMR_EDGAR_LOCAL_DATA_DIR", edgar_dir)
        monkeypatch.setenv("GMR_PRICE_DATA_DIR", price_dir)
        resp = client.get("/v1/health/data")
        assert resp.status_code == 200

    def test_response_has_required_keys(self, client, data_dirs, monkeypatch):
        edgar_dir, price_dir = data_dirs
        monkeypatch.setenv("GMR_EDGAR_LOCAL_DATA_DIR", edgar_dir)
        monkeypatch.setenv("GMR_PRICE_DATA_DIR", price_dir)
        body = client.get("/v1/health/data").json()
        assert "status" in body
        assert "edgar" in body
        assert "prices" in body

    def test_returns_ok_for_populated_dirs(self, client, data_dirs, monkeypatch):
        edgar_dir, price_dir = data_dirs
        monkeypatch.setenv("GMR_EDGAR_LOCAL_DATA_DIR", edgar_dir)
        monkeypatch.setenv("GMR_PRICE_DATA_DIR", price_dir)
        body = client.get("/v1/health/data").json()
        assert body["status"] == "ok"

    def test_returns_empty_for_missing_dirs(self, client, tmp_path, monkeypatch):
        monkeypatch.setenv("GMR_EDGAR_LOCAL_DATA_DIR", str(tmp_path / "no_edgar"))
        monkeypatch.setenv("GMR_PRICE_DATA_DIR", str(tmp_path / "no_prices"))
        body = client.get("/v1/health/data").json()
        assert body["status"] == "empty"

    def test_always_returns_200_even_when_dirs_missing(self, client, tmp_path, monkeypatch):
        monkeypatch.setenv("GMR_EDGAR_LOCAL_DATA_DIR", str(tmp_path / "x"))
        monkeypatch.setenv("GMR_PRICE_DATA_DIR", str(tmp_path / "y"))
        resp = client.get("/v1/health/data")
        assert resp.status_code == 200
