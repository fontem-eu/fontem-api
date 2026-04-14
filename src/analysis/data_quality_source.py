"""Data Quality Source — Abstract interface for platform health metrics."""
from __future__ import annotations

from abc import ABC, abstractmethod


class DataQualitySource(ABC):
    """Interface for querying platform data quality and health metrics.

    Provides the metrics displayed on the data quality dashboard.
    Inject a concrete implementation (GraphDataQualitySource or a test mock).
    """

    @abstractmethod
    def get_graph_stats(self) -> dict:
        """Return node/relationship counts by label."""

    @abstractmethod
    def get_matching_stats(self) -> dict:
        """Return entity resolution metrics (SAME_AS queue, duplicates)."""

    @abstractmethod
    def get_data_freshness(self) -> dict:
        """Return freshness info (latest contract date, GLEIF load date, etc.)."""

    @abstractmethod
    def get_coverage_stats(self) -> dict:
        """Return coverage metrics (companies with contracts, by country, etc.)."""

    # Per-pipeline methods — default implementations return empty dicts
    # so existing mocks/tests don't break.
    def get_contracts_timeline(self) -> list[dict]: return []
    def get_contracts_by_country(self) -> list[dict]: return []
    def get_contracts_nulls(self) -> dict: return {"total": 0, "missing": {}}
    def get_contracts_currency_quality(self) -> dict: return {}
    def get_contracts_value_timeline(self) -> list[dict]: return []
    def get_gleif_stats(self) -> dict: return {}
    def get_edgar_stats(self) -> dict: return {}
    def get_esef_stats(self) -> dict: return {}
    def get_lobbying_stats(self) -> dict: return {}
    def get_directors_stats(self) -> dict: return {}
    def get_trade_edges_stats(self) -> dict: return {}
    def get_dedup_stats(self) -> dict: return {}
    def get_sanctions_stats(self) -> dict: return {}
    def get_firds_stats(self) -> dict: return {}
    def get_openfigi_stats(self) -> dict: return {}
    def get_beneficial_ownership_stats(self) -> dict: return {}
    def get_cdp_stats(self) -> dict: return {}
