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

All ~10,000 wrong claims were deleted from the live graph. The loader
now delegates Lobbyist → Company linkage to gmr-consolidator's
/resolve endpoint (single source of truth for entity matching). This
test file pins:

  - the in-cypher fuzzy matcher must NOT come back
  - REPRESENTS edges only get written for confident /resolve matches
  - REPRESENTS for Tier-4 fuzzy / ambiguous results is skipped
  - the parser still populates country_iso on every Lobbyist node
    (so the resolver call gets a workable country)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import src.etl.load_eu_lobbying as load_lobbying
from src.etl._hooks import ResolveMatch, ResolveResult
from src.etl.load_eu_lobbying import (
    MERGE_REPRESENTS,
    _COUNTRY_MAP,
    _parse_entity,
    load_eu_lobbying,
)


# ─────────────────────────────────────────────────────────────────────
# The old fuzzy matcher must be gone — bringing it back means
# bypassing the /resolve service that owns the guards.
# ─────────────────────────────────────────────────────────────────────


def test_no_inline_match_company_cypher():
    """The previous inline fulltext match has been removed in favour
    of /resolve. Re-introducing it must require explicit code review."""
    assert not hasattr(load_lobbying, "MATCH_COMPANY"), (
        "MATCH_COMPANY belongs in the past — Lobbyist → Company linkage "
        "now goes through gmr-consolidator's /resolve endpoint."
    )
    assert not hasattr(load_lobbying, "CREATE_FT_INDEX"), (
        "the lobbying loader no longer needs to ensure the company_name_ft "
        "index — the resolver owns that"
    )


# ─────────────────────────────────────────────────────────────────────
# Cypher invariant on the new edge writer
# ─────────────────────────────────────────────────────────────────────


def test_merge_represents_uses_gmr_id_join():
    """The resolver returns a gmr_id; the loader joins on that, NOT on
    the lobbyist name (which is what the old broken matcher did)."""
    assert "MATCH (c:Company {gmr_id: row.gmr_id})" in MERGE_REPRESENTS
    assert "fulltext" not in MERGE_REPRESENTS.lower()


def test_merge_represents_review_flag_per_tier():
    """LEI / VAT / CIK matches are auto-reviewed=true; name+country and
    fuzzy stay reviewed=false until a human acts."""
    assert "row.tier IN ['lei','vat','cik']" in MERGE_REPRESENTS
    assert "reviewed" in MERGE_REPRESENTS


def test_merge_represents_tags_method_resolver():
    """Edges from this loader must be tagged so we can audit later."""
    assert "method" in MERGE_REPRESENTS
    assert "'resolver'" in MERGE_REPRESENTS


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


def _xml_for(country_full: str) -> object:
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
    """The /resolve call uses country_iso first, then falls back to
    country. country_iso must be populated on every Lobbyist."""
    parsed = _parse_entity(_xml_for(full))
    assert parsed["country_iso"] == iso


def test_parser_falls_back_to_full_name_for_unknown_country():
    parsed = _parse_entity(_xml_for("UNKNOWNLAND"))
    assert parsed["country_iso"] == "UNKNOWNLAND"


# ─────────────────────────────────────────────────────────────────────
# End-to-end behaviour: only confident /resolve hits become REPRESENTS.
# ─────────────────────────────────────────────────────────────────────


def _mk_xml_doc(*entities_xml: str) -> bytes:
    body = "\n".join(entities_xml)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<root xmlns="">
  <metaData><exportDate>2026-04-28</exportDate><numberOfIR>3</numberOfIR></metaData>
  <resultList>
    {body}
  </resultList>
</root>""".encode()


_ENTITY_TEMPLATE = """
<interestRepresentative>
  <identificationCode>{tr_id}</identificationCode>
  <name><originalName>{name}</originalName></name>
  <headOffice>
    <country>{country}</country>
    <city>X</city>
  </headOffice>
  <registrationCategory>Trade</registrationCategory>
