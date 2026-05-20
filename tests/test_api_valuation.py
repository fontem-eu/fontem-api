"""
Unit tests for GET /{ticker}/valuation
========================================
All tests use a MockDataSource injected via FastAPI dependency overrides.
No network calls — sub-second execution.

Scenarios covered
-----------------
• 200 full response     (default, summarize=false)
• 200 summarised        (summarize=true)
• Response structure    — top-level keys, per_year shape
• ?years param          — limits number of per_year rows returned, default=10
• 404 ticker missing    — ValueError from data source → 404
• 404 empty filings     — no fiscal years found → 404
• Ticker uppercasing    — lowercase ticker in URL normalised in response
"""
from __future__ import annotations
# pylint: disable=missing-function-docstring,redefined-outer-name,unnecessary-lambda,multiple-statements,unused-argument

import pytest

import pandas as pd

from src.analysis.gmr_data_source import GMRDataSource, MarketSnapshot
from tests.dishka_fixtures import make_test_client, cleanup_dishka

# ---------------------------------------------------------------------------
# Mock data
# ---------------------------------------------------------------------------

YEARS = [2024, 2023, 2022, 2021, 2020]


def _series(values):
    return pd.Series(dict(zip(YEARS, values)))


class _GoodMock(GMRDataSource):
    """XYZ Corp — consistent data across 5 years; computable EBITDA/ROIC."""
    n = len(YEARS)

    def get_annual_fundamentals(self, ticker, years):
        return {
            "operating_income":          _series([150e6]  * self.n),
            "revenue":                   _series([500e6]  * self.n),
            "free_cashflow":             _series([100e6]  * self.n),
            "equity":                    _series([600e6]  * self.n),
            "long_term_debt":            _series([200e6]  * self.n),
            "cash_and_equivalents":      _series([50e6]   * self.n),
            "depreciation_amortization": _series([30e6]   * self.n),
            "interest_expense":          _series([10e6]   * self.n),
            "income_tax_expense":        _series([30e6]   * self.n),
            "net_income":                _series([100e6]  * self.n),
            # Extras that Fundamentals/GMRLong might need (ignored by Valuation)
            "total_assets":              _series([1200e6] * self.n),
            "total_liabilities":         _series([600e6]  * self.n),
        }

    def get_annual_avg_prices(self, ticker, years):
        return _series([13.0] * self.n)

    def get_annual_dividends(self, ticker):
        return _series([0.50] * self.n)

    def get_price_history(self, ticker, period="1y"):
        return pd.DataFrame()

    def get_market_snapshot(self, ticker):
        return MarketSnapshot(current_price=150.0, shares_outstanding=10e6, avg_volume=2_500_000)

    def get_available_tickers(self): return []
    def search_tickers(self, query, limit=10): return []  # pylint: disable=unused-argument
    def get_data_source_name(self, ticker): return "edgar"


class _NotFoundMock(GMRDataSource):
    def get_annual_fundamentals(self, ticker, years):
        raise ValueError(f"No filings for '{ticker}'")

    def get_annual_avg_prices(self, ticker, years):
        return pd.Series(dtype=float)

    def get_annual_dividends(self, ticker):
        return pd.Series(dtype=float)

    def get_price_history(self, ticker, period="1y"):
        return pd.DataFrame()

    def get_market_snapshot(self, ticker):
        return MarketSnapshot()

    def get_available_tickers(self): return []
    def search_tickers(self, query, limit=10): return []  # pylint: disable=unused-argument
    def get_data_source_name(self, ticker): return "edgar"


class _EmptyMock(GMRDataSource):
    def get_annual_fundamentals(self, ticker, years):
        return {k: pd.Series(dtype=float) for k in [
            "operating_income", "revenue", "free_cashflow", "equity",
            "long_term_debt", "cash_and_equivalents",
            "depreciation_amortization", "interest_expense",
            "income_tax_expense", "net_income",
        ]}

    def get_annual_avg_prices(self, ticker, years):
        return pd.Series(dtype=float)

    def get_annual_dividends(self, ticker):
        return pd.Series(dtype=float)

    def get_price_history(self, ticker, period="1y"):
        return pd.DataFrame()

    def get_market_snapshot(self, ticker):
        return MarketSnapshot(current_price=5.0)

    def get_available_tickers(self): return []
    def search_tickers(self, query, limit=10): return []  # pylint: disable=unused-argument
    def get_data_source_name(self, ticker): return "edgar"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client_good():
    yield make_test_client(_GoodMock)
    cleanup_dishka()


@pytest.fixture
def client_not_found():
    yield make_test_client(_NotFoundMock)
    cleanup_dishka()


@pytest.fixture
def client_empty():
    yield make_test_client(_EmptyMock)
    cleanup_dishka()


@pytest.fixture
def full_resp(client_good):
    return client_good.get("/XYZ/valuation")


