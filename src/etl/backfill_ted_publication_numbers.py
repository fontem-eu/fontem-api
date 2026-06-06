"""TED publication-number backfill.

Resumable, single-threaded, ops-observable backfill that fills
``Contract.ted_publication_number`` from the TED v3 search API.

State lives in the graph (no external store, no checkpoint file):

  - ted_publication_number               the value we're filling
  - ted_publication_lookup_state         pending | in_progress | done |
                                         not_published | transient_error
  - ted_publication_lookup_attempted_at  datetime of last attempt
  - ted_publication_lookup_claimed_at    datetime of in-flight claim
                                         (claims older than
                                         _STALE_CLAIM_MINUTES are
                                         reclaimable by the next batch)
  - ted_publication_lookup_last_error    short error tag, kept on
                                         transient rows for forensics
  - ted_publication_lookup_attempts      monotonic transient counter;
                                         rows with attempts>=MAX
                                         drop out of the work-list

Why this script doesn't reuse ``src.services.ted_lookup``:
  - ``resolve_publication_number`` has an LRU cache (useful for the
    runtime redirector; backfill's UUIDs are all unique, the cache
    just burns memory).
  - It raises ``TedLookupError`` on empty results. Backfill needs to
    distinguish three outcomes — resolved / not_published / transient
    — and writing exception-handlers around every call would be
    noisier and weaker than the dedicated state machine here.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from neo4j import Driver, GraphDatabase, ManagedTransaction
from neo4j.exceptions import (
    Neo4jError,
    ServiceUnavailable,
    SessionExpired,
    TransientError,
)

logger = logging.getLogger("backfill_ted_pubnum")

_TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=15.0)
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
)
_PUBNUM_RE = re.compile(r"^\d{1,8}-\d{4}$")

_MAX_ATTEMPTS = 5
_STALE_CLAIM_MINUTES = 15
_MAX_BACKOFF_S = 60.0
_RETRIES_PER_CALL = 6
_NOT_PUBLISHED_CIRCUIT_THRESHOLD = 0.20
_CIRCUIT_WINDOW = 100

_HEARTBEAT_SEC = 30
_STALLED_WARN_SEC = 120
_VALIDATION_SAMPLE_SIZE = 10
_VALIDATION_MAX_NO_MATCH_RATE = 0.80

_THROTTLE_FILE = Path(os.environ.get(
    "BACKFILL_THROTTLE_FILE", "/etc/backfill/rate",
))

_STOP = threading.Event()
_GRACE_DEADLINE: Optional[float] = None


def _install_signal_handlers(grace_seconds: int) -> None:
    """SIGTERM/SIGINT start a bounded drain; SIGUSR1 halves the rate."""

    def _handle_term(signum, _frame):
        global _GRACE_DEADLINE  # pylint: disable=global-statement
        logger.warning("signal=%s entering drain", signum)
        _GRACE_DEADLINE = time.monotonic() + max(grace_seconds - 10, 5)
        _STOP.set()

    def _handle_usr1(_signum, _frame):
        try:
            current = _read_throttle(default=None)
            if current is not None:
                new_rate = max(current / 2.0, 0.1)
                _THROTTLE_FILE.parent.mkdir(parents=True, exist_ok=True)
                _THROTTLE_FILE.write_text(f"{new_rate}\n")
                logger.warning("SIGUSR1 rate %.3f -> %.3f", current, new_rate)
        except OSError as exc:
            logger.warning("SIGUSR1 throttle update failed: %s", exc)

    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)
    signal.signal(signal.SIGUSR1, _handle_usr1)


def _interruptible_sleep(seconds: float) -> bool:
    """Sleep up to ``seconds`` — capped at grace if drain is running.
    Returns True on a clean wake, False when interrupted by ``_STOP``."""
    if seconds <= 0:
        return True
    if _GRACE_DEADLINE is not None:
        remaining = _GRACE_DEADLINE - time.monotonic()
        if remaining <= 0:
            return False
        seconds = min(seconds, remaining)
    return not _STOP.wait(seconds)


def _read_throttle(default: Optional[float]) -> Optional[float]:
    """Read the live rate from the ConfigMap-mounted file; return
    ``default`` on missing/empty/unparseable rather than blowing up
    the loop on a bad operator edit."""
    try:
        text = _THROTTLE_FILE.read_text(encoding="utf-8").strip()
        if not text:
            return default
        value = float(text)
        if value <= 0:
            return default
        return value
    except (OSError, ValueError):
        return default


class TokenBucket:
    """Single-threaded pacer. Backfill is intentionally single-
    threaded so no lock is needed; one bucket per process."""

    def __init__(self, rate: float):
        self.rate = rate
        self.interval = 1.0 / rate
        self.next_at = time.monotonic()

    def update_rate(self, rate: float) -> None:
        if rate <= 0 or rate == self.rate:
            return
        self.rate = rate
        self.interval = 1.0 / rate

    def wait(self) -> bool:
        now = time.monotonic()
        delay = self.next_at - now
        ok = True
        if delay > 0:
            ok = _interruptible_sleep(delay)
        self.next_at = max(self.next_at + self.interval, time.monotonic())
        return ok


@dataclass
class Stats:  # pylint: disable=too-many-instance-attributes
    """Rolling counters + heartbeat builder. Recent outcomes drive
    the circuit breaker; final values go into the SUMMARY log line."""
    started_at_mono: float = field(default_factory=time.monotonic)
    started_at_iso: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    claimed: int = 0
    resolved: int = 0
    not_published: int = 0
    transient: int = 0
    written: int = 0
    write_failures: int = 0
    ted_429: int = 0
    ted_5xx: int = 0
    ted_4xx_other: int = 0
    last_ted_response_mono: float = field(default_factory=time.monotonic)
    recent_outcomes: list = field(default_factory=list)

    def record(self, outcome: str) -> None:
        self.recent_outcomes.append(outcome)
        if len(self.recent_outcomes) > _CIRCUIT_WINDOW:
            self.recent_outcomes.pop(0)

    def not_published_rate(self) -> float:
        if len(self.recent_outcomes) < _CIRCUIT_WINDOW:
            return 0.0
        return sum(
            1 for o in self.recent_outcomes if o == "np"
        ) / len(self.recent_outcomes)

    def line(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, remaining: Optional[int] = None,
        current_uuid: Optional[str] = None,
        attempt: Optional[int] = None,
        last_status: Optional[int] = None,
    ) -> str:
        elapsed = time.monotonic() - self.started_at_mono
        rate = self.resolved / elapsed if elapsed else 0
        eta = (remaining / rate / 3600) if (rate and remaining) else None
        parts = [
            f"progress claimed={self.claimed}",
            f"resolved={self.resolved}",
            f"not_published={self.not_published}",
            f"transient={self.transient}",
            f"written={self.written}",
            f"write_failures={self.write_failures}",
            f"ted_429={self.ted_429}",
            f"ted_5xx={self.ted_5xx}",
            f"ted_4xx_other={self.ted_4xx_other}",
            f"rate={rate:.2f}/s",
            f"elapsed={elapsed:.0f}s",
        ]
        if remaining is not None:
            parts.append(f"remaining={remaining}")
        if eta is not None:
            parts.append(f"eta_h={eta:.2f}")
        if current_uuid is not None:
            parts.append(f"current_uuid={current_uuid}")
        if attempt is not None:
            parts.append(f"attempt={attempt}")
        if last_status is not None:
            parts.append(f"last_status={last_status}")
        return " ".join(parts)


CLAIM_CYPHER = """
MATCH (c:Contract)
WHERE c.ted_notice_id IS NOT NULL
  AND c.ted_publication_number IS NULL
  AND coalesce(c.ted_publication_lookup_attempts, 0) < $max_attempts
  AND (
        c.ted_publication_lookup_state IS NULL
        OR c.ted_publication_lookup_state = 'pending'
        OR c.ted_publication_lookup_state = 'transient_error'
        OR (
            c.ted_publication_lookup_state = 'in_progress'
            AND c.ted_publication_lookup_claimed_at <
                datetime() - duration({minutes: $stale_minutes})
        )
      )
  AND ($skip_uuids IS NULL OR NOT c.ted_notice_id IN $skip_uuids)
