"""Link contract-modification notices to the award they modify.

eForms publishes contract modifications as their own notices
(``notice_type = "can-modif"``) that share the original award's
``procedure-identifier``. The TED loader stamps every contract with
``procedure_id``; this pass joins each modification to the award(s) under
the same procedure and emits a ``MODIFIES`` relationship
(modification -> original) through the event log, so BOTH sinks
materialise it. Idempotent — only not-yet-linked pairs are emitted — so
it is safe to re-run after every incremental load and as a standalone
backfill step.
"""
from __future__ import annotations

import logging
import os
import uuid

from fontem_event_schemas import builders
from fontem_events import EventLog
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

# A modification (can-modif, with a procedure_id) links to every contract
# under the same procedure that is NOT itself a modification and isn't
# already linked. Procedure-level linking: one procedure usually has a
# single award notice, and a modification applies to that procedure.
_PAIRS_QUERY = """
MATCH (m:Contract {notice_type: 'can-modif'})
WHERE m.procedure_id IS NOT NULL
MATCH (o:Contract)
WHERE o.procedure_id = m.procedure_id
  AND o.ted_notice_id <> m.ted_notice_id
  AND (o.notice_type IS NULL OR o.notice_type <> 'can-modif')
  AND NOT (m)-[:MODIFIES]->(o)
RETURN m.ted_notice_id AS mod_id, o.ted_notice_id AS orig_id
"""


def link_modifications(driver, log: EventLog, batch_size: int = 500) -> int:
    """Emit MODIFIES edges for every not-yet-linked (modification, award)
    pair that shares a procedure_id. Returns the number of edges emitted."""
    with driver.session() as session:
        pairs = [(r["mod_id"], r["orig_id"]) for r in session.run(_PAIRS_QUERY)]
    logger.info("MODIFIES linking: %d new (modification, award) pairs", len(pairs))
    emitted = 0
    for start in range(0, len(pairs), batch_size):
        chunk = pairs[start:start + batch_size]
        with log.batch(uuid.uuid4(), producer="link_ted_modifications") as emit:
            for mod_id, orig_id in chunk:
                mod_iri = f"http://data.fontem.eu/id/Contract/{mod_id}"
                orig_iri = f"http://data.fontem.eu/id/Contract/{orig_id}"
                emit.upsert(
                    "UpsertRelationship", iri=mod_iri, domain="contract",
                    payload=builders.upsert_relationship(
                        src_iri=mod_iri, dst_iri=orig_iri, predicate="modifies",
                    ),
                )
                emitted += 1
    logger.info("MODIFIES linking: %d edges emitted", emitted)
    return emitted


def main(argv=None):  # pylint: disable=unused-argument
    """CLI entry point for a standalone re-link pass."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )
    driver = GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://neo4j:7687"),
        auth=(
            os.environ.get("NEO4J_USER", "neo4j"),
            os.environ.get("NEO4J_PASSWORD", ""),
        ),
    )
    log = EventLog.from_env()
    try:
        link_modifications(driver, log)
    finally:
        log.close()
        driver.close()


if __name__ == "__main__":
    main()
