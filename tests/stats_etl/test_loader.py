"""Tests for stats_etl.loader — the generic dataset orchestrator.

Mocks both upstream (EurostatSource) and downstream (StatsDatabase) so
the loader's contract is what's exercised, not the integrations.
"""
from __future__ import annotations

# pylint: disable=missing-function-docstring

from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

from src.stats_etl.db import Dataset
from src.stats_etl.eurostat_source import DatasetMetadata, Observation
from src.stats_etl.loader import EurostatLoader, sync_many


def _ds(code: str = "demo_test", enabled: bool = True) -> Dataset:
    return Dataset(
        code=code, label="t", theme="population",
        source="eurostat", source_url="https://x",
        nuts_levels=[3], dim_ids=[], dim_sizes=[],
        time_unit="year", update_freq="1 year", enabled=enabled,
    )


def _meta(code: str = "demo_test",
          updated: datetime | None = None) -> DatasetMetadata:
    return DatasetMetadata(
        code=code, label="t",
        upstream_modified=updated or datetime(2026, 1, 1, tzinfo=timezone.utc),
        dim_ids=["geo", "time"], dim_sizes=[10, 5],
    )


def _obs(n: int = 3) -> list[Observation]:
    return [
        Observation(
            time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            geo_code=f"BE{i:03d}",
            dimensions={"sex": "T"},
            value=float(i),
        )
        for i in range(n)
    ]


def test_sync_returns_skipped_when_dataset_missing():
    db = MagicMock()
    db.get_dataset.return_value = None
    loader = EurostatLoader(MagicMock(), db)
    result = loader.sync("missing_code")
    assert result.status == "failed"
    assert "not in catalog" in (result.error or "")


def test_sync_returns_skipped_when_disabled():
    db = MagicMock()
    db.get_dataset.return_value = _ds(enabled=False)
    loader = EurostatLoader(MagicMock(), db)
    result = loader.sync("demo_test")
    assert result.status == "skipped"
    db.start_run.assert_not_called()


def test_sync_skipped_when_upstream_unchanged():
    """If last_successful_run.upstream_modified >= upstream_modified, skip."""
    last = datetime(2026, 2, 1, tzinfo=timezone.utc)
    src = MagicMock()
    src.fetch_metadata.return_value = _meta(updated=last)
    db = MagicMock()
    db.get_dataset.return_value = _ds()
    db.last_successful_run.return_value = (last, last)
    db.start_run.return_value = 42

    loader = EurostatLoader(src, db)
    result = loader.sync("demo_test")
    assert result.status == "skipped"
    db.bulk_upsert_observations.assert_not_called()
    db.finish_run.assert_called_once()
    assert db.finish_run.call_args.kwargs["status"] == "skipped"


def test_sync_proceeds_when_force_even_if_unchanged():
    last = datetime(2026, 2, 1, tzinfo=timezone.utc)
    src = MagicMock()
    src.fetch_metadata.return_value = _meta(updated=last)
    src.iter_observations.return_value = iter([_obs(2)])
    db = MagicMock()
    db.get_dataset.return_value = _ds()
    db.last_successful_run.return_value = (last, last)
    db.bulk_upsert_observations.return_value = (2, 0)

    loader = EurostatLoader(src, db)
    result = loader.sync("demo_test", force=True)
    assert result.status == "success"
    assert result.rows_total == 2
    # --force must bypass the startPeriod incremental — weekly cron
    # uses --force as the historical-revision reconcile path.
    src.iter_observations.assert_called_once()
    assert src.iter_observations.call_args.kwargs.get("start_period") is None