WITH c
ORDER BY c.ted_notice_id
LIMIT $batch_size
SET c.ted_publication_lookup_state = 'in_progress',
    c.ted_publication_lookup_claimed_at = datetime()
RETURN c.ted_notice_id AS uuid
"""

WRITE_ONE_CYPHER = """
MATCH (c:Contract {ted_notice_id: $uuid})
WHERE c.ted_publication_number IS NULL
  AND (
       c.ted_publication_lookup_state IS NULL
       OR c.ted_publication_lookup_state IN
          ['pending', 'in_progress', 'transient_error']
      )
WITH c, $state AS new_state, $pub_num AS pub_num, $err AS err
SET c.ted_publication_lookup_attempted_at = datetime(),
    c.ted_publication_lookup_claimed_at = NULL,
    c.ted_publication_number =
        CASE WHEN pub_num IS NOT NULL THEN pub_num
             ELSE c.ted_publication_number END,
    c.ted_publication_lookup_state =
        CASE
            WHEN pub_num IS NOT NULL THEN 'done'
            WHEN new_state = 'not_published'
                 AND c.ted_publication_number IS NULL THEN 'not_published'
            WHEN new_state = 'transient_error' THEN 'transient_error'
            ELSE c.ted_publication_lookup_state
        END,
    c.ted_publication_lookup_attempts =
        CASE
            WHEN new_state = 'transient_error'
                THEN coalesce(c.ted_publication_lookup_attempts, 0) + 1
            ELSE c.ted_publication_lookup_attempts
        END,
    c.ted_publication_lookup_last_error =
        CASE
            WHEN new_state = 'transient_error' THEN err
            ELSE c.ted_publication_lookup_last_error
        END
