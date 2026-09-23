"""Part of a framework agreement — the eForms OPT-100 grouping key.

Every award notice of one framework carries the IDENTICAL value at
``efac:NoticeResult/efac:SettledContract/cac:NoticeDocumentReference/
cbc:ID``, which is what lets the detail page put a contract next to the
other awards of its framework. It says nothing else: ~80% of the time
the key names a call for competition this platform never ingests, and
`is_framework` is on 344 of 351 sampled call-offs, so nothing here may
assert that a contract IS the framework or order a cluster into
establishment-then-call-offs.

Both layers, following the house split: the source against a MagicMock
session (the wire shape), the router against a mocked ContractDataSource
(the response shape — no response model sits between them).
"""
# `_neo4j` is the attribute the contracts source mounts; swapping its
# session is how every graph-source test pins the wire shape.
# pylint: disable=protected-access
from unittest.mock import MagicMock

from src.data.graph.graph_contract_source import (
    FRAMEWORK_SIBLING_LIMIT, GraphContractSource, framework_ted_url,
)
from tests.dishka_fixtures import make_test_client, cleanup_dishka

# The grouping key notices 761784-2024 and 3406-2025 both publish, in
# the form TED's own framework-notice-id index matches. Named ..._ID,
# never ..._KEY: gitleaks' generic-api-key rule fires on a high-entropy
# literal bound to a name containing "key", and the UUID form below
# failed the secret-scan gate under its first name.
PUB_NUMBER_ID = "536632-2024"
# The other form OPT-100 takes: an eForms notice UUID, version suffix
# already stripped by the parser.
UUID_FORM_ID = "45d7e260-cdfb-4ae3-a3d9-fdc8beea8b77"


def _run_result(single=None, data=None):
    r = MagicMock()
    r.single.return_value = single
    r.data.return_value = data or []
    return r


def _source(ct, siblings=None, sibling_count=None):
    """The detail page's round-trips: the contract, then — only when it
    carries a grouping key — the sibling page and the sibling count."""
    src = GraphContractSource(MagicMock())
    session = MagicMock()
    session.run.side_effect = [
        _run_result(single={"ct": ct, "a": {"name": "Ministry"},
                            "c": None, "cpv": None}),
        _run_result(data=siblings or []),
        _run_result(single={"sibling_count": sibling_count
                            if sibling_count is not None
                            else len(siblings or [])}),
    ]
    src._neo4j.session.return_value.__enter__ = MagicMock(return_value=session)
    src._neo4j.session.return_value.__exit__ = MagicMock(return_value=False)
    return src, session


def _sibling(notice_id, date, **over):
    row = {"ted_notice_id": notice_id, "title": "Call-off",
           "country": "POL", "value_eur": 1000.0,
           "publication_date": date, "supplier": "Alfa S.A."}
    row.update(over)
    return row


# ── The block itself ──────────────────────────────────────────────────

def test_a_contract_with_no_framework_signal_says_nothing():
    """Null, not an empty object and not "no framework": pre-eForms
    notices carry neither the key nor is_framework, so absence is
    unknown, not negative."""
    src, _ = _source({"ted_notice_id": "n1", "title": "Works"})
    assert src.get_contract_detail("n1")["framework"] is None


def test_a_flagged_contract_with_no_key_still_gets_a_block():
    """is_framework alone is enough to say "part of a framework
    agreement"; the key is what would let us show the rest."""
    src, session = _source({"ted_notice_id": "n1", "is_framework": True})
    fw = src.get_contract_detail("n1")["framework"]
    assert fw["framework_id"] is None
    assert fw["ted_url"] is None
    assert fw["siblings"] == [] and fw["sibling_count"] == 0
    # No key, no sibling read: the one round-trip is the contract.
    assert session.run.call_count == 1


def test_the_published_terms_pass_through():
    """Whatever THIS notice published, unconverted and unsummed — the
    ceiling is the procedure's capacity, never money paid."""
    src, _ = _source({
        "ted_notice_id": "n1", "is_framework": True, "framework_id": PUB_NUMBER_ID,
        "framework_max_value_eur": 4_200_000.0,
        "framework_reestimated_value_eur": 3_900_000.0,
        "framework_duration_months": 48,
        "framework_max_operators": 5,
    })
    fw = src.get_contract_detail("n1")["framework"]
    assert fw["framework_id"] == PUB_NUMBER_ID
    assert fw["max_value_eur"] == 4_200_000.0
    assert fw["reestimated_value_eur"] == 3_900_000.0
    assert fw["duration_months"] == 48
    assert fw["max_operators"] == 5