def test_sync_uses_start_period_when_prior_data_exists_and_upstream_newer():
    """Incremental fetch: when last_successful_run exists and upstream
    has moved on, pass startPeriod=max_observed_year-1 to the source so
    Eurostat's bulk TSV returns only the recent window. The PK on
    observation makes the overlap idempotent.
    """
    last = datetime(2026, 2, 1, tzinfo=timezone.utc)
    newer = datetime(2026, 5, 1, tzinfo=timezone.utc)
    src = MagicMock()
    src.fetch_metadata.return_value = _meta(updated=newer)
    src.iter_observations.return_value = iter([_obs(2)])
    db = MagicMock()
    db.get_dataset.return_value = _ds()
    db.last_successful_run.return_value = (last, last)
    db.max_observed_year.return_value = 2024
    db.bulk_upsert_observations.return_value = (2, 0)

    loader = EurostatLoader(src, db)
    result = loader.sync("demo_test")
    assert result.status == "success"
    src.iter_observations.assert_called_once()
    assert src.iter_observations.call_args.kwargs.get("start_period") == 2023


def test_sync_full_pull_on_first_sync_even_when_max_observed_year_none():
    """First sync (no prior success row): max_observed_year returns
    None; the loader must NOT pass startPeriod — first load needs
    every historical period.
    """
    src = MagicMock()
    src.fetch_metadata.return_value = _meta()
    src.iter_observations.return_value = iter([_obs(2)])
    db = MagicMock()
    db.get_dataset.return_value = _ds()
    db.last_successful_run.return_value = (None, None)  # never synced
    db.max_observed_year.return_value = None
    db.bulk_upsert_observations.return_value = (2, 0)

    loader = EurostatLoader(src, db)
    result = loader.sync("demo_test")
    assert result.status == "success"
    src.iter_observations.assert_called_once()
    assert src.iter_observations.call_args.kwargs.get("start_period") is None


def test_sync_success_path_writes_rows_and_finishes_success():
    src = MagicMock()
    src.fetch_metadata.return_value = _meta()
    src.iter_observations.return_value = iter([_obs(3), _obs(2)])
    db = MagicMock()
    db.get_dataset.return_value = _ds()
    db.last_successful_run.return_value = (None, None)  # never synced
    db.bulk_upsert_observations.side_effect = [(3, 0), (2, 0)]

    loader = EurostatLoader(src, db)
    result = loader.sync("demo_test")
    assert result.status == "success"
    assert result.rows_total == 5
    assert db.bulk_upsert_observations.call_count == 2
    db.finish_run.assert_called_once()
    assert db.finish_run.call_args.kwargs["status"] == "success"
    assert db.finish_run.call_args.kwargs["rows_total"] == 5


def test_sync_failure_path_logs_error_and_records_failed_run():
    # Triggered via the data pull, not the metadata probe: a probe failure
    # is deliberately non-fatal now (see
    # test_probe_failure_still_syncs_the_data), so it no longer exercises
    # this path.
    src = MagicMock()
    src.fetch_metadata.return_value = _meta()
    src.iter_observations.side_effect = RuntimeError("API blew up")
    db = MagicMock()
    db.get_dataset.return_value = _ds()
    db.last_successful_run.return_value = (None, None)

    loader = EurostatLoader(src, db)
    result = loader.sync("demo_test")
    assert result.status == "failed"
    assert "API blew up" in (result.error or "")
    db.finish_run.assert_called_once()
    assert db.finish_run.call_args.kwargs["status"] == "failed"
    assert "API blew up" in db.finish_run.call_args.kwargs["error_message"]


def test_sync_recomputes_slice_stats_after_success():
    """After a successful sync the loader must trigger
    `recompute_slice_stats`, which is what keeps the Atlas legend
    + colour scale in sync with the freshly-loaded data.
    """
    src = MagicMock()
    src.fetch_metadata.return_value = _meta()
    src.iter_observations.return_value = iter([_obs(3)])
    db = MagicMock()
    db.get_dataset.return_value = _ds()
    db.last_successful_run.return_value = (None, None)
    db.bulk_upsert_observations.return_value = (3, 0)
    db.recompute_slice_stats.return_value = 4

    loader = EurostatLoader(src, db)
    result = loader.sync("demo_test")
    assert result.status == "success"
    db.migrate_slice_stats.assert_called_once()
    db.recompute_slice_stats.assert_called_once_with("demo_test")


