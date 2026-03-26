"""
Unit tests for RoutingDataSource and get_data_source_name.
Uses lightweight in-memory stubs — no files, no network.
"""
from __future__ import annotations
# pylint: disable=missing-function-docstring,redefined-outer-name

import pytest
import pandas as pd

from src.analysis.gmr_data_source import FinancialDataSource
from src.data.routing_data_source import RoutingDataSource, get_data_source_name


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

    def get_market_snapshot(self, ticker: str) -> dict:
        self.calls.append(ticker)
        return {"_source": self.name}

    def get_available_tickers(self) -> list[dict]:
        return self._tickers

    def search_tickers(self, query: str, limit: int = 10) -> list[dict]:
        return [t for t in self._tickers if query.lower() in t.get("symbol", "").lower()][:limit]


@pytest.fixture()
def na_stub() -> _StubSource:
    return _StubSource("na", tickers=[{"symbol": "AAPL", "name": "Apple Inc."}])


@pytest.fixture()
def eu_stub() -> _StubSource:
    return _StubSource("eu", tickers=[{"symbol": "ASML", "name": "ASML Holding"}])


@pytest.fixture()
def router(na_stub: _StubSource, eu_stub: _StubSource) -> RoutingDataSource:
    return RoutingDataSource(na_source=na_stub, eu_source=eu_stub)


# ---------------------------------------------------------------------------
# get_data_source_name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ticker,expected", [
    ("ASML.AS",  "esef"),
    ("SAP.DE",   "esef"),
    ("VOW3.DE",  "esef"),
    ("AAPL",     "edgar"),
    ("MSFT",     "edgar"),
    ("AAPL1234", "edgar"),
])
def test_get_data_source_name(ticker, expected):
    assert get_data_source_name(ticker) == expected


def test_get_data_source_name_na():
    assert get_data_source_name("AAPL") == "edgar"
    assert get_data_source_name("MSFT") == "edgar"


def test_get_data_source_name_eu():
    assert get_data_source_name("ASML.AS") == "esef"
    assert get_data_source_name("SAP.DE") == "esef"


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


def test_eu_market_snapshot_routed(router: RoutingDataSource):
    snap = router.get_market_snapshot("SAP.DE")
    assert snap.get("_source") == "eu"


def test_na_market_snapshot_routed(router: RoutingDataSource):
    snap = router.get_market_snapshot("MSFT")
    assert snap.get("_source") == "na"


# ---------------------------------------------------------------------------
# Ticker discovery — EU-first merge
# ---------------------------------------------------------------------------

def test_get_available_tickers_eu_first(router: RoutingDataSource):
    tickers = router.get_available_tickers()
    symbols = [t["symbol"] for t in tickers]
    assert symbols.index("ASML") < symbols.index("AAPL")


def test_search_tickers_eu_first(router: RoutingDataSource):
    # Both stubs return results for different queries; test combined ordering
    results = router.search_tickers("", limit=10)
    # EU stub result should appear before NA
    symbols = [t["symbol"] for t in results]
    assert symbols[0] == "ASML"
    assert "AAPL" in symbols


def test_search_tickers_limit_total(router: RoutingDataSource):
    results = router.search_tickers("", limit=1)
    assert len(results) == 1
    assert results[0]["symbol"] == "ASML"  # EU first
