"""Entity Resolution — Abstract interface for similarity search and identity lookup."""
from __future__ import annotations

from abc import ABC, abstractmethod


class EntityResolver(ABC):
    """Shared abstraction for entity identity resolution.

    Used by both ETL (matching during ingestion) and API
    (operator review, similarity search endpoints).
    """

    @abstractmethod
    def search_similar_companies(
        self, name: str, country: str, limit: int = 10,
    ) -> list[dict]:
        """Find companies similar to the given name + country."""

    @abstractmethod
    def search_similar_authorities(
        self, name: str, country: str, limit: int = 10,
    ) -> list[dict]:
        """Find authorities similar to the given name + country."""

    @abstractmethod
    def resolve_by_vat(self, country: str, vat: str) -> str | None:
        """Return gmr_id for a company with this VAT, or None."""

    @abstractmethod
    def resolve_by_lei(self, lei: str) -> str | None:
        """Return gmr_id for a company with this LEI, or None."""
