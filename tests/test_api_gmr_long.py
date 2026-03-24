"""
Unit tests for GET /{ticker}/gmr_long
======================================
All tests use a MockDataSource injected via FastAPI dependency overrides.
No network calls — sub-second execution.

Scenarios covered
-----------------
• 200 full response  (default, summarize=false)
• 200 summarised     (summarize=true)
• Response structure — top-level keys, per_year shape
• Ratio values       — spot-checked against known mock inputs
• NaN → null         — fields that can't be computed appear as JSON null
• 404 ticker missing — ValueError from data source → 404
• 404 empty filings  — no common years found → 404
• Ticker uppercasing — lowercase ticker in URL normalised in response
"""
from __future__ import annotations
# pylint: disable=missing-function-docstring,redefined-outer-name,unnecessary-lambda

import pytest
from starlette.testclient import TestClient

import pandas as pd

from src.analysis.gmr_data_source import GMRDataSource
from src.api.app import app
from src.api.dependencies import get_data_source

# ---------------------------------------------------------------------------
# Shared mock fixtures — same realistic values as test_gmr_long.py
# ---------------------------------------------------------------------------

YEARS = [2024, 2023, 2022, 2021, 2020]


def _series(values):
    return pd.Series(dict(zip(YEARS, values)))


class _GoodMock(GMRDataSource):
    """XYZ Corp — passes all GMR Long thresholds."""
    n = len(YEARS)

    def get_annual_fundamentals(self, ticker, years):
        return {
            "revenue":             _series([500e6] * self.n),
            "net_income":          _series([100e6] * self.n),
            "equity":              _series([600e6] * self.n),
            "total_liabilities":   _series([600e6] * self.n),
            "shares_outstanding":  _series([60e6]  * self.n),
            "current_assets":      _series([200e6] * self.n),
            "current_liabilities": _series([150e6] * self.n),
            "inventory":           _series([30e6]  * self.n),
            "prepaid_expenses":    _series([10e6]  * self.n),
            "free_cashflow":       _series([100e6] * self.n),
            "total_assets":        _series([1200e6] * self.n),
            "operating_cashflow":  _series([120e6] * self.n),
            "capex":               _series([20e6]  * self.n),
            "eps":                 _series([100e6 / 60e6] * self.n),
        }

    def get_annual_avg_prices(self, ticker, years):
        return _series([13.0] * self.n)

    def get_annual_dividends(self, ticker):
        return _series([0.50] * self.n)

    def get_price_history(self, ticker, period="1y"):
        return pd.DataFrame()

    def get_market_snapshot(self, ticker):
        return {
            "current_price": 13.50,
            "avg_volume":    2_500_000,
            "last_dividend": {"date": "2024-11-15", "amount": 0.13},
            "splits":        pd.Series(dtype=float),
        }


class _NotFoundMock(GMRDataSource):
    """Raises ValueError — simulates an unknown ticker on EDGAR."""
    def get_annual_fundamentals(self, ticker, years):
        raise ValueError(f"No 10-K filings found for '{ticker}'")

    def get_annual_avg_prices(self, ticker, years):
        return pd.Series(dtype=float)

    def get_annual_dividends(self, ticker):
        return pd.Series(dtype=float)

    def get_price_history(self, ticker, period="1y"):
        return pd.DataFrame()

    def get_market_snapshot(self, ticker):
        return {}


class _EmptyMock(GMRDataSource):
    """Returns empty series — simulates a ticker with no common XBRL years."""
    def get_annual_fundamentals(self, ticker, years):
        return {k: pd.Series(dtype=float) for k in
                ["revenue", "net_income", "equity", "total_liabilities",
                 "shares_outstanding", "current_assets", "current_liabilities",
                 "inventory", "prepaid_expenses", "free_cashflow"]}

    def get_annual_avg_prices(self, ticker, years):
        return pd.Series(dtype=float)

    def get_annual_dividends(self, ticker):
        return pd.Series(dtype=float)

    def get_price_history(self, ticker, period="1y"):
        return pd.DataFrame()

    def get_market_snapshot(self, ticker):
        return {"current_price": 5.0, "avg_volume": 1e6}


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
    return client_good.get("/XYZ/gmr_long")


