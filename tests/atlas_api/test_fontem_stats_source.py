"""Read-path coverage for FontemStatsSource.

Every method here is the only place Atlas issues SQL against fontem_stats,
so the behaviour worth pinning is what happens when the store is *not*
pristine: a missing table mid-migration, a read-only role, a database that
will not answer. Each of those has a deliberate degradation, and each of
them used to be untested.

psycopg is mocked. The SQL is not the subject — the flow control around it
is. DSN normalisation lives in test_source_dsn.py and is not repeated here.
"""
from __future__ import annotations

# pylint: disable=missing-function-docstring,protected-access
# pylint: disable=unsupported-membership-test

from unittest.mock import MagicMock, patch

import psycopg
import pytest

from src.atlas_api.sources.fontem_stats import FontemStatsSource

DSN = "postgresql://u:p@h/db"


def _source_with_cursor(cur):
    """A configured source whose _connect yields a scripted cursor."""
    src = FontemStatsSource(DSN)
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    ctx.__exit__.return_value = False
    return src, patch.object(src, "_connect", return_value=ctx), conn


def _cursor(rows, cols=("a", "b")):
    cur = MagicMock()
    cur.fetchall.return_value = rows
    cur.description = [MagicMock(name=c) for c in cols]
    for m, c in zip(cur.description, cols):
        m.name = c
    return cur


# ── _connect ──────────────────────────────────────────────────

def test_connecting_without_a_dsn_is_an_explicit_error():
    src = FontemStatsSource(None)
    with pytest.raises(RuntimeError, match="STATS_DATABASE_URL"):
        with src._connect():
            pass


def test_the_connection_is_closed_even_when_the_body_raises():
    src = FontemStatsSource(DSN)
    conn = MagicMock()
    with patch.object(psycopg, "connect", return_value=conn):
        with pytest.raises(ValueError):
            with src._connect():
                raise ValueError("boom")
    conn.close.assert_called_once()


# ── health ────────────────────────────────────────────────────

def test_health_is_ok_and_timed_when_the_database_answers():
    cur = _cursor([])
    src, patched, _ = _source_with_cursor(cur)
    with patched:
        h = src.health()
    assert h.status == "ok"
    assert h.latency_ms is not None and h.latency_ms >= 0
    cur.execute.assert_called_once_with("SELECT 1")


def test_health_is_down_rather_than_raising_when_the_database_refuses():
    src = FontemStatsSource(DSN)
    with patch.object(src, "_connect", side_effect=psycopg.OperationalError("no route")):
        h = src.health()
    assert h.status == "down"
    assert "no route" in h.detail


def test_an_unsubstituted_env_var_is_diagnosed_not_just_reported_missing():
    # The failure mode this names was observed in prod: libpq otherwise
    # 28P01s with the literal, which reads as a wrong password.
    src = FontemStatsSource("postgresql://u:$(PW)@h/db")
    h = src.health()
    assert h.status == "unconfigured"
    assert "$(VAR)" in h.detail


# ── migrate ───────────────────────────────────────────────────

def test_migrate_is_a_no_op_when_unconfigured():
    src = FontemStatsSource(None)
    with patch.object(psycopg, "connect") as conn:
        src.migrate()
    conn.assert_not_called()


def test_migrate_runs_its_statements_when_configured():
    cur = _cursor([])
    src, patched, _ = _source_with_cursor(cur)
    with patched:
        src.migrate()
    assert cur.execute.called


def test_migrate_survives_a_read_only_role():
    # A role without CREATE must not take the API down: the dataset query
    # degrades to NULL availability and empty slice stats.
    src = FontemStatsSource(DSN)
    with patch.object(
        src, "_connect",
        side_effect=psycopg.errors.InsufficientPrivilege("no CREATE"),
    ):
        src.migrate()  # must not raise


# ── catalogue reads ───────────────────────────────────────────

def test_list_datasets_maps_rows_onto_column_names():
    cur = _cursor([("nama_10_gdp", "GDP")], cols=("code", "label"))
    src, patched, _ = _source_with_cursor(cur)
    with patched:
        rows = src.list_datasets()
    assert rows == [{"code": "nama_10_gdp", "label": "GDP"}]


def test_year_availability_is_scoped_to_the_dataset():
    cur = _cursor([(2, "{}", 2020, 5, 10, 50.0)],
                  cols=("nuts_level", "dimensions", "year",
                        "regions_with_value", "regions_total", "availability_pct"))
    src, patched, _ = _source_with_cursor(cur)
    with patched:
        rows = src.fetch_year_availability("nama_10_gdp")
    assert rows[0]["availability_pct"] == 50.0
    assert cur.execute.call_args[0][1] == ("nama_10_gdp",)


def test_year_availability_is_empty_when_the_table_is_missing():
    # Mid-migration, or a role that cannot see it. Absent availability means
    # "show everything" to the frontend, which beats 500-ing the selector.
    src = FontemStatsSource(DSN)
    with patch.object(src, "_connect", side_effect=psycopg.errors.UndefinedTable("nope")):
        assert src.fetch_year_availability("x") == []


def test_slice_stats_are_scoped_to_the_dataset():
    cur = _cursor([("{}", 1.0, 9.0)], cols=("dimensions", "value_min", "value_max"))
    src, patched, _ = _source_with_cursor(cur)
    with patched:
        rows = src.fetch_slice_stats("nama_10_gdp")
    assert rows[0]["value_max"] == 9.0
    assert cur.execute.call_args[0][1] == ("nama_10_gdp",)


def test_slice_stats_are_empty_when_the_table_is_missing():
    src = FontemStatsSource(DSN)
    with patch.object(src, "_connect", side_effect=psycopg.errors.UndefinedTable("nope")):
        assert src.fetch_slice_stats("x") == []
