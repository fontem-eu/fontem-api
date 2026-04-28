"""Regression tests for the EU sanctions loader.

The original incident: 8 SANCTIONED edges in production, all false
positives, where 3-4 letter Company names ("AMD", "TSA", "CRL",
"LRA", "NADA") matched a SanctionedEntity whose primary `name` was
the same short code (the actual entity name lived in `aliases`).
All 8 companies were unrelated EU entities — defamation risk.

The fix migrated this loader to gmr-consolidator's /resolve service.
This file pins:

  - the in-cypher matchers (MATCH_COMPANY_EXACT / _FUZZY) must NOT
    come back; bringing them back means bypassing the resolver's
    central guards (MIN_NAME_LEN, country normalisation, score floor)
  - SANCTIONED edges only get written for confident /resolve hits,
    and even then with reviewed=false (defamation-class consequences
    require human sign-off)
  - the loader retries each alias (the real entity name lives there,
    not in the primary `name` field which is often an acronym)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import src.etl.load_eu_sanctions as load_sanctions
from src.etl._hooks import ResolveMatch, ResolveResult
from src.etl.load_eu_sanctions import (
    MERGE_SANCTIONED,
    MIN_NAME_LEN,
    _resolve_sanction_to_company,
    parse_sanctions_xml,
)

NS = "http://eu.europa.ec/fpi/fsd/export"


def _wrap(entities_xml: str) -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<export xmlns="{NS}">{entities_xml}</export>'
    ).encode("utf-8")


# ─────────────────────────────────────────────────────────────────────
# The old in-cypher matchers must be gone.
# ─────────────────────────────────────────────────────────────────────


def test_in_cypher_matchers_removed():
    assert not hasattr(load_sanctions, "MATCH_COMPANY_EXACT"), (
        "MATCH_COMPANY_EXACT belongs to the past — sanction → company "
        "linkage now goes through gmr-consolidator's /resolve endpoint."
    )
    assert not hasattr(load_sanctions, "MATCH_COMPANY_FUZZY")
    assert not hasattr(load_sanctions, "CREATE_FT_INDEX")


def test_min_name_len_unchanged():
    """The resolver enforces this internally too. Local constant kept
    for backward-compatible imports in the test suite."""
    assert MIN_NAME_LEN >= 6


# ─────────────────────────────────────────────────────────────────────
# MERGE_SANCTIONED cypher invariants
# ─────────────────────────────────────────────────────────────────────


def test_merge_sanctioned_uses_resolver_gmr_id():
    """Edges are now written from a gmr_id supplied by the resolver,
    NOT by name matching here."""
    assert "MATCH (c:Company {gmr_id: row.gmr_id})" in MERGE_SANCTIONED
    assert "fulltext" not in MERGE_SANCTIONED.lower()


def test_merge_sanctioned_always_unreviewed():
    """Sanctions matches always start as reviewed=false regardless of
    resolver tier — defamation consequences are too severe to lean on
    automated tiers alone."""
    assert "r.reviewed = false" in MERGE_SANCTIONED


def test_merge_sanctioned_records_tier_and_alias_metadata():
    assert "r.tier = row.tier" in MERGE_SANCTIONED
    assert "r.matched_via_alias = row.matched_via_alias" in MERGE_SANCTIONED


# ─────────────────────────────────────────────────────────────────────
# Historical false-positive cases — the resolver must reject them.
# ─────────────────────────────────────────────────────────────────────

REGRESSION_FALSE_POSITIVE_CASES = [
    pytest.param("AMD",  "IR", id="AMD-Iranian-defense"),
    pytest.param("TSA",  "IR", id="TSA-Iran-Centrifuge"),
    pytest.param("CRL",  "IR", id="CRL-Iran-Composites"),
    pytest.param("LRA",  "UG", id="LRA-Lords-Resistance-Army"),
    pytest.param("NADA", "KP", id="NADA-DPRK-Aerospace"),
]


@pytest.mark.parametrize(
    "short_name,nationality", REGRESSION_FALSE_POSITIVE_CASES,
)
def test_short_acronym_with_no_useful_aliases_yields_no_match(
    short_name, nationality,
):
    """If a sanction's primary name is too short and there are no
    aliases, the loader must yield None (no row → no SANCTIONED edge).
    This is the shape of the original 8 false positives."""
    entity = {
        "entity_id": "x",
        "name": short_name,
        "aliases": [],
        "nationality": nationality,
        "designation_date": "2011-05-24",
    }
    with patch(
        "src.etl.load_eu_sanctions.resolve_entity",
        return_value=ResolveResult(
            hint="no_match", match=None, candidates=[],
            normalised_country=nationality,
        ),
    ):
        row = _resolve_sanction_to_company(entity)
    assert row is None


def test_resolver_called_with_each_alias_until_match():
    """When the primary name doesn't match, the loader must try each
    alias in turn — the actual entity name (e.g. "Aran Modern Devices")
    lives there."""
    calls: list[str] = []

    def _resolve(*, name, country, **_):  # pylint: disable=unused-argument
        calls.append(name)
        if name == "Aran Modern Devices":
            return ResolveResult(
                hint="matched",
                match=ResolveMatch(
                    gmr_id="gmr-real-amd",
                    name="Aran Modern Devices",
                    country="IRN",
                    lei=None,
                    tier="name_country",
                    confidence=0.95,
                ),
                candidates=[],
                normalised_country="IRN",
            )
        return ResolveResult(
            hint="no_match", match=None, candidates=[],
            normalised_country="IRN",
        )

    entity = {
        "entity_id": "EU.2518.30",
        "name": "AMD",
        "aliases": ["Aran Modern Devices", "Aran"],
        "nationality": "IR",
        "designation_date": "2011-05-24",
    }
    with patch("src.etl.load_eu_sanctions.resolve_entity", side_effect=_resolve):
        row = _resolve_sanction_to_company(entity)
    assert row is not None
    assert row["gmr_id"] == "gmr-real-amd"
    assert row["matched_via_alias"] is True
    # The resolver was called with the primary name first, then each
    # alias in order until a match was found.
    assert calls[0] == "AMD"
    assert calls[1] == "Aran Modern Devices"


def test_first_call_match_does_not_use_alias():
    """When the primary name itself resolves cleanly (a long, unique
    name), matched_via_alias is False."""
    entity = {
        "entity_id": "EU.X",
        "name": "Specific Long Entity Name",
        "aliases": ["Specific Long Entity"],
        "nationality": "DE",
        "designation_date": "2024-01-01",
    }
    with patch(
        "src.etl.load_eu_sanctions.resolve_entity",
        return_value=ResolveResult(
            hint="matched",
            match=ResolveMatch(
                gmr_id="gmr-x", name="Specific Long Entity Name",
                country="DEU", lei=None, tier="name_country", confidence=0.95,
            ),
            candidates=[], normalised_country="DEU",
        ),
    ):
        row = _resolve_sanction_to_company(entity)
    assert row is not None
    assert row["matched_via_alias"] is False
    assert row["tier"] == "name_country"


def test_empty_nationality_yields_no_match():
    """The resolver requires a country; sanctions with blank nationality
    are skipped rather than risk an unguarded match."""
    entity = {
        "entity_id": "x",
        "name": "Some Long Entity Name",
        "aliases": [],
        "nationality": "",
        "designation_date": "2020-01-01",
    }
    row = _resolve_sanction_to_company(entity)
    assert row is None


# ─────────────────────────────────────────────────────────────────────
# Parser smoke
# ─────────────────────────────────────────────────────────────────────


def test_parse_extracts_aliases():
    xml = _wrap(f"""
      <sanctionEntity euReferenceNumber="EU.2518.30">
        <subjectType code="enterprise"/>
        <nameAlias wholeName="AMD"/>
        <nameAlias wholeName="Aran Modern Devices"/>
        <citizenship countryIso2Code="IR"/>
        <regulation publicationDate="2011-05-24" programme="IRN"
                    numberTitle="503/2011 (OJ L136)"/>
      </sanctionEntity>
    """)
    out = list(parse_sanctions_xml(xml))
    assert len(out) == 1
    record = out[0]
    assert record["name"] == "AMD"
    assert "Aran Modern Devices" in record["aliases"]
    assert record["nationality"] == "IR"
