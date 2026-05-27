"""
Link entities to NUTS regions
=============================
Creates LOCATED_IN edges from Company, Authority, and Lobbyist nodes to the
matching NUTSRegion. Two-pass:

1. **Postcode → NUTS-3** for entities that carry a ``postal_code``. The
   Eurostat PCODE → NUTS-3 lookup is loaded from the vendored
   ``data/nuts/PCODE_2025_NUTS-2024_v2.0.zip`` and joined in Python.
2. **Country → NUTS-0** fallback for entities with no postal_code or whose
   normalised postcode isn't in the PCODE table. The match joins on
   ``country_alpha3``: entity ``country`` is stored as alpha-3 (platform
   convention), and load_nuts populates ``country_alpha3`` on every
   NUTSRegion via LocationService.

Usage:
    python -m src.etl.link_entities_to_nuts
"""
from __future__ import annotations

import argparse
import logging
import os
import time

from neo4j import GraphDatabase

from src.etl._pcode import load_lookup, normalise
from src.services.location_service import LocationService

logger = logging.getLogger(__name__)

# Labels linked by this ETL. CohesionProject already has its own linking in
# load_eu_knowledge_graph (via explicit nuts_code field).
ENTITY_LABELS = ("Company", "Authority", "Lobbyist")

# Batch size for the postcode pass — small enough that one UNWIND doesn't
# overflow the per-tx memory cap, large enough that we don't pay round-trip
# latency on every Company.
POSTCODE_BATCH_SIZE = 10_000

# Page size for streaming candidate rows out of Neo4j. Same logic as the
# write batch — the bolt protocol streams cursor results so we don't keep
# the full result set resident.
POSTCODE_FETCH_PAGE = 50_000

# Pulls candidates that have a postcode + country and aren't already pinned
# to a NUTS-3 region. The not-already-linked check uses a non-existence
# pattern instead of LIMIT/SKIP so a partial previous run resumes cleanly.
_FETCH_POSTCODE_CANDIDATES = """
MATCH (e:{label})
WHERE e.postal_code IS NOT NULL
  AND e.country IS NOT NULL
  AND NOT (e)-[:LOCATED_IN]->(:NUTSRegion {{level: 3}})
RETURN elementId(e) AS eid, e.country AS a3, e.postal_code AS pc
"""

# UNWIND batch write: pre-resolved (entity_eid, nuts3_code) pairs MERGE
# LOCATED_IN edges into the matching :NUTSRegion. The MATCH on the NUTSRegion
# uses the new ``code`` lookup (level + code uniqueness is guaranteed by the
# nuts-region UNIQUE constraint in stats_etl).
_MERGE_POSTCODE_EDGES = """
UNWIND $rows AS row
MATCH (e) WHERE elementId(e) = row.eid
MATCH (n:NUTSRegion {code: row.nuts3})
MERGE (e)-[:LOCATED_IN]->(n)
"""

# Country pass: candidate fetch + UNWIND-merge, same shape as the
# postcode pass. The earlier version used a single CALL { ... } IN
# TRANSACTIONS OF 10000 ROWS statement, which let Neo4j run multiple
# batches concurrently — the per-batch transactions contended on the
# same handful of NUTS-0 nodes (only ~38 exist, but ~3M Companies all
# MERGE LOCATED_IN edges into them) and the run died with a Forseti
# deadlock partway through. Driving the batches sequentially from
# Python gives identical throughput on a single-writer Neo4j without
# the lock contention.
_FETCH_COUNTRY_CANDIDATES = """
MATCH (e:{label})
WHERE e.country IS NOT NULL
  AND NOT (e)-[:LOCATED_IN]->(:NUTSRegion)
RETURN elementId(e) AS eid, e.country AS a3
"""

_MERGE_COUNTRY_EDGES = """
UNWIND $rows AS row
MATCH (e) WHERE elementId(e) = row.eid
MATCH (n:NUTSRegion {level: 0, country_alpha3: row.a3})
MERGE (e)-[:LOCATED_IN]->(n)
"""

COUNTRY_BATCH_SIZE = 10_000

_COUNT_LABEL_TEMPLATE = (
    "MATCH (:{label})-[r:LOCATED_IN]->(:NUTSRegion) RETURN count(r) AS n"
)


def _resolve_postcode_rows(
    candidates,
    pcode_lookup: dict[tuple[str, str], str],
) -> list[dict]:
    """In-memory join of Neo4j candidate rows against the PCODE lookup.

    Returns a list of ``{"eid": <neo4j elementId>, "nuts3": <code>}`` dicts
    ready for UNWIND. Candidates whose alpha-3 country can't be mapped to
    alpha-2 (sanctioned-out, unknown) or whose normalised postcode isn't in
    the table are simply skipped — the country-level fallback will catch
    them in the second pass.
    """
    out: list[dict] = []
    for rec in candidates:
        a2 = LocationService.alpha3_to_alpha2(rec["a3"])
        if not a2:
            continue
        nuts3 = pcode_lookup.get((a2, normalise(rec["pc"])))
        if not nuts3:
            continue
        out.append({"eid": rec["eid"], "nuts3": nuts3})
    return out


