"""Pydantic request/response models for the Atlas API.

Kept deliberately flat — every field is optional unless the route
hard-requires it, so adding a new dimension to a payload doesn't
need a schema bump.
"""
# pylint: disable=missing-class-docstring
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Health ──────────────────────────────────────────────────────────


class SourceHealth(BaseModel):
    name: str
    status: str = Field(
        description="ok | degraded | unconfigured | down",
    )
    detail: str | None = None
    latency_ms: float | None = None


class AtlasHealth(BaseModel):
    status: str = Field(description="ok if every source is ok, else degraded")
    sources: list[SourceHealth]


# ── Datasets ────────────────────────────────────────────────────────


class SliceStats(BaseModel):
    """Per-(dataset, dimension-slice) value distribution.

    The frontend reads this to draw a stable colour scale across
    years (and a legend). `dimensions` is the slice key as a regular
    JSON object — the same shape as `Observation.dimensions` — so
    the frontend can match it to the active picker selection.

    `value_kind` decides palette family:
      - 'sequential' → viridis-style ramp anchored at p02..p98
      - 'diverging'  → PuOr-style ramp anchored around 0

    `skew_ratio` = (p98-p50) / (p50-p02). > ~5 hints "use log scale";
    NULL for pathological flat distributions where (p50-p02) == 0.
    """
    dimensions: dict[str, Any] = Field(default_factory=dict)
    value_min: float | None = None
    value_max: float | None = None
    value_p02: float | None = None
    value_p50: float | None = None
    value_p98: float | None = None
    observation_count: int = 0
    value_kind: str = "sequential"
    skew_ratio: float | None = None


class YearAvailability(BaseModel):
    """Per-(nuts_level, slice, year) coverage row for one dataset.

    The frontend uses `availability_pct` (0..1) to:
      - hide years where pct < threshold (default 0.20)
      - hide datasets whose best year (max pct) is < threshold

    `regions_total` is the level-wide universe — distinct geo_codes
    ever observed at that NUTS level across the whole stats schema
    (so a dataset that only covers EU-15 can still show "100%" for
    its best year if no other dataset covers more level-0 codes).
    """
    nuts_level: int
    dimensions: dict[str, Any] = Field(default_factory=dict)
    year: int
    regions_with_value: int
    regions_total: int | None = None
    availability_pct: float | None = None


class DatasetSummary(BaseModel):
    code: str
    label: str
    theme: str
    nuts_levels: list[int]
    time_unit: str
    update_freq: str
    enabled: bool
    notes: str | None = None
    last_sync_started_at: datetime | None = None
    last_upstream_modified: datetime | None = None
    last_sync_rows: int | None = None
    # Dimension axes (e.g. ["freq","iccs","unit","geo","time"]) and the
    # human labels for the codes that appear in those axes. The UI uses
    # these to render slice pickers. Empty until the first sync writes
    # them. `freq` and `time` are excluded from labels (period parser
    # handles time; freq is constant).
    dim_ids: list[str] = Field(default_factory=list)
    dim_labels: dict[str, dict[str, str]] = Field(default_factory=dict)
    # Per-slice value-distribution stats. Empty until a sync (or
    # `stats-etl recompute-stats`) has run against this dataset.
    slice_stats: list[SliceStats] = Field(default_factory=list)
    # Best (level, slice, year) coverage as a fraction (0..1) of the
    # NUTS-level universe. Drives the Atlas "hide low-coverage
    # datasets" toggle. None on pre-backfill clusters or read-only
    # roles where the availability table couldn't be filled.
    max_availability_pct: float | None = None
    # Per-dataset aggregate value range — min/max + p02/p50/p98 over
    # every slice and every observed period. Drives the catalog
    # range line and the stable colour scale when viewing a dataset
    # over time. All NULL on pre-backfill clusters or datasets with
    # no observations yet — the frontend falls back to per-data
    # bounds when missing.
    value_min: float | None = None
    value_max: float | None = None
    value_p02: float | None = None
    value_p50: float | None = None
    value_p98: float | None = None
    observation_count: int | None = None
    time_min: datetime | None = None
    time_max: datetime | None = None
    value_kind: str | None = None


# ── Observations ────────────────────────────────────────────────────


class Observation(BaseModel):
    geo_code: str
    year: int
    time: datetime
    dimensions: dict[str, Any]
    value: float | None
    # Eurostat flags are 1-character codes ("p"=provisional, "e"=estimate,
    # "b"=break in series, etc.). The DB stores them as `text[]` (a row
    # can carry several at once); this field carries the raw array so
    # the UI can decide whether to render them as a footnote or icon.
    # Empty array and NULL are both valid — Pydantic coerces None to None
    # and an empty list stays empty.
    flags: list[str] | None = None


# ── ETL run log ─────────────────────────────────────────────────────


class EtlRun(BaseModel):
    """One ETL CronJob invocation. Source: events.etl_run.

    `status` is `running` | `success` | `failed`. The dashboard
    treats a `running` row whose `started_at` is older than the
    cronjob's deadline as crashed (no need for a column — clients
    derive it). `summary` is the loader's human-friendly count line
    (capped 500 chars by the writer); `error_message` is a truncated
    traceback (capped 2000 chars).
    """
    run_id: int
    cronjob_name: str
    image_tag: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    status: str
    summary: str | None = None
    error_message: str | None = None


class SeriesResponse(BaseModel):
    dataset: str
    geo: list[str] | None
    nuts_level: int | None
    start: int | None
    end: int | None
    dimensions_filter: dict[str, Any] | None
    count: int
    truncated: bool = Field(
        default=False,
        description="True if `count` hit the configured row limit.",
    )
    data: list[Observation]


class SourcePipelineHealth(BaseModel):
    """Per-source pipeline health for the data-quality hub: one row per
    registered DataSource, joining the events-DB metrics (run status,
    volume, dead-letter) so the dashboard can flag stale / failing /
    lossy feeds at a glance."""

    id: str
    label: str
    theme: str
    route: str | None = None
    events_total: int = 0
    events_30d: int = 0
    last_event_at: datetime | None = None
    last_run_at: datetime | None = None
    last_run_finished_at: datetime | None = None
    last_run_status: str | None = Field(
        default=None, description="running | success | failed",
    )
    last_run_summary: str | None = None
    deadletter: int = 0
    deadletter_pct: float = 0.0
    age_hours: float | None = Field(
        default=None,
        description="hours since the last successful run (or last event)",
    )
    stale: bool = False
