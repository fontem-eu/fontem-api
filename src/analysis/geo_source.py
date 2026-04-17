"""Geo Source — Abstract interface for geographic aggregation."""
from __future__ import annotations

from abc import ABC, abstractmethod


class GeoSource(ABC):  # pylint: disable=too-few-public-methods
    """Interface for aggregating entities and contracts by NUTS region.

    Inject a concrete implementation (GraphGeoSource or a test mock).
    """

    @abstractmethod
    def aggregate_by_nuts(
        self,
        level: int,
        metric: str,
        scope_nuts: str | None = None,
        connected_to_country: str | None = None,
    ) -> list[dict]:
        """Aggregate a metric across NUTS regions at a given level.

        Args:
            level: 0–3. Level 3 requires ``scope_nuts`` (a parent NUTS 1 code)
                to cap query size.
            metric: ``companies``, ``contracts``, or ``contracts_eur``.
            scope_nuts: optional ancestor NUTS code — filter to descendants.
            connected_to_country: optional alpha-3 — only entities with a
                graph path to at least one entity of that country.

        Returns a list of dicts: ``{nuts_code, label, level, value}``.
        """
