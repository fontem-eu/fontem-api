"""CLI smoke tests for stats_etl.

Mocks the database so the CLI exercises argparse + flow control without
needing a live Postgres.
"""
from __future__ import annotations

# pylint: disable=missing-function-docstring,protected-access,import-outside-toplevel

from unittest.mock import MagicMock, patch

from src.stats_etl.cli import _parse_duration, main
from src.stats_etl.loader import SyncResult


def test_parse_duration_supports_common_units():
    assert _parse_duration("1d") == 86400
    assert _parse_duration("12h") == 43200
    assert _parse_duration("30m") == 1800
    assert _parse_duration("90s") == 90


def test_parse_duration_rejects_unknown():
    import pytest
    with pytest.raises(ValueError):
        _parse_duration("1y")


def test_cli_sync_with_codes(capsys):
    fake_db = MagicMock()
    with patch("src.stats_etl.cli.StatsDatabase", return_value=fake_db), \
         patch("src.stats_etl.cli.sync_many",
               return_value=[SyncResult("demo_test", "success", 100)]):
        rc = main(["sync", "demo_test"])
    assert rc == 0
    assert "summary: 1 synced, 0 skipped, 0 failed, 100 rows" in capsys.readouterr().out


def test_cli_sync_all(capsys):
    fake_db = MagicMock()
    fake_db.list_datasets.return_value = [
        MagicMock(code="a"), MagicMock(code="b"), MagicMock(code="c"),
    ]
    with patch("src.stats_etl.cli.StatsDatabase", return_value=fake_db), \
         patch("src.stats_etl.cli.sync_many",
               return_value=[
                   SyncResult("a", "success", 10),
                   SyncResult("b", "skipped"),
                   SyncResult("c", "failed", 0, "boom"),
               ]):
        rc = main(["sync", "--all"])
    # Failed dataset → exit 2 (per docstring contract)
    assert rc == 2
    out = capsys.readouterr().out
    assert "1 synced, 1 skipped, 1 failed, 10 rows" in out


def test_cli_sync_stale_after_with_no_results_returns_zero(capsys):
    fake_db = MagicMock()
    fake_db.stale_datasets.return_value = []
    with patch("src.stats_etl.cli.StatsDatabase", return_value=fake_db):
        rc = main(["sync", "--stale-after", "1d"])
    assert rc == 0
    assert "nothing stale" in capsys.readouterr().out


def test_cli_recompute_availability_runs_for_all_datasets(capsys):
    fake_db = MagicMock()
    fake_db.list_datasets.return_value = [
        MagicMock(code="a"), MagicMock(code="b"),
    ]
    fake_db.recompute_year_availability.side_effect = [12, 7]
    fake_db.recompute_level_universe.return_value = 4
    with patch("src.stats_etl.cli.StatsDatabase", return_value=fake_db):
        rc = main(["recompute-availability"])
    assert rc == 0
    fake_db.migrate_year_availability.assert_called_once()
    # level_universe is refreshed once before the per-dataset loop so
    # `recompute_year_availability` reads from the cache instead of
    # recomputing the level-wide denominator inline.
    fake_db.recompute_level_universe.assert_called_once()
    assert fake_db.recompute_year_availability.call_count == 2
    out = capsys.readouterr().out
    assert "summary: 2 dataset(s), 19 availability row(s) written" in out


def test_cli_register_seed_upserts_all_seeds(capsys):
    fake_db = MagicMock()
    with patch("src.stats_etl.cli.StatsDatabase", return_value=fake_db):
        rc = main(["register-seed"])
    assert rc == 0
    # Every SEED_DATASETS entry should round-trip through upsert_dataset.
    from src.stats_etl.datasets import SEED_DATASETS
    assert fake_db.upsert_dataset.call_count == len(SEED_DATASETS)
    assert f"registered {len(SEED_DATASETS)} seed datasets" in capsys.readouterr().out
