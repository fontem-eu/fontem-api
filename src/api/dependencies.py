"""
FastAPI dependency that provides the application's FinancialDataSource.

Set GMR_DATA_SOURCE=graph to use the Neo4j-backed GraphDataSource.
Any other value (or unset) uses the legacy RoutingDataSource.

In unit tests, override via app.dependency_overrides[get_data_source].
"""
from __future__ import annotations

import os
from functools import lru_cache

from src.analysis.gmr_data_source import FinancialDataSource
from src.data.europe.esef_data_source import EsefDataSource
from src.data.north_america.live_data_source import LiveDataSource
from src.data.routing_data_source import RoutingDataSource


@lru_cache(maxsize=1)
def _routing_source() -> RoutingDataSource:
    local_data_dir = os.environ.get("GMR_EDGAR_LOCAL_DATA_DIR", "/edgar-data/full")
    local_price_data_dir = os.environ.get("GMR_PRICE_DATA_DIR", "/edgar-data/prices")
    esef_data_dir = os.environ.get("GMR_ESEF_DATA_DIR", "/esef-data/esef")
    return RoutingDataSource(
        na_source=LiveDataSource(
            local_data_dir=local_data_dir,
            local_price_data_dir=local_price_data_dir,
        ),
        eu_source=EsefDataSource(esef_data_dir=esef_data_dir),
    )


@lru_cache(maxsize=1)
def _graph_source() -> FinancialDataSource:
    from src.data.graph.graph_data_source import GraphDataSource  # pylint: disable=import-outside-toplevel
    from src.data.graph.neo4j_client import Neo4jClient  # pylint: disable=import-outside-toplevel
    return GraphDataSource(
        neo4j_client=Neo4jClient(),
        price_data_dir=os.environ.get("GMR_PRICE_DATA_DIR", "/edgar-data/prices"),
        edgar_data_dir=os.environ.get("GMR_EDGAR_LOCAL_DATA_DIR", "/edgar-data/full"),
    )


def get_data_source() -> FinancialDataSource:
    """
    Dependency injected into every endpoint.
    Override in tests::

        from src.api.dependencies import get_data_source
        app.dependency_overrides[get_data_source] = lambda: MyMock()
    """
    if os.environ.get("GMR_DATA_SOURCE") == "graph":
        return _graph_source()
    return _routing_source()
