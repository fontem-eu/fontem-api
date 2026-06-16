"""Per-contract value confidence scoring.

Motivation
----------
The 2026-06-14 TED value investigation found that a meaningful share of
contract values in the graph are wrong, via two distinct mechanisms:

1. Our parser read the wrong field. eForms notices carry three money
   signals: the lot/notice estimate (EstimatedOverallContractAmount), the
   notice-level awarded total (efac:NoticeResult/cbc:TotalAmount), and the
   per-award payable (cac:LegalMonetaryTotal/cbc:PayableAmount). For the
   Romanian VAMTAC vehicles the raw XML shows TotalAmount = 9989377.06
   (correct, ~EUR 9.99M) but PayableAmount = 9989377060 (the same digits
   x1000). We stored the payable, so the graph showed EUR 9.99B. Same
   shape on the Portuguese Forca Aerea training-aircraft notice (real EUR
   7.27M, stored EUR 7.27B). Preferring TotalAmount recovers the value.

2. The source itself is wrong. For the Greek municipality of Drama both
   award fields are x1,000,000 inflated (estimate EUR 1.08M, total EUR
   1.07T). For the Slovak hospital both the estimate (EUR 35B) and the
   total (EUR 29.7B) are ~x100 inflated and agree with each other, so
   internal-consistency alone cannot catch it.

Design
------
A single confidence in [0, 1] is the product of two interpretable
factors:

* consistency -- how well the value we chose to store (the awarded
  TotalAmount, falling back to PayableAmount) agrees, in log10 space,
  with the independently-entered estimate. A gaussian on the absolute
  log-ratio: agreement -> 1.0, a x10 gap -> ~0.13, a x1000 gap -> ~0.
  When no estimate exists the value is unverifiable and consistency is a
  neutral prior. The estimate is the independent signal -- comparing the
  total to the payable is weak because both are "result" fields that
  share the same source corruption (the Drama case).

* plausibility -- a soft absolute bound in EUR. 1.0 up to EUR 1B, then a
  log-decay to ~0 by EUR ~300B. This is what catches the Slovak hospital
  (fields agree but jointly impossible) and any single-signal giant where
  there is no estimate to cross-check (the German schools notice, total
  EUR 33B, no estimate).

Below LOW_CONFIDENCE_THRESHOLD a contract should still be stored (we never
destroy data) but excluded from default aggregates and flagged, so an
impossible value never silently distorts a country total while the
underlying record stays available for review and correction.

This module is pure and side-effect free so it can be unit-tested
exhaustively against the real notices the investigation surfaced (see
tests/test_contract_confidence.py).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

# Tunables -------------------------------------------------------------

# Width of the gaussian on |log10(value / estimate)|. 0.7 means a value
# ~x5 from its estimate still scores ~0.37 (frameworks routinely award a
# few-fold under a ceiling), while a x100+ gap collapses to ~0.
_CONSISTENCY_SIGMA = 0.7

# When there is no cross-check signal at all, consistency is unknown. Use
# a neutral-but-cautious prior so a lone signal can still pass on
# plausibility but never reaches full confidence on its own.
_NO_ESTIMATE_CONSISTENCY = 0.6

# Cross-check weights. The estimate is entered independently of the award,
# so it is the trustworthy anchor. The payable lives next to the total in
# the result block and the two share the same source data-entry
# corruption (the Drama notice has BOTH total and payable inflated
# x1,000,000 while only the estimate is sane), so the payable is a weaker
# corroborator — it must never be able to out-vote the estimate.
_WEIGHT_ESTIMATE = 1.0
_WEIGHT_PAYABLE = 0.4

# When the payable is the only cross-check (no estimate published), cap
# its consistency: two agreeing result fields are weaker evidence than an
# independent estimate.
_PAYABLE_ONLY_CAP = 0.75

# Above this log10 gap the total and payable are treated as an internal
# discrepancy (a corrupted sibling field) worth flagging. log10(5) ~= 0.7
# => a x5 gap or more.
_DISCREPANCY_TOL = math.log10(5)

# Plausibility: full confidence up to this EUR value, then decay.
_PLAUSIBILITY_FULL_EUR = 1e9          # EUR 1B
# log10 span over which plausibility falls from 1.0 to 0.0 above the
# full-confidence ceiling. 2.5 => EUR 10B ~0.6, EUR 31.6B ~0.4,
# EUR 100B ~0.2, EUR 316B ~0.0.
_PLAUSIBILITY_DECADE_SPAN = 2.5

# A contract below this overall confidence is excluded from default
# queries and flagged.
LOW_CONFIDENCE_THRESHOLD = 0.5


class ValueFlag(str, Enum):
    """Why a contract value is suspect (or fine). Stored on the Contract
    so dashboards can filter and explain."""

    OK = "ok"
    NO_AWARDED_VALUE = "no_awarded_value"
    ZERO_VALUE = "zero_value"
    CONCESSION_NEGATIVE = "concession_negative"
    VALUE_DISAGREEMENT = "value_disagreement"
    IMPLAUSIBLE_MAGNITUDE = "implausible_magnitude"
    UNVERIFIED_SINGLE_SIGNAL = "unverified_single_signal"


@dataclass(frozen=True)
class ConfidenceResult:  # pylint: disable=too-many-instance-attributes
    """Outcome of scoring one contract's monetary fields."""

    chosen_value: float | None
    chosen_field: str | None
    confidence: float
    consistency: float
    plausibility: float
    flag: ValueFlag
    is_low_confidence: bool
    has_payable_discrepancy: bool
    reason: str

    def as_payload(self) -> dict:
        """Fields to merge into the Contract event payload."""
        return {
            "value_confidence": round(self.confidence, 4),
            "value_confidence_consistency": round(self.consistency, 4),
            "value_confidence_plausibility": round(self.plausibility, 4),
            "value_quality_flag": self.flag.value,
            "value_low_confidence": self.is_low_confidence,
            "value_payable_discrepancy": self.has_payable_discrepancy,
        }


