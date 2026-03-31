"""Person Data Source — Abstract interface for company officer/director data."""
from __future__ import annotations

from abc import ABC, abstractmethod


class PersonDataSource(ABC):
    """Interface for querying person (director/officer) data.

    Same dependency injection pattern as FinancialDataSource and
    ContractDataSource — mockable for tests.
    """

    @abstractmethod
    def get_company_directors(
        self, gmr_id: str, include_former: bool = False,
    ) -> list[dict]:
        """Return directors/officers for a company."""

    @abstractmethod
    def get_person_roles(self, person_id: str) -> list[dict]:
        """Return all company roles held by a person."""

    @abstractmethod
    def search_persons(
        self, name: str, limit: int = 10,
    ) -> list[dict]:
        """Search persons by name."""
