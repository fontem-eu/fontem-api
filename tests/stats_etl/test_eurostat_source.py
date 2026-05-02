"""Tests for stats_etl.eurostat_source — the TSV parser + period parser."""
from __future__ import annotations

# pylint: disable=missing-function-docstring,protected-access

import gzip
from datetime import datetime, timezone
from unittest.mock import MagicMock

from src.stats_etl.eurostat_source import (
    EurostatSource,
    Observation,
    _parse_cell,
    _parse_period,
)

# Re-export Observation just so the type-stub-style usage above resolves
# under static analyzers that don't see the mock import.
__all__ = ["Observation"]


# ── Cell parsing ─────────────────────────────────────────────────

def test_parse_cell_plain_value():
    assert _parse_cell("1234.5") == (1234.5, [])


def test_parse_cell_with_flag():
    assert _parse_cell("1234.5 b") == (1234.5, ["b"])


def test_parse_cell_with_multiple_flags():
    assert _parse_cell("1234.5 b p") == (1234.5, ["b", "p"])


def test_parse_cell_missing():
    assert _parse_cell(":") == (None, [])
    assert _parse_cell(": c") == (None, [])
    assert _parse_cell("") == (None, [])


def test_parse_cell_negative():
    assert _parse_cell("-12.3") == (-12.3, [])


# ── Period parsing ────────────────────────────────────────────────

def test_parse_period_year():
    assert _parse_period("2024") == datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_parse_period_month():
    assert _parse_period("2024-M07") == datetime(2024, 7, 1, tzinfo=timezone.utc)


def test_parse_period_month_no_dash():
    # Some TSV bulks omit the dash entirely.
    assert _parse_period("2024M07") == datetime(2024, 7, 1, tzinfo=timezone.utc)


def test_parse_period_month_iso_form():
    # MIGR_ASYAPPCTZM (and other monthly TSVs) use bare ISO YYYY-MM.
    assert _parse_period("2008-01") == datetime(2008, 1, 1, tzinfo=timezone.utc)
    assert _parse_period("2024-12") == datetime(2024, 12, 1, tzinfo=timezone.utc)


def test_parse_period_quarter_distinct_from_iso_month():
    # Sanity: 2024-Q3 must not be mis-parsed as YYYY-MM.
    assert _parse_period("2024-Q3") == datetime(2024, 7, 1, tzinfo=timezone.utc)


def test_parse_period_quarter():
    assert _parse_period("2024-Q3") == datetime(2024, 7, 1, tzinfo=timezone.utc)
    assert _parse_period("2024-Q1") == datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_parse_period_quarter_no_dash():
    assert _parse_period("2024Q3") == datetime(2024, 7, 1, tzinfo=timezone.utc)


def test_parse_period_week():
    # ISO week 1 of 2024 starts on Mon 2024-01-01
    p = _parse_period("2024-W01")
    assert p == datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_parse_period_week_no_dash():
    assert _parse_period("2024W01") == datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_parse_period_semester():
    assert _parse_period("2024-S2") == datetime(2024, 7, 1, tzinfo=timezone.utc)


def test_parse_period_semester_no_dash():
    assert _parse_period("2024S2") == datetime(2024, 7, 1, tzinfo=timezone.utc)


def test_parse_period_invalid():
    assert _parse_period("garbage") is None
    assert _parse_period("") is None


# ── Bulk-TSV stream parsing ──────────────────────────────────────

def _fake_tsv_response(payload: str) -> MagicMock:
    """Build a fake httpx streaming response delivering gzipped TSV."""
    gz = gzip.compress(payload.encode("utf-8"))
    resp = MagicMock()
    resp.iter_bytes.return_value = iter([gz])
    resp.raise_for_status.return_value = None
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = None
    return resp


def test_iter_observations_minimal_tsv():
    """A 2-row TSV with two time periods → 4 observations."""
    payload = (
        "freq,unit,geo\\TIME_PERIOD\t2024 \t2023 \n"
        "A,NR,BE100\t1234.0 \t1240.5 \n"
        "A,NR,BE211\t999.9 \t:\n"  # second cell is missing → skipped
    )
    http = MagicMock()
    http.stream.return_value = _fake_tsv_response(payload)
    source = EurostatSource(http=http)

    batches = list(source.iter_observations("DEMO_TEST"))
    assert len(batches) == 1
    obs: list[Observation] = batches[0]
    assert len(obs) == 3   # 4 cells, 1 missing

    # Sort for deterministic checks
    obs.sort(key=lambda o: (o.geo_code, o.time))
    assert obs[0].geo_code == "BE100"
    assert obs[0].time == datetime(2023, 1, 1, tzinfo=timezone.utc)
    assert obs[0].value == 1240.5
    assert obs[1].geo_code == "BE100"
    assert obs[1].time == datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert obs[1].value == 1234.0
    assert obs[2].geo_code == "BE211"
    assert obs[2].value == 999.9


