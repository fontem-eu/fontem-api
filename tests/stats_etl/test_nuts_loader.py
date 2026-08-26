"""Tests for stats_etl.nuts_loader — GISCO GeoJSON → PostGIS upsert.

This module had no tests at all: 49 of 49 lines uncovered, the single
largest untested file in the project. It matters more than its size
suggests, because a silently-wrong WKT coercion produces geometry that
PostGIS accepts and every downstream region query then reads wrongly.

StatsDatabase is mocked — the contract under test is the GeoJSON
coercion and the upsert loop, not psycopg.
"""
from __future__ import annotations

# pylint: disable=missing-function-docstring,protected-access

import json
from unittest.mock import MagicMock, patch

from src.stats_etl import nuts_loader


# ── _to_multipolygon_wkt ──────────────────────────────────────────────────

def test_polygon_is_wrapped_into_a_multipolygon():
    """A bare Polygon must still come out as MULTIPOLYGON — the column is
    ST_Multi, and a plain POLYGON would be rejected on insert."""
    wkt = nuts_loader._to_multipolygon_wkt(
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    )
    assert wkt == "MULTIPOLYGON(((0 0, 1 0, 1 1, 0 0)))"


def test_multipolygon_keeps_every_part():
    wkt = nuts_loader._to_multipolygon_wkt(
        {
            "type": "MultiPolygon",
            "coordinates": [
                [[[0, 0], [1, 0], [1, 1], [0, 0]]],
                [[[5, 5], [6, 5], [6, 6], [5, 5]]],
            ],
        }
    )
    assert wkt == "MULTIPOLYGON(((0 0, 1 0, 1 1, 0 0)), ((5 5, 6 5, 6 6, 5 5)))"


def test_interior_rings_are_preserved():
    """Islands with holes: dropping the inner ring would silently inflate area."""
    wkt = nuts_loader._to_multipolygon_wkt(
        {
            "type": "Polygon",
            "coordinates": [
                [[0, 0], [10, 0], [10, 10], [0, 0]],
                [[2, 2], [3, 2], [3, 3], [2, 2]],
            ],
        }
    )
    assert wkt == (
        "MULTIPOLYGON(((0 0, 10 0, 10 10, 0 0), (2 2, 3 2, 3 3, 2 2)))"
    )


def test_elevation_is_dropped_from_3d_coordinates():
    """GISCO occasionally ships XYZ; 4326 geometry takes two ordinates."""
    wkt = nuts_loader._to_multipolygon_wkt(
        {"type": "Polygon",
         "coordinates": [[[0, 0, 99], [1, 0, 99], [1, 1, 99], [0, 0, 99]]]}
    )
    assert wkt == "MULTIPOLYGON(((0 0, 1 0, 1 1, 0 0)))"


def test_unsupported_geometry_types_are_skipped():
    assert nuts_loader._to_multipolygon_wkt(
        {"type": "Point", "coordinates": [1, 2]}) is None


def test_empty_geometry_is_skipped():
    assert nuts_loader._to_multipolygon_wkt({"type": "Polygon", "coordinates": []}) is None
    assert nuts_loader._to_multipolygon_wkt({}) is None


# ── _load_level ───────────────────────────────────────────────────────────

def test_load_level_reads_the_versioned_gisco_filename(tmp_path):
    payload = {"features": [{"properties": {"NUTS_ID": "PT"}}]}
    (tmp_path / "NUTS_RG_10M_2024_4326_LEVL_0.geojson").write_text(
        json.dumps(payload), encoding="utf-8")
    assert nuts_loader._load_level("2024", 0, tmp_path) == payload


# ── run ───────────────────────────────────────────────────────────────────

def _write_levels(tmp_path, features_by_level):
    for level, feats in features_by_level.items():
        (tmp_path / f"NUTS_RG_10M_2024_4326_LEVL_{level}.geojson").write_text(
            json.dumps({"features": feats}), encoding="utf-8")


