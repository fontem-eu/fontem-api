"""One-shot backfill: trigger translation+embedding enrichment for every
:Authority in Neo4j that's missing multilingual data.

Integration shape
-----------------
The consolidator's TranslationEnrichmentAuthority rule handles the actual
Mistral calls + Neo4j writes. This script just enumerates the authority_ids
that still need work and posts them, in batches, to /consolidate/batch. Idempotent:
re-running only re-processes nodes the rule's `applies()` still gates True on.

Usage
-----
    python -m src.etl.backfill_authority_multilingual [--batch 25] [--limit 0]

Environment
-----------
    NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD   — graph access
    CONSOLIDATOR_URL                           — consolidator base URL
    BACKFILL_TIMEOUT                           — per-batch HTTP timeout (s)
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from typing import Iterable

import httpx
from neo4j import GraphDatabase


log = logging.getLogger(__name__)


# Query: any :Authority whose name_<any-EU-lang> is missing OR has no embedding.
# Return only the id — the rule filters in applies() anyway, we just narrow the
# set so we don't round-trip every single authority on a re-run.
_AUTHORITIES_NEEDING_BACKFILL = """
MATCH (a:Authority)
WHERE a.name IS NOT NULL
  AND (
    a.name_embedding IS NULL
    OR a.name_bg IS NULL OR a.name_cs IS NULL OR a.name_da IS NULL
    OR a.name_de IS NULL OR a.name_el IS NULL OR a.name_en IS NULL
    OR a.name_es IS NULL OR a.name_et IS NULL OR a.name_fi IS NULL
    OR a.name_fr IS NULL OR a.name_ga IS NULL OR a.name_hr IS NULL
    OR a.name_hu IS NULL OR a.name_it IS NULL OR a.name_lt IS NULL
    OR a.name_lv IS NULL OR a.name_mt IS NULL OR a.name_nl IS NULL
    OR a.name_pl IS NULL OR a.name_pt IS NULL OR a.name_ro IS NULL
    OR a.name_sk IS NULL OR a.name_sl IS NULL OR a.name_sv IS NULL
  )
RETURN a.authority_id AS authority_id
ORDER BY coalesce(a.multilingual_updated_at, datetime('1970-01-01')) ASC
"""


def _chunks(xs: list[str], n: int) -> Iterable[list[str]]:
    for i in range(0, len(xs), n):
        yield xs[i : i + n]


def _driver():
    uri = os.environ["NEO4J_URI"]
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ["NEO4J_PASSWORD"]
    return GraphDatabase.driver(uri, auth=(user, password))


def _consolidator_url() -> str:
    return os.environ.get(
        "CONSOLIDATOR_URL",
        "http://gmr-consolidator.gmr.svc.cluster.local:8000",
    )


def _enrich_batch(client: httpx.Client, base_url: str, ids: list[str]) -> dict:
    r = client.post(
        f"{base_url}/consolidate/batch",
        json={
            "entity_type": "Authority",
            "ids": ids,
            "triggered_by": "backfill_multilingual",
        },
    )
    r.raise_for_status()
    return r.json()


def run(batch: int, limit: int) -> dict:
    driver = _driver()
    url = _consolidator_url()
    timeout = float(os.environ.get("BACKFILL_TIMEOUT", "600"))

    with driver.session() as session:
        t0 = time.time()
        ids: list[str] = [r["authority_id"] for r in session.run(_AUTHORITIES_NEEDING_BACKFILL)]
        log.info("backfill: %d authorities need multilingual data (query %.1fs)",
                 len(ids), time.time() - t0)
        if limit:
            ids = ids[:limit]

    driver.close()

    processed = 0
    merged = linked = flagged = conflicts = 0
    with httpx.Client(timeout=timeout) as client:
        for chunk in _chunks(ids, batch):
            t1 = time.time()
            try:
                res = _enrich_batch(client, url, chunk)
            except httpx.HTTPError as exc:
                log.warning("backfill: batch of %d failed: %s", len(chunk), exc)
                continue
            processed += res.get("processed", 0)
            merged += res.get("merged", 0)
            linked += res.get("linked", 0)
            flagged += res.get("flagged", 0)
            conflicts += res.get("conflicts", 0)
            log.info(
                "backfill: +%d (total %d/%d) in %.1fs",
                res.get("processed", 0), processed, len(ids), time.time() - t1,
            )

    summary = {
        "processed": processed, "total_pending": len(ids),
        "merged": merged, "linked": linked,
        "flagged": flagged, "conflicts": conflicts,
    }
    log.info("backfill: done %s", summary)
    return summary


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Multilingual backfill for :Authority nodes.")
    parser.add_argument("--batch", type=int, default=25, help="authority ids per /consolidate/batch call")
    parser.add_argument("--limit", type=int, default=0, help="cap total authorities processed (0 = no cap)")
    args = parser.parse_args()
    run(batch=args.batch, limit=args.limit)


if __name__ == "__main__":
    main()
