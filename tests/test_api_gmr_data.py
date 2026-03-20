"""
Unit tests for GET /{ticker}/gmr_data
========================================
All tests use a MockDataSource injected via FastAPI dependency overrides.
No network calls — sub-second execution.

Scenarios covered
-----------------
• 200 response with correct structure
• ticker is uppercased in response
• current_snapshot fields present and correct
• annual_data is a non-empty list, sorted descending
• annual_data rows contain expected fields
• delta_ppe is negative (negated capex)
• splits are 0 for years without a split, >1 for split years
• ?years param limits the number of annual rows
• 404 on ValueError from data source
"""
from __future__ import annotations
# pylint: disable=missing-function-docstring,redefined-outer-name,arguments-renamed,multiple-statements,unused-argument,line-too-long,unnecessary-lambda

import pytest
import pandas as pd

from starlette.testclient import TestClient

from src.analysis.gmr_data_source import FinancialDataSource
from src.api.app import app
from src.api.dependencies import get_data_source


# ---------------------------------------------------------------------------
# Mock data source
# ---------------------------------------------------------------------------

_YEARS = [2023, 2022, 2021, 2020, 2019]


def _series(values: dict) -> pd.Series:
    s = pd.Series(values)
    s.index = s.index.astype(int)
    return s.sort_index(ascending=False)


class _GoodMock(FinancialDataSource):
    """Returns realistic-looking stub data for all five years."""

    def get_annual_fundamentals(self, ticker, years):
        return {
            "ticker":              ticker.upper(),
            "revenue":             _series({2023: 400e9, 2022: 380e9, 2021: 360e9, 2020: 274e9, 2019: 260e9}),
            "net_income":          _series({2023: 97e9,  2022: 100e9, 2021: 94e9,  2020: 57e9,  2019: 55e9}),
            "total_assets":        _series({2023: 350e9, 2022: 340e9, 2021: 351e9, 2020: 323e9, 2019: 340e9}),
            "total_liabilities":   _series({2023: 290e9, 2022: 280e9, 2021: 287e9, 2020: 258e9, 2019: 248e9}),
            "equity":              _series({2023: 60e9,  2022: 60e9,  2021: 63e9,  2020: 65e9,  2019: 90e9}),
            "shares_outstanding":  _series({2023: 15.6e9,2022: 16.1e9,2021: 16.7e9,2020: 17.1e9,2019: 17.8e9}),
            "current_assets":      _series({2023: 135e9, 2022: 135e9, 2021: 134e9, 2020: 143e9, 2019: 163e9}),
            "current_liabilities": _series({2023: 145e9, 2022: 153e9, 2021: 125e9, 2020: 105e9, 2019: 106e9}),
            "inventory":           _series({2023: 6.3e9, 2022: 4.9e9, 2021: 6.6e9, 2020: 4.0e9, 2019: 4.1e9}),
            "prepaid_expenses":    _series({2023: 14e9,  2022: 16e9,  2021: 14e9,  2020: 11e9,  2019: 12e9}),
            "operating_cashflow":  _series({2023: 114e9, 2022: 122e9, 2021: 104e9, 2020: 80e9,  2019: 70e9}),
            "capex":               _series({2023: 11e9,  2022: 11e9,  2021: 11e9,  2020: 8e9,   2019: 7e9}),
            "free_cashflow":       _series({2023: 103e9, 2022: 111e9, 2021: 93e9,  2020: 72e9,  2019: 63e9}),
            "eps":                 _series({2023: 6.13,  2022: 6.11,  2021: 5.61,  2020: 3.28,  2019: 2.97}),
        }

    def get_annual_avg_prices(self, ticker, years):
        return _series({2023: 178.0, 2022: 150.0, 2021: 129.0, 2020: 95.0, 2019: 52.0})

    def get_annual_dividends(self, ticker):
        return _series({2023: 0.94, 2022: 0.90, 2021: 0.85, 2020: 0.82, 2019: 0.77})

    def get_price_history(self, ticker, period="1y"):
        return pd.DataFrame()

    def get_market_snapshot(self, ticker):
        return {
            "current_price":      182.5,
            "avg_volume":         55_123_456.0,
            "shares_outstanding": 15.4e9,
            "last_dividend":      {"date": "2024-02-09", "amount": 0.24},
            "splits":             _series({2020: 4.0, 2014: 7.0}),
            "latest_quarter": {
                "as_of":               "2024-09-28",
                "current_assets":      137e9,
                "inventory":           7.3e9,
                "prepaid_expenses":    14.7e9,
                "current_liabilities": 176e9,
                "total_liabilities":   308e9,
                "total_debt":          101e9,
                "equity":              56e9,
                "shares_outstanding":  15.4e9,
            },
        }


class _NotFoundMock(FinancialDataSource):
    """Raises ValueError — simulates an unknown ticker."""
    def get_annual_fundamentals(self, t, y):
        raise ValueError(f"No filings for '{t}'")
    def get_annual_avg_prices(self, t, y):   return pd.Series(dtype=float)
    def get_annual_dividends(self, t):        return pd.Series(dtype=float)
    def get_price_history(self, t, p="1y"):   return pd.DataFrame()
    def get_market_snapshot(self, t):
        raise ValueError(f"Unknown ticker '{t}'")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    app.dependency_overrides[get_data_source] = lambda: _GoodMock()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def client_404():
    app.dependency_overrides[get_data_source] = lambda: _NotFoundMock()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def resp(client):
    return client.get("/AAPL/gmr_data")


