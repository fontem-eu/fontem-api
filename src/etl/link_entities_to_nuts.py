"""
Link entities to NUTS regions
=============================
Creates LOCATED_IN edges from Company, Authority, and Lobbyist nodes to the
matching NUTSRegion. Best-effort: matches at NUTS 0 (country) today because
none of those entity types carry a postal_code property yet. Finer-grained
linking will come once GLEIF/TED ETLs are extended to extract postal codes.

The match joins on ``country_alpha3``: entity ``country`` is stored as
alpha-3 (platform convention), and load_nuts populates ``country_alpha3``
on every NUTSRegion via LocationService.

Usage:
    python -m src.etl.link_entities_to_nuts
"""
from __future__ import annotations

import argparse
import logging
import os
import time

from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

# Labels linked by this ETL. CohesionProject already has its own linking in
# load_eu_knowledge_graph (via explicit nuts_code field).
ENTITY_LABELS = ("Company", "Authority", "Lobbyist")

LINK_LABEL_TEMPLATE = """
MATCH (e:{label})
WHERE e.country IS NOT NULL AND NOT (e)-[:LOCATED_IN]->(:NUTSRegion)
WITH e, e.country AS a3
MATCH (n:NUTSRegion {{level: 0, country_alpha3: a3}})
MERGE (e)-[:LOCATED_IN]->(n)
"""


def link_label(session, label: str) -> int:
    """Link all unlinked entities of a given label to their NUTS 0 region."""
    query = LINK_LABEL_TEMPLATE.format(label=label)
    result = session.run(query)
    return result.consume().counters.relationships_created


def run(driver) -> dict:
    """Link Company, Authority, and Lobbyist nodes to NUTS 0 regions."""
    t0 = time.time()
    counts = {}
    with driver.session() as session:
        for label in ENTITY_LABELS:
            logger.info("Linking %s nodes to NUTS 0 ...", label)
            created = link_label(session, label)
            counts[label] = created
            logger.info("  %s: %d LOCATED_IN edges created", label, created)
    return {
        "counts": counts,
        "elapsed_s": round(time.time() - t0, 1),
    }


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Link Company/Authority/Lobbyist nodes to their NUTS region"
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
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password)
    )
    try:
        summary = run(driver)
    finally:
        driver.close()

    logger.info(
        "Done: %s in %.1fs",
        summary["counts"],
        summary["elapsed_s"],
    )


if __name__ == "__main__":
    main()
