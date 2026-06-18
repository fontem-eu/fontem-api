"""On-demand data-quality assertion runner.

Runs the assertion catalog against the environment this process is
pointed at (NEO4J_* + EVENTS_DATABASE_URL — the same env the ETL jobs
read), prints a grouped report, and exits non-zero if any block-tier
assertion fails. Intended to run as a Kubernetes Job, on demand, after
an ETL change is deployed to a shared store:

    python -m src.data_quality.assertions            # all families
    python -m src.data_quality.assertions --family keys,refs,values
    python -m src.data_quality.assertions --json     # machine-readable

Apply the Job in the staging namespace to check the shared store, or in
the prod namespace to check prod — each picks up that namespace's
connection secrets.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from typing import Any

from src.atlas_api.config import load as load_settings
from src.data.graph.neo4j_client import Neo4jClient
from src.data_quality.assertions.catalog import ASSERTIONS
from src.data_quality.assertions.runner import (
    format_report,
    run_catalog,
    exit_code,
    summarise,
)


def _normalise_dsn(dsn: str | None) -> str | None:
    # Mirror EtlRunsSource: psycopg (sync) doesn't speak the asyncpg
    # dialect, and an unsubstituted $(VAR) means the env list is mis-ordered.
    if not dsn:
        return None
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
    if "$(" in dsn:
        return None
    return dsn


def _build_cypher_runner(client: Neo4jClient):
    def _run(query: str) -> Mapping[str, Any]:
        with client.session() as session:
            rec = session.run(query).single()
            return dict(rec) if rec else {}
    return _run


def _build_sql_runner(dsn: str | None):
    import psycopg  # pylint: disable=import-outside-toplevel  # lazy: SQL-only

    def _run(query: str) -> Mapping[str, Any]:
        if not dsn:
            raise RuntimeError("EVENTS_DATABASE_URL not set")
        with psycopg.connect(dsn, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(query)
            row = cur.fetchone()
            if row is None:
                return {}
            cols = [d.name for d in cur.description]
            return dict(zip(cols, row))
    return _run


def _select(families: str | None):
    if not families:
        return ASSERTIONS
    wanted = {f.strip() for f in families.split(",") if f.strip()}
    return [a for a in ASSERTIONS if a.family in wanted]


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(prog="dq-assert", description=__doc__)
    parser.add_argument(
        "--family",
        help="comma-separated families to run (keys,refs,values,pipeline,freshness)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    args = parser.parse_args(argv)

    settings = load_settings()
    dsn = _normalise_dsn(settings.events_database_url)
    env_label = os.environ.get("DQ_ENV_LABEL", os.environ.get("NEO4J_URI", ""))

    client = Neo4jClient()
    try:
        cypher = _build_cypher_runner(client)
        sql = _build_sql_runner(dsn)
        results = run_catalog(cypher, sql, _select(args.family))
    finally:
        client.close()

    if args.json:
        payload = {
            "env": env_label,
            "summary": summarise(results),
            "results": [r.__dict__ for r in results],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(format_report(results, env_label))

    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
