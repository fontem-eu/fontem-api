"""Wikimedia EventStreams `recentchange` → Postgres dirty-set.

Long-lived process. Subscribes to the SSE stream filtered to
``wiki=wikidatawiki`` and, for every event whose entity matters to us,
upserts a row into ``wikidata.dirty_entities`` — one row per affected
entity, with ``last_changed_at`` advanced on each touch. A separate
hourly worker reads this set, fetches each entity's current truthy
state from Wikidata, applies our language filter, and writes diffs
into Virtuoso.

The dirty-set shape (rather than an event log) is deliberate: the SSE
events have no content of their own, only the entity-id and a hint of
which kind of edit happened. Storing a row per event is just a longer
way of building a deduped set, so we build the set directly.

Resilience:

  * Stream disconnect (timeout, server-side reset): reconnect with a
    fresh ``since`` cursor read from ``wikidata.relay_state``.
  * Postgres reconnect: a fresh connection is opened per outer-loop
    iteration so a transient DB blip just costs us the SSE reconnect.
  * Pre-filter (see ``event_filter.py``): irrelevant edits (sitelinks,
    non-EU labels, log-noise) are dropped before they reach Postgres.
  * Tombstones: a Wikidata page-delete log event flips an
    ``is_deleted`` flag rather than bumping ``last_changed_at`` — the
    worker uses that to issue a Virtuoso DELETE instead of an API
    fetch. Detected from the event payload itself
    (``log_type=delete AND log_action=delete``); never from an API
    404, which is too easily faked by a flaky firewall or proxy.

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

from src.relay.event_filter import EventAction, classify

logger = logging.getLogger(__name__)

STREAM_URL = "https://stream.wikimedia.org/v2/stream/recentchange"
USER_AGENT = "Fontem-WikidataRelay/1.0 (+https://fontem.eu; team@fontem.eu)"
TARGET_WIKI = "wikidatawiki"

# Commit + advance the resume cursor every N processed events (where
# "processed" means accepted *or* explicitly ignored — the cursor
# advances either way so we don't reprocess on restart).
CHECKPOINT_BATCH = int(os.environ.get("WIKIDATA_RELAY_CHECKPOINT_BATCH", "100"))

# Wikidata entity titles look like ``Q42``, ``P31``, or ``Lexeme:L1234``.
# We strip any namespace prefix and grab the bare id.
ENTITY_RE = re.compile(r"^(?:Property:|Lexeme:|EntitySchema:)?([QPLE]\d+)$")


@dataclass
class StreamEvent:
    """Parsed SSE event after wiki + entity-id parsing. ``action`` is
    the verdict from the pre-filter; the relay uses it to decide
    whether to upsert as dirty, mark deleted, or skip."""

    event_id: str | None
    event_ts: datetime
    wiki: str
    entity_id: str | None
    action: EventAction
    comment_kind: str | None


def _parse_entity_id(title: str | None) -> str | None:
    if not title:
        return None
    match = ENTITY_RE.match(title.strip())
    return match.group(1) if match else None


def parse_event(raw: dict) -> StreamEvent | None:
    """Convert a raw SSE event dict into a ``StreamEvent`` — or None if
    the event is for a wiki we don't care about or has no usable
    timestamp."""
    wiki = raw.get("wiki")
    if wiki != TARGET_WIKI:
        return None
    ts_epoch = raw.get("timestamp")
    if ts_epoch is None:
        return None
    decision = classify(raw)
    return StreamEvent(
        event_id=str(raw["id"]) if raw.get("id") is not None else None,
        event_ts=datetime.fromtimestamp(int(ts_epoch), tz=timezone.utc),
        wiki=wiki,
        entity_id=_parse_entity_id(raw.get("title")),
        action=decision.action,
        comment_kind=decision.comment_kind,
    )


def iter_sse_data_lines(line_iter):
    """Yield decoded JSON dicts from the ``data:`` lines of an SSE
    stream. Wikimedia uses one ``data:`` per event; multi-line ``data:``
    blocks are extremely rare here so we treat each ``data:`` line as
    one full event."""
    for line in line_iter:
        if not line:
            continue
        if line.startswith("data: "):
            payload = line[6:]
        elif line.startswith("data:"):
            payload = line[5:]
        else:
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("SSE line didn't parse as JSON: %r", payload[:120])


def get_resume_cursor(conn) -> datetime:
    """Read the resume cursor from ``wikidata.relay_state``. Seeded on
    first deploy with the truthy-dump snapshot timestamp so we
    cleanly cover the post-snapshot gap. Falls back to
    ``WIKIDATA_RELAY_SINCE`` only if no row exists."""
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


def mark_dirty(conn, entity_id: str, event_ts: datetime,
               comment_kind: str | None) -> None:
    """Upsert one entity into ``wikidata.dirty_entities``. Idempotent:
    multiple events for the same entity coalesce to one row whose
    ``last_changed_at`` is the most-recent event timestamp.

    A previously-tombstoned entity that gets a new edit (e.g. via
    undelete) flips ``is_deleted`` back to false — the worker will
    refetch and rewrite triples."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO wikidata.dirty_entities
              (entity_id, last_changed_at, first_seen_at, edit_count,
               last_comment_kind, is_deleted)
            VALUES (%s, %s, %s, 1, %s, FALSE)
            ON CONFLICT (entity_id) DO UPDATE
              SET last_changed_at   = GREATEST(
                    wikidata.dirty_entities.last_changed_at,
                    EXCLUDED.last_changed_at),
                  edit_count        = wikidata.dirty_entities.edit_count + 1,
                  last_comment_kind = EXCLUDED.last_comment_kind,
                  is_deleted        = FALSE
            """,
            (entity_id, event_ts, event_ts, comment_kind),
        )


def mark_deleted(conn, entity_id: str, event_ts: datetime) -> None:
    """Upsert an entity as deleted. The worker treats this as a
    Virtuoso DELETE of all triples in the entity's graph, no fetch."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO wikidata.dirty_entities
              (entity_id, last_changed_at, first_seen_at, edit_count,
               last_comment_kind, is_deleted)
            VALUES (%s, %s, %s, 1, 'log-delete-delete', TRUE)
            ON CONFLICT (entity_id) DO UPDATE
              SET last_changed_at   = GREATEST(
                    wikidata.dirty_entities.last_changed_at,
                    EXCLUDED.last_changed_at),
                  edit_count        = wikidata.dirty_entities.edit_count + 1,
                  last_comment_kind = 'log-delete-delete',
                  is_deleted        = TRUE
            """,
            (entity_id, event_ts, event_ts),
        )


def advance_cursor(
    conn, last_event_id: str | None, last_event_ts: datetime,
    batch_size: int,
) -> None:
    """Commit + advance the resume cursor."""
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


def stream_loop(database_url: str) -> None:  # pylint: disable=too-many-locals,too-many-statements,too-many-branches
    """Outer loop: open Postgres, read cursor, open SSE stream, drain.
    Reconnects with exponential backoff on any transient error."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/event-stream",
    }
    backoff = 5
    while True:
        try:
            with psycopg.connect(database_url) as conn:
                since = get_resume_cursor(conn)
                since_str = since.astimezone(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                logger.info("Streaming since %s", since_str)

                params = {"since": since_str}
                timeout = httpx.Timeout(connect=10.0, read=60.0,
                                        write=10.0, pool=10.0)
                with httpx.stream(
                    "GET", STREAM_URL, params=params,
                    headers=headers, timeout=timeout,
                ) as resp:
                    resp.raise_for_status()
                    backoff = 5
                    pending_id: str | None = None
                    pending_ts: datetime | None = None
                    pending_n = 0
                    dirty_n = 0
                    deleted_n = 0
                    ignored_n = 0
                    for raw in iter_sse_data_lines(resp.iter_lines()):
                        ev = parse_event(raw)
                        if ev is None:
                            continue

                        # Cursor advances on every wikidatawiki event,
                        # even ignored — an ignore-decision is a
                        # positive verdict, not an absence of one.
                        pending_id = ev.event_id
                        pending_ts = ev.event_ts
                        pending_n += 1

                        if ev.entity_id is not None:
                            if ev.action is EventAction.DELETED:
                                mark_deleted(conn, ev.entity_id, ev.event_ts)
                                deleted_n += 1
                            elif ev.action is EventAction.DIRTY:
                                mark_dirty(conn, ev.entity_id, ev.event_ts,
                                           ev.comment_kind)
                                dirty_n += 1
                            else:
                                ignored_n += 1
                        else:
                            ignored_n += 1

                        if pending_n >= CHECKPOINT_BATCH:
                            assert pending_ts is not None
                            advance_cursor(
                                conn, pending_id, pending_ts, pending_n,
                            )
                            logger.info(
                                "checkpoint: total=%d dirty=%d deleted=%d ignored=%d cursor=%s",
                                pending_n, dirty_n, deleted_n, ignored_n,
                                pending_ts.isoformat(),
                            )
                            pending_id = None
                            pending_ts = None
                            pending_n = 0
                            dirty_n = 0
                            deleted_n = 0
                            ignored_n = 0
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

    def _on_term(_sig, _frame):
        logger.info("SIGTERM received, exiting")
        sys.exit(0)
    signal.signal(signal.SIGTERM, _on_term)
    stream_loop(database_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
