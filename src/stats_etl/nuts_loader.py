"""One-off loader for NUTS region polygons → PostGIS.

Reads the GISCO NUTS GeoJSON for all four levels from a vendored
directory, upserts into ``fontem_stats.nuts_region``. Idempotent:
re-runs replace geometry without disturbing FK references.

GISCO publishes GeoJSON at multiple resolutions; we ship 1:10M
(see ``data/nuts/polygons/``). The cluster has no egress to
``gisco-services.ec.europa.eu`` so we vendor the files and bump
them by hand when GISCO publishes a new NUTS vintage (roughly
every three years).
"""
from __future__ import annotations

import json
import logging
import pathlib

from .db import StatsDatabase
from .geo_levels import country_of, parent_code

logger = logging.getLogger(__name__)

VENDORED_DIR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "data" / "nuts" / "polygons"
)


def _load_level(version: str, level: int, src_dir: pathlib.Path) -> dict:
    path = src_dir / f"NUTS_RG_10M_{version}_4326_LEVL_{level}.geojson"
    logger.info("reading NUTS-%d polygons (%s)", level, path)
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _to_multipolygon_wkt(geometry: dict) -> str | None:
    """Coerce GeoJSON Polygon|MultiPolygon → WKT MultiPolygon."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return None
    if gtype == "Polygon":
        polys = [coords]
    elif gtype == "MultiPolygon":
        polys = coords
    else:
        return None

    def _ring(ring):
        return ", ".join(f"{x} {y}" for x, y, *_ in ring)

    def _poly(poly):
        return "(" + ", ".join(f"({_ring(r)})" for r in poly) + ")"

    return "MULTIPOLYGON(" + ", ".join(_poly(p) for p in polys) + ")"


def run(version: str = "2024", src_dir: pathlib.Path | None = None) -> int:
    src = src_dir or VENDORED_DIR
    db = StatsDatabase()
    total = 0
    with db.connect() as conn, conn.cursor() as cur:
        # Upsert in two passes: parents first, then children — the FK
        # on parent_code requires the parent row to already exist.
        for level in (0, 1, 2, 3):
            geo = _load_level(version, level, src)
            for feat in geo.get("features", []):
                props = feat.get("properties", {})
                code = props.get("NUTS_ID")
                if not code:
                    continue
                wkt = _to_multipolygon_wkt(feat.get("geometry", {}))
                if not wkt:
                    continue
                cur.execute(
                    """
                    INSERT INTO fontem_stats.nuts_region (
                        code, level, name, name_native, parent_code,
                        country_code, geometry, nuts_version, valid_from
                    )
                    VALUES (
                        %(code)s, %(level)s, %(name)s, %(name_native)s,
                        %(parent)s, %(country)s,
                        ST_Multi(ST_GeomFromText(%(wkt)s, 4326)),
                        %(version)s, %(valid_from)s
                    )
                    ON CONFLICT (code) DO UPDATE SET
                        level = EXCLUDED.level,
                        name = EXCLUDED.name,
                        name_native = EXCLUDED.name_native,
                        parent_code = EXCLUDED.parent_code,
                        country_code = EXCLUDED.country_code,
                        geometry = EXCLUDED.geometry,
                        nuts_version = EXCLUDED.nuts_version,
                        valid_from = EXCLUDED.valid_from,
                        updated_at = now()
                    """,
                    {
                        "code": code,
                        "level": props.get("LEVL_CODE", level),
                        "name": props.get("NAME_LATN") or props.get("NUTS_NAME"),
                        "name_native": props.get("NAME"),
                        "parent": parent_code(code),
                        "country": (country_of(code) or "??").upper(),
                        "wkt": wkt,
                        "version": version,
                        "valid_from": f"{version}-01-01",
                    },
                )
                total += 1
        conn.commit()
    logger.info("loaded %d NUTS regions (version %s)", total, version)
    print(f"loaded {total} NUTS regions (version {version})")
    return 0
