"""Supplier not disclosed in the notice (data-backlog Part 5, C2).

The cleaning stage refuses to mint a company when the supplier name
field holds a sentence, a URL or a placeholder; the sink keeps what was
published as two parallel lists on the :Contract, and such a contract
has no AWARDED_TO edge. Two readers have to cope: the detail page,
which used to show an empty contractor cell, and the authority list,
whose totals counted these contracts while its rows never listed them.
"""
# `_neo4j` is the attribute the contracts source mounts; swapping its
# session is how every graph-source test pins the wire shape.
# pylint: disable=protected-access
from unittest.mock import MagicMock

from src.data.graph.graph_contract_source import GraphContractSource
from tests.dishka_fixtures import make_test_client, cleanup_dishka

DECREE = ("Gara aggiudicata come da determina n. 543 del 2013 pubblicata "
          "sul sito www.csc.sanita.fvg.it")


def _run_result(single=None, data=None):
    r = MagicMock()
    r.single.return_value = single
    r.data.return_value = data or []
    return r


def _detail_source(row):
    src = GraphContractSource(MagicMock())
    session = MagicMock()
    session.run.return_value.single.return_value = row
    src._neo4j.session.return_value.__enter__ = MagicMock(return_value=session)
    src._neo4j.session.return_value.__exit__ = MagicMock(return_value=False)
    return src


def _authority_source(rows, total=None):
    """The authority list's three round-trips: the buyer, the page of
    rows, the totals over every contract it awarded."""
    src = GraphContractSource(MagicMock())
    session = MagicMock()
    session.run.side_effect = [
        _run_result(single={"name": "Ministry", "country": "HUN"}),
        _run_result(data=rows),
        _run_result(single=total or {"total": 0, "cnt": len(rows)}),
    ]
    src._neo4j.session.return_value.__enter__ = MagicMock(return_value=session)
    src._neo4j.session.return_value.__exit__ = MagicMock(return_value=False)
    return src, session


def _authority_row(**overrides):
    row = {
        "notice_id": "1-2026", "publication_number": "1-2026", "title": "Award",
        "value_eur": 1000.0, "award_date": "2026-01-01", "cpv": "72000000",
        "value_low_confidence": False, "value_quality_flag": "ok",
        "value_payable_discrepancy": False, "estimated_value_eur": None,
        "procedure_type": "open", "ted_url": None, "cpv_description": None,
        "notice_type": "can-standard", "value_currency": "EUR",
        "value_original": 1000.0, "value_before_eur": None,
        "value_before_original": None, "modifies_publication_number": None,
        "contractor": "Acme", "contractor_country": "HUN",
        "contractor_gmr_id": "g1", "supplier_withheld_count": None,
    }
    row.update(overrides)
    return row


# ── Detail page ───────────────────────────────────────────────────────

def test_detail_names_the_withheld_suppliers():
    """184512-2013: the sentence sat inside <OFFICIALNAME>, so there is
    no supplier to link, only the published text and the rule that
    withheld it. Both reach the page, zipped in payload order."""
    src = _detail_source({
        "ct": {"ted_notice_id": "184512-2013", "title": "Servizi",
               "suppliers_withheld_names": [DECREE, "www.example.it"],
               "suppliers_withheld_reasons": [
                   "it.notice_text_in_supplier_name",
                   "generic.name_contains_url"],
               "suppliers_withheld_count": 2,
               "cleaning_rules": ["it.notice_text_in_supplier_name",
                                  "generic.name_contains_url"],
               "value_quarantine_reason": "ambiguous_scale"},
        "a": {"name": "ASS 3 Alto Friuli"},
        "c": None,
        "cpv": None,
    })
    out = src.get_contract_detail("184512-2013")
    assert out["contractor"] is None
    assert out["supplier_not_disclosed"] is True
    assert out["suppliers_withheld"] == [
        {"name_raw": DECREE, "reason": "it.notice_text_in_supplier_name"},
        {"name_raw": "www.example.it", "reason": "generic.name_contains_url"},
    ]
    assert out["cleaning_rules"] == ["it.notice_text_in_supplier_name",
                                     "generic.name_contains_url"]
    assert out["value_quarantine_reason"] == "ambiguous_scale"


def test_detail_without_cleaning_marks_withholds_nothing():
    """A contract written before the cleaning stage carries none of the
    lists. That is not "not disclosed" - it is simply an award with no
    supplier on record - so the flag stays off and the lists are empty
    rather than absent, which keeps the response shape stable."""
    src = _detail_source({
        "ct": {"ted_notice_id": "n1", "title": "Works"},
        "a": {"name": "Exploateringskontoret"},
        "c": None,
        "cpv": None,
    })
    out = src.get_contract_detail("n1")
    assert out["contractor"] is None
    assert out["supplier_not_disclosed"] is False
    assert out["suppliers_withheld"] == []
    assert out["cleaning_rules"] == []
    assert out["value_quarantine_reason"] is None


