"""Contract Data Source — Abstract interface for procurement data queries."""
from __future__ import annotations

from abc import ABC, abstractmethod


class ContractDataSource(ABC):
    """Interface for contract/procurement data queries.

    Separate from FinancialDataSource — contracts are a different concern.
    Inject a concrete implementation (GraphContractSource or a test mock).
    """

    @abstractmethod
    def get_company_contracts(
        self, gmr_id: str, years: int = 5, limit: int = 50,
        lang: str | None = None,
    ) -> dict:
        """Return contracts awarded to a company. `lang` picks the
        translated Authority name (`name_<lang>`) with a fallback to the
        original `name` when the translation is missing."""

    @abstractmethod
    def get_authority_contracts(
        self, authority_id: str, years: int = 5, limit: int = 50,
        lang: str | None = None,
    ) -> dict:
        """Return contracts issued by an authority. `lang` → translated
        name coalesced with the stored original."""

    @abstractmethod
    def get_contract_detail(
        self, notice_id: str, lang: str | None = None,
    ) -> dict | None:
        """Return full detail for a single contract. `lang` → translated
        Authority name coalesced with the stored original."""

    @abstractmethod
    def get_sector_summary(
        self, country: str | None = None, year: int | None = None,
    ) -> list[dict]:
        """Return aggregated contract values by CPV division."""
