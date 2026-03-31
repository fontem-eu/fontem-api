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
