"""
Graph Data Source
==================
FinancialDataSource backed by Neo4j (entities + financials) and NFS CSVs
(price time series).  Replaces RoutingDataSource + EsefDataSource +
LiveDataSource once fully operational.
"""
from __future__ import annotations

import logging

import pandas as pd

from ...analysis.gmr_data_source import FinancialDataSource, MarketSnapshot
from ..north_america.local_edgar_fetcher import LocalEdgarFetcher
from ..north_america.local_price_fetcher import LocalPriceFetcher
from .neo4j_client import Neo4jClient

logger = logging.getLogger(__name__)

_FUNDAMENTALS_KEYS = [
    "revenue", "gross_profit", "operating_income", "net_income",
    "total_assets", "total_liabilities", "equity",
    "operating_cashflow", "capex", "free_cashflow",
    "current_assets", "current_liabilities",
    "inventory", "prepaid_expenses", "shares_outstanding", "eps",
    "long_term_debt", "cash_and_equivalents",
    "depreciation_amortization", "interest_expense", "income_tax_expense",
]

# Mapping from FinancialYear Neo4j properties to our standard keys
_NEO4J_PROP_MAP = {
    "revenue": "revenue",
    "gross_profit": "gross_profit",
    "operating_income": "operating_income",
    "net_income": "net_income",
    "total_assets": "total_assets",
    "total_liabilities": "total_liabilities",
    "equity": "equity",
    "operating_cashflow": "operating_cashflow",
    "capex": "capex",
    "free_cashflow": "free_cashflow",
    "current_assets": "current_assets",
    "current_liabilities": "current_liabilities",
    "inventory": "inventory",
    "prepaid_expenses": "prepaid_expenses",
    "shares_outstanding": "shares_outstanding",
    "eps": "eps",
    "long_term_debt": "long_term_debt",
    "cash": "cash_and_equivalents",
    "interest_expense": "interest_expense",
    "income_tax_expense": "income_tax_expense",
    "depreciation_amortization": "depreciation_amortization",
}


