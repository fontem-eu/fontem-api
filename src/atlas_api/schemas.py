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


class DatasetDetail(DatasetSummary):
    """Catalog row with its observed time range and dim-combo count.

    Costs one extra aggregate query per dataset; we surface this only
    on the single-dataset endpoint to keep the catalog list cheap.
    """
    observation_count: int | None = None
    earliest_year: int | None = None
    latest_year: int | None = None
    distinct_dim_combos: int | None = None


# ── Observations ────────────────────────────────────────────────────


class Observation(BaseModel):
    geo_code: str
    year: int
    time: datetime
    dimensions: dict[str, Any]
    value: float | None
    flags: str | None = None


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


# ── Snapshot (choropleth-shaped) ────────────────────────────────────


class SnapshotCell(BaseModel):
    geo_code: str
    value: float | None


class SnapshotResponse(BaseModel):
    dataset: str
    year: int
    nuts_level: int
    dimensions_filter: dict[str, Any] | None
    available_dim_combos: list[dict[str, Any]] = Field(
        description=(
            "Other dimension combinations present in the data for the "
            "same (dataset, year, nuts_level). Lets the UI offer a "
            "slice picker without a second round-trip."
        ),
    )
    count: int
    cells: list[SnapshotCell]
