"""Tests for GraphDataSource."""
from unittest.mock import MagicMock, patch

import pandas as pd

from src.data.graph.graph_data_source import GraphDataSource


def _make_source(session_data=None, edgar=None):
    """Create a GraphDataSource with a mocked Neo4j client and price fetcher."""
    neo4j = MagicMock()
    session = MagicMock()
    neo4j.session.return_value.__enter__ = MagicMock(return_value=session)
    neo4j.session.return_value.__exit__ = MagicMock(return_value=False)

    if session_data is not None:
        session.run.return_value.data.return_value = session_data
        session.run.return_value.single.return_value = (
            session_data[0] if session_data else None
        )

    with patch(
        "src.data.graph.graph_data_source.LocalPriceFetcher"
    ) as mock_price_cls, patch(
        "src.data.graph.graph_data_source.LocalEdgarFetcher",
        return_value=edgar,
    ) if edgar else patch(
        "src.data.graph.graph_data_source.LocalEdgarFetcher",
        return_value=None,
    ):
        mock_price = MagicMock()
        mock_price_cls.return_value = mock_price
        ds = GraphDataSource(
            neo4j_client=neo4j,
            price_data_dir="/fake/prices",
            edgar_data_dir="/fake/edgar" if edgar else None,
        )
    return ds, session, mock_price


# ── get_annual_fundamentals ──────────────────────────────────────────

def test_fundamentals_from_graph():
    """Financials are read from Neo4j FinancialYear nodes."""
    rows = [
        {"f": {"year": 2024, "revenue": 2225601000.0,
               "net_income": 925163000.0, "eps": 29.69}},
        {"f": {"year": 2023, "revenue": 1863406000.0,
               "net_income": 698322000.0, "eps": 22.52}},
    ]
    ds, _, _ = _make_source(session_data=rows)
    result = ds.get_annual_fundamentals("ADYEN.AS")
    assert not result["revenue"].empty
    assert result["revenue"].iloc[0] == 2225601000.0
    assert result["eps"].iloc[1] == 22.52


def test_fundamentals_fallback_to_edgar():
    """When graph has no financials, falls back to LocalEdgarFetcher."""
    edgar = MagicMock()
    edgar.fetch_fundamentals.return_value = {"revenue": pd.Series({2024: 100.0})}
    ds, session, _ = _make_source(session_data=[], edgar=edgar)
    result = ds.get_annual_fundamentals("AAPL")
    edgar.fetch_fundamentals.assert_called_once_with("AAPL", years=10)
    assert result["revenue"].iloc[0] == 100.0


def test_fundamentals_empty_when_no_source():
    """Returns empty series when neither graph nor EDGAR has data."""
    ds, _, _ = _make_source(session_data=[])
    result = ds.get_annual_fundamentals("UNKNOWN.XX")
    assert all(s.empty for s in result.values())


# ── get_price_history ────────────────────────────────────────────────

def test_price_history_delegates_to_price_fetcher():
    """Price history reads from LocalPriceFetcher."""
    ds, _, mock_price = _make_source(session_data=[])
    expected = pd.DataFrame({"Close": [100.0]})
    mock_price.get_history.return_value = expected
    result = ds.get_price_history("AAPL", "1y")
    mock_price.get_history.assert_called_once_with("AAPL", period="1y")
    assert len(result) == 1


# ── get_annual_avg_prices ────────────────────────────────────────────

def test_annual_avg_prices_delegates():
    """Annual avg prices delegates to LocalPriceFetcher."""
    ds, _, mock_price = _make_source(session_data=[])
    mock_price.get_annual_avg_prices.return_value = pd.Series({2024: 50.0})
    result = ds.get_annual_avg_prices("AAPL", years=5)
    mock_price.get_annual_avg_prices.assert_called_once_with("AAPL", period="5y")
    assert result.iloc[0] == 50.0


# ── search_tickers ───────────────────────────────────────────────────

def test_search_tickers_returns_enriched_results():
    """Search results include search_name and data_source fields."""
    rows = [
        {"ticker": "ADYEN.AS", "symbol": "ADYEN.AS", "name": "Adyen N.V.",
         "exchange": "AS", "country": "NL", "currency": "EUR",
         "is_active": True},
    ]
    ds, _, _ = _make_source(session_data=rows)
    results = ds.search_tickers("ADYEN")
    assert len(results) == 1
    assert results[0]["data_source"] == "esef"
    assert "adyen" in results[0]["search_name"]


# ── get_data_source_name ─────────────────────────────────────────────

def test_data_source_name_from_financial_year():
    """Returns the source field from FinancialYear."""
    ds, session, _ = _make_source()
    session.run.return_value.single.return_value = {"source": "ESEF"}
    result = ds.get_data_source_name("ADYEN.AS")
    assert result == "esef"


# ── get_market_snapshot ──────────────────────────────────────────────

def test_market_snapshot_from_price_data():
    """Market snapshot reads price data from CSV."""
    ds, _, mock_price = _make_source(session_data=[])
    mock_price.get_snapshot.return_value = {
        "current_price": 1500.0,
        "avg_volume": 200000.0,
        "shares_outstanding": None,
        "last_dividend": {"date": None, "amount": 0.0},
        "splits": pd.Series(dtype=float),
        "latest_quarter": {},
        "week_52_high": 1600.0,
        "week_52_low": 1200.0,
        "beta": None,
        "market_cap": None,
    }
    snap = ds.get_market_snapshot("ADYEN.AS")
    assert snap.current_price == 1500.0
    assert snap.week_52_high == 1600.0
