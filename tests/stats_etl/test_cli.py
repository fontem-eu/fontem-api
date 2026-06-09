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


def test_cli_recompute_dataset_stats_runs_for_all_datasets(capsys):
    """`recompute-dataset-stats` with no codes must iterate every
    registered dataset (enabled OR disabled) — disabled rows still
    have legacy observations whose aggregate is worth refreshing.
    """
    fake_db = MagicMock()
    fake_db.list_datasets.return_value = [
        MagicMock(code="a"), MagicMock(code="b"),
    ]
    fake_db.recompute_dataset_stats.side_effect = [1, 1]
    with patch("src.stats_etl.cli.StatsDatabase", return_value=fake_db):
        rc = main(["recompute-dataset-stats"])
    assert rc == 0
    fake_db.migrate_dataset_stats.assert_called_once()
    assert fake_db.recompute_dataset_stats.call_count == 2
    out = capsys.readouterr().out
    assert "summary: 2 dataset(s), 2 aggregate row(s) written" in out


def test_cli_recompute_dataset_stats_with_explicit_codes(capsys):
    fake_db = MagicMock()
    fake_db.recompute_dataset_stats.return_value = 1
    with patch("src.stats_etl.cli.StatsDatabase", return_value=fake_db):
        rc = main(["recompute-dataset-stats", "demo_test"])
    assert rc == 0
    # Explicit codes must NOT call list_datasets (avoids a needless
    # round-trip + lets the operator target a single dataset cheaply).
    fake_db.list_datasets.assert_not_called()
    fake_db.recompute_dataset_stats.assert_called_once_with("demo_test")
    assert "summary: 1 dataset(s), 1 aggregate row(s) written" \
        in capsys.readouterr().out


def test_cli_register_seed_upserts_all_seeds(capsys):
    fake_db = MagicMock()
    with patch("src.stats_etl.cli.StatsDatabase", return_value=fake_db):
        rc = main(["register-seed"])
    assert rc == 0
    # Every SEED_DATASETS entry should round-trip through upsert_dataset.
    from src.stats_etl.datasets import SEED_DATASETS
    assert fake_db.upsert_dataset.call_count == len(SEED_DATASETS)
    assert f"registered {len(SEED_DATASETS)} seed datasets" in capsys.readouterr().out
    # No filter → no disable-pass either.
    fake_db.disable_datasets_not_in.assert_not_called()


def test_cli_register_seed_filters_to_codes_from_file(tmp_path, capsys):
    """--from-file restricts which seeds get registered and disables
    the rest of the catalog. Used by the staging cronjob to keep only
    a handful of medium-sized datasets active."""
    f = tmp_path / "datasets.txt"
    f.write_text("# comment\ndemo_r_gind3\n\nlfst_r_lfp2act\n")

    fake_db = MagicMock()
    fake_db.disable_datasets_not_in.return_value = 38
    with patch("src.stats_etl.cli.StatsDatabase", return_value=fake_db):
        rc = main(["register-seed", "--from-file", str(f)])

    assert rc == 0
    # Only the 2 codes in the file were upserted, not the full seed list.
    called_codes = {c.args[0].code for c in fake_db.upsert_dataset.call_args_list}
    assert called_codes == {"demo_r_gind3", "lfst_r_lfp2act"}
    fake_db.disable_datasets_not_in.assert_called_once_with(
        {"demo_r_gind3", "lfst_r_lfp2act"},
    )
    out = capsys.readouterr().out
    assert "registered 2" in out
    assert "filtered to 2 codes" in out
    assert "disabled 38" in out


def test_cli_register_seed_missing_file_falls_back_to_all(tmp_path):
    """A missing --from-file path should NOT silently disable every
    dataset — a misconfigured mount must degrade to the unfiltered
    path, not to an empty catalog."""
    fake_db = MagicMock()
    missing = tmp_path / "nope.txt"
    with patch("src.stats_etl.cli.StatsDatabase", return_value=fake_db):
        rc = main(["register-seed", "--from-file", str(missing)])
    assert rc == 0
    from src.stats_etl.datasets import SEED_DATASETS
    assert fake_db.upsert_dataset.call_count == len(SEED_DATASETS)
    fake_db.disable_datasets_not_in.assert_not_called()


def test_cli_register_seed_empty_file_registers_all_seeds(tmp_path, capsys):
    """A ``--from-file`` path that exists but contains zero codes
    (e.g. a ConfigMap whose only content is a comment line) must
    register the full SEED_DATASETS catalog and NOT disable anything.

    Regression: prod was deployed with a ``datasets.txt`` ConfigMap
    that read::

        # Empty file → register-seed registers every SEED_DATASETS entry.

    Pre-fix the code treated that as an empty filter set: every seed
    was skipped (none matched) and ``disable_datasets_not_in(set())``
    flipped the whole catalog off. The prod stats-sync cron then
    reported ``0 synced`` for weeks and the DQ ``/eurostat`` endpoint
    showed ``enabled: 0 of 40`` despite 202 M observations sitting in
    the store.
    """
    f = tmp_path / "datasets.txt"
    f.write_text("# only a comment\n\n\n# another comment\n")

    fake_db = MagicMock()
    with patch("src.stats_etl.cli.StatsDatabase", return_value=fake_db):
        rc = main(["register-seed", "--from-file", str(f)])
    assert rc == 0
    from src.stats_etl.datasets import SEED_DATASETS  # pylint: disable=import-outside-toplevel
    assert fake_db.upsert_dataset.call_count == len(SEED_DATASETS)
    fake_db.disable_datasets_not_in.assert_not_called()
    out = capsys.readouterr().out
    assert f"registered {len(SEED_DATASETS)} seed datasets" in out