</interestRepresentative>
"""


@pytest.fixture
def fake_driver():
    """Mock Neo4j driver capturing every cypher call so we can assert on
    what was written."""
    driver = MagicMock()
    session = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)
    counters = MagicMock()
    counters.relationships_created = 1
    counters.properties_set = 1
    session.run.return_value.consume.return_value = counters
    return driver, session


def test_only_confident_matches_become_represents(fake_driver):
    """Three lobbyists in the input XML — one matches confidently
    (Tier 3), one is ambiguous, one has no match. Only the confident
    one yields a row passed to MERGE_REPRESENTS."""
    driver, session = fake_driver
    xml = _mk_xml_doc(
        _ENTITY_TEMPLATE.format(tr_id="111-1", name="Confident Match Inc", country="GERMANY"),
        _ENTITY_TEMPLATE.format(tr_id="222-2", name="Ambiguous Org GmbH", country="GERMANY"),
        _ENTITY_TEMPLATE.format(tr_id="333-3", name="Nothing To Match Here", country="GERMANY"),
    )

    def _resolve(*, name, country, **_):
        if name == "Confident Match Inc":
            return ResolveResult(
                hint="matched",
                match=ResolveMatch(gmr_id="gmr-111", name=name, country="DEU",
                                    lei=None, tier="name_country", confidence=0.95),
                candidates=[], normalised_country="DEU",
            )
        if name == "Ambiguous Org GmbH":
            return ResolveResult(
                hint="ambiguous", match=None,
                candidates=[
                    ResolveMatch(gmr_id="gmr-A", name=name, country="DEU",
                                 lei=None, tier="fuzzy", confidence=0.5),
                    ResolveMatch(gmr_id="gmr-B", name=name, country="DEU",
                                 lei=None, tier="fuzzy", confidence=0.5),
                ],
                normalised_country="DEU",
            )
        return ResolveResult(hint="no_match", match=None, candidates=[],
                             normalised_country="DEU")

    with patch("src.etl.load_eu_lobbying.GraphDatabase.driver", return_value=driver), \
         patch("src.etl.load_eu_lobbying.httpx.Client") as mock_http_cls, \
         patch("src.etl.load_eu_lobbying.resolve_entity", side_effect=_resolve):
        mock_http = MagicMock()
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_http.get.return_value.content = xml
        mock_http.get.return_value.raise_for_status = MagicMock()
        mock_http_cls.return_value = mock_http

        load_eu_lobbying("bolt://x", "u", "p")

    merge_calls = [
        call for call in session.run.call_args_list
        if "MERGE (l)-[r:REPRESENTS]->(c)" in str(call.args[0]) if call.args
    ]
    assert len(merge_calls) == 1, (
        f"expected exactly one MERGE_REPRESENTS call, got {len(merge_calls)}"
    )
    rows = merge_calls[0].kwargs["rows"]
    assert len(rows) == 1
    assert rows[0]["tr_id"] == "111-1"
    assert rows[0]["gmr_id"] == "gmr-111"
    assert rows[0]["tier"] == "name_country"


def test_no_represents_when_resolver_unavailable(fake_driver):
    """Transport failure: resolve_entity returns None. Loader must
    gracefully skip — don't write a REPRESENTS edge we couldn't
    validate. Silent miss > silent corruption."""
    driver, session = fake_driver
    xml = _mk_xml_doc(
        _ENTITY_TEMPLATE.format(tr_id="111-1", name="Anything Long Enough", country="GERMANY"),
    )

    with patch("src.etl.load_eu_lobbying.GraphDatabase.driver", return_value=driver), \
         patch("src.etl.load_eu_lobbying.httpx.Client") as mock_http_cls, \
         patch("src.etl.load_eu_lobbying.resolve_entity", return_value=None):
        mock_http = MagicMock()
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_http.get.return_value.content = xml
        mock_http.get.return_value.raise_for_status = MagicMock()
        mock_http_cls.return_value = mock_http

        load_eu_lobbying("bolt://x", "u", "p")

    merge_calls = [
        call for call in session.run.call_args_list
        if "MERGE (l)-[r:REPRESENTS]->(c)" in str(call.args[0]) if call.args
    ]
    assert merge_calls == [], (
        "no REPRESENTS edges should be written when /resolve is unreachable"
    )
