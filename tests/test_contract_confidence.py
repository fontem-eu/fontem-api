"""Validation of the contract value confidence score against the real
notices surfaced by the 2026-06-14 TED value investigation.

Each case carries the estimate / total / payable as they actually appear
in the source (the ROU / GRC / ITA figures are from the authoritative
eForms XML fetched during the investigation; the rest from per-notice
source verification). The score must:

  * KEEP the genuinely-fine contracts at confidence >= threshold, storing
    the *correct* value (TotalAmount), and in particular RECOVER the
    Forca Aerea aircraft (EUR 7.27M) and ROU VAMTAC (EUR 9.99M) instead of
    the x1000-corrupted payable;
  * FLAG every corrupted/impossible value below threshold, including the
    Slovak hospital where the estimate and total agree with each other but
    are jointly inflated (only the plausibility component catches it);
  * treat negative concession values as their own flagged category.

If a tuning change regresses any of these, this test fails -- the score is
only as good as its behaviour on the cases that motivated it.
"""
import pytest

from src.etl.contract_confidence import (
    LOW_CONFIDENCE_THRESHOLD,
    ValueFlag,
    score_contract_value,
)

# (name, estimate_eur, total_eur, payable_eur, expect_keep, expect_flag)
CASES = [
    ("PRT aircraft (Forca Aerea)", 7_317_073.17, 7_274_615.93, 7_274_615_930,
     True, ValueFlag.OK),
    ("ROU VAMTAC vehicles", 9_999_560, 9_989_377.06, 9_989_377_060,
     True, ValueFlag.OK),
    ("healthy small contract", 100_000, 98_000, 98_000, True, ValueFlag.OK),
    ("legit EUR 500M framework", 480_000_000, 500_000_000, 500_000_000,
     True, ValueFlag.OK),

    ("GRC Drama digital x1e6", 1_083_901.24, 1_073_062_200_000, 1_073_062_200_000,
     False, ValueFlag.IMPLAUSIBLE_MAGNITUDE),
    ("ITA museum x84k", 2_997_320.60, 252_295_847_646, None,
     False, ValueFlag.VALUE_DISAGREEMENT),
    ("SWE Umea bus x1e6", 172_000_000, 182_082_833_599_378, None,
     False, ValueFlag.IMPLAUSIBLE_MAGNITUDE),
    ("FRA port x1000", 72_000_000, 75_968_000_000, None,
     False, ValueFlag.VALUE_DISAGREEMENT),
    ("ESP firefighting x1000", 44_955_000, 44_955_000_000, None,
     False, ValueFlag.VALUE_DISAGREEMENT),
    ("SVN food x1e6", 30_000, 38_627_490_100, None,
     False, ValueFlag.VALUE_DISAGREEMENT),
    ("DEU schools x1000 no estimate", None, 33_260_500_000, None,
     False, ValueFlag.IMPLAUSIBLE_MAGNITUDE),

    ("SVK hospital both x100", 35_000_094_166, 29_745_741_813, None,
     False, ValueFlag.IMPLAUSIBLE_MAGNITUDE),

    ("BEL agency framework x100", 204_000_000, None, 20_788_495_891,
     False, ValueFlag.VALUE_DISAGREEMENT),
    ("BEL hospital insurance x100", 171_000_000, 17_398_482_782, None,
     False, ValueFlag.VALUE_DISAGREEMENT),
    ("IRL HCM payroll x100", 95_000_000, 10_272_000_000, None,
     False, ValueFlag.VALUE_DISAGREEMENT),
    ("POL diesel no estimate", None, 257_760_216_431, None,
     False, ValueFlag.IMPLAUSIBLE_MAGNITUDE),

    ("FRA Paris concession 19B", 15_000_000_000, 19_000_000_000, None,
     False, ValueFlag.IMPLAUSIBLE_MAGNITUDE),
]


