"""
Unit tests for RoutingDataSource.
Uses lightweight in-memory stubs — no files, no network.

Routing is now registry-based: a ticker is EU if it appears in the EU
source's get_available_tickers() registry; everything else routes to NA.
This means BRK.A correctly routes to NA even though it matches the old
dot-suffix heuristic.
"""
from __future__ import annotations
# pylint: disable=missing-function-docstring,redefined-outer-name

import pytest
import pandas as pd

from src.analysis.gmr_data_source import FinancialDataSource, MarketSnapshot
from src.data.routing_data_source import RoutingDataSource


# ---------------------------------------------------------------------------
# Minimal stubs
# ---------------------------------------------------------------------------

class _StubSource(FinancialDataSource):
    """Records which tickers were routed to it."""

    def __init__(self, name: str, tickers: list[dict] | None = None) -> None:
        self.name = name
        self._tickers = tickers or []
        self.calls: list[str] = []

    def get_annual_fundamentals(self, ticker: str, years: int) -> dict:
        self.calls.append(ticker)
        return {"_routed_to": self.name}

    def get_annual_avg_prices(self, ticker: str, years: int) -> pd.Series:
        self.calls.append(ticker)
        return pd.Series({"_source": self.name})

    def get_annual_dividends(self, ticker: str) -> pd.Series:
        return pd.Series(dtype=float)

    def get_price_history(self, ticker: str, period: str = "1y") -> pd.DataFrame:
        return pd.DataFrame()

    def get_market_snapshot(self, ticker: str) -> MarketSnapshot:
        self.calls.append(ticker)
        return MarketSnapshot()

    def get_available_tickers(self) -> list[dict]:
        return self._tickers

    def search_tickers(self, query: str, limit: int = 10) -> list[dict]:
        return [t for t in self._tickers if query.lower() in t.get("symbol", "").lower()][:limit]

    def get_data_source_name(self, ticker: str) -> str:  # pylint: disable=unused-argument
        return "esef" if self.name == "eu" else "edgar"


@pytest.fixture()
def na_stub() -> _StubSource:
    return _StubSource("na", tickers=[{"symbol": "AAPL", "name": "Apple Inc."}])


@pytest.fixture()
def eu_stub() -> _StubSource:
    # EU tickers carry a "ticker" field with the full exchange-suffix symbol
    return _StubSource("eu", tickers=[
        {"symbol": "ASML", "ticker": "ASML.AS", "name": "ASML Holding"},
        {"symbol": "SAP",  "ticker": "SAP.DE",  "name": "SAP SE"},
    ])


@pytest.fixture()
def router(na_stub: _StubSource, eu_stub: _StubSource) -> RoutingDataSource:
    return RoutingDataSource(na_source=na_stub, eu_source=eu_stub)


# ---------------------------------------------------------------------------
# Registry building
# ---------------------------------------------------------------------------

def test_eu_tickers_indexed_at_init(router: RoutingDataSource):
    # pylint: disable=protected-access
    assert "ASML.AS" in router._eu_tickers
    assert "SAP.DE"  in router._eu_tickers


def test_na_ticker_not_in_eu_index(router: RoutingDataSource):
    # pylint: disable=protected-access
    assert "AAPL" not in router._eu_tickers


def test_dot_suffix_na_ticker_not_in_eu_index(router: RoutingDataSource):
    """BRK.A looks like an EU ticker by regex but is not in the EU registry."""
    # pylint: disable=protected-access
    assert "BRK.A" not in router._eu_tickers


# ---------------------------------------------------------------------------
# Routing — fundamentals
# ---------------------------------------------------------------------------

def test_eu_ticker_routed_to_eu(router: RoutingDataSource, eu_stub: _StubSource):
    result = router.get_annual_fundamentals("ASML.AS", years=5)
    assert result.get("_routed_to") == "eu"
    assert "ASML.AS" in eu_stub.calls


def test_na_ticker_routed_to_na(router: RoutingDataSource, na_stub: _StubSource):
    result = router.get_annual_fundamentals("AAPL", years=5)
    assert result.get("_routed_to") == "na"
    assert "AAPL" in na_stub.calls


def test_dot_suffix_na_ticker_routed_to_na(router: RoutingDataSource, na_stub: _StubSource):
    """BRK.A must route to NA, not EU, because it is not in the EU registry."""
    router.get_annual_fundamentals("BRK.A", years=5)
    assert "BRK.A" in na_stub.calls


# ---------------------------------------------------------------------------
# Routing — market snapshot
# ---------------------------------------------------------------------------

def test_eu_market_snapshot_routed(router: RoutingDataSource, eu_stub: _StubSource):
    router.get_market_snapshot("SAP.DE")
    assert "SAP.DE" in eu_stub.calls


def test_na_market_snapshot_routed(router: RoutingDataSource, na_stub: _StubSource):
    router.get_market_snapshot("MSFT")
    assert "MSFT" in na_stub.calls


# ---------------------------------------------------------------------------
# get_data_source_name — registry-aware
# ---------------------------------------------------------------------------

def test_get_data_source_name_eu(router: RoutingDataSource):
    assert router.get_data_source_name("ASML.AS") == "esef"
    assert router.get_data_source_name("SAP.DE")  == "esef"


def test_get_data_source_name_na(router: RoutingDataSource):
    assert router.get_data_source_name("AAPL") == "edgar"
    assert router.get_data_source_name("MSFT") == "edgar"


def test_get_data_source_name_dot_na(router: RoutingDataSource):
    """BRK.A is not in the EU registry — must report 'edgar'."""
    assert router.get_data_source_name("BRK.A") == "edgar"


# ---------------------------------------------------------------------------
# Ticker discovery — EU-first merge
# ---------------------------------------------------------------------------

def test_get_available_tickers_eu_first(router: RoutingDataSource):
    tickers = router.get_available_tickers()
    symbols = [t.get("ticker") or t.get("symbol") for t in tickers]
    # EU tickers come before the NA one
    assert symbols.index("ASML.AS") < symbols.index("AAPL")


def test_search_tickers_eu_first(router: RoutingDataSource):
    results = router.search_tickers("", limit=10)
    symbols = [t.get("ticker") or t.get("symbol") for t in results]
    assert "ASML.AS" in symbols
    assert "AAPL" in symbols
    assert symbols.index("ASML.AS") < symbols.index("AAPL")


def test_search_tickers_limit_total(router: RoutingDataSource):
    results = router.search_tickers("", limit=1)
    assert len(results) == 1
    # EU result wins (EU-first)
    assert results[0].get("ticker") == "ASML.AS"
