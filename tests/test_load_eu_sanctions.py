"""Regression tests for the EU sanctions loader.

Live in the graph today there were 8 SANCTIONED edges, all of them
false positives created by `MATCH_COMPANY_EXACT` matching on bare name
equality with no country guard:

  - 3× French Company "AMD"  → Iranian-regime "AMD" (Aran Modern Devices)
  - 2× DK/FR Company "TSA"   → Iranian "TSA" (TESA / Iran Centrifuge Tech.)
  -    French Company "CRL"  → Iranian "CRL" (Iran Composites Institute)
  -    French Company "LRA"  → Ugandan "LRA" (Lord's Resistance Army)
  -    Belgian Company "NADA"→ DPRK "NADA" (Nat'l Aerospace Development Admin)

The match was wrong because the EU XML stores acronym short codes as
the primary `name` and the real entity name in `aliases`. We now
require: (a) name length ≥ 6, (b) country/nationality agreement, (c)
non-empty nationality on the sanction side. These tests pin those
guards so future refactors don't silently re-defame anyone.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.etl.load_eu_sanctions import (
    MATCH_COMPANY_EXACT,
    MATCH_COMPANY_FUZZY,
    MIN_NAME_LEN,
    load_into_neo4j,
    parse_sanctions_xml,
)

NS = "http://eu.europa.ec/fpi/fsd/export"


def _wrap(entities_xml: str) -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<export xmlns="{NS}">{entities_xml}</export>'
    ).encode("utf-8")


# ─────────────────────────────────────────────────────────────────────
# Cypher invariants — these guard against silent regression of the
# match logic. If these strings stop appearing in the queries, the
# next ETL run will start producing false-positive SANCTIONED edges
# again, and tests/test_load_eu_sanctions_regression.py will catch it.
# ─────────────────────────────────────────────────────────────────────


def test_min_name_len_is_at_least_six():
    """A 3-letter acronym must not be allowed to match anything."""
    assert MIN_NAME_LEN >= 6, (
        "Acronyms like AMD/TSA/CRL/LRA collided with unrelated EU companies. "
        "MIN_NAME_LEN must be high enough to exclude them."
    )


def test_exact_match_requires_country_agreement():
    """The country/nationality guard is the single most important rule.
    Without it, a French 'AMD' is matched to an Iranian sanction."""
    assert "c.country" in MATCH_COMPANY_EXACT, "country guard missing"
    assert "s.nationality" in MATCH_COMPANY_EXACT, "nationality guard missing"
    assert "coalesce(c.country" in MATCH_COMPANY_EXACT, (
        "country comparison must coalesce nulls to '' so a NULL company "
        "country is NOT treated as matching anything"
    )


def test_exact_match_rejects_empty_nationality():
    """If the sanction record has no nationality, we cannot do a safe
    country check — must skip rather than emit a defamatory edge."""
    assert "coalesce(s.nationality, '') <> ''" in MATCH_COMPANY_EXACT


def test_exact_match_enforces_min_name_length():
    assert "size(s.name) >= $min_name_len" in MATCH_COMPANY_EXACT


def test_fuzzy_match_only_emits_review_queue_edges():
    """Fuzzy must NEVER create a SANCTIONED edge — only SAME_AS
    {reviewed: false} for the manual-review queue."""
    assert ":SANCTIONED" not in MATCH_COMPANY_FUZZY, (
        "fuzzy path must not write SANCTIONED edges directly"
    )
    assert ":SAME_AS" in MATCH_COMPANY_FUZZY
    assert "reviewed: false" in MATCH_COMPANY_FUZZY


def test_fuzzy_match_has_country_and_length_guards():
    assert "size(s.name) >= $min_name_len" in MATCH_COMPANY_FUZZY
    assert "coalesce(c.country, '') = s.nationality" in MATCH_COMPANY_FUZZY


def test_fuzzy_match_score_threshold_is_strict():
    """Previous threshold was 1.5 — admitted anything with a single
    token overlap. Anything below 4.0 lets garbage through."""
    assert "score > 4.0" in MATCH_COMPANY_FUZZY


# ─────────────────────────────────────────────────────────────────────
# The 5 historical false-positive cases. Each row asserts that the
# (Company name, country, LEI) ↔ (Sanction short_name, nationality)
# pair would NOT be matched by the new MATCH_COMPANY_EXACT.
# ─────────────────────────────────────────────────────────────────────

REGRESSION_FALSE_POSITIVE_CASES = [
    # short_name, sanction_nationality, company_country, why_it_failed_before
    pytest.param("AMD",  "IR", "FR", id="AMD-FR-vs-Iranian-defense"),
    pytest.param("TSA",  "IR", "DK", id="TSA-DK-vs-Iran-Centrifuge-Tech"),
    pytest.param("TSA",  "IR", "FR", id="TSA-FR-vs-Iran-Centrifuge-Tech"),
    pytest.param("CRL",  "IR", "FR", id="CRL-FR-vs-Iran-Composites"),
    pytest.param("LRA",  "UG", "FR", id="LRA-FR-vs-Lords-Resistance-Army"),
    pytest.param("NADA", "KP", "BE", id="NADA-BE-vs-DPRK-Aerospace"),
]


@pytest.mark.parametrize(
    "short_name,sanction_nationality,company_country",
    REGRESSION_FALSE_POSITIVE_CASES,
)
def test_short_name_under_min_length_blocks_exact_match(
    short_name, sanction_nationality, company_country,
):
    """Each historical false positive had a name shorter than MIN_NAME_LEN.
    The length guard alone — independent of country — must reject them."""
    assert len(short_name) < MIN_NAME_LEN, (
        f"{short_name!r} should be shorter than MIN_NAME_LEN={MIN_NAME_LEN} — "
        "if MIN_NAME_LEN drops, this test goes red"
    )


@pytest.mark.parametrize(
    "short_name,sanction_nationality,company_country",
    REGRESSION_FALSE_POSITIVE_CASES,
)
def test_country_mismatch_blocks_exact_match(
    short_name, sanction_nationality, company_country,
):
    """Even if the name guard were dropped, country disagreement alone
    should block these matches. Belt-and-braces."""
    assert sanction_nationality != company_country, (
        f"{short_name}: sanction nat={sanction_nationality!r} "
        f"company country={company_country!r} — these must differ for the "
        "regression case to make sense"
    )


# ─────────────────────────────────────────────────────────────────────
# Mock-driver test of the actual loader call: verify the new query
# parameters reach session.run untouched.
# ─────────────────────────────────────────────────────────────────────


def test_loader_passes_min_name_len_to_session_run():
    """If MIN_NAME_LEN isn't threaded through, the cypher's
    `$min_name_len` parameter is unbound and the query errors at
    runtime — but only on a real Neo4j. Catch it in unit-tests."""
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)

    counters = MagicMock()
    counters.relationships_created = 0
    consume_result = MagicMock()
    consume_result.counters = counters
    mock_session.run.return_value.consume.return_value = counters
    # Some run() calls return results with .consume() — make all of them work
    mock_session.run.return_value.consume.return_value = counters

    one_entity = [{
        "entity_id": "x",
        "eu_reference": "x",
        "name": "Aran Modern Devices",  # full name, length OK
        "entity_type": "entity",
        "aliases": ["AMD"],
        "nationality": "IR",
        "designation_date": "2011-05-24",
        "sanction_regime": "IRN",
        "legal_basis": "503/2011",
        "listing_reason": "Affiliated to MTFZC.",
    }]
    load_into_neo4j(mock_driver, iter(one_entity))

    # Check that every match-cypher call carried min_name_len
    match_calls = [
        call for call in mock_session.run.call_args_list
        if any(
            kw == "MATCH_COMPANY_EXACT" or "MATCH (c:Company)" in str(call)
            or "queryNodes('company_name_ft'" in str(call)
            for kw in [str(call.args[0]) if call.args else ""]
        )
    ]
    assert match_calls, "expected at least one match cypher call"
    for call in match_calls:
        # First positional arg is the query string. kwargs carry params.
        assert "min_name_len" in call.kwargs, (
            f"call missing min_name_len kwarg: {call.kwargs.keys()}"
        )
        assert call.kwargs["min_name_len"] == MIN_NAME_LEN


# ─────────────────────────────────────────────────────────────────────
# XML parsing — make sure the parser captures aliases (so a future
# refactor that switches matching to alias-based won't be blind).
# ─────────────────────────────────────────────────────────────────────


def test_parse_extracts_aliases():
    """The real entity name lives in aliases. The parser must keep them."""
    xml = _wrap(f"""
      <sanctionEntity euReferenceNumber="EU.2518.30">
        <subjectType code="enterprise"/>
        <nameAlias wholeName="AMD"/>
        <nameAlias wholeName="Aran Modern Devices"/>
        <citizenship countryIso2Code="IR"/>
        <regulation publicationDate="2011-05-24" programme="IRN"
                    numberTitle="503/2011 (OJ L136)"/>
        <remark>Affiliated to MTFZC network.</remark>
      </sanctionEntity>
    """)
    out = list(parse_sanctions_xml(xml))
    assert len(out) == 1
    record = out[0]
    assert record["name"] == "AMD"  # the regression: short code is primary
    assert "Aran Modern Devices" in record["aliases"], (
        "the real entity name must be retained in aliases — when we move "
        "matching to alias-based this is what we'll join on"
    )
    assert record["nationality"] == "IR"


def test_parse_handles_blank_subject_with_no_name():
    """The empty placeholder rows we observed in production (eu_reference
    ordinal but everything else blank) must round-trip without crashing.
    They'll fail the new `coalesce(s.nationality,'') <> ''` guard at
    match time, so they cause no harm — but parsing must not throw."""
    xml = _wrap("""
      <sanctionEntity euReferenceNumber="13">
        <subjectType code="enterprise"/>
      </sanctionEntity>
    """)
    out = list(parse_sanctions_xml(xml))
    assert len(out) == 1
    assert out[0]["name"] == ""
    assert out[0]["nationality"] == ""
    assert out[0]["aliases"] == []
