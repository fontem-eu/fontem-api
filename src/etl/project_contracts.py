"""Migrate the contract subgraph from notice-grain to the Contract/Notice model.

Before: every TED notice (the award + each modification) is its own :Contract
node with its own AWARDED_TO / AWARDED edges, so value aggregates double-count
modification restatements (see the contract-grain problem).

After:
    (:Notice {ted_notice_id, notice_kind, value_eur, tenders_received, ...})
        -[:NOTICE_OF]->
    (:Contract {contract_key, current_value, award_value, title, cpv, nuts, country})
        -[:AWARDED_TO]-> (:Company)          (all awardees; consortia keep several)
    (:Authority)-[:AWARDED]-> (:Contract)

One :Contract per real contract (keyed by contract_key), so `MATCH (:Contract)`
is 1:1 with reality and the double-count is structurally impossible — no
canonical filter needed at read time. Notices carry raw per-notice provenance;
their MODIFIES edges stay between notices.

Identity: contract_key = the value the collapse pass already stamped (procedure
grouping + legacy fallbacks); singleton awards fall back to their ted_notice_id.

Idempotent + batched, so it is safe to re-run and to resume after interruption.
Phase 1 relabels notices; phase 2 builds Contract entities + NOTICE_OF; phase 3
sets Contract properties + moves the awardee/authority edges off the canonical
notice; phase 4 strips the (now-duplicated) aggregatable edges from notices.
"""
from __future__ import annotations

import argparse
import logging
import os

from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

# ── Phase 1: relabel every notice node :Contract -> :Notice ──────────────
_RELABEL = """
MATCH (n:Contract)
WHERE n.ted_notice_id IS NOT NULL
  // Exclude the projected entities. finalize denormalises the canonical
  // notice's fields (including ted_notice_id) onto the :Contract entity, so
  // ted_notice_id alone does NOT distinguish a raw notice from an entity.
  // An entity always has notices pointing at it; a freshly-sunk notice never
  // does. Without this guard a re-run relabels every entity into a :Notice
  // and destroys the model.
  AND NOT (n)<-[:NOTICE_OF]-()
WITH n LIMIT $batch
SET n:Notice,
    n.notice_kind = CASE WHEN n.notice_type = 'can-modif'
                         THEN 'modification' ELSE 'award' END
REMOVE n:Contract
RETURN count(n) AS done
"""

# ── Phase 2: one :Contract entity per contract_key + NOTICE_OF ───────────
_PROJECT = """
MATCH (n:Notice)
WHERE NOT (n)-[:NOTICE_OF]->(:Contract)
WITH n LIMIT $batch
WITH n, coalesce(n.contract_key, n.ted_notice_id) AS ckey
MERGE (c:Contract {contract_key: ckey})
MERGE (n)-[:NOTICE_OF]->(c)
RETURN count(n) AS done
"""

# ── Phase 3: derive Contract props + move awardee/authority edges ────────
# The canonical notice (is_current, else the latest) supplies the current
# value, the descriptive fields, and the awardee/authority edges.
_FINALIZE = """
MATCH (c:Contract)
WHERE c.notice_count IS NULL AND EXISTS { (:Notice)-[:NOTICE_OF]->(c) }
WITH c LIMIT $batch
CALL (c) {
  MATCH (n:Notice)-[:NOTICE_OF]->(c)
  WITH n ORDER BY coalesce(n.is_current, false) DESC,
                  coalesce(n.publication_date, '') DESC
  RETURN collect(n) AS notices
}
WITH c, notices, head(notices) AS canon,
     head([x IN notices WHERE (x.notice_type IS NULL OR x.notice_type <> 'can-modif')]) AS award
// Denormalise the canonical notice's display fields onto the Contract so the
// existing read surfaces (which read these off the contract node) work
// unchanged. value_eur is set to the collapsed current_value so every legacy
// `ct.value_eur` aggregate returns the de-duplicated figure with no query
// change; the raw per-notice values stay on the :Notice nodes.
SET c += properties(canon)
SET c.current_value = coalesce(canon.current_value, canon.value_eur),
    c.value_eur     = coalesce(canon.current_value, canon.value_eur),
    c.award_value   = coalesce(award.value_eur, canon.value_eur),
    c.notice_count  = size(notices),
    c.contract_key  = c.contract_key,
    c.is_current    = true,
    c.notice_type   = null,
    c.modifies_publication_number = null
WITH c, canon
CALL (c, canon) {
  MATCH (canon)-[:AWARDED_TO]->(co:Company)
  MERGE (c)-[:AWARDED_TO]->(co)
}
CALL (c, canon) {
  OPTIONAL MATCH (a:Authority)-[:AWARDED]->(canon)
  FOREACH (_ IN CASE WHEN a IS NULL THEN [] ELSE [1] END |
    MERGE (a)-[:AWARDED]->(c))
}
RETURN count(c) AS done
"""

# ── Phase 4: strip the aggregatable edges off notices (now on the Contract)
_STRIP = """
MATCH (n:Notice)-[r:AWARDED_TO|AWARDED]-()
WITH r LIMIT $batch
DELETE r
RETURN count(r) AS done
"""


def _run_until_drained(driver, cypher: str, label: str, batch: int) -> int:
    total = 0
    while True:
        with driver.session() as session:
            done = session.execute_write(
                lambda tx: tx.run(cypher, batch=batch).single()["done"])
        # A real driver returns an int; a mocked one (unit tests) returns a
        # non-int sentinel — treat that as "nothing to do" so the pass is inert.
        if not isinstance(done, int):
            break
        total += done
        if done:
            logger.info("%s: %d (running %d)", label, done, total)
        if done < batch:
            break
    logger.info("%s: done, %d total", label, total)
    return total


def migrate(driver, batch: int = 5000) -> dict:
    return {
        "relabelled": _run_until_drained(driver, _RELABEL, "relabel notices", batch),
        "projected": _run_until_drained(driver, _PROJECT, "project contracts", batch),
        "finalized": _run_until_drained(driver, _FINALIZE, "finalize contracts", batch),
        "stripped": _run_until_drained(driver, _STRIP, "strip notice edges", batch),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Migrate contract subgraph to the Contract/Notice model")
    parser.add_argument("--batch", type=int, default=5000)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    driver = GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://neo4j:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"),
              os.environ.get("NEO4J_PASSWORD", "")),
    )
    try:
        result = migrate(driver, args.batch)
        logger.info("migration complete: %s", result)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
