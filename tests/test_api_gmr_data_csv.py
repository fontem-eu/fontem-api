"""
Unit tests for GET /{ticker}/gmr_data_csv
==========================================
All tests use a MockDataSource injected via FastAPI dependency overrides.
No network calls — sub-second execution.

Scenarios covered
-----------------
• 200 response with content-type text/csv
• Content-Disposition header contains the ticker filename
• ticker is uppercased in the CSV output
• Exact CSV format match against EXPECTED_CSV (edit this constant when the
  spreadsheet template changes — each section is clearly labelled)
• Row 1  — Ticker / Price / Avg. Volume
• Row 2  — Latest-quarter balance sheet
• Row 3  — Last dividend / last split
• Row 4  — Column headers
• Row 5+ — Per-year data rows, descending, correct values
• ?years param limits the number of data rows
• 404 on ValueError from data source
"""
from __future__ import annotations

import pytest
import pandas as pd

from starlette.testclient import TestClient

from src.analysis.gmr_data_source import FinancialDataSource
from src.api.app import app
from src.api.dependencies import get_data_source


# ---------------------------------------------------------------------------
# Mock data source  (small round numbers → easy to read the expected CSV)
# ---------------------------------------------------------------------------

def _series(values: dict) -> pd.Series:
    s = pd.Series(values)
    s.index = s.index.astype(int)
    return s.sort_index(ascending=False)


class _CsvMock(FinancialDataSource):
    """Minimal stub returning two years of simple round numbers."""

    def get_annual_fundamentals(self, ticker, years):
        return {
            "ticker":              ticker.upper(),
            "revenue":             _series({2023: 2000, 2022: 1800}),
            "net_income":          _series({2023: 500,  2022: 400}),
            "total_assets":        _series({2023: 3000, 2022: 2800}),
            "total_liabilities":   _series({2023: 1000, 2022: 900}),
            "equity":              _series({2023: 2000, 2022: 1900}),
            "shares_outstanding":  _series({2023: 100,  2022: 105}),
            "current_assets":      _series({2023: 600,  2022: 550}),
            "current_liabilities": _series({2023: 300,  2022: 280}),
            "inventory":           _series({2023: 150,  2022: 140}),
            "prepaid_expenses":    _series({2023: 50,   2022: 45}),
            "operating_cashflow":  _series({2023: 800,  2022: 700}),
            "capex":               _series({2023: 200,  2022: 180}),
            "free_cashflow":       _series({2023: 600,  2022: 520}),
            "eps":                 _series({2023: 5.0,  2022: 3.8}),
        }

    def get_annual_avg_prices(self, ticker, years):
        return _series({2023: 25.5, 2022: 20.0})

    def get_annual_dividends(self, ticker):
        return _series({2023: 2.8, 2022: 2.4})

    def get_price_history(self, ticker, period="1y"):
        return pd.DataFrame()

    def get_market_snapshot(self, ticker):
        return {
            "current_price":      30.0,
            "avg_volume":         500000.0,
            "shares_outstanding": 98.0,
            "last_dividend":      {"date": "3/15/2024", "amount": 0.7},
            "splits":             _series({2014: 2.0}),
            "latest_quarter": {
                "as_of":               "2024-09-30",
                "current_assets":      620,
                "inventory":           155,
                "prepaid_expenses":    55,
                "current_liabilities": 310,
                "total_liabilities":   1050,
                "total_debt":          950,
                "equity":              2100,
                "shares_outstanding":  98.0,
            },
        }


class _NotFoundMock(FinancialDataSource):
    """Simulates an unknown ticker."""
    def get_annual_fundamentals(self, t, y):
        raise ValueError(f"No filings for '{t}'")
    def get_annual_avg_prices(self, t, y):    return pd.Series(dtype=float)
    def get_annual_dividends(self, t):         return pd.Series(dtype=float)
    def get_price_history(self, t, p="1y"):    return pd.DataFrame()
    def get_market_snapshot(self, t):
        raise ValueError(f"Unknown ticker '{t}'")