def test_detail_a_named_awardee_is_disclosed():
    """A multi-supplier award where one operator was named and another
    withheld: the named one is the contractor, so the notice did
    disclose a supplier. The withheld one is still listed."""
    src = _detail_source({
        "ct": {"ted_notice_id": "n1",
               "suppliers_withheld_names": ["diversi"],
               "suppliers_withheld_reasons": ["generic.name_is_placeholder"]},
        "a": {"name": "Comune"},
        "c": {"gmr_id": "g1", "name": "Acme S.p.A.", "country": "ITA"},
        "cpv": None,
    })
    out = src.get_contract_detail("n1")
    assert out["contractor"] == {"gmr_id": "g1", "name": "Acme S.p.A.",
                                 "country": "ITA"}
    assert out["supplier_not_disclosed"] is False
    assert out["suppliers_withheld"] == [
        {"name_raw": "diversi", "reason": "generic.name_is_placeholder"}]


def test_detail_tolerates_misaligned_withheld_lists():
    """The sink writes '' for a missing reason and drops nameless items,
    but the lists are two properties and nothing in the graph enforces
    their lengths. A short reasons list reads as unknown reasons, never
    as a shifted one; a name that is empty has nothing to show."""
    src = _detail_source({
        "ct": {"ted_notice_id": "n1",
               "suppliers_withheld_names": [DECREE, "", "n/a"],
               "suppliers_withheld_reasons": [""]},
        "a": {"name": "ASS"},
        "c": None,
        "cpv": None,
    })
    out = src.get_contract_detail("n1")
    assert out["suppliers_withheld"] == [
        {"name_raw": DECREE, "reason": None},
        {"name_raw": "n/a", "reason": None},
    ]
    assert out["supplier_not_disclosed"] is True


def test_detail_endpoint_passes_the_withheld_shape_through():
    """No response model sits between the source and the wire: the
    fields the source adds are the fields the page reads."""
    mock = MagicMock()
    mock.get_contract_detail.return_value = {
        "ted_notice_id": "184512-2013", "contractor": None,
        "suppliers_withheld": [
            {"name_raw": DECREE, "reason": "it.notice_text_in_supplier_name"}],
        "supplier_not_disclosed": True,
        "cleaning_rules": ["it.notice_text_in_supplier_name"],
        "value_quarantine_reason": None,
    }
    client = make_test_client(contract_source=mock)
    resp = client.get("/contracts/184512-2013")
    cleanup_dishka()
    assert resp.status_code == 200
    body = resp.json()
    assert body["contractor"] is None
    assert body["supplier_not_disclosed"] is True
    assert body["suppliers_withheld"][0]["reason"] == "it.notice_text_in_supplier_name"


# ── Authority list ────────────────────────────────────────────────────

def test_authority_list_includes_the_supplier_less_contracts():
    """The totals always counted every contract the buyer awarded; the
    rows only listed the ones with a supplier, so a buyer whose award
    named nobody the cleaner would accept summed a contract the list
    never showed. The supplier is optional in the row query now, and
    the row says how many the notice withheld."""
    src, session = _authority_source([
        _authority_row(notice_id="184512-2013", contractor=None,
                       contractor_country=None, contractor_gmr_id=None,
                       supplier_withheld_count=1),
        _authority_row(notice_id="2-2026"),
    ], total={"total": 2000.0, "cnt": 2})
    out = src.get_authority_contracts("a-1")
    rows_query = [c.args[0] for c in session.run.call_args_list
                  if "LIMIT" in c.args[0]][0]
    assert "OPTIONAL MATCH (ct)-[:AWARDED_TO]->(c:Company)" in rows_query
    assert "MATCH (a)-[:AWARDED]->(ct:Contract)-[:AWARDED_TO]" not in rows_query
    assert "ct.suppliers_withheld_count AS supplier_withheld_count" in rows_query
    assert out["contract_count"] == 2
    withheld, named = out["contracts"]
    assert withheld["contractor"] is None
    assert withheld["contractor_gmr_id"] is None
    assert withheld["supplier_withheld_count"] == 1
    assert named["contractor"] == "Acme"
    assert named["supplier_withheld_count"] == 0


def test_authority_row_without_a_count_reads_as_zero():
    """A row written before the cleaning stage has no count property;
    the UI compares against zero, so it must not see null."""
    src, _ = _authority_source([_authority_row()])
    out = src.get_authority_contracts("a-1")
    assert out["contracts"][0]["supplier_withheld_count"] == 0
