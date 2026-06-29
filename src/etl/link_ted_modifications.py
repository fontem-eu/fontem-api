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

import argparse
import logging
import os
import uuid

import httpx
from fontem_event_schemas import builders
from fontem_events import EventLog
from neo4j import GraphDatabase

from . import ted_search

# Award notice-types (mirrors eforms.filters._AWARD_TYPES) used to find the
# award for a procedure when resolving historical modifications.
_AWARD_TYPES = ("can-standard", "can-social", "can-desg", "can-tran")

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


def _resolve_awards_for_procedures(pids: list[str], client: httpx.Client) -> dict[str, str]:
    """One batched search query: map each procedure-identifier to its award
    notice-identifier (the first award found)."""
    proc_clause = " OR ".join(f'procedure-identifier="{p}"' for p in pids)
    type_clause = " OR ".join(f'notice-type="{t}"' for t in _AWARD_TYPES)
    resp = client.post(ted_search.SEARCH_URL, json={
        "query": f"({proc_clause}) AND ({type_clause})",
        "fields": ["notice-identifier", "procedure-identifier"],
        "limit": 250, "paginationMode": "PAGE_NUMBER",
    })
    resp.raise_for_status()
    out: dict[str, str] = {}
    for n in resp.json().get("notices", []):
        pid, nid = n.get("procedure-identifier"), n.get("notice-identifier")
        if pid and nid and pid not in out:
            out[pid] = nid
    return out


def resolve_and_link_awards(driver, log: EventLog, batch_size: int = 40) -> int:  # pylint: disable=too-many-locals
    """Link modifications whose award lacks ``procedure_id`` (e.g. the
    pre-existing monthly-loaded awards) by resolving the award's UUID via
    TED's search API (by procedure_id) and emitting MODIFIES when that award
    is already in the graph — matched by ``ted_notice_id`` (the UUID), which
    old awards have even without a stamped procedure_id. Used by the
    historical modification backfill. Idempotent + batched (one search per
    ``batch_size`` procedures)."""
    with driver.session() as session:
        mods = [(r["mod_id"], r["pid"]) for r in session.run(
            "MATCH (m:Contract {notice_type:'can-modif'}) "
            "WHERE m.procedure_id IS NOT NULL AND NOT (m)-[:MODIFIES]->(:Contract) "
            "RETURN m.ted_notice_id AS mod_id, m.procedure_id AS pid"
        )]
    logger.info("resolve-and-link: %d unlinked modifications", len(mods))
    http = httpx.Client(timeout=ted_search.SEARCH_TIMEOUT)
    emitted = 0
    try:
        for start in range(0, len(mods), batch_size):
            chunk = mods[start:start + batch_size]
            pids = list({pid for _, pid in chunk})
            try:
                proc_award = _resolve_awards_for_procedures(pids, http)
            except httpx.HTTPError:
                logger.exception("resolve-and-link: search failed for a batch; skipping")
                continue
            award_ids = list(set(proc_award.values()))
            with driver.session() as session:
                present = {r["id"] for r in session.run(
                    "MATCH (c:Contract) WHERE c.ted_notice_id IN $ids "
                    "RETURN c.ted_notice_id AS id", ids=award_ids)}
            pairs = [(m, proc_award[p]) for m, p in chunk
                     if p in proc_award and proc_award[p] in present and proc_award[p] != m]
            if not pairs:
                continue
            with log.batch(uuid.uuid4(), producer="link_ted_modifications") as emit:
                for mod_id, award_id in pairs:
                    mod_iri = f"http://data.fontem.eu/id/Contract/{mod_id}"
                    award_iri = f"http://data.fontem.eu/id/Contract/{award_id}"
                    emit.upsert(
                        "UpsertRelationship", iri=mod_iri, domain="contract",
                        payload=builders.upsert_relationship(
                            src_iri=mod_iri, dst_iri=award_iri, predicate="modifies"),
                    )
                    emitted += 1
            if start % (batch_size * 25) == 0:
                logger.info("resolve-and-link: %d edges emitted so far", emitted)
    finally:
        http.close()
    logger.info("resolve-and-link: %d MODIFIES edges emitted", emitted)
    return emitted


def main(argv=None):
    """CLI entry point. Phase 1 (procedure_id graph join) always runs;
    --resolve-awards adds phase 2 (search-API award resolution) for the
    historical modification backfill."""
    parser = argparse.ArgumentParser(description="Link contract modifications to awards")
    parser.add_argument(
        "--resolve-awards", action="store_true",
        help="Also resolve awards via TED search for modifications whose "
             "award lacks procedure_id (historical backfill).",
    )
    args = parser.parse_args(argv)
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
        if args.resolve_awards:
            resolve_and_link_awards(driver, log)
    finally:
        log.close()
        driver.close()


if __name__ == "__main__":
    main()
