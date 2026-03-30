"""Deterministic company ID generation using UUID5."""
import uuid

GMR_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def from_lei(lei: str) -> str:
    """Generate a gmr_id from a LEI."""
    return str(uuid.uuid5(GMR_NAMESPACE, f"lei:{lei}"))


def from_cik(cik: str) -> str:
    """Generate a gmr_id from a zero-padded CIK."""
    return str(uuid.uuid5(GMR_NAMESPACE, f"edgar:{cik.zfill(10)}"))


def from_national_id(country: str, national_id: str) -> str:
    """Generate a gmr_id from a national registration number."""
    return str(uuid.uuid5(GMR_NAMESPACE, f"{country.upper()}:{national_id}"))


def from_name(country: str, name: str) -> str:
    """Generate a gmr_id from a normalised legal name (last resort)."""
    return str(uuid.uuid5(GMR_NAMESPACE, f"{country.upper()}:{name.strip().upper()}"))


def from_vat(country: str, vat: str) -> str:
    """Generate a gmr_id from a country + VAT number."""
    return str(uuid.uuid5(GMR_NAMESPACE, f"{country.upper()}:{vat.strip()}"))
