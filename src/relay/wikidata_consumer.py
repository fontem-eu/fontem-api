"""Wikidata dirty-set consumer.

Reads pending rows from ``wikidata.dirty_entities`` and, for each
entity, either fetches its current truthy state from Wikidata and
rewrites it in Virtuoso, or — for tombstoned rows — issues a DELETE.
Concurrency-safe so it can run alongside the relay (the relay only
upserts; the worker only reads + deletes).

Designed as a single-shot batch process invoked by a Kubernetes
CronJob. Each invocation processes a configurable budget of entities
(``WIKIDATA_CONSUMER_BATCH``, default 1000), then exits. The hour-cron
scheduling means we trade a small staleness window for huge
operational simplicity over a long-lived process: no health-check, no
graceful-shutdown, no leader-election, no leases. If a run is killed
mid-flight, the next run picks up where it left off because every
written entity is removed from dirty_entities only after Virtuoso
commits.

Optimistic concurrency:
  The worker captures ``last_changed_at`` at lease-time, and on
  successful write does `DELETE … WHERE entity_id = $1 AND
  last_changed_at = $2`. If the relay bumped the timestamp in the
  interim, the DELETE is a no-op and the row stays for the next run.
  No locks needed.

Required env:
  * ``EVENTS_DATABASE_URL`` — Postgres connection string
  * ``VIRTUOSO_SPARQL_UPDATE_URL`` — e.g.
    ``http://virtuoso.fontem-prod.svc.cluster.local:8890/sparql-auth``
  * ``VIRTUOSO_DBA_USER`` (default ``dba``)
  * ``VIRTUOSO_DBA_PASSWORD`` — required for write endpoint
  * ``WIKIDATA_CONSUMER_BATCH`` (optional, default 1000)
"""
from __future__ import annotations

import logging
import os
import sys
import time

import psycopg

from src.relay.wikidata_fetcher import FetchOutcome, fetch_truthy, make_client
from src.relay.wikidata_writer import (
    filter_graph,
    tombstone_entity,
    write_entity,
)

logger = logging.getLogger(__name__)

BATCH_SIZE = int(os.environ.get("WIKIDATA_CONSUMER_BATCH", "1000"))


def lease_batch(conn, batch_size: int) -> list[tuple[str, object, bool]]:
    """Read a batch of pending entities, oldest-first. We do NOT take
    Postgres locks here: the optimistic-delete after write handles
    concurrency. Returns a list of (entity_id, last_changed_at,
    is_deleted) tuples."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT entity_id, last_changed_at, is_deleted
              FROM wikidata.dirty_entities
             ORDER BY last_changed_at ASC
             LIMIT %s
            """,
            (batch_size,),
        )
        return list(cur.fetchall())


def clear_dirty(conn, entity_id: str, last_changed_at: object) -> bool:
    """Optimistic-delete the dirty-row for ``entity_id`` iff its
    ``last_changed_at`` is still what we observed when we leased. If
    the relay bumped it (concurrent edit), the WHERE clause is false
    and the row remains. Returns True when the row was actually
    removed."""
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM wikidata.dirty_entities
             WHERE entity_id = %s AND last_changed_at = %s
            """,
            (entity_id, last_changed_at),
        )
        deleted = cur.rowcount
    conn.commit()
    return deleted > 0


def process_one(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    entity_id: str, last_changed_at: object, is_deleted: bool,
    pg_conn, http_client, sparql_url: str, auth: tuple[str, str],
) -> str:
    """Process one entity end-to-end. Returns the outcome label for
    metrics. Exceptions propagate so the caller can decide whether to
    leave the row in place (transient) or escalate."""
    if is_deleted:
        tombstone_entity(entity_id, http_client, sparql_url, auth)
        clear_dirty(pg_conn, entity_id, last_changed_at)
        return "tombstoned"

    fetched = fetch_truthy(entity_id, http_client)
    if fetched.outcome is FetchOutcome.NOT_FOUND:
        # Wikidata says it's gone; we did NOT come here from a tombstone
        # event so this is most likely a race with a delete that hasn't
        # flowed through our relay yet, OR a flaky 404. Conservative
        # choice: do nothing now, leave the row, let the next run
        # decide. If it really is deleted, the relay will catch the
        # log event soon and flip is_deleted true.
        return "not_found_left_pending"

    graph = fetched.graph
    assert graph is not None  # OK and REDIRECT both carry a graph
    filtered = filter_graph(graph, entity_id)
    write_entity(entity_id, filtered, http_client, sparql_url, auth)
    clear_dirty(pg_conn, entity_id, last_changed_at)

    if fetched.outcome is FetchOutcome.REDIRECT and fetched.redirected_to:
        # The survivor's RDF was returned by Wikidata; we've written
        # it. The redirected-from id no longer needs separate
        # tracking. (The owl:sameAs from source→target is included in
        # the graph itself.) The survivor will get its own
        # dirty_entities row from future edits — we don't pre-emptively
        # mark it dirty here.
        return "redirected"
    return "written"


def run_batch(database_url: str, sparql_url: str,
              auth: tuple[str, str], batch_size: int) -> dict[str, int]:
    """One invocation. Lease a batch, process each entity, return
    counts. Closes resources before exit so the pod can shut down
    cleanly."""
    counts: dict[str, int] = {
        "leased": 0, "written": 0, "tombstoned": 0,
        "redirected": 0, "not_found_left_pending": 0, "errors": 0,
    }
    with psycopg.connect(database_url) as pg_conn, make_client() as http_client:
        rows = lease_batch(pg_conn, batch_size)
        counts["leased"] = len(rows)
        for entity_id, last_changed_at, is_deleted in rows:
            try:
                outcome = process_one(
                    entity_id, last_changed_at, is_deleted,
                    pg_conn, http_client, sparql_url, auth,
                )
                counts[outcome] = counts.get(outcome, 0) + 1
            except Exception as exc:  # pylint: disable=broad-except
                # One entity's failure must not poison the whole batch.
                # Log + leave the dirty row in place for the next run.
                logger.exception("processing %s failed: %s", entity_id, exc)
                counts["errors"] += 1
    return counts


def main(argv: list[str] | None = None) -> int:  # pylint: disable=unused-argument
    """Entry point for the CronJob pod. Exits 0 even on per-entity
    failures so Kubernetes doesn't escalate to backoff — the next
    cron tick will retry the leftover rows."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    database_url = os.environ.get("EVENTS_DATABASE_URL")
    sparql_url = os.environ.get("VIRTUOSO_SPARQL_UPDATE_URL")
    dba_user = os.environ.get("VIRTUOSO_DBA_USER", "dba")
    dba_pw = os.environ.get("VIRTUOSO_DBA_PASSWORD")
    if not database_url or not sparql_url or not dba_pw:
        logger.error(
            "EVENTS_DATABASE_URL, VIRTUOSO_SPARQL_UPDATE_URL, and "
            "VIRTUOSO_DBA_PASSWORD must be set"
        )
        return 1

    started = time.monotonic()
    counts = run_batch(database_url, sparql_url, (dba_user, dba_pw), BATCH_SIZE)
    elapsed = time.monotonic() - started
    logger.info(
        "batch done in %.1fs: leased=%d written=%d tombstoned=%d "
        "redirected=%d not_found_left_pending=%d errors=%d",
        elapsed, counts["leased"], counts["written"], counts["tombstoned"],
        counts["redirected"], counts["not_found_left_pending"], counts["errors"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
