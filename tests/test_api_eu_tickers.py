"""
API tests for European (ESEF) tickers.

Verifies that:
  • EU tickers (ASML.AS, SAP.DE) are routed correctly
  • data_source field is "esef" in all responses
  • /fundamentals returns financial data for EU tickers
  • /gmr_long returns a verdict (no price → ratios that need price are null)
  • /tickers/search returns EU tickers with data_source="esef"
  • /prices returns 404 for EU tickers (no price data)
"""
from __future__ import annotations
# pylint: disable=missing-function-docstring,redefined-outer-name

import pytest
import pandas as pd
from starlette.testclient import TestClient

from src.analysis.gmr_data_source import FinancialDataSource
from src.api.app import app
from src.api.dependencies import get_data_source

# ---------------------------------------------------------------------------
# EU mock data source
# ---------------------------------------------------------------------------

_YEARS = [2023, 2022, 2021]


def _s(values):
    return pd.Series(dict(zip(_YEARS, values)))


class EUMockDataSource(FinancialDataSource):
    """Mimics EsefDataSource — no prices, IFRS fundamentals only."""

    def get_annual_fundamentals(self, ticker: str, years: int) -> dict:
        return {
            "revenue":                  _s([27_600e6, 21_200e6, 18_600e6]),
            "net_income":               _s([7_800e6,  5_600e6,  4_900e6]),
            "total_assets":             _s([30_000e6, 24_000e6, 20_000e6]),
            "total_liabilities":        _s([12_000e6, 10_000e6,  8_000e6]),
            "equity":                   _s([18_000e6, 14_000e6, 12_000e6]),
            "operating_cashflow":       _s([9_000e6,  7_500e6,  6_000e6]),
            "capex":                    _s([-2_000e6, -1_800e6, -1_500e6]),
            "free_cashflow":            _s([7_000e6,  5_700e6,  4_500e6]),
            "current_assets":           _s([8_000e6,  7_000e6,  6_000e6]),
            "current_liabilities":      _s([4_000e6,  3_500e6,  3_000e6]),
            "inventory":                _s([800e6,    700e6,    600e6]),
            "prepaid_expenses":         pd.Series(dtype=float),
            "shares_outstanding":       _s([400e6,    402e6,    403e6]),
            "eps":                      _s([19.5,     13.9,     12.2]),
            "long_term_debt":           _s([5_000e6,  4_500e6,  4_000e6]),
            "cash_and_equivalents":     _s([4_000e6,  3_500e6,  3_000e6]),
            "depreciation_amortization":_s([600e6,    550e6,    500e6]),
            "interest_expense":         _s([200e6,    180e6,    160e6]),
            "income_tax_expense":       _s([1_500e6,  1_100e6,    900e6]),
        }

    def get_annual_avg_prices(self, ticker: str, years: int) -> pd.Series:
        return pd.Series(dtype=float)

    def get_annual_dividends(self, ticker: str) -> pd.Series:
        return pd.Series(dtype=float)

    def get_price_history(self, ticker: str, period: str = "1y") -> pd.DataFrame:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    def get_market_snapshot(self, ticker: str) -> dict:
        return {
            "current_price":      None,
            "avg_volume":         None,
            "shares_outstanding": None,
            "last_dividend":      {"date": None, "amount": None},
            "splits":             pd.Series(dtype=float),
            "latest_quarter":     {},
        }

    def get_available_tickers(self) -> list[dict]:
        return [
            {
                "symbol": "ASML", "name": "ASML Holding N.V.",
                "ticker": "ASML.AS", "exchange": "AS", "country": "NL",
                "data_source": "esef",
                "search_name": "asml holding n.v. asml",
                "search_keywords": "asml holding n.v. asml",
            },
            {
                "symbol": "SAP", "name": "SAP SE",
                "ticker": "SAP.DE", "exchange": "DE", "country": "DE",
                "data_source": "esef",
                "search_name": "sap se sap",
                "search_keywords": "sap se sap",
            },
        ]

    def search_tickers(self, query: str, limit: int = 10) -> list[dict]:
        all_t = self.get_available_tickers()
        if not query:
            return all_t[:limit]
        q = query.lower()
        return [t for t in all_t if q in t["search_name"]][:limit]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    app.dependency_overrides[get_data_source] = EUMockDataSource
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# /fundamentals
# ---------------------------------------------------------------------------

def test_fundamentals_eu_200(client: TestClient):
    resp = client.get("/ASML.AS/fundamentals")
    assert resp.status_code == 200


def test_fundamentals_eu_data_source_field(client: TestClient):
    resp = client.get("/ASML.AS/fundamentals")
    assert resp.json()["data_source"] == "esef"


def test_fundamentals_eu_has_per_year(client: TestClient):
    resp = client.get("/ASML.AS/fundamentals")
    data = resp.json()
    assert "per_year" in data
    assert len(data["per_year"]) > 0


def test_fundamentals_eu_revenue_not_null(client: TestClient):
    resp = client.get("/ASML.AS/fundamentals")
    first_year = resp.json()["per_year"][0]
    assert first_year["revenue"] is not None


def test_fundamentals_eu_no_price_ratio(client: TestClient):
    """PE, PB, PS should be null — no price data for ESEF entities."""
    resp = client.get("/ASML.AS/fundamentals")
    first_year = resp.json()["per_year"][0]
    assert first_year.get("pe") is None
    assert first_year.get("pb") is None


def test_fundamentals_na_data_source_field(client: TestClient):
    """NA tickers must still show data_source=edgar."""
    resp = client.get("/AAPL/fundamentals")
    # EUMockDataSource also handles AAPL (same data) — just check the field
    assert resp.json()["data_source"] == "edgar"


# ---------------------------------------------------------------------------
# /gmr_long
# ---------------------------------------------------------------------------

def test_gmr_long_eu_200(client: TestClient):
    resp = client.get("/ASML.AS/gmr_long")
    assert resp.status_code == 200


def test_gmr_long_eu_data_source(client: TestClient):
    resp = client.get("/ASML.AS/gmr_long")
    assert resp.json()["data_source"] == "esef"


def test_gmr_long_eu_has_gmr_ratio(client: TestClient):
    resp = client.get("/ASML.AS/gmr_long")
    assert "gmr_ratio" in resp.json()


# ---------------------------------------------------------------------------
# /valuation
# ---------------------------------------------------------------------------

def test_valuation_eu_200(client: TestClient):
    resp = client.get("/ASML.AS/valuation")
    assert resp.status_code == 200


def test_valuation_eu_data_source(client: TestClient):
    resp = client.get("/ASML.AS/valuation")
    assert resp.json()["data_source"] == "esef"


# ---------------------------------------------------------------------------
# /prices — should 404 for EU tickers
# ---------------------------------------------------------------------------

def test_prices_eu_404(client: TestClient):
    resp = client.get("/ASML.AS/prices")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /tickers/search
# ---------------------------------------------------------------------------

def test_ticker_search_eu_results(client: TestClient):
    resp = client.get("/tickers/search?query=ASML")
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) >= 1
    assert results[0]["symbol"] == "ASML"


def test_ticker_search_eu_data_source(client: TestClient):
    resp = client.get("/tickers/search?query=ASML")
    results = resp.json()["results"]
    assert results[0].get("data_source") == "esef"
