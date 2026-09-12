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
