"""Tests for the LocationService.

Covers alpha-2/alpha-3 conversion, NUTS validation,
and edge cases (None, empty, mixed case, unknown codes).
"""
# pylint: disable=missing-function-docstring,missing-class-docstring
from __future__ import annotations

from src.services.location_service import LocationService


class TestToAlpha3:
    """to_alpha3: accepts both alpha-2 and alpha-3, returns alpha-3."""

    def test_alpha2_de(self):
        assert LocationService.to_alpha3("DE") == "DEU"

    def test_alpha2_pt(self):
        assert LocationService.to_alpha3("PT") == "PRT"

    def test_alpha2_gb(self):
        assert LocationService.to_alpha3("GB") == "GBR"

    def test_alpha3_passthrough(self):
        assert LocationService.to_alpha3("DEU") == "DEU"

    def test_alpha3_prt(self):
        assert LocationService.to_alpha3("PRT") == "PRT"

    def test_lowercase(self):
        assert LocationService.to_alpha3("de") == "DEU"

    def test_mixed_case(self):
        assert LocationService.to_alpha3("De") == "DEU"

    def test_none(self):
        assert LocationService.to_alpha3(None) is None

    def test_empty_string(self):
        assert LocationService.to_alpha3("") is None

    def test_whitespace(self):
        assert LocationService.to_alpha3("  ") is None

    def test_whitespace_around(self):
        assert LocationService.to_alpha3(" PT ") == "PRT"

    def test_unknown_alpha2(self):
        assert LocationService.to_alpha3("ZZ") is None

    def test_unknown_alpha3(self):
        assert LocationService.to_alpha3("ZZZ") is None

    def test_greece_el(self):
        """EU uses EL for Greece, not GR."""
        assert LocationService.to_alpha3("EL") == "GRC"

    def test_greece_gr(self):
        assert LocationService.to_alpha3("GR") == "GRC"

    def test_kosovo(self):
        assert LocationService.to_alpha3("XK") == "XKX"

    def test_uk_alias(self):
        """NUTS uses UK, ISO uses GB."""
        assert LocationService.to_alpha3("UK") == "GBR"


class TestAlpha2ToAlpha3:

    def test_basic(self):
        assert LocationService.alpha2_to_alpha3("FR") == "FRA"

    def test_none(self):
        assert LocationService.alpha2_to_alpha3(None) is None

    def test_empty(self):
        assert LocationService.alpha2_to_alpha3("") is None

    def test_unknown(self):
        assert LocationService.alpha2_to_alpha3("ZZ") is None


class TestAlpha3ToAlpha2:

    def test_basic(self):
        assert LocationService.alpha3_to_alpha2("FRA") == "FR"

    def test_none(self):
        assert LocationService.alpha3_to_alpha2(None) is None

    def test_empty(self):
        assert LocationService.alpha3_to_alpha2("") is None

    def test_unknown(self):
        assert LocationService.alpha3_to_alpha2("ZZZ") is None


class TestValidateNuts:
    """validate_nuts: checks NUTS pattern (country + 1-3 alphanumeric)."""

    def test_nuts1(self):
        assert LocationService.validate_nuts("PT1") is True

    def test_nuts2(self):
        assert LocationService.validate_nuts("PT11") is True

    def test_nuts3(self):
        assert LocationService.validate_nuts("PT11A") is True

    def test_country_only_not_valid(self):
        """Level 0 (just country code) is not a NUTS 1-3 code."""
        assert LocationService.validate_nuts("PT") is False

    def test_too_long(self):
        assert LocationService.validate_nuts("PT11AB") is False

    def test_empty(self):
        assert LocationService.validate_nuts("") is False

    def test_none(self):
        assert LocationService.validate_nuts(None) is False

    def test_lowercase(self):
        assert LocationService.validate_nuts("pt11a") is True


class TestNutsLevel:

    def test_level_0(self):
        assert LocationService.nuts_level("PT") == 0

    def test_level_1(self):
        assert LocationService.nuts_level("PT1") == 1

    def test_level_2(self):
        assert LocationService.nuts_level("PT11") == 2

    def test_level_3(self):
        assert LocationService.nuts_level("PT11A") == 3

    def test_none(self):
        assert LocationService.nuts_level(None) is None

    def test_empty(self):
        assert LocationService.nuts_level("") is None

    def test_too_long(self):
        assert LocationService.nuts_level("PT11AB") is None


class TestCountryFromNuts:

    def test_nuts3(self):
        assert LocationService.country_from_nuts("PT11A") == "PRT"

    def test_nuts2(self):
        assert LocationService.country_from_nuts("DE21") == "DEU"

    def test_nuts1(self):
        assert LocationService.country_from_nuts("FR1") == "FRA"

    def test_nuts0(self):
        assert LocationService.country_from_nuts("IT") == "ITA"

    def test_greece(self):
        assert LocationService.country_from_nuts("EL30") == "GRC"

    def test_none(self):
        assert LocationService.country_from_nuts(None) is None

    def test_empty(self):
        assert LocationService.country_from_nuts("") is None

    def test_single_char(self):
        assert LocationService.country_from_nuts("P") is None
