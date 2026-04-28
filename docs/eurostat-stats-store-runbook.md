# fontem-stats — operations runbook

Companion to [eurostat-stats-store.md](./eurostat-stats-store.md). That
doc is the design / why; this one is the *how do I operate it* once it's
deployed.

---

## Provisioning a fresh environment

These are the ordered steps to bring up `fontem-stats-postgres` in a new
environment (staging or prod).

### 1. NFS export (host side)

On the storage host (whatever box owns `/srv/nfs`):

```sh
mkdir -p /srv/nfs/fontem-stats-prod   # or fontem-stats-staging
# Add to /etc/exports if not already covered by a wildcard:
echo '/srv/nfs/fontem-stats-prod 10.0.0.0/8(rw,sync,no_subtree_check,no_root_squash)' >> /etc/exports
exportfs -ra
```

No `chown` needed: the StatefulSet sets `securityContext.fsGroup: 999`,
which makes kubelet recursively chown the mounted volume to the
postgres group on first start. This is the only side of the chart
where pods need write access; consumers (gmr-api, the sync CronJobs)
read via the database connection, never the filesystem.

The matching `PersistentVolume` resource lives in
[gitops/infra/prod.yaml](../../gitops/infra/prod.yaml) (and one per env).

### 2. Vault secrets

Two paths per environment:

```sh
# Postgres credentials — only POSTGRES_PASSWORD is consumed by the chart.
vault kv put secret/gmr/fontem-stats-postgres \
    POSTGRES_PASSWORD="$(openssl rand -base64 24)"

# Kuma push URLs — paste the full URL emitted by Kuma when you create
# the monitor (see "Kuma monitors" section below).
vault kv put secret/gmr/fontem-stats-kuma \
    kuma_push_url_daily="https://kuma.void42.internal/api/push/<token>?ping=" \
    kuma_push_url_weekly="https://kuma.void42.internal/api/push/<token>?ping="
```

The matching `VaultStaticSecret` resources live in `gitops/infra/<env>.yaml`.

### 3. Argo sync

Once `gitops/appset.yaml` carries the new `fontem-stats` Application
entry and `gitops/<env>/fontem-stats.yaml` has a `version:` set, Argo
syncs the chart automatically. Check:

```sh
kubectl -n argocd get application fontem-stats
kubectl -n gmr get statefulset fontem-stats-postgres
kubectl -n gmr get pod -l app=fontem-stats-postgres
```

The init scripts in the ConfigMap run on first boot and create the
schema, extensions, hypertable, and indexes. Subsequent restarts skip
init (Postgres image's documented behaviour).

### 4. First daily-sync run bootstraps the catalog automatically

The daily CronJob runs `register-seed` and `nuts-polygons` as the
first two steps of every run before the actual sync (both are
idempotent — `ON CONFLICT` on the catalog rows, MERGE-style upsert
on the polygons). Fresh deploys are fully usable after the next
03:00 UTC run with no manual exec; you can trigger one immediately
with:

```sh
kubectl -n gmr create job --from=cronjob/fontem-stats-sync-daily \
    fontem-stats-sync-bootstrap
kubectl -n gmr wait --for=condition=complete \
    job/fontem-stats-sync-bootstrap --timeout=30m
kubectl -n gmr logs job/fontem-stats-sync-bootstrap --tail=200
```

If you'd rather run the bootstrap steps individually (e.g. to debug
one in isolation), the same commands still work via:

```sh
kubectl -n gmr exec -it deploy/gmr-api -- python -m src.stats_etl register-seed
kubectl -n gmr exec -it deploy/gmr-api -- python -m src.stats_etl nuts-polygons --version 2024
```

### 5. Backfill Neo4j NUTS hierarchy

After step 5, populate the NUTS-1/2/3 nodes in the graph:

```sh
kubectl -n gmr exec -it deploy/gmr-api -- \
    python -m src.etl.sync_nuts_from_stats
```

Closes the long-standing `CLAUDE.md` data-quality gap ("only level 0 (39
countries), need levels 1-3"). Existing
`(:Company)-[:LOCATED_IN]->(:NUTSRegion)` edges keep working; this script
adds the lower-level nodes plus `[:PART_OF]` chains between them.

---

## Kuma monitors

We push heartbeats from the two sync CronJobs. Set up the monitors *in
Kuma* by hand (via UI), then paste their push URLs into Vault per the
section above.

### Monitor 1 — `fontem-stats-sync-daily`

| Field | Value |
|---|---|
| Type | Push |
| Name | `fontem-stats sync (daily)` |
| Heartbeat interval | 86400 s (24 h) |
| Heartbeat retry interval | 600 s (10 min) — tolerate slow runs |
| Resend notification every | 3 down |
| Tags | `fontem-stats`, `cronjob`, `daily` |
| Notification channel | same one the smoke-cron uses |

After Kuma issues the push URL, store it in Vault as
`kuma_push_url_daily` under `gmr/fontem-stats-kuma`.

### Monitor 2 — `fontem-stats-sync-weekly`

| Field | Value |
|---|---|
| Type | Push |
| Name | `fontem-stats sync (weekly)` |
| Heartbeat interval | 604800 s (7 d) |
| Heartbeat retry interval | 3600 s |
| Resend notification every | 1 down |
| Tags | `fontem-stats`, `cronjob`, `weekly` |

