"""The in-memory event log a dry run writes to instead of Postgres.

It must present the same surface as ``EventLog`` and validate every
payload, so a dry run proves the events would have been accepted.
"""
import uuid

import pytest
from fontem_event_schemas import builders

from src.etl.dry_run_log import DryRunEventLog


def _upsert(emit, name="Alfa S.p.A."):
    payload = builders.upsert_company(
        gmr_id="11111111-1111-5111-8111-111111111111", name=name, country="ITA")
    return emit.upsert("UpsertCompany", iri="urn:c:1", domain="company",
                       payload=payload)


def test_events_are_counted_by_type_and_nothing_is_written():
    log = DryRunEventLog()
    with log.batch(uuid.uuid4(), "test") as emit:
        _upsert(emit)
        _upsert(emit, "Beta Srl")
    assert log.counts == {"UpsertCompany": 2}
    assert log.total == 2
    assert log.batches == 1
    assert log.sample[0]["producer"] == "test"
    assert log.sample[0]["op"] == "upsert"


def test_an_invalid_payload_is_refused_just_like_the_real_log():
    log = DryRunEventLog()
    with pytest.raises(Exception):
        with log.batch(uuid.uuid4(), "test") as emit:
            emit.upsert("UpsertCompany", iri="urn:c:1", domain="company",
                        payload={"not": "a company"})


def test_a_failed_batch_contributes_nothing():
    log = DryRunEventLog()
    with pytest.raises(RuntimeError):
        with log.batch(uuid.uuid4(), "test") as emit:
            _upsert(emit)
            raise RuntimeError("boom")
    assert log.total == 0
    assert log.batches == 0


def test_the_sample_is_capped_but_the_counts_are_not():
    log = DryRunEventLog(sample_size=2)
    with log.batch(uuid.uuid4(), "test") as emit:
        for i in range(5):
            _upsert(emit, f"Company {i}")
    assert len(log.sample) == 2
    assert log.counts["UpsertCompany"] == 5


def test_a_delete_is_recorded_without_schema_validation():
    log = DryRunEventLog()
    with log.batch(uuid.uuid4(), "test") as emit:
        emit.delete("DeleteCompany", iri="urn:c:1", domain="company")
    assert log.counts == {"DeleteCompany": 1}
    assert log.sample[0]["payload"] == {"iri": "urn:c:1"}


def test_the_batch_counts_its_own_events():
    log = DryRunEventLog()
    with log.batch(uuid.uuid4(), "test") as emit:
        assert emit.count == 0
        _upsert(emit)
        assert emit.count == 1


def test_close_is_a_no_op():
    log = DryRunEventLog()
    log.close()
