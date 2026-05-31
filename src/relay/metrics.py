"""Prometheus metrics for the Wikidata relay + consumer pair.

The relay is the long-lived pod so it owns the metrics HTTP endpoint.
The consumer is short-lived (CronJob), so it can't host its own
``/metrics``; instead it writes a summary row to
``wikidata.consumer_runs`` at end of each batch and the relay's
background poller catches up on new rows and increments the
corresponding counters.

Three flavours of metric:

  * **Counters**: monotonically-increasing, useful with ``rate()``.
    ``wikidata_relay_events_total`` is incremented inline in the
    stream loop. ``wikidata_consumer_entities_total`` is back-filled
    from the consumer_runs table by ``poll_postgres_state()``.

  * **Gauges**: latest-value, useful for dashboards.
    ``wikidata_dirty_entities_total`` is refreshed by
    ``poll_postgres_state()`` every ``METRIC_REFRESH_SECONDS``.
    ``wikidata_relay_cursor_lag_seconds`` shows how far the relay
    is behind real-time.

  * **Info**: build version etc, set once at startup.

The HTTP server is in-process (started via
``prometheus_client.start_http_server``) — adds no extra container,
just one extra port. Prometheus scrapes via a ServiceMonitor that
selects the relay's Service.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import psycopg
from prometheus_client import Counter, Gauge, start_http_server

logger = logging.getLogger(__name__)

METRIC_REFRESH_SECONDS = int(os.environ.get("WIKIDATA_METRIC_REFRESH_S", "30"))
METRICS_PORT = int(os.environ.get("WIKIDATA_METRICS_PORT", "9100"))

# Inline counters — incremented by the relay loop on every event.
EVENTS_TOTAL = Counter(
    "wikidata_relay_events_total",
    "Wikidata SSE events processed by the relay, by outcome",
    ["outcome"],
)

# Polled-from-postgres gauges. Refreshed every METRIC_REFRESH_SECONDS.
DIRTY_ENTITIES = Gauge(
    "wikidata_dirty_entities_total",
    "Entities in wikidata.dirty_entities waiting for the consumer",
    ["state"],  # refetch | tombstone
)
CURSOR_LAG_SECONDS = Gauge(
    "wikidata_relay_cursor_lag_seconds",
    "Wallclock minus wikidata.relay_state.last_event_ts",
)

# Consumer counters — back-filled from wikidata.consumer_runs.
CONSUMER_ENTITIES_TOTAL = Counter(
    "wikidata_consumer_entities_total",
    "Entities processed by the consumer, by outcome",
    ["outcome"],  # written | tombstoned | redirected | not_found | errors
)
CONSUMER_LAST_FINISHED = Gauge(
    "wikidata_consumer_last_finished_timestamp",
    "Unix timestamp of the last completed consumer batch",
)


def _poll_once(conn) -> None:  # pylint: disable=too-many-locals
    """One refresh of the gauges + catchup on consumer_runs."""
    with conn.cursor() as cur:
        # Queue size.
        cur.execute(
            "SELECT count(*) FILTER (WHERE NOT is_deleted),"
            "       count(*) FILTER (WHERE is_deleted)"
            "  FROM wikidata.dirty_entities"
        )
        refetch, tombstone = cur.fetchone()
        DIRTY_ENTITIES.labels(state="refetch").set(refetch)
        DIRTY_ENTITIES.labels(state="tombstone").set(tombstone)

        # Relay cursor lag.
        cur.execute(
            "SELECT EXTRACT(EPOCH FROM (now() - last_event_ts))::int"
            "  FROM wikidata.relay_state WHERE id = 1"
        )
        row = cur.fetchone()
        if row and row[0] is not None:
            CURSOR_LAG_SECONDS.set(row[0])

        # Consumer counters — catch up on rows we haven't observed yet.
        # ``cursor`` row in wikidata.metrics_state stores the highest
        # consumer_runs.id we've already folded into Counter() so we
        # only ever inc by the delta. Survives relay restarts.
        cur.execute(
            "SELECT last_consumer_run_id FROM wikidata.metrics_state"
            "  WHERE id = 1"
        )
        seen_row = cur.fetchone()
        last_seen = seen_row[0] if seen_row else 0

        cur.execute(
            "SELECT id, written, tombstoned, redirected,"
            "       not_found_left_pending, errors, finished_at"
            "  FROM wikidata.consumer_runs"
            " WHERE id > %s ORDER BY id",
            (last_seen,),
        )
        max_id = last_seen
        last_finished_ts = None
        for row in cur.fetchall():
            (run_id, written, tombstoned, redirected,
             not_found, errors, finished_at) = row
            CONSUMER_ENTITIES_TOTAL.labels(outcome="written").inc(written)
            CONSUMER_ENTITIES_TOTAL.labels(outcome="tombstoned").inc(tombstoned)
            CONSUMER_ENTITIES_TOTAL.labels(outcome="redirected").inc(redirected)
            CONSUMER_ENTITIES_TOTAL.labels(outcome="not_found").inc(not_found)
            CONSUMER_ENTITIES_TOTAL.labels(outcome="errors").inc(errors)
            max_id = run_id
            last_finished_ts = finished_at

        if max_id > last_seen:
            cur.execute(
                "INSERT INTO wikidata.metrics_state (id, last_consumer_run_id)"
                " VALUES (1, %s)"
                " ON CONFLICT (id) DO UPDATE"
                "   SET last_consumer_run_id = EXCLUDED.last_consumer_run_id",
                (max_id,),
            )
            conn.commit()
        if last_finished_ts is not None:
            CONSUMER_LAST_FINISHED.set(last_finished_ts.timestamp())


def _refresh_loop(database_url: str) -> None:
    """Background thread that polls Postgres on a fixed interval. On
    DB errors, log and back off — never crash the relay."""
    while True:
        try:
            with psycopg.connect(database_url) as conn:
                _poll_once(conn)
        except psycopg.Error as exc:
            logger.warning("metrics refresh failed: %s", exc)
        time.sleep(METRIC_REFRESH_SECONDS)


def start(database_url: str) -> None:
    """Spin up the HTTP server + background poller. Call once from
    main() before entering the stream loop."""
    start_http_server(METRICS_PORT)
    logger.info("metrics http server listening on :%d", METRICS_PORT)
    threading.Thread(
        target=_refresh_loop, args=(database_url,),
        daemon=True, name="metrics-refresh",
    ).start()
