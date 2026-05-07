"""Tests for the GLEIF Level 2 relationships loader (post-event-log)."""
from unittest.mock import MagicMock

from src.etl.load_gleif_relationships import emit_relationships


def _mock_log():
    log = MagicMock()
    emit = MagicMock()
    log.batch.return_value.__enter__ = MagicMock(return_value=emit)
    log.batch.return_value.__exit__ = MagicMock(return_value=False)
    return log, emit


def test_emits_one_event_per_relationship():
    log, emit = _mock_log()
    records = iter([
        ("724500973ODKK3IFQ447", "529900D69KFL8IAP8Q63", "direct"),
        ("724500973ODKK3IFQ447", "529900D69KFL8IAP8Q63", "ultimate"),
    ])
    summary = emit_relationships(log, records)
    assert summary["total"] == 2
    assert emit.upsert.call_count == 2
    types = {c.args[0] for c in emit.upsert.call_args_list}
    assert types == {"UpsertRelationship"}


def test_payload_uses_subsidiary_of_predicate_and_carries_type():
    log, emit = _mock_log()
    records = iter([
        ("724500973ODKK3IFQ447", "529900D69KFL8IAP8Q63", "direct"),
    ])
    emit_relationships(log, records)
    payload = emit.upsert.call_args.kwargs["payload"]
    assert payload["predicate"] == "subsidiaryOf"
    assert payload["properties"]["consolidation_type"] == "direct"
    assert "Company/" in payload["src_iri"]
    assert "Company/" in payload["dst_iri"]
