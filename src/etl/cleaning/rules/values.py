"""C4 — monetary scale.

Two kinds of rule:

* the milli-euro tiers, which already RESCALE with a marker
  (``scale_normalization``: tier A needs a sane sibling estimate in
  the same notice, tier B a PRT notice at >= EUR 1B). They are wrapped
  here unchanged so they are counted like every other rule and so the
  peer test sees the corrected value.
* the peer test, which only QUARANTINES. Nothing in the XML marks the
  scale (verified 2026-09-21: "no decimals" does not imply cents), so
  a leak that the tiers cannot prove is catchable only as
  implausibility against (country, cpv4) peers — and the correction
  (/100 or /1000) is then undecidable, so the value is withheld and
  the candidates go to the review queue.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Sequence

from src.etl.scale_normalization import normalize_scale

from ..facts import NoticeFacts, ValueFacts
from ..lookups import Lookups, PeerBand
from ..outcomes import Outcome, ValueQuarantined, ValueRescaled

# The peer band is read only when the group is big enough to mean
# something.
PEER_MIN_N = 30
# A value more than K x the peers' p90 is an outlier. The gateway
# watermark (2000-01-01 / "0.0") is a prior, so the bar drops.
PEER_K_DEFAULT = 50.0
PEER_K_WITH_PLACEHOLDER = 20.0
# The corrections a scale error could need; a candidate that lands
# inside the peers' [p10, p99] makes the case "ambiguous", not
# "impossible".
SCALE_CANDIDATE_DIVISORS = (100.0, 1000.0)

REASON_AMBIGUOUS_SCALE = "ambiguous_scale_x100_or_x1000"
REASON_IMPLAUSIBLE_VS_PEERS = "implausible_vs_peers"

_MILLI = 1000.0


def _corrected_facts(value: ValueFacts, scale) -> ValueFacts:
    return replace(
        value,
        estimate_eur=scale.estimate_eur, total_eur=scale.total_eur,
        payable_eur=scale.payable_eur, total_original=scale.total_original,
        payable_original=scale.payable_original,
    )


class _MilliEuroRule:
    """Wraps ``normalize_scale``; claims the outcome only for its tier."""

    id = ""
    tier = ""

    def applies(self, facts: NoticeFacts, lookups: Lookups) -> bool:  # pylint: disable=unused-argument
        return facts.value.chosen_eur is not None or facts.value.estimate_eur is not None

    def decide(self, facts: NoticeFacts, lookups: Lookups) -> Sequence[Outcome]:  # pylint: disable=unused-argument
        value = facts.value
        scale = normalize_scale(
            estimate_eur=value.estimate_eur, total_eur=value.total_eur,
            payable_eur=value.payable_eur, total_original=value.total_original,
            payable_original=value.payable_original, country=value.country,
        )
        if not scale.corrected or scale.tier != self.tier:
            return []
        return [ValueRescaled(
            rule_id=self.id, subject="value", factor=1 / _MILLI,
            tier=scale.tier, detail=scale.detail or "",
            corrected=_corrected_facts(value, scale),
        )]


class MilliEuroSiblingRatioRule(_MilliEuroRule):
    """``generic.value_scale_sibling_ratio``: tier A — the award total
    disagrees with its own estimate by ~x1000 and carries the cents
    fingerprint (or comes from a proven gateway)."""

    id = "generic.value_scale_sibling_ratio"
    tier = "ratio"


class MilliEuroCountryPriorRule(_MilliEuroRule):
    """``pt.value_scale_country_prior``: tier B — every field consistent
    but absurd (>= EUR 1B) on a PRT notice."""

    id = "pt.value_scale_country_prior"
    tier = "country_prior"


class ValuePeerOutlierRule:
    """``generic.value_peer_outlier``: quarantine a value far above its
    (country, cpv4) peers. Inactive without injected peer stats."""

    id = "generic.value_peer_outlier"

    def applies(self, facts: NoticeFacts, lookups: Lookups) -> bool:
        value = facts.value
        return (
            lookups.peer_stats is not None
            and value.chosen_eur is not None
            and bool(value.country) and value.cpv4 is not None
        )

    def decide(self, facts: NoticeFacts, lookups: Lookups) -> Sequence[Outcome]:
        value = facts.value
        assert lookups.peer_stats is not None  # applies() guards it
        band = lookups.peer_stats.band(value.country or "", value.cpv4 or "")
        if band is None or band.n < PEER_MIN_N:
            return []
        chosen = value.chosen_eur or 0.0
        k = PEER_K_WITH_PLACEHOLDER if facts.has_gateway_placeholder else PEER_K_DEFAULT
        if chosen <= k * band.p90:
            return []
        candidates = tuple(chosen / d for d in SCALE_CANDIDATE_DIVISORS)
        in_band = [c for c in candidates if band.p10 <= c <= band.p99]
        reason = REASON_AMBIGUOUS_SCALE if in_band else REASON_IMPLAUSIBLE_VS_PEERS
        return [ValueQuarantined(
            rule_id=self.id, subject="value", reason=reason,
            detail=_detail(chosen, k, band, value, candidates, in_band,
                           facts.has_gateway_placeholder),
            value_eur=chosen, candidates=candidates,
        )]


def _detail(chosen, k, band: PeerBand, value: ValueFacts, candidates,  # pylint: disable=too-many-arguments,too-many-positional-arguments
            in_band, placeholder) -> str:
    cand = ", ".join(
        f"/{d:g}={c:,.2f}{' (in band)' if c in in_band else ''}"
        for d, c in zip(SCALE_CANDIDATE_DIVISORS, candidates)
    )
    return (
        f"EUR {chosen:,.2f} is > {k:g} x p90 ({band.p90:,.0f}) of "
        f"{value.country}/{value.cpv4} peers (n={band.n}, p10 {band.p10:,.0f}, "
        f"median {band.median:,.0f}, p99 {band.p99:,.0f})"
        f"{'; gateway placeholder present' if placeholder else ''}; "
        f"candidates {cand}"
    )