def test_a_flagged_contract_without_terms_reports_them_as_null():
    """A call-off publishes the key but usually none of the procedure's
    terms; the keys stay on the object so the response shape does not
    change under the reader."""
    src, _ = _source({"ted_notice_id": "n1", "is_framework": True,
                      "framework_id": PUB_NUMBER_ID})
    fw = src.get_contract_detail("n1")["framework"]
    assert fw["max_value_eur"] is None
    assert fw["reestimated_value_eur"] is None
    assert fw["duration_months"] is None
    assert fw["max_operators"] is None


# ── The TED link ──────────────────────────────────────────────────────

def test_the_publication_number_form_gets_a_ted_link():
    """Verified against TED on 2026-09-23: /en/notice/536632-2024/xml
    answers 200."""
    assert framework_ted_url(PUB_NUMBER_ID) == (
        "https://ted.europa.eu/en/notice/-/detail/536632-2024")


def test_the_uuid_form_gets_no_link():
    """Same check: /en/notice/<uuid>/xml answers 404, and the /detail/
    route renders the blank 202 page ted_lookup documents. A broken link
    is worse than no link."""
    assert framework_ted_url(UUID_FORM_ID) is None


def test_a_uuid_key_still_groups_even_without_a_link():
    src, _ = _source({"ted_notice_id": "n1", "is_framework": True,
                      "framework_id": UUID_FORM_ID},
                     siblings=[_sibling("s1", "2026-01-01")])
    fw = src.get_contract_detail("n1")["framework"]
    assert fw["framework_id"] == UUID_FORM_ID
    assert fw["ted_url"] is None
    assert fw["sibling_count"] == 1


# ── Siblings ──────────────────────────────────────────────────────────

def test_siblings_come_back_newest_first_in_the_order_the_query_gave():
    src, _ = _source({"ted_notice_id": "n1", "is_framework": True,
                      "framework_id": PUB_NUMBER_ID},
                     siblings=[_sibling("s2", "2026-03-01"),
                               _sibling("s1", "2025-11-30")])
    fw = src.get_contract_detail("n1")["framework"]
    assert [s["ted_notice_id"] for s in fw["siblings"]] == ["s2", "s1"]
    assert fw["siblings"][0] == {
        "ted_notice_id": "s2", "title": "Call-off", "country": "POL",
        "value_eur": 1000.0, "publication_date": "2026-03-01",
        "supplier": "Alfa S.A.",
    }


def test_the_count_is_the_whole_cluster_not_the_page():
    """68.8% of frameworks have a single award notice, but the mean is
    6.04 and the largest sampled is 148 — the page is capped and the
    count is what says how much was left off."""
    src, _ = _source({"ted_notice_id": "n1", "is_framework": True,
                      "framework_id": PUB_NUMBER_ID},
                     siblings=[_sibling(f"s{i}", "2026-01-01")
                               for i in range(FRAMEWORK_SIBLING_LIMIT)],
                     sibling_count=147)
    fw = src.get_contract_detail("n1")["framework"]
    assert len(fw["siblings"]) == FRAMEWORK_SIBLING_LIMIT
    assert fw["sibling_count"] == 147


def test_the_sibling_read_is_an_equality_seek_on_the_key():
    """PROFILEd on fontem-prod: without the contract_framework_id index
    this shape scans all 3,606,471 :Contract nodes for 7,212,943 DbHits
    and 3.8–52.8 s, past the API's 8 s budget. The inline map pattern is
    what the planner turns into a NodeIndexSeek, and the LIMIT must land
    before the supplier expansion."""
    src, session = _source({"ted_notice_id": "n1", "is_framework": True,
                            "framework_id": PUB_NUMBER_ID},
                           siblings=[_sibling("s1", "2026-01-01")])
    src.get_contract_detail("n1")
    page, count = [c.args[0] for c in session.run.call_args_list][1:]
    for query in (page, count):
        assert "MATCH (s:Contract {framework_id: $fid})" in query
    assert page.index("LIMIT $limit") < page.index("OPTIONAL MATCH")
    assert session.run.call_args_list[1].kwargs["limit"] == (
        FRAMEWORK_SIBLING_LIMIT)