def test_sync_success_survives_slice_stats_recompute_failure():
    """Recompute is best-effort: if the SQL raises, the sync result
    must still report success because the observations themselves
    are committed. The user can re-run via `recompute-stats`.
    """
    src = MagicMock()
    src.fetch_metadata.return_value = _meta()
    src.iter_observations.return_value = iter([_obs(2)])
    db = MagicMock()
    db.get_dataset.return_value = _ds()
    db.last_successful_run.return_value = (None, None)
    db.bulk_upsert_observations.return_value = (2, 0)
    db.recompute_slice_stats.side_effect = RuntimeError("perm denied")

    loader = EurostatLoader(src, db)
    result = loader.sync("demo_test")
    assert result.status == "success"
    assert result.rows_total == 2


def test_sync_recomputes_dataset_stats_after_success():
    """After a successful sync the loader must trigger
    `recompute_dataset_stats` (per-dataset aggregate) so the
    catalog range + stable colour scale stay in sync with the
    freshly-loaded observations.
    """
    src = MagicMock()
    src.fetch_metadata.return_value = _meta()
    src.iter_observations.return_value = iter([_obs(3)])
    db = MagicMock()
    db.get_dataset.return_value = _ds()
    db.last_successful_run.return_value = (None, None)
    db.bulk_upsert_observations.return_value = (3, 0)
    db.recompute_dataset_stats.return_value = 1

    loader = EurostatLoader(src, db)
    result = loader.sync("demo_test")
    assert result.status == "success"
    db.migrate_dataset_stats.assert_called_once()
    db.recompute_dataset_stats.assert_called_once_with("demo_test")


def test_sync_success_survives_dataset_stats_recompute_failure():
    """Same best-effort contract as slice-stats and year-availability:
    a perm error or transient DB hiccup recomputing the per-dataset
    aggregate must not unwind the committed observations.
    """
    src = MagicMock()
    src.fetch_metadata.return_value = _meta()
    src.iter_observations.return_value = iter([_obs(2)])
    db = MagicMock()
    db.get_dataset.return_value = _ds()
    db.last_successful_run.return_value = (None, None)
    db.bulk_upsert_observations.return_value = (2, 0)
    db.recompute_dataset_stats.side_effect = RuntimeError("perm denied")

    loader = EurostatLoader(src, db)
    result = loader.sync("demo_test")
    assert result.status == "success"
    assert result.rows_total == 2


def test_sync_recomputes_year_availability_after_success():
    """The Atlas low-coverage filter depends on this sidecar staying
    fresh, so the loader must trigger it on every successful sync.
    """
    src = MagicMock()
    src.fetch_metadata.return_value = _meta()
    src.iter_observations.return_value = iter([_obs(3)])
    db = MagicMock()
    db.get_dataset.return_value = _ds()
    db.last_successful_run.return_value = (None, None)
    db.bulk_upsert_observations.return_value = (3, 0)
    db.recompute_year_availability.return_value = 7

    loader = EurostatLoader(src, db)
    result = loader.sync("demo_test")
    assert result.status == "success"
    db.migrate_year_availability.assert_called_once()
    # first-ever sync pulls all history, so the rebuild is unbounded
    db.recompute_year_availability.assert_called_once_with(
        "demo_test", since_year=None)


def test_sync_success_survives_year_availability_recompute_failure():
    """Same best-effort contract as slice-stats — a permission error
    in the availability recompute must not unwind the committed
    observations or downgrade the result to 'failed'.
    """
    src = MagicMock()
    src.fetch_metadata.return_value = _meta()
    src.iter_observations.return_value = iter([_obs(2)])
    db = MagicMock()
    db.get_dataset.return_value = _ds()
    db.last_successful_run.return_value = (None, None)
    db.bulk_upsert_observations.return_value = (2, 0)
    db.recompute_year_availability.side_effect = RuntimeError("perm denied")

    loader = EurostatLoader(src, db)
    result = loader.sync("demo_test")
    assert result.status == "success"
    assert result.rows_total == 2