def _agreement(a_eur: float, b_eur: float) -> float:
    """Gaussian on the absolute log10 ratio of two amounts: 1.0 when they
    match, ~0 once they differ by orders of magnitude."""
    d = abs(math.log10(a_eur) - math.log10(b_eur))
    return math.exp(-((d / _CONSISTENCY_SIGMA) ** 2))


def _consistency(
    chosen_eur: float,
    chosen_field: str,
    estimate_eur: float | None,
    payable_eur: float | None,
) -> tuple[float, bool, bool]:
    """Weighted agreement of the chosen value with the other money signals.

    The estimate is the independent, full-weight anchor; the payable (when
    it is not itself the chosen field) is a down-weighted corroborator, so
    a corrupted total+payable pair can never out-vote a sane estimate.

    Returns ``(score, had_estimate, payable_discrepancy)``.
    """
    had_estimate = estimate_eur is not None and estimate_eur > 0

    payable_is_crosscheck = (
        payable_eur is not None and payable_eur > 0 and chosen_field != "payable"
    )
    payable_agreement = 0.0
    payable_discrepancy = False
    if payable_is_crosscheck:
        gap = abs(math.log10(chosen_eur) - math.log10(payable_eur))
        payable_agreement = _agreement(chosen_eur, payable_eur)
        payable_discrepancy = gap > _DISCREPANCY_TOL

    if had_estimate:
        # The estimate is the independent anchor; the payable is a
        # down-weighted corroborator. A corrupted total+payable pair can
        # never out-vote a sane estimate.
        checks = [(_agreement(chosen_eur, estimate_eur), _WEIGHT_ESTIMATE)]
        if payable_is_crosscheck:
            checks.append((payable_agreement, _WEIGHT_PAYABLE))
        score = sum(a * w for a, w in checks) / sum(w for _, w in checks)
        return score, True, payable_discrepancy

    # No independent estimate. The payable shares the result-block source
    # corruption (the Drama / x1000 notices), so it is *asymmetric* evidence:
    # it may corroborate a value (raising consistency toward the payable-only
    # cap) but a disagreement must NOT be read as proof of error. Treating a
    # corrupt sibling field as decisive wrongly flagged ~750 contracts whose
    # stored total was perfectly sane (e.g. a EUR 274k air-transport award
    # sitting next to a PayableAmount of EUR 490). Fall back to the neutral
    # "unverifiable" prior and record the discrepancy for review instead.
    score = _NO_ESTIMATE_CONSISTENCY
    if payable_is_crosscheck and not payable_discrepancy:
        score = min(max(score, payable_agreement), _PAYABLE_ONLY_CAP)
    return score, False, payable_discrepancy


def _positive(value: float | None) -> float | None:
    """Return the value if it is a usable positive amount, else None."""
    return value if (value is not None and value > 0) else None


def _plausibility(chosen_eur: float) -> float:
    """Soft absolute bound. 1.0 up to the full-confidence ceiling, then a
    linear-in-log decay to 0."""
    if chosen_eur <= _PLAUSIBILITY_FULL_EUR:
        return 1.0
    over = math.log10(chosen_eur) - math.log10(_PLAUSIBILITY_FULL_EUR)
    return max(0.0, 1.0 - over / _PLAUSIBILITY_DECADE_SPAN)


