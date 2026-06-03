"""Verify Listing tickers against the local Yahoo-backed price index.

usa-stock-price-fetcher writes /edgar-data/prices/_index.yml with a
``status`` per ticker: ``complete``, ``in_progress``, ``no_data``.
``no_data`` means Yahoo refused the symbol -> the ticker is
fabricated or wrong. We already pay for this index nightly and the
NFS read is local, so it's a free correctness oracle.

Modes:

  * ``--ticker EGL.LS`` (repeatable) — look up a specific shortlist.
  * ``--from-neo4j`` — verify every Listing in Neo4j. Bounded by
    ``--limit``.
  * ``--from-neo4j --where-no-isin`` — verify just the legacy
    fabricated cohort (Listings whose ``isin`` is null).

Run from any pod that mounts the edgar-data NFS share (the fontem-api
prod pod does)::

    kubectl -n fontem-prod cp scripts/verify_listings_local_prices.py \\
        fontem-api-...:/tmp/v.py
    kubectl -n fontem-prod exec fontem-api-... -c fontem-api -- \\
        bash -c "PYTHONPATH=/app python /tmp/v.py \\
                 --from-neo4j --where-no-isin --limit 100"
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DEFAULT_INDEX_PATH = Path("/edgar-data/prices/_index.yml")


def load_index(path: Path) -> dict[str, dict]:
    """Read the price-fetcher's per-ticker progress index."""
    if not path.exists():
        logger.error("price index not found at %s", path)
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def fetch_listings_no_isin(driver, limit: int) -> list[dict]:
    """The legacy-fabricated cohort - Listings missing an ISIN."""
    query = (
        "MATCH (l:Listing) "
        "WHERE l.isin IS NULL OR l.isin = '' "
        "OPTIONAL MATCH (c:Company)-[:LISTED_AS]->(l) "
        "RETURN l.ticker AS ticker, c.name AS company, "
        "       c.lei AS lei "
        "LIMIT $limit"
    )
    with driver.session() as s:
        return [
            {"ticker": r["ticker"], "company": r["company"],
             "lei": r["lei"], "isin": None}
            for r in s.run(query, limit=limit)
        ]


def fetch_all_listings(driver, limit: int) -> list[dict]:
    """All Listings - for a global health view."""
    query = (
        "MATCH (l:Listing) "
        "OPTIONAL MATCH (c:Company)-[:LISTED_AS]->(l) "
        "RETURN l.ticker AS ticker, c.name AS company, "
        "       c.lei AS lei, l.isin AS isin "
        "LIMIT $limit"
    )
    with driver.session() as s:
        return [
            {"ticker": r["ticker"], "company": r["company"],
             "lei": r["lei"], "isin": r["isin"]}
            for r in s.run(query, limit=limit)
        ]


def classify(ticker: str, index: dict[str, dict]) -> str:
    """Ticker's index status, or 'not_tracked' if the fetcher has
    never attempted it (no Yahoo signal yet - common for freshly-
    minted Listings)."""
    if not ticker:
        return "missing"
    entry = index.get(ticker)
    if entry is None:
        return "not_tracked"
    return entry.get("status") or "unknown"


def _rows_from_args(args, ap) -> list[dict]:
    if args.ticker:
        return [{"ticker": t, "company": None, "lei": None,
                 "isin": None}
                for t in args.ticker]
    if not args.from_neo4j:
        ap.error("specify either --ticker or --from-neo4j")
    from neo4j import GraphDatabase  # pylint: disable=import-outside-toplevel
    driver = GraphDatabase.driver(
        args.neo4j_uri,
        auth=(args.neo4j_user, args.neo4j_password),
    )
    try:
        if args.where_no_isin:
            return fetch_listings_no_isin(driver, args.limit)
        return fetch_all_listings(driver, args.limit)
    finally:
        driver.close()


def _print_table(rows: list[dict], index: dict[str, dict]) -> Counter:
    statuses: Counter = Counter()
    print()
    print(f"  {'ticker':>16s}  {'status':<14s}  {'name':<40s}  "
          f"{'lei':<22s}")
    print(f"  {'-'*16}  {'-'*14}  {'-'*40}  {'-'*22}")
    for row in rows:
        ticker = row["ticker"] or ""
        status = classify(ticker, index)
        statuses[status] += 1
        name = (row.get("company") or "")[:40]
        lei = (row.get("lei") or "")
        print(f"  {ticker:>16s}  {status:<14s}  {name:<40s}  "
              f"{lei:<22s}")
    return statuses


def _print_summary(rows: list[dict], statuses: Counter) -> None:
    total = max(len(rows), 1)
    print()
    print(f"total: {len(rows)}")
    for status, count in statuses.most_common():
        print(f"  {status:<14s} {count:>5d} "
              f"({100 * count / total:.0f}%)")


def _print_breakdown(rows: list[dict],
                     index: dict[str, dict]) -> None:
    """Per-exchange-suffix status breakdown - the most actionable
    view for spotting entirely-fabricated exchanges (``.PFTS`` was
    100% no_data in fontem-prod 2026-06)."""
    by_suffix: dict[str, Counter] = {}
    for row in rows:
        ticker = row["ticker"] or ""
        suf = ticker.rsplit(".", 1)[-1] if "." in ticker else "(none)"
        by_suffix.setdefault(suf, Counter())[
            classify(ticker, index)
        ] += 1
    print()
    print("per-exchange-suffix breakdown "
          "(complete | in_progress | no_data | not_tracked):")
    for suf in sorted(by_suffix,
                      key=lambda s: -sum(by_suffix[s].values())):
        counts = by_suffix[suf]
        total = sum(counts.values())
        print(f"  .{suf:<6s} "
              f"{counts.get('complete', 0):>5d} | "
              f"{counts.get('in_progress', 0):>5d} | "
              f"{counts.get('no_data', 0):>5d} | "
              f"{counts.get('not_tracked', 0):>5d}   "
              f"(total {total})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", action="append", default=[],
                    help="probe a specific ticker (repeatable). "
                         "Bypasses Neo4j entirely.")
    ap.add_argument("--from-neo4j", action="store_true",
                    help="probe Listings pulled from Neo4j")
    ap.add_argument("--where-no-isin", action="store_true",
                    help="(with --from-neo4j) only Listings whose "
                         "isin is null/empty - the legacy "
                         "fabricated cohort")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--index-path", type=Path,
                    default=DEFAULT_INDEX_PATH)
    ap.add_argument("--neo4j-uri",
                    default=os.environ.get("NEO4J_URI",
                                           "bolt://neo4j:7687"))
    ap.add_argument("--neo4j-user",
                    default=os.environ.get("NEO4J_USER", "neo4j"))
    ap.add_argument("--neo4j-password",
                    default=os.environ.get("NEO4J_PASSWORD", ""))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    index = load_index(args.index_path)
    if not index:
        print(f"warning: empty or missing price index at "
              f"{args.index_path}")

    rows = _rows_from_args(args, ap)
    print(f"checking {len(rows)} ticker(s) against "
          f"{args.index_path}")
    statuses = _print_table(rows, index)
    _print_summary(rows, statuses)
    _print_breakdown(rows, index)
    return 0


if __name__ == "__main__":
    sys.exit(main())