def test_sync_many_refreshes_level_universe_once_before_loop():
    """The level-wide denominator cache must be refreshed once per
    batch, BEFORE the per-dataset loop, so each per-dataset
    `recompute_year_availability` reads a current-as-of-batch value
    instead of full-scanning the observation hypertable inline.
    """
    db = MagicMock()
    db.get_dataset.return_value = _ds()
    src = MagicMock()
    src.fetch_metadata.return_value = _meta()
    src.iter_observations.return_value = iter([])
    db.last_successful_run.return_value = (None, None)
    db.bulk_upsert_observations.return_value = (0, 0)
    db.recompute_level_universe.return_value = 4
    with patch("src.stats_etl.loader.StatsDatabase", return_value=db), \
            patch("src.stats_etl.loader.EurostatSource", return_value=src):
        results = sync_many(["a", "b", "c"])
    assert len(results) == 3
    db.recompute_level_universe.assert_called_once()


def test_sync_many_swallows_level_universe_refresh_failure():
    """A perm-error or transient DB hiccup on the level_universe
    refresh must not abort the per-dataset loop — stale denominators
    are a graceful-degradation, sync data is too important to skip.
    """
    db = MagicMock()
    db.get_dataset.return_value = _ds()
    src = MagicMock()
    src.fetch_metadata.return_value = _meta()
    src.iter_observations.return_value = iter([])
    db.last_successful_run.return_value = (None, None)
    db.bulk_upsert_observations.return_value = (0, 0)
    db.recompute_level_universe.side_effect = RuntimeError("perm denied")
    with patch("src.stats_etl.loader.StatsDatabase", return_value=db), \
            patch("src.stats_etl.loader.EurostatSource", return_value=src):
        results = sync_many(["a"])
    assert len(results) == 1
    assert results[0].status == "success"


# ── catalogue-based freshness gate ───────────────────────────────────
# The per-dataset probe is not cheap for wide datasets: migr_asyappctzm
# (103M values) returns ~3.7 MB just to read `updated`, and 413s under
# load — which used to fail the entire nightly sync. The catalogue
# answers the same question for every dataset in one request.

def _loader_with_catalogue(cat: dict, *, last_upstream: datetime | None):
    src, db = MagicMock(), MagicMock()
    db.get_dataset.return_value = _ds()
    db.last_successful_run.return_value = (None, last_upstream)
    src.fetch_catalogue_updates.return_value = cat
    return EurostatLoader(source=src, db=db), src, db


def test_catalogue_older_than_watermark_skips_without_probing():
    """The whole point: no per-dataset probe on the common path."""
    loader, src, _db = _loader_with_catalogue(
        {"demo_test": date(2026, 1, 1)},
        last_upstream=datetime(2026, 6, 1, tzinfo=timezone.utc))
    res = loader.sync("demo_test")
    assert res.status == "skipped"
    src.fetch_metadata.assert_not_called()
    src.iter_observations.assert_not_called()


def test_same_day_catalogue_date_still_probes():
    """The catalogue carries a date, our watermark a timestamp. A tie is
    ambiguous — fall through rather than risk missing a same-day update."""
    loader, src, _ = _loader_with_catalogue(
        {"demo_test": date(2026, 6, 1)},
        last_upstream=datetime(2026, 6, 1, 9, tzinfo=timezone.utc))
    src.fetch_metadata.return_value = _meta(
        updated=datetime(2026, 6, 1, 11, tzinfo=timezone.utc))
    src.iter_observations.return_value = iter([_obs(1)])
    loader.sync("demo_test")
    src.fetch_metadata.assert_called_once()


def test_unknown_code_in_catalogue_never_counts_as_unchanged():
    """Absence of information must not be read as 'nothing changed'."""
    loader, src, _ = _loader_with_catalogue(
        {}, last_upstream=datetime(2026, 6, 1, tzinfo=timezone.utc))
    src.fetch_metadata.return_value = _meta(
        updated=datetime(2026, 7, 1, tzinfo=timezone.utc))
    src.iter_observations.return_value = iter([_obs(1)])
    loader.sync("demo_test")
    src.fetch_metadata.assert_called_once()


