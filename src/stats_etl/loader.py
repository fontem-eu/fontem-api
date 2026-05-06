"""Generic Eurostat → Postgres loader.

One class, parameterised by the dataset row from the catalog. Adding
a new dataset is *register the row + invoke sync(code)* — no per-
dataset code.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .db import Dataset, StatsDatabase
from .eurostat_source import EurostatSource

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    code: str
    status: str   # 'success' | 'failed' | 'skipped'
    rows_total: int = 0
    error: str | None = None


class EurostatLoader:
    def __init__(
        self,
        source: EurostatSource,
        db: StatsDatabase,
    ) -> None:
        self._source = source
        self._db = db

    def sync(self, code: str, *, force: bool = False) -> SyncResult:
        ds = self._db.get_dataset(code)
        if ds is None:
            return SyncResult(
                code=code, status="failed",
                error=f"dataset {code} not in catalog",
            )
        if not ds.enabled and not force:
            return SyncResult(code=code, status="skipped")

        run_id = self._db.start_run(code)
        try:
            meta = self._source.fetch_metadata(code)
            # Refresh dim_ids/sizes/labels on every metadata fetch (cheap)
            # so the catalog stays current even when the data load itself
            # gets skipped because upstream hasn't changed.
            self._db.update_dataset_metadata(
                code,
                dim_ids=meta.dim_ids,
                dim_sizes=meta.dim_sizes,
                dim_labels=meta.dim_labels,
            )
            _, last_upstream = self._db.last_successful_run(code)
            if (not force and last_upstream is not None
                    and meta.upstream_modified <= last_upstream):
                self._db.finish_run(
                    run_id, status="skipped",
                    upstream_modified=meta.upstream_modified,
                )
                logger.info(
                    "%s: upstream unchanged (last=%s); skipping",
                    code, last_upstream,
                )
                return SyncResult(code=code, status="skipped")

            total = 0
            for batch in self._source.iter_observations(code):
                inserted, _ = self._db.bulk_upsert_observations(code, batch)
                total += inserted
                if total % 50_000 == 0:
                    logger.info("%s: %d rows so far", code, total)

            self._db.finish_run(
                run_id, status="success",
                rows_inserted=total, rows_total=total,
                upstream_modified=meta.upstream_modified,
            )
            logger.info("%s: synced %d rows (upstream=%s)",
                        code, total, meta.upstream_modified)

            # Recompute slice stats so the Atlas legend + colour
            # scale stay in sync with the new observations. Cheap
            # enough to do on every successful sync (one aggregation
            # query per slice). Failures here don't unwind the sync —
            # the observations are already committed; stats can be
            # refilled out-of-band via `stats-etl recompute-stats`.
            try:
                self._db.migrate_slice_stats()
                slice_rows = self._db.recompute_slice_stats(code)
                logger.info("%s: recomputed stats for %d slice(s)",
                            code, slice_rows)
            except Exception as stats_exc:  # pylint: disable=broad-except
                logger.warning(
                    "%s: slice-stats recompute failed (%s); "
                    "sync itself succeeded — recompute via "
                    "`stats-etl recompute-stats %s`",
                    code, stats_exc, code,
                )

            return SyncResult(
                code=code, status="success", rows_total=total,
            )
        except Exception as exc:  # pylint: disable=broad-except
            self._db.finish_run(
                run_id, status="failed",
                error_message=str(exc)[:1000],
            )
            logger.exception("%s: sync failed", code)
            return SyncResult(code=code, status="failed", error=str(exc))


def sync_one(code: str, *, force: bool = False) -> SyncResult:
    """Convenience constructor + run, used by the CLI."""
    db = StatsDatabase()
    source = EurostatSource()
    loader = EurostatLoader(source, db)
    return loader.sync(code, force=force)


def sync_many(codes: list[str], *, force: bool = False) -> list[SyncResult]:
    """Sequential — Eurostat rate-limits aggressively when parallel."""
    db = StatsDatabase()
    source = EurostatSource()
    loader = EurostatLoader(source, db)
    return [loader.sync(c, force=force) for c in codes]