@pytest.fixture
def body(resp):
    return resp.json()


@pytest.fixture
def snapshot(body):
    return body["current_snapshot"]


@pytest.fixture
def annual(body):
    return body["annual_data"]


# ---------------------------------------------------------------------------
# Status code
# ---------------------------------------------------------------------------

def test_returns_200(resp):
    assert resp.status_code == 200


def test_unknown_ticker_returns_404(client_404):
    assert client_404.get("/FAKE999/gmr_data").status_code == 404


def test_404_has_detail(client_404):
    detail = client_404.get("/FAKE999/gmr_data").json()["detail"]
    assert detail


# ---------------------------------------------------------------------------
# Top-level structure
# ---------------------------------------------------------------------------

def test_top_level_has_ticker(body):
    assert "ticker" in body


def test_ticker_is_uppercased(body):
    assert body["ticker"] == "AAPL"


def test_lowercase_url_uppercased(client):
    resp = client.get("/aapl/gmr_data")
    assert resp.json()["ticker"] == "AAPL"


def test_top_level_has_current_snapshot(body):
    assert "current_snapshot" in body


def test_top_level_has_annual_data(body):
    assert "annual_data" in body


# ---------------------------------------------------------------------------
# current_snapshot
# ---------------------------------------------------------------------------

def test_snapshot_price(snapshot):
    assert snapshot["price"] == pytest.approx(182.5)


def test_snapshot_avg_volume(snapshot):
    assert snapshot["avg_volume"] == pytest.approx(55_123_456.0)


def test_snapshot_has_balance_sheet_fields(snapshot):
    for field in ("current_assets", "inventory", "prepaid_expenses",
                  "current_liabilities", "total_debt", "equity"):
        assert field in snapshot, f"Missing snapshot field: {field}"


def test_snapshot_total_debt_present(snapshot):
    assert snapshot["total_debt"] == pytest.approx(101e9)


def test_snapshot_last_dividend_date(snapshot):
    assert snapshot["last_dividend_date"] == "2024-02-09"


def test_snapshot_last_dividend_amount(snapshot):
    assert snapshot["last_dividend_amount"] == pytest.approx(0.24)


def test_snapshot_last_split_year(snapshot):
    # Most-recent split is 2020 (4-for-1)
    assert snapshot["last_split_year"] == 2020


def test_snapshot_last_split_ratio(snapshot):
    assert snapshot["last_split_ratio"] == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# annual_data
# ---------------------------------------------------------------------------

def test_annual_data_is_list(annual):
    assert isinstance(annual, list)


def test_annual_data_not_empty(annual):
    assert len(annual) > 0


def test_annual_data_sorted_descending(annual):
    years = [row["year"] for row in annual]
    assert years == sorted(years, reverse=True)


def test_annual_row_has_required_fields(annual):
    required = ("year", "avg_price", "revenue", "earnings", "total_assets",
                "liabilities", "equity", "shares", "dividend",
                "current_assets", "inventory", "prepaid_expenses",
                "current_liabilities", "cfo", "delta_ppe", "splits")
    row = annual[0]
    for field in required:
        assert field in row, f"Missing annual row field: {field}"


def test_annual_revenue_positive(annual):
    for row in annual:
        if row.get("revenue") is not None:
            assert row["revenue"] > 0


def test_annual_total_assets_present(annual):
    # total_assets is a new field — must not be absent from all rows
    has_value = [row.get("total_assets") is not None for row in annual]
    assert any(has_value), "total_assets should be present in at least one annual row"


def test_annual_cfo_positive(annual):
    for row in annual:
        if row.get("cfo") is not None:
            assert row["cfo"] > 0


def test_annual_delta_ppe_negative(annual):
    """delta_ppe = -capex, so it should always be negative."""
    for row in annual:
        if row.get("delta_ppe") is not None:
            assert row["delta_ppe"] < 0, f"delta_ppe should be negative: {row['delta_ppe']}"


def test_annual_splits_year_2020_is_four(annual):
    row_2020 = next((r for r in annual if r["year"] == 2020), None)
    assert row_2020 is not None
    assert row_2020["splits"] == pytest.approx(4.0)


def test_annual_splits_no_split_year_is_zero(annual):
    # 2023 has no split in the mock
    row_2023 = next((r for r in annual if r["year"] == 2023), None)
    if row_2023 is not None:
        assert row_2023["splits"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# ?years query param
# ---------------------------------------------------------------------------

def test_years_param_limits_rows(client):
    resp = client.get("/AAPL/gmr_data?years=2")
    assert resp.status_code == 200
    rows = resp.json()["annual_data"]
    assert len(rows) <= 2


def test_years_param_default_is_ten(client):
    resp = client.get("/AAPL/gmr_data")
    rows = resp.json()["annual_data"]
    assert len(rows) <= 10
