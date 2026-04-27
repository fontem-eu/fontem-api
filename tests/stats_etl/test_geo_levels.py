"""Tests for stats_etl.geo_levels."""
from __future__ import annotations

# pylint: disable=missing-function-docstring

from src.stats_etl.geo_levels import country_of, detect_nuts_level, parent_code


def test_detect_nuts_level_country():
    assert detect_nuts_level("BE") == 0
    assert detect_nuts_level("DE") == 0
    assert detect_nuts_level("EL") == 0  # Greece's Eurostat code


def test_detect_nuts_level_nuts1_2_3():
    assert detect_nuts_level("BE2") == 1
    assert detect_nuts_level("BE21") == 2
    assert detect_nuts_level("BE211") == 3


def test_detect_nuts_level_unknown():
    # Eurostat aggregates like EU27_2020 are not NUTS — return None.
    assert detect_nuts_level("EU27_2020") is None
    assert detect_nuts_level("") is None
    assert detect_nuts_level(None) is None  # type: ignore[arg-type]


def test_parent_code():
    assert parent_code("BE211") == "BE21"
    assert parent_code("BE21") == "BE2"
    assert parent_code("BE2") == "BE"
    assert parent_code("BE") is None  # country has no parent
    assert parent_code("EU27_2020") is None


def test_country_of():
    assert country_of("BE211") == "BE"
    assert country_of("DE") == "DE"
    assert country_of("9X") is None  # not alpha
    assert country_of("") is None