RETURN count(c) AS updated
"""

COUNT_REMAINING_CYPHER = """
MATCH (c:Contract)
WHERE c.ted_notice_id IS NOT NULL
  AND c.ted_publication_number IS NULL
  AND coalesce(c.ted_publication_lookup_attempts, 0) < $max_attempts
  AND (
        c.ted_publication_lookup_state IS NULL
        OR c.ted_publication_lookup_state IN
           ['pending', 'transient_error', 'in_progress']
      )
RETURN count(c) AS n
"""

RELEASE_CLAIM_CYPHER = """
MATCH (c:Contract {ted_notice_id: $uuid})
WHERE c.ted_publication_lookup_state = 'in_progress'
SET c.ted_publication_lookup_state = 'pending',
    c.ted_publication_lookup_claimed_at = NULL
"""

UNIQUENESS_CYPHER = (
    "CREATE CONSTRAINT contract_ted_notice_id_unique IF NOT EXISTS "
    "FOR (c:Contract) REQUIRE c.ted_notice_id IS UNIQUE"
)
INDEX_CYPHER = (
    "CREATE INDEX contract_ted_pub_lookup_state IF NOT EXISTS "
    "FOR (c:Contract) ON "
    "(c.ted_publication_lookup_state, c.ted_notice_id)"
)


@dataclass
class TedResult:
    pub_num: Optional[str]
    state: str               # 'done' | 'not_published' | 'transient_error'
    err: Optional[str]


def _read_body_safely(resp: httpx.Response) -> Optional[dict]:
    """Cap response size + tolerate malformed JSON. Returning None
    causes the caller to classify the row as transient — so we
    re-attempt rather than tombstoning on a parse error."""
    cl = resp.headers.get("Content-Length")
    if cl is not None:
        try:
            if int(cl) > _MAX_RESPONSE_BYTES:
                return None
        except ValueError:
            return None
    content = resp.content
    if len(content) > _MAX_RESPONSE_BYTES:
        return None
    try:
        return resp.json()
    except (ValueError, json.JSONDecodeError):
        return None


def _ted_call(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-return-statements,too-many-branches,too-many-statements,too-many-locals
    uuid: str, client: httpx.Client, bucket: TokenBucket,
    stats: Stats, heartbeat_state: dict,
) -> TedResult:
    """Resolve a single UUID, classifying every response into one of
    three states. All sleeps are drain-aware so SIGTERM exits within
    the grace budget."""
    if not _UUID_RE.match(uuid):
        logger.error("invalid uuid shape uuid=%r skipping", uuid)
        return TedResult(None, "transient_error", "bad_uuid_shape")

    payload = {
        "query": f'notice-identifier="{uuid}"',
        "fields": ["publication-number", "notice-identifier"],
        "limit": 1,
        "checkQuerySyntax": False,
    }
    backoff = 1.0

    for attempt in range(_RETRIES_PER_CALL):
        if (
            _STOP.is_set() and _GRACE_DEADLINE is not None
            and time.monotonic() >= _GRACE_DEADLINE
        ):
            return TedResult(None, "transient_error", "drained")

        live_rate = _read_throttle(default=None)
        if live_rate is not None:
            bucket.update_rate(live_rate)
        if not bucket.wait():
            return TedResult(None, "transient_error", "drained")

        heartbeat_state["current_uuid"] = uuid
        heartbeat_state["attempt"] = attempt
        try:
            resp = client.post(_TED_SEARCH_URL, json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            logger.warning(
                "ted_transport uuid=%s attempt=%d err=%s",
                uuid, attempt, exc,
            )
            if not _interruptible_sleep(backoff):
                return TedResult(None, "transient_error", "drained")
            backoff = min(backoff * 2, _MAX_BACKOFF_S)
            continue

        stats.last_ted_response_mono = time.monotonic()
        heartbeat_state["last_status"] = resp.status_code

        if resp.status_code == 429:
            stats.ted_429 += 1
            try:
                retry_after = float(
                    resp.headers.get("Retry-After", str(backoff)),
                )
            except ValueError:
                retry_after = backoff
            if _GRACE_DEADLINE is not None:
                remaining = _GRACE_DEADLINE - time.monotonic()
                if remaining <= 0:
                    return TedResult(None, "transient_error", "drained")
                retry_after = min(retry_after, remaining)
            else:
                retry_after = min(retry_after, _MAX_BACKOFF_S)
            logger.warning(
                "ted_429 uuid=%s retry_after=%.1fs attempt=%d",
                uuid, retry_after, attempt,
            )
            if not _interruptible_sleep(retry_after):
                return TedResult(None, "transient_error", "drained")
            backoff = min(backoff * 2, _MAX_BACKOFF_S)
            continue

        if 500 <= resp.status_code < 600:
            stats.ted_5xx += 1
            logger.warning(
                "ted_5xx uuid=%s status=%d attempt=%d",
                uuid, resp.status_code, attempt,
            )
            if not _interruptible_sleep(backoff):
                return TedResult(None, "transient_error", "drained")
            backoff = min(backoff * 2, _MAX_BACKOFF_S)
            continue

        if resp.status_code == 404:
            body = _read_body_safely(resp)
            if body is None:
                logger.warning(
                    "ted_404_unparseable uuid=%s transient", uuid,
                )
                return TedResult(
                    None, "transient_error", "http_404_unparseable",
                )
            notices = body.get("notices")
            if notices in (None, []):
                return TedResult(None, "not_published", None)
            return TedResult(None, "transient_error", "http_404_with_body")

        if 400 <= resp.status_code < 500:
            stats.ted_4xx_other += 1
            logger.error(
                "ted_4xx_other uuid=%s status=%d body=%r",
                uuid, resp.status_code, resp.text[:512],
            )
            return TedResult(
                None, "transient_error", f"http_{resp.status_code}",
            )

        body = _read_body_safely(resp)
        if body is None:
            logger.warning(
                "ted_unparseable uuid=%s status=%d",
                uuid, resp.status_code,
            )
            return TedResult(None, "transient_error", "unparseable_body")

        notices = body.get("notices")
        if notices is None:
            return TedResult(None, "not_published", None)
        if not isinstance(notices, list):
            return TedResult(None, "transient_error", "notices_not_list")
        if not notices:
            return TedResult(None, "not_published", None)

        first = notices[0]
        if not isinstance(first, dict):
            return TedResult(None, "transient_error", "notice_not_object")

        returned_id = first.get("notice-identifier")
        if returned_id != uuid:
            logger.error(
                "ted_mismatch uuid=%s returned=%r transient",
                uuid, returned_id,
            )
            return TedResult(None, "transient_error", "id_mismatch")

        pub_num = first.get("publication-number")
        if pub_num is None or "publication-number" not in first:
            return TedResult(None, "not_published", None)
        if not isinstance(pub_num, str) or not _PUBNUM_RE.match(pub_num):
            logger.error(
                "ted_bad_pubnum uuid=%s pub_num=%r", uuid, pub_num,
            )
            return TedResult(None, "transient_error", "bad_pubnum_shape")
        return TedResult(pub_num, "done", None)

    return TedResult(None, "transient_error", "max_retries")


def _claim_batch(
    driver: Driver, batch_size: int, skip_uuids: list,
) -> list:
    def _tx(tx: ManagedTransaction):
        result = tx.run(
            CLAIM_CYPHER,
            batch_size=batch_size,
            max_attempts=_MAX_ATTEMPTS,
            stale_minutes=_STALE_CLAIM_MINUTES,
            skip_uuids=skip_uuids or None,
        )
        return [r["uuid"] for r in result]

    with driver.session(database="neo4j") as session:
        return session.execute_write(_tx)


def _write_one(driver: Driver, uuid: str, result: TedResult) -> bool:
    def _tx(tx: ManagedTransaction):
        record = tx.run(
            WRITE_ONE_CYPHER,
            uuid=uuid,
            pub_num=result.pub_num,
            state=result.state,
            err=result.err,
        ).single()
        return record["updated"] if record else 0

    with driver.session(database="neo4j") as session:
        updated = session.execute_write(_tx)
    return updated == 1


def _count_remaining(driver: Driver) -> int:
    def _tx(tx: ManagedTransaction):
        return tx.run(
            COUNT_REMAINING_CYPHER, max_attempts=_MAX_ATTEMPTS,
        ).single()["n"]

    with driver.session(database="neo4j") as session:
        return session.execute_read(_tx)


def _release_claim(driver: Driver, uuid: str) -> None:
    def _tx(tx: ManagedTransaction):
        tx.run(RELEASE_CLAIM_CYPHER, uuid=uuid).consume()

    with driver.session(database="neo4j") as session:
        session.execute_write(_tx)


def _ensure_schema(driver: Driver) -> None:
    with driver.session(database="neo4j") as session:
        session.execute_write(
            lambda tx: tx.run(UNIQUENESS_CYPHER).consume(),
        )
        session.execute_write(
            lambda tx: tx.run(INDEX_CYPHER).consume(),
        )


def _validation_gate(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    driver: Driver, client: httpx.Client, bucket: TokenBucket,
    stats: Stats, heartbeat_state: dict,
) -> bool:
    """Resolve a small sample without writing. Abort if the no-match
    rate is implausibly high — most likely cause is a TED-side
    semantic shift we'd otherwise tombstone 56k rows on."""
    uuids = _claim_batch(driver, _VALIDATION_SAMPLE_SIZE, skip_uuids=[])
    if not uuids:
        logger.info("validation_gate empty nothing to backfill")
        return True
    logger.info("validation_gate sample=%d", len(uuids))
    for uuid in uuids:
        _release_claim(driver, uuid)
    no_match = resolved = transient = 0
    for uuid in uuids:
        result = _ted_call(uuid, client, bucket, stats, heartbeat_state)
        if result.state == "done":
            resolved += 1
        elif result.state == "not_published":
            no_match += 1
        else:
            transient += 1
    rate = no_match / max(len(uuids) - transient, 1)
    logger.info(
        "validation_gate resolved=%d not_published=%d "
        "transient=%d no_match_rate=%.2f",
        resolved, no_match, transient, rate,
    )
    if (len(uuids) - transient) > 0 and rate > _VALIDATION_MAX_NO_MATCH_RATE:
        logger.error(
            "validation_gate ABORT no_match_rate=%.2f > %.2f "
            "check TED identifier semantics",
            rate, _VALIDATION_MAX_NO_MATCH_RATE,
        )
        return False
    return True


