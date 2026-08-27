"""Repository behaviour for StatsDatabase.

The SQL is not the subject — psycopg is mocked. What is tested is the
behaviour around it, which is where the decisions live: the transaction
contract, "no rows" answers that callers branch on, and the catalog
tolerance for rows written by an older image.

`max_observed_year` returning None and `last_successful_run` returning
(None, None) are load-bearing: the loader reads them to choose between an
incremental window and a full fetch. Getting either wrong silently changes
what the ETL pulls.
"""
from __future__ import annotations

# pylint: disable=missing-function-docstring,protected-access

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from src.stats_etl.db import Dataset, StatsDatabase, _normalize_url, _row_to_dataset

DSN = "postgresql://u:p@h/d"


def _db_with(cur):
    """A StatsDatabase whose connect() yields a scripted cursor."""
    database = StatsDatabase(DSN)
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    ctx.__exit__.return_value = False
    return database, patch.object(database, "connect", return_value=ctx), conn


def _catalog_row(**over):
    row = {
        "code": "nama_10_gdp", "label": "GDP", "theme": "economy",
        "source": "eurostat", "source_url": "https://x", "nuts_levels": [0, 2],
        "dim_ids": ["unit"], "dim_sizes": [3], "time_unit": "year",
        "update_freq": "1 day", "enabled": True, "notes": None,
        "dim_labels": None,
    }
    row.update(over)
    return row


# ── url + row mapping ─────────────────────────────────────────

def test_the_asyncpg_dialect_is_stripped_for_psycopg():
    assert _normalize_url("postgresql+asyncpg://u@h/d") == "postgresql://u@h/d"


def test_a_plain_url_is_left_alone():
    assert _normalize_url("postgresql://u@h/d") == "postgresql://u@h/d"


def test_dim_labels_arriving_as_json_text_are_parsed():
    ds = _row_to_dataset(_catalog_row(dim_labels=json.dumps({"unit": {"MIO": "Million"}})))
    assert ds.dim_labels == {"unit": {"MIO": "Million"}}


def test_dim_labels_that_are_not_valid_json_do_not_break_the_row():
    # A catalog row written by an older image. Reading it must degrade to
    # "no labels", not fail the whole listing.
    ds = _row_to_dataset(_catalog_row(dim_labels="{not json"))
    assert ds.dim_labels is None


def test_a_catalog_without_the_column_at_all_still_reads():
    row = _catalog_row()
    del row["dim_labels"]
    assert _row_to_dataset(row).dim_labels is None


def test_empty_dim_labels_normalise_to_none():
    assert _row_to_dataset(_catalog_row(dim_labels={})).dim_labels is None


# ── connect ───────────────────────────────────────────────────

def test_a_successful_block_commits_and_closes():
    conn = MagicMock()
    with patch.object(psycopg, "connect", return_value=conn):
        with StatsDatabase(DSN).connect():
            pass
    conn.commit.assert_called_once()
    conn.close.assert_called_once()
    conn.rollback.assert_not_called()


def test_a_failing_block_rolls_back_and_still_closes():
    conn = MagicMock()
    with patch.object(psycopg, "connect", return_value=conn):
        with pytest.raises(ValueError):
            with StatsDatabase(DSN).connect():
                raise ValueError("boom")
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()
    conn.close.assert_called_once()


def test_the_dsn_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("STATS_DATABASE_URL", "postgresql+asyncpg://env@h/d")
    assert StatsDatabase()._dsn == "postgresql://env@h/d"


# ── catalog reads ─────────────────────────────────────────────

def test_listing_only_enabled_datasets_filters_in_sql():
    cur = MagicMock()
    cur.fetchall.return_value = [_catalog_row()]
    database, patched, _ = _db_with(cur)
    with patched:
        out = database.list_datasets(only_enabled=True)
    assert [d.code for d in out] == ["nama_10_gdp"]
    assert "enabled = true" in cur.execute.call_args[0][0]


def test_listing_everything_drops_the_filter():
    cur = MagicMock()
    cur.fetchall.return_value = []
    database, patched, _ = _db_with(cur)
    with patched:
        database.list_datasets(only_enabled=False)
    assert "enabled = true" not in cur.execute.call_args[0][0]


def test_an_unknown_dataset_code_is_none_not_an_error():
    cur = MagicMock()
    cur.fetchone.return_value = None
    database, patched, _ = _db_with(cur)
    with patched:
        assert database.get_dataset("nope") is None