def score_contract_value(  # pylint: disable=too-many-locals
    *,
    estimate_eur: float | None,
    total_eur: float | None,
    payable_eur: float | None,
    total_original: float | None = None,
    payable_original: float | None = None,
) -> ConfidenceResult:
    """Score a contract's value confidence and pick the value to store.

    All *_eur args are the EUR-converted signals (used for the absolute
    plausibility bound and for value selection). The *_original args are
    the same signals in the notice's original currency; when supplied the
    chosen original-currency value is returned alongside, so the loader
    can persist both. Consistency is currency-invariant (a log-ratio), so
    it is computed from the EUR figures.

    Value selection follows the investigation's conclusion: prefer the
    awarded TotalAmount (the clean field), fall back to PayableAmount only
    when no total is published. The estimate is never used as the stored
    value -- it is the cross-check.
    """
    est = _positive(estimate_eur)
    tot = _positive(total_eur)
    pay = _positive(payable_eur)

    # Negative awarded value: plausible only as a concession (the
    # contractor pays the authority). Keep it, flag it, do not score it
    # on the positive-value path.
    neg_award = None
    if total_eur is not None and total_eur < 0:
        neg_award = ("total", total_eur, total_original)
    elif payable_eur is not None and payable_eur < 0:
        neg_award = ("payable", payable_eur, payable_original)
    if neg_award is not None:
        field, val_eur, val_orig = neg_award
        plaus = _plausibility(abs(val_eur))
        conf = min(0.5, plaus * _NO_ESTIMATE_CONSISTENCY)
        return ConfidenceResult(
            chosen_value=val_orig if val_orig is not None else val_eur,
            chosen_field=field,
            confidence=conf,
            consistency=_NO_ESTIMATE_CONSISTENCY,
            plausibility=plaus,
            flag=ValueFlag.CONCESSION_NEGATIVE,
            is_low_confidence=True,
            has_payable_discrepancy=False,
            reason=(
                f"negative awarded value (~EUR {val_eur:,.0f}); plausible "
                "concession where the contractor pays the authority -- kept "
                "and flagged, excluded from value aggregates"
            ),
        )

    # Pick the value to store: prefer total, else payable.
    if tot is not None:
        chosen_eur, chosen_field = tot, "total"
        chosen_orig = total_original
    elif pay is not None:
        chosen_eur, chosen_field = pay, "payable"
        chosen_orig = payable_original
    else:
        zero_award = (total_eur == 0) or (payable_eur == 0)
        return ConfidenceResult(
            chosen_value=0.0 if zero_award else None,
            chosen_field="total" if total_eur == 0 else ("payable" if payable_eur == 0 else None),
            confidence=0.0,
            consistency=0.0,
            plausibility=0.0,
            flag=ValueFlag.ZERO_VALUE if zero_award else ValueFlag.NO_AWARDED_VALUE,
            is_low_confidence=True,
            has_payable_discrepancy=False,
            reason=(
                "awarded value disclosed as exactly 0"
                if zero_award else
                "no awarded value published"
            ),
        )

    consistency, had_estimate, payable_discrepancy = _consistency(
        chosen_eur, chosen_field, est, pay,
    )
    plausibility = _plausibility(chosen_eur)
    confidence = consistency * plausibility
    is_low = confidence < LOW_CONFIDENCE_THRESHOLD

    sub = f"(consistency {consistency:.2f}, plausibility {plausibility:.2f})"
    if not is_low:
        flag = ValueFlag.OK
        reason = (
            f"value EUR {chosen_eur:,.0f} from {chosen_field} corroborated "
            f"by estimate {sub}"
            if had_estimate else
            f"value EUR {chosen_eur:,.0f} from {chosen_field} within "
            f"plausible range; no estimate to cross-check {sub}"
        )
    elif had_estimate and consistency < plausibility:
        flag = ValueFlag.VALUE_DISAGREEMENT
        ratio = chosen_eur / est if est else float("inf")
        reason = (
            f"awarded value EUR {chosen_eur:,.0f} disagrees with estimate "
            f"EUR {est:,.0f} by ~{ratio:.0f}x -- likely a source data-entry "
            "error or wrong field; stored but flagged and excluded from "
            "aggregates"
        )
    elif not had_estimate:
        flag = (
            ValueFlag.UNVERIFIED_SINGLE_SIGNAL
            if plausibility >= LOW_CONFIDENCE_THRESHOLD
            else ValueFlag.IMPLAUSIBLE_MAGNITUDE
        )
        reason = (
            f"value EUR {chosen_eur:,.0f} from {chosen_field} with no "
            f"estimate to cross-check; magnitude implausible "
            f"(plausibility {plausibility:.2f}) -- flagged"
        )
    else:
        flag = ValueFlag.IMPLAUSIBLE_MAGNITUDE
        reason = (
            f"value EUR {chosen_eur:,.0f} too large to be a single real "
            "contract even though it agrees with its estimate (both likely "
            f"inflated) -- flagged (plausibility {plausibility:.2f})"
        )

    if payable_discrepancy:
        reason += (
            f"; note: payable EUR {pay:,.0f} disagrees with the stored "
            f"{chosen_field} -- internal source inconsistency"
        )

    return ConfidenceResult(
        chosen_value=chosen_orig if chosen_orig is not None else chosen_eur,
        chosen_field=chosen_field,
        confidence=confidence,
        consistency=consistency,
        plausibility=plausibility,
        flag=flag,
        is_low_confidence=is_low,
        has_payable_discrepancy=payable_discrepancy,
        reason=reason,
    )