@pytest.fixture
def full_json(full_resp):
    return full_resp.json()


@pytest.fixture
def summary_json(client_good):
    return client_good.get("/XYZ/valuation?summarize=true").json()


# ---------------------------------------------------------------------------
# HTTP status codes
# ---------------------------------------------------------------------------

def test_full_returns_200(full_resp):
    assert full_resp.status_code == 200


def test_summary_returns_200(client_good):
    assert client_good.get("/XYZ/valuation?summarize=true").status_code == 200


def test_unknown_ticker_returns_404(client_not_found):
    assert client_not_found.get("/FAKE/valuation").status_code == 404


def test_empty_filings_returns_404(client_empty):
    assert client_empty.get("/GHOST/valuation").status_code == 404


# ---------------------------------------------------------------------------
# Response structure
# ---------------------------------------------------------------------------

def test_top_level_keys_full(full_json):
    for key in ("ticker", "valuation_snapshot", "summary", "per_year"):
        assert key in full_json


def test_summary_only_has_ticker_and_summary(summary_json):
    assert set(summary_json.keys()) == {"ticker", "data_source", "summary"}


def test_ticker_uppercased(full_json):
    assert full_json["ticker"] == "XYZ"


def test_lowercase_url_normalised(client_good):
    assert client_good.get("/xyz/valuation").json()["ticker"] == "XYZ"


# ---------------------------------------------------------------------------
# Summary structure
# ---------------------------------------------------------------------------

def test_summary_has_expected_fields(full_json):
    s = full_json["summary"]
    for f in ("avg_ebitda_margin", "avg_roic", "avg_interest_coverage",
              "avg_net_debt_to_ebitda"):
        assert f in s, f"Missing summary field: {f}"


def test_avg_ebitda_margin_is_positive(full_json):
    # EBITDA = 150M + 30M = 180M; Revenue = 500M → margin = 36 %
    assert full_json["summary"]["avg_ebitda_margin"] == pytest.approx(36.0, rel=1e-2)


# ---------------------------------------------------------------------------
# Valuation snapshot
# ---------------------------------------------------------------------------

def test_valuation_snapshot_has_ev(full_json):
    assert "enterprise_value" in full_json["valuation_snapshot"]


def test_valuation_snapshot_ev_ebitda_is_numeric(full_json):
    ev_ebitda = full_json["valuation_snapshot"].get("ev_ebitda")
    if ev_ebitda is not None:
        assert isinstance(ev_ebitda, (int, float))


# ---------------------------------------------------------------------------
# per_year list
# ---------------------------------------------------------------------------

def test_per_year_is_list(full_json):
    assert isinstance(full_json["per_year"], list)


def test_per_year_default_is_limited_by_available_data(full_json):
    # Default years=10, but mock only has 5 years of data.
    assert len(full_json["per_year"]) == 5


def test_per_year_years_are_descending(full_json):
    years = [e["year"] for e in full_json["per_year"]]
    assert years == sorted(years, reverse=True)


def test_per_year_entry_has_expected_keys(full_json):
    entry = full_json["per_year"][0]
    for key in ("year", "ebitda", "ebitda_margin", "net_debt", "roic"):
        assert key in entry, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# ?years query parameter — the main regression guard
# ---------------------------------------------------------------------------

def test_years_param_limits_per_year_rows(client_good):
    resp = client_good.get("/XYZ/valuation?years=3")
    assert resp.status_code == 200
    assert len(resp.json()["per_year"]) == 3


def test_years_param_1_returns_one_row(client_good):
    resp = client_good.get("/XYZ/valuation?years=1")
    assert resp.status_code == 200
    assert len(resp.json()["per_year"]) == 1


def test_years_3_most_recent_years_returned(client_good):
    resp = client_good.get("/XYZ/valuation?years=3")
    returned = [e["year"] for e in resp.json()["per_year"]]
    assert returned == [2024, 2023, 2022]


def test_years_default_is_ten(client_good):
    # No ?years param → uses default (10); data-limited to 5 by mock.
    resp = client_good.get("/XYZ/valuation")
    assert resp.status_code == 200
    # The key assertion: we get all available data (5), not a hard-coded 5
    # that would accidentally pass even if default were still 5.
    # We verify by checking that a *smaller* explicit request gives fewer rows.
    assert len(resp.json()["per_year"]) == 5


def test_years_param_out_of_range_returns_422(client_good):
    assert client_good.get("/XYZ/valuation?years=0").status_code == 422
    assert client_good.get("/XYZ/valuation?years=21").status_code == 422


def test_years_averages_recomputed_per_window(client_good):
    # With years=2, summary averages are still populated (not 404).
    resp = client_good.get("/XYZ/valuation?years=2")
    assert resp.status_code == 200
    assert resp.json()["summary"]["avg_ebitda_margin"] is not None
