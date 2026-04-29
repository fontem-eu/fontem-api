"""Per-source freshness markers — single source of truth for both
the data-quality dashboard and the assistant's system-prompt
preamble.

Every loader calls :func:`update_source` at the end of a successful
run with its own canonical id, the date range of the data it just
ingested, and the row count. Nodes are MERGE'd by id so re-runs
update in place.

Schema::

    (:DataSource {
       id:                "sanctions",          // canonical, kebab-case
       label:             "EU consolidated sanctions",
       coverage_start:    "2026-01-01",         // earliest data covered (ISO date)
       coverage_end:      "2026-04-29",         // latest data covered  (ISO date)
       last_loaded:       <neo4j datetime>,     // wall-clock UTC of this run
       record_count:      3015,
       expected_cadence_hours: 25,              // for the freshness gate
    })

Best-effort: failures are logged and swallowed. The ETL never fails
because the freshness write failed — that would block the actual
data load for a monitoring side-effect, which is the wrong tradeoff.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


_CONSTRAINT_CYPHER = (
    "CREATE CONSTRAINT data_source_id IF NOT EXISTS "
    "FOR (s:DataSource) REQUIRE s.id IS UNIQUE"
)

_MERGE_CYPHER = """
MERGE (s:DataSource {id: $id})
ON CREATE SET s.created_at = datetime()
SET s.label                  = $label,
    s.coverage_start         = $coverage_start,
    s.coverage_end           = $coverage_end,
    s.last_loaded            = datetime($last_loaded),
    s.record_count           = $record_count,
    s.expected_cadence_hours = $expected_cadence_hours
"""


def update_source(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    driver,
    *,
    source_id: str,
    label: str,
    coverage_start: str | None = None,
    coverage_end: str | None = None,
    record_count: int = 0,
    expected_cadence_hours: int = 25,
) -> None:
    """Write a single ``:DataSource`` marker.

    ``driver`` may be either a sync ``GraphDatabase.driver`` or any
    object exposing ``.session()`` (we don't rely on async here — the
    helper is called from sync ETL loaders).

    All failures are logged and swallowed. This call is monitoring
    plumbing, not a data-correctness step.
    """
    if not source_id or not label:
        logger.warning("freshness: missing source_id or label, skipping")
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        with driver.session() as session:
            # Idempotent: every loader calls this, the first one through
            # creates the constraint, the rest are no-ops.
            session.run(_CONSTRAINT_CYPHER)
            session.run(
                _MERGE_CYPHER,
                id=source_id,
                label=label,
                coverage_start=coverage_start,
                coverage_end=coverage_end,
                last_loaded=now_iso,
                record_count=int(record_count or 0),
                expected_cadence_hours=int(expected_cadence_hours or 25),
            )
            logger.info(
                "freshness: marker updated source=%s coverage=%s..%s rows=%d",
                source_id, coverage_start or "?", coverage_end or "?", record_count or 0,
            )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("freshness: marker update failed for %s: %s", source_id, exc)
