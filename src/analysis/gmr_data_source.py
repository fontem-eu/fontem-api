"""
GMR Data Source — Abstract Port & Settings
==========================================
Defines the data contract that GMRLong and GMRShort depend on.
Concrete adapters (LiveDataSource, MockDataSource) implement this interface,
enabling full unit-testability with zero network traffic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import pandas as pd


@dataclass
class GMRSettings:  # pylint: disable=too-many-instance-attributes
    """
    Thresholds that drive pass/fail decisions in both GMR screens.
    Mirrors the defaults in gmrtool/GMRTool/Settings.cs.
    """
    # ── Long-term thresholds ────────────────────────────────────────────
    pb_value: float = 1.5        # Price-to-Book   ≤ pb_value     ✅
    pe: int = 15                 # Price-to-Earnings ≤ pe          ✅
    dividend_yield: float = 0.035  # ≥ 3.5 % (expressed as 0.035) ✅
    debt_equity: float = 1.5    # Total Liabilities / Equity      ✅
    roe: int = 15                # Return on Equity  ≥ 15 %        ✅
    net_profit_margin: int = 15  # Net Profit Margin ≥ 15 %        ✅
    years_for_avg: int = 5       # historical look-back window

    # ── Short-term thresholds ───────────────────────────────────────────
    win_probability: float = 0.50  # fraction of positive-return days > 0.50
    diff_mat: float = -0.025       # (MAT − price) / price  > −0.025
    trigger_v_up: float = 0.30    # avg monthly VUp  > 0.30
    trigger_v_down: float = -0.30  # avg monthly VDown < −0.30
    min_volume: float = 1_000_000  # average daily trading volume
    min_price: float = 0.40        # current share price lower bound
    max_price: float = 2.50        # current share price upper bound


@dataclass(eq=False)
class MarketSnapshot:  # pylint: disable=too-many-instance-attributes
    """
    Typed market snapshot returned by ``get_market_snapshot()``.

    ``eq=False`` avoids pd.Series equality issues in dataclass comparison.
    All fields default to ``None`` / empty so callers can work with partial
    data (e.g. ESEF sources that have no live price feed).
    """
    current_price: float | None = None
    avg_volume: float | None = None
    shares_outstanding: float | None = None
    # Dividend — flattened from the historic {"date": …, "amount": …} dict
    last_dividend_date: str | None = None
    last_dividend_amount: float | None = None
    # Time-series / nested fields
    splits: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    latest_quarter: dict = field(default_factory=dict)
    # Optional extra market data
    beta: float | None = None
    week_52_high: float | None = None
    week_52_low: float | None = None
    market_cap: float | None = None


class FinancialDataSource(ABC):
    """
    Port / interface for all financial data needed by the GMR screens
    and any other analysis built on top of this project.
    Inject a concrete implementation (LiveDataSource or a test mock).
    """

    @abstractmethod
    def get_annual_fundamentals(self, ticker: str, years: int) -> dict:
        """
        Return a dict of annual financial series, each a pd.Series indexed
        by integer fiscal year (descending).

        Expected keys:
            revenue, net_income, total_assets, total_liabilities, equity,
            operating_cashflow, capex, free_cashflow,
            current_assets, current_liabilities,
            inventory, prepaid_expenses, shares_outstanding, eps,
            long_term_debt, cash_and_equivalents,
            depreciation_amortization, interest_expense, income_tax_expense
        """

    @abstractmethod
    def get_annual_avg_prices(self, ticker: str, years: int) -> pd.Series:
        """Average daily close for each calendar year (int index, descending)."""

    @abstractmethod
    def get_annual_dividends(self, ticker: str) -> pd.Series:
        """Total dividends paid per calendar year (int index, descending)."""

    @abstractmethod
    def get_price_history(self, ticker: str, period: str = "1y") -> pd.DataFrame:
        """Daily OHLCV with tz-naive DatetimeIndex (Open, High, Low, Close, Volume)."""

    @abstractmethod
    def get_market_snapshot(self, ticker: str) -> MarketSnapshot:
        """Return a typed MarketSnapshot for the given ticker."""

    @abstractmethod
    def get_available_tickers(self) -> list[dict]:
        """Return all available tickers with metadata for discovery."""

    @abstractmethod
    def search_tickers(self, query: str, limit: int = 10) -> list[dict]:
        """Search tickers by name, symbol, or keywords (case-insensitive)."""

    @abstractmethod
    def get_data_source_name(self, ticker: str) -> str:
        """
        Return the canonical source name for this ticker.
        Concrete classes return a fixed string (e.g. ``'edgar'``, ``'esef'``).
        RoutingDataSource delegates to whichever sub-source owns the ticker.
        """


# Alias kept for backward compatibility with tests and legacy call sites.
GMRDataSource = FinancialDataSource
