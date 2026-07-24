"""Milli-euro scale-error normalization for TED monetary fields.

Root cause (proven 2026-07 investigation): a subset of Portuguese
notices submitted to TED via the eNotices2 gateway carry monetary
amounts serialized from a fixed-point THREE-implied-decimals integer
representation ("milli-euros") without the final divide-by-1000. The
Figueira da Foz school award published as EUR 9,281,922,790 is the
locally-documented EUR 9,281,922.79 contract (Veiga Lopes SA), exactly
x1000. Affected notices share machine-generated fingerprints
(AwardDate 2000-01-01, TenderReference "0.0") across unrelated buyers,
so this is one submitting software, not data entry.

Detection tiers (applied per notice, BEFORE confidence scoring):

  A. ratio evidence — the award total disagrees with its own sibling
     estimate by ~x1000 (either direction). The sane sibling proves the
     scale; correction is safe for any country.
  B. country prior — ALL monetary fields are internally consistent but
     absurdly large (>= EUR 1B) on a PRT notice. The whole notice went
     through the broken path (school case: estimate AND award both
     x1000). Corrected only when the /1000 value lands back inside a
     plausible band; flagged for review either way.

A corrected notice keeps a marker so dashboards can count and report
the affected notices upstream (OP / the national gateway operator).
"""
from __future__ import annotations

from dataclasses import dataclass

# A true x1000 leak lands within float/rounding noise of exactly 1000.
# The band is generous (900-1100) because the sibling signals are
# sometimes rounded to whole euros on one side only.
_RATIO_LO = 900.0
_RATIO_HI = 1100.0

# Tier-B floor: internally-consistent notices are only rescaled when the
# claimed value is >= this (EUR). Portugal's entire annual procurement
# is ~EUR 10-15B; no single real PT contract reaches 1B outside
# frameworks, which the gateway in question does not carry.
_PRIOR_FLOOR_EUR = 1e9
# ... and only when the corrected value lands back in a band where real
# contracts live. Below 10k the "correction" would more likely be
# manufacturing a wrong number than fixing one.
_CORRECTED_MIN_EUR = 1e4
_CORRECTED_MAX_EUR = 5e8

_SCALE = 1000.0

# Countries with a proven fixed-point-3 gateway leak. Alpha-3.
_AFFECTED_COUNTRIES = frozenset({"PRT"})


@dataclass(frozen=True)
class ScaleNormalization:  # pylint: disable=too-many-instance-attributes
    """Outcome of the pre-scoring scale check for one notice."""

    estimate_eur: float | None
    total_eur: float | None
    payable_eur: float | None
    # parallel corrections for the original-currency figures
    total_original: float | None
    payable_original: float | None
    corrected: bool
    tier: str | None          # "ratio" | "country_prior" | None
    detail: str | None


def _ratio(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return a / b


def _is_x1000(r: float | None) -> bool:
    return r is not None and _RATIO_LO <= r <= _RATIO_HI


def _has_cents_signature(v: float | None) -> bool:
    """True when v looks like (X.XX euros) * 1000: divisible by 10 but
    NOT by 1000 — the embedded-cents fingerprint of the milli-euro
    leak. A round-thousand value is ambiguous (could be a real award
    that genuinely disagrees with its estimate), so it does NOT count.

    2026-07 census: the fingerprint appears exclusively on PT notices;
    the ~x1000 disagreements elsewhere (POL, ESP, NLD) are all round
    thousands — different phenomena that must stay flagged, not
    "corrected" into plausible-looking wrong numbers.
    """
    if v is None:
        return False
    n = round(v)
    return abs(v - n) < 1e-6 and n % 10 == 0 and n % 1000 != 0


def _ratio_correctable(inflated: float | None, country: str | None) -> bool:
    """Ratio evidence alone says the two fields disagree by ~x1000 —
    but only the cents fingerprint (or a gateway already proven to
    leak) says which one is wrong AND why. Without either, leave the
    row to the value_disagreement flag path."""
    return _has_cents_signature(inflated) or country in _AFFECTED_COUNTRIES


def normalize_scale(  # pylint: disable=too-many-branches,too-many-arguments
    *,
    estimate_eur: float | None,
    total_eur: float | None,
    payable_eur: float | None,
    total_original: float | None = None,
    payable_original: float | None = None,
    country: str | None = None,
) -> ScaleNormalization:
    """Detect and undo x1000 milli-euro leaks before scoring.

    Returns corrected values plus a marker. When nothing is suspect the
    inputs pass through untouched.
    """
    est, tot, pay = estimate_eur, total_eur, payable_eur
    tot_orig, pay_orig = total_original, payable_original

    # Tier A: a sane sibling proves the scale.
    if _is_x1000(_ratio(tot, est)) and _ratio_correctable(tot, country):
        detail = (
            f"award total {tot:,.0f} is ~x1000 its own estimate "
            f"{est:,.0f}; milli-euro leak on the award fields"
        )
        tot = tot / _SCALE
        tot_orig = tot_orig / _SCALE if tot_orig is not None else None
        if _is_x1000(_ratio(pay, est)):
            pay = pay / _SCALE
            pay_orig = pay_orig / _SCALE if pay_orig is not None else None
        return ScaleNormalization(
            est, tot, pay, tot_orig, pay_orig, True, "ratio", detail,
        )

    if _is_x1000(_ratio(est, tot)) and _ratio_correctable(est, country):
        detail = (
            f"estimate {est:,.0f} is ~x1000 the award total {tot:,.0f}; "
            "milli-euro leak on the estimate field"
        )
        est = est / _SCALE
        return ScaleNormalization(
            est, tot, pay, tot_orig, pay_orig, True, "ratio", detail,
        )

    # Payable-only leak: total absent, payable disagrees with estimate.
    if tot is None and _is_x1000(_ratio(pay, est)) \
            and _ratio_correctable(pay, country):
        detail = (
            f"payable {pay:,.0f} is ~x1000 the estimate {est:,.0f}; "
            "milli-euro leak on the payable field"
        )
        pay = pay / _SCALE
        pay_orig = pay_orig / _SCALE if pay_orig is not None else None
        return ScaleNormalization(
            est, tot, pay, tot_orig, pay_orig, True, "ratio", detail,
        )

    # Tier B: everything consistent but absurd, on an affected gateway.
    if country in _AFFECTED_COUNTRIES:
        chosen = tot if tot is not None else pay
        internally_consistent = est is None or not (
            _is_x1000(_ratio(chosen, est)) or _is_x1000(_ratio(est, chosen))
        )
        if (
            chosen is not None
            and chosen >= _PRIOR_FLOOR_EUR
            and internally_consistent
            and _CORRECTED_MIN_EUR <= chosen / _SCALE <= _CORRECTED_MAX_EUR
        ):
            detail = (
                f"all monetary fields internally consistent at "
                f"{chosen:,.0f} EUR on a {country} notice — whole notice "
                "went through the milli-euro path; rescaled /1000"
            )
            est = est / _SCALE if est is not None else None
            tot = tot / _SCALE if tot is not None else None
            pay = pay / _SCALE if pay is not None else None
            tot_orig = tot_orig / _SCALE if tot_orig is not None else None
            pay_orig = pay_orig / _SCALE if pay_orig is not None else None
            return ScaleNormalization(
                est, tot, pay, tot_orig, pay_orig, True,
                "country_prior", detail,
            )

    return ScaleNormalization(
        est, tot, pay, tot_orig, pay_orig, False, None, None,
    )