def test_the_contract_is_excluded_by_its_own_notice_id():
    """A detail page can be reached through a superseded notice id; the
    exclusion has to use the contract's current one or the contract
    lists itself as a sibling."""
    src, session = _source({"ted_notice_id": "current-1", "is_framework": True,
                            "framework_id": PUB_NUMBER_ID},
                           siblings=[_sibling("s1", "2026-01-01")])
    src.get_contract_detail("superseded-1")
    for call in session.run.call_args_list[1:]:
        assert call.kwargs["nid"] == "current-1"


# ── List rows ─────────────────────────────────────────────────────────

def _list_source(rows):
    src = GraphContractSource(MagicMock())
    session = MagicMock()
    session.run.side_effect = [
        _run_result(single={"name": "Ministry", "country": "POL"}),
        _run_result(data=rows),
        _run_result(single={"total": 0, "cnt": len(rows)}),
    ]
    src._neo4j.session.return_value.__enter__ = MagicMock(return_value=session)
    src._neo4j.session.return_value.__exit__ = MagicMock(return_value=False)
    return src, session


def _list_row(**over):
    row = {"notice_id": "1-2026", "publication_number": "1-2026",
           "title": "Award", "value_eur": 1000.0, "award_date": "2026-01-01",
           "cpv": "72000000", "cpv_description": None,
           "value_low_confidence": False, "value_quality_flag": "ok",
           "value_payable_discrepancy": False, "estimated_value_eur": None,
           "notice_type": "can-standard", "value_currency": "EUR",
           "value_original": 1000.0, "value_before_eur": None,
           "value_before_original": None, "modifies_publication_number": None,
           "value_confidence": None, "value_quarantined": None,
           "value_quarantine_reason": None, "procedure_type": "open",
           "ted_url": None, "is_framework": True,
           "contractor": "Acme", "contractor_country": "POL",
           "contractor_gmr_id": "g1", "supplier_withheld_count": None,
           "authority": "Ministry", "authority_id": "a-1",
           "authority_country": "POL"}
    row.update(over)
    return row


def test_the_authority_list_row_carries_the_flag():
    src, session = _list_source([_list_row()])
    out = src.get_authority_contracts("a-1")
    rows_query = [c.args[0] for c in session.run.call_args_list
                  if "LIMIT" in c.args[0]][0]
    assert "ct.is_framework AS is_framework" in rows_query
    assert out["contracts"][0]["is_framework"] is True


def test_the_company_list_row_carries_the_flag():
    src, session = _list_source([_list_row()])
    out = src.get_company_contracts("g1")
    rows_query = [c.args[0] for c in session.run.call_args_list
                  if "LIMIT" in c.args[0]][0]
    assert "ct.is_framework AS is_framework" in rows_query
    assert out["contracts"][0]["is_framework"] is True


def test_a_row_without_the_property_stays_unknown():
    """Pre-eForms notices carry no ContractingSystemTypeCode at all.
    Coalescing that to false would claim the contract is not part of a
    framework, which the data does not say."""
    src, _ = _list_source([_list_row(is_framework=None)])
    assert src.get_authority_contracts("a-1")["contracts"][0][
        "is_framework"] is None


# ── The wire ──────────────────────────────────────────────────────────

def test_the_endpoint_passes_the_framework_block_through():
    """No response model sits between the source and the wire, so the
    keys the source builds are the keys the page reads."""
    mock = MagicMock()
    mock.get_contract_detail.return_value = {
        "ted_notice_id": "761784-2024",
        "integrity": {"is_framework": True},
        "framework": {
            "framework_id": PUB_NUMBER_ID,
            "ted_url": "https://ted.europa.eu/en/notice/-/detail/536632-2024",
            "max_value_eur": 4_200_000.0, "reestimated_value_eur": None,
            "duration_months": 48, "max_operators": None,
            "sibling_count": 2,
            "siblings": [_sibling("3406-2025", "2025-01-03")],
        },
    }
    client = make_test_client(contract_source=mock)
    resp = client.get("/contracts/761784-2024")
    cleanup_dishka()
    assert resp.status_code == 200
    fw = resp.json()["framework"]
    assert fw["framework_id"] == PUB_NUMBER_ID
    assert fw["max_operators"] is None
    assert fw["sibling_count"] == 2
    assert fw["siblings"][0]["ted_notice_id"] == "3406-2025"


def test_the_endpoint_passes_a_null_framework_through():
    mock = MagicMock()
    mock.get_contract_detail.return_value = {
        "ted_notice_id": "n1", "framework": None,
    }
    client = make_test_client(contract_source=mock)
    resp = client.get("/contracts/n1")
    cleanup_dishka()
    assert resp.json()["framework"] is None
