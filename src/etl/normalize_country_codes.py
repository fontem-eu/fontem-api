"""
Country Code Normalization → ISO alpha-3 (via LocationService)
==============================================================
Migrates all Company and Authority nodes from ISO alpha-2
to ISO alpha-3 country codes using the LocationService.

This is idempotent: running it twice is safe because alpha-3 codes
are left unchanged by ``to_alpha3()``.

Usage:
    python -m src.etl.normalize_country_codes --neo4j-uri bolt://localhost:7687
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from collections import Counter

from neo4j import GraphDatabase

from src.services.location_service import LocationService

logger = logging.getLogger(__name__)

FETCH_COUNTRIES = """
MATCH (n:{label})
WHERE n.country IS NOT NULL AND size(n.country) = 2
RETURN DISTINCT n.country AS code, count(n) AS cnt
"""


def normalize_graph(driver):
    """Normalize all alpha-2 country codes to alpha-3 in the graph.

    Processes one country code at a time to stay within transaction
    memory limits.  Returns a dict with per-label conversion counts.
    """
    t0 = time.time()
    totals: Counter = Counter()

    for label in ("Company", "Authority"):
        query_fetch = FETCH_COUNTRIES.format(label=label)
        with driver.session() as session:
            records = list(session.run(query_fetch))

        for record in records:
            a2 = record["code"]
            cnt = record["cnt"]
            a3 = LocationService.to_alpha3(a2)
            if a3 is None or a3 == a2:
                continue

            with driver.session() as session:
                result = session.run(
                    f"MATCH (n:{label} {{country: $a2}}) "
                    "SET n.country = $a3 "
                    "RETURN count(n) AS updated",
                    a2=a2, a3=a3,
                ).single()
                updated = result["updated"]

            if updated > 0:
                totals[label] += updated
                logger.info(
                    "  %s %s -> %s: %d nodes (expected %d)",
                    label, a2, a3, updated, cnt,
                )

    elapsed = time.time() - t0
    for label in ("Company", "Authority"):
        logger.info("%s normalized: %d", label, totals.get(label, 0))
    logger.info("Done in %.1fs", elapsed)
    return dict(totals)


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Normalize country codes to ISO alpha-3 via LocationService",
    )
    parser.add_argument(
        "--neo4j-uri",
        default=os.environ.get("NEO4J_URI", "bolt://neo4j:7687"),
    )
    parser.add_argument(
        "--neo4j-user",
        default=os.environ.get("NEO4J_USER", "neo4j"),
    )
    parser.add_argument(
        "--neo4j-password",
        default=os.environ.get("NEO4J_PASSWORD", ""),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password),
    )
    try:
        normalize_graph(driver)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
