"""
Deterministic company ID generation using UUID5.

Every company gets a stable ``gmr_id`` derived from its best available
external identifier.  The priority order is documented in plan.md:

1. ``lei:{LEI}``              — GLEIF Legal Entity Identifier
2. ``edgar:{zero-padded-CIK}`` — SEC EDGAR Central Index Key
3. ``{ISO2}:{national_id}``   — national registration number
4. ``{ISO2}:{normalized_name}`` — last resort, name-based
"""
from __future__ import annotations

import uuid

GMR_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def from_lei(lei: str) -> str:
    """Generate a gmr_id from a GLEIF LEI."""
    return str(uuid.uuid5(GMR_NAMESPACE, f"lei:{lei}"))


def from_cik(cik: str | int) -> str:
    """Generate a gmr_id from a zero-padded SEC CIK."""
    padded = str(cik).zfill(10)
    return str(uuid.uuid5(GMR_NAMESPACE, f"edgar:{padded}"))


def from_national(country_iso2: str, national_id: str) -> str:
    """Generate a gmr_id from a national registration number."""
    return str(uuid.uuid5(GMR_NAMESPACE, f"{country_iso2}:{national_id}"))


def from_name(country_iso2: str, name: str) -> str:
    """Generate a gmr_id from a normalized legal name (last resort)."""
    normalized = name.strip().upper()
    return str(uuid.uuid5(GMR_NAMESPACE, f"{country_iso2}:{normalized}"))
