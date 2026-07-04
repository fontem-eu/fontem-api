"""Price-layer freshness stats for the data-quality surface.

Reads the two files the price pipeline maintains on NFS:

* ``_index.yml`` — the fetcher's per-ticker progress index
  (earliest/latest date, status, error_count), machine-written by
  ``usa-stock-price-fetcher``.
* ``universe_graph.json`` — the graph-exported ticker universe
  (``etl-price-universe`` cron), i.e. what the platform *wants*
  covered.

The index parser is deliberately hand-rolled for the fetcher's flat
two-level ``yaml.safe_dump`` output — PyYAML isn't a dependency of
this image and a full YAML parser is overkill for::

    TICKER:
      earliest_date: '2004-09-22'
      latest_date: '2026-03-27'
      status: in_progress
      error_count: 0
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

FRESH_DAYS = 7
STALE_DAYS = 30


def parse_index(path: str) -> dict[str, dict]:
    """Parse the fetcher's ``_index.yml``. Unknown lines are skipped —
    the file is machine-written, so anything surprising is better
    surfaced by the coverage numbers than by a parse crash."""
    entries: dict[str, dict] = {}
    current: str | None = None
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                if not line.startswith(" ") and line.rstrip().endswith(":"):
                    current = line.rstrip()[:-1].strip().strip("'\"")
                    entries[current] = {}
                elif current and ":" in line:
                    key, _, val = line.strip().partition(":")
                    val = val.strip().strip("'\"")
                    entries[current][key.strip()] = (
                        None if val in ("null", "~", "") else val
                    )
    except FileNotFoundError:
        return {}
    return entries


def _parse_date(raw) -> date | None:
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _classify_index(index: dict, today: date) -> dict:
    """Bucket every index entry by freshness."""
    fresh = stale = no_data = never = 0
    latest_overall: date | None = None
    for entry in index.values():
        if entry.get("status") == "no_data":
            no_data += 1
            continue
        latest = _parse_date(entry.get("latest_date"))
        if latest is None:
            never += 1
            continue
        if latest_overall is None or latest > latest_overall:
            latest_overall = latest
        if latest >= today - timedelta(days=FRESH_DAYS):
            fresh += 1
        elif latest < today - timedelta(days=STALE_DAYS):
            stale += 1
    return {"fresh": fresh, "stale": stale, "no_data": no_data,
            "never": never, "latest_overall": latest_overall}


def get_price_stats(data_dir: str, today: date | None = None) -> dict:
    """Freshness + coverage summary for the DQ dashboard and the
    dq-assert prices engine."""
    today = today or date.today()
    index = parse_index(os.path.join(data_dir, "_index.yml"))
    buckets = _classify_index(index, today)
    fresh, stale = buckets["fresh"], buckets["stale"]
    no_data, never = buckets["no_data"], buckets["never"]
    latest_overall = buckets["latest_overall"]

    universe: dict = {}
    try:
        with open(os.path.join(data_dir, "universe_graph.json"),
                  encoding="utf-8") as fh:
            universe = json.load(fh)
    except (FileNotFoundError, ValueError):
        pass
    universe_symbols = {
        e.get("ticker") for e in universe.values()
        if isinstance(e, dict) and e.get("ticker")
    }
    covered = sum(1 for s in universe_symbols if s in index)

    tracked = len(index)
    with_data = tracked - no_data - never
    return {
        "tracked": tracked,
        "with_data": with_data,
        "fresh_7d": fresh,
        "stale_30d": stale,
        "no_data": no_data,
        "never_fetched": never,
        "fresh_ratio": round(fresh / with_data, 3) if with_data else 0.0,
        "latest_date_overall": (latest_overall.isoformat()
                                if latest_overall else None),
        "universe_symbols": len(universe_symbols),
        "universe_covered": covered,
        "universe_backlog": len(universe_symbols) - covered,
        "universe_coverage_ratio": (
            round(covered / len(universe_symbols), 3)
            if universe_symbols else 0.0
        ),
        "index_present": bool(index),
        "universe_present": bool(universe_symbols),
    }
