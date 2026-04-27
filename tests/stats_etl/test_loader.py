"""Tests for stats_etl.loader — the generic dataset orchestrator.

Mocks both upstream (EurostatSource) and downstream (StatsDatabase) so
the loader's contract is what's exercised, not the integrations.
"""
from __future__ import annotations

# pylint: disable=missing-function-docstring

from datetime import datetime, timezone
from unittest.mock import MagicMock

from src.stats_etl.db import Dataset
from src.stats_etl.eurostat_source import DatasetMetadata, Observation
from src.stats_etl.loader import EurostatLoader


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
    src = MagicMock()
    src.fetch_metadata.side_effect = RuntimeError("API blew up")
    db = MagicMock()
    db.get_dataset.return_value = _ds()

    loader = EurostatLoader(src, db)
    result = loader.sync("demo_test")
    assert result.status == "failed"
    assert "API blew up" in (result.error or "")
    db.finish_run.assert_called_once()
    assert db.finish_run.call_args.kwargs["status"] == "failed"
    assert "API blew up" in db.finish_run.call_args.kwargs["error_message"]
