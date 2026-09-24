"""Assertions that judge the C8 reprocess campaign.

A separate module because tests/data_quality/test_assertions.py is at
pylint's 1000-line limit — the same reason the supplier-withheld loader
tests live apart from test_load_ted_contracts.py.

Every assertion here is red on prod today for a defect fixed on
2026-09-23/24, and the rescan is what turns it green.
"""
import pytest

from src.data_quality.assertions.catalog import BLOCK, WARN, by_id, max_ratio


POST_RESCAN_IDS = [
    "grain.notice_belongs_to_one_contract",
    "values.company_name_is_not_a_placeholder_family",
    "values.framework_id_is_normalised",
    "coverage.framework_key_present",
    "grain.entity_quarantine_follows_canonical_notice",
    "grain.canonical_quarantine_reaches_the_entity",
    "values.company_vat_is_country_prefixed",
    "values.supplier_withheld_rate_is_sane",
]


@pytest.mark.parametrize("aid", POST_RESCAN_IDS)
def test_post_rescan_assertions_are_registered_and_cypher(aid):
    a = by_id()[aid]
    assert a.engine == "cypher"
    assert a.severity in (BLOCK, WARN)
    assert a.rationale and len(a.rationale) > 80


@pytest.mark.parametrize("aid,expected", [
    ("values.company_name_is_not_a_placeholder_family", BLOCK),
    ("values.framework_id_is_normalised", BLOCK),
    ("grain.entity_quarantine_follows_canonical_notice", BLOCK),
    ("grain.canonical_quarantine_reaches_the_entity", BLOCK),
    ("values.company_vat_is_country_prefixed", BLOCK),
    ("grain.notice_belongs_to_one_contract", BLOCK),
    # coverage of what buyers published, and a tripwire — neither is a
    # statement about our own correctness, so neither blocks a release.
    ("coverage.framework_key_present", WARN),
    ("values.supplier_withheld_rate_is_sane", WARN),
])
def test_post_rescan_severities(aid, expected):
    assert by_id()[aid].severity == expected


def test_placeholder_family_assertion_seeks_the_name_index():
    """3.6M :Company is a 90-second scan; the runner gives a statement
    90 s. This one seeks the full-text index and regexes the candidates,
    measured at 1.7 s on prod."""
    q = by_id()["values.company_name_is_not_a_placeholder_family"].query
    assert "db.index.fulltext.queryNodes('company_name_ft'" in q
    assert "MATCH (c:Company) WHERE" not in q
    # the phrase list has to mirror the lexicon families
    for phrase in ("zie bijlage", "keine angabe", "multiple suppliers",
                   "voir liste", "ver anexo", "not applicable"):
        assert phrase in q


def test_framework_id_normalisation_rejects_the_three_bad_shapes():
    q = by_id()["values.framework_id_is_normalised"].query
    assert "^0[0-9]*-[0-9]{4}$" in q     # zero-padded publication number
    assert ".*-[0-9]{2}$" in q           # UUID that kept its -NN version
    assert "^0+$" in q                   # a zero placeholder, not a key


@pytest.mark.parametrize("aid", [
    "grain.entity_quarantine_follows_canonical_notice",
    "grain.canonical_quarantine_reaches_the_entity",
])
def test_quarantine_assertions_anchor_on_the_canonical_notice(aid):
    """Both directions of the chain-rollup invariant: the entity's value
    and its quarantine marker come from the SAME notice."""
    q = by_id()[aid].query
    assert "x.is_current = true" in q
    assert ":NOTICE_OF" in q


def test_quarantine_assertions_are_two_not_one():
    """Measured on prod: each direction alone is ~4.1 s, but both as CALL
    subqueries of one statement planned badly and took 50 s. Two
    assertions also report their counts separately, which is what says
    which direction is still broken."""
    a = by_id()["grain.entity_quarantine_follows_canonical_notice"]
    b = by_id()["grain.canonical_quarantine_reaches_the_entity"]
    assert a.query != b.query
    assert "CALL () {" not in a.query and "CALL () {" not in b.query


def test_withheld_rate_tripwire_would_have_caught_the_heuristic():
    """generic.name_is_sentence withheld 1,250 of 23,814 names (5.2%)."""
    a = by_id()["values.supplier_withheld_rate_is_sane"]
    assert a.evaluate({"total": 12128, "hits": 1})[0]
    ok, obs = a.evaluate({"total": 23814, "hits": 1250})
    assert not ok and "5.25%" in obs
    # an empty window is not a failure
    assert a.evaluate({"total": 0, "hits": 0})[0]


def test_framework_key_coverage_bar():
    a = by_id()["coverage.framework_key_present"]
    assert not a.evaluate({"total": 105738, "covered": 274})[0]
    assert a.evaluate({"total": 100, "covered": 70})[0]
    assert not a.evaluate({"total": 100, "covered": 69})[0]


def test_vat_assertion_is_a_green_guard_today():
    a = by_id()["values.company_vat_is_country_prefixed"]
    ok, obs = a.evaluate({"total": 67641, "violations": 0})
    assert ok and "67641" in obs
    assert not a.evaluate({"total": 67641, "violations": 1})[0]


def test_max_ratio_helper():
    ev = max_ratio(0.01, "x")
    assert ev({"total": 0, "hits": 0})[0]
    assert ev({"total": 1000, "hits": 10})[0]
    assert not ev({"total": 1000, "hits": 11})[0]


def test_notice_home_assertion_counts_notices_not_edges():
    """The duplicate-contract driver. 2,673 notices carry two NOTICE_OF
    edges on prod; only the 123 where the shared notice is the LATEST of
    both entities surface as duplicate contracts today, so counting
    notices — not the visible duplicates — is what measures the cause."""
    a = by_id()["grain.notice_belongs_to_one_contract"]
    assert "count(r) AS k" in a.query and "k > 1" in a.query
    assert a.evaluate({"violations": 0})[0]
    ok, obs = a.evaluate({"violations": 2673})
    assert not ok and "2673" in obs