@pytest.mark.parametrize(
    "name,est,tot,pay,expect_keep,expect_flag",
    CASES,
    ids=[c[0] for c in CASES],
)
def test_scoring_matches_investigation(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    name, est, tot, pay, expect_keep, expect_flag,
):
    r = score_contract_value(estimate_eur=est, total_eur=tot, payable_eur=pay)
    kept = r.confidence >= LOW_CONFIDENCE_THRESHOLD
    assert kept is expect_keep, (
        f"{name}: confidence {r.confidence:.3f} "
        f"(cons {r.consistency:.2f} x plaus {r.plausibility:.2f}) "
        f"=> kept={kept}, expected keep={expect_keep}. {r.reason}"
    )
    assert r.flag is expect_flag, (
        f"{name}: flag {r.flag} != expected {expect_flag}. {r.reason}"
    )


def test_aircraft_and_rou_recover_the_correct_total_not_the_x1000_payable():
    """The headline fix: prefer TotalAmount, so the value stored is the
    real ~EUR 7.27M / 9.99M, not the x1000-corrupted PayableAmount."""
    aircraft = score_contract_value(
        estimate_eur=7_317_073.17, total_eur=7_274_615.93,
        payable_eur=7_274_615_930,
        total_original=7_274_615.93, payable_original=7_274_615_930,
    )
    assert aircraft.chosen_field == "total"
    assert abs(aircraft.chosen_value - 7_274_615.93) < 1
    # Kept (estimate corroborates the stored total) but the x1000 payable
    # is flagged as an internal source inconsistency, so confidence sits
    # below a clean contract's 1.0.
    assert aircraft.confidence >= LOW_CONFIDENCE_THRESHOLD
    assert aircraft.confidence < 1.0
    assert aircraft.has_payable_discrepancy is True

    rou = score_contract_value(
        estimate_eur=9_999_560, total_eur=9_989_377.06,
        payable_eur=9_989_377_060,
        total_original=9_989_377.06, payable_original=9_989_377_060,
    )
    assert rou.chosen_field == "total"
    assert abs(rou.chosen_value - 9_989_377.06) < 1
    assert rou.has_payable_discrepancy is True


def test_clean_contract_with_agreeing_payable_has_no_discrepancy():
    """When total and payable agree, no discrepancy flag and full
    confidence -- the payable corroborates rather than penalises."""
    r = score_contract_value(
        estimate_eur=480_000_000, total_eur=500_000_000, payable_eur=500_000_000,
    )
    assert r.has_payable_discrepancy is False
    assert r.confidence >= 0.99


def test_slovak_hospital_needs_plausibility_not_just_consistency():
    """Both fields agree (consistency high) yet jointly impossible; the
    plausibility factor is what flags it. Guards the two-component design."""
    r = score_contract_value(
        estimate_eur=35_000_094_166, total_eur=29_745_741_813, payable_eur=None,
    )
    assert r.consistency > 0.9
    assert r.plausibility < 0.5
    assert r.confidence < LOW_CONFIDENCE_THRESHOLD
    assert r.flag is ValueFlag.IMPLAUSIBLE_MAGNITUDE


def test_negative_concession_is_kept_and_flagged():
    r = score_contract_value(
        estimate_eur=None, total_eur=-13_310_960.29, payable_eur=None,
        total_original=-151_159_265,
    )
    assert r.flag is ValueFlag.CONCESSION_NEGATIVE
    assert r.is_low_confidence is True
    assert r.chosen_value == -151_159_265


def test_zero_and_missing_values_distinguished():
    zero = score_contract_value(estimate_eur=10_000, total_eur=0, payable_eur=None)
    assert zero.flag is ValueFlag.ZERO_VALUE
    missing = score_contract_value(
        estimate_eur=10_000, total_eur=None, payable_eur=None,
    )
    assert missing.flag is ValueFlag.NO_AWARDED_VALUE
    assert missing.chosen_value is None


def test_as_payload_shape():
    r = score_contract_value(estimate_eur=100_000, total_eur=98_000, payable_eur=98_000)
    p = r.as_payload()
    assert set(p) == {
        "value_confidence", "value_confidence_consistency",
        "value_confidence_plausibility", "value_quality_flag",
        "value_low_confidence", "value_payable_discrepancy",
    }
    assert p["value_quality_flag"] == "ok"
    assert p["value_low_confidence"] is False
    assert p["value_payable_discrepancy"] is False
