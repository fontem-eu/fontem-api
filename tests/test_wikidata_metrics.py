"""Unit tests for the relay's metrics module.

The polling thread itself is HTTP + Postgres I/O; tests cover the
pure logic in ``_poll_once`` and the counter/gauge wiring around it.
psycopg is mocked at the cursor level so we don't need a live DB.

Two main behaviours pinned:

  * ``_poll_once`` reads ``wikidata.dirty_entities`` + ``relay_state``
    and writes the right gauges.
  * Consumer-runs catch-up reads ``wikidata.consumer_runs`` rows
    whose ``id`` is greater than the bookmark in
    ``wikidata.metrics_state``, increments the corresponding
    counters by the row deltas, and persists the new bookmark so a
    restart doesn't double-count.
"""
# Reaching into ``_poll_once`` from a test is deliberate — it's the
# unit under test. The leading underscore just keeps the prod-side
# import surface small.
# pylint: disable=protected-access
from __future__ import annotations

import datetime as dt

import pytest
from prometheus_client import REGISTRY

from src.relay import metrics


# ----- fixtures ----- #

@pytest.fixture(autouse=True)
def _reset_prom_counters():
    """Counters are module-level singletons. Reset between tests so
    we observe only the deltas this test produced."""
    metrics.EVENTS_TOTAL.clear()
    metrics.DIRTY_ENTITIES.clear()
    metrics.CONSUMER_ENTITIES_TOTAL.clear()
    metrics.CURSOR_LAG_SECONDS.set(0)
    metrics.CONSUMER_LAST_FINISHED.set(0)
    yield


class _FakeCursor:
    """Minimal psycopg-cursor stand-in. Each ``execute`` arms the
    next ``fetchone`` / ``fetchall`` from a queued list. We don't
    inspect SQL strings here — the call ORDER in ``_poll_once`` is
    what defines the contract under test."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self._current = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql: str, params=None):  # pylint: disable=unused-argument
        self._current = self._responses.pop(0)

    def fetchone(self):
        return self._current

    def fetchall(self):
        return self._current


class _FakeConn:
    """Wrapper that hands out the cursor and tracks commit() calls."""

    def __init__(self, responses: list):
        self.cursor_obj = _FakeCursor(responses)
        self.commits = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1


def _consumer_run_row(run_id: int, written: int, errors: int,
                      finished_at: dt.datetime):
    """Build a row in the shape ``_poll_once`` expects from
    SELECT id, written, tombstoned, redirected,
           not_found_left_pending, errors, finished_at."""
    return (run_id, written, 0, 0, 0, errors, finished_at)


# ----- queue gauges + cursor lag ----- #

def test_poll_writes_dirty_and_tombstone_gauges() -> None:
    conn = _FakeConn([
        (1_234_567, 2_345),     # SELECT count() ..., count() ... dirty
        (42,),                  # SELECT EXTRACT(EPOCH) ... cursor lag
        (0,),                   # SELECT last_consumer_run_id
        [],                     # SELECT consumer_runs (no new rows)
    ])

    metrics._poll_once(conn)

    refetch = REGISTRY.get_sample_value(
        "wikidata_dirty_entities_total", {"state": "refetch"})
    tombstone = REGISTRY.get_sample_value(
        "wikidata_dirty_entities_total", {"state": "tombstone"})
    assert refetch == 1_234_567
    assert tombstone == 2_345


def test_poll_writes_cursor_lag_gauge() -> None:
    conn = _FakeConn([
        (10, 0),
        (123,),
        (0,),
        [],
    ])

    metrics._poll_once(conn)

    assert REGISTRY.get_sample_value(
        "wikidata_relay_cursor_lag_seconds") == 123


def test_poll_handles_null_relay_state_row() -> None:
    """If wikidata.relay_state has no row (fresh install), the
    fetchone returns None — the gauge should stay at its prior
    value rather than crashing."""
    conn = _FakeConn([
        (0, 0),
        None,        # no relay_state row
        (0,),
        [],
    ])

    metrics._poll_once(conn)  # must not raise

    # Untouched
    assert REGISTRY.get_sample_value(
        "wikidata_relay_cursor_lag_seconds") == 0


# ----- consumer runs catch-up ----- #

def test_poll_increments_consumer_counters_from_new_runs() -> None:
    finished = dt.datetime(2026, 1, 1, 12, 0, 0,
                           tzinfo=dt.timezone.utc)
    conn = _FakeConn([
        (0, 0),
        (1,),
        (5,),       # last bookmark = 5
        [
            _consumer_run_row(6, written=100, errors=2,
                              finished_at=finished),
            _consumer_run_row(7, written=200, errors=3,
                              finished_at=finished),
        ],
        None,       # INSERT INTO metrics_state ... bookmark
    ])

    metrics._poll_once(conn)

    assert REGISTRY.get_sample_value(
        "wikidata_consumer_entities_total",
        {"outcome": "written"}) == 300
    assert REGISTRY.get_sample_value(
        "wikidata_consumer_entities_total",
        {"outcome": "errors"}) == 5


def test_poll_persists_new_bookmark_after_catchup() -> None:
    finished = dt.datetime(2026, 1, 1, 12, 0, 0,
                           tzinfo=dt.timezone.utc)
    conn = _FakeConn([
        (0, 0),
        (0,),
        (5,),       # last bookmark
        [
            _consumer_run_row(6, 1, 0, finished),
            _consumer_run_row(7, 2, 0, finished),
            _consumer_run_row(8, 3, 0, finished),
        ],
        None,       # INSERT INTO metrics_state ... bookmark
    ])

    metrics._poll_once(conn)

    # Bookmark update + commit happens iff we saw new rows.
    assert conn.commits == 1


def test_poll_skips_bookmark_update_when_no_new_runs() -> None:
    conn = _FakeConn([
        (0, 0),
        (0,),
        (42,),
        [],
    ])

    metrics._poll_once(conn)

    # No new rows → no bookmark INSERT → no commit.
    assert conn.commits == 0


def test_poll_updates_last_finished_timestamp_gauge() -> None:
    finished = dt.datetime(2026, 5, 31, 23, 45, 30,
                           tzinfo=dt.timezone.utc)
    conn = _FakeConn([
        (0, 0),
        (0,),
        (0,),
        [_consumer_run_row(1, 100, 0, finished)],
        None,   # INSERT INTO metrics_state ... bookmark
    ])

    metrics._poll_once(conn)

    expected = finished.timestamp()
    assert REGISTRY.get_sample_value(
        "wikidata_consumer_last_finished_timestamp") == expected


# ----- counter family wiring ----- #

def test_relay_events_total_has_three_outcome_labels_defined() -> None:
    """The relay loop expects these three labels to exist on the
    counter family; pin the contract so a typo can't ship."""
    for outcome in ("dirty", "deleted", "ignored"):
        metrics.EVENTS_TOTAL.labels(outcome=outcome).inc(0)
        # If get_sample_value finds it (even at 0), the family is wired.
        assert REGISTRY.get_sample_value(
            "wikidata_relay_events_total", {"outcome": outcome}) == 0


def test_consumer_entities_total_label_set() -> None:
    for outcome in ("written", "tombstoned", "redirected",
                    "not_found", "errors"):
        metrics.CONSUMER_ENTITIES_TOTAL.labels(outcome=outcome).inc(0)
        assert REGISTRY.get_sample_value(
            "wikidata_consumer_entities_total",
            {"outcome": outcome}) == 0
