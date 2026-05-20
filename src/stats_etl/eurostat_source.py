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
from dataclasses import dataclass, field
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
    # Per-dimension {code → human label} maps. Only carries the dims that
    # have meaningful labels — `freq` and `time` are skipped because they
    # don't appear in observation rows or aren't worth labelling. Defaults
    # to empty so older test fixtures and callers don't break.
    dim_labels: dict[str, dict[str, str]] = field(default_factory=dict)


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
        """Cheap: fetch one cell and read the metadata sidecar.

        Eurostat ships category labels in the sidecar under
        ``dimension.{name}.category.label``. We collect them so the UI
        and downstream consumers can render ``ICCS0101`` as ``Intentional
        homicide`` instead of as an opaque code. ``freq`` and ``time``
        are skipped: ``freq`` is a constant noise dimension and ``time``
        labels are handled by the period parser.
        """
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
        dim_ids = list(data.get("id", []))
        dim_labels: dict[str, dict[str, str]] = {}
        for dim in dim_ids:
            if dim in ("freq", "time"):
                continue
            cat = data.get("dimension", {}).get(dim, {}).get("category", {})
            labels = cat.get("label") or {}
            if labels:
                dim_labels[dim] = {str(k): str(v) for k, v in labels.items()}
        return DatasetMetadata(
            code=code,
            label=data.get("label", ""),
            upstream_modified=upstream_modified,
            dim_ids=dim_ids,
            dim_sizes=list(data.get("size", [])),
            dim_labels=dim_labels,
        )

    # iter_observations() owns the bulk-TSV stream parse: header row,
    # dim-index lookup, per-row value-and-flag normalisation, NUTS-code
    # filter, batch buffer. All loop-locals of a single streaming parser.
    def iter_observations(  # pylint: disable=too-many-locals
        self,
        code: str,
        batch_size: int = 5000,
        start_period: int | None = None,
    ) -> Iterator[list[Observation]]:
        """Pull the bulk TSV and yield observations in batches.

        TSV format (one row per dim-product combination):

            freq,unit,geo,age,sex\\TIME_PERIOD\t2024 \t2023 \t2022 \t...
            A,NR,BE100,Y15-19,F\t1234.0 \t1240.5 \t...

        First column is comma-separated dim values; remaining columns are
        per-period numeric+flag tuples ("1234.5 b" or ":" for missing).

        ``start_period`` (when set) appends ``&startPeriod=YYYY`` to the
        bulk URL — Eurostat returns only observations at that year or
        later. The TSV bulk endpoint ignores ``sinceTimePeriod`` /
        ``lastTimePeriod``, but ``startPeriod`` works, and produces a
        much smaller payload (~6× for the last 2 years on MIGR_IMM8 —
        17k×35 cells → 15k×3 cells). The PK on observation guarantees
        that re-fetching overlapping periods is idempotent. Pre-
        ``startPeriod`` historical revisions are missed; the weekly
        ``--all --force`` cron is the catch-all reconcile.
        """
        url = f"{BULK_BASE}/{code.upper()}/"
        params = {"format": "TSV", "compressed": "true"}
        if start_period is not None:
            params["startPeriod"] = str(start_period)

        # Plain GET with an explicit total deadline, not stream() — the
        # streaming variant's timeout applies to inactivity between
        # chunks, so Eurostat trickling 1 byte every 119s would hang
        # the loader forever (observed live on MIGR_ACQ: 9+ minutes
        # with httpx.stream while a direct urllib.urlopen for the same
        # URL completed in 12s from the same pod). The whole gzip
        # payload is in the dozens of MB at worst, easily fits in
        # memory; nothing to gain from streaming.
        logger.info("%s: GET %s (startPeriod=%s)", code, url,
                    start_period if start_period is not None else "—")
        resp = self._http.get(url, params=params, timeout=300.0)
        resp.raise_for_status()
        raw = resp.content
        logger.info("%s: fetched %d bytes (gzip)", code, len(raw))
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


# Each early-return is a distinct Eurostat time-format branch (YYYY, YYYY-MM,
# YYYY-MM-DD, YYYYMxx, YYYY-Qx, YYYY-Sx, YYYY-Wxx, fallback). Eight returns
# matches the eight formats — collapsing them into one regex eats the comments.
def _parse_period(period: str) -> datetime | None:  # pylint: disable=too-many-return-statements
    """Map Eurostat time strings to UTC datetime aligned at start.

    Eurostat is inconsistent about the monthly form across endpoints
    and dataset families. We see all three of these in the wild:

      - ``2024-01``  — bare ISO YYYY-MM (e.g. ``MIGR_ASYAPPCTZM`` TSV)
      - ``2024-M01`` — SDMX-JSON style with explicit M marker
      - ``2024M01``  — same with the dash dropped (some TSV bulks)

    Quarterly / weekly / semestral forms are similarly accepted with
    or without the dash.
    """
    period = period.strip()
    if not period:
        return None
    try:
        # YYYY-MM form: 4-digit year, dash, two digits. Distinct from
        # 2024-Q3 / 2024-M07 because the chars after the dash are
        # purely numeric.
        if len(period) == 7 and period[4] == "-" and period[5:].isdigit():
            year, month = period.split("-", 1)
            return datetime(int(year), int(month), 1, tzinfo=timezone.utc)
        if "M" in period:
            sep = "-M" if "-M" in period else "M"
            year, month = period.split(sep, 1)
            return datetime(int(year), int(month), 1, tzinfo=timezone.utc)
        if "Q" in period:
            sep = "-Q" if "-Q" in period else "Q"
            year, q = period.split(sep, 1)
            month = (int(q) - 1) * 3 + 1
            return datetime(int(year), month, 1, tzinfo=timezone.utc)
        if "W" in period:
            sep = "-W" if "-W" in period else "W"
            year, week = period.split(sep, 1)
            return datetime.fromisocalendar(int(year), int(week), 1).replace(
                tzinfo=timezone.utc,
            )
        if "S" in period:
            sep = "-S" if "-S" in period else "S"
            year, sem = period.split(sep, 1)
            month = (int(sem) - 1) * 6 + 1
            return datetime(int(year), month, 1, tzinfo=timezone.utc)
        # Default: bare year
        return datetime(int(period), 1, 1, tzinfo=timezone.utc)
    except (ValueError, IndexError):
        return None
