"""
FastAPI dependency that provides the application's FinancialDataSource.

Always uses local files:
  GMR_EDGAR_LOCAL_DATA_DIR  — path to EDGAR bulk data (default: /edgar-data/full)
  GMR_PRICE_DATA_DIR        — path to EOD price CSVs (default: /edgar-data/prices)

In unit tests, override via app.dependency_overrides[get_data_source].
"""
from __future__ import annotations

import os
from functools import lru_cache

from src.analysis.gmr_data_source import FinancialDataSource
from src.data.live_data_source import LiveDataSource


@lru_cache(maxsize=1)
def _live_source() -> LiveDataSource:
    local_data_dir = os.environ.get("GMR_EDGAR_LOCAL_DATA_DIR", "/edgar-data/full")
    local_price_data_dir = os.environ.get("GMR_PRICE_DATA_DIR", "/edgar-data/prices")
    return LiveDataSource(
        local_data_dir=local_data_dir,
        local_price_data_dir=local_price_data_dir,
    )


def get_data_source() -> FinancialDataSource:
    """
    Dependency injected into every endpoint.
    Override in tests::

        from src.api.dependencies import get_data_source
        app.dependency_overrides[get_data_source] = lambda: MyMock()
    """
    return _live_source()
