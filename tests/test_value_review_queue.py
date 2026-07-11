"""Tests for the value-review queue module (no live DB)."""
from unittest.mock import MagicMock

from src.etl import value_review_queue as q
from src.etl.value_review_queue import _dsn


def test_dsn_absent_disables_queue(monkeypatch):
    monkeypatch.delenv("EVENTS_DATABASE_URL", raising=False)
    assert q.connect() is None
    assert q.enqueue(None, ted_notice_id="x", reason="zero_value") is False


def test_dsn_normalises_asyncpg(monkeypatch):
    monkeypatch.setenv("EVENTS_DATABASE_URL",
                       "postgresql+asyncpg://u:p@h/db")
    assert _dsn() == "postgresql://u:p@h/db"


def test_enqueue_is_conflict_safe():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.rowcount = 1
    assert q.enqueue(conn, ted_notice_id="n-1",
                     reason="implausible_magnitude",
                     claimed_value_eur=1.8e14) is True
    sql = cur.execute.call_args.args[0]
    assert "ON CONFLICT (ted_notice_id) DO NOTHING" in sql
    cur.rowcount = 0          # duplicate on re-ingest
    assert q.enqueue(conn, ted_notice_id="n-1",
                     reason="implausible_magnitude") is False


def test_enqueue_default_never_raises(monkeypatch):
    monkeypatch.setattr(q, "_CONN", None)
    monkeypatch.setattr(q, "_CONN_FAILED", False)
    monkeypatch.setattr(q, "connect", lambda: None)
    assert q.enqueue_default(ted_notice_id="x", reason="zero_value") is False
    # and stays disabled without repeated connection attempts
    assert q.enqueue_default(ted_notice_id='y', reason='zero_value') is False  # stays disabled


def test_enqueue_leaves_connection_open_for_reuse():
    """psycopg3's `with conn:` closes the connection — enqueue must NOT
    use it, or the cached default connection dies after one insert
    (the 2026-07-06 backfill queued 0 of its review rows this way)."""
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.rowcount = 1
    assert q.enqueue(conn, ted_notice_id="n-1", reason="zero_value") is True
    conn.__enter__.assert_not_called()      # no close-on-exit context
    conn.commit.assert_called_once()
    conn.close.assert_not_called()
