"""
Company Deduplication Script
==============================
Finds duplicate Company nodes (same name + country, different gmr_id)
and either auto-merges them (no property conflicts) or creates [:SAME_AS]
relationships for manual review.

Usage:
    python -m src.etl.dedup_companies --neo4j-uri bolt://localhost:7687
"""
from __future__ import annotations

import argparse
import logging
import os
import time

from neo4j import GraphDatabase

logger = logging.getLogger(__name__)


def find_and_resolve_duplicates(driver, dry_run: bool = False):
    """Find exact-name+country duplicates and resolve them."""
    t0 = time.time()
    auto_merged = 0
    deferred = 0
    junk_cleaned = 0

    with driver.session() as session:
        # Step 0: Clean junk VATs (0, empty, < 3 chars)
        if not dry_run:
            result = session.run(
                "MATCH (c:Company) "
                "WHERE c.vat IS NOT NULL AND (c.vat = '0' OR size(c.vat) < 3) "
                "SET c.vat = null "
                "RETURN count(c) AS n"
            ).single()
            junk_cleaned = result["n"]
            logger.info("Cleaned %d junk VAT values", junk_cleaned)

        # Step 1: Find duplicates where one has LEI and others don't
        # (LEI-bearing node is always canonical)
        dupes = session.run(
            "MATCH (canonical:Company), (dup:Company) "
            "WHERE canonical.name = dup.name "
            "  AND canonical.country = dup.country "
            "  AND canonical.gmr_id < dup.gmr_id "
            "  AND canonical.lei IS NOT NULL "
            "  AND dup.lei IS NULL "
            "RETURN canonical.gmr_id AS can_id, canonical.name AS name, "
            "  canonical.country AS country, "
            "  dup.gmr_id AS dup_id, "
            "  canonical.vat AS can_vat, dup.vat AS dup_vat "
            "LIMIT 5000"
        ).data()
        logger.info("Found %d duplicate pairs (LEI vs no-LEI)", len(dupes))

        for pair in dupes:
            can_vat = pair.get("can_vat")
            dup_vat = pair.get("dup_vat")

            # Check for property conflicts
            has_conflict = (
                can_vat is not None
                and dup_vat is not None
                and can_vat != dup_vat
            )

            if has_conflict:
                # Defer: create SAME_AS for manual review
                if not dry_run:
                    session.run(
                        "MATCH (dup:Company {gmr_id: $dup_id}), "
                        "  (can:Company {gmr_id: $can_id}) "
                        "MERGE (dup)-[:SAME_AS {"
                        "  confidence: 0.9, method: 'exact_name_country', "
                        "  detected_at: datetime(), reviewed: false"
                        "}]->(can)",
                        dup_id=pair["dup_id"],
                        can_id=pair["can_id"],
                    )
                deferred += 1
            else:
                # Auto-merge: no conflicts
                if not dry_run:
                    # Audit node
                    session.run(
                        "MATCH (dup:Company {gmr_id: $dup_id}) "
                        "CREATE (:MergeEvent {"
                        "  canonical_id: $can_id, merged_id: $dup_id, "
                        "  merged_at: datetime(), method: 'dedup_auto', "
                        "  dup_name: dup.name, dup_country: dup.country, "
                        "  dup_vat: dup.vat"
                        "})",
                        dup_id=pair["dup_id"],
                        can_id=pair["can_id"],
                    )
                    # Merge nodes
                    session.run(
                        "MATCH (dup:Company {gmr_id: $dup_id}), "
                        "  (can:Company {gmr_id: $can_id}) "
                        "CALL apoc.refactor.mergeNodes("
                        "  [can, dup], "
                        "  {properties: 'combine', mergeRels: true}"
                        ") YIELD node "
                        "SET node.gmr_id = $can_id "
                        "RETURN node",
                        dup_id=pair["dup_id"],
                        can_id=pair["can_id"],
                    )
                auto_merged += 1

            if (auto_merged + deferred) % 100 == 0 and (auto_merged + deferred) > 0:
                logger.info(
                    "  Progress: %d merged, %d deferred",
                    auto_merged, deferred,
                )

    elapsed = time.time() - t0
    logger.info(
        "Done: %d auto-merged, %d deferred to SAME_AS, "
        "%d junk VATs cleaned in %.1fs",
        auto_merged, deferred, junk_cleaned, elapsed,
    )
    return {
        "auto_merged": auto_merged,
        "deferred": deferred,
        "junk_cleaned": junk_cleaned,
    }


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Deduplicate Company nodes",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Report duplicates without merging")
    parser.add_argument("--neo4j-uri", default=os.environ.get("NEO4J_URI", "bolt://neo4j:7687"))
    parser.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", "gmr-neo4j-2026"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    driver = GraphDatabase.driver(args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password))
    try:
        find_and_resolve_duplicates(driver, dry_run=args.dry_run)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
