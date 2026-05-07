"""Regression tests for the EU Transparency Register loader.

We observed 16,161 REPRESENTS edges in production with the country
guard fully disabled (every Lobbyist had country_iso=NULL, the
cypher short-circuited the OR-chain to TRUE on every match). All
~10,000 wrong claims were deleted from the live graph. The loader
now delegates Lobbyist → Company linkage to gmr-consolidator's
/resolve endpoint (single source of truth for entity matching).

Post event-log migration this test file pins:
  - the in-cypher fuzzy matcher must NOT come back
  - the loader emits NO direct Cypher (no MERGE_LOBBYIST etc.)
  - REPRESENTS becomes UpsertRelationship events; only confident
    /resolve matches yield events; ambiguous / no_match are skipped
  - the parser still populates country_iso on every Lobbyist
    (so the resolver call gets a workable country)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.etl import load_eu_lobbying
from src.etl.load_eu_lobbying import (
    _COUNTRY_MAP,
    _parse_entity,
    emit_lobbyist_disclosures,
    emit_represents_relationships,
)


# ── helpers ─────────────────────────────────────────────────

def _mock_log():
    log = MagicMock()
    emit = MagicMock()
    log.batch.return_value.__enter__ = MagicMock(return_value=emit)
    log.batch.return_value.__exit__ = MagicMock(return_value=False)
    return log, emit


# ── invariants ──────────────────────────────────────────────

def test_no_inline_match_company_cypher():
    """The deleted in-cypher fulltext fan-out is the misattribution
    risk we got bitten by in production. Re-introducing it would
    have to add a NEW symbol — fail loudly if any of the old shapes
    sneak back."""
    src = open(load_eu_lobbying.__file__, encoding="utf-8").read()
    assert "FULLTEXT" not in src.upper()
    assert "MERGE_LOBBYIST" not in src
    assert "MERGE_REPRESENTS" not in src
    assert "MERGE_INTERESTS" not in src


# ── country normalization ───────────────────────────────────

@pytest.mark.parametrize("full,iso", [
    ("UNITED STATES", "US"), ("GERMANY", "DEU"), ("FRANCE", "FRA"),
    ("PORTUGAL", "PRT"), ("CZECH REPUBLIC", "CZE"),
])
def test_country_map_covers_top_eu_jurisdictions(full, iso):
    assert _COUNTRY_MAP[full] == iso


# ── parser ──────────────────────────────────────────────────

def _entity_xml(country: str = "GERMANY") -> str:
    return f"""
<interestRepresentative>
  <identificationCode>123456789-12</identificationCode>
  <name><originalName>Test Lobby AG</originalName></name>
  <acronym>TL</acronym>
  <headOffice><country>{country}</country><city>Berlin</city></headOffice>
  <registrationCategory>Companies</registrationCategory>
  <entityForm>AG</entityForm>
  <webSiteURL>https://test-lobby.example</webSiteURL>
  <goals>Promote test things</goals>
  <EPAccreditedNumber>3</EPAccreditedNumber>
  <members><membersFTE>4.5</membersFTE></members>
  <financialData>
    <closedYear>
      <costs><range><min>100000</min><max>200000</max></range></costs>
    </closedYear>
  </financialData>
  <registrationDate>2020-01-01T00:00:00</registrationDate>
  <lastUpdateDate>2024-06-01T00:00:00</lastUpdateDate>
  <interests>
    <interest><name>Climate</name></interest>
    <interest><name>Energy</name></interest>
  </interests>