def link_label_postcode(  # pylint: disable=too-many-locals
    driver,
    label: str,
    pcode_lookup: dict[tuple[str, str], str],
) -> int:
    """Postcode → NUTS-3 pass for one label.

    Uses TWO sessions: one for the read cursor that streams candidates,
    one for the UNWIND writes. Running a write on the same session that
    has an open read result forces the driver to fully materialise the
    read into memory before the write begins — fatal at 3M+ rows
    (~1.5 GB Python heap, OOMKilled with a 2 GiB cronjob limit). Two
    sessions keep the read cursor genuinely streaming.

    Returns the number of MERGEd edges (after − before diff, since
    UNWIND-MERGE summaries don't separate ``created`` from ``matched``
    reliably across driver versions).
    """
    count_query = _COUNT_LABEL_TEMPLATE.format(label=label)
    fetch_query = _FETCH_POSTCODE_CANDIDATES.format(label=label)

    with driver.session() as count_sess:
        before = count_sess.run(count_query).single()["n"]

    batch: list[dict] = []
    written = 0
    with driver.session() as read_sess, driver.session() as write_sess:
        candidates_iter = iter(read_sess.run(fetch_query))
        while True:
            page = []
            for _ in range(POSTCODE_FETCH_PAGE):
                rec = next(candidates_iter, None)
                if rec is None:
                    break
                page.append(rec)
            if not page:
                break
            resolved = _resolve_postcode_rows(page, pcode_lookup)
            for row in resolved:
                batch.append(row)
                if len(batch) >= POSTCODE_BATCH_SIZE:
                    write_sess.run(_MERGE_POSTCODE_EDGES, rows=batch).consume()
                    written += len(batch)
                    batch = []
        if batch:
            write_sess.run(_MERGE_POSTCODE_EDGES, rows=batch).consume()
            written += len(batch)

    with driver.session() as count_sess:
        after = count_sess.run(count_query).single()["n"]
    logger.info("  %s postcode pass: matched %d candidates, edges Δ=%d",
                label, written, after - before)
    return after - before


def link_label_country(driver, label: str) -> int:  # pylint: disable=too-many-locals
    """Country-level (NUTS-0) fallback for entities the postcode pass missed.

    Mirrors ``link_label_postcode``'s two-session UNWIND-batch shape:
    stream candidates on a read session, send sequential UNWIND
    batches on a write session. Sequential batching is intentional —
    only ~38 NUTS-0 nodes exist; running batches in parallel (the old
    CALL IN TRANSACTIONS shape) caused Forseti deadlocks where two
    batches MERGE'd the same LOCATED_IN edge target concurrently.
    Single-writer Neo4j can't really exploit batch parallelism here
    anyway, so we trade nothing for crash-free behaviour.
    """
    count_query = _COUNT_LABEL_TEMPLATE.format(label=label)
    fetch_query = _FETCH_COUNTRY_CANDIDATES.format(label=label)

    with driver.session() as count_sess:
        before = count_sess.run(count_query).single()["n"]

    batch: list[dict] = []
    written = 0
    with driver.session() as read_sess, driver.session() as write_sess:
        for rec in read_sess.run(fetch_query):
            batch.append({"eid": rec["eid"], "a3": rec["a3"]})
            if len(batch) >= COUNTRY_BATCH_SIZE:
                write_sess.run(_MERGE_COUNTRY_EDGES, rows=batch).consume()
                written += len(batch)
                batch = []
        if batch:
            write_sess.run(_MERGE_COUNTRY_EDGES, rows=batch).consume()
            written += len(batch)

    with driver.session() as count_sess:
        after = count_sess.run(count_query).single()["n"]
    logger.info("  %s country pass: matched %d candidates, edges Δ=%d",
                label, written, after - before)
    return after - before


def run(driver, pcode_lookup: dict[tuple[str, str], str] | None = None) -> dict:
    """Two-pass link of Company, Authority, and Lobbyist nodes.

    ``pcode_lookup`` is loaded lazily from the vendored zip if not provided
    (tests inject a small fixture dict to avoid the disk read).
    """
    if pcode_lookup is None:
        pcode_lookup = load_lookup()

    t0 = time.time()
    postcode_counts: dict[str, int] = {}
    country_counts: dict[str, int] = {}
    # Both passes own their sessions per call (one read + one write each)
    # to keep the bolt cursor genuinely streaming and to drive batches
    # sequentially without the in-Cypher CALL-IN-TRANSACTIONS deadlock.
    for label in ENTITY_LABELS:
        logger.info("Linking %s nodes via postcode (NUTS-3) ...", label)
        postcode_counts[label] = link_label_postcode(
            driver, label, pcode_lookup,
        )
    for label in ENTITY_LABELS:
        logger.info("Linking %s nodes via country (NUTS-0 fallback) ...",
                    label)
        country_counts[label] = link_label_country(driver, label)
    return {
        "postcode_counts": postcode_counts,
        "country_counts": country_counts,
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
        "Done: postcode=%s, country=%s in %.1fs",
        summary["postcode_counts"],
        summary["country_counts"],
        summary["elapsed_s"],
    )


if __name__ == "__main__":
    main()
