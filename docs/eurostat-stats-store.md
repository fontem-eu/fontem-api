# Eurostat stats store: design + plan

A new Postgres deployment (with TimescaleDB + PostGIS extensions) to hold
Eurostat regional time-series data and NUTS region geometries, plus a
generic ETL framework that syncs any Eurostat dataset by config rather than
by per-dataset script.

This is the storage + ingest backbone for the bivariate-analysis layer of
Fontem ("cohesion funding per capita × procurement value per capita at
NUTS-2", and similar). The `:NUTSRegion` graph anchors in Neo4j stay where
they are; numeric time series move out of the graph.

---

## Context

- We picked **Postgres + TimescaleDB + PostGIS** in a previous discussion. Volume
  estimate: ~120 MB raw TSV / ~5–10 MB Timescale-compressed for ~26 datasets,
  ~5–10M sparse rows. PostGIS is for the NUTS polygons + future map overlays.
- User direction: **separate Postgres deployment**, not shared with the
  existing `gmr-postgres` instance. Reasons: blast-radius isolation, different
  workload profile (analytical / time-series vs OLTP for community-api),
  cleaner ops once the dataset count grows.
- User direction: **internal catalog table** tracking datasets + their sync
  state, and a **single ETL framework** that drives every dataset from
  config.

Out of scope for this plan, called out so they don't drift in:

- Eurostat data going *into* Neo4j. Numeric Eurostat lives in Postgres only.
  Neo4j keeps the slim `(:NUTSRegion {code, name, level})` nodes as graph
  anchors so `(:Company)-[:LOCATED_IN]->(:NUTSRegion)` continues to work.
- Migration of existing graph-side NUTS data ([edgar-gmr-etl/src/etl/load_nuts.py](src/etl/load_nuts.py),
  [edgar-gmr-etl/src/etl/link_entities_to_nuts.py](src/etl/link_entities_to_nuts.py)).
  Those keep running; the only change is that the new `nuts_region` Postgres
  table becomes the authoritative source for *geometry + area + parent
  hierarchy*, and the Cypher loaders read from there.
- The bivariate dashboard view itself ([gmr-web](../../gmr-web/) frontend).
  Separate work — this plan ships the data; the view consumes it later.
- Non-Eurostat statistical sources (national stats institutes, Copernicus,
  OECD). The schema is generic enough to hold them; the ETL framework is too;
  but the initial load is Eurostat-only.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  Eurostat dissemination API + bulk TSV (cdn.dissem ec.europa.eu)     │
└──────────────┬───────────────────────────────────────────────────────┘
               │
               │ daily/weekly pull (cron)
               ▼
┌──────────────────────────────────────────────────────────────────────┐
│  fontem_stats ETL (new package in edgar-gmr-etl/src/stats_etl/)       │
│   - generic loader driven by fontem_stats.dataset catalog rows        │
│   - one CLI entrypoint, one Docker image, one CronJob template        │
└──────────────┬───────────────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────────────┐
│  fontem-stats Postgres (new StatefulSet, gmr ns)                      │
│   image: timescale/timescaledb-ha:pg16-ts2.x  (PostGIS bundled)       │
│   schema: fontem_stats                                                │
│     - dataset           (catalog: code, label, dims, schedule)        │
│     - sync_run          (history: per-pull status, rows, errors)      │
│     - observation       (hypertable: dataset × time × geo × dims)     │
│     - nuts_region       (PostGIS polygons + hierarchy)                │
│     - nuts_region_xref  (region code mapping across NUTS revisions)   │
└──────────────┬───────────────────────────────────────────────────────┘
               │
               │ read by:
               │   - fontem-api (bivariate analysis endpoints)
               │   - edgar-gmr-etl (NUTS hierarchy backfill into Neo4j)
               │   - DQ dashboard (sync_run + dataset coverage panels)
               ▼
┌──────────────────────────────────────────────────────────────────────┐
│  gmr-web bivariate dashboard, /data-quality stats panel, etc.         │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Storage: new Postgres deployment

Mirrors the existing PG StatefulSet pattern in [gitops/infra/prod.yaml](../../gitops/infra/prod.yaml)
(see lines 228–315 for the `gmr-postgres` precedent). Key differences:

- **Image**: `timescale/timescaledb-ha:pg16-ts2.x` (PostGIS bundled in this
  image; saves us building our own postgres+pgis+timescale image).
- **Service name**: `fontem-stats-postgres` (so DNS doesn't collide with the
  existing `postgresql` service).
- **Namespace**: `gmr` (same as everything else for now; isolation is
  workload-level, not namespace-level).
- **Storage**: dedicated NFS-backed PV at `/srv/nfs/fontem-stats-prod`,
  separate from `/srv/nfs/postgres-data-prod`. Sizing: 50 GB initial — gives
  ~500× headroom on raw data and lots of room for future Copernicus rasters.
- **Vault-managed creds**: a new Vault path `gmr/fontem-stats-postgres`,
  synced via `VaultStaticSecret` to a `fontem-stats-credentials` K8s secret.
  Mirror the pattern at [gitops/infra/prod.yaml:55–69](../../gitops/infra/prod.yaml).
- **Init**: a small initContainer / SQL bootstrap that runs `CREATE EXTENSION
  timescaledb; CREATE EXTENSION postgis;` on first start.
- **Per-environment**: full deployment in `gmr-staging` + `gmr` (prod). No
  dast/dev for now — the ETL is destructive only via UPSERT, no data
  segregation needed.
- **Backups**: pgBackRest sidecar to S3-compatible storage (MinIO already
  deployed in cluster). Daily base + WAL archiving. Same pattern as the
  existing PG would use; keep retention 14 days.
- **Resources**: `requests: 1Gi/500m`, `limits: 2Gi/1`. Single-node cluster
  budget. Bump after full ETL load is measured.

### ArgoCD wiring

Add a new ApplicationSet entry — the existing pattern in
[gitops/appset.yaml](../../gitops/appset.yaml) generates one Application per
service per environment. Either:

- Extend the matrix to include `fontem-stats-postgres` as a new appName, or
- Add a singleton Application (matching `gmr-consolidator` / `gmr-linguistics`
  precedent in the same file) — which is closer to reality since the
  Eurostat data is shared across all envs reading the same prod copy.

Singleton fits better. The `fontem-stats-postgres` chart lives at
`edgar-gmr-etl/deployment-stats/` (new sibling chart; doesn't pollute the
existing `deployment/` chart that ships the fontem-api app).

---

## Schema design

One Postgres schema `fontem_stats` to namespace everything. All tables below
live there.

### `dataset` — the catalog

Source of truth for what we sync, how often, and what shape the data has.
Insert a row to add a new dataset; the ETL picks it up on next run.

```sql
CREATE TABLE fontem_stats.dataset (
    code              text        PRIMARY KEY,            -- 'demo_r_pjangrp3'
    label             text        NOT NULL,               -- human-readable
    theme             text        NOT NULL,               -- 'population', 'economy', ...
    source            text        NOT NULL DEFAULT 'eurostat',
    source_url        text        NOT NULL,               -- bulk TSV URL
    nuts_levels       smallint[]  NOT NULL,               -- e.g. ARRAY[2,3]
    dim_ids           text[]      NOT NULL,               -- ['sex','age','unit']
    dim_sizes         int[]       NOT NULL,               -- [3, 24, 1]
    time_unit         text        NOT NULL DEFAULT 'year',-- year/month/week
    update_freq       interval    NOT NULL,               -- '1 year', '1 month'
    enabled           boolean     NOT NULL DEFAULT true,
    notes             text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);
```

`enabled = false` lets us pause a dataset without deleting it.

### `sync_run` — history

Every ETL invocation against a dataset writes one row. Drives the DQ
dashboard's "freshness" panel + alerting on consecutive failures.

```sql
CREATE TABLE fontem_stats.sync_run (
    id                bigserial   PRIMARY KEY,
    dataset_code      text        NOT NULL REFERENCES fontem_stats.dataset(code),
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,
    status            text        NOT NULL CHECK (status IN
                                    ('running','success','failed','skipped')),
    upstream_modified timestamptz,                        -- Eurostat 'updated' header
    rows_inserted     bigint      DEFAULT 0,
    rows_updated      bigint      DEFAULT 0,
    rows_total        bigint      DEFAULT 0,
    error_message     text
);
CREATE INDEX ON fontem_stats.sync_run (dataset_code, started_at DESC);
CREATE INDEX ON fontem_stats.sync_run (status, started_at DESC)
    WHERE status IN ('running','failed');
```

### `observation` — the hypertable

One universal table, not per-dataset. Trades a small per-query filter cost
for a much simpler ETL and operational story (one set of indexes, one
compression policy, one continuous-aggregate framework).

```sql
CREATE TABLE fontem_stats.observation (
    dataset_code text             NOT NULL REFERENCES fontem_stats.dataset(code),
    time         timestamptz      NOT NULL,
    geo_code     text             NOT NULL,
    dimensions   jsonb            NOT NULL DEFAULT '{}'::jsonb,
    value        double precision,
    flags        text[],
    PRIMARY KEY (dataset_code, time, geo_code, dimensions)
);

SELECT create_hypertable('fontem_stats.observation', 'time',
    chunk_time_interval => INTERVAL '5 years');

CREATE INDEX ON fontem_stats.observation (dataset_code, geo_code, time DESC);
CREATE INDEX ON fontem_stats.observation (geo_code, time DESC)
    INCLUDE (dataset_code, value);
CREATE INDEX ON fontem_stats.observation USING GIN (dimensions jsonb_path_ops);

ALTER TABLE fontem_stats.observation SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'dataset_code, geo_code',
    timescaledb.compress_orderby   = 'time DESC'
);
SELECT add_compression_policy('fontem_stats.observation', INTERVAL '1 year');
```

Notes:
- `dimensions JSONB` because Eurostat dim sets vary wildly by dataset
  (sex×age, sectperf×unit, c_resid×nace_r2, etc.). A fixed-column shape
  would explode. GIN index on `jsonb_path_ops` handles `dimensions @> '{"sex":"F"}'`
  cleanly.
- 5-year chunks: most queries are 1–10 year ranges; this gives 1–2 chunks
  per query. With ~5–10M rows total across 25–30 years, chunks are small
  and the planner picks them efficiently.
- Compression after 1y old: still 100% queryable, ~5× smaller on disk.
- PRIMARY KEY makes upserts (`ON CONFLICT DO UPDATE`) the natural ETL
  pattern; idempotent re-runs are free.

### `nuts_region` — PostGIS

```sql
CREATE TABLE fontem_stats.nuts_region (
    code         text                       PRIMARY KEY,        -- 'DE21A', 'BE100'
    level        smallint                   NOT NULL CHECK (level BETWEEN 0 AND 3),
    name         text                       NOT NULL,
    name_native  text,
    parent_code  text                       REFERENCES fontem_stats.nuts_region(code),
    country_code char(2)                    NOT NULL,
    geometry     geometry(MultiPolygon,4326) NOT NULL,
    area_km2     double precision           GENERATED ALWAYS AS
                                              (ST_Area(geometry::geography) / 1e6) STORED,
    nuts_version text                       NOT NULL,            -- '2024', '2021', ...
    valid_from   date                       NOT NULL,
    valid_until  date,                                            -- NULL = current
    updated_at   timestamptz                NOT NULL DEFAULT now()
);
CREATE INDEX ON fontem_stats.nuts_region USING GIST (geometry);
CREATE INDEX ON fontem_stats.nuts_region (level);
CREATE INDEX ON fontem_stats.nuts_region (parent_code);
CREATE INDEX ON fontem_stats.nuts_region (country_code, level);
```

Geometry comes from [Eurostat GISCO](https://ec.europa.eu/eurostat/web/gisco/geodata/statistical-units/territorial-units-statistics).
Versioning matters because NUTS revisions reshuffle codes every ~3 years
(NUTS 2013 → 2016 → 2021 → 2024…); `valid_from`/`valid_until` lets us answer
"which polygon was DE21A in 2018?".

### `nuts_region_xref` — code mapping across NUTS revisions

```sql
CREATE TABLE fontem_stats.nuts_region_xref (
    from_version text NOT NULL,
    from_code    text NOT NULL,
    to_version   text NOT NULL,
    to_code      text NOT NULL,
    relation     text NOT NULL CHECK (relation IN
                   ('identical','renamed','split','merged','absorbed')),
    PRIMARY KEY (from_version, from_code, to_version, to_code)
);
```

Rarely used but essential when a query crosses the 2021/2024 revision
boundary. Eurostat publishes the mapping CSVs.

---

## ETL framework

Single Python package `src/stats_etl/` in this repo (sibling to the existing
`src/etl/`). Drives every dataset from the `dataset` catalog row — adding a
new dataset is *insert + run*, no new code.

### Layout

```
edgar-gmr-etl/
└── src/
    └── stats_etl/
        ├── __init__.py
        ├── catalog.py         # dataset + sync_run repos (dishka-injected)
        ├── eurostat_source.py # SDMX-JSON + bulk TSV upstream client
        ├── geo_levels.py      # NUTS code → level detection (already in /tmp/eurostat_probe.py)
        ├── loader.py          # generic per-dataset loader (the core)
        ├── nuts_loader.py     # one-off PostGIS polygon loader (different shape, separate module)
        ├── orchestrator.py    # `sync --all` driver + scheduler glue
        ├── schema.sql         # the schema above (idempotent CREATE IF NOT EXISTS)
        └── cli.py             # argparse entrypoint
```

### Generic loader contract

```python
# stats_etl/loader.py — sketch
class EurostatLoader:
    def __init__(
        self,
        source: EurostatSource,    # talks to dissemination API
        catalog: CatalogRepo,      # writes dataset / sync_run rows
        obs:     ObservationRepo,  # bulk-upserts into observation
    ): ...

    def sync(self, dataset_code: str, since: datetime | None = None) -> SyncRun:
        ds = self.catalog.get_dataset(dataset_code)
        run = self.catalog.start_run(ds.code)
        try:
            meta = self.source.fetch_metadata(ds.code)
            if meta.upstream_modified <= ds.last_synced_at and not since:
                self.catalog.finish_run(run.id, status='skipped')
                return run
            for batch in self.source.iter_observations(ds, since=since):
                rows_in, rows_up = self.obs.bulk_upsert(ds.code, batch)
                run.rows_inserted += rows_in
                run.rows_updated  += rows_up
            self.catalog.finish_run(run.id, status='success',
                upstream_modified=meta.upstream_modified)
        except Exception as exc:
            self.catalog.finish_run(run.id, status='failed',
                error_message=str(exc))
            raise
        return run
```

`EurostatSource.iter_observations` parses the SDMX-JSON response (or bulk
TSV for big datasets), yields `(time, geo_code, dimensions_dict, value, flags)`
tuples in batches of ~10k. The "is it newer than last time?" check uses the
`updated` header in the SDMX-JSON response — Eurostat returns it on every
query, so we get free freshness detection without reading observations.

### CLI surface

```
# Sync one dataset
python -m src.stats_etl sync demo_r_pjangrp3

# Sync all enabled datasets, sequentially
python -m src.stats_etl sync --all

# Sync everything that hasn't been synced in 7d
python -m src.stats_etl sync --stale-after 7d

# Force a full re-sync of one dataset (skip the upstream-not-changed shortcut)
python -m src.stats_etl sync demo_r_pjangrp3 --force

# Add a new dataset to the catalog (idempotent)
python -m src.stats_etl register --code demo_r_d3dens --theme population --nuts-levels 2,3

# One-off: load NUTS polygons from GISCO
python -m src.stats_etl nuts-polygons --version 2024
```

Mirrors the existing argparse-based ETL CLIs in this repo (e.g.
[src/etl/load_eu_sanctions.py](../src/etl/load_eu_sanctions.py)).

### Scheduling

One Kubernetes CronJob per *cadence*, not per dataset:

- `stats-sync-daily` — runs `sync --stale-after 1d` daily at 03:00 UTC. Most
  Eurostat regional datasets update annually; the daily check is cheap (a
  HEAD-equivalent for each dataset's `updated` header) and catches the rare
  monthly/weekly ones.
- `stats-sync-weekly` — runs `sync --all` Sunday at 04:00 UTC. Catches
  anything that slipped past the daily check (e.g., a dataset with a
  delayed `updated` timestamp).

Both deployed alongside the existing `gmr-smoke-tests-prod` CronJob pattern.

---

## Initial dataset list (26)

Picked for: highest analytical value × deepest NUTS reach × smallest size.
Total uncompressed: ~120 MB raw TSV; Timescale-compressed: probably under
10 MB. Loadable to a fresh database in well under an hour from cold.

### Population & demography (8)
| Code | What | Reach | Time | gz |
|---|---|---|---|---|
| `demo_r_pjangrp3` | Population × age × sex × NUTS-3 | NUTS-3 | 2014–2025 | 4.4 MB |
| `demo_r_pjanaggr3` | Population aggregates × NUTS-3 (broad age) | NUTS-3 | 1990–2024 | – |
| `demo_r_d3dens` | Population density × NUTS-3 | NUTS-3 | 1990–2024 | 0.12 MB |
| `demo_r_gind3` | Demographic balance + crude rates × NUTS-3 | NUTS-3 | 2000–2025 | 1.1 MB |
| `demo_r_births` | Live births × NUTS-3 | NUTS-3 | 1990–2024 | 0.14 MB |
| `demo_r_magec3` | Deaths × age × sex × NUTS-3 | NUTS-3 | 1990–2024 | 2.2 MB |
| `demo_r_mwk3_t` | Weekly deaths × NUTS-3 (excess-mortality) | NUTS-3 | 2000-W01 → | 1.7 MB |
| `demo_r_minfind` | Infant mortality × NUTS-2 | NUTS-2 | 1990–2024 | – |

### Life expectancy & health (3)
| Code | What | Reach | Time | gz |
|---|---|---|---|---|
| `demo_r_mlifexp` | Life expectancy × age × sex × NUTS-2 | NUTS-2 | 1990–2024 | – |
| `hlth_cd_acdr2` | Causes of death × NUTS-2 (3y averages) | NUTS-2 | 2011–2023 | – |
| `hlth_rs_bdsrg` | Hospital beds × NUTS-2 (frozen 2016) | NUTS-2 | 1993–2016 | – |

### Economy (5)
| Code | What | Reach | Time | gz |
|---|---|---|---|---|
| `nama_10r_3gdp` | GDP × NUTS-3 | NUTS-3 | 2000–2024 | 0.74 MB |
| `nama_10r_3gva` | GVA × NUTS-3 | NUTS-3 | 2000–2024 | – |
| `nama_10r_3popgdp` | Population for GDP/cap × NUTS-3 | NUTS-3 | 2000–2024 | 0.12 MB |
| `nama_10r_2gdp` | GDP × NUTS-2 | NUTS-2 | 2000–2024 | 0.20 MB |
| `nama_10r_2hhinc` | Household disposable income × NUTS-2 | NUTS-2 | 2000–2024 | 0.82 MB |

### Labour market (3)
| Code | What | Reach | Time | gz |
|---|---|---|---|---|
| `lfst_r_lfu3rt` | Unemployment rate × education × NUTS-2 | NUTS-2 | 1999–2025 | 2.0 MB |
| `lfst_r_lfp2act` | Labour force × NUTS-2 | NUTS-2 | 1999–2025 | – |
| `lfst_r_lfe2en2` | Employed × NACE × NUTS-2 | NUTS-2 | 2008–2025 | – |

### Education & R&D (4)
| Code | What | Reach | Time | gz |
|---|---|---|---|---|
| `edat_lfse_04` | Population × educational attainment × NUTS-2 | NUTS-2 | 2000–2025 | 1.3 MB |
| `rd_e_gerdreg` | R&D expenditure (% GDP) × NUTS-2 | NUTS-2 | 1980–2024 | 1.1 MB |
| `rd_p_persreg` | R&D personnel × NUTS-2 | NUTS-2 | 1980–2024 | – |
| `htec_emp_reg2` | High-tech employment × NUTS-2 | NUTS-2 | 2008–2025 | – |

### Social, mobility, geometry (3)
| Code | What | Reach | Time | gz |
|---|---|---|---|---|
| `ilc_li41` | At-risk-of-poverty rate × NUTS-2 | NUTS-2 | 2003–2025 | – |
| `isoc_r_iuse_i` | Internet users × NUTS-2 | NUTS-2 | 2006–2025 | 0.18 MB |
| `tour_occ_nin2` | Nights at tourist accommodation × NUTS-3 | NUTS-3 | 1990–2024 | 1.3 MB |
| `tran_r_vehst` | Stock of vehicles × NUTS-2 | NUTS-2 | 1990–2024 | – |
| `reg_area3` | NUTS-3 area km² (one-off) | NUTS-3 | static | 0.04 MB |

Crime is **deliberately excluded**: Eurostat publishes only at country level
(`crim_off_cat`, etc.). Our user mentioned crime as a candidate variable;
we'll need national-stats-institute feeds (INE/INSEE/ISTAT/Destatis) if we
want it at NUTS-2/3, which is a separate, much heavier project. Flag it as
a known gap on the data-quality dashboard rather than trying to harmonise
27 national feeds in this scope.

---

## Implementation phases

Roughly 3–4 weeks of part-time work; squeezable if a heavy week lands.

### Phase A — infra (week 1)
1. Helm chart at [edgar-gmr-etl/deployment-stats/](../deployment-stats/)
   (sibling to existing `deployment/`). StatefulSet, Service, PVC pulling
   the new NFS PV.
2. Vault path `gmr/fontem-stats-postgres` provisioned + `VaultStaticSecret`
   resource added to [gitops/infra/prod.yaml](../../gitops/infra/prod.yaml).
3. ArgoCD Application (singleton, like `gmr-consolidator`) in
   [gitops/appset.yaml](../../gitops/appset.yaml).
4. NFS export `/srv/nfs/fontem-stats-prod` provisioned on the storage host.
5. Bootstrap SQL — `CREATE SCHEMA fontem_stats; CREATE EXTENSION
   timescaledb; CREATE EXTENSION postgis;` plus the table DDL above. Run on
   first-start via initContainer or a one-off Job. Idempotent so re-runs are
   safe.

**Phase A gate**: `kubectl exec -it fontem-stats-postgres-0 -- psql -U
fontem_stats -c '\dx'` shows both extensions; `\dt fontem_stats.*` lists the
five tables; `SELECT 1 FROM fontem_stats.observation LIMIT 1` returns
empty cleanly.

### Phase B — ETL skeleton (week 2)
6. New package `src/stats_etl/` with the layout above. Reuse:
   - dishka DI (`Neo4jClient` precedent → new `StatsPostgresClient`)
   - argparse CLI shape from `src/etl/load_eu_sanctions.py`
   - `httpx.AsyncClient` upstream pattern
7. NUTS polygon loader: pull from
   [GISCO NUTS 2024 download API](https://ec.europa.eu/eurostat/web/gisco/geodata/statistical-units/territorial-units-statistics).
   GeoJSON, single one-off load.
8. Generic loader against the 5 smallest datasets first (`demo_r_d3dens`,
   `demo_r_births`, `nama_10r_3popgdp`, `reg_area3`, `isoc_r_iuse_i`). Get
   the contract right before scaling.

**Phase B gate**: `python -m src.stats_etl sync demo_r_d3dens` runs
end-to-end, leaves a `success` row in `sync_run` and ~74k rows in
`observation`. NUTS polygons rendered correctly via
`SELECT ST_AsGeoJSON(geometry) FROM fontem_stats.nuts_region WHERE code='DE'`.

### Phase C — full load + scheduling (week 3)
9. Load remaining 21 datasets via `sync --all`. Cold load measured; resource
   limits adjusted if needed.
10. CronJob manifests for daily and weekly sync. Mirror the
    [gmr-smoke-tests-prod CronJob pattern](../../gmr-smoke-tests/deployment/cronjob.yaml)
    — same imagePullSecret, linkerd-disabled annotation, Kuma push for
    success/failure signal.
11. DQ dashboard: extend [edgar-gmr-etl/src/api/routers/data_quality.py](../src/api/routers/data_quality.py)
    with a `/data-quality/eurostat` endpoint reading `dataset` + `sync_run`
    aggregates (last sync, freshness in days, row count, error history).
    A new card on [DataQualityHubView.vue](../../gmr-web/src/views/DataQualityHubView.vue).

**Phase C gate**: all 26 datasets in `enabled=true` state with at least one
`success` sync_run; CronJob has run unattended for 48h; DQ dashboard shows
green status for each dataset.

### Phase D — read path + Neo4j NUTS hierarchy backfill (week 4)
12. Read endpoint on `fontem-api`: `GET /stats/series?dataset=<code>&geo=<code>&from=<year>&to=<year>`
    returns the time-series. Joinable to the existing entity APIs by NUTS
    code.
13. Use `nuts_region` to backfill the NUTS-1/2/3 hierarchy in Neo4j —
    addresses the long-standing CLAUDE.md gap ("only level 0 (39 countries),
    need levels 1–3 ~1960 regions"). New script
    `edgar-gmr-etl/src/etl/sync_nuts_from_stats.py` reads from Postgres,
    MERGEs `(:NUTSRegion)` nodes + `(:NUTSRegion)-[:PART_OF]->(:NUTSRegion)`
    edges. Existing `link_entities_to_nuts.py` consumers keep working.

**Phase D gate**: `/data-quality/nuts` shows `by_level: [{level:0, n:39},
{level:1, n: ~125}, {level:2, n: ~352}, {level:3, n: ~1620}]`. A bivariate
test query (e.g., `SELECT … FROM observation o JOIN nuts_region r ON …
WHERE level=2`) returns in <100ms for the cohesion-vs-procurement payload.

---

## Operational considerations

- **Sizing review** at end of Phase C. If any single dataset's chunk size
  goes over 1 GB before compression, lower `chunk_time_interval` for the
  hypertable. Unlikely at this volume but worth checking.
- **Backup**: pgBackRest sidecar to MinIO. Daily base, WAL archived. Test
  restore quarterly into the dast environment.
- **Observability**: Postgres + Timescale expose stats via
  `pg_stat_*` views. Once Loki + Grafana land
  ([Fontem launch calendar memory](/config/.claude/projects/-config-repos/memory/project_fontem_launch_calendar.md)),
  a single Grafana dashboard with rows-per-table, chunk count, compression
  ratio, slow-query log.
- **Smoke**: extend the existing smoke suite with one read-only query
  against `fontem_stats.observation` so a broken Postgres deployment shows
  up alongside graph API and consolidator failures.
- **Schema migrations**: future schema changes go through Alembic. New
  migration directory `edgar-gmr-etl/src/stats_etl/migrations/`. First
  migration is the bootstrap above; everything after is a delta.
- **Failure modes**: Eurostat API returns 4xx/5xx surprisingly often during
  their nightly publish window; the loader already retries with exponential
  backoff and falls through to bulk-TSV if SDMX-JSON consistently fails.
  Failed runs leave `sync_run.status='failed'` for the alerter.

---

## Verification

End-to-end sanity check after Phase D:

```sql
-- bivariate sanity: GDP per capita vs unemployment, NUTS-2, latest year
WITH gdp AS (
  SELECT geo_code,
         (value /
          (SELECT value
             FROM fontem_stats.observation
             WHERE dataset_code = 'nama_10r_3popgdp'
               AND geo_code = o.geo_code
               AND time = o.time
             LIMIT 1)
         ) AS gdp_per_cap
  FROM fontem_stats.observation o
  WHERE dataset_code = 'nama_10r_2gdp' AND time = '2023-01-01'
),
unemp AS (
  SELECT geo_code, value AS unemp_rate
  FROM fontem_stats.observation
  WHERE dataset_code = 'lfst_r_lfu3rt'
    AND time = '2023-01-01'
    AND dimensions @> '{"sex":"T","age":"Y15-74"}'
)
SELECT g.geo_code, gdp_per_cap, unemp_rate
FROM gdp g JOIN unemp u USING (geo_code)
WHERE LENGTH(g.geo_code) = 4 AND gdp_per_cap IS NOT NULL
ORDER BY unemp_rate DESC
LIMIT 20;
```

If that returns 20 rows in <100ms with sensible values (Greek/Spanish
regions at the high-unemployment end, North-Italian / German regions at
the low end), the system is wired correctly.

---

## Critical files (quick index)

| What | Where |
|---|---|
| Schema DDL | `edgar-gmr-etl/src/stats_etl/schema.sql` (new) |
| Generic loader | `edgar-gmr-etl/src/stats_etl/loader.py` (new) |
| Eurostat client | `edgar-gmr-etl/src/stats_etl/eurostat_source.py` (new) |
| CLI | `edgar-gmr-etl/src/stats_etl/cli.py` (new) |
| Helm chart for new PG | `edgar-gmr-etl/deployment-stats/` (new) |
| ArgoCD wiring | [gitops/appset.yaml](../../gitops/appset.yaml) (extend) |
| Vault secret + PV | [gitops/infra/prod.yaml](../../gitops/infra/prod.yaml) (extend, mirroring `gmr-postgres` block at lines 228–315) |
| Sync CronJobs | `edgar-gmr-etl/deployment-stats/templates/cronjob-*.yaml` (new) |
| DQ extension | [edgar-gmr-etl/src/api/routers/data_quality.py](../src/api/routers/data_quality.py) (extend) + [gmr-web/src/views/DataQualityHubView.vue](../../gmr-web/src/views/DataQualityHubView.vue) (extend) |
| NUTS hierarchy backfill | `edgar-gmr-etl/src/etl/sync_nuts_from_stats.py` (new) |

---

## Out of scope (mentioned, not designed)

- **Bivariate dashboard view in gmr-web** — separate work, consumes the
  read endpoint built in Phase D.
- **Non-Eurostat statistical sources** — the schema is generic; adding
  OECD or national stats institutes is "register a new dataset row + write
  one source adapter".
- **Copernicus / land-cover rasters** — PostGIS supports raster but raster
  storage is a different sizing/operational profile; defer until a real
  use case lands.
- **Full crime data at NUTS level** — would require harmonising 27 national
  stats institute feeds. Big project. Worth pursuing later as its own
  multi-quarter effort.
- **Continuous aggregates** for hot-path bivariate queries — add as needed
  once we see actual query patterns from the dashboard. Don't pre-optimise.
