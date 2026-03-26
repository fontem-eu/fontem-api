"""
API tests for European (ESEF) tickers.

Verifies that:
  • EU tickers (ASML.AS, SAP.DE) are routed correctly
  • data_source field is "esef" in all responses
  • /fundamentals returns financial data for EU tickers
  • /gmr_long returns a verdict (no price → ratios that need price are null)
  • /gmr_data returns 200 for EU tickers (regression: was 500 due to float(None))
  • /tickers/search returns EU tickers with data_source="esef"
  • /prices returns 404 for EU tickers (no price data)
  • Sparse ESEF data (many None fields, as seen with GALP.LS) does not cause 500
"""
from __future__ import annotations
# pylint: disable=missing-function-docstring,redefined-outer-name

import pytest
import pandas as pd
from starlette.testclient import TestClient

from src.analysis.gmr_data_source import FinancialDataSource, MarketSnapshot
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

    def get_market_snapshot(self, ticker: str) -> MarketSnapshot:
        return MarketSnapshot()

    def get_data_source_name(self, ticker: str) -> str:
        if ticker in {"ASML.AS", "SAP.DE"}:
            return "esef"
        return "edgar"

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


def test_ticker_search_eu_ticker_field(client: TestClient):
    """Full ticker (ASML.AS) must be present so the frontend emits the right value."""
    resp = client.get("/tickers/search?query=ASML")
    results = resp.json()["results"]
    assert results[0].get("ticker") == "ASML.AS"


# ---------------------------------------------------------------------------
# /gmr_data — regression: was 500 for EU tickers due to float(None) on price
# ---------------------------------------------------------------------------

def test_gmr_data_eu_200(client: TestClient):
    """/{ticker}/gmr_data must return 200 for EU tickers (not 500)."""
    resp = client.get("/ASML.AS/gmr_data")
    assert resp.status_code == 200


def test_gmr_data_eu_data_source(client: TestClient):
    resp = client.get("/ASML.AS/gmr_data")
    assert resp.json()["data_source"] == "esef"


def test_gmr_data_eu_no_price(client: TestClient):
    """current_snapshot.price must be null — ESEF has no price data."""
    resp = client.get("/ASML.AS/gmr_data")
    assert resp.json()["current_snapshot"].get("price") is None


# ---------------------------------------------------------------------------
# Sparse ESEF data — many None fields (regression: GALP.LS pattern)
#
# Real ESEF filings often omit gross_profit, operating_income, capex,
# free_cashflow, eps, shares_outstanding, etc.  All endpoints must return
# 200 and not raise TypeError / float(None).
# ---------------------------------------------------------------------------

def _s_none(years):
    """Series indexed by year where every value is None (sparse ESEF field)."""
    return pd.Series({y: None for y in years}, dtype=object)


class SparseEUMockDataSource(EUMockDataSource):
    """
    Mimics a real ESEF company like GALP.LS where many optional fields
    (gross_profit, capex, eps, shares_outstanding, …) are None.
    """

    def get_annual_fundamentals(self, ticker: str, years: int) -> dict:
        base = super().get_annual_fundamentals(ticker, years)
        # Nullify the fields that are commonly missing in real ESEF filings.
        sparse = {
            "gross_profit":               _s_none(_YEARS),
            "operating_income":           _s_none(_YEARS),
            "capex":                      _s_none(_YEARS),
            "free_cashflow":              _s_none(_YEARS),
            "eps":                        _s_none(_YEARS),
            "shares_outstanding":         _s_none(_YEARS),
            "income_tax_expense":         _s_none(_YEARS),
            "depreciation_amortization":  _s_none(_YEARS),
            "prepaid_expenses":           _s_none(_YEARS),
        }
        return {**base, **sparse}


@pytest.fixture()
def sparse_client():
    app.dependency_overrides[get_data_source] = SparseEUMockDataSource
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_sparse_fundamentals_200(sparse_client: TestClient):
    """Sparse ESEF data must not crash /fundamentals."""
    resp = sparse_client.get("/GALP.LS/fundamentals")
    assert resp.status_code == 200


def test_sparse_fundamentals_has_revenue(sparse_client: TestClient):
    resp = sparse_client.get("/GALP.LS/fundamentals")
    first = resp.json()["per_year"][0]
    assert first["revenue"] is not None


def test_sparse_gmr_long_200(sparse_client: TestClient):
    """Sparse ESEF data must not crash /gmr_long."""
    resp = sparse_client.get("/GALP.LS/gmr_long")
    assert resp.status_code == 200


def test_sparse_gmr_data_200(sparse_client: TestClient):
    """Regression: float(None) on current_price must not cause 500."""
    resp = sparse_client.get("/GALP.LS/gmr_data")
    assert resp.status_code == 200


def test_sparse_valuation_200(sparse_client: TestClient):
    """Sparse ESEF data must not crash /valuation."""
    resp = sparse_client.get("/GALP.LS/valuation")
    assert resp.status_code == 200