Push URL → Vault `kuma_push_url_weekly`.

### Monitor 3 — Postgres availability (optional but recommended)

| Field | Value |
|---|---|
| Type | TCP |
| Hostname | `fontem-stats-postgres.gmr.svc.cluster.local` |
| Port | 5432 |
| Heartbeat | 60 s |

This catches "pod is up but Postgres isn't accepting connections" —
useful during the rare WAL-replay-on-restart case where the pod is
`Ready` but conn refused for a few seconds.

### Monitor 4 — `/stats/datasets` HTTP (optional)

| Field | Value |
|---|---|
| Type | HTTP(s) |
| URL | `https://gmr.void42.net/api/stats/datasets` |
| Method | GET |
| Expected status | 200 |
| Body keyword | `nuts_levels` |
| Heartbeat | 5 min |

Catches "DB up, app can't talk to it" — different failure mode than the
push monitors.

---

## Common operations

### Manually re-trigger a sync

Spawn an ad-hoc Job from the CronJob template:

```sh
kubectl -n gmr create job --from=cronjob/fontem-stats-sync-daily \
    fontem-stats-sync-manual-$(date +%s)
```

For one specific dataset:

```sh
kubectl -n gmr exec -it deploy/gmr-api -- \
    python -m src.stats_etl sync demo_r_pjangrp3 --force
```

### Watch a sync run live

```sh
JOB=fontem-stats-sync-daily-29412345   # last cron's pod
kubectl -n gmr logs -f job/$JOB
```

### Check recent run history

```sh
kubectl -n gmr exec -it sts/fontem-stats-postgres -- \
    psql -U fontem_stats -d fontem_stats -c "
SELECT dataset_code,
       started_at,
       status,
       rows_total,
       error_message
FROM fontem_stats.sync_run
ORDER BY started_at DESC LIMIT 30;"
```

The same data is exposed at `GET /api/data-quality/eurostat` for the
DQ dashboard.

### Disable a flaky dataset

If a single Eurostat dataset starts failing repeatedly without an
upstream fix:

```sh
kubectl -n gmr exec -it sts/fontem-stats-postgres -- \
    psql -U fontem_stats -d fontem_stats -c "
UPDATE fontem_stats.dataset SET enabled = false
WHERE code = 'demo_r_mwk3_t';"
```

The cron sync will skip it on the next run; sync-runs already in flight
finish normally. Re-enable when you've fixed it.

### Disk usage check

The hypertable is compressed after 1 year; recent chunks are full-size.
Quick health check:

```sh
psql -c "SELECT
    hypertable_name,
    pg_size_pretty(hypertable_size('fontem_stats.observation')) AS total,
    (SELECT count(*) FROM timescaledb_information.chunks
     WHERE hypertable_name = 'observation') AS chunks
FROM timescaledb_information.hypertables;"
```

Target: < 200 MB for the full curated 26-dataset set after compression.
If it's bigger, check that the compression policy is actually running
(`SELECT * FROM timescaledb_information.jobs;` should list one for the
observation table).

---

## Failure modes + responses

| Symptom | Diagnostic | Fix |
|---|---|---|
| Daily Kuma monitor goes red | `kubectl get jobs -n gmr -l app=fontem-stats-sync` | Inspect the latest failed pod's logs; usually upstream 5xx — wait one cycle. |
| `sync_run.error_message` shows `psycopg.errors.UndefinedColumn` | Schema drifted | Apply migrations; current version of init SQL is in the chart's ConfigMap. |
| Pod stuck in `CrashLoopBackOff` after a chart bump | `kubectl logs -p` on the pod | Usually a TimescaleDB-HA image-tag bump that broke the data dir; pin back to the previous tag in `gitops/<env>/fontem-stats.yaml`. |
| `/stats/datasets` returns 503 | `STATS_DATABASE_URL` env var missing on `gmr-api` | Add the env var to the gmr-api Deployment in [edgar-gmr-etl/deployment/templates/deployment.yaml](../deployment/templates/deployment.yaml) — it's intentionally optional so dev/dast can run without the stats store. |
| All datasets report 0 rows but `success` | `iter_observations` parser drift on a Eurostat header change | Capture the raw TSV (`curl -O <source_url>`) and run the parser locally; usually a new flag or column. |

---

## When to add a new dataset

```sql
INSERT INTO fontem_stats.dataset (
    code, label, theme, source, source_url, nuts_levels,
    dim_ids, dim_sizes, time_unit, update_freq, enabled
)
VALUES (
    'crim_off_pop',
    'Police-recorded offences per 100k pop',
    'social', 'eurostat',
    'https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/CRIM_OFF_POP/?format=TSV&compressed=true',
    ARRAY[0]::smallint[],   -- country only; Eurostat doesn't publish at NUTS for crime
    ARRAY['unit', 'iccs']::text[],
    ARRAY[1, 25]::int[],
    'year', '1 year', true
);
```

Then either let the daily cron pick it up, or trigger it immediately:

```sh
python -m src.stats_etl sync crim_off_pop
```

No code change required. That's the whole point.
