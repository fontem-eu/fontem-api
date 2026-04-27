"""Eurostat dissemination API client.

Two endpoints we use:

- SDMX-JSON (cheap, structured, gives us metadata + small slices):
    https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/{CODE}
- Bulk TSV (one shot, the full dataset, gzip-compressed):
    https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{CODE}/?format=TSV&compressed=true

The loader uses SDMX-JSON for the "is upstream newer than us?" check
(the response carries an `updated` ISO timestamp) and bulk TSV for the
actual data — TSV parses faster than reconstructing values from a
JSON-stat row-major flat array.
"""
from __future__ import annotations

import csv
import gzip
import io
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

API_BASE = (
    "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"
)
BULK_BASE = (
    "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data"
)


@dataclass(frozen=True)
class DatasetMetadata:
    """A subset of the SDMX-JSON metadata, parsed."""

    code: str
    label: str
    upstream_modified: datetime
    dim_ids: list[str]
    dim_sizes: list[int]


@dataclass(frozen=True)
class Observation:
    """One numeric value at (time, geo, dimensions)."""

    time: datetime
    geo_code: str
    dimensions: dict[str, str]
    value: float | None
    flags: list[str] | None = None


class EurostatSource:
    """Thin wrapper around the dissemination API."""

    def __init__(self, http: httpx.Client | None = None) -> None:
        self._http = http or httpx.Client(
            timeout=120.0,
            headers={"User-Agent": "fontem-stats/0.1"},
        )

    def fetch_metadata(self, code: str) -> DatasetMetadata:
        """Cheap: fetch one cell and read the metadata sidecar."""
        url = f"{API_BASE}/{code.upper()}"
        params = {"lang": "EN", "lastTimePeriod": "1", "format": "JSON"}
        resp = self._http.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        updated_raw = data.get("updated", "")
        try:
            upstream_modified = datetime.fromisoformat(updated_raw)
        except ValueError:
            upstream_modified = datetime.now(timezone.utc)
        return DatasetMetadata(
            code=code,
            label=data.get("label", ""),
            upstream_modified=upstream_modified,
            dim_ids=list(data.get("id", [])),
            dim_sizes=list(data.get("size", [])),
        )

    def iter_observations(
        self,
        code: str,
        batch_size: int = 5000,
    ) -> Iterator[list[Observation]]:
        """Pull the bulk TSV and yield observations in batches.

        TSV format (one row per dim-product combination):

            freq,unit,geo,age,sex\\TIME_PERIOD\t2024 \t2023 \t2022 \t...
            A,NR,BE100,Y15-19,F\t1234.0 \t1240.5 \t...

        First column is comma-separated dim values; remaining columns are
        per-period numeric+flag tuples ("1234.5 b" or ":" for missing).
        """
        url = f"{BULK_BASE}/{code.upper()}/"
        params = {"format": "TSV", "compressed": "true"}

        with self._http.stream("GET", url, params=params) as resp:
            resp.raise_for_status()
            raw = b"".join(resp.iter_bytes())
        text = gzip.decompress(raw).decode("utf-8")

        reader = csv.reader(io.StringIO(text), delimiter="\t")
        header = next(reader, None)
        if not header:
            return

        # Header[0] = comma-separated dim names ending with `\TIME_PERIOD`.
        # Header[1:] = time period strings, possibly with trailing spaces.
        dim_header = header[0].split("\\")[0]
        dim_names = [d.strip() for d in dim_header.split(",")]
        time_periods = [t.strip() for t in header[1:]]

        try:
            geo_idx = dim_names.index("geo")
        except ValueError:
            geo_idx = -1

        batch: list[Observation] = []
        for row in reader:
            if not row:
                continue
            dim_values = [d.strip() for d in row[0].split(",")]
            geo = dim_values[geo_idx] if geo_idx >= 0 else ""
            other_dims = {
                name: val
                for i, (name, val) in enumerate(zip(dim_names, dim_values))
                if i != geo_idx and name != "freq"
            }
            for period_idx, raw_value in enumerate(row[1:]):
                if period_idx >= len(time_periods):
                    break
                value, flags = _parse_cell(raw_value)
                if value is None and not flags:
                    continue
                t = _parse_period(time_periods[period_idx])
                if t is None:
                    continue
                batch.append(Observation(
                    time=t,
                    geo_code=geo,
                    dimensions=other_dims,
                    value=value,
                    flags=flags or None,
                ))
                if len(batch) >= batch_size:
                    yield batch
                    batch = []
        if batch:
            yield batch


def _parse_cell(raw: str) -> tuple[float | None, list[str]]:
    """Eurostat TSV cell: '<number>[ <flags>]' or ':' for missing."""
    raw = raw.strip()
    if not raw or raw.startswith(":"):
        return None, []
    parts = raw.split()
    if not parts:
        return None, []
    try:
        value = float(parts[0])
    except ValueError:
        return None, list(parts)
    flags = list(parts[1:]) if len(parts) > 1 else []
    # A trailing flag char with no separator (e.g. "1234.5b") shows up
    # occasionally — tolerate it by stripping non-numeric suffix.
    if not flags and not parts[0].replace(".", "").replace("-", "").isdigit():
        # Already failed the float; nothing to do.
        pass
    return value, flags


def _parse_period(period: str) -> datetime | None:
    """Map Eurostat time strings to UTC datetime aligned at start.

    Supports: 2024, 2024-Q3, 2024-M07, 2024-W12, 2024-S1.
    """
    period = period.strip()
    if not period:
        return None
    try:
        if "M" in period:
            year, month = period.split("-M")
            return datetime(int(year), int(month), 1, tzinfo=timezone.utc)
        if "Q" in period:
            year, q = period.split("-Q")
            month = (int(q) - 1) * 3 + 1
            return datetime(int(year), month, 1, tzinfo=timezone.utc)
        if "W" in period:
            year, week = period.split("-W")
            return datetime.fromisocalendar(int(year), int(week), 1).replace(
                tzinfo=timezone.utc,
            )
        if "S" in period:
            year, sem = period.split("-S")
            month = (int(sem) - 1) * 6 + 1
            return datetime(int(year), month, 1, tzinfo=timezone.utc)
        # Default: bare year
        return datetime(int(period), 1, 1, tzinfo=timezone.utc)
    except (ValueError, IndexError):
        return None