def test_catalogue_fetched_once_and_reused_across_datasets():
    loader, src, _db = _loader_with_catalogue(
        {"demo_test": date(2026, 1, 1)},
        last_upstream=datetime(2026, 6, 1, tzinfo=timezone.utc))
    for _ in range(3):
        loader.sync("demo_test")
    assert src.fetch_catalogue_updates.call_count == 1


def test_catalogue_outage_falls_back_to_probing():
    """A catalogue failure must not silently skip every dataset."""
    src, db = MagicMock(), MagicMock()
    db.get_dataset.return_value = _ds()
    db.last_successful_run.return_value = (None, datetime(2026, 6, 1, tzinfo=timezone.utc))
    src.fetch_catalogue_updates.side_effect = RuntimeError("toc 503")
    src.fetch_metadata.return_value = _meta(
        updated=datetime(2026, 7, 1, tzinfo=timezone.utc))
    src.iter_observations.return_value = iter([_obs(1)])
    loader = EurostatLoader(source=src, db=db)
    res = loader.sync("demo_test")
    src.fetch_metadata.assert_called_once()
    assert res.status != "skipped"


def test_probe_failure_still_syncs_the_data():
    """A 413 on the label probe must not fail the dataset — the bulk TSV
    is a different endpoint and handles these sizes fine."""
    src, db = MagicMock(), MagicMock()
    db.get_dataset.return_value = _ds()
    db.last_successful_run.return_value = (None, None)
    src.fetch_catalogue_updates.return_value = {"demo_test": date(2026, 7, 1)}
    src.fetch_metadata.side_effect = RuntimeError("413 Request Entity Too Large")
    src.iter_observations.return_value = iter([_obs(2)])
    db.bulk_upsert_observations.side_effect = [(2, 0)]
    loader = EurostatLoader(source=src, db=db)
    res = loader.sync("demo_test")
    assert res.status == "success", res.error
    src.iter_observations.assert_called_once()
    # stale labels are left alone rather than blanked
    db.update_dataset_metadata.assert_not_called()


# ── bounded availability rebuild ─────────────────────────────────────
# Recomputing all history after every sync is what made the nightly run
# long: unbounded, TimescaleDB scans every chunk (36.8s / 10 chunks on
# migr_acq) where one year scans two (2.9s). Only the years we fetched
# can have changed, so pass that same window through.

def test_incremental_sync_bounds_the_availability_rebuild():
    src, db = MagicMock(), MagicMock()
    db.get_dataset.return_value = _ds()
    db.last_successful_run.return_value = (
        None, datetime(2026, 1, 1, tzinfo=timezone.utc))
    db.max_observed_year.return_value = 2026
    src.fetch_catalogue_updates.return_value = {}
    src.fetch_metadata.return_value = _meta(
        updated=datetime(2026, 6, 1, tzinfo=timezone.utc))
    src.iter_observations.return_value = iter([_obs(1)])
    db.bulk_upsert_observations.return_value = (1, 0)

    EurostatLoader(src, db).sync("demo_test")

    # start_period is max_observed_year - 1, and the rebuild must use it
    assert src.iter_observations.call_args.kwargs["start_period"] == 2025
    db.recompute_year_availability.assert_called_once_with(
        "demo_test", since_year=2025)


def test_forced_sync_rebuilds_all_years():
    """--force is the weekly reconcile that catches historical revisions,
    so its rebuild must not be bounded or those years keep stale rows."""
    src, db = MagicMock(), MagicMock()
    db.get_dataset.return_value = _ds()
    db.last_successful_run.return_value = (
        None, datetime(2026, 1, 1, tzinfo=timezone.utc))
    db.max_observed_year.return_value = 2026
    src.fetch_metadata.return_value = _meta()
    src.iter_observations.return_value = iter([_obs(1)])
    db.bulk_upsert_observations.return_value = (1, 0)

    EurostatLoader(src, db).sync("demo_test", force=True)

    assert src.iter_observations.call_args.kwargs["start_period"] is None
    db.recompute_year_availability.assert_called_once_with(
        "demo_test", since_year=None)