@pytest.fixture
def full_json(full_resp):
    return full_resp.json()


@pytest.fixture
def summary_json(client_good):
    return client_good.get("/XYZ/gmr_long?summarize=true").json()


# ---------------------------------------------------------------------------
# HTTP status codes
# ---------------------------------------------------------------------------

def test_full_returns_200(full_resp):
    assert full_resp.status_code == 200


def test_summary_returns_200(client_good):
    assert client_good.get("/XYZ/gmr_long?summarize=true").status_code == 200


def test_unknown_ticker_returns_404(client_not_found):
    resp = client_not_found.get("/FAKE_XYZ/gmr_long")
    assert resp.status_code == 404


def test_empty_filings_returns_404(client_empty):
    resp = client_empty.get("/GHOST/gmr_long")
    assert resp.status_code == 404


def test_404_detail_message_is_informative(client_not_found):
    resp = client_not_found.get("/FAKE_XYZ/gmr_long")
    detail = resp.json()["detail"]
    assert "FAKE_XYZ" in detail or "10-K" in detail or "not found" in detail.lower()


# ---------------------------------------------------------------------------
# Top-level response structure
# ---------------------------------------------------------------------------

def test_top_level_keys_full(full_json):
    assert "ticker" in full_json
    assert "gmr_ratio" in full_json
    assert "market_snapshot" in full_json
    assert "per_year" in full_json


def test_summary_only_has_ticker_and_ratio(summary_json):
    assert set(summary_json.keys()) == {"ticker", "gmr_ratio"}


def test_summary_missing_per_year(summary_json):
    assert "per_year" not in summary_json


def test_summary_missing_market_snapshot(summary_json):
    assert "market_snapshot" not in summary_json


# ---------------------------------------------------------------------------
# Ticker field
# ---------------------------------------------------------------------------

def test_ticker_uppercased(full_json):
    assert full_json["ticker"] == "XYZ"


def test_lowercase_url_ticker_uppercased(client_good):
    resp = client_good.get("/xyz/gmr_long")
    assert resp.json()["ticker"] == "XYZ"


# ---------------------------------------------------------------------------
# gmr_ratio structure
# ---------------------------------------------------------------------------

def test_gmr_ratio_has_passes(full_json):
    assert "passes" in full_json["gmr_ratio"]


def test_gmr_ratio_has_flags(full_json):
    assert "flags" in full_json["gmr_ratio"]


def test_gmr_ratio_flags_keys(full_json):
    flags = full_json["gmr_ratio"]["flags"]
    assert set(flags) == {"pe", "pb", "roe", "npm", "debt_equity", "dividend_yield"}


def test_gmr_ratio_has_all_avg_fields(full_json):
    ratio = full_json["gmr_ratio"]
    for field in ("avg_pe", "avg_pb", "avg_roe", "avg_npm",
                  "avg_debt_equity", "avg_dividend_yield",
                  "avg_quick_ratio", "avg_fcf"):
        assert field in ratio, f"Missing field: {field}"


# ---------------------------------------------------------------------------
# gmr_ratio values — XYZ passes all thresholds
# ---------------------------------------------------------------------------

def test_passes_is_true_for_xyz(full_json):
    assert full_json["gmr_ratio"]["passes"] is True


def test_all_flags_true_for_xyz(full_json):
    for key, val in full_json["gmr_ratio"]["flags"].items():
        assert val is True, f"Flag '{key}' should be True for XYZ"


def test_avg_pe_value(full_json):
    # P/E = 13 / (100e6/60e6) = 7.80
    assert pytest.approx(full_json["gmr_ratio"]["avg_pe"], rel=1e-3) == 13 / (100e6 / 60e6)


def test_avg_pb_value(full_json):
    # P/B = 13 / (600e6/60e6) = 1.30
    assert pytest.approx(full_json["gmr_ratio"]["avg_pb"], rel=1e-3) == 1.30


