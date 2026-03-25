"""
Data Health Endpoint
=====================
GET /v1/health/data

Returns ingestion statistics for the local EDGAR and price data stores:
- number of companyfacts JSON files
- age of the reference ticker list
- number of price CSVs
- modification time of the most recently updated price file
- last trading date in that file

HTTP 200 always — callers check the ``status`` field.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

router = APIRouter(prefix="/v1/health", tags=["Health"])


def _iso(ts: float | None) -> str | None:
    """Convert a POSIX timestamp to an ISO-8601 UTC string."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _last_csv_date(path: Path) -> str | None:
    """Read the last (newest) date from a price CSV without loading the whole file."""
    try:
        lines = path.read_bytes().splitlines()
        if len(lines) < 2:
            return None
        last_line = lines[-1].decode("utf-8", errors="replace").strip()
        if not last_line:
            return None
        return last_line.split(",")[0]  # first column is the date
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def collect_data_health(edgar_dir: str, price_dir: str) -> dict:
    """
    Scan the data directories and return a health dict.

    Separated from the route handler so it can be unit-tested directly.
    """
    edgar_path = Path(edgar_dir)
    price_path = Path(price_dir)

    # ── EDGAR ────────────────────────────────────────────────────────
    companyfacts_dir = edgar_path / "companyfacts"
    companyfacts_count = (
        sum(1 for _ in companyfacts_dir.glob("CIK*.json"))
        if companyfacts_dir.is_dir()
        else 0
    )

    reference_file = edgar_path / "reference" / "company_tickers.json"
    edgar_reference_modified = (
        _iso(reference_file.stat().st_mtime) if reference_file.exists() else None
    )

    # ── Prices ───────────────────────────────────────────────────────
    daily_dir = price_path / "daily"
    price_csvs = list(daily_dir.glob("*.csv")) if daily_dir.is_dir() else []
    price_csv_count = len(price_csvs)

    price_newest_modified: str | None = None
    price_newest_date: str | None = None
    if price_csvs:
        newest = max(price_csvs, key=lambda p: p.stat().st_mtime)
        price_newest_modified = _iso(newest.stat().st_mtime)
        price_newest_date = _last_csv_date(newest)

    # ── Status ───────────────────────────────────────────────────────
    status = "ok" if (companyfacts_count > 0 and price_csv_count > 0) else "empty"

    return {
        "status": status,
        "edgar": {
            "companyfacts_count": companyfacts_count,
            "reference_last_modified": edgar_reference_modified,
        },
        "prices": {
            "csv_count": price_csv_count,
            "newest_file_modified": price_newest_modified,
            "newest_price_date": price_newest_date,
        },
    }


@router.get(
    "/data",
    summary="Data Ingestion Health",
    description=(
        "Returns counts and freshness timestamps for the locally stored EDGAR "
        "companyfacts and EOD price data. Always returns HTTP 200 — check the "
        "``status`` field: ``ok`` means both stores have data, ``empty`` means "
        "one or both directories are unpopulated."
    ),
)
def data_health() -> dict:
    """Read data directories from env vars and return ingestion health stats."""
    edgar_dir = os.environ.get("GMR_EDGAR_LOCAL_DATA_DIR", "/edgar-data/full")
    price_dir = os.environ.get("GMR_PRICE_DATA_DIR", "/edgar-data/prices")
    return collect_data_health(edgar_dir, price_dir)
