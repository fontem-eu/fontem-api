"""Tests for the GLEIF Level 2 relationships loader (post-event-log)."""
from unittest.mock import MagicMock

from src.etl.load_gleif_relationships import emit_relationships


def _mock_log():
    log = MagicMock()
    emit = MagicMock()
    log.batch.return_value.__enter__ = MagicMock(return_value=emit)
    log.batch.return_value.__exit__ = MagicMock(return_value=False)
    return log, emit


def test_emits_one_event_per_relationship_plus_deduped_endpoints():
    log, emit = _mock_log()
    records = iter([
        ("724500973ODKK3IFQ447", "529900D69KFL8IAP8Q63", "direct"),
        ("724500973ODKK3IFQ447", "529900D69KFL8IAP8Q63", "ultimate"),
    ])
    summary = emit_relationships(log, records)
    assert summary["total"] == 2
    # Both records reference the same two LEIs, so the endpoint
    # companies are ensured exactly once each (deduped per run).
    assert summary["companies_ensured"] == 2
    assert emit.upsert.call_count == 4  # 2 companies + 2 relationships
    types = [c.args[0] for c in emit.upsert.call_args_list]
    assert types.count("UpsertCompany") == 2
    assert types.count("UpsertRelationship") == 2


def test_ensures_both_endpoints_exist_before_the_edge():
    """Resolve-or-create: every relationship endpoint LEI gets a minimal
    lei-bearing UpsertCompany so the SUBSIDIARY_OF edge always attaches,
    even when the base LEI-CDF record was never loaded."""
    log, emit = _mock_log()
    records = iter([
        ("724500973ODKK3IFQ447", "529900D69KFL8IAP8Q63", "direct"),
    ])
    emit_relationships(log, records)
    companies = [c for c in emit.upsert.call_args_list if c.args[0] == "UpsertCompany"]
    assert len(companies) == 2
    leis = {c.kwargs["payload"]["lei"] for c in companies}
    assert leis == {"724500973ODKK3IFQ447", "529900D69KFL8IAP8Q63"}
    # Minimal stub: carries gmr_id + lei, nothing fabricated.
    for c in companies:
        pay = c.kwargs["payload"]
        assert pay["gmr_id"] and pay["lei"]
        assert "name" not in pay
    # The companies are emitted BEFORE the relationship so the edge's
    # MATCH finds them.
    types = [c.args[0] for c in emit.upsert.call_args_list]
    assert types == ["UpsertCompany", "UpsertCompany", "UpsertRelationship"]


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
