"""Generic Eurostat → Postgres loader.

One class, parameterised by the dataset row from the catalog. Adding
a new dataset is *register the row + invoke sync(code)* — no per-
dataset code.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from dataclasses import dataclass

from .db import StatsDatabase
from .eurostat_source import DatasetMetadata, EurostatSource

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    code: str
    status: str   # 'success' | 'failed' | 'skipped'
    rows_total: int = 0
    error: str | None = None


def _fallback_metadata(code: str, cat_date: "date | None") -> DatasetMetadata:
    """Minimal metadata for when the probe is unavailable.

    upstream_modified drives the freshness watermark, so it must not be
    invented as "now" — that would record a sync as covering data it
    never saw. Use the catalogue date when we have one; otherwise epoch,
    which simply means "no watermark advance" and leaves the dataset due
    again next run.
    """
    stamp = (datetime.combine(cat_date, time.min, tzinfo=timezone.utc)
             if cat_date is not None
             else datetime.fromtimestamp(0, tz=timezone.utc))
    return DatasetMetadata(
        code=code, label="", upstream_modified=stamp,
        dim_ids=[], dim_sizes=[], dim_labels={},
    )



class EurostatLoader:
    def __init__(
        self,
        source: EurostatSource,
        db: StatsDatabase,
    ) -> None:
        self._source = source
        self._db = db
        # Fetched at most once per Loader (i.e. once per sync run) and
        # reused for every dataset. None until the first lookup; an empty
        # dict after a failed fetch, so a catalogue outage degrades to the
        # old per-dataset probe rather than skipping everything.
        self._catalogue: dict[str, "date"] | None = None

    def _catalogue_date(self, code: str) -> "date | None":
        """Upstream 'last update of data' for `code`, from the cached
        catalogue. Returns None when unknown — callers must treat that as
        'no information', never as 'unchanged'."""
        if self._catalogue is None:
            try:
                self._catalogue = self._source.fetch_catalogue_updates()
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning(
                    "eurostat catalogue unavailable (%s); "
                    "falling back to per-dataset probes", exc,
                )
                self._catalogue = {}
        val = self._catalogue.get(code.lower())
        # Only a genuine date may short-circuit a sync. A malformed or
        # unparsed catalogue entry is missing information, and missing
        # information must never be read as "upstream unchanged".
        return val if isinstance(val, date) else None

    # sync() carries the full Eurostat → Postgres pipeline state inline
    # (run row, observation iterator, batch counters, freshness probes,
    # error capture). Splitting it into 3 sub-methods would force shared
    # state into instance attributes, which is worse — the locals here
    # are the actual loop variables of one sequential pipeline.
    def sync(self, code: str, *, force: bool = False) -> SyncResult:  # pylint: disable=too-many-locals
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
            _, last_upstream = self._db.last_successful_run(code)

            # Cheap gate first. The catalogue answers "is upstream newer?"
            # for every dataset in one request; the per-dataset probe below
            # can cost megabytes for a wide dataset and 413s outright for
            # migr_asyappctzm, which used to fail the whole nightly run.
            #
            # Only a STRICTLY older catalogue date short-circuits: the
            # catalogue carries a date where our watermark is a timestamp,
            # so a same-day tie is ambiguous and falls through to the
            # authoritative probe rather than risking a missed update.
            if not force and last_upstream is not None:
                cat_date = self._catalogue_date(code)
                if cat_date is not None and cat_date < last_upstream.date():
                    self._db.finish_run(
                        run_id, status="skipped",
                        upstream_modified=last_upstream,
                    )
                    logger.info(
                        "%s: upstream unchanged per catalogue (%s <= %s); skipping",
                        code, cat_date, last_upstream.date(),
                    )
                    return SyncResult(code=code, status="skipped")

            try:
                meta = self._source.fetch_metadata(code)
            except Exception as exc:  # pylint: disable=broad-except
                # The probe is a nice-to-have (dimension labels); the bulk
                # TSV below is the actual data and is served by a different
                # endpoint that handles these sizes fine. Losing labels for
                # one dataset beats failing it — that asymmetry is exactly
                # what turned a 413 into a red nightly run.
                cat_date = self._catalogue_date(code)
                logger.warning(
                    "%s: metadata probe failed (%s); syncing without "
                    "refreshed dimension labels", code, exc,
                )
                meta = _fallback_metadata(code, cat_date)
            if meta.dim_ids:
                self._db.update_dataset_metadata(
                    code,
                    dim_ids=meta.dim_ids,
                    dim_sizes=meta.dim_sizes,
                    dim_labels=meta.dim_labels,
                )
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

            # Incremental fetch: when this dataset has been synced
            # before AND we're not in --force mode, restrict the bulk
            # TSV pull to the year before our most-recent observation
            # via Eurostat's `startPeriod=YYYY` filter. The
            # observation PK makes the overlap idempotent. First-ever
            # sync (no prior data → max_observed_year is None) and
            # --force runs still pull the full history; --force is
            # the weekly reconcile that catches pre-startPeriod
            # historical revisions.
            start_period: int | None = None
            if not force and last_upstream is not None:
                last_year = self._db.max_observed_year(code)
                if last_year is not None:
                    start_period = last_year - 1
                    logger.info(
                        "%s: incremental fetch from %d "
                        "(upstream changed: last=%s now=%s)",
                        code, start_period,
                        last_upstream, meta.upstream_modified,
                    )

            total = 0
            for batch in self._source.iter_observations(
                code, start_period=start_period,
            ):
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

            # Recompute the dataset-level aggregate (one row per
            # dataset: value_min/max + percentiles + time_min/max
            # across every slice and every period). Used by the
            # Atlas catalog view + by "show this dataset over time"
            # to pin a stable colour scale. Cheap relative to slice
            # stats — one aggregation over `observation` filtered to
            # this dataset_code, the existing index on dataset_code
            # makes it fast. Same best-effort contract.
            try:
                self._db.migrate_dataset_stats()
                self._db.recompute_dataset_stats(code)
                logger.info("%s: recomputed dataset-level stats", code)
            except Exception as ds_exc:  # pylint: disable=broad-except
                logger.warning(
                    "%s: dataset-stats recompute failed (%s); "
                    "sync itself succeeded — recompute via "
                    "`stats-etl recompute-dataset-stats %s`",
                    code, ds_exc, code,
                )

            # Recompute per-year availability so the Atlas frontend
            # can hide low-coverage years (and low-coverage datasets)
            # without the user having to discover them mid-explore.
            # Same best-effort pattern as slice-stats — failure here
            # doesn't unwind the sync, and `stats-etl
            # recompute-availability` can patch up out-of-band.
            try:
                self._db.migrate_year_availability()
                avail_rows = self._db.recompute_year_availability(code)
                logger.info("%s: recomputed availability for %d (level,slice,year) row(s)",
                            code, avail_rows)
            except Exception as avail_exc:  # pylint: disable=broad-except
                logger.warning(
                    "%s: year-availability recompute failed (%s); "
                    "sync itself succeeded — recompute via "
                    "`stats-etl recompute-availability %s`",
                    code, avail_exc, code,
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
    # Per-dataset `recompute_year_availability` reads from
    # `fontem_stats.level_universe` instead of recomputing the
    # level-wide region counts inline (a full-hypertable scan per
    # dataset; was the only non-trivial query in the schema at ~20s
    # mean). Refresh the cache once before the batch loop runs;
    # within-batch drift is fine — datasets refresh existing geo_codes
    # rather than introducing new ones. Failures are non-fatal:
    # stale denominators degrade availability_pct gracefully.
    try:
        db.migrate_year_availability()
        n = db.recompute_level_universe()
        logger.info("level_universe refreshed (%d level row(s))", n)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("level_universe refresh failed (%s) — denominators"
                       " may be stale", exc)
    return [loader.sync(c, force=force) for c in codes]
