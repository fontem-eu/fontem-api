"""
FastAPI dependency that provides a FinancialDataSource.

In production:  returns a LiveDataSource (real EDGAR + yfinance).
In unit tests:  override with app.dependency_overrides[get_data_source].
"""
from __future__ import annotations

from functools import lru_cache

from src.analysis.gmr_data_source import FinancialDataSource
from src.data.live_data_source import LiveDataSource


@lru_cache(maxsize=1)
def _live_source() -> LiveDataSource:
    """Singleton live data source — constructed once per process."""
    return LiveDataSource()


def get_data_source() -> FinancialDataSource:
    """
    Dependency injected into every GMR endpoint.
    Override in tests::

        from src.api.dependencies import get_data_source
        app.dependency_overrides[get_data_source] = lambda: MyMock()
    """
    return _live_source()
