# Atlas API

Read surface for the Fontem Atlas frontend (`gmr-web/src/views/AtlasView.vue`).
Lives inside the main `gmr-api` image today; designed to be extracted to
its own service when traffic or feature scope justifies it.

## What's served

| Path | Description |
|---|---|
| `GET /atlas/health` | Probes the fontem_stats Postgres connection and reports per-source status. |
| `GET /atlas/datasets` | Catalog: every enabled dataset + last successful sync. |
| `GET /atlas/datasets/{code}` | One dataset's full metadata + freshness + observed time range. |
| `GET /atlas/series` | Time-series rows for one dataset — filter by `geo[]`, `nuts_level`, `start`, `end`, `dimensions`. |
| `GET /atlas/snapshot` | One value per geo for a single (dataset, year, NUTS level) — the choropleth-shaped query. |

## Module layout

```
atlas_api/
  app.py           — FastAPI factory: build_app() + build_router()
  config.py        — env-var settings (STATS_DATABASE_URL, CORS, etc.)
  schemas.py       — Pydantic request/response models
  sources/
    fontem_stats.py  — reads from the fontem_stats Postgres store
  routers/
    health.py
    datasets.py
    series.py
    snapshot.py
  __main__.py      — `python -m src.atlas_api` for standalone runs
```

The only out-of-module import this directory makes is
`src.stats_etl.db` (for the StatsDatabase connection helper). When
extracting the service, copy or vendor that one file — see the
checklist below.

## Adding a new source

The Atlas grows by overlaying data from places other than the
fontem_stats Postgres (Neo4j procurement, reports DB, third-party APIs).
Each one becomes a new module under `sources/` with a small, explicit
contract:

```python
class Source(Protocol):
    def health(self) -> dict[str, Any]: ...
    # plus whatever read methods the routers need
```

Wire it into `app.py`'s factory next to the existing `FontemStatsSource`,
expose its routes from a router under `routers/`, and update
`/atlas/health` to include it. Keep one source per file so the
boundaries are obvious.

## Running standalone

```bash
export STATS_DATABASE_URL="postgresql://fontem_stats:...@fontem-stats-postgres:5432/fontem_stats"
python -m src.atlas_api  # uvicorn on :8001 with all /atlas/* routes
```

The standalone server exposes the same routes at the root (no
`/atlas` prefix), so a future deployment fronted by an Ingress/Service
of its own can keep client URLs stable while routing differently.

## Extraction checklist (when you're ready to break it out)

1. Copy `src/atlas_api/` and the few methods it needs from
   `src/stats_etl/db.py` into a new repo (or a sibling package).
2. Carry forward `requirements.txt` lines: `fastapi`, `uvicorn`,
   `psycopg[binary]>=3.1`, `dishka`, `pydantic`. No Neo4j, no SEC
   EDGAR, no analytics deps.
3. Write a `Dockerfile` that runs `python -m atlas_api` and exposes
   `:8001`.
4. Add a `gmr-web` API client switch from `/api/atlas/*` to
   `https://atlas.<env>.void42.internal/*` (or keep the legacy path
   working via nginx alias).
5. Drop `app.include_router(atlas_router)` from `src/api/app.py` and
   delete this directory.
