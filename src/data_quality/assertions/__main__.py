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


def _build_consistency_runner(client: Neo4jClient):
    # Cross-store engine needs Neo4j (multi-row sample) + Virtuoso (SPARQL).
    # Lazy imports keep this off the hot path; returns None when Virtuoso is
    # unconfigured, so the runner reports the consistency assertions as WARN
    # rather than failing.
    from src.data.sparql.virtuoso_client import VirtuosoClient  # pylint: disable=import-outside-toplevel
    from src.data_quality.assertions import consistency  # pylint: disable=import-outside-toplevel
    virtuoso = VirtuosoClient.from_env()
    if virtuoso is None:
        return None

    def _http_get(url: str, params: Mapping[str, str]):
        import httpx  # pylint: disable=import-outside-toplevel
        r = httpx.get(url, params=dict(params), timeout=120.0,
                      follow_redirects=True)
        r.raise_for_status()
        return r.json()

    def _run(entity_type: str) -> Mapping[str, Any]:
        if entity_type == "CellarMirror":
            return consistency.cellar_mirror_check(
                os.environ["VIRTUOSO_SPARQL_URL"], _http_get)
        if entity_type == "PetitionParity":
            return consistency.petition_parity_check(client, virtuoso)
        if entity_type == "LegislativeSpine":
            return consistency.legal_act_spine_check(client, virtuoso)
        if entity_type == "CellarFtIndex":
            return consistency.cellar_ft_index_check(virtuoso)
        return consistency.check(client, virtuoso, entity_type)
    return _run


def _select(families: str | None):
    if not families:
        return ASSERTIONS
    wanted = {f.strip() for f in families.split(",") if f.strip()}
    return [a for a in ASSERTIONS if a.family in wanted]


def _build_prices_runner():
    """Prices-engine runner: every query gets the same stats row,
    computed from the NFS price index + graph universe. Returns None
    (engine unwired → assertions WARN, not crash) when the price dir
    isn't mounted in this environment."""
    data_dir = os.environ.get("GMR_PRICE_DATA_DIR", "/edgar-data/prices")
    if not os.path.isdir(data_dir):
        return None
    # lazy import: keeps the graph-only path import-light
    from src.data_quality import price_index  # pylint: disable=import-outside-toplevel

    def _run(_query: str) -> Mapping[str, Any]:
        return price_index.get_price_stats(data_dir)
    return _run


def _build_consolidator_runner():
    """Consolidator-engine runner: each assertion's ``query`` is a JSON
    spec ``{"path": ..., "body": ...}``; POST it to the consolidator and
    flatten the /resolve response into the single row the evaluators
    read. Returns None (engine unwired -> BLOCK assertions surface as
    ERROR, per the no-runner path) when CONSOLIDATOR_URL is explicitly
    emptied — e.g. an environment without the service."""
    base_url = os.environ.get(
        "CONSOLIDATOR_URL", "http://fontem-consolidator:8000").rstrip("/")
    if not base_url:
        return None

    def _run(query: str) -> Mapping[str, Any]:
        import httpx  # pylint: disable=import-outside-toplevel  # lazy
        spec = json.loads(query)
        resp = httpx.post(base_url + spec["path"], json=spec["body"],
                          timeout=60.0)
        resp.raise_for_status()
        payload = resp.json()
        match = payload.get("match")
        candidates = payload.get("candidates") or []
        top = match or max(
            candidates, key=lambda c: c.get("confidence") or 0.0,
            default=None)
        return {
            "hint": payload.get("hint") or "",
            "match_found": 1 if match else 0,
            "n_candidates": len(candidates),
            "top_confidence": float((top or {}).get("confidence") or 0.0),
            "top_tier": (top or {}).get("tier") or "",
        }
    return _run


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(prog="dq-assert", description=__doc__)
    parser.add_argument(
        "--family",
        help="comma-separated families to run (keys,refs,values,pipeline,freshness)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    parser.add_argument("--persist", action="store_true",
                        help="write results to events.dq_result (the "
                             "assertion monitor's history)")
    parser.add_argument("--exit-zero", action="store_true",
                        help="always exit 0 (observability crons must not "
                             "look like failures whenever known debt fails "
                             "an assertion; the gate Job omits this)")
    args = parser.parse_args(argv)

    settings = load_settings()
    dsn = _normalise_dsn(settings.events_database_url)
    env_label = os.environ.get("DQ_ENV_LABEL", os.environ.get("NEO4J_URI", ""))

    client = Neo4jClient()
    try:
        cypher = _build_cypher_runner(client)
        sql = _build_sql_runner(dsn)
        consistency_runner = _build_consistency_runner(client)
        prices_runner = _build_prices_runner()
        results = run_catalog(cypher, sql, _select(args.family),
                              consistency_runner, prices_runner,
                              _build_consolidator_runner())
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

    if args.persist and dsn:
        from src.data_quality.assertions.persist import persist_results  # pylint: disable=import-outside-toplevel
        n = persist_results(dsn, results)
        print(f"persisted {n} results to events.dq_result")

    return 0 if args.exit_zero else exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
