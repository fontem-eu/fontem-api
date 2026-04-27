"""Backfill NUTS hierarchy in Neo4j from the fontem_stats Postgres store.

Closes the long-standing CLAUDE.md gap ("only level 0 (39 countries),
need levels 1-3 (~1960 regions)") by reading authoritative NUTS data
from Postgres (loaded via stats_etl.nuts_loader) and MERGEing it into
the graph.

Idempotent — re-runs upsert without disturbing existing edges. Existing
:Company-[:LOCATED_IN]->:NUTSRegion relationships keep working; this
script only adds the NUTS-1/2/3 nodes and the :PART_OF chain between
levels.

Usage:
    python -m src.etl.sync_nuts_from_stats
    python -m src.etl.sync_nuts_from_stats --neo4j-uri bolt://localhost:7687
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s")

CONSTRAINT_CYPHER = """
CREATE CONSTRAINT nuts_region_code IF NOT EXISTS
FOR (n:NUTSRegion) REQUIRE n.code IS UNIQUE
"""

UPSERT_CYPHER = """
UNWIND $rows AS row
MERGE (n:NUTSRegion {code: row.code})
SET n.level        = row.level,
    n.name         = row.name,
    n.name_native  = row.name_native,
    n.country_code = row.country_code,
    n.area_km2     = row.area_km2,
    n.nuts_version = row.nuts_version
WITH n, row
WHERE row.parent_code IS NOT NULL
MATCH (p:NUTSRegion {code: row.parent_code})
MERGE (n)-[:PART_OF]->(p)
"""


def _normalize_url(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


def _fetch_nuts_rows(pg_dsn: str) -> list[dict]:
    with psycopg.connect(_normalize_url(pg_dsn)) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT code, level, name, name_native, parent_code,
                   country_code, area_km2, nuts_version
            FROM fontem_stats.nuts_region
            ORDER BY level, code
            """,
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--pg-dsn", default=os.environ.get("STATS_DATABASE_URL"))
    p.add_argument("--neo4j-uri", default=os.environ.get("NEO4J_URI", "bolt://neo4j:7687"))
    p.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USER", "neo4j"))
    p.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", ""))
    p.add_argument("--batch", type=int, default=500)
    args = p.parse_args(argv)

    if not args.pg_dsn:
        print("STATS_DATABASE_URL is required", file=sys.stderr)
        return 1

    rows = _fetch_nuts_rows(args.pg_dsn)
    if not rows:
        logger.warning("no rows in fontem_stats.nuts_region — "
                       "did you run `python -m src.stats_etl nuts-polygons` yet?")
        return 0

    by_level: dict[int, list[dict]] = {}
    for r in rows:
        by_level.setdefault(r["level"], []).append(r)

    driver = GraphDatabase.driver(
        args.neo4j_uri,
        auth=(args.neo4j_user, args.neo4j_password),
    )
    try:
        with driver.session() as sess:
            sess.run(CONSTRAINT_CYPHER)
            # Insert level by level so PART_OF edges always find the parent.
            for level in sorted(by_level):
                level_rows = by_level[level]
                logger.info("level %d: %d regions", level, len(level_rows))
                for i in range(0, len(level_rows), args.batch):
                    chunk = level_rows[i:i + args.batch]
                    sess.run(UPSERT_CYPHER, rows=chunk)
        logger.info("done — total %d regions across levels %s",
                    len(rows), sorted(by_level))
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
