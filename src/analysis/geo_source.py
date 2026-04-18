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

    @abstractmethod
    def aggregate_entity_by_nuts(
        self,
        entity_id: str,
        level: int,
        metric: str,
        scope_nuts: str | None = None,
    ) -> list[dict]:
        """Aggregate one entity's contract volume by NUTS region.

        For a Company: groups contracts by the NUTS region of the awarding
        Authority.  For an Authority: groups contracts by the NUTS region of
        the receiving Company.  Resolution: the node's LOCATED_IN edge is
        followed to a leaf NUTSRegion which is then traversed upward via
        PART_OF* to reach the requested level.

        Args:
            entity_id: ``gmr_id`` (Company UUID) or ``authority_id`` (TED id).
            level: 0–3. Level > 0 without ``scope_nuts`` returns results but
                may be empty if counterparties are only linked at NUTS 0.
            metric: ``contracts`` (count) or ``contracts_eur`` (EUR sum).
            scope_nuts: optional ancestor NUTS code — restrict result to
                regions whose code starts with this prefix (e.g. ``"DE"``
                for all German NUTS regions at any level).

        Returns a list of dicts: ``{nuts_code, label, level, value}``.
        """