</interestRepresentative>
"""


def test_parser_populates_country_iso():
    import xml.etree.ElementTree as ET  # pylint: disable=import-outside-toplevel
    parsed = _parse_entity(ET.fromstring(_entity_xml("GERMANY")))
    assert parsed["country"] == "GERMANY"
    assert parsed["country_iso"] == "DEU"
    assert parsed["interests"] == ["Climate", "Energy"]
    assert parsed["cost_min"] == 100000
    assert parsed["cost_max"] == 200000


def test_parser_falls_back_to_full_name_for_unknown_country():
    import xml.etree.ElementTree as ET  # pylint: disable=import-outside-toplevel
    parsed = _parse_entity(ET.fromstring(_entity_xml("ATLANTIS")))
    # Unknown country falls back to the upper-cased full name.
    assert parsed["country_iso"] == "ATLANTIS"


# ── disclosure emit ─────────────────────────────────────────

def test_emit_disclosure_omits_company_gmr_id():
    """The Lobbyist registers itself; the Disclosure has no parent
    Company. Schema relaxation in gmr-event-schemas allows this."""
    log, emit = _mock_log()
    entities = [{
        "tr_id": "111-22", "name": "Foo Lobby",
        "country": "FRANCE", "country_iso": "FRA",
        "city": "Paris", "category": "Companies",
        "entity_form": "SA", "website": "https://foo",
        "goals": "g", "ep_passes": 2, "members_fte": 1.0,
        "cost_min": 0, "cost_max": 50000,
        "registration_date": "2020-01-01", "last_updated": "2024-01-01",
        "acronym": "FL",
        "interests": ["topic1"],
    }]
    n = emit_lobbyist_disclosures(log, entities)
    assert n == 1
    payload = emit.upsert.call_args.kwargs["payload"]
    assert payload["system"] == "eu-lobbying"
    assert payload["disclosure_id"] == "111-22"
    assert "company_gmr_id" not in payload
    assert payload["details"]["country_iso"] == "FRA"
    assert payload["details"]["interests"] == ["topic1"]


def test_emit_disclosure_skips_zero_cost_fields():
    """cost_min=0 / members_fte=0.0 are 'unset' artefacts of the
    parser — they don't belong in details."""
    log, emit = _mock_log()
    entities = [{
        "tr_id": "x", "name": "Y", "country": "X", "country_iso": "X",
        "city": "", "category": "", "entity_form": "", "website": "",
        "goals": "", "ep_passes": 0, "members_fte": 0.0,
        "cost_min": 0, "cost_max": 0, "interests": [],
        "acronym": "", "registration_date": "", "last_updated": "",
    }]
    emit_lobbyist_disclosures(log, entities)
    payload = emit.upsert.call_args.kwargs["payload"]
    details = payload.get("details") or {}
    assert "cost_min" not in details
    assert "ep_passes" not in details
    assert "members_fte" not in details


# ── relationship emit ───────────────────────────────────────

def test_only_confident_matches_become_represents():
    """The /resolve hook returns matched/ambiguous/no_match; only
    matched should yield a UpsertRelationship event."""
    log, emit = _mock_log()
    entities = [
        {"tr_id": "A", "name": "MatchedCo", "country_iso": "DEU",
         "country": "GERMANY"},
        {"tr_id": "B", "name": "AmbiguousCo", "country_iso": "FRA",
         "country": "FRANCE"},
        {"tr_id": "C", "name": "NoMatchCo", "country_iso": "ESP",
         "country": "SPAIN"},
    ]

    def fake_resolve(*, entity_type, name, country):  # pylint: disable=unused-argument
        if name == "MatchedCo":
            res = MagicMock()
            res.hint = "matched"
            res.match.gmr_id = "00040372-dad6-5d34-882c-8b8624b4e734"
            res.match.tier = "name_country"
            res.match.confidence = 0.92
            return res
        if name == "AmbiguousCo":
            res = MagicMock()
            res.hint = "ambiguous"
            res.match = None
            return res
        res = MagicMock()
        res.hint = "no_match"
        res.match = None
        return res

    with patch.object(load_eu_lobbying, "resolve_entity", side_effect=fake_resolve):
        summary = emit_represents_relationships(log, entities)
    assert summary == {"confident": 1, "ambiguous": 1, "no_match": 1}
    assert emit.upsert.call_count == 1
    payload = emit.upsert.call_args.kwargs["payload"]
    assert payload["predicate"] == "represents"
    assert "EuLobbyingDisclosure/A" in payload["src_iri"]
    assert "Company/00040372" in payload["dst_iri"]
    assert payload["properties"]["tier"] == "name_country"


def test_no_represents_when_resolver_unavailable():
    log, emit = _mock_log()
    entities = [{"tr_id": "A", "name": "X", "country_iso": "FR", "country": "F"}]
    with patch.object(load_eu_lobbying, "resolve_entity", return_value=None):
        summary = emit_represents_relationships(log, entities)
    assert summary == {"confident": 0, "ambiguous": 0, "no_match": 0}
    emit.upsert.assert_not_called()
