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
    ) -> dict:
        """Return contracts awarded to a company."""

    @abstractmethod
    def get_authority_contracts(
        self, authority_id: str, years: int = 5, limit: int = 50,
    ) -> dict:
        """Return contracts issued by an authority."""

    @abstractmethod
    def get_contract_detail(self, notice_id: str) -> dict | None:
        """Return full detail for a single contract."""

    @abstractmethod
    def get_sector_summary(
        self, country: str | None = None, year: int | None = None,
    ) -> list[dict]:
        """Return aggregated contract values by CPV division."""
