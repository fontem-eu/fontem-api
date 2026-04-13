"""
Materialize CLIENT_OF / SUPPLIER_OF from AWARDED + AWARDED_TO
==============================================================
Creates aggregated trade relationships between Authority and Company
nodes by traversing the contract graph:

  (Authority)-[:AWARDED]->(Contract)-[:AWARDED_TO]->(Company)
  =>
  (Authority)-[:CLIENT_OF {contracts, total_eur}]->(Company)
  (Company)-[:SUPPLIER_OF {contracts, total_eur}]->(Authority)

These summary edges make the graph explorer cleaner — instead of
hundreds of individual contract edges, one weighted CLIENT_OF edge
with contract count and total value.

Usage:
    python -m src.etl.materialize_trade_edges
    python -m src.etl.materialize_trade_edges --neo4j-uri bolt://localhost:7687
"""
from __future__ import annotations

import argparse
import logging
import os
import time

from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

BATCH_SIZE = 2000


def materialize(driver):
    """Create CLIENT_OF and SUPPLIER_OF edges from contract data."""
    t0 = time.time()

    with driver.session() as session:
        # Drop existing edges so we can re-run idempotently
        logger.info("Dropping existing CLIENT_OF / SUPPLIER_OF edges...")
        session.run("MATCH ()-[r:CLIENT_OF]->() DELETE r")
        session.run("MATCH ()-[r:SUPPLIER_OF]->() DELETE r")

        # Aggregate: for each (authority, company) pair, count contracts
        # and sum values, then create the summary edges.
        logger.info("Aggregating authority→company trade pairs...")
        result = session.run("""
            MATCH (a:Authority)-[:AWARDED]->(ct:Contract)-[:AWARDED_TO]->(c:Company)
            WITH a, c,
                 count(ct) AS contracts,
                 sum(COALESCE(ct.value_eur, 0)) AS total_eur,
                 min(ct.publication_date) AS earliest,
                 max(ct.publication_date) AS latest
            RETURN a.authority_id AS auth_id,
                   c.gmr_id AS company_id,
                   contracts, total_eur, earliest, latest
        """).data()

        logger.info("Found %d trade pairs", len(result))

        # Batch-create CLIENT_OF edges
        for i in range(0, len(result), BATCH_SIZE):
            batch = result[i:i + BATCH_SIZE]
            session.run("""
                UNWIND $batch AS row
                MATCH (a:Authority {authority_id: row.auth_id})
                MATCH (c:Company {gmr_id: row.company_id})
                CREATE (a)-[:CLIENT_OF {
                    contracts: row.contracts,
                    total_eur: row.total_eur,
                    earliest: row.earliest,
                    latest: row.latest
                }]->(c)
                CREATE (c)-[:SUPPLIER_OF {
                    contracts: row.contracts,
                    total_eur: row.total_eur,
                    earliest: row.earliest,
                    latest: row.latest
                }]->(a)
            """, batch=batch)
            if (i + BATCH_SIZE) % 10000 < BATCH_SIZE:
                logger.info("  %d pairs processed", i + len(batch))

        elapsed = time.time() - t0
        logger.info(
            "Done: %d CLIENT_OF + %d SUPPLIER_OF edges in %.1fs",
            len(result), len(result), elapsed,
        )
        return {"pairs": len(result), "elapsed_s": round(elapsed, 1)}


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Materialize CLIENT_OF / SUPPLIER_OF edges",
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
        materialize(driver)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
