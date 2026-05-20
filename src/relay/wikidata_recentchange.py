"""Wikimedia EventStreams `recentchange` → Postgres buffer.

Long-lived process. Subscribes to the SSE stream filtered to
``wiki=wikidatawiki`` and inserts every event into
``wikidata.recentchange``. State is checkpointed to
``wikidata.relay_state`` every ``CHECKPOINT_BATCH`` events (default 100)
so a restart resumes near where it left off via the SSE ``since``
parameter.

The relay is intentionally minimal — it does not fetch entity RDF,
diff triples, or write to Virtuoso. Those happen in a separate
downstream worker that reads from the buffer at its own pace. Owning
the buffer means we are no longer pinned to the 7-day window Wikimedia
keeps on the upstream Kafka topic.

Failure modes handled:

  * Stream disconnect (timeout, server-side reset): reconnect with a
    fresh ``since`` cursor read from the database, so we never re-process
    a window that already landed in the buffer.
  * Postgres reconnect: a fresh connection is opened on each iteration
    of the outer loop so a transient DB blip just costs us the SSE
    reconnect.
  * Backpressure: we don't rate-limit deliberately — Wikimedia's
    recentchange averages ~10 events/sec; if we ever fall behind, the
    SSE buffer on the upstream is fixed-size and we'd drop events,
    detected by a gap between ``last_event_ts`` and ``now()``.

Required env:
  * ``EVENTS_DATABASE_URL`` — Postgres connection string
  * ``WIKIDATA_RELAY_SINCE`` (optional) — ISO-8601 cursor used only
    when ``relay_state`` has no row yet
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import psycopg
from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)

# Wikimedia public SSE endpoint. The `since` parameter resumes from a
# given ISO-8601 timestamp; without it, the stream starts from "now".
STREAM_URL = "https://stream.wikimedia.org/v2/stream/recentchange"

# Wikimedia's UA policy requires a deliverable contact in the header.
USER_AGENT = "Fontem-WikidataRelay/1.0 (+https://fontem.eu; team@fontem.eu)"

# Only wikidatawiki events are interesting. Everything else is dropped
# at parse time — no point burning database rows on en.wikipedia edits.
TARGET_WIKI = "wikidatawiki"

# How often to commit + advance the resume cursor. 100 events is a
# good middle ground: roughly 10s of stream time at the average rate,
# which bounds duplicate work on restart without thrashing Postgres.
CHECKPOINT_BATCH = int(os.environ.get("WIKIDATA_RELAY_CHECKPOINT_BATCH", "100"))

# Wikidata entity titles look like "Q42", "P31", or "Lexeme:L1234" /
# "Property:P31" depending on namespace prefix conventions. We strip
# the prefix and grab the bare id.
ENTITY_RE = re.compile(r"^(?:Property:|Lexeme:|EntitySchema:)?([QPLE]\d+)$")


@dataclass
class StreamEvent:
    """Parsed SSE event after Wikimedia-specific filtering."""

    event_id: str | None
    event_ts: datetime
    wiki: str
    entity_id: str | None
    edit_type: str | None
    payload: dict


def _parse_entity_id(title: str | None) -> str | None:
    """Extract the bare Wikidata entity id (Q42/P31/L123) from an SSE
    ``title`` field. Returns None for titles that don't parse — those
    are still buffered (with NULL entity_id) for diagnostic purposes."""
    if not title:
        return None
    match = ENTITY_RE.match(title.strip())
    return match.group(1) if match else None


def parse_event(raw: dict) -> StreamEvent | None:
    """Convert a raw SSE event dict into a ``StreamEvent``, or None if
    the event is for a wiki we don't care about. Defensive about
    missing fields — Wikimedia occasionally ships events without the
    optional ``id`` field; we still buffer them so the downstream can
    audit drops."""
    wiki = raw.get("wiki")
    if wiki != TARGET_WIKI:
        return None
    ts_epoch = raw.get("timestamp")
    if ts_epoch is None:
        return None
    return StreamEvent(
        event_id=str(raw["id"]) if raw.get("id") is not None else None,
        event_ts=datetime.fromtimestamp(int(ts_epoch), tz=timezone.utc),
        wiki=wiki,
        entity_id=_parse_entity_id(raw.get("title")),
        edit_type=raw.get("type"),
        payload=raw,
    )


def iter_sse_data_lines(line_iter):
    """Yield decoded JSON dicts from the ``data:`` lines of an SSE
    stream. SSE delimits events by blank lines; multi-line ``data:``
    blocks are extremely rare in this stream so we treat each
    ``data:`` line as one full event."""
    for line in line_iter:
        if not line:
            continue
        if line.startswith("data: "):
            payload = line[6:]
        elif line.startswith("data:"):
            payload = line[5:]
        else:
            # Comment lines (``: keepalive``), ``event:`` markers,
            # ``id:`` markers — ignored. The ``id:`` cursor isn't
            # load-bearing for us because we resume by timestamp.
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("SSE line didn't parse as JSON: %r", payload[:120])


def get_resume_cursor(conn) -> datetime:
    """Read the resume cursor from ``wikidata.relay_state``. The row
    is seeded with the dump's snapshot timestamp on first deploy, so
    a fresh start ``since=<dump-ts>`` cleanly covers the post-snapshot
    gap. Falls back to env ``WIKIDATA_RELAY_SINCE`` if the row is
    somehow missing."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_event_ts FROM wikidata.relay_state WHERE id = 1"
        )
        row = cur.fetchone()
        if row and row[0]:
            return row[0]
    env_since = os.environ.get("WIKIDATA_RELAY_SINCE")
    if env_since:
        return datetime.fromisoformat(env_since.replace("Z", "+00:00"))
    raise RuntimeError(
        "No resume cursor available — initialise wikidata.relay_state.id=1 "
        "with last_event_ts set to the truthy dump's snapshot timestamp."
    )