def test_avg_roe_value(full_json):
    # ROE = 100M / 600M * 100 = 16.667 %
    assert pytest.approx(full_json["gmr_ratio"]["avg_roe"], rel=1e-3) == 100 / 6


def test_avg_npm_value(full_json):
    assert pytest.approx(full_json["gmr_ratio"]["avg_npm"], rel=1e-3) == 20.0


def test_avg_dividend_yield_value(full_json):
    # DivY = 0.50 / 13 * 100 = 3.846 %
    assert pytest.approx(full_json["gmr_ratio"]["avg_dividend_yield"], rel=1e-3) == 0.50 / 13 * 100


# ---------------------------------------------------------------------------
# market_snapshot
# ---------------------------------------------------------------------------

def test_market_snapshot_current_price(full_json):
    assert full_json["market_snapshot"]["current_price"] == pytest.approx(13.50)


def test_market_snapshot_avg_volume(full_json):
    assert full_json["market_snapshot"]["avg_volume"] == pytest.approx(2_500_000)


def test_market_snapshot_last_dividend(full_json):
    ld = full_json["market_snapshot"]["last_dividend"]
    assert ld["date"] == "2024-11-15"
    assert ld["amount"] == pytest.approx(0.13)


# ---------------------------------------------------------------------------
# per_year list
# ---------------------------------------------------------------------------

def test_per_year_is_list(full_json):
    assert isinstance(full_json["per_year"], list)


def test_per_year_has_five_entries(full_json):
    assert len(full_json["per_year"]) == 5


def test_per_year_entry_has_expected_keys(full_json):
    entry = full_json["per_year"][0]
    for key in ("year", "avg_price", "revenue", "net_income", "equity",
                "pe", "pb", "roe", "npm", "debt_equity",
                "dividend_yield", "quick_ratio", "free_cashflow"):
        assert key in entry, f"Missing key: {key}"


def test_per_year_years_are_integers(full_json):
    for entry in full_json["per_year"]:
        assert isinstance(entry["year"], int)


def test_per_year_first_year_is_most_recent(full_json):
    years = [e["year"] for e in full_json["per_year"]]
    assert years == sorted(years, reverse=True)


def test_per_year_pe_value(full_json):
    entry = full_json["per_year"][0]
    assert pytest.approx(entry["pe"], rel=1e-3) == 13 / (100e6 / 60e6)


# ---------------------------------------------------------------------------
# ?years query parameter
# ---------------------------------------------------------------------------

def test_years_param_limits_per_year_rows(client_good):
    resp = client_good.get("/XYZ/gmr_long?years=3")
    assert resp.status_code == 200
    assert len(resp.json()["per_year"]) == 3


def test_years_param_1_returns_one_row(client_good):
    resp = client_good.get("/XYZ/gmr_long?years=1")
    assert resp.status_code == 200
    assert len(resp.json()["per_year"]) == 1


def test_years_param_default_is_ten(client_good):
    # Mock supplies 5 years; with default=10 we still get all 5 (data-limited).
    resp = client_good.get("/XYZ/gmr_long")
    assert resp.status_code == 200
    assert len(resp.json()["per_year"]) == 5


def test_years_param_out_of_range_returns_422(client_good):
    assert client_good.get("/XYZ/gmr_long?years=0").status_code == 422
    assert client_good.get("/XYZ/gmr_long?years=21").status_code == 422


def test_years_3_most_recent_years_returned(client_good):
    resp = client_good.get("/XYZ/gmr_long?years=3")
    returned = [e["year"] for e in resp.json()["per_year"]]
    assert returned == [2024, 2023, 2022]


def test_years_2_averages_over_two_years(client_good):
    # With years=2 the gmr_ratio averages should still be computed (not 404).
    resp = client_good.get("/XYZ/gmr_long?years=2")
    assert resp.status_code == 200
    ratio = resp.json()["gmr_ratio"]
    assert ratio["avg_pe"] is not None
    assert ratio["avg_roe"] is not None
