"""
Shared mock FinancialDataSource for unit tests.

Returns deterministic, realistic-looking data so every computation path
in the analysis layer can be exercised without hitting the network.
"""
from __future__ import annotations

import pandas as pd

from src.analysis.gmr_data_source import FinancialDataSource

# ---------------------------------------------------------------------------
# Canonical test data (3 fiscal years: 2023, 2022, 2021)
# ---------------------------------------------------------------------------

_YEARS = [2023, 2022, 2021]

_FUNDAMENTALS: dict = {
    "ticker":              "TEST",
    "form_type":           "10-K",
    # Income statement
    "revenue":             pd.Series({2023: 100_000, 2022: 90_000,  2021: 80_000}),
    "gross_profit":        pd.Series({2023: 60_000,  2022: 54_000,  2021: 48_000}),
    "operating_income":    pd.Series({2023: 25_000,  2022: 22_000,  2021: 19_000}),
    "net_income":          pd.Series({2023: 18_000,  2022: 15_000,  2021: 12_000}),
    "eps":                 pd.Series({2023: 1.80,    2022: 1.50,    2021: 1.20}),
    "income_tax_expense":  pd.Series({2023: 5_000,   2022: 4_500,   2021: 4_000}),
    "interest_expense":    pd.Series({2023: 2_000,   2022: 2_200,   2021: 2_400}),
    # Balance sheet
    "total_assets":        pd.Series({2023: 200_000, 2022: 180_000, 2021: 160_000}),
    "total_liabilities":   pd.Series({2023: 120_000, 2022: 110_000, 2021: 100_000}),
    "equity":              pd.Series({2023: 80_000,  2022: 70_000,  2021: 60_000}),
    "current_assets":      pd.Series({2023: 50_000,  2022: 45_000,  2021: 40_000}),
    "current_liabilities": pd.Series({2023: 30_000,  2022: 28_000,  2021: 26_000}),
    "long_term_debt":      pd.Series({2023: 40_000,  2022: 42_000,  2021: 44_000}),
    "cash_and_equivalents":pd.Series({2023: 15_000,  2022: 12_000,  2021: 10_000}),
    "inventory":           pd.Series({2023: 8_000,   2022: 7_500,   2021: 7_000}),
    "prepaid_expenses":    pd.Series({2023: 2_000,   2022: 1_800,   2021: 1_600}),
    "shares_outstanding":  pd.Series({2023: 10_000,  2022: 10_000,  2021: 10_000}),
    # Cash flow
    "operating_cashflow":  pd.Series({2023: 22_000,  2022: 19_000,  2021: 16_000}),
    "capex":               pd.Series({2023: 5_000,   2022: 4_500,   2021: 4_000}),
    "free_cashflow":       pd.Series({2023: 17_000,  2022: 14_500,  2021: 12_000}),
    "depreciation_amortization": pd.Series({2023: 4_000, 2022: 3_800, 2021: 3_600}),
}

_ANNUAL_PRICES = pd.Series({2023: 20.0, 2022: 18.0, 2021: 15.0})

_ANNUAL_DIVIDENDS = pd.Series({2023: 0.40, 2022: 0.35, 2021: 0.30})

_SNAPSHOT: dict = {
    "current_price":      22.0,
    "avg_volume":         500_000.0,
    "shares_outstanding": 10_000.0,
    "last_dividend":      {"date": "2023-12-15", "amount": 0.10},
    "splits":             pd.Series(dtype=float),
    "latest_quarter":     {},
    "beta":               1.10,
    "week_52_high":       25.0,
    "week_52_low":        16.0,
}


class MockDataSource(FinancialDataSource):
    """Deterministic in-memory data source for unit tests."""

    def get_annual_fundamentals(self, ticker: str, years: int) -> dict:
        return _FUNDAMENTALS

    def get_annual_avg_prices(self, ticker: str, years: int) -> pd.Series:
        return _ANNUAL_PRICES

    def get_annual_dividends(self, ticker: str) -> pd.Series:
        return _ANNUAL_DIVIDENDS

    def get_price_history(self, ticker: str, period: str = "1y") -> pd.DataFrame:
        return pd.DataFrame()

    def get_market_snapshot(self, ticker: str) -> dict:
        return _SNAPSHOT


class EmptyDataSource(FinancialDataSource):
    """Returns empty data to test graceful 404 handling."""

    def get_annual_fundamentals(self, ticker: str, years: int) -> dict:
        return {}

    def get_annual_avg_prices(self, ticker: str, years: int) -> pd.Series:
        return pd.Series(dtype=float)

    def get_annual_dividends(self, ticker: str) -> pd.Series:
        return pd.Series(dtype=float)

    def get_price_history(self, ticker: str, period: str = "1y") -> pd.DataFrame:
        return pd.DataFrame()

    def get_market_snapshot(self, ticker: str) -> dict:
        return {"current_price": float("nan"), "shares_outstanding": None}


class ErrorDataSource(FinancialDataSource):
    """Raises ValueError on every call to test 404 propagation."""

    def get_annual_fundamentals(self, ticker: str, years: int) -> dict:
        raise ValueError(f"No filings for '{ticker}'")

    def get_annual_avg_prices(self, ticker: str, years: int) -> pd.Series:
        raise ValueError(f"No prices for '{ticker}'")

    def get_annual_dividends(self, ticker: str) -> pd.Series:
        return pd.Series(dtype=float)

    def get_price_history(self, ticker: str, period: str = "1y") -> pd.DataFrame:
        return pd.DataFrame()

    def get_market_snapshot(self, ticker: str) -> dict:
        return {}