def _square(offset=0):
    return {"type": "Polygon",
            "coordinates": [[[offset, offset], [offset + 1, offset],
                             [offset + 1, offset + 1], [offset, offset]]]}


def _run_with(tmp_path):
    cur = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=None)
    db = MagicMock()
    db.connect.return_value.__enter__ = MagicMock(return_value=conn)
    db.connect.return_value.__exit__ = MagicMock(return_value=None)
    with patch.object(nuts_loader, "StatsDatabase", return_value=db):
        rc = nuts_loader.run(version="2024", src_dir=tmp_path)
    return rc, cur, conn


def test_run_upserts_every_feature_and_commits(tmp_path):
    _write_levels(tmp_path, {
        0: [{"properties": {"NUTS_ID": "PT", "LEVL_CODE": 0,
                            "NAME_LATN": "Portugal", "NAME": "Portugal"},
             "geometry": _square()}],
        1: [{"properties": {"NUTS_ID": "PT1", "LEVL_CODE": 1,
                            "NAME_LATN": "Continente", "NAME": "Continente"},
             "geometry": _square(2)}],
        2: [], 3: [],
    })
    rc, cur, conn = _run_with(tmp_path)
    assert rc == 0
    assert cur.execute.call_count == 2
    conn.commit.assert_called_once()


def test_run_derives_parent_and_country_from_the_code(tmp_path):
    """parent_code/country_of drive the FK and the country column; getting
    either wrong orphans the row or files it under the wrong country."""
    _write_levels(tmp_path, {
        0: [], 1: [{"properties": {"NUTS_ID": "PT1", "LEVL_CODE": 1,
                                   "NAME_LATN": "Continente"},
                    "geometry": _square()}],
        2: [], 3: [],
    })
    _, cur, _ = _run_with(tmp_path)
    params = cur.execute.call_args.args[1]
    assert params["code"] == "PT1"
    assert params["parent"] == "PT"
    assert params["country"] == "PT"
    assert params["version"] == "2024"
    assert params["valid_from"] == "2024-01-01"


def test_run_falls_back_to_nuts_name_when_latin_is_absent(tmp_path):
    _write_levels(tmp_path, {
        0: [{"properties": {"NUTS_ID": "EL", "LEVL_CODE": 0,
                            "NUTS_NAME": "Ελλάδα", "NAME": "Ελλάδα"},
             "geometry": _square()}],
        1: [], 2: [], 3: [],
    })
    _, cur, _ = _run_with(tmp_path)
    assert cur.execute.call_args.args[1]["name"] == "Ελλάδα"


def test_run_skips_features_without_a_code_or_geometry(tmp_path):
    """A feature with no NUTS_ID has no primary key, and one whose geometry
    will not coerce would insert NULL into a NOT NULL column."""
    _write_levels(tmp_path, {
        0: [
            {"properties": {}, "geometry": _square()},
            {"properties": {"NUTS_ID": "XX"}, "geometry": {"type": "Point",
                                                           "coordinates": [1, 2]}},
            {"properties": {"NUTS_ID": "PT", "LEVL_CODE": 0,
                            "NAME_LATN": "Portugal"}, "geometry": _square()},
        ],
        1: [], 2: [], 3: [],
    })
    _, cur, _ = _run_with(tmp_path)
    assert cur.execute.call_count == 1
    assert cur.execute.call_args.args[1]["code"] == "PT"


def test_run_processes_levels_parents_first(tmp_path):
    """The parent_code FK requires the parent row to exist, so level order
    is load-bearing, not cosmetic."""
    _write_levels(tmp_path, {
        lvl: [{"properties": {"NUTS_ID": "PT" + "1" * lvl, "LEVL_CODE": lvl,
                              "NAME_LATN": f"L{lvl}"}, "geometry": _square(lvl)}]
        for lvl in (0, 1, 2, 3)
    })
    _, cur, _ = _run_with(tmp_path)
    codes = [c.args[1]["code"] for c in cur.execute.call_args_list]
    assert codes == ["PT", "PT1", "PT11", "PT111"]
