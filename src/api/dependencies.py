"""
FastAPI dependency that provides a FinancialDataSource.

In production:  returns a LiveDataSource (real EDGAR + yfinance).
                Set EDGAR_USE_LOCAL_DATA=1 and EDGAR_LOCAL_DATA_DIR=/edgar-data/full
                to read EDGAR fundamentals from local bulk data instead.
In unit tests:  override with app.dependency_overrides[get_data_source].
"""
from __future__ import annotations

import os
from functools import lru_cache

from src.analysis.gmr_data_source import FinancialDataSource
from src.data.live_data_source import LiveDataSource


@lru_cache(maxsize=1)
def _live_source() -> LiveDataSource:
    """Singleton data source — constructed once per process.

    Reads EDGAR_USE_LOCAL_DATA and EDGAR_LOCAL_DATA_DIR at startup to decide
    whether to use local bulk data or live SEC API for fundamentals.
    """
    local_dir: str | None = None
    if os.environ.get("EDGAR_USE_LOCAL_DATA") == "1":
        # Use GMR_EDGAR_LOCAL_DATA_DIR (not EDGAR_LOCAL_DATA_DIR) to avoid
        # edgartools reading it at import time and trying to write _tcache
        # into the read-only volume.
        local_dir = os.environ.get("GMR_EDGAR_LOCAL_DATA_DIR")

    return LiveDataSource(local_data_dir=local_dir)


def get_data_source() -> FinancialDataSource:
    """
    Dependency injected into every GMR endpoint.
    Override in tests::

        from src.api.dependencies import get_data_source
        app.dependency_overrides[get_data_source] = lambda: MyMock()
    """
    return _live_source()
