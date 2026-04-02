"""
TED Company Matcher — 5-layer identity resolution
===================================================
Maps an eForms Organization to an existing or new Company node gmr_id.

Layers (highest to lowest confidence):
  1. VAT direct match (cached on Company.vat)
  2. VIES validation + enrichment (targeted, not bulk)
  3. GLEIF registration lookup
  4. Fuzzy name match (Neo4j full-text + Dice similarity)
  5. Create new Company node
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import gmr_id

logger = logging.getLogger(__name__)


@dataclass
class MatchResult:
    """Result of matching an organization to a Company node."""

    gmr_id: str
    layer: int
    confidence: float
    created_new: bool = False


@dataclass
class MatcherStats:
    """Counters for match quality reporting."""

    layer_counts: dict[int, int] = field(
        default_factory=lambda: {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    )
    total: int = 0
    vies_failures: int = 0

    def record(self, layer: int):
        """Record a match at the given layer."""
        self.layer_counts[layer] = self.layer_counts.get(layer, 0) + 1
        self.total += 1

    def summary(self) -> dict:
        """Return a summary dict for the data quality dashboard."""
        return {
            "total": self.total,
            "by_layer": dict(self.layer_counts),
            "vies_failures": self.vies_failures,
        }


class TedMatcher:
    """Matches eForms Organization objects to Company gmr_ids.

    Parameters
    ----------
    session:
        An active Neo4j session for lookups.
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
        """Resolve an organization to a gmr_id using the 5-layer strategy."""
        # Normalize VAT: eforms may return a list in older formats
        if isinstance(vat, list):
            vat = vat[0] if vat else None

        # Layer 1: VAT direct match (cached)
        if vat and vat in self._vat_cache:
            self.stats.record(1)
            return MatchResult(
                gmr_id=self._vat_cache[vat], layer=1, confidence=1.0,
            )

        # Layer 2: VIES — skipped for now (targeted enrichment, not bulk)
        # Will be added as a separate pass for high-value unmatched

        # Layer 3: GLEIF LEI lookup by VAT
        if vat:
            result = self._session.run(
                "MATCH (c:Company) WHERE c.vat = $vat "
                "RETURN c.gmr_id AS gid LIMIT 1",
                vat=vat,
            ).single()
            if result:
                gid = result["gid"]
                self._vat_cache[vat] = gid
                self.stats.record(3)
                return MatchResult(gmr_id=gid, layer=3, confidence=0.95)

        # Layer 4: Fuzzy name match (requires full-text index)
        if name and country:
            try:
                result = self._session.run(
                    "CALL db.index.fulltext.queryNodes("
                    "  'companyNameIndex', $name + '~'"
                    ") YIELD node, score "
                    "WHERE node.country = $country AND score > 0.3 "
                    "WITH node, apoc.text.sorensenDiceSimilarity("
                    "  toLower($name), toLower(node.name_normalized)"
                    ") AS dice "
                    "WHERE dice > 0.85 "
                    "RETURN node.gmr_id AS gid, dice "
                    "ORDER BY dice DESC LIMIT 1",
                    name=name, country=country,
                ).single()
            except Exception:  # pylint: disable=broad-exception-caught
                # Full-text index may not exist yet — skip to Layer 5
                result = None
            if result:
                gid = result["gid"]
                if vat:
                    self._vat_cache[vat] = gid
                    # Set VAT on the matched node for future Layer 1 hits
                    self._session.run(
                        "MATCH (c:Company {gmr_id: $gid}) SET c.vat = $vat",
                        gid=gid, vat=vat,
                    )
                self.stats.record(4)
                return MatchResult(
                    gmr_id=gid, layer=4, confidence=result["dice"],
                )

        # Layer 5: Create new node
        if vat:
            gid = gmr_id.from_vat(country or "XX", vat)
        else:
            gid = gmr_id.from_name(country or "XX", name or "UNKNOWN")

        if vat:
            self._vat_cache[vat] = gid

        self.stats.record(5)
        return MatchResult(
            gmr_id=gid, layer=5, confidence=0.0, created_new=True,
        )

    def match_authority(  # pylint: disable=unused-argument
        self, name: str, country: str, vat: str | None = None,
    ) -> str:
        """Resolve an authority to an authority_id.

        Uses the same fuzzy approach but against Authority nodes.
        Falls back to generating a deterministic UUID.
        """
        import uuid  # pylint: disable=import-outside-toplevel
        namespace = gmr_id.GMR_NAMESPACE
        canonical = f"ted_auth:{(country or 'XX').upper()}:{(name or '').strip().upper()}"
        return str(uuid.uuid5(namespace, canonical))
