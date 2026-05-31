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
  * ``WIKIDATA_CONSUMER_CONCURRENCY`` (optional, default 3) — number of
    entities processed in parallel per batch. Serial processing was
    ~340ms per entity which never kept up with the live arrival rate.
    A first parallel run at 10 workers triggered Wikimedia's rate
    limit hard (~90% 429s in a single batch). 3 workers keeps the
    sustained request rate well under their threshold while still
    giving us 3-5x the serial throughput.
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import sys
import time
from datetime import datetime, timezone

import psycopg

from src.relay.wikidata_fetcher import FetchOutcome, fetch_truthy, make_client
from src.relay.wikidata_writer import (
    filter_graph,
    tombstone_entity,
    write_entity,
)

logger = logging.getLogger(__name__)

BATCH_SIZE = int(os.environ.get("WIKIDATA_CONSUMER_BATCH", "1000"))
CONCURRENCY = int(os.environ.get("WIKIDATA_CONSUMER_CONCURRENCY", "3"))


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
    http_client, sparql_url: str, auth: tuple[str, str],
) -> tuple[str, str, object]:
    """Process one entity end-to-end against Wikidata + Virtuoso. The
    caller is responsible for clearing the dirty-row in postgres
    after this returns successfully. Returns ``(outcome, entity_id,
    last_changed_at)`` so the caller can build a batched DELETE.

    Postgres is intentionally not touched here — the function runs
    inside a ThreadPoolExecutor and psycopg connections are not
    thread-safe, so keeping the only pg connection on the main thread
    is simpler than maintaining a pool."""
    if is_deleted:
        tombstone_entity(entity_id, http_client, sparql_url, auth)
        return ("tombstoned", entity_id, last_changed_at)

    fetched = fetch_truthy(entity_id, http_client)
    if fetched.outcome is FetchOutcome.NOT_FOUND:
        # Wikidata says it's gone; we did NOT come here from a tombstone
        # event so this is most likely a race with a delete that hasn't
        # flowed through our relay yet, OR a flaky 404. Conservative
        # choice: leave the row, let the next run decide. If it really
        # is deleted, the relay will catch the log event soon and flip
        # is_deleted true.
        return ("not_found_left_pending", entity_id, last_changed_at)

    graph = fetched.graph
    assert graph is not None  # OK and REDIRECT both carry a graph
    filtered = filter_graph(graph, entity_id)
    write_entity(entity_id, filtered, http_client, sparql_url, auth)

    if fetched.outcome is FetchOutcome.REDIRECT and fetched.redirected_to:
        # The survivor's RDF was returned by Wikidata; we've written
        # it. The redirected-from id no longer needs separate
        # tracking. (The owl:sameAs from source→target is included in
        # the graph itself.) The survivor will get its own
        # dirty_entities row from future edits — we don't pre-emptively
        # mark it dirty here.
        return ("redirected", entity_id, last_changed_at)
    return ("written", entity_id, last_changed_at)


def clear_dirty_batch(conn, pairs: list[tuple[str, object]]) -> None:
    """Optimistic-batched-delete the dirty-rows whose
    ``(entity_id, last_changed_at)`` is still what we observed at
    lease time. Rows whose timestamp moved (relay bumped them mid-
    batch) are simply not matched and stay for the next run."""
    if not pairs:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """
            DELETE FROM wikidata.dirty_entities
             WHERE entity_id = %s AND last_changed_at = %s
            """,
            pairs,
        )
    conn.commit()


def log_run(conn, started_at: datetime, counts: dict[str, int]) -> None:
    """Write the per-batch summary into ``wikidata.consumer_runs`` so
    the relay's metrics poller can fold it into the
    ``wikidata_consumer_entities_total`` counter. This is the only
    way the short-lived CronJob pod's per-batch numbers reach
    Prometheus — there is no scrape target on the consumer side."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO wikidata.consumer_runs
              (started_at, finished_at, leased, written, tombstoned,
               redirected, not_found_left_pending, errors)
            VALUES (%s, now(), %s, %s, %s, %s, %s, %s)
            """,
            (started_at, counts["leased"], counts["written"],
             counts["tombstoned"], counts["redirected"],
             counts["not_found_left_pending"], counts["errors"]),
        )
    conn.commit()


def run_batch(database_url: str, sparql_url: str,  # pylint: disable=too-many-locals
              auth: tuple[str, str], batch_size: int,
              concurrency: int = CONCURRENCY) -> dict[str, int]:
    """One invocation. Lease a batch, process entities in parallel,
    bulk-clear the dirty rows for successfully-processed entities at
    the end."""
    started_at = datetime.now(timezone.utc)
    counts: dict[str, int] = {
        "leased": 0, "written": 0, "tombstoned": 0,
        "redirected": 0, "not_found_left_pending": 0, "errors": 0,
    }
    # Outcomes that mean "we did our work, drop the dirty row".
    success_outcomes = {"written", "tombstoned", "redirected"}
    to_clear: list[tuple[str, object]] = []
    with psycopg.connect(database_url) as pg_conn, make_client() as http_client:
        rows = lease_batch(pg_conn, batch_size)
        counts["leased"] = len(rows)
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix="consumer",
        ) as pool:
            futures = [
                pool.submit(process_one, eid, lc, isd,
                            http_client, sparql_url, auth)
                for eid, lc, isd in rows
            ]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    outcome, eid, lc = fut.result()
                    counts[outcome] = counts.get(outcome, 0) + 1
                    if outcome in success_outcomes:
                        to_clear.append((eid, lc))
                except Exception as exc:  # pylint: disable=broad-except
                    # One entity's failure must not poison the whole
                    # batch. Log + leave the dirty row in place for
                    # the next run.
                    logger.exception("processing failed: %s", exc)
                    counts["errors"] += 1
        clear_dirty_batch(pg_conn, to_clear)
        log_run(pg_conn, started_at, counts)
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
