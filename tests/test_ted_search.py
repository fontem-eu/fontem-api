"""Tests for the TED search-API client + incremental wiring."""
from eforms.filters import _AWARD_TYPES, _MODIFICATION_TYPES

from src.etl import ted_search


def test_notice_types_match_eforms_filters():
    """The search query must request exactly the notice-types the eForms
    filters accept — else we'd fetch notices the parser drops, or skip
    ones it would keep."""
    assert set(ted_search.NOTICE_TYPES) == set(_AWARD_TYPES) | set(_MODIFICATION_TYPES)


def test_day_query_is_single_day_with_types():
    q = ted_search._day_query(  # pylint: disable=protected-access
        "20260624", ("can-standard", "can-modif"))
    assert "publication-date>=20260624" in q
    assert "publication-date<=20260624" in q
    assert 'notice-type="can-standard"' in q
    assert 'notice-type="can-modif"' in q


def test_xml_url_extracts_mul_link():
    rec = {"links": {"xml": {"MUL": "https://ted.europa.eu/en/notice/1-2026/xml"}}}
    assert ted_search.xml_url(rec) == "https://ted.europa.eu/en/notice/1-2026/xml"
    assert ted_search.xml_url({}) is None
    assert ted_search.xml_url({"links": {}}) is None


def test_modifies_publication_number_first_of_list_or_scalar():
    assert ted_search.modifies_publication_number(
        {"modification-previous-notice-identifier": ["708565-2022", "x"]}
    ) == "708565-2022"
    assert ted_search.modifies_publication_number(
        {"modification-previous-notice-identifier": "65c1c820-01"}
    ) == "65c1c820-01"
    assert ted_search.modifies_publication_number({}) is None
