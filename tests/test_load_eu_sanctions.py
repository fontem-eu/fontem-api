"""Regression tests for the EU sanctions loader.

The original incident: 8 SANCTIONED edges in production, all false
positives, where 3-4 letter Company names ("AMD", "TSA", "CRL",
"LRA", "NADA") matched a SanctionedEntity whose primary `name` was
the same short code (the actual entity name lived in `aliases`).
All 8 companies were unrelated EU entities — defamation risk.

The *auto* SANCTIONED edge stays retired. The resolver's confident,
guarded matches are now emitted as ``AssertSameAs`` *review
candidates* (unreviewed ``:SAME_AS``) for a human to adjudicate —
never an automatic edge. The in-cypher Cypher guards (MERGE_ENTITY,
MERGE_SANCTIONED, the in-cypher matchers) stay gone. This file pins:

  - the historical short-acronym false positives still resolve to
    no-match (the resolver guards stand on their own) and so produce
    ZERO same_as review candidates
  - a confident, guarded match becomes a same_as review candidate row
  - the resolver retries each alias when the primary fails
  - the in-cypher matchers (MATCH_COMPANY_EXACT/_FUZZY) and the
    Neo4j MERGE templates do NOT come back — the loader writes events
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import src.etl.load_eu_sanctions as load_sanctions
from src.etl._hooks import ResolveMatch, ResolveResult
from src.etl.load_eu_sanctions import (
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
    """The legacy in-cypher matchers (and the broader Cypher write
    surface) must NOT come back. Phase 2 retired the Neo4j-side
    store; reintroducing any of these means we re-introduced a
    parallel write path that drifts from Virtuoso."""
    forbidden = (
        "MATCH_COMPANY_EXACT", "MATCH_COMPANY_FUZZY", "CREATE_FT_INDEX",
        "MERGE_ENTITY", "MERGE_SANCTIONED", "CONSTRAINT_CYPHER",
        "load_into_neo4j",
    )
    for name in forbidden:
        assert not hasattr(load_sanctions, name), (
            f"{name} belongs to the past — sanctions live in Virtuoso "
            "now and the loader must not write Neo4j."
        )


def test_min_name_len_unchanged():
    """The resolver enforces this internally too. Local constant kept
    for backward-compatible imports in the test suite."""
    assert MIN_NAME_LEN >= 6


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
    xml = _wrap("""
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
    # Portal exposes alpha-2 ("IR"); loader normalises to alpha-3 ("IRN")
    # for fontem's internal convention.
    assert record["nationality"] == "IRN"


# ─────────────────────────────────────────────────────────────────────
# New behaviour: confident matches become same_as REVIEW candidates,
# acronym false-positives become nothing at all.
# ─────────────────────────────────────────────────────────────────────


def test_guarded_match_becomes_same_as_review_candidate():
    """A confident, guarded resolver match is returned as a same_as
    REVIEW candidate row (entity_id + gmr_id + tier) — the caller emits
    it as an AssertSameAs (unreviewed), never an automatic SANCTIONED
    edge."""
    entities = [{
        "entity_id": "EU.X",
        "name": "Specific Long Entity Name",
        "aliases": [],
        "nationality": "DE",
        "designation_date": "2024-01-01",
    }]
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
        rows, summary = load_sanctions.resolve_company_links(entities)
    assert summary["matched"] == 1
    assert len(rows) == 1
    assert rows[0]["entity_id"] == "EU.X"
    assert rows[0]["resolved_gmr_id"] == "gmr-x"
    assert rows[0]["name"] == "Specific Long Entity Name"
    assert rows[0]["tier"] == "name_country"


@pytest.mark.parametrize(
    "short_name,nationality", REGRESSION_FALSE_POSITIVE_CASES,
)
def test_acronym_fp_produces_no_same_as_candidate(short_name, nationality):
    """The historical 8 false positives (short acronyms with no useful
    alias) must produce ZERO same_as candidates — the guard keeps them
    out of the review queue, not just out of an auto-edge."""
    entities = [{
        "entity_id": "x",
        "name": short_name,
        "aliases": [],
        "nationality": nationality,
        "designation_date": "2011-05-24",
    }]
    with patch(
        "src.etl.load_eu_sanctions.resolve_entity",
        return_value=ResolveResult(
            hint="no_match", match=None, candidates=[],
            normalised_country=nationality,
        ),
    ):
        rows, summary = load_sanctions.resolve_company_links(entities)
    assert not rows
    assert summary["matched"] == 0
    assert summary["no_match"] == 1


def test_enterprise_country_read_from_address():
    """Enterprises have no <citizenship>; their country lives on
    <address>. The loader must read it there — otherwise the
    country-gated resolver can never match a non-person sanction
    (the bug that left 0/1589 entities with a country)."""
    xml = _wrap("""
      <sanctionEntity euReferenceNumber="EU.9.9">
        <subjectType code="enterprise"/>
        <nameAlias wholeName="Some Trading Company LLC"/>
        <address countryIso2Code="RU" countryDescription="Russia"/>
        <regulation publicationDate="2022-03-01" programme="UKR"/>
      </sanctionEntity>
    """)
    out = list(parse_sanctions_xml(xml))
    assert len(out) == 1
    assert out[0]["nationality"] == "RUS"  # alpha-2 RU -> alpha-3 RUS
