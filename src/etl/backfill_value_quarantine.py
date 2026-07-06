"""One-off backfill: quarantine the already-rendered bad contract values.

The quarantine pipeline only fires on (re-)emitted notices, so contracts
already in the graph with a hard-flagged value keep it until they happen
to be re-published. This job closes that gap: it scans the graph for
contracts whose value_quality_flag is a quarantine reason but which still
carry monetary props, emits a corrective quarantine event per contract
(the sinks clear the props), and — for the review tier — snapshots the
claimed numbers into events.value_review.

Idempotent: re-running finds nothing left to do (the corrective event
removes value_eur, taking the contract out of the scan; the queue insert
is ON CONFLICT DO NOTHING).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid

from fontem_event_schemas import builders
from fontem_events import EventLog
from neo4j import GraphDatabase

from src.etl.contract_confidence import (
    QUARANTINE_AUTO_FLAGS,
    QUARANTINE_REVIEW_FLAGS,
)
from src.etl import value_review_queue

logger = logging.getLogger(__name__)

# Contracts already rendered with a quarantine-tier flag AND still
# carrying any monetary prop. Paged with a fresh read tx per batch —
# same discipline every prod scan uses (90s tx ceiling).
_SCAN = """
MATCH (ct:Contract)
WHERE ct.value_quality_flag IN $flags
  AND (ct.value_quality_flag <> 'implausible_magnitude'
       OR ct.value_confidence < 0.05)
  AND (ct.value_eur IS NOT NULL OR ct.value_original IS NOT NULL
       OR ct.estimated_value_eur IS NOT NULL
       OR ct.value_payable_eur IS NOT NULL)
RETURN ct.ted_notice_id AS ted_notice_id,
       ct.value_quality_flag AS flag,
       ct.value_eur AS value_eur,
       ct.value_original AS value_original,
       ct.value_currency AS value_currency,
       ct.estimated_value_eur AS estimated_value_eur,
       ct.value_payable_eur AS value_payable_eur
LIMIT $limit
"""


def backfill(driver, log: EventLog, batch_size: int = 500) -> dict:
    flags = sorted(QUARANTINE_REVIEW_FLAGS | QUARANTINE_AUTO_FLAGS)
    emitted = queued = 0
    while True:
        with driver.session() as session:
            rows = [dict(r) for r in session.run(
                _SCAN, flags=flags, limit=batch_size)]
        if not rows:
            break
        with log.batch(uuid.uuid4(), producer="backfill_value_quarantine") as emit:
            for row in rows:
                reason = row["flag"]
                if reason in QUARANTINE_REVIEW_FLAGS:
                    if value_review_queue.enqueue_default(
                        ted_notice_id=row["ted_notice_id"],
                        reason=reason,
                        claimed_value_eur=row["value_eur"],
                        claimed_value_original=row["value_original"],
                        claimed_currency=row["value_currency"],
                        claimed_estimated_eur=row["estimated_value_eur"],
                        claimed_payable_eur=row["value_payable_eur"],
                        detail="backfill of pre-quarantine rendering",
                    ):
                        queued += 1
                emit.upsert(
                    "UpsertContract",
                    iri=("http://data.fontem.eu/id/Contract/"
                         f"{row['ted_notice_id']}"),
                    domain="contract",
                    payload=builders.upsert_contract(
                        ted_notice_id=row["ted_notice_id"],
                        value_quarantined=True,
                        value_quarantine_reason=reason,
                    ),
                )
                emitted += 1
        logger.info("quarantine backfill: %d corrective events so far "
                    "(%d review rows queued)", emitted, queued)
    summary = {"emitted": emitted, "queued": queued}
    logger.info("Done: %s", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--neo4j-uri",
                        default=os.environ.get("NEO4J_URI",
                                               "bolt://neo4j:7687"))
    parser.add_argument("--neo4j-user",
                        default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--neo4j-password",
                        default=os.environ.get("NEO4J_PASSWORD", ""))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password))
    log = EventLog.from_env()
    try:
        backfill(driver, log, batch_size=args.batch_size)
    finally:
        driver.close()


if __name__ == "__main__":
    sys.exit(main())
