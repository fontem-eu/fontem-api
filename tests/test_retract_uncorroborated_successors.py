"""Withdrawing successor merges that rested on a category attribute.

`successor_lei_match` briefly accepted `legal_form` as corroboration.
OV32 is the ELF code for an Italian S.R.L. and 157,598 Italian
companies carry it, so "both are an S.R.L." corroborated every pair of
same-named Italian companies in the country: 46 distinct
"FUTURA S.R.L." records across Reggio Emilia, Ancona and Pisa merged
into one entity, and 7,873 of 12,148 successor edges rested on
legal_form alone.

The danger in the cleanup is retracting a pair the fixed rule would
still assert. These tests pin the selection to exactly the corroborator
set the fixed rule uses, so the two cannot drift apart.
"""
# pylint: disable=protected-access
from unittest.mock import MagicMock

from src.etl import retract_uncorroborated_successors as retract


def test_selection_requires_a_discriminating_attribute():
    """The Cypher must accept exactly what the fixed rule accepts:
    postal code, vat, registered_as, cik — and nothing else."""
    q = retract._FIND
    assert "postal_code" in q
    for prop in ("vat", "registered_as", "cik"):
        assert f"a.{prop}" in q and f"b.{prop}" in q, f"{prop} not consulted"
    assert "legal_form" not in q, (
        "selecting on legal_form would re-introduce the very bug this "
        "cleanup exists to undo"
    )


def test_selection_normalises_postal_codes():
    """"821 09" and "82109" are the same Slovak code; comparing raw
    would retract pairs that are genuinely corroborated."""
    q = retract._FIND
    assert "replace(toUpper(a.postal_code),' ','')" in q
    assert "replace(toUpper(b.postal_code),' ','')" in q


def test_selection_is_scoped_to_the_offending_rule():
    """Only successor_lei_match assertions are in scope. fuzzy and the
    authority rules were never affected by the legal_form corroborator."""
    q = retract._FIND
    assert "successor_lei_match" in q
    assert "status:'approved'" in q, "must not touch pending candidates"


def test_selection_keeps_corroborated_pairs():
    """The WHERE inverts the corroboration test, so anything with an
    agreeing attribute is excluded from retraction."""
    assert "WHERE NOT (postal_ok OR vat_ok OR reg_ok OR cik_ok)" in retract._FIND


def test_emits_retract_not_delete():
    """RetractSameAs removes exactly the two owl:sameAs triples. An op
    of 'delete' would make the Virtuoso sink drop the whole subject
    instead — every triple the company has."""
    log = MagicMock()
    batch = log.batch.return_value.__enter__.return_value
    retract.emit_retractions(log, [{
        "a_id": "a1", "b_id": "b1", "a_name": "FUTURA S.R.L.",
        "b_name": "FUTURA - S.R.L.", "a_post": "42015", "b_post": "56029",
    }])
    assert batch.upsert.call_count == 1
    event_type = batch.upsert.call_args.args[0]
    kwargs = batch.upsert.call_args.kwargs
    assert event_type == "RetractSameAs"
    assert kwargs["domain"] == "company"
    assert kwargs["iri"].endswith("/Company/a1")


def test_reason_records_why_and_which_rule():
    """retracted_method exists so we can analyse which rules misfire;
    the reason has to survive as the audit trail for a bulk withdrawal."""
    log = MagicMock()
    batch = log.batch.return_value.__enter__.return_value
    retract.emit_retractions(log, [{
        "a_id": "a1", "b_id": "b1", "a_name": "X", "b_name": "Y",
        "a_post": "42015", "b_post": "56029",
    }])
    payload = batch.upsert.call_args.kwargs["payload"]
    assert payload["retracted_method"] == "successor_lei_match"
    assert "legal_form" in payload["reason"]
    assert "42015" in payload["reason"] and "56029" in payload["reason"]
    assert payload["a_iri"].endswith("/Company/a1")
    assert payload["b_iri"].endswith("/Company/b1")


def test_dry_run_emits_nothing(monkeypatch):
    """This withdraws thousands of published assertions; running it by
    accident must cost nothing."""
    monkeypatch.setattr(retract, "find_uncorroborated", lambda _d: [
        {"a_id": "a", "b_id": "b", "a_name": "X", "b_name": "Y",
         "a_post": "1", "b_post": "2"}])
    driver = MagicMock()
    monkeypatch.setattr(retract.GraphDatabase, "driver",
                        staticmethod(lambda *a, **k: driver))
    monkeypatch.setenv("NEO4J_URI", "bolt://x")
    monkeypatch.setenv("NEO4J_PASSWORD", "p")
    called = []
    monkeypatch.setattr(retract, "emit_retractions",
                        lambda *a, **k: called.append(a))
    retract.main([])
    assert not called
