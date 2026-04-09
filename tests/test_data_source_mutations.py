"""
Mutation-killing tests for MarketSnapshot defaults and FinancialDataSource abstracts.
"""
# pylint: disable=missing-function-docstring,missing-class-docstring,abstract-class-instantiated,too-few-public-methods
import pytest
import pandas as pd

from src.analysis.gmr_data_source import (
    GMRDataSource, FinancialDataSource, MarketSnapshot,
)


class TestMarketSnapshotDefaults:
    def test_current_price_default(self):
        assert MarketSnapshot().current_price is None

    def test_avg_volume_default(self):
        assert MarketSnapshot().avg_volume is None

    def test_shares_outstanding_default(self):
        assert MarketSnapshot().shares_outstanding is None

    def test_last_dividend_date_default(self):
        assert MarketSnapshot().last_dividend_date is None

    def test_last_dividend_amount_default(self):
        assert MarketSnapshot().last_dividend_amount is None

    def test_splits_default_is_empty_series(self):
        s = MarketSnapshot().splits
        assert isinstance(s, pd.Series)
        assert s.empty

    def test_splits_default_is_fresh_instance(self):
        s1 = MarketSnapshot()
        s2 = MarketSnapshot()
        assert s1.splits is not s2.splits

    def test_latest_quarter_default_is_empty_dict(self):
        assert MarketSnapshot().latest_quarter == {}

    def test_latest_quarter_default_is_fresh_instance(self):
        q1 = MarketSnapshot().latest_quarter
        q2 = MarketSnapshot().latest_quarter
        assert q1 is not q2

    def test_beta_default(self):
        assert MarketSnapshot().beta is None

    def test_week_52_high_default(self):
        assert MarketSnapshot().week_52_high is None

    def test_week_52_low_default(self):
        assert MarketSnapshot().week_52_low is None

    def test_market_cap_default(self):
        assert MarketSnapshot().market_cap is None


class TestMarketSnapshotEqFalse:
    """eq=False means two snapshots with same data are not equal via ==."""
    def test_same_data_not_equal(self):
        a = MarketSnapshot(current_price=10.0)
        b = MarketSnapshot(current_price=10.0)
        assert a is not b
        # With eq=False, == falls back to identity
        assert not (a == b)


class TestFinancialDataSourceAbstract:
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

    def test_search_tickers_default_limit(self):
        """search_tickers has default limit=10 in signature."""
        class Complete(GMRDataSource):
            def get_annual_fundamentals(self, t, y): return {}
            def get_annual_avg_prices(self, t, y): return pd.Series()
            def get_annual_dividends(self, t): return pd.Series()
            def get_price_history(self, t, p="1y"): return pd.DataFrame()
            def get_market_snapshot(self, t): return MarketSnapshot()
            def get_available_tickers(self): return []
            def search_tickers(self, q, limit=10): return [{"limit": limit}]
            def get_data_source_name(self, t): return ""
        ds = Complete()
        result = ds.search_tickers("test")
        assert result[0]["limit"] == 10

    def test_price_history_default_period(self):
        """get_price_history has default period='1y'."""
        class Complete(GMRDataSource):
            def get_annual_fundamentals(self, t, y): return {}
            def get_annual_avg_prices(self, t, y): return pd.Series()
            def get_annual_dividends(self, t): return pd.Series()
            def get_price_history(self, t, p="1y"):
                return pd.DataFrame({"period": [p]})
            def get_market_snapshot(self, t): return MarketSnapshot()
            def get_available_tickers(self): return []
            def search_tickers(self, q, limit=10): return []
            def get_data_source_name(self, t): return ""
        ds = Complete()
        result = ds.get_price_history("X")
        assert result["period"].iloc[0] == "1y"
