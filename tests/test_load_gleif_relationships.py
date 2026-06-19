"""Tests for the GLEIF Level 2 relationships loader (post-event-log)."""
import io
from unittest.mock import MagicMock

from src.etl.load_gleif_relationships import emit_relationships, parse_relationships


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


def _rr_xml(records):
    """Minimal GLEIF RR XML stream for parse_relationships tests.

    records: list of (child_lei, parent_lei, rel_type, status).
    """
    ns = "http://www.gleif.org/data/schema/rr/2016"
    body = "".join(
        f"<RelationshipRecord><Relationship>"
        f"<StartNode><NodeID>{c}</NodeID></StartNode>"
        f"<EndNode><NodeID>{p}</NodeID></EndNode>"
        f"<RelationshipType>{rt}</RelationshipType>"
        f"<RelationshipStatus>{st}</RelationshipStatus>"
        f"</Relationship></RelationshipRecord>"
        for c, p, rt, st in records
    )
    return io.BytesIO(
        f'<?xml version="1.0"?><RelationshipData xmlns="{ns}">{body}</RelationshipData>'.encode()
    )


def test_parse_skips_self_consolidation():
    out = list(parse_relationships(_rr_xml([
        ("AAAA0000000000000001", "BBBB0000000000000002", "IS_DIRECTLY_CONSOLIDATED_BY", "ACTIVE"),
        # self-loop: child == parent → must be dropped
        ("CCCC0000000000000003", "CCCC0000000000000003", "IS_DIRECTLY_CONSOLIDATED_BY", "ACTIVE"),
    ])))
    assert out == [("AAAA0000000000000001", "BBBB0000000000000002", "direct")]


def test_parse_keeps_normal_and_ultimate():
    out = list(parse_relationships(_rr_xml([
        ("AAAA0000000000000001", "BBBB0000000000000002", "IS_ULTIMATELY_CONSOLIDATED_BY", "ACTIVE"),
    ])))
    assert out == [("AAAA0000000000000001", "BBBB0000000000000002", "ultimate")]
