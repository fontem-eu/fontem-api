"""Sanity checks on the bundled SEED_DATASETS list."""
from __future__ import annotations

# pylint: disable=missing-function-docstring

from src.stats_etl.datasets import SEED_DATASETS, find


def test_seed_size_and_uniqueness():
    """The plan calls for 26 datasets — make sure we shipped them all and
    nobody accidentally duplicated a code."""
    assert len(SEED_DATASETS) >= 26
    codes = [d.code for d in SEED_DATASETS]
    assert len(codes) == len(set(codes)), \
        f"duplicate dataset codes: {set(c for c in codes if codes.count(c) > 1)}"


def test_seed_themes_cover_expected_buckets():
    """Plan-level promise: population, economy, labour, education, health,
    rd, social, digital, tourism, transport, geometry — plus migration
    and crime once they were added."""
    themes = {d.theme for d in SEED_DATASETS}
    expected = {"population", "health", "economy", "labour", "education",
                "rd", "social", "digital", "tourism", "transport", "geometry",
                "migration", "crime"}
    missing = expected - themes
    assert not missing, f"themes missing from seed: {missing}"


def test_every_seed_has_nuts_levels():
    for d in SEED_DATASETS:
        assert d.nuts_levels, f"{d.code}: empty nuts_levels"
        assert all(0 <= lvl <= 3 for lvl in d.nuts_levels), \
            f"{d.code}: invalid nuts_levels {d.nuts_levels}"


def test_every_seed_has_eurostat_source_url():
    for d in SEED_DATASETS:
        assert d.source == "eurostat"
        assert d.source_url.startswith("https://ec.europa.eu/")
        assert d.code.upper() in d.source_url


def test_find_returns_known_dataset():
    d = find("demo_r_pjangrp3")
    assert d is not None
    assert d.theme == "population"


def test_find_returns_none_for_unknown():
    assert find("not_a_real_code") is None


def test_eu_gbv_survey_datasets_present_and_country_level():
    """The EU-GBV survey pair is what lets an analyst separate violence
    that HAPPENED from violence that got RECORDED — crim_off_cat alone
    cannot, since its cross-country level tracks legal definitions and
    counting rules. Country-level only; Eurostat publishes no NUTS
    breakdown for the survey."""
    for code in ("gbv_vtp_perp", "gbv_vtp_rp"):
        d = find(code)
        assert d is not None, f"{code} missing from the seed catalog"
        assert d.theme == "crime", f"{code}: expected the crime theme"
        assert d.nuts_levels == [0], f"{code}: survey is country-level only"
        assert d.enabled


def test_eu_gbv_datasets_warn_they_are_a_single_wave():
    """One 2021 wave and a subset of Member States. The notes must say so:
    differencing these against the 2014 FRA survey looks like a trend and
    is not one — the instruments differ. Losing this caveat is how a
    cross-section silently becomes a time series in someone's chart."""
    for code in ("gbv_vtp_perp", "gbv_vtp_rp"):
        notes = (find(code).notes or "").lower()
        assert "2021" in notes, f"{code}: notes must name the wave"
        assert "subset" in notes, f"{code}: notes must flag partial country coverage"
