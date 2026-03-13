"""
GMR Data Source — Abstract Port & Settings
==========================================
Defines the data contract that GMRLong and GMRShort depend on.
Concrete adapters (LiveDataSource, MockDataSource) implement this interface,
enabling full unit-testability with zero network traffic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

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
            inventory, prepaid_expenses, shares_outstanding, eps
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
    def get_market_snapshot(self, ticker: str) -> dict:
        """
        Return a dict with:
            current_price  (float)
            avg_volume     (float)
            shares_outstanding (float | None)
            last_dividend  (dict: {"date": str, "amount": float})
            splits         (pd.Series)
            latest_quarter (dict)
        """


# Backward-compatible alias — will be removed once all call sites are updated.
GMRDataSource = FinancialDataSource
