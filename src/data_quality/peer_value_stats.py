"""Peer value bands per (country, cpv4) — the injected data behind the
cleaning stage's ``generic.value_peer_outlier`` rule.

Nothing in a TED notice marks the monetary scale, so a x100 / x1000
leak that the milli-euro tiers cannot prove is catchable only as
implausibility against what contracts of the same kind, in the same
country, actually cost. This job computes those bands from the graph
(``:Contract.value_eur > 0``, grouped by buyer country and the first
four CPV digits; 27,424 groups over 2.8M contracts, 8,218 with n >= 30
on 2026-09-22) and upserts them into ``dq.peer_value_stats`` in the
events store, where the TED loader reads them at start-up. The table
is created on first run; until it exists the rule is simply inactive.

    python -m src.data_quality.peer_value_stats            # compute + upsert
    python -m src.data_quality.peer_value_stats --dry-run  # compute, print
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass

import psycopg
from neo4j import GraphDatabase

from src.etl.cleaning.lookups import InMemoryPeerStats, PeerBand

logger = logging.getLogger(__name__)

TABLE = "dq.peer_value_stats"
MIN_GROUP_SIZE = 30

DDL = """
CREATE SCHEMA IF NOT EXISTS dq;
CREATE TABLE IF NOT EXISTS dq.peer_value_stats (
    country     TEXT NOT NULL,
    cpv4        TEXT NOT NULL,
    n           INTEGER NOT NULL,
    p10         DOUBLE PRECISION,
    median      DOUBLE PRECISION,
    p90         DOUBLE PRECISION,
    p99         DOUBLE PRECISION,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (country, cpv4)
);
"""

# One row per (buyer country, cpv4). Quarantined contracts carry no
# value_eur, so they never enter a band; low-confidence values do, and
# percentiles absorb them.
CYPHER = """
MATCH (c:Contract)
WHERE c.value_eur > 0 AND c.cpv IS NOT NULL AND c.country IS NOT NULL
WITH c.country AS country, left(c.cpv, 4) AS cpv4, c.value_eur AS v
RETURN country, cpv4, count(*) AS n,
       percentileCont(v, 0.10) AS p10, percentileCont(v, 0.50) AS median,
       percentileCont(v, 0.90) AS p90, percentileCont(v, 0.99) AS p99
"""

UPSERT = """
INSERT INTO dq.peer_value_stats
    (country, cpv4, n, p10, median, p90, p99, computed_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (country, cpv4) DO UPDATE SET
    n = EXCLUDED.n, p10 = EXCLUDED.p10, median = EXCLUDED.median,
    p90 = EXCLUDED.p90, p99 = EXCLUDED.p99, computed_at = now()
"""

SELECT = "SELECT country, cpv4, n, p10, median, p90, p99 FROM dq.peer_value_stats"


@dataclass(frozen=True)
class PeerRow:  # pylint: disable=too-many-instance-attributes
    country: str
    cpv4: str
    n: int
    p10: float
    median: float
    p90: float
    p99: float

    def as_tuple(self) -> tuple:
        return (self.country, self.cpv4, self.n, self.p10, self.median,
                self.p90, self.p99)


def compute(session) -> list[PeerRow]:
    """Run the grouping query; a Neo4j session (or anything whose
    ``run`` yields mappings with the RETURN columns)."""
    return [
        PeerRow(
            country=str(rec["country"]), cpv4=str(rec["cpv4"]), n=int(rec["n"]),
            p10=float(rec["p10"]), median=float(rec["median"]),
            p90=float(rec["p90"]), p99=float(rec["p99"]),
        )
        for rec in session.run(CYPHER)
    ]


def upsert(conn, rows: list[PeerRow]) -> int:
    """Create the table if needed and upsert every row. Returns the
    number of rows written."""
    with conn.cursor() as cur:
        cur.execute(DDL)
        cur.executemany(UPSERT, [r.as_tuple() for r in rows])
    conn.commit()
    return len(rows)


def load_peer_stats(conn) -> InMemoryPeerStats | None:
    """Every band in the table, or None when the table does not exist
    yet (the rule is then inactive, never an error)."""
    try:
        with conn.cursor() as cur:
            cur.execute(SELECT)
            rows = cur.fetchall()
    except psycopg.errors.UndefinedTable:
        conn.rollback()
        return None
    return InMemoryPeerStats({
        (country, cpv4): PeerBand(n=int(n), p10=float(p10), median=float(median),
                                  p90=float(p90), p99=float(p99))
        for country, cpv4, n, p10, median, p90, p99 in rows
    })


def events_dsn() -> str | None:
    """The events store DSN in psycopg form, or None when unset."""
    dsn = os.environ.get("EVENTS_DATABASE_URL")
    if not dsn or "$(" in dsn:
        return None
    return (dsn.replace("postgresql+asyncpg://", "postgresql://")
               .replace("postgresql+psycopg://", "postgresql://"))


def load_peer_stats_from_env() -> InMemoryPeerStats | None:
    """What the TED loader calls at start-up. Logs once why the rule is
    inactive when it is; never raises."""
    dsn = events_dsn()
    if not dsn:
        logger.info("peer value stats: EVENTS_DATABASE_URL not set; "
                    "generic.value_peer_outlier inactive")
        return None
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            stats = load_peer_stats(conn)
    except psycopg.Error as exc:
        logger.warning("peer value stats: cannot read %s (%s); "
                       "generic.value_peer_outlier inactive", TABLE, exc)
        return None
    if stats is None:
        logger.info("peer value stats: %s not present yet; "
                    "generic.value_peer_outlier inactive", TABLE)
        return None
    logger.info("peer value stats: %d groups loaded from %s", len(stats), TABLE)
    return stats


def summarise(rows: list[PeerRow]) -> dict:
    return {
        "groups": len(rows),
        "groups_min_n": sum(1 for r in rows if r.n >= MIN_GROUP_SIZE),
        "contracts": sum(r.n for r in rows),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute per-(country, cpv4) contract value bands "
                    "and upsert them into dq.peer_value_stats",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute and print the summary; write nothing")
    parser.add_argument("--neo4j-uri",
                        default=os.environ.get("NEO4J_URI", "bolt://neo4j:7687"))
    parser.add_argument("--neo4j-user",
                        default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--neo4j-password",
                        default=os.environ.get("NEO4J_PASSWORD", ""))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password))
    try:
        with driver.session() as session:
            rows = compute(session)
    finally:
        driver.close()
    summary = summarise(rows)
    logger.info("peer value stats: %d groups, %d with n >= %d, %d contracts",
                summary["groups"], summary["groups_min_n"], MIN_GROUP_SIZE,
                summary["contracts"])
    if args.dry_run:
        print(json.dumps(summary))
        return 0
    dsn = events_dsn()
    if not dsn:
        logger.error("EVENTS_DATABASE_URL not set; nothing written")
        return 2
    with psycopg.connect(dsn) as conn:
        written = upsert(conn, rows)
    logger.info("peer value stats: %d rows upserted into %s", written, TABLE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