def run(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-branches,too-many-statements
    driver: Driver, batch_size: int, rate: float, dry_run: bool,
    max_rows: Optional[int], log_interval: int,
    skip_validation: bool,
) -> Stats:
    """Claim then resolve then write loop. Single-threaded, session-
    per-batch, idempotent across restarts."""
    bucket = TokenBucket(rate)
    stats = Stats()
    heartbeat_state: dict = {
        "current_uuid": None, "attempt": None, "last_status": None,
    }

    with httpx.Client(
        timeout=_TIMEOUT,
        headers={"User-Agent": "fontem-backfill/1.0"},
    ) as client:
        _ensure_schema(driver)

        if not skip_validation and not dry_run:
            if not _validation_gate(
                driver, client, bucket, stats, heartbeat_state,
            ):
                logger.error("aborting before any writes")
                return stats

        remaining = _count_remaining(driver)
        logger.info(
            "startup remaining=%d batch_size=%d rate=%.1f/s dry_run=%s",
            remaining, batch_size, rate, dry_run,
        )

        last_heartbeat = time.monotonic()
        run_skip_uuids: list = []

        while not _STOP.is_set():
            try:
                uuids = _claim_batch(
                    driver, batch_size, run_skip_uuids[-5000:],
                )
            except (ServiceUnavailable, SessionExpired,
                    TransientError) as exc:
                logger.warning("claim_retry neo4j_error=%s", exc)
                if not _interruptible_sleep(2.0):
                    break
                continue
            if not uuids:
                logger.info("work-list empty done")
                break

            stats.claimed += len(uuids)

            for uuid in uuids:
                if _STOP.is_set():
                    _release_claim(driver, uuid)
                    continue

                result = _ted_call(
                    uuid, client, bucket, stats, heartbeat_state,
                )

                if result.state == "done":
                    stats.resolved += 1
                    stats.record("ok")
                elif result.state == "not_published":
                    stats.not_published += 1
                    stats.record("np")
                else:
                    stats.transient += 1
                    stats.record("tr")
                    run_skip_uuids.append(uuid)

                if dry_run:
                    logger.info(
                        "dry_run uuid=%s state=%s pub_num=%s",
                        uuid, result.state, result.pub_num,
                    )
                else:
                    try:
                        updated = _write_one(driver, uuid, result)
                        if updated:
                            stats.written += 1
                        else:
                            logger.warning(
                                "write_no_match uuid=%s state=%s "
                                "node missing or already resolved",
                                uuid, result.state,
                            )
                    except (ServiceUnavailable, SessionExpired,
                            TransientError, Neo4jError) as exc:
                        stats.write_failures += 1
                        logger.error(
                            "write_failed uuid=%s state=%s err=%s",
                            uuid, result.state, exc,
                        )
                        try:
                            _release_claim(driver, uuid)
                        except Neo4jError as rel_exc:
                            logger.error(
                                "release_failed uuid=%s err=%s",
                                uuid, rel_exc,
                            )

                np_rate = stats.not_published_rate()
                if np_rate > _NOT_PUBLISHED_CIRCUIT_THRESHOLD:
                    logger.error(
                        "circuit_breaker not_published_rate=%.2f > "
                        "%.2f over last %d aborting",
                        np_rate, _NOT_PUBLISHED_CIRCUIT_THRESHOLD,
                        _CIRCUIT_WINDOW,
                    )
                    _STOP.set()
                    break

                now = time.monotonic()
                if now - last_heartbeat >= log_interval:
                    try:
                        remaining = _count_remaining(driver)
                    except Neo4jError:
                        remaining = None
                    logger.info(stats.line(
                        remaining=remaining,
                        current_uuid=heartbeat_state.get("current_uuid"),
                        attempt=heartbeat_state.get("attempt"),
                        last_status=heartbeat_state.get("last_status"),
                    ))
                    last_heartbeat = now

                stalled_for = (
                    time.monotonic() - stats.last_ted_response_mono
                )
                if stalled_for > _STALLED_WARN_SEC:
                    logger.warning(
                        "ted_stalled no_response_for=%.0fs "
                        "current_uuid=%s attempt=%s",
                        stalled_for,
                        heartbeat_state.get("current_uuid"),
                        heartbeat_state.get("attempt"),
                    )

                if max_rows is not None and (
                    stats.resolved + stats.not_published + stats.transient
                ) >= max_rows:
                    logger.info("max_rows reached stopping")
                    _STOP.set()
                    break

    logger.info("final %s", stats.line())
    summary = {
        "started": stats.started_at_iso,
        "ended": datetime.now(timezone.utc).isoformat(),
        "claimed": stats.claimed,
        "resolved": stats.resolved,
        "not_published": stats.not_published,
        "transient": stats.transient,
        "written": stats.written,
        "write_failures": stats.write_failures,
        "ted_429": stats.ted_429,
        "ted_5xx": stats.ted_5xx,
        "ted_4xx_other": stats.ted_4xx_other,
    }
    logger.info("SUMMARY %s", json.dumps(summary, separators=(",", ":")))
    return stats


