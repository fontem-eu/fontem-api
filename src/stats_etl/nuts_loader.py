"""One-off loader for NUTS region polygons → PostGIS.

Pulls the GISCO NUTS GeoJSON for all four levels at the given revision,
upserts into `fontem_stats.nuts_region`. Idempotent: re-runs replace
geometry without disturbing FK references.

GISCO publishes GeoJSON at multiple resolutions; we use 1:60M for
display-quality without stratospheric file size (the 1:1M variant is
~120 MB; 1:60M is ~2 MB).
"""
from __future__ import annotations

import logging

import httpx

from .db import StatsDatabase
from .geo_levels import country_of, parent_code

logger = logging.getLogger(__name__)

GISCO_URL = (
    "https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/"
    "NUTS_RG_60M_{version}_4326_LEVL_{level}.geojson"
)


def _fetch_level(version: str, level: int) -> dict:
    url = GISCO_URL.format(version=version, level=level)
    logger.info("fetching NUTS-%d polygons (%s)", level, url)
    r = httpx.get(url, timeout=120.0,
                  headers={"User-Agent": "fontem-stats/0.1"})
    r.raise_for_status()
    return r.json()


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


def run(version: str = "2024") -> int:
    db = StatsDatabase()
    total = 0
    with db.connect() as conn, conn.cursor() as cur:
        # Upsert in two passes: parents first, then children — the FK
        # on parent_code requires the parent row to already exist.
        for level in (0, 1, 2, 3):
            geo = _fetch_level(version, level)
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
