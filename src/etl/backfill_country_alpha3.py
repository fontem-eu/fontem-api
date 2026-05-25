"""
One-shot backfill: Company / Authority / Lobbyist country → alpha-3
====================================================================
Earlier versions of the GLEIF / sanctions / TED loaders wrote the
``country`` property raw from upstream — ISO 3166-1 alpha-2. The
internal convention is alpha-3, so every downstream join (entity
linker against :NUTSRegion, location-service map lookups, stats
country joins) silently missed.

The loaders are fixed; this script retrofits the existing data.
Looks up alpha-2 → alpha-3 in Python (so we use the same pycountry
+ EL/UK/XK overrides that ``LocationService`` knows about), batches
into Neo4j via UNWIND, and updates per-batch in its own transaction
so a partial failure leaves the cluster in a valid state and re-runs
just pick up where they left off.

Usage:
    python -m src.etl.backfill_country_alpha3
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from neo4j import GraphDatabase

from src.services.location_service import LocationService

logger = logging.getLogger(__name__)

LABELS = ("Company", "Authority", "Lobbyist")

# Page in length-2 rows; per-batch UNWIND below. Combined with the
# CALL ... IN TRANSACTIONS auto-commit the per-tx memory stays small.
BATCH_SIZE = 10_000

_FETCH_ALPHA2_ROWS = """
MATCH (e:{label}) WHERE e.country IS NOT NULL AND size(e.country) = 2
RETURN elementId(e) AS eid, toUpper(e.country) AS a2
"""

# UNWIND-driven update: we resolve alpha-3 in Python and send the
# (eid, a3) pairs back. Pure-Cypher conversion would require a 250-row
# map literal in the query — cleaner to keep the lookup in code.
_SET_ALPHA3 = """
UNWIND $rows AS row
MATCH (e) WHERE elementId(e) = row.eid
SET e.country = row.a3
"""


def _backfill_label(session, label: str) -> dict:
    """Stream rows for one label, look up alpha-3, write back in batches."""
    fetch = _FETCH_ALPHA2_ROWS.format(label=label)
    candidates = iter(session.run(fetch))

    batch: list[dict] = []
    converted = 0
    unknown = 0
    while True:
        rec = next(candidates, None)
        if rec is None:
            break
        a3 = LocationService.alpha2_to_alpha3(rec["a2"])
        if not a3:
            unknown += 1
            continue
        batch.append({"eid": rec["eid"], "a3": a3})
        if len(batch) >= BATCH_SIZE:
            session.run(_SET_ALPHA3, rows=batch).consume()
            converted += len(batch)
            batch = []
    if batch:
        session.run(_SET_ALPHA3, rows=batch).consume()
        converted += len(batch)

    logger.info("  %s: converted=%d, unknown_a2=%d", label, converted, unknown)
    return {"converted": converted, "unknown": unknown}


def run(driver) -> dict:
    t0 = time.time()
    summary: dict[str, dict] = {}
    with driver.session() as session:
        for label in LABELS:
            logger.info("Backfilling %s.country (alpha-2 → alpha-3) ...", label)
            summary[label] = _backfill_label(session, label)
    summary["elapsed_s"] = round(time.time() - t0, 1)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Backfill alpha-2 country strings on entity nodes to alpha-3",
    )
    parser.add_argument("--neo4j-uri", default=os.environ.get(
        "NEO4J_URI", "bolt://neo4j:7687"))
    parser.add_argument("--neo4j-user", default=os.environ.get(
        "NEO4J_USER", "neo4j"))
    parser.add_argument("--neo4j-password", default=os.environ.get(
        "NEO4J_PASSWORD", ""))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not args.neo4j_password:
        print("NEO4J_PASSWORD is required", file=sys.stderr)
        return 1

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password),
    )
    try:
        summary = run(driver)
    finally:
        driver.close()
    logger.info("Done: %s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