# ---------------------------------------------------------------------------
# Expected CSV
# ---------------------------------------------------------------------------
# Edit this string to update the spreadsheet template format.
# Each section is labelled so you can find the relevant row quickly.
#
# Formatting rules applied by the endpoint:
#   • Whole floats are rendered as integers  (30.0 → "30", 20.0 → "20")
#   • Non-whole floats are rounded to 2 dp   (25.5 → "25.5", 2.8 → "2.8")
#   • None / NaN / Inf render as ""          (missing data → empty cell)
#   • Split Ratio is "No" when absent
#
EXPECTED_CSV = (
    # ── Row 1: Ticker / Price / Avg. Volume ─────────────────────────
    "Ticker,TEST,Price,30,Avg. Volume,500000\n"
    # ── Row 2: Latest-quarter balance sheet ─────────────────────────
    "Cur Assets,620,Inv.,155,PrePaidEx.,55,Cur Liabi.,310,debt,950,equity,2100,shares,98\n"
    # ── Row 3: Last dividend / last split ───────────────────────────
    "Last Div,3/15/2024,Amount,0.7,Last Split,2014,Split Ratio,2\n"
    # ── Row 4: Per-year column headers ──────────────────────────────
    "Year,Avg. Price,Revenue,Earnings,Assets,Liabilities,Equity,Shares,"
    "Dividend,Cur. Assets,Inventory,Prepaid Ex.,Cur. Liabi.,CFO,Delta PP&E,Splits\n"
    # ── Row 5+: Per-year data rows (descending year) ─────────────────
    # year,avg_price,revenue,earnings,assets,liabilities,equity,shares,
    # dividend,cur_assets,inventory,prepaid,cur_liabi,cfo,delta_ppe,splits
    "2023,25.5,2000,500,3000,1000,2000,100,2.8,600,150,50,300,800,-200,0\n"
    "2022,20,1800,400,2800,900,1900,105,2.4,550,140,45,280,700,-180,0\n"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    app.dependency_overrides[get_data_source] = lambda: _CsvMock()
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
    return client.get("/TEST/gmr_data_csv")


# ---------------------------------------------------------------------------
# HTTP basics
# ---------------------------------------------------------------------------

def test_returns_200(resp):
    assert resp.status_code == 200


def test_content_type_is_csv(resp):
    assert "text/csv" in resp.headers["content-type"]


def test_content_disposition_contains_ticker(resp):
    disposition = resp.headers.get("content-disposition", "")
    assert "TEST_gmr_data.csv" in disposition


def test_unknown_ticker_returns_404(client_404):
    assert client_404.get("/FAKE999/gmr_data_csv").status_code == 404


def test_404_has_detail(client_404):
    assert client_404.get("/FAKE999/gmr_data_csv").json()["detail"]


# ---------------------------------------------------------------------------
# Ticker uppercasing
# ---------------------------------------------------------------------------

def test_lowercase_ticker_uppercased_in_csv(client):
    body = client.get("/test/gmr_data_csv").text
    first_line = body.splitlines()[0]
    assert first_line.startswith("Ticker,TEST,")


# ---------------------------------------------------------------------------
# Exact format validation  (line-by-line for readable failure messages)
# ---------------------------------------------------------------------------

def test_exact_csv_output(resp):
    """Full format check — edit EXPECTED_CSV above to update the template."""
    assert resp.text == EXPECTED_CSV


def test_row1_ticker_price_volume(resp):
    line = resp.text.splitlines()[0]
    assert line == "Ticker,TEST,Price,30,Avg. Volume,500000"


def test_row2_balance_sheet(resp):
    line = resp.text.splitlines()[1]
    assert line == "Cur Assets,620,Inv.,155,PrePaidEx.,55,Cur Liabi.,310,debt,950,equity,2100,shares,98"


def test_row3_dividend_split(resp):
    line = resp.text.splitlines()[2]
    assert line == "Last Div,3/15/2024,Amount,0.7,Last Split,2014,Split Ratio,2"


def test_row4_column_headers(resp):
    line = resp.text.splitlines()[3]
    assert line == (
        "Year,Avg. Price,Revenue,Earnings,Assets,Liabilities,Equity,Shares,"
        "Dividend,Cur. Assets,Inventory,Prepaid Ex.,Cur. Liabi.,CFO,Delta PP&E,Splits"
    )


def test_row5_year_2023(resp):
    line = resp.text.splitlines()[4]
    assert line == "2023,25.5,2000,500,3000,1000,2000,100,2.8,600,150,50,300,800,-200,0"


def test_row6_year_2022(resp):
    line = resp.text.splitlines()[5]
    assert line == "2022,20,1800,400,2800,900,1900,105,2.4,550,140,45,280,700,-180,0"


def test_data_rows_sorted_descending(resp):
    data_lines = resp.text.splitlines()[4:]  # skip 4 header rows
    years = [int(line.split(",")[0]) for line in data_lines if line]
    assert years == sorted(years, reverse=True)


def test_split_ratio_absent_renders_no(client):
    """When there is no split history, Split Ratio field should be 'No'."""
    # Patch the mock to return an empty splits series
    class _NoSplitMock(_CsvMock):
        def get_market_snapshot(self, ticker):
            snap = super().get_market_snapshot(ticker)
            snap["splits"] = pd.Series(dtype=float)
            return snap

    app.dependency_overrides[get_data_source] = lambda: _NoSplitMock()
    with TestClient(app) as c:
        body = c.get("/TEST/gmr_data_csv").text
    app.dependency_overrides.clear()

    row3 = body.splitlines()[2]
    assert row3.endswith(",Split Ratio,No")


# ---------------------------------------------------------------------------
# ?years query parameter
# ---------------------------------------------------------------------------

def test_years_param_limits_data_rows(client):
    body = client.get("/TEST/gmr_data_csv?years=1").text
    data_rows = [l for l in body.splitlines()[4:] if l]
    assert len(data_rows) == 1


def test_years_param_default_returns_both_rows(resp):
    data_rows = [l for l in resp.text.splitlines()[4:] if l]
    assert len(data_rows) == 2
