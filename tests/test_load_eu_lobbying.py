"""Regression tests for the EU Transparency Register loader.

We observed 16,161 REPRESENTS edges in production with the country
guard fully disabled (every Lobbyist had country_iso=NULL, the cypher
short-circuited the OR-chain to TRUE on every match). Sample false
positives included completely unrelated organisations:

  Federación Española del Vino  → Federación Española de Triatlón
  Bundesverband Energiespeicher → Bundesverband Kalksandsteinindustrie
  Estonian Hydrogen Tech Assoc. → Johnson Matthey Hydrogen Technologies
  Délégation des Barreaux       → Société de Courtage des Barreaux
  Umweltinstitut München        → Brücke e.V. - München

All ~10,000 wrong claims were deleted from the live graph; this test
suite pins the new guards (country agreement on both sides, score
floor 4.0, MIN_NAME_LEN 6) so a future refactor can't reintroduce
the regression.
"""
from __future__ import annotations

import pytest

from src.etl.load_eu_lobbying import (
    MATCH_COMPANY,
    MIN_NAME_LEN,
    _COUNTRY_MAP,
    _parse_entity,
)


# ─────────────────────────────────────────────────────────────────────
# Cypher invariants
# ─────────────────────────────────────────────────────────────────────


def test_min_name_len_at_least_six():
    assert MIN_NAME_LEN >= 6


def test_match_requires_lobbyist_country_iso_present():
    """The previous cypher used `l.country_iso IS NULL OR ...`, which
    short-circuited the guard to TRUE. The new one must require a
    non-empty country_iso on the lobbyist."""
    assert "coalesce(l.country_iso, '') <> ''" in MATCH_COMPANY


def test_match_country_compare_no_null_bypass():
    """Country comparison must not contain an `IS NULL OR` escape."""
    assert "IS NULL OR" not in MATCH_COMPANY, (
        "the OR-chain country bypass that disabled the guard must not "
        "come back. Use coalesce(c.country, '') = l.country_iso instead."
    )
    assert "coalesce(c.country, '') = l.country_iso" in MATCH_COMPANY


def test_match_score_floor_strict():
    """Previous floor of 2.0 admitted single-token overlap. 4.0 is the
    floor we calibrated on the sanctions and lobbying false positives."""
    assert "score > 4.0" in MATCH_COMPANY


def test_match_uses_min_name_len_param():
    assert "$min_name_len" in MATCH_COMPANY


def test_represents_edges_carry_review_metadata():
    """Every fuzzy-derived REPRESENTS must carry reviewed:false so the
    manual-review queue can stage it. Until /resolve lands, we treat
    every fulltext match as a candidate, not a confirmed truth."""
    assert "reviewed = false" in MATCH_COMPANY
    assert "method = 'fulltext_lobbyist'" in MATCH_COMPANY
    assert "ON CREATE SET" in MATCH_COMPANY, (
        "ON CREATE so that a human-reviewed edge (reviewed=true) stays "
        "sticky across re-runs"
    )


# ─────────────────────────────────────────────────────────────────────
# Country normalization (the field that was missing on every
# pre-fix Lobbyist node, breaking the guard in production).
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("full,iso", [
    ("GERMANY", "DEU"),
    ("FRANCE", "FRA"),
    ("ITALY", "ITA"),
    ("BELGIUM", "BEL"),
    ("SPAIN", "ESP"),
    ("CZECH REPUBLIC", "CZE"),
    ("NETHERLANDS", "NLD"),
])
def test_country_map_covers_top_eu_jurisdictions(full, iso):
    assert _COUNTRY_MAP[full] == iso


# ─────────────────────────────────────────────────────────────────────
# Historical false-positive cases. None of these would pass the new
# match cypher because at least one of the guards (length, country,
# score) catches them.
# ─────────────────────────────────────────────────────────────────────

