"""
Match Unlinked Entities
=======================
Finds SanctionedEntity and CohesionProject beneficiary nodes that lack
relationships to Company nodes, and attempts fuzzy matching via the
Neo4j full-text index ``company_name_ft``.

Creates ``SAME_AS`` edges with confidence scores and method tags so
that human reviewers can approve or reject matches.

Usage:
    python -m src.etl.match_unlinked --neo4j-uri bolt://localhost:7687
    python -m src.etl.match_unlinked --confidence-threshold 0.9 --batch-size 200
"""
from __future__ import annotations

import argparse
import logging
import os
import time

from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

CREATE_FT_INDEX = """
CREATE FULLTEXT INDEX company_name_ft IF NOT EXISTS
FOR (c:Company) ON EACH [c.name]
"""

# ── Unlinked sanctions ──────────────────────────────────────────

FETCH_UNLINKED_SANCTIONS = """
MATCH (s:SanctionedEntity)
WHERE s.name IS NOT NULL AND size(s.name) > 3
  AND NOT (s)-[:SANCTIONED|SAME_AS]-(:Company)
RETURN s.entity_id AS entity_id, s.name AS name
SKIP $offset LIMIT $limit
"""

MATCH_SANCTION_FUZZY = """
UNWIND $batch AS row
WITH row,
     reduce(n = row.name, c IN ['+','-','&&','||','!','(',')','{','}',
            '[',']','^','"','~','*','?',':','\\\\','/']
            | replace(n, c, ' ')) AS clean_name
WHERE size(trim(clean_name)) > 3
CALL db.index.fulltext.queryNodes('company_name_ft', clean_name)
     YIELD node AS c, score
WITH row, c, score
WHERE score > $min_score
WITH row, c, score ORDER BY score DESC
WITH row, collect({company: c, score: score})[0] AS best
WHERE best IS NOT NULL
  AND best.score / (size(row.name) * 0.1 + 1) > $threshold_scaled
MATCH (s:SanctionedEntity {entity_id: row.entity_id})
MERGE (s)-[r:SAME_AS]->(best.company)
SET r.confidence  = round(1000.0 * best.score / (size(row.name) * 0.1 + 1)) / 1000.0,
    r.method      = 'fulltext_sanctions',
    r.detected_at = datetime(),
    r.reviewed    = false
RETURN count(r) AS matched
"""

# ── Unlinked cohesion beneficiaries ─────────────────────────────

FETCH_UNLINKED_BENEFICIARIES = """
MATCH (c:Company)-[:BENEFICIARY_OF]->(:CohesionProject)
WHERE c.name IS NOT NULL AND size(c.name) > 3
  AND NOT (c)-[:SAME_AS]-(:Company)
  AND NOT exists {
      MATCH (c) WHERE c.lei IS NOT NULL
  }
RETURN DISTINCT c.gmr_id AS gmr_id, c.name AS name
SKIP $offset LIMIT $limit
"""

MATCH_BENEFICIARY_FUZZY = """
UNWIND $batch AS row
WITH row,
     reduce(n = row.name, c IN ['+','-','&&','||','!','(',')','{','}',
            '[',']','^','"','~','*','?',':','\\\\','/']
            | replace(n, c, ' ')) AS clean_name
WHERE size(trim(clean_name)) > 3
CALL db.index.fulltext.queryNodes('company_name_ft', clean_name)
     YIELD node AS target, score
WITH row, target, score
WHERE target.gmr_id <> row.gmr_id
  AND score > $min_score
WITH row, target, score ORDER BY score DESC
WITH row, collect({company: target, score: score})[0] AS best
WHERE best IS NOT NULL
  AND best.score / (size(row.name) * 0.1 + 1) > $threshold_scaled
MATCH (src:Company {gmr_id: row.gmr_id})
MERGE (src)-[r:SAME_AS]->(best.company)
SET r.confidence  = round(1000.0 * best.score / (size(row.name) * 0.1 + 1)) / 1000.0,
    r.method      = 'fulltext_beneficiary',
    r.detected_at = datetime(),
    r.reviewed    = false
RETURN count(r) AS matched
"""


def _process_batches(session, fetch_query, match_query, batch_size,
                     confidence_threshold):
    """Fetch unlinked entities and run fuzzy matching in batches."""
    offset = 0
    total_matched = 0
    # Scale threshold for the Lucene score normalization
    min_score = 1.0
    threshold_scaled = confidence_threshold * 0.5

    while True:
        records = list(session.run(
            fetch_query, offset=offset, limit=batch_size,
        ))
        if not records:
            break

        batch = [dict(r) for r in records]
        result = session.run(
            match_query,
            batch=batch,
            min_score=min_score,
            threshold_scaled=threshold_scaled,
        )
        summary = result.consume()
        matched = summary.counters.relationships_created
        total_matched += matched

        offset += batch_size
        logger.info(
            "  offset %d: %d candidates, %d matched",
            offset, len(batch), matched,
        )

    return total_matched


def run_matching(driver, batch_size, confidence_threshold):
    """Run all fuzzy matching passes."""
    t0 = time.time()

    with driver.session() as session:
        session.run(CREATE_FT_INDEX)
        logger.info("Full-text index ensured")

        logger.info("Matching unlinked SanctionedEntity nodes ...")
        sanctions_matched = _process_batches(
            session,
            FETCH_UNLINKED_SANCTIONS,
            MATCH_SANCTION_FUZZY,
            batch_size,
            confidence_threshold,
        )
        logger.info("  sanctions matched: %d", sanctions_matched)

        logger.info("Matching unlinked CohesionProject beneficiaries ...")
        beneficiaries_matched = _process_batches(
            session,
            FETCH_UNLINKED_BENEFICIARIES,
            MATCH_BENEFICIARY_FUZZY,
            batch_size,
            confidence_threshold,
        )
        logger.info("  beneficiaries matched: %d", beneficiaries_matched)

    elapsed = time.time() - t0
    return {
        "sanctions_matched": sanctions_matched,
        "beneficiaries_matched": beneficiaries_matched,
        "elapsed_s": round(elapsed, 1),
    }


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Match unlinked entities via fuzzy name matching",
    )
    parser.add_argument(
        "--batch-size", type=int, default=500,
        help="Number of entities to process per batch (default: 500)",
    )
    parser.add_argument(
        "--confidence-threshold", type=float, default=0.85,
        help="Minimum confidence score for SAME_AS edges (default: 0.85)",
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
        summary = run_matching(driver, args.batch_size,
                               args.confidence_threshold)
    finally:
        driver.close()

    logger.info(
        "Done: %d sanctions matched, %d beneficiaries matched in %.1fs",
        summary["sanctions_matched"],
        summary["beneficiaries_matched"],
        summary["elapsed_s"],
    )


if __name__ == "__main__":
    main()