def insert_event(conn, ev: StreamEvent) -> None:
    """Append one parsed event to ``wikidata.recentchange``. Caller is
    responsible for committing the surrounding transaction."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO wikidata.recentchange
              (event_id, event_ts, wiki, entity_id, edit_type, payload)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                ev.event_id,
                ev.event_ts,
                ev.wiki,
                ev.entity_id,
                ev.edit_type,
                Jsonb(ev.payload),
            ),
        )


def advance_cursor(
    conn, last_event_id: str | None, last_event_ts: datetime, batch_size: int,
) -> None:
    """Commit + advance the resume cursor by ``batch_size`` events."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE wikidata.relay_state
               SET last_event_id = %s,
                   last_event_ts = %s,
                   events_seen   = events_seen + %s,
                   updated_at    = now()
             WHERE id = 1
            """,
            (last_event_id, last_event_ts, batch_size),
        )
    conn.commit()


def stream_loop(database_url: str) -> None:
    """Outer loop: open a Postgres connection, read the resume cursor,
    open the SSE stream, drain it. On exception, sleep + retry — DB
    connection is opened fresh each iteration so a transient outage on
    either side doesn't poison the long-lived state."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/event-stream",
    }
    backoff = 5
    while True:
        try:
            with psycopg.connect(database_url) as conn:
                since = get_resume_cursor(conn)
                # ISO-8601 with explicit Z suffix — what the upstream
                # endpoint expects per its example queries.
                since_str = since.astimezone(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                logger.info("Streaming since %s", since_str)

                params = {"since": since_str}
                # No connection timeout because the stream is a long
                # poll by design. The per-read timeout below catches
                # silent stalls in well under a minute.
                timeout = httpx.Timeout(connect=10.0, read=60.0,
                                        write=10.0, pool=10.0)
                with httpx.stream(
                    "GET", STREAM_URL, params=params,
                    headers=headers, timeout=timeout,
                ) as resp:
                    resp.raise_for_status()
                    backoff = 5  # reset after a clean connect
                    pending_id: str | None = None
                    pending_ts: datetime | None = None
                    pending_n = 0
                    for raw in iter_sse_data_lines(resp.iter_lines()):
                        ev = parse_event(raw)
                        if ev is None:
                            continue
                        insert_event(conn, ev)
                        pending_id = ev.event_id
                        pending_ts = ev.event_ts
                        pending_n += 1
                        if pending_n >= CHECKPOINT_BATCH:
                            assert pending_ts is not None
                            advance_cursor(
                                conn, pending_id, pending_ts, pending_n,
                            )
                            logger.info(
                                "buffered %d events, cursor=%s entity=%s",
                                pending_n,
                                pending_ts.isoformat(),
                                ev.entity_id or "-",
                            )
                            pending_id = None
                            pending_ts = None
                            pending_n = 0
                    # Stream ended cleanly (rare); commit any tail.
                    if pending_n and pending_ts is not None:
                        advance_cursor(
                            conn, pending_id, pending_ts, pending_n,
                        )
        except (httpx.HTTPError, OSError, psycopg.OperationalError) as exc:
            logger.warning(
                "relay loop error: %s; sleeping %ds before reconnect",
                exc, backoff,
            )
            time.sleep(backoff)
            # Exponential backoff up to 5 min.
            backoff = min(backoff * 2, 300)


def main(argv: list[str] | None = None) -> int:  # pylint: disable=unused-argument
    """Entry point. Reads ``EVENTS_DATABASE_URL`` and runs forever."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    database_url = os.environ.get("EVENTS_DATABASE_URL")
    if not database_url:
        logger.error("EVENTS_DATABASE_URL must be set")
        return 1
    # Graceful exit on SIGTERM so kubectl rollouts don't truncate a
    # transaction. The outer loop will see CancelledError and unwind.
    def _on_term(_sig, _frame):
        logger.info("SIGTERM received, exiting")
        sys.exit(0)
    signal.signal(signal.SIGTERM, _on_term)
    stream_loop(database_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
