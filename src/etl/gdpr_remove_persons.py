"""
GDPR: Remove personal data from Neo4j
=======================================
One-time migration script that deletes all Person nodes, DIRECTS
relationships, and person-type SanctionedEntity nodes from the graph.

Idempotent — safe to run multiple times.

Usage:
    python -m src.etl.gdpr_remove_persons --neo4j-uri bolt://localhost:7687
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from neo4j import GraphDatabase

logger = logging.getLogger(__name__)


def remove_personal_data(driver):
    """Delete Person nodes, DIRECTS relationships, and person-type sanctions."""
    with driver.session() as session:
        # 1. Delete all DIRECTS relationships
        result = session.run(
            "MATCH ()-[r:DIRECTS]->() DELETE r RETURN count(r) AS n"
        )
        directs_count = result.single()["n"]
        logger.info("Deleted %d DIRECTS relationships", directs_count)

        # 2. Delete all Person nodes
        result = session.run(
            "MATCH (p:Person) DETACH DELETE p RETURN count(p) AS n"
        )
        person_count = result.single()["n"]
        logger.info("Deleted %d Person nodes", person_count)

        # 3. Delete person-type SanctionedEntity nodes and their SANCTIONED rels
        result = session.run(
            "MATCH (s:SanctionedEntity {entity_type: 'person'}) "
            "DETACH DELETE s RETURN count(s) AS n"
        )
        sanctioned_person_count = result.single()["n"]
        logger.info(
            "Deleted %d person-type SanctionedEntity nodes",
            sanctioned_person_count,
        )

    return {
        "directs_deleted": directs_count,
        "persons_deleted": person_count,
        "sanctioned_persons_deleted": sanctioned_person_count,
    }


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="GDPR: Remove personal data (Person nodes, DIRECTS rels, "
                    "person-type sanctions) from Neo4j"
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
        summary = remove_personal_data(driver)
    except (OSError, RuntimeError):
        logger.exception("Failed to remove personal data")
        sys.exit(1)
    finally:
        driver.close()

    logger.info(
        "Done: %d DIRECTS, %d Person nodes, %d sanctioned persons removed",
        summary["directs_deleted"],
        summary["persons_deleted"],
        summary["sanctioned_persons_deleted"],
    )


if __name__ == "__main__":
    main()
