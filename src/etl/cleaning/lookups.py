"""Injected data the rules may consult. Rules do no I/O; whoever calls
the stage loads these first (the TED loader reads peer stats from the
events store, unit tests hand in literals).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Protocol

# ISO 3166-1 alpha-3 -> the prefix VIES expects in a VAT number, for the
# EU/EEA, the UK and Switzerland. Greece is "EL" in VAT numbers, not
# "GR" (both are accepted by canon_vat; EL is what buyers publish).
EU_EEA_ISO3_TO_VAT_PREFIX: Mapping[str, str] = MappingProxyType({
    "AUT": "AT", "BEL": "BE", "BGR": "BG", "HRV": "HR", "CYP": "CY",
    "CZE": "CZ", "DNK": "DK", "EST": "EE", "FIN": "FI", "FRA": "FR",
    "DEU": "DE", "GRC": "EL", "HUN": "HU", "IRL": "IE", "ITA": "IT",
    "LVA": "LV", "LTU": "LT", "LUX": "LU", "MLT": "MT", "NLD": "NL",
    "POL": "PL", "PRT": "PT", "ROU": "RO", "SVK": "SK", "SVN": "SI",
    "ESP": "ES", "SWE": "SE",
    "ISL": "IS", "LIE": "LI", "NOR": "NO",
    "GBR": "GB", "CHE": "CH",
})

# Alpha-2 spellings that need translating before use as a VAT prefix.
_ALPHA2_ALIASES: Mapping[str, str] = {"GR": "EL", "UK": "GB"}


def vat_prefix_for(country: str | None,
                   iso3_map: Mapping[str, str] = EU_EEA_ISO3_TO_VAT_PREFIX,
                   ) -> str | None:
    """The VAT prefix for a country given as alpha-3 or alpha-2, or None
    when the country is unknown or outside the map."""
    if not country:
        return None
    code = country.strip().upper()
    if len(code) == 3:
        return iso3_map.get(code)
    if len(code) == 2:
        code = _ALPHA2_ALIASES.get(code, code)
        return code if code in set(iso3_map.values()) else None
    return None


@dataclass(frozen=True)
class PeerBand:
    """Value percentiles of one (country, cpv4) peer group."""

    n: int
    p10: float
    median: float
    p90: float
    p99: float


class PeerStats(Protocol):
    """Where a rule looks up the peer band of a contract."""

    def band(self, country: str, cpv4: str) -> PeerBand | None:
        """The band for the group, or None when unknown."""


class InMemoryPeerStats:
    """A dict-backed PeerStats: what the loader builds from the events
    store table, and what tests hand in directly."""

    def __init__(self, bands: Mapping[tuple[str, str], PeerBand] | None = None):
        self._bands = dict(bands or {})

    def __len__(self) -> int:
        return len(self._bands)

    def band(self, country: str, cpv4: str) -> PeerBand | None:
        return self._bands.get((country, cpv4))


@dataclass(frozen=True)
class Lookups:
    """Everything injected into a stage run."""

    peer_stats: PeerStats | None = None
    iso3_to_vat_prefix: Mapping[str, str] = field(
        default_factory=lambda: EU_EEA_ISO3_TO_VAT_PREFIX)
