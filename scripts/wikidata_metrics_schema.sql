-- Two small tables backing the Prometheus metrics endpoint on the
-- relay (src/relay/metrics.py).
--
-- wikidata.consumer_runs   — per-batch summary written by the
--                            consumer at end of each CronJob run.
--                            The relay's metrics-refresh thread
--                            polls this and increments
--                            wikidata_consumer_entities_total
--                            counters by the row deltas.
--
-- wikidata.metrics_state   — singleton bookmark row. Stores the
--                            highest consumer_runs.id we've already
--                            folded into Prometheus counters, so
--                            the relay survives restarts without
--                            double-counting.

CREATE TABLE IF NOT EXISTS wikidata.consumer_runs (
    id                       BIGSERIAL PRIMARY KEY,
    started_at               TIMESTAMPTZ NOT NULL,
    finished_at              TIMESTAMPTZ NOT NULL,
    leased                   INTEGER NOT NULL,
    written                  INTEGER NOT NULL,
    tombstoned               INTEGER NOT NULL,
    redirected               INTEGER NOT NULL,
    not_found_left_pending   INTEGER NOT NULL,
    errors                   INTEGER NOT NULL
);

-- Used by the relay's catch-up query in metrics.py.
CREATE INDEX IF NOT EXISTS consumer_runs_id_idx
    ON wikidata.consumer_runs (id);

CREATE TABLE IF NOT EXISTS wikidata.metrics_state (
    id                        INTEGER PRIMARY KEY DEFAULT 1,
    last_consumer_run_id      BIGINT NOT NULL DEFAULT 0,
    CHECK (id = 1)
);

-- Seed row so the catch-up SELECT doesn't return empty on the
-- first poll.
INSERT INTO wikidata.metrics_state (id, last_consumer_run_id)
VALUES (1, 0)
ON CONFLICT (id) DO NOTHING;
