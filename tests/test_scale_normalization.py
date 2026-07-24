"""Milli-euro (x1000) gateway-leak normalization — the four canonical
shapes from the 2026-07 investigation, plus guard rails."""
from src.etl.scale_normalization import normalize_scale


def test_school_case_whole_notice_inflated_prt():
    """Figueira da Foz school: est+total+payable all x1000, internally
    consistent — only the PRT country prior can catch it."""
    r = normalize_scale(
        estimate_eur=9289549170.0, total_eur=9281922790.0,
        payable_eur=9281922790.0, total_original=9281922790.0,
        country="PRT",
    )
    assert r.corrected and r.tier == "country_prior"
    assert r.total_eur == 9281922.79
    assert r.estimate_eur == 9289549.17
    assert r.payable_eur == 9281922.79
    assert r.total_original == 9281922.79


def test_aircraft_case_sane_estimate_proves_scale():
    """Forca Aerea: estimate sane, award x1000 — ratio evidence."""
    r = normalize_scale(
        estimate_eur=7317073.17, total_eur=7274615930.0,
        payable_eur=7274615930.0, country="PRT",
    )
    assert r.corrected and r.tier == "ratio"
    assert r.total_eur == 7274615.93
    assert r.estimate_eur == 7317073.17  # untouched


def test_inflated_estimate_sane_award():
    r = normalize_scale(
        estimate_eur=3219000000.0, total_eur=3219000.0,
        payable_eur=None, country="PRT",
    )
    assert r.corrected and r.tier == "ratio"
    assert r.estimate_eur == 3219000.0
    assert r.total_eur == 3219000.0


def test_normal_contract_untouched():
    r = normalize_scale(
        estimate_eur=150000.0, total_eur=149500.0,
        payable_eur=149500.0, country="PRT",
    )
    assert not r.corrected
    assert r.total_eur == 149500.0


def test_non_affected_country_mega_framework_untouched():
    """Polish rail framework at EUR 15.5B is huge but internally
    consistent and NOT on an affected gateway — leave it to the
    plausibility flags, do not rescale."""
    r = normalize_scale(
        estimate_eur=15.4e9, total_eur=15.47e9, payable_eur=None,
        country="POL",
    )
    assert not r.corrected


def test_prior_tier_needs_corrected_value_in_plausible_band():
    """A PRT value whose /1000 would land below 10k is not 'corrected'
    into a plausible-looking wrong number."""
    r = normalize_scale(
        estimate_eur=None, total_eur=2e9, payable_eur=None, country="PRT",
    )
    assert r.corrected  # 2e9/1000 = 2M -> in band, corrected
    r2 = normalize_scale(
        estimate_eur=None, total_eur=1.2e9, payable_eur=None, country="ESP",
    )
    assert not r2.corrected  # not an affected gateway


def test_payable_only_leak():
    r = normalize_scale(
        estimate_eur=200000.0, total_eur=None,
        payable_eur=199000000.0, payable_original=199000000.0,
        country="PRT",
    )
    assert r.corrected and r.tier == "ratio"
    assert r.payable_eur == 199000.0
    assert r.payable_original == 199000.0


def test_round_thousand_disagreement_outside_pt_not_corrected():
    """POL award 5,169,000,000 vs estimate ~5,169,000: ratio is x1000
    but the value is a round thousand and POL has no proven leak —
    stays uncorrected (value_disagreement flag path)."""
    r = normalize_scale(
        estimate_eur=5169000.0, total_eur=5169000000.0,
        payable_eur=None, country="POL",
    )
    assert not r.corrected


def test_cents_signature_corrects_regardless_of_country():
    """A non-PT value carrying the embedded-cents fingerprint
    (…930 = X.93 * 1000) is unambiguous — corrected."""
    r = normalize_scale(
        estimate_eur=7317073.17, total_eur=7274615930.0,
        payable_eur=None, country="NLD",
    )
    assert r.corrected and r.tier == "ratio"
    assert r.total_eur == 7274615.93


def test_round_thousand_on_pt_still_corrected():
    """PT round-thousand x1000 disagreements ride the proven-gateway
    prior (71/110 of PT type-A census rows are round thousands)."""
    r = normalize_scale(
        estimate_eur=3219000.0, total_eur=3219000000.0,
        payable_eur=None, country="PRT",
    )
    assert r.corrected and r.tier == "ratio"
