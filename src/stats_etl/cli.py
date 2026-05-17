"""CLI for the stats ETL.

Usage::

    python -m src.stats_etl sync <code>
    python -m src.stats_etl sync --all
    python -m src.stats_etl sync --stale-after 1d
    python -m src.stats_etl sync --stale-after 1d --force
    python -m src.stats_etl register-seed
    python -m src.stats_etl nuts-polygons [--version 2024]
    python -m src.stats_etl list

Exit codes:
    0  success (or partial — see summary line)
    1  fatal error (bad args, can't reach DB, etc.)
    2  any dataset failed during a sync run
"""
from __future__ import annotations

import argparse
import logging
import re
import sys

from .datasets import SEED_DATASETS
from .db import StatsDatabase
from .loader import sync_many

logger = logging.getLogger(__name__)


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _parse_duration(text: str) -> int:
    """`1d`, `12h`, `30m`, `90s` → seconds."""
    m = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", text or "", re.IGNORECASE)
    if not m:
        raise ValueError(f"unsupported duration: {text!r}")
    n, unit = int(m.group(1)), m.group(2).lower()
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _print_summary(results: list) -> int:
    synced = sum(1 for r in results if r.status == "success")
    skipped = sum(1 for r in results if r.status == "skipped")
    failed = sum(1 for r in results if r.status == "failed")
    rows = sum(r.rows_total for r in results)
    print(
        f"summary: {synced} synced, {skipped} skipped, "
        f"{failed} failed, {rows} rows",
    )
    return 2 if failed else 0


def cmd_sync(args: argparse.Namespace) -> int:
    db = StatsDatabase()
    if args.all:
        codes = [d.code for d in db.list_datasets(only_enabled=True)]
    elif args.stale_after:
        secs = _parse_duration(args.stale_after)
        codes = db.stale_datasets(secs)
        if not codes:
            print("summary: 0 synced, 0 skipped, 0 failed, 0 rows "
                  "(nothing stale)")
            return 0
    elif args.codes:
        codes = list(args.codes)
    else:
        print("error: pass at least one of <code>, --all, --stale-after",
              file=sys.stderr)
        return 1

    results = sync_many(codes, force=args.force)
    return _print_summary(results)


def cmd_register_seed(args: argparse.Namespace) -> int:  # pylint: disable=unused-argument
    db = StatsDatabase()
    n = 0
    for d in SEED_DATASETS:
        db.upsert_dataset(d)
        n += 1
    print(f"registered {n} seed datasets")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    db = StatsDatabase()
    for d in db.list_datasets(only_enabled=not args.all):
        levels = ",".join(str(lv) for lv in d.nuts_levels)
        print(f"  {d.code:24} [NUTS {levels:5}] {d.theme:12} {d.label}")
    return 0


def cmd_nuts_polygons(args: argparse.Namespace) -> int:
    """Stub. Real implementation in nuts_loader; called via this CLI."""
    from . import nuts_loader   # local import — heavy deps (shapely)
    return nuts_loader.run(version=args.version)


def cmd_recompute_stats(args: argparse.Namespace) -> int:
    """One-shot backfill of `dataset_slice_stats` from `observation`.

    Useful for clusters that existed before slice-stats were a
    thing — running `sync` will compute them automatically going
    forward, but won't re-run for datasets where upstream is
    unchanged. This command bypasses the upstream check.
    """
    db = StatsDatabase()
    db.migrate_slice_stats()
    if args.codes:
        codes = list(args.codes)
    else:
        codes = [d.code for d in db.list_datasets(only_enabled=False)]
    if not codes:
        print("error: no datasets registered", file=sys.stderr)
        return 1
    total = 0
    for code in codes:
        n = db.recompute_slice_stats(code)
        total += n
        print(f"  {code:24} {n:5} slice(s)")
    print(f"summary: {len(codes)} dataset(s), {total} slice row(s) written")
    return 0


def cmd_recompute_availability(args: argparse.Namespace) -> int:
    """One-shot backfill of `dataset_year_availability` from observations.

    Same purpose as `recompute-stats` but for the per-year coverage
    sidecar. Useful for clusters that pre-date the table or for
    datasets whose last sync was upstream-skipped (so the loader
    didn't recompute on the way out).
    """
    db = StatsDatabase()
    db.migrate_year_availability()
    if args.codes:
        codes = list(args.codes)
    else:
        codes = [d.code for d in db.list_datasets(only_enabled=False)]
    if not codes:
        print("error: no datasets registered", file=sys.stderr)
        return 1
    # Refresh the level_universe cache once before the per-dataset
    # loop — recompute_year_availability JOINs against it instead of
    # full-scanning observation per dataset.
    lu_n = db.recompute_level_universe()
    print(f"  level_universe refreshed ({lu_n} level row(s))")
    total = 0
    for code in codes:
        n = db.recompute_year_availability(code)
        total += n
        print(f"  {code:24} {n:5} (level,slice,year) row(s)")
    print(f"summary: {len(codes)} dataset(s), {total} availability row(s) written")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="src.stats_etl")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync", help="run loaders against the catalog")
    p_sync.add_argument("codes", nargs="*",
                        help="dataset codes (or use --all / --stale-after)")
    p_sync.add_argument("--all", action="store_true",
                        help="sync every enabled dataset")
    p_sync.add_argument("--stale-after",
                        help="sync any dataset whose last success is older "
                             "than this (e.g. 1d, 12h, 7d)")
    p_sync.add_argument("--force", action="store_true",
                        help="ignore the upstream-unchanged shortcut")
    p_sync.set_defaults(func=cmd_sync)

    p_seed = sub.add_parser("register-seed",
                            help="upsert the bundled SEED_DATASETS")
    p_seed.set_defaults(func=cmd_register_seed)

    p_list = sub.add_parser("list", help="show registered datasets")
    p_list.add_argument("--all", action="store_true",
                        help="include disabled datasets")
    p_list.set_defaults(func=cmd_list)

    p_nuts = sub.add_parser("nuts-polygons",
                            help="load NUTS polygons from GISCO into PostGIS")
    p_nuts.add_argument("--version", default="2024",
                        help="NUTS revision (default 2024)")
    p_nuts.set_defaults(func=cmd_nuts_polygons)

    p_stats = sub.add_parser(
        "recompute-stats",
        help="recompute dataset_slice_stats from observations (backfill)",
    )
    p_stats.add_argument(
        "codes", nargs="*",
        help="dataset codes (default: every registered dataset)",
    )
    p_stats.set_defaults(func=cmd_recompute_stats)

    p_avail = sub.add_parser(
        "recompute-availability",
        help="recompute dataset_year_availability from observations (backfill)",
    )
    p_avail.add_argument(
        "codes", nargs="*",
        help="dataset codes (default: every registered dataset)",
    )
    p_avail.set_defaults(func=cmd_recompute_availability)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
