"""Dishka test helpers for the GMR ETL API.

Provides ``make_test_client`` to build a TestClient with mock data
sources injected via dishka's container, replacing the old
``app.dependency_overrides[get_X]`` pattern.
"""
from __future__ import annotations

from dishka import Provider, Scope, provide, make_async_container
from dishka.integrations.fastapi import setup_dishka
from fastapi.testclient import TestClient

from src.analysis.contract_data_source import ContractDataSource
from src.analysis.data_quality_source import DataQualitySource
from src.analysis.gmr_data_source import FinancialDataSource
from src.analysis.person_data_source import PersonDataSource
from src.api.app import app
from src.data.graph.neo4j_client import Neo4jClient


class _FakeNeo4jSession:
    """Stub session that returns empty results."""

    def run(self, query, **kwargs):
        return _FakeResult()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _FakeResult:
    def single(self):
        return None


class _FakeNeo4jClient:
    """Stub Neo4jClient for tests — returns empty results instead of crashing."""

    def session(self):
        return _FakeNeo4jSession()

    def close(self):
        pass


class FlexibleMockProvider(Provider):
    """Accepts any combination of mock sources via keyword args.

    Usage::

        FlexibleMockProvider(
            data_source=MyMockDataSource(),
            contract_source=MyMockContractSource(),
            neo4j_client=FakeNeo4jClient(),
        )
    """

    def __init__(self, **kwargs) -> None:
        super().__init__()
        self._mocks = {}
        for key, val in kwargs.items():
            self._mocks[key] = val() if isinstance(val, type) else val

    @provide(scope=Scope.APP)
    def neo4j_client(self) -> Neo4jClient:
        return self._mocks.get("neo4j_client", _FakeNeo4jClient())  # type: ignore[return-value]

    @provide(scope=Scope.APP)
    def financial_data_source(self) -> FinancialDataSource:
        return self._mocks.get("data_source")  # type: ignore[return-value]

    @provide(scope=Scope.APP)
    def contract_data_source(self) -> ContractDataSource:
        return self._mocks.get("contract_source")  # type: ignore[return-value]

    @provide(scope=Scope.APP)
    def data_quality_source(self) -> DataQualitySource:
        return self._mocks.get("data_quality_source")  # type: ignore[return-value]

    @provide(scope=Scope.APP)
    def person_data_source(self) -> PersonDataSource:
        return self._mocks.get("person_source")  # type: ignore[return-value]


def make_test_client(data_source=None, **kwargs) -> TestClient:
    """Build a TestClient with mock sources via dishka.

    Usage::

        # Simple — just a FinancialDataSource mock:
        client = make_test_client(MockDataSource)

        # Multiple sources:
        client = make_test_client(
            contract_source=mock_contract,
            neo4j_client=FakeNeo4j(),
        )
    """
    if data_source is not None:
        kwargs.setdefault("data_source", data_source)
    app.middleware_stack = None
    container = make_async_container(FlexibleMockProvider(**kwargs))
    setup_dishka(container, app)
    return TestClient(app)


def cleanup_dishka():
    """Reset app state after test — call in fixture teardown."""
    app.middleware_stack = None