def test_a_known_dataset_code_is_mapped():
    cur = MagicMock()
    cur.fetchone.return_value = _catalog_row()
    database, patched, _ = _db_with(cur)
    with patched:
        assert database.get_dataset("nama_10_gdp").label == "GDP"


# ── runs ──────────────────────────────────────────────────────

def test_starting_a_run_returns_its_id():
    cur = MagicMock()
    cur.fetchone.return_value = (42,)
    database, patched, _ = _db_with(cur)
    with patched:
        assert database.start_run("nama_10_gdp") == 42


def test_finishing_a_run_records_the_outcome():
    cur = MagicMock()
    database, patched, _ = _db_with(cur)
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with patched:
        database.finish_run(7, status="success", rows_inserted=3, rows_updated=1,
                            rows_total=4, upstream_modified=when)
    args = cur.execute.call_args[0][1]
    assert args[0] == "success" and args[1] == 3 and args[-1] == 7


def test_a_dataset_never_synced_reports_no_last_run():
    # The loader branches on this to choose a full fetch over an
    # incremental window.
    cur = MagicMock()
    cur.fetchone.return_value = None
    database, patched, _ = _db_with(cur)
    with patched:
        assert database.last_successful_run("x") == (None, None)


def test_a_previous_success_is_returned_as_a_pair():
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    upstream = datetime(2025, 12, 31, tzinfo=timezone.utc)
    cur = MagicMock()
    cur.fetchone.return_value = (started, upstream)
    database, patched, _ = _db_with(cur)
    with patched:
        assert database.last_successful_run("x") == (started, upstream)


# ── catalog maintenance ───────────────────────────────────────

def test_disabling_keeps_the_codes_it_was_given():
    cur = MagicMock()
    cur.rowcount = 2
    database, patched, _ = _db_with(cur)
    with patched:
        assert database.disable_datasets_not_in({"b", "a"}) == 2
    assert cur.execute.call_args[0][1] == (["a", "b"], ), "codes are sorted for a stable query"


def test_an_empty_keep_set_disables_everything():
    # Distinct SQL: `code <> ALL('{}')` would match nothing, so an empty
    # seed would silently disable no rows rather than all of them.
    cur = MagicMock()
    cur.rowcount = 5
    database, patched, _ = _db_with(cur)
    with patched:
        assert database.disable_datasets_not_in(set()) == 5
    assert len(cur.execute.call_args[0]) == 1, "no parameters on the unfiltered form"


def test_a_null_rowcount_counts_as_zero():
    cur = MagicMock()
    cur.rowcount = None
    database, patched, _ = _db_with(cur)
    with patched:
        assert database.disable_datasets_not_in({"a"}) == 0


# ── observations ──────────────────────────────────────────────

def test_no_observations_yet_means_no_max_year():
    cur = MagicMock()
    cur.fetchone.return_value = (None,)
    database, patched, _ = _db_with(cur)
    with patched:
        assert database.max_observed_year("x") is None


def test_an_empty_result_also_means_no_max_year():
    cur = MagicMock()
    cur.fetchone.return_value = None
    database, patched, _ = _db_with(cur)
    with patched:
        assert database.max_observed_year("x") is None


def test_the_latest_observed_year_is_returned():
    cur = MagicMock()
    cur.fetchone.return_value = (2024,)
    database, patched, _ = _db_with(cur)
    with patched:
        assert database.max_observed_year("x") == 2024


def test_stale_datasets_are_returned_as_codes():
    cur = MagicMock()
    cur.fetchall.return_value = [("a",), ("b",)]
    database, patched, _ = _db_with(cur)
    with patched:
        assert database.stale_datasets(3600) == ["a", "b"]
    assert cur.execute.call_args[0][1] == (3600,)


def test_upserting_a_dataset_passes_every_field():
    cur = MagicMock()
    database, patched, _ = _db_with(cur)
    ds = Dataset(code="c", label="l", theme="t", source="s", source_url="u",
                 nuts_levels=[0], dim_ids=["d"], dim_sizes=[1], time_unit="year",
                 update_freq="1 day", enabled=True, notes="n")
    with patched:
        database.upsert_dataset(ds)
    params = cur.execute.call_args[0][1]
    assert params["code"] == "c" and params["update_freq"] == "1 day"
    assert params["notes"] == "n"
