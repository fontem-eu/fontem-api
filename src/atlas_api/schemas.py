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
    # Dimension axes (e.g. ["freq","iccs","unit","geo","time"]) and the
    # human labels for the codes that appear in those axes. The UI uses
    # these to render slice pickers. Empty until the first sync writes
    # them. `freq` and `time` are excluded from labels (period parser
    # handles time; freq is constant).
    dim_ids: list[str] = Field(default_factory=list)
    dim_labels: dict[str, dict[str, str]] = Field(default_factory=dict)


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
