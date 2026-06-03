"""Unit tests for the wikidata-recentchange relay helpers.

`stream_loop` is the daemon entry point and hits the network +
Postgres — covered separately by the integration shape in
test_wikidata_relay.py. These tests pin the helpers we extracted to
keep cognitive complexity down: `_BatchCounters`, `_apply_event`,
and `main()`'s early-exit error path.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.relay.wikidata_recentchange import (
    EventAction,
    StreamEvent,
    _BatchCounters,
    _apply_event,
    main,
)


# ── _BatchCounters ──────────────────────────────────────────────


def test_batch_counters_start_at_zero():
    c = _BatchCounters()
    assert (c.pending_n, c.dirty_n, c.deleted_n, c.ignored_n) == (0, 0, 0, 0)


def test_batch_counters_bump_dispatches_by_outcome():
    c = _BatchCounters()
    c.bump("dirty")
    c.bump("dirty")
    c.bump("deleted")
    c.bump("ignored")
    c.bump("unknown")  # any non-dirty/deleted counts as ignored
    assert c.pending_n == 5
    assert c.dirty_n == 2
    assert c.deleted_n == 1
    assert c.ignored_n == 2


def test_batch_counters_reset_zeroes_all_fields():
    c = _BatchCounters()
    c.bump("dirty")
    c.bump("deleted")
    c.reset()
    assert (c.pending_n, c.dirty_n, c.deleted_n, c.ignored_n) == (0, 0, 0, 0)


# ── _apply_event ────────────────────────────────────────────────


def _ev(action: EventAction, entity_id: str | None) -> StreamEvent:
    return StreamEvent(
        event_id="evt-1",
        event_ts=datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc),
        wiki="wikidatawiki",
        entity_id=entity_id,
        action=action,
        comment_kind=None,
    )


def test_apply_event_ignores_entity_id_none():
    conn = MagicMock()
    assert _apply_event(_ev(EventAction.DIRTY, None), conn) == "ignored"
    # The mark_dirty / mark_deleted patches are NOT applied because
    # entity_id is None — no DB calls made.
    assert not conn.method_calls


def test_apply_event_marks_deleted_and_returns_deleted():
    conn = MagicMock()
    with patch("src.relay.wikidata_recentchange.mark_deleted") as md:
        out = _apply_event(_ev(EventAction.DELETED, "Q42"), conn)
    assert out == "deleted"
    md.assert_called_once()


def test_apply_event_marks_dirty_and_returns_dirty():
    conn = MagicMock()
    with patch("src.relay.wikidata_recentchange.mark_dirty") as md:
        out = _apply_event(_ev(EventAction.DIRTY, "Q42"), conn)
    assert out == "dirty"
    md.assert_called_once()


def test_apply_event_ignore_action_returns_ignored():
    conn = MagicMock()
    # IGNORE action — neither DIRTY nor DELETED — counts as ignored
    # without touching the conn (no mark_* call).
    out = _apply_event(_ev(EventAction.IGNORE, "Q42"), conn)
    assert out == "ignored"
    assert not conn.method_calls


# ── main() error path ───────────────────────────────────────────


def test_main_exits_1_when_events_database_url_missing(monkeypatch, caplog):
    monkeypatch.delenv("EVENTS_DATABASE_URL", raising=False)
    with caplog.at_level("ERROR"):
        assert main() == 1
    assert "EVENTS_DATABASE_URL" in caplog.text