class GraphDataSource(FinancialDataSource):
    """
    Production data source backed by Neo4j (company/financial data)
    and NFS CSV files (price time series).

    For US tickers with no FinancialYear nodes in the graph, falls back
    to LocalEdgarFetcher to read bulk EDGAR data.
    """

    def __init__(
        self,
        neo4j_client: Neo4jClient,
        price_data_dir: str,
        edgar_data_dir: str | None = None,
    ) -> None:
        self._neo4j = neo4j_client
        self._prices = LocalPriceFetcher(price_data_dir)
        self._edgar = (
            LocalEdgarFetcher(edgar_data_dir) if edgar_data_dir else None
        )
        logger.info("GraphDataSource ready")

    # ------------------------------------------------------------------
    # FinancialDataSource interface
    # ------------------------------------------------------------------

    def get_annual_fundamentals(  # pylint: disable=too-many-locals
        self, ticker: str, years: int = 10,
    ) -> dict:
        """Fetch financials from Neo4j; fall back to EDGAR for US tickers."""
        with self._neo4j.session() as session:
            rows = session.run(
                """
                MATCH (l:Listing {ticker: $ticker})<-[:LISTED_AS]-(c)
                      -[:REPORTED]->(f:FinancialYear)
                RETURN f ORDER BY f.year DESC LIMIT $years
                """,
                ticker=ticker, years=years,
            ).data()

        if rows:
            return self._rows_to_fundamentals(rows)

        # Fallback: EDGAR for US tickers without graph financials
        if self._edgar:
            try:
                return self._edgar.fetch_fundamentals(ticker, years=years)
            except (ValueError, FileNotFoundError) as exc:
                logger.debug(
                    "EDGAR fallback failed for %s: %s", ticker, exc
                )

        return {k: pd.Series(dtype=float) for k in _FUNDAMENTALS_KEYS}

    def get_annual_avg_prices(
        self, ticker: str, years: int = 10,
    ) -> pd.Series:
        """Annual average prices from local CSV."""
        return self._prices.get_annual_avg_prices(
            ticker, period=f"{min(years, 10)}y"
        )

    def get_annual_dividends(self, ticker: str) -> pd.Series:
        """Dividends from price CSV (if available)."""
        return self._prices.get_annual_dividends(ticker)

    def get_price_history(
        self, ticker: str, period: str = "1y",
    ) -> pd.DataFrame:
        """Price history from NFS CSV."""
        return self._prices.get_history(ticker, period=period)

    def get_market_snapshot(self, ticker: str) -> MarketSnapshot:
        """Company metadata from graph + price data from CSV."""
        snap = self._prices.get_snapshot(ticker)
        return MarketSnapshot(
            current_price=snap.get("current_price") or None,
            avg_volume=snap.get("avg_volume") or None,
            shares_outstanding=snap.get("shares_outstanding"),
            last_dividend_date=(
                snap.get("last_dividend", {}).get("date")
            ),
            last_dividend_amount=(
                snap.get("last_dividend", {}).get("amount")
            ),
            splits=snap.get("splits", pd.Series(dtype=float)),
            latest_quarter=snap.get("latest_quarter") or {},
            beta=snap.get("beta"),
            week_52_high=snap.get("week_52_high"),
            week_52_low=snap.get("week_52_low"),
            market_cap=snap.get("market_cap"),
        )

    def get_data_source_name(self, ticker: str) -> str:
        """Return 'esef' or 'edgar' based on FinancialYear.source."""
        with self._neo4j.session() as session:
            row = session.run(
                """
                MATCH (l:Listing {ticker: $ticker})<-[:LISTED_AS]-(c)
                      -[:REPORTED]->(f:FinancialYear)
                RETURN f.source AS source LIMIT 1
                """,
                ticker=ticker,
            ).single()
        if row and row["source"]:
            return row["source"].lower()
        # Fallback: check if it's a known US listing
        with self._neo4j.session() as session:
            row = session.run(
                """
                MATCH (l:Listing {ticker: $ticker})<-[:LISTED_AS]-(c)
                RETURN c.country AS country LIMIT 1
                """,
                ticker=ticker,
            ).single()
        if row and row["country"] == "US":
            return "edgar"
        return "esef"

    # ------------------------------------------------------------------
    # Ticker discovery
    # ------------------------------------------------------------------

    def get_available_tickers(self) -> list[dict]:
        """Return all listings from the graph."""
        with self._neo4j.session() as session:
            rows = session.run("""
                MATCH (c:Company)-[:LISTED_AS]->(l:Listing)
                RETURN l.ticker AS ticker, l.ticker AS symbol,
                       c.name AS name, l.exchange AS exchange,
                       c.country AS country, l.currency AS currency,
                       l.active AS is_active
            """).data()
        result = []
        for r in rows:
            r["search_name"] = (
                f"{r.get('name', '')} {r.get('ticker', '')}".lower()
            )
            r["search_keywords"] = r["search_name"]
            r["data_source"] = (
                "esef" if r.get("currency") != "USD" else "edgar"
            )
            result.append(r)
        return result

    def search_tickers(
        self, query: str, limit: int = 10,
    ) -> list[dict]:
        """Full-text search on company name + ticker."""
        if not query:
            return self.get_available_tickers()[:limit]

        with self._neo4j.session() as session:
            rows = session.run(
                """
                MATCH (c:Company)-[:LISTED_AS]->(l:Listing)
                WHERE toLower(c.name) CONTAINS toLower($q)
                   OR toLower(l.ticker) CONTAINS toLower($q)
                RETURN l.ticker AS ticker, l.ticker AS symbol,
                       c.name AS name, l.exchange AS exchange,
                       c.country AS country, l.currency AS currency,
                       l.active AS is_active
                LIMIT $limit
                """,
                q=query, limit=limit,
            ).data()

        result = []
        for r in rows:
            r["search_name"] = (
                f"{r.get('name', '')} {r.get('ticker', '')}".lower()
            )
            r["search_keywords"] = r["search_name"]
            r["data_source"] = (
                "esef" if r.get("currency") != "USD" else "edgar"
            )
            result.append(r)
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _rows_to_fundamentals(rows: list[dict]) -> dict:
        """Convert Neo4j FinancialYear rows to the standard dict format."""
        data: dict[str, dict[int, float | None]] = {
            k: {} for k in _FUNDAMENTALS_KEYS
        }

        for row in rows:
            f = row["f"]
            year = f.get("year")
            if year is None:
                continue
            for neo4j_prop, key in _NEO4J_PROP_MAP.items():
                val = f.get(neo4j_prop)
                if val is not None:
                    data[key][year] = val

        result: dict[str, pd.Series] = {}
        for key in _FUNDAMENTALS_KEYS:
            if data[key]:
                series = pd.Series(data[key], dtype=float)
                series.index = series.index.astype(int)
                series = series.sort_index(ascending=False)
                result[key] = series
            else:
                result[key] = pd.Series(dtype=float)

        return result
