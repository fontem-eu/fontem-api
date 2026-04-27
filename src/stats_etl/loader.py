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
