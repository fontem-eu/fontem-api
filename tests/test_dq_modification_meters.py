"""The P0 meters of the contract-modifications plan.

RED today by design; what these pin is that they exist, sit in the right
family at the right severity, and read the counter the query emits. A
meter that silently read the wrong column would report green on a graph
that is still split.
"""
from src.data_quality.assertions import catalog

IDS = (
    "grain.eforms_entity_without_procedure_id",
    "grain.split_modifications",
    "grain.modification_only_entities",
)


def test_the_three_meters_exist_in_the_grain_family():
    by_id = catalog.by_id()
    for i in IDS:
        assert i in by_id, i
        assert by_id[i].family == catalog.GRAIN
        assert by_id[i].severity == catalog.WARN, "WARN until P6 has run"


def test_meters_read_violations_and_fail_on_any():
    by_id = catalog.by_id()
    for i in IDS:
        ok, _ = by_id[i].evaluate({"violations": 0, "total": 10})
        assert ok, i
        ok, obs = by_id[i].evaluate({"violations": 3, "total": 10})
        assert not ok, i
        assert "3" in str(obs)


def test_split_meter_resolves_both_back_link_forms():
    q = catalog.by_id()["grain.split_modifications"].query
    assert "[0-9a-f-]{36}-[0-9]+" in q, "versioned notice UUID form"
    assert "[0-9]+-[0-9]{4}" in q, "TED publication-number form"
    assert "ted_notice_id: ref_uuid" in q and "ted_publication_number: ref_pub" in q


def test_split_meter_does_not_count_a_back_link_the_sink_rejected():
    """Keeping a Bulgarian contract apart from the German one its typo
    names is the correct outcome, not a split."""
    q = catalog.by_id()["grain.split_modifications"].query
    assert "coalesce(m.back_link_status, '') <> 'rejected'" in q


def test_a_rejected_back_link_must_join_nothing():
    a = catalog.by_id()["grain.rejected_back_link_still_joined"]
    assert a.family == catalog.GRAIN and a.severity == catalog.WARN
    # both ways two notices can still be joined: an edge, or a shared entity
    assert "(m)-[:MODIFIES]->(a)" in a.query
    assert "(m)-[:NOTICE_OF]->(:Contract)<-[:NOTICE_OF]-(a)" in a.query
    assert " OR b" not in a.query and "ted_notice_id: ref_uuid" in a.query
    ok, _ = a.evaluate({"total": 362, "violations": 0})
    assert ok
    ok, obs = a.evaluate({"total": 362, "violations": 2})
    assert not ok and "2" in str(obs)


def test_doubtful_back_links_have_a_ceiling_not_a_zero():
    a = catalog.by_id()["grain.doubtful_back_links"]
    assert a.evaluate({"doubtful": 0})[0]
    assert a.evaluate({"doubtful": 2000})[0]
    ok, obs = a.evaluate({"doubtful": 2001})
    assert not ok and "2001" in str(obs)
