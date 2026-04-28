"""
TED Company / Authority Matcher
================================
Maps an eForms Organization to an existing or new gmr_id, calling
gmr-consolidator's /resolve endpoint for the deterministic tiers.
Layer 5 (create new node) stays local because /resolve is read-only
by design — it never invents new gmr_ids.

Layers:
  1. VAT direct match (in-process cache, hot path)
  2. /resolve LEI/VAT/CIK or name+country tiers
  3. /resolve fuzzy candidates (single high-confidence top candidate
     accepted; otherwise treated as no-match to avoid the false
     positives the old Dice-only matcher allowed)
  4. Layer 5: deterministic new gmr_id from VAT or normalised name
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import gmr_id
from ._hooks import resolve_entity

logger = logging.getLogger(__name__)


# Confidence floor for accepting a /resolve fuzzy candidate as the
# match. The resolver caps fuzzy confidence at ~0.94; a single
# candidate above this floor is what the old Dice-based Layer 4
# would have accepted. Below this we fall through to a new node.
FUZZY_ACCEPT_CONF = 0.85


@dataclass
class MatchResult:
    """Result of matching an organization to a Company node."""

    gmr_id: str
    layer: int
    confidence: float
    created_new: bool = False
    resolver_tier: str | None = None


@dataclass
class MatcherStats:
    """Counters for match quality reporting."""

    layer_counts: dict[int, int] = field(
        default_factory=lambda: {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    )
    total: int = 0
    resolver_failures: int = 0

    def record(self, layer: int):
        """Record a match at the given layer."""
        self.layer_counts[layer] = self.layer_counts.get(layer, 0) + 1
        self.total += 1

    def summary(self) -> dict:
        """Return a summary dict for the data quality dashboard."""
        return {
            "total": self.total,
            "by_layer": dict(self.layer_counts),
            "resolver_failures": self.resolver_failures,
        }


class TedMatcher:
    """Matches eForms Organization objects to Company gmr_ids.

    Parameters
    ----------
    session:
        An active Neo4j session for warming the VAT cache. (Lookups
        themselves go through /resolve.)
    """

    def __init__(self, session) -> None:
        self._session = session
        self._vat_cache: dict[str, str] = {}
        self.stats = MatcherStats()

        # Warm the VAT cache from existing Company nodes
        result = session.run(
            "MATCH (c:Company) WHERE c.vat IS NOT NULL "
            "RETURN c.vat AS vat, c.gmr_id AS gid"
        )
        for record in result:
            vat_val = record["vat"]
            gid = record["gid"]
            if isinstance(vat_val, list):
                for v in vat_val:
                    if v:
                        self._vat_cache[v] = gid
            elif vat_val:
                self._vat_cache[vat_val] = gid
        logger.info(
            "TedMatcher: warmed VAT cache with %d entries",
            len(self._vat_cache),
        )

    def match_company(
        self, name: str, country: str, vat: str | list | None = None,
    ) -> MatchResult:
        """Resolve an organization to a gmr_id."""
        # Normalize VAT: eforms may return a list in older formats
        if isinstance(vat, list):
            vat = vat[0] if vat else None

        # Layer 1: VAT direct match (cached). Skips an HTTP round-trip
        # for repeated awardees within a single ingestion run.
        if vat and vat in self._vat_cache:
            self.stats.record(1)
            return MatchResult(
                gmr_id=self._vat_cache[vat], layer=1, confidence=1.0,
            )

        # Layer 2 + 3: ask /resolve. The resolver handles VAT lookup
        # (Tier 2), name+country (Tier 3), and fuzzy candidates (Tier 4)
        # with the same guards used everywhere else (MIN_NAME_LEN=6,
        # country normalisation, score floor 4.0).
        result = resolve_entity(
            entity_type="Company", name=name, country=country, vat=vat,
        )
        if result is None:
            self.stats.resolver_failures += 1
        elif result.hint == "matched" and result.match is not None:
            tier = result.match.tier
            layer = 2 if tier in ("lei", "vat", "cik", "name_country") else 3
            self.stats.record(layer)
            if vat:
                self._vat_cache[vat] = result.match.gmr_id
            return MatchResult(
                gmr_id=result.match.gmr_id,
                layer=layer,
                confidence=result.match.confidence,
                resolver_tier=tier,
            )
        elif result.hint == "ambiguous" and result.candidates:
            # Single high-confidence top candidate matches the old
            # Dice>0.85 behaviour without the unguarded Layer 4 issues.
            top = result.candidates[0]
            second = result.candidates[1] if len(result.candidates) > 1 else None
            if (
                top.confidence >= FUZZY_ACCEPT_CONF
                and (second is None or second.confidence < FUZZY_ACCEPT_CONF)
            ):
                self.stats.record(3)
                if vat:
                    self._vat_cache[vat] = top.gmr_id
                return MatchResult(
                    gmr_id=top.gmr_id, layer=3, confidence=top.confidence,
                    resolver_tier="fuzzy",
                )

        # Layer 5: create new node with deterministic UUID
        if vat:
            gid = gmr_id.from_vat(country or "XX", vat)
            self._vat_cache[vat] = gid
        else:
            gid = gmr_id.from_name(country or "XX", name or "UNKNOWN")
        self.stats.record(5)
        return MatchResult(
            gmr_id=gid, layer=5, confidence=0.0, created_new=True,
        )

    def match_authority(  # pylint: disable=unused-argument
        self, name: str, country: str, _vat: str | None = None,
    ) -> str:
        """Resolve an authority to an authority_id.

        Tries /resolve for the deterministic tiers; falls back to a
        deterministic UUID derived from (country, name) for new
        authorities.
        """
        result = resolve_entity(
            entity_type="Authority", name=name, country=country,
        )
        if result is not None and result.hint == "matched" and result.match is not None:
            return result.match.gmr_id

        import uuid  # pylint: disable=import-outside-toplevel
        namespace = gmr_id.GMR_NAMESPACE
        canonical = f"ted_auth:{(country or 'XX').upper()}:{(name or '').strip().upper()}"
        return str(uuid.uuid5(namespace, canonical))
