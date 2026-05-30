"""Unit tests for the Wikidata EventStreams → Postgres relay.

The pure-function helpers — entity-id parsing, SSE line decoding, and
parse_event's wiki+timestamp gating — are covered here. The pre-filter
itself is exercised in test_wikidata_event_filter.py; we only check
that parse_event correctly threads its verdict through.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.relay.event_filter import EventAction
from src.relay.wikidata_recentchange import (
    StreamEvent, iter_sse_data_lines, parse_event, _parse_entity_id,
)


def test_parse_entity_id_handles_plain_q_p_l() -> None:
    assert _parse_entity_id("Q42") == "Q42"
    assert _parse_entity_id("P1278") == "P1278"
    assert _parse_entity_id("L99") == "L99"


def test_parse_entity_id_strips_namespace_prefixes() -> None:
    assert _parse_entity_id("Property:P31") == "P31"
    assert _parse_entity_id("Lexeme:L42") == "L42"
    assert _parse_entity_id("EntitySchema:E5") == "E5"


def test_parse_entity_id_returns_none_for_non_entities() -> None:
    assert _parse_entity_id("Wikidata:Project chat") is None
    assert _parse_entity_id("Q42/whatever") is None
    assert _parse_entity_id("User:Some person") is None
    assert _parse_entity_id("") is None
    assert _parse_entity_id(None) is None


def test_parse_event_filters_non_wikidata() -> None:
    raw = {
        "wiki": "enwiki",
        "title": "Apple Inc.",
        "timestamp": 1_715_000_000,
        "id": 12345,
        "type": "edit",
    }
    assert parse_event(raw) is None


def test_parse_event_keeps_wikidata_with_dirty_verdict() -> None:
    raw = {
        "wiki": "wikidatawiki",
        "title": "Q312",
        "timestamp": 1_715_000_000,
        "id": 999,
        "type": "edit",
        "user": "SomeBot",
        "namespace": 0,
        "comment": "/* wbcreateclaim-create:1|P31 */ instance-of human",
    }
    ev = parse_event(raw)
    assert isinstance(ev, StreamEvent)
    assert ev.wiki == "wikidatawiki"
    assert ev.entity_id == "Q312"
    assert ev.action is EventAction.DIRTY
    assert ev.comment_kind == "wbcreateclaim-create"
    assert ev.event_id == "999"
    assert ev.event_ts == datetime(2024, 5, 6, 12, 53, 20, tzinfo=timezone.utc)


def test_parse_event_threads_ignore_verdict() -> None:
    # Non-EU-language description add — filter marks IGNORE, parse_event
    # must preserve that.
    raw = {
        "wiki": "wikidatawiki",
        "title": "Q42",
        "timestamp": 1_715_000_000,
        "id": 1,
        "type": "edit",
        "comment": "/* wbsetdescription-add:1|bn */ Bengali description",
    }
    ev = parse_event(raw)
    assert ev is not None
    assert ev.action is EventAction.IGNORE


def test_parse_event_threads_delete_verdict() -> None:
    raw = {
        "wiki": "wikidatawiki",
        "title": "Q139814288",
        "timestamp": 1_715_000_000,
        "id": 1,
        "type": "log",
        "log_type": "delete",
        "log_action": "delete",
        "comment": "Does not meet notability policy",
    }
    ev = parse_event(raw)
    assert ev is not None
    assert ev.action is EventAction.DELETED
    assert ev.entity_id == "Q139814288"


def test_parse_event_keeps_event_with_null_entity_when_title_unparseable() -> None:
    raw = {
        "wiki": "wikidatawiki",
        "title": "Wikidata:Sandbox",
        "timestamp": 1_715_000_000,
        "id": 1001,
        "type": "edit",
        "comment": "/* wbeditentity-update:0| */ ",
    }
    ev = parse_event(raw)
    assert ev is not None
    assert ev.entity_id is None
    assert ev.wiki == "wikidatawiki"


def test_parse_event_handles_missing_id_field() -> None:
    raw = {
        "wiki": "wikidatawiki",
        "title": "Q42",
        "timestamp": 1_715_000_000,
        "type": "log",
        "log_type": "delete",
        "log_action": "delete",
    }
    ev = parse_event(raw)
    assert ev is not None
    assert ev.event_id is None


def test_parse_event_drops_event_without_timestamp() -> None:
    raw = {
        "wiki": "wikidatawiki",
        "title": "Q42",
        "id": 1,
    }
    assert parse_event(raw) is None


def test_iter_sse_data_lines_strips_data_prefix_and_handles_both_separator_styles():
    lines = [
        ": this is a heartbeat comment",
        "event: message",
        'data: {"a": 1}',
        "",
        'data:{"b": 2}',
        "",
        "id: 1234",
        "",
    ]
    out = list(iter_sse_data_lines(iter(lines)))
    assert out == [{"a": 1}, {"b": 2}]


def test_iter_sse_data_lines_skips_invalid_json_without_raising():
    lines = [
        'data: {"a": 1}',
        "data: not json",
        'data: {"b": 2}',
    ]
    out = list(iter_sse_data_lines(iter(lines)))
    assert out == [{"a": 1}, {"b": 2}]