def _parse_args(argv: list) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="backfill_ted_publication_numbers")
    p.add_argument("--batch-size", type=int, default=200)
    p.add_argument(
        "--rate", type=float, default=3.0,
        help=(
            "TED req/s ceiling overridden live by "
            "BACKFILL_THROTTLE_FILE or SIGUSR1"
        ),
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-rows", type=int, default=None)
    p.add_argument("--log-interval", type=int, default=_HEARTBEAT_SEC)
    p.add_argument(
        "--grace-seconds", type=int, default=180,
        help="must match terminationGracePeriodSeconds on the Job",
    )
    p.add_argument(
        "--skip-validation", action="store_true",
        help=(
            "skip the 10-UUID validation gate "
            "use only after first canary run"
        ),
    )
    p.add_argument("--neo4j-uri", default=os.environ.get("NEO4J_URI"))
    p.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USER"))
    p.add_argument(
        "--neo4j-password", default=os.environ.get("NEO4J_PASSWORD"),
    )
    return p.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    args = _parse_args(
        argv if argv is not None else sys.argv[1:],
    )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.Formatter.converter = time.gmtime

    if not all([args.neo4j_uri, args.neo4j_user, args.neo4j_password]):
        logger.error("missing NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD")
        return 2

    _install_signal_handlers(args.grace_seconds)

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password),
    )
    try:
        driver.verify_connectivity()
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("neo4j connectivity failed: %s", exc)
        driver.close()
        return 3

    try:
        stats = run(
            driver=driver,
            batch_size=args.batch_size,
            rate=args.rate,
            dry_run=args.dry_run,
            max_rows=args.max_rows,
            log_interval=args.log_interval,
            skip_validation=args.skip_validation,
        )
    finally:
        driver.close()

    if not args.dry_run and stats.claimed > 0 and stats.written == 0:
        logger.error(
            "exit_nonzero claimed=%d but written=0", stats.claimed,
        )
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
