-- Wikidata relay: migrate from per-event buffer to dirty-set.
--
-- Run once against the events Postgres (gmr_app DB) before deploying
-- the new relay image. The new relay code reads/writes
-- `wikidata.dirty_entities`; the old `wikidata.recentchange` table is
-- left in place by this migration, but its data is collapsed into the
-- new table first so we don't lose the 3M-event backlog accumulated
-- during the bulk-load window.
--
-- After this runs cleanly and the new relay is live + healthy, drop
-- the old table separately:
--     DROP TABLE wikidata.recentchange;

BEGIN;

-- The dirty-set table itself. One row per Wikidata entity that has
-- pending work for the worker. `is_deleted=true` is a tombstone --
-- the worker DELETEs all triples for that entity from Virtuoso
-- without an API fetch. `is_deleted=false` is a "needs refetch"
-- marker; the worker fetches truthy, filters, and rewrites.
CREATE TABLE IF NOT EXISTS wikidata.dirty_entities (
    entity_id          TEXT PRIMARY KEY,
    last_changed_at    TIMESTAMPTZ NOT NULL,
    first_seen_at      TIMESTAMPTZ NOT NULL,
    edit_count         BIGINT NOT NULL DEFAULT 1,
    last_comment_kind  TEXT,
    is_deleted         BOOLEAN NOT NULL DEFAULT FALSE
);

-- The worker drains in `ORDER BY last_changed_at` so it processes
-- the oldest pending work first — bounds staleness.
CREATE INDEX IF NOT EXISTS dirty_entities_changed_idx
    ON wikidata.dirty_entities (last_changed_at);

-- One-shot backfill. Two CTEs:
--   per_entity   — aggregates count + min/max ts per entity
--   latest_event — extracts the single most-recent event row per
--                  entity using a window function; tombstone iff
--                  that latest event was a real entity deletion
-- The join is on entity_id; both CTEs scan recentchange once.
WITH per_entity AS (
    SELECT
        entity_id,
        max(event_ts) AS last_changed_at,
        min(event_ts) AS first_seen_at,
        count(*)      AS edit_count
    FROM wikidata.recentchange
    WHERE entity_id IS NOT NULL
    GROUP BY entity_id
),
ranked AS (
    SELECT
        entity_id,
        edit_type,
        payload,
        row_number() OVER (PARTITION BY entity_id
                           ORDER BY event_ts DESC) AS rn
    FROM wikidata.recentchange
    WHERE entity_id IS NOT NULL
),
latest_event AS (
    SELECT
        entity_id,
        edit_type = 'log'
            AND payload->>'log_type'   = 'delete'
            AND payload->>'log_action' = 'delete' AS is_deleted_latest
    FROM ranked
    WHERE rn = 1
)
INSERT INTO wikidata.dirty_entities
    (entity_id, last_changed_at, first_seen_at, edit_count,
     last_comment_kind, is_deleted)
SELECT
    pe.entity_id,
    pe.last_changed_at,
    pe.first_seen_at,
    pe.edit_count,
    NULL,                          -- backfill rows have no comment_kind
    le.is_deleted_latest
FROM per_entity pe
JOIN latest_event le USING (entity_id)
ON CONFLICT (entity_id) DO UPDATE
    SET last_changed_at = GREATEST(
            wikidata.dirty_entities.last_changed_at,
            EXCLUDED.last_changed_at),
        first_seen_at   = LEAST(
            wikidata.dirty_entities.first_seen_at,
            EXCLUDED.first_seen_at),
        edit_count      = wikidata.dirty_entities.edit_count
                          + EXCLUDED.edit_count,
        is_deleted      = wikidata.dirty_entities.is_deleted
                          OR EXCLUDED.is_deleted;

COMMIT;

-- Sanity output. Useful when running via
--     kubectl exec ... psql -f wikidata_dirty_set_migration.sql
SELECT
    count(*) FILTER (WHERE is_deleted = FALSE) AS pending_refetch,
    count(*) FILTER (WHERE is_deleted = TRUE)  AS pending_tombstone,
    min(first_seen_at)                          AS earliest_seen,
    max(last_changed_at)                        AS latest_change
FROM wikidata.dirty_entities;