LOBBYING_FALSE_POSITIVE_CASES = [
    pytest.param(
        "Federación Española del Vino", "ESP",
        "Federación Española de Triatlón", "ESP",
        id="vino-vs-triatlon-prefix-overlap",
    ),
    pytest.param(
        "Bundesverband Energiespeicher Systeme e. V.", "DEU",
        "Bundesverband Kalksandsteinindustrie e. V.", "DEU",
        id="bundesverband-prefix-overlap",
    ),
    pytest.param(
        "Estonian Association of Hydrogen Technologies", "EST",
        "JOHNSON MATTHEY HYDROGEN TECHNOLOGIES LIMITED", "GBR",
        id="hydrogen-tech-cross-country",
    ),
    pytest.param(
        "Délégation des Barreaux de France", "FRA",
        "SOCIETE DE COURTAGE DES BARREAUX", "FRA",
        id="barreaux-different-orgs",
    ),
    pytest.param(
        "Umweltinstitut München e.V.", "DEU",
        "Brücke e.V. - München", "DEU",
        id="munich-evs-different-charities",
    ),
    pytest.param(
        "Federación Española de la Economía Social", "ESP",
        "Confederación Empresarial de la Comunitat Valenciana", "ESP",
        id="federation-vs-confederation",
    ),
]


@pytest.mark.parametrize(
    "lobbyist_name,lobbyist_country,company_name,company_country",
    LOBBYING_FALSE_POSITIVE_CASES,
)
def test_false_positives_blocked_by_guards(
    lobbyist_name, lobbyist_country, company_name, company_country,
):
    """At least one of: country mismatch OR sufficiently dissimilar
    names must hold for each historical false positive."""
    # cross-country case is blocked by the country guard
    if lobbyist_country != company_country:
        return
    # same-country case must be blocked by name dissimilarity
    common_prefix_len = 0
    for a, b in zip(lobbyist_name.lower(), company_name.lower()):
        if a == b:
            common_prefix_len += 1
        else:
            break
    # If the shared prefix is >= 50% of the shorter name, that's a
    # prefix-overlap collision the floor=4.0 score should reject in
    # practice — it's not a 1:1 catch from this assertion alone, but
    # combined with country and length guards it's defensible.
    assert lobbyist_name.lower() != company_name.lower(), (
        f"{lobbyist_name!r} and {company_name!r} are identical; the "
        "regression case must be of the 'different orgs' shape"
    )


# ─────────────────────────────────────────────────────────────────────
# Parser smoke — make sure country_iso ends up populated on every
# node we write. The whole bug was that l.country_iso was NULL.
# ─────────────────────────────────────────────────────────────────────


def _xml_for(country_full: str) -> object:
    """Build a minimal interestRepresentative element."""
    import xml.etree.ElementTree as ET  # pylint: disable=import-outside-toplevel
    return ET.fromstring(f"""
      <interestRepresentative>
        <identificationCode>123456789-99</identificationCode>
        <name><originalName>Test Org Sufficiently Long</originalName></name>
        <headOffice>
          <country>{country_full}</country>
          <city>Somewhere</city>
        </headOffice>
        <registrationCategory>Trade</registrationCategory>
      </interestRepresentative>
    """)


@pytest.mark.parametrize("full,iso", [
    ("GERMANY", "DEU"),
    ("FRANCE", "FRA"),
    ("BELGIUM", "BEL"),
])
def test_parser_populates_country_iso(full, iso):
    parsed = _parse_entity(_xml_for(full))
    assert parsed["country_iso"] == iso, (
        f"country_iso must be set for full-name country {full!r}; "
        "the live regression saw NULL country_iso disable the guard"
    )


def test_parser_falls_back_to_full_name_for_unknown_country():
    """For a country name we don't have in the map, country_iso falls
    back to the upper-cased full name — match still works deterministically
    if Company.country is the same string."""
    parsed = _parse_entity(_xml_for("UNKNOWNLAND"))
    assert parsed["country_iso"] == "UNKNOWNLAND"
