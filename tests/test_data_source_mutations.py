"""
Tests for FinancialDataSource and GMRDataSource — verify abstract contract
enforcement and default parameter behavior through the public interface.
"""
# Each test method in this file constructs an inline `class Partial(FinancialDataSource)`
# that re-declares the protocol's stub methods on one line: `def m(self, t, y): return ...`.
# That's the *point* — we're asserting the abstract surface, not implementing it. Hence:
#   arguments-renamed (W0237) — params shortened to t/y/p/q for terseness; the override
#       relationship is the contract being tested.
#   multiple-statements (C0321) — `def m(...): return ...` keeps each stub on one line.
#   unused-argument (W0613) — every parameter is by definition unused; the stub just
#       returns the empty value the test expects.
# pylint: disable=missing-function-docstring,missing-class-docstring,abstract-class-instantiated,too-few-public-methods,arguments-renamed,multiple-statements,unused-argument
import pytest
import pandas as pd

from src.analysis.gmr_data_source import (
    GMRDataSource, FinancialDataSource, MarketSnapshot,
)


class TestFinancialDataSourceAbstract:
    """Verify that the abstract base class enforces its contract."""

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            FinancialDataSource()

    def test_gmr_data_source_cannot_instantiate(self):
        with pytest.raises(TypeError):
            GMRDataSource()

    def test_missing_get_annual_fundamentals(self):
        class Partial(FinancialDataSource):
            def get_annual_avg_prices(self, t, y): return pd.Series()
            def get_annual_dividends(self, t): return pd.Series()
            def get_price_history(self, t, p="1y"): return pd.DataFrame()
            def get_market_snapshot(self, t): return MarketSnapshot()
            def get_available_tickers(self): return []
            def search_tickers(self, q, limit=10): return []
            def get_data_source_name(self, t): return ""
        with pytest.raises(TypeError):
            Partial()

    def test_missing_search_tickers(self):
        class Partial(FinancialDataSource):
            def get_annual_fundamentals(self, t, y): return {}
            def get_annual_avg_prices(self, t, y): return pd.Series()
            def get_annual_dividends(self, t): return pd.Series()
            def get_price_history(self, t, p="1y"): return pd.DataFrame()
            def get_market_snapshot(self, t): return MarketSnapshot()
            def get_available_tickers(self): return []
            def get_data_source_name(self, t): return ""
        with pytest.raises(TypeError):
            Partial()


class TestDataSourceDefaultParameters:
    """Verify that default parameter values on public methods work correctly."""

    def test_search_tickers_default_limit(self):
        class DS(GMRDataSource):
            def get_annual_fundamentals(self, t, y): return {}
            def get_annual_avg_prices(self, t, y): return pd.Series()
            def get_annual_dividends(self, t): return pd.Series()
            def get_price_history(self, t, p="1y"): return pd.DataFrame()
            def get_market_snapshot(self, t): return MarketSnapshot()
            def get_available_tickers(self): return []
            def search_tickers(self, q, limit=10): return [{"limit": limit}]
            def get_data_source_name(self, t): return ""
        result = DS().search_tickers("test")
        assert result[0]["limit"] == 10

    def test_price_history_default_period(self):
        class DS(GMRDataSource):
            def get_annual_fundamentals(self, t, y): return {}
            def get_annual_avg_prices(self, t, y): return pd.Series()
            def get_annual_dividends(self, t): return pd.Series()
            def get_price_history(self, t, p="1y"):
                return pd.DataFrame({"period": [p]})
            def get_market_snapshot(self, t): return MarketSnapshot()
            def get_available_tickers(self): return []
            def search_tickers(self, q, limit=10): return []
            def get_data_source_name(self, t): return ""
        result = DS().get_price_history("X")
        assert result["period"].iloc[0] == "1y"
