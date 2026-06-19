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
    resolve_lobbyist_companies,
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
    with open(load_eu_lobbying.__file__, encoding="utf-8") as _f:
        src = _f.read()
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

def test_confident_match_sets_company_gmr_id_for_filed_by():
    """The /resolve hook returns matched/ambiguous/no_match. A confident
    match becomes the disclosure's company_gmr_id (→ working FILED_BY
    edge); ambiguous / no_match leave the disclosure standalone."""
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
        res = MagicMock()
        res.hint = "ambiguous" if name == "AmbiguousCo" else "no_match"
        res.match = None
        return res

    with patch.object(load_eu_lobbying, "resolve_entity", side_effect=fake_resolve):
        matches, summary = resolve_lobbyist_companies(entities)
    assert summary == {"confident": 1, "ambiguous": 1, "no_match": 1}
    assert matches == {"A": ("00040372-dad6-5d34-882c-8b8624b4e734",
                             "name_country", 0.92)}

    # The matched disclosure carries company_gmr_id (the sink turns that
    # into Disclosure-[:FILED_BY]->Company); the others don't.
    log, emit = _mock_log()
    emit_lobbyist_disclosures(log, entities, matches)
    by_id = {c.kwargs["payload"]["disclosure_id"]: c.kwargs["payload"]
             for c in emit.upsert.call_args_list}
    assert by_id["A"]["company_gmr_id"] == "00040372-dad6-5d34-882c-8b8624b4e734"
    assert by_id["A"]["details"]["registrant_match_tier"] == "name_country"
    assert "company_gmr_id" not in by_id["B"]
    assert "company_gmr_id" not in by_id["C"]


def test_no_matches_when_resolver_unavailable():
    entities = [{"tr_id": "A", "name": "X", "country_iso": "FR", "country": "F"}]
    with patch.object(load_eu_lobbying, "resolve_entity", return_value=None):
        matches, summary = resolve_lobbyist_companies(entities)
    assert not matches
    assert summary == {"confident": 0, "ambiguous": 0, "no_match": 0}


def test_main_accepts_argv(monkeypatch):
    """Regression: previously `def main()` rejected the argv positional
    that `_run_wrapper` always passes, so the cronjob path failed with
    `TypeError: main() takes 0 positional arguments but 1 was given`.
    Passing the empty argv list (the wrapper's normal contract) must
    not raise.
    """
    monkeypatch.setattr(
        load_eu_lobbying.EventLog, "from_env",
        classmethod(lambda cls: MagicMock()),
    )
    monkeypatch.setattr(
        load_eu_lobbying, "load_eu_lobbying",
        lambda _log: {"emitted": 0, "represents": {"confident": 0}},
    )
    load_eu_lobbying.main([])


def _entity_xml_costs(cmin: str, cmax: str) -> str:
    return f"""
<interestRepresentative>
  <identificationCode>999-99</identificationCode>
  <name><originalName>Band Test</originalName></name>
  <headOffice><country>GERMANY</country><city>Berlin</city></headOffice>
  <financialData><closedYear>
    <costs><range><min>{cmin}</min><max>{cmax}</max></range></costs>
  </closedYear></financialData>
  <registrationDate>2020-01-01T00:00:00</registrationDate>
  <lastUpdateDate>2024-06-01T00:00:00</lastUpdateDate>
</interestRepresentative>
"""


def test_parser_normalises_transposed_cost_band():
    import xml.etree.ElementTree as ET  # pylint: disable=import-outside-toplevel
    # Source has min > max (registrant transposed the bounds).
    parsed = _parse_entity(ET.fromstring(_entity_xml_costs("50000", "10000")))
    assert parsed["cost_min"] == 10000
    assert parsed["cost_max"] == 50000
    assert parsed["cost_max"] >= parsed["cost_min"]


def test_parser_leaves_well_ordered_cost_band():
    import xml.etree.ElementTree as ET  # pylint: disable=import-outside-toplevel
    parsed = _parse_entity(ET.fromstring(_entity_xml_costs("10000", "50000")))
    assert parsed["cost_min"] == 10000 and parsed["cost_max"] == 50000


def test_parser_keeps_single_open_bound_untouched():
    import xml.etree.ElementTree as ET  # pylint: disable=import-outside-toplevel
    # Only a lower bound present (max absent → 0): not reordered into a
    # misleading [0, 50000]; the 0 stays and is dropped at emit time.
    parsed = _parse_entity(ET.fromstring(_entity_xml_costs("50000", "")))
    assert parsed["cost_min"] == 50000 and parsed["cost_max"] == 0
