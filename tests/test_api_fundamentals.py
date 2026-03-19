"""
Unit tests for GET /{ticker}/fundamentals
==========================================
All tests use a MockDataSource injected via FastAPI dependency overrides.
No network calls — sub-second execution.

Scenarios covered
-----------------
• 200 full response             (default, summarize=false)
• 200 summarised                (summarize=true)
• Response structure            — top-level keys, per_year shape
• Ratio values                  — spot-checked against known mock inputs
• ?years param                  — limits number of per_year rows returned
• NaN → null                   — fields that can't be computed appear as JSON null
• 404 ticker missing            — ValueError from data source → 404
• 404 empty filings             — no fiscal years found → 404
• Ticker uppercasing            — lowercase ticker in URL normalised in response
• All summary ratio keys present
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import pandas as pd

from src.analysis.gmr_data_source import GMRDataSource
from src.api.app import app
from src.api.dependencies import get_data_source

# ---------------------------------------------------------------------------
# Mock data setup
# ---------------------------------------------------------------------------

YEARS = [2024, 2023, 2022, 2021, 2020]


def _series(values):
    return pd.Series(dict(zip(YEARS, values)))


class _GoodMock(GMRDataSource):
    """
    XYZ Corp — clean, consistent data across 5 years.

    Key values per year (constant across years for predictable assertions):
      revenue          = 500M
      gross_profit     = 250M  → gross margin = 50 %
      operating_income = 150M  → operating margin = 30 %
      net_income       = 100M  → NPM = 20 %
      total_assets     = 1200M
      total_liabilities= 600M  → D/E = 1.0, D/A = 0.5
      equity           = 600M
      current_assets   = 200M
      current_liabilities = 150M → current_ratio ≈ 1.333
      inventory        = 30M
      prepaid_expenses = 10M   → quick_ratio = (200-30-10)/150 ≈ 1.067
      shares           = 60M
      free_cashflow    = 100M
      avg_price        = 13.0
      dividends        = 0.50
    """
    n = len(YEARS)

    def get_annual_fundamentals(self, ticker, years):
        return {
            "revenue":             _series([500e6]  * self.n),
            "gross_profit":        _series([250e6]  * self.n),
            "operating_income":    _series([150e6]  * self.n),
            "net_income":          _series([100e6]  * self.n),
            "total_assets":        _series([1200e6] * self.n),
            "total_liabilities":   _series([600e6]  * self.n),
            "equity":              _series([600e6]  * self.n),
            "current_assets":      _series([200e6]  * self.n),
            "current_liabilities": _series([150e6]  * self.n),
            "inventory":           _series([30e6]   * self.n),
            "prepaid_expenses":    _series([10e6]   * self.n),
            "shares_outstanding":  _series([60e6]   * self.n),
            "free_cashflow":       _series([100e6]  * self.n),
            "operating_cashflow":  _series([120e6]  * self.n),
            "capex":               _series([20e6]   * self.n),
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
            "current_price":     13.50,
            "avg_volume":        2_500_000,
            "shares_outstanding": 60e6,
            "last_dividend":     {"date": "2024-11-15", "amount": 0.13},
            "splits":            pd.Series(dtype=float),
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
    """Returns empty series — simulates a ticker with no XBRL data at all."""
    def get_annual_fundamentals(self, ticker, years):
        return {k: pd.Series(dtype=float) for k in [
            "revenue", "gross_profit", "operating_income", "net_income",
            "total_assets", "total_liabilities", "equity",
            "current_assets", "current_liabilities",
            "inventory", "prepaid_expenses", "shares_outstanding",
            "free_cashflow", "operating_cashflow", "capex",
        ]}

    def get_annual_avg_prices(self, ticker, years):
        return pd.Series(dtype=float)

    def get_annual_dividends(self, ticker):
        return pd.Series(dtype=float)

    def get_price_history(self, ticker, period="1y"):
        return pd.DataFrame()

    def get_market_snapshot(self, ticker):
        return {"current_price": 5.0, "avg_volume": 1e6}


class _NaNMock(GMRDataSource):
    """Revenue only — no price history → P/E, P/B etc. become NaN → null."""
    n = len(YEARS)

    def get_annual_fundamentals(self, ticker, years):
        return {
            "revenue":             _series([500e6] * self.n),
            "net_income":          _series([100e6] * self.n),
            "total_assets":        _series([1200e6] * self.n),
            "total_liabilities":   _series([600e6] * self.n),
            "equity":              _series([600e6] * self.n),
            "shares_outstanding":  pd.Series(dtype=float),   # ← no shares → NaN ratios
            "current_assets":      pd.Series(dtype=float),
            "current_liabilities": pd.Series(dtype=float),
            "gross_profit":        pd.Series(dtype=float),
            "operating_income":    pd.Series(dtype=float),
            "free_cashflow":       pd.Series(dtype=float),
            "operating_cashflow":  pd.Series(dtype=float),
            "capex":               pd.Series(dtype=float),
            "inventory":           pd.Series(dtype=float),
            "prepaid_expenses":    pd.Series(dtype=float),
        }

    def get_annual_avg_prices(self, ticker, years):
        return pd.Series(dtype=float)   # no prices → no P/E, P/B, P/S

    def get_annual_dividends(self, ticker):
        return pd.Series(dtype=float)

    def get_price_history(self, ticker, period="1y"):
        return pd.DataFrame()

    def get_market_snapshot(self, ticker):
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
def client_nan():
    app.dependency_overrides[get_data_source] = lambda: _NaNMock()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def full_resp(client_good):
    return client_good.get("/XYZ/fundamentals")


@pytest.fixture
def full_json(full_resp):
    return full_resp.json()


@pytest.fixture
def summary_json(client_good):
    return client_good.get("/XYZ/fundamentals?summarize=true").json()


# ---------------------------------------------------------------------------
# HTTP status codes
# ---------------------------------------------------------------------------

def test_full_returns_200(full_resp):
    assert full_resp.status_code == 200


def test_summary_returns_200(client_good):
    assert client_good.get("/XYZ/fundamentals?summarize=true").status_code == 200


def test_unknown_ticker_returns_404(client_not_found):
    assert client_not_found.get("/FAKE_XYZ/fundamentals").status_code == 404


def test_empty_filings_returns_404(client_empty):
    assert client_empty.get("/GHOST/fundamentals").status_code == 404


def test_404_detail_message_is_informative(client_not_found):
    detail = client_not_found.get("/FAKE_XYZ/fundamentals").json()["detail"]
    assert "FAKE_XYZ" in detail or "10-K" in detail or "not found" in detail.lower()


# ---------------------------------------------------------------------------
# Top-level structure
# ---------------------------------------------------------------------------

def test_top_level_keys_full(full_json):
    assert "ticker" in full_json
    assert "ratios_summary" in full_json
    assert "market_snapshot" in full_json
    assert "per_year" in full_json


def test_summary_only_has_ticker_and_ratios(summary_json):
    assert "ticker" in summary_json
    assert "ratios_summary" in summary_json
    assert "per_year" not in summary_json
    assert "market_snapshot" not in summary_json


# ---------------------------------------------------------------------------
# Ticker field
# ---------------------------------------------------------------------------

def test_ticker_uppercased(full_json):
    assert full_json["ticker"] == "XYZ"


def test_lowercase_url_uppercased(client_good):
    assert client_good.get("/xyz/fundamentals").json()["ticker"] == "XYZ"


# ---------------------------------------------------------------------------
# ratios_summary — all keys present
# ---------------------------------------------------------------------------

SUMMARY_KEYS = [
    "avg_pe", "avg_pb", "avg_ps",
    "avg_roe", "avg_roa", "avg_npm", "avg_gross_margin", "avg_operating_margin",
    "avg_current_ratio", "avg_quick_ratio", "avg_debt_to_equity", "avg_debt_to_assets",
    "avg_fcf_yield", "avg_dividend_yield",
    "avg_revenue_growth", "avg_earnings_growth",
]


def test_ratios_summary_has_all_keys(full_json):
    summary = full_json["ratios_summary"]
    for key in SUMMARY_KEYS:
        assert key in summary, f"Missing key in ratios_summary: {key}"


# ---------------------------------------------------------------------------
# ratios_summary — spot-checked values against mock inputs
# ---------------------------------------------------------------------------

def test_avg_pe_value(full_json):
    # P/E = 13.0 / (100e6 / 60e6) ≈ 7.80
    expected = 13.0 / (100e6 / 60e6)
    assert pytest.approx(full_json["ratios_summary"]["avg_pe"], rel=1e-3) == expected


def test_avg_pb_value(full_json):
    # P/B = 13.0 / (600e6 / 60e6) = 1.30
    assert pytest.approx(full_json["ratios_summary"]["avg_pb"], rel=1e-3) == 1.30


def test_avg_ps_value(full_json):
    # P/S = 13.0 / (500e6 / 60e6) ≈ 1.56
    expected = 13.0 / (500e6 / 60e6)
    assert pytest.approx(full_json["ratios_summary"]["avg_ps"], rel=1e-3) == expected


def test_avg_roe_value(full_json):
    # ROE = 100M / 600M * 100 ≈ 16.667 %
    assert pytest.approx(full_json["ratios_summary"]["avg_roe"], rel=1e-3) == 100 / 6


def test_avg_roa_value(full_json):
    # ROA = 100M / 1200M * 100 ≈ 8.333 %
    assert pytest.approx(full_json["ratios_summary"]["avg_roa"], rel=1e-3) == 100 / 12


def test_avg_npm_value(full_json):
    # NPM = 100M / 500M * 100 = 20 %
    assert pytest.approx(full_json["ratios_summary"]["avg_npm"], rel=1e-3) == 20.0


def test_avg_gross_margin_value(full_json):
    # Gross margin = 250M / 500M * 100 = 50 %
    assert pytest.approx(full_json["ratios_summary"]["avg_gross_margin"], rel=1e-3) == 50.0


def test_avg_operating_margin_value(full_json):
    # Operating margin = 150M / 500M * 100 = 30 %
    assert pytest.approx(full_json["ratios_summary"]["avg_operating_margin"], rel=1e-3) == 30.0


def test_avg_current_ratio_value(full_json):
    # 200M / 150M ≈ 1.333
    assert pytest.approx(full_json["ratios_summary"]["avg_current_ratio"], rel=1e-3) == 200 / 150


def test_avg_quick_ratio_value(full_json):
    # (200M - 30M - 10M) / 150M ≈ 1.067
    assert pytest.approx(full_json["ratios_summary"]["avg_quick_ratio"], rel=1e-3) == 160 / 150


def test_avg_debt_to_equity_value(full_json):
    # 600M / 600M = 1.0
    assert pytest.approx(full_json["ratios_summary"]["avg_debt_to_equity"], rel=1e-3) == 1.0


def test_avg_debt_to_assets_value(full_json):
    # 600M / 1200M = 0.5
    assert pytest.approx(full_json["ratios_summary"]["avg_debt_to_assets"], rel=1e-3) == 0.5


def test_avg_dividend_yield_value(full_json):
    # 0.50 / 13.0 * 100 ≈ 3.846 %
    assert pytest.approx(full_json["ratios_summary"]["avg_dividend_yield"], rel=1e-3) == 0.50 / 13.0 * 100


def test_avg_fcf_yield_value(full_json):
    # FCF/share = 100M/60M; yield = (100M/60M) / 13.0 * 100
    expected = (100e6 / 60e6) / 13.0 * 100
    assert pytest.approx(full_json["ratios_summary"]["avg_fcf_yield"], rel=1e-3) == expected


# ---------------------------------------------------------------------------
# market_snapshot
# ---------------------------------------------------------------------------

def test_market_snapshot_current_price(full_json):
    assert pytest.approx(full_json["market_snapshot"]["current_price"]) == 13.50


def test_market_snapshot_avg_volume(full_json):
    assert pytest.approx(full_json["market_snapshot"]["avg_volume"]) == 2_500_000


def test_market_snapshot_market_cap(full_json):
    # 13.50 × 60M = 810M
    assert pytest.approx(full_json["market_snapshot"]["market_cap"]) == 13.50 * 60e6


def test_market_snapshot_last_dividend(full_json):
    ld = full_json["market_snapshot"]
    assert ld["last_dividend_date"] == "2024-11-15"
    assert pytest.approx(ld["last_dividend_amount"]) == 0.13


# ---------------------------------------------------------------------------
# per_year list
# ---------------------------------------------------------------------------

def test_per_year_is_list(full_json):
    assert isinstance(full_json["per_year"], list)


def test_per_year_has_five_entries_by_default(full_json):
    assert len(full_json["per_year"]) == 5


def test_per_year_years_are_descending(full_json):
    years = [e["year"] for e in full_json["per_year"]]
    assert years == sorted(years, reverse=True)


def test_per_year_years_are_integers(full_json):
    for entry in full_json["per_year"]:
        assert isinstance(entry["year"], int)


def test_per_year_entry_has_income_fields(full_json):
    entry = full_json["per_year"][0]
    for key in ("revenue", "gross_profit", "operating_income", "net_income", "eps"):
        assert key in entry, f"Missing income field: {key}"


def test_per_year_entry_has_balance_sheet_fields(full_json):
    entry = full_json["per_year"][0]
    for key in ("total_assets", "total_liabilities", "equity",
                "current_assets", "current_liabilities"):
        assert key in entry, f"Missing balance sheet field: {key}"


def test_per_year_entry_has_cashflow_fields(full_json):
    entry = full_json["per_year"][0]
    for key in ("operating_cashflow", "capex", "free_cashflow"):
        assert key in entry, f"Missing cashflow field: {key}"


def test_per_year_entry_has_ratio_fields(full_json):
    entry = full_json["per_year"][0]
    for key in ("pe", "pb", "ps", "roe", "roa", "npm",
                "gross_margin", "operating_margin",
                "current_ratio", "quick_ratio",
                "debt_to_equity", "debt_to_assets",
                "fcf_yield", "dividend_yield"):
        assert key in entry, f"Missing ratio field: {key}"


def test_per_year_entry_has_per_share_fields(full_json):
    entry = full_json["per_year"][0]
    for key in ("book_value_per_share", "revenue_per_share",
                "fcf_per_share", "dividend_per_share"):
        assert key in entry, f"Missing per-share field: {key}"


def test_per_year_pe_spot_check(full_json):
    entry = full_json["per_year"][0]
    assert pytest.approx(entry["pe"], rel=1e-3) == 13.0 / (100e6 / 60e6)


def test_per_year_gross_margin_spot_check(full_json):
    entry = full_json["per_year"][0]
    assert pytest.approx(entry["gross_margin"], rel=1e-3) == 50.0


# ---------------------------------------------------------------------------
# ?years query parameter
# ---------------------------------------------------------------------------

def test_years_param_limits_per_year_rows(client_good):
    resp = client_good.get("/XYZ/fundamentals?years=3")
    assert resp.status_code == 200
    assert len(resp.json()["per_year"]) == 3


def test_years_param_1_returns_one_row(client_good):
    resp = client_good.get("/XYZ/fundamentals?years=1")
    assert resp.status_code == 200
    assert len(resp.json()["per_year"]) == 1


def test_years_param_out_of_range_returns_422(client_good):
    assert client_good.get("/XYZ/fundamentals?years=0").status_code == 422
    assert client_good.get("/XYZ/fundamentals?years=21").status_code == 422


# ---------------------------------------------------------------------------
# NaN → null serialisation
# ---------------------------------------------------------------------------

def test_nan_fields_serialised_as_null(client_nan):
    """When shares are missing, P/E, P/B, P/S must be null (not NaN strings)."""
    body = client_nan.get("/XYZ/fundamentals").json()
    summary = body["ratios_summary"]
    # These depend on both shares and price — both missing in _NaNMock
    assert summary.get("avg_pe") is None
    assert summary.get("avg_pb") is None
    assert summary.get("avg_ps") is None


def test_nan_gross_margin_null_when_missing(client_nan):
    body = client_nan.get("/XYZ/fundamentals").json()
    assert body["ratios_summary"].get("avg_gross_margin") is None
