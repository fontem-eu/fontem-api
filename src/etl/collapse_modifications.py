"""Materialise a per-contract ``current_value`` that collapses modification chains.

TED publishes every contract *modification* as its own notice
(``notice_type = "can-modif"``) that **restates the full contract value**
(not a delta). The loader stores each notice as its own :Contract node with
its own ``AWARDED_TO`` edge, so summing ``value_eur`` over a company's awards
counts each modification on top of the original award — a large over-count
(a contract modified twice is counted three times).

This pass derives, per distinct underlying contract, a single canonical node
carrying:

* ``is_current`` — exactly one node per contract is ``true``; aggregations
  sum over these only.
* ``current_value`` — the latest *restated* value (award value when the
  contract was never modified).
* ``contract_key`` — the stable contract identity the collapse grouped on.

Three cohorts (chains are depth-1 in the data — a modification points straight
at an award, never at another modification):

1. **Award with modifications** — canonical (``is_current=true``);
   ``current_value`` = latest non-null restated value across the award and its
   linked modifications.
2. **Linked modification** (has a ``MODIFIES`` edge) — superseded
   (``is_current=false``); it shares its award's ``contract_key``.
3. **Orphan modification** (no ``MODIFIES`` edge — its award was never
   ingested; ~4/5 of modifications) — grouped by
   ``coalesce(procedure_id, modifies_publication_number)``; the latest notice in
   the group is canonical, the rest superseded. This keeps the contract in the
   totals exactly once instead of dropping it.

Awards that were never modified are intentionally **not** emitted: readers
default an un-stamped, non-modification node to canonical with
``value_eur``, so the ~4/5 of contracts with no modification need no backfill.

Emits ``UpsertContract`` partial updates through the event log so BOTH sinks
(Neo4j and Virtuoso) materialise the fields. Idempotent — safe to re-run after
every incremental load and as a standalone backfill.
"""
from __future__ import annotations

import argparse
import logging
import os
import uuid

from fontem_event_schemas import builders
from fontem_events import EventLog
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

_MOD = "can-modif"

# Cohort 1: awards that have >=1 modification. current_value = latest non-null
# restated value across the award + its modifications (mods restate the full
# value, so the most recent notice by publication_date wins; fall back to the
# award's own value when every modification withheld a value).
_AWARDS_WITH_MODS = """
MATCH (a:Contract)
WHERE (a.notice_type IS NULL OR a.notice_type <> $mod)
  AND EXISTS { (:Contract)-[:MODIFIES]->(a) }
MATCH (m:Contract)-[:MODIFIES]->(a)
WITH a, [x IN collect(m) + [a] WHERE x.value_eur IS NOT NULL] AS valued
CALL {
  WITH valued
  UNWIND valued AS x
  WITH x ORDER BY x.publication_date DESC
  RETURN collect(x.value_eur)[0] AS latest_value
}
RETURN a.ted_notice_id AS id,
       coalesce(a.procedure_id, a.ted_publication_number, a.ted_notice_id) AS contract_key,
       coalesce(latest_value, a.value_eur) AS current_value,
       true AS is_current
"""

# Cohort 2: modifications that link to an award -> superseded.
_LINKED_MODS = """
MATCH (m:Contract {notice_type: $mod})-[:MODIFIES]->(a:Contract)
RETURN m.ted_notice_id AS id,
       coalesce(a.procedure_id, a.ted_publication_number, a.ted_notice_id) AS contract_key,
       m.value_eur AS current_value,
       false AS is_current
"""

# Cohort 3: orphan modifications (no MODIFIES edge). Group by contract identity;
# latest notice in the group is canonical, the rest superseded.
_ORPHAN_MODS = """
MATCH (m:Contract {notice_type: $mod})
WHERE NOT (m)-[:MODIFIES]->(:Contract)
WITH coalesce(m.procedure_id, m.modifies_publication_number, m.ted_notice_id) AS ckey, m
ORDER BY m.publication_date DESC
WITH ckey, collect(m) AS grp
WITH ckey, grp,
     head([x IN grp WHERE x.value_eur IS NOT NULL] + grp) AS canonical
UNWIND grp AS m
RETURN m.ted_notice_id AS id,
       ckey AS contract_key,
       CASE WHEN m = canonical THEN canonical.value_eur ELSE m.value_eur END AS current_value,
       (m = canonical) AS is_current
"""


def _emit_rows(rows, log: EventLog, batch_size: int) -> int:
    emitted = 0
    for start in range(0, len(rows), batch_size):
        chunk = rows[start:start + batch_size]
        with log.batch(uuid.uuid4(), producer="collapse_modifications") as emit:
            for r in chunk:
                payload = builders.upsert_contract(ted_notice_id=r["id"])
                # Rollup-only partial: the sinks SET n += props, so only these
                # three fields change; value/title/integrity props are untouched.
                payload.update({
                    "contract_key": r["contract_key"],
                    "current_value": r["current_value"],
                    "is_current": r["is_current"],
                })
                emit.upsert(
                    "UpsertContract",
                    iri=f"http://data.fontem.eu/id/Contract/{r['id']}",
                    domain="contract",
                    payload=payload,
                )
                emitted += 1
    return emitted


def collapse_modifications(driver, log: EventLog, batch_size: int = 500) -> int:
    """Compute and emit current_value / is_current / contract_key for every
    contract touched by a modification. Returns the number of nodes emitted."""
    total = 0
    for label, query in (
        ("awards-with-mods", _AWARDS_WITH_MODS),
        ("linked-mods", _LINKED_MODS),
        ("orphan-mods", _ORPHAN_MODS),
    ):
        with driver.session() as session:
            rows = [dict(r) for r in session.run(query, mod=_MOD)]
        n = _emit_rows(rows, log, batch_size)
        logger.info("collapse: %s -> %d nodes emitted", label, n)
        total += n
    logger.info("collapse: %d nodes emitted total", total)
    return total


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Materialise current_value collapsing contract modifications")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    driver = GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://neo4j:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"),
              os.environ.get("NEO4J_PASSWORD", "")),
    )
    log = EventLog.from_env()
    try:
        collapse_modifications(driver, log)
    finally:
        log.close()
        driver.close()


if __name__ == "__main__":
    main()
