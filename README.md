# fontem-api

FastAPI server **and** the scheduled ETL CronJobs for the Fontem platform.
Single repo, single Python codebase, single container image
(`contribute.void42.internal/fontem/fontem-api`). The Deployment runs
the API server; ~15 `etl-*` CronJobs in `fontem-prod` run the same
image with different `python -m src.etl.<loader>` entrypoints.

## What lives here

| Path | Role |
|---|---|
| `src/api/` | FastAPI routes, OpenAPI surface, dependency injection wiring |
| `src/etl/` | One module per upstream data source (FIRDS, TED, GLEIF, EDGAR, ESEF, sanctions, …) |
| `src/services/` | Cross-cutting services: currency, location, valuation |
| `src/data/` | Domain-specific query helpers (graph + relational) |
| `src/analysis/` | Read-side analytics queries powering Atlas / Public Spending / Data-Quality |
| `deployment/` | Helm chart used by Argo for every env |
| `infra/` | Out-of-band manifests (currency-data PV, …) applied alongside the chart |
| `scripts/` | One-off operational scripts (backfills, migrations) |
| `tests/` | pytest suite, integration tests against ephemeral Neo4j / Postgres |

## Why one image for both the API and the ETLs

The ETL loaders share a lot of code with the API (`gmr_id`, the consolidator
client, event-emit helpers, the resolver client). Single monorepo
+ single image keeps refactors cheap (one PR ships everywhere) at the
cost of image bloat (~620 MiB carrying pandas + pyarrow even for trivial
loaders). The trade-off has been accepted; splitting into separate images
is on the backlog only if cadence-coupling becomes a real pain point.

## Local dev

Standard Python venv + the workspace `make` targets:

```sh
make install        # editable install + fontem-events / fontem-event-schemas
make test           # pytest
make lint           # pylint src tests
```

ETL loaders take `--neo4j-uri`, `--neo4j-user`, `--neo4j-password`
flags (or read the same names as env vars). Most have a `--since` or
`--year` / `--month` cutoff for incremental runs.

## Deploy

CI auto-deploys to the `testing` env on every merge to `main`.
Promotion to `staging` / `prod` is **manual** — bump the version in
`gitops/<env>/fontem-api.yaml` to land it in a given environment. ETL
CronJob image versions come from the same `version:` value in the prod
overlay, so promoting the API promotes the ETLs in lockstep.

<!-- CI validation 2026-04-15T14:02:28 -->
