"""Unit tests for the wikidata-consumer DB helpers.

The existing test_wikidata_consumer.py covers `process_one` end-to-end.
This module pins the SQL-shaped helpers — lease_batch, clear_dirty,
clear_dirty_batch, log_run — using a hand-rolled fake psycopg
connection so we exercise the actual SQL templates without needing a
running Postgres.

Also pins `main()`'s early-exit error path (missing env vars → exit 1
without setting up signal handlers or starting the loop), which is the
single biggest uncovered region in the module.
"""
from __future__ import annotations

from datetime import datetime, timezone
from src.relay.wikidata_consumer import (
    clear_dirty,
    clear_dirty_batch,
    lease_batch,
    log_run,
    main,
)


class _FakeCursor:
    def __init__(self, *, fetchall=None, rowcount=0):
        self._fetchall = fetchall or []
        self.rowcount = rowcount
        self.executed: list[tuple[str, object]] = []
        self.executemany_calls: list[tuple[str, list]] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def executemany(self, sql, seq):
        self.executemany_calls.append((sql, list(seq)))

    def fetchall(self):
        return self._fetchall

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None


class _FakeConn:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1


# ── lease_batch ─────────────────────────────────────────────────


def test_lease_batch_orders_by_last_changed_asc_and_returns_rows():
    rows = [("Q42", "2026-01-01", False), ("Q43", "2026-01-02", True)]
    cur = _FakeCursor(fetchall=rows)
    conn = _FakeConn(cur)
    out = lease_batch(conn, batch_size=10)
    assert out == rows
    # Verify the SQL skeleton + parameter binding.
    sql, params = cur.executed[0]
    assert "wikidata.dirty_entities" in sql
    assert "ORDER BY last_changed_at ASC" in sql
    assert params == (10,)


# ── clear_dirty (singular) ──────────────────────────────────────


def test_clear_dirty_returns_true_when_row_deleted():
    cur = _FakeCursor(rowcount=1)
    conn = _FakeConn(cur)
    assert clear_dirty(conn, "Q42", "2026-01-01") is True
    assert conn.commits == 1


def test_clear_dirty_returns_false_when_row_not_matched():
    cur = _FakeCursor(rowcount=0)
    conn = _FakeConn(cur)
    assert clear_dirty(conn, "Q42", "2026-01-01") is False


# ── clear_dirty_batch ───────────────────────────────────────────


def test_clear_dirty_batch_no_op_on_empty_input():
    cur = _FakeCursor()
    conn = _FakeConn(cur)
    clear_dirty_batch(conn, [])
    assert not cur.executemany_calls
    assert conn.commits == 0


def test_clear_dirty_batch_executemany_with_pairs():
    cur = _FakeCursor()
    conn = _FakeConn(cur)
    pairs = [("Q42", "2026-01-01"), ("Q43", "2026-01-02")]
    clear_dirty_batch(conn, pairs)
    assert len(cur.executemany_calls) == 1
    sql, seq = cur.executemany_calls[0]
    assert "DELETE" in sql
    assert seq == pairs
    assert conn.commits == 1


# ── log_run ─────────────────────────────────────────────────────


def test_log_run_inserts_counts_into_consumer_runs():
    cur = _FakeCursor()
    conn = _FakeConn(cur)
    counts = {
        "leased": 5, "written": 3, "tombstoned": 1,
        "redirected": 1, "not_found_left_pending": 0, "errors": 0,
    }
    started = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)
    log_run(conn, started, counts)
    sql, params = cur.executed[0]
    assert "INSERT INTO wikidata.consumer_runs" in sql
    assert params[0] == started
    assert params[1:] == (5, 3, 1, 1, 0, 0)
    assert conn.commits == 1


# ── main() error paths ──────────────────────────────────────────


def test_main_exits_1_when_events_database_url_missing(monkeypatch, caplog):
    monkeypatch.delenv("EVENTS_DATABASE_URL", raising=False)
    monkeypatch.setenv("VIRTUOSO_SPARQL_UPDATE_URL", "http://v/sparql-auth")
    monkeypatch.setenv("VIRTUOSO_DBA_PASSWORD", "secret")
    with caplog.at_level("ERROR"):
        assert main() == 1
    assert "must be set" in caplog.text


def test_main_exits_1_when_sparql_update_url_missing(monkeypatch):
    monkeypatch.setenv("EVENTS_DATABASE_URL", "postgresql://x")
    monkeypatch.delenv("VIRTUOSO_SPARQL_UPDATE_URL", raising=False)
    monkeypatch.setenv("VIRTUOSO_DBA_PASSWORD", "secret")
    assert main() == 1


def test_main_exits_1_when_dba_password_missing(monkeypatch):
    monkeypatch.setenv("EVENTS_DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("VIRTUOSO_SPARQL_UPDATE_URL", "http://v/sparql-auth")
    monkeypatch.delenv("VIRTUOSO_DBA_PASSWORD", raising=False)
    assert main() == 1