def test_iter_observations_carries_dimensions():
    payload = (
        "freq,sex,age,geo\\TIME_PERIOD\t2024 \n"
        "A,F,Y15-19,DE111\t100.0 \n"
    )
    http = MagicMock()
    http.stream.return_value = _fake_tsv_response(payload)
    source = EurostatSource(http=http)

    batches = list(source.iter_observations("DEMO_TEST"))
    obs = batches[0][0]
    assert obs.geo_code == "DE111"
    # `freq` is dropped (not interesting); `sex` and `age` are kept.
    assert obs.dimensions == {"sex": "F", "age": "Y15-19"}


def test_iter_observations_batches_correctly():
    """Verifies the batch_size knob splits output into multiple lists."""
    rows = []
    for i in range(25):
        rows.append(f"A,NR,XX{i:03d}\t10.0 ")
    payload = "freq,unit,geo\\TIME_PERIOD\t2024 \n" + "\n".join(rows) + "\n"
    http = MagicMock()
    http.stream.return_value = _fake_tsv_response(payload)
    source = EurostatSource(http=http)

    batches = list(source.iter_observations("X", batch_size=10))
    assert [len(b) for b in batches] == [10, 10, 5]


# ── fetch_metadata + dim_labels ──────────────────────────────────


def _fake_meta_response(body: dict) -> MagicMock:
    """Build a fake httpx GET response delivering a JSON payload."""
    resp = MagicMock()
    resp.json.return_value = body
    resp.raise_for_status.return_value = None
    return resp


def test_fetch_metadata_extracts_dim_labels():
    """SDMX-JSON ships dim labels under dimension.{name}.category.label —
    fetch_metadata should harvest those for non-time, non-freq dims."""
    body = {
        "label": "Recorded offences",
        "updated": "2026-04-29T09:00:00+0000",
        "id": ["freq", "iccs", "unit", "geo", "time"],
        "size": [1, 25, 2, 41, 17],
        "dimension": {
            "freq": {"category": {"label": {"A": "Annual"}}},
            "iccs": {"category": {"label": {
                "ICCS0101": "Intentional homicide",
                "ICCS0102": "Attempted intentional homicide",
            }}},
            "unit": {"category": {"label": {
                "NR": "Number",
                "P_HTHAB": "Per hundred thousand inhabitants",
            }}},
            "geo": {"category": {"label": {
                "BE": "Belgium",
                "DE": "Germany",
            }}},
            "time": {"category": {"label": {"2024": "2024"}}},
        },
    }
    http = MagicMock()
    http.get.return_value = _fake_meta_response(body)
    source = EurostatSource(http=http)
    meta = source.fetch_metadata("crim_off_cat")

    assert meta.dim_ids == ["freq", "iccs", "unit", "geo", "time"]
    # Only labelled dims are kept; freq + time are skipped.
    assert set(meta.dim_labels.keys()) == {"iccs", "unit", "geo"}
    assert meta.dim_labels["iccs"]["ICCS0101"] == "Intentional homicide"
    assert meta.dim_labels["unit"]["P_HTHAB"] == "Per hundred thousand inhabitants"


def test_fetch_metadata_handles_missing_labels():
    """When the catalog row has no labels (sparse upstream), dim_labels
    is just an empty dict — not a crash."""
    body = {
        "label": "Sparse",
        "updated": "2026-04-29T09:00:00+0000",
        "id": ["freq", "geo", "time"],
        "size": [1, 0, 0],
        "dimension": {
            "freq": {"category": {"label": {"A": "Annual"}}},
            # geo block missing entirely
            "time": {"category": {"label": {"2024": "2024"}}},
        },
    }
    http = MagicMock()
    http.get.return_value = _fake_meta_response(body)
    source = EurostatSource(http=http)
    meta = source.fetch_metadata("sparse_test")

    assert meta.dim_labels == {}
