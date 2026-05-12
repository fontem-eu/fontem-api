"""
OpenFIGI ticker enrichment → event log
================================================
Two complementary modes share the same OpenFIGI ``/mapping``
endpoint and the same emit path; only the upstream selector
and the OpenFIGI ``idType`` differ.

* **ISIN mode** (the original, pre-2026-05 behaviour). Picks up
  Listings that already exist with an ISIN but no canonical
  ticker — typically the ISIN-keyed placeholders that FIRDS
  emits. OpenFIGI rewrites them to the canonical ticker.

* **LEI mode** (added 2026-05). Picks up Companies that have an
  LEI but *no* Listing edge at all, queries OpenFIGI by
  ``ID_LEI``, and emits one ``UpsertListing`` per equity
  instrument OpenFIGI returns for the entity. This is what
  closes the ~3.3 M-LEI-but-no-Listing gap in the graph —
  GLEIF/EDGAR/ESEF populate plenty of LEIs but the prior
  pipeline only minted a Listing when an upstream record
  *already* carried a ticker.

The Listing identity key is the ticker, so an LEI lookup that
yields multiple share classes / venues produces multiple
``UpsertListing`` events, one per unique ``(ticker, exchCode)``
pair. The Neo4j sink MERGEs on ticker; the Virtuoso sink
INSERTs one IRI per ticker.

Source for both modes is still Neo4j (a derived read store).
The event log remains canonical for writes.

Usage::

    python -m src.etl.load_openfigi               # default: --mode both
    python -m src.etl.load_openfigi --mode lei    # LEI discovery only
    python -m src.etl.load_openfigi --mode isin   # legacy FIRDS enrichment

"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid

import httpx
from gmr_event_schemas import builders
from gmr_events import EventLog
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
API_BATCH_SIZE = 100  # OpenFIGI max per request (with API key)
# Rate limit: 25 requests per 6 seconds (with API key)
RATE_LIMIT_SLEEP = 0.25  # seconds between requests (conservative)

# Equity-ish market sectors. OpenFIGI returns ETFs, bonds, options,
# warrants, etc. against the same LEI; we only want shares so the
# Listing graph stays clean. "Equity" covers ordinary + preferred
# shares + depositary receipts; "Pref Equity" is preferred only on
# some venues. Anything else (Govt, Corp, MMkt, ...) is filtered out.
_EQUITY_SECTORS = {"Equity", "Pref Equity"}


# ── ISIN mode ─────────────────────────────────────────────────────
#
# Pull the parent Company so the UpsertListing events carry
# company_gmr_id (the schema requires it). LISTED_AS is the
# Company → Listing edge maintained by the sinks.
FETCH_ISINS = """
MATCH (c:Company)-[:LISTED_AS]->(l:Listing)
WHERE l.isin IS NOT NULL AND (l.ticker IS NULL OR l.ticker = '')
RETURN l.isin AS isin, c.gmr_id AS company_gmr_id
LIMIT $limit
"""


# ── LEI mode ──────────────────────────────────────────────────────
#
# Companies with an LEI but no Listing yet. GLEIF + EDGAR + ESEF
# populate plenty of LEIs; the original FIRDS+OpenFIGI flow only
# materialised a Listing when an upstream record already shipped a
# ticker, leaving ~3.3 M LEIs unmaterialised. This selector finds
# them; OpenFIGI answers with the venues the entity is actually
# listed on (or nothing, for private companies — by far the common
# case).
FETCH_LEIS_NO_LISTING = """
MATCH (c:Company)
WHERE c.lei IS NOT NULL
  AND NOT EXISTS { (c)-[:LISTED_AS]->(:Listing) }
RETURN c.lei AS lei, c.gmr_id AS company_gmr_id
LIMIT $limit
"""


def fetch_isins(driver, limit):
    """Get (isin, company_gmr_id) pairs for Listings without a ticker."""
    with driver.session() as session:
        result = session.run(FETCH_ISINS, limit=limit)
        return [
            {"isin": r["isin"], "company_gmr_id": r["company_gmr_id"]}
            for r in result
        ]


def fetch_leis_no_listing(driver, limit):
    """Get (lei, company_gmr_id) pairs for Companies without any Listing."""
    with driver.session() as session:
        result = session.run(FETCH_LEIS_NO_LISTING, limit=limit)
        return [
            {"lei": r["lei"], "company_gmr_id": r["company_gmr_id"]}
            for r in result
        ]


def query_openfigi(payload, api_key=None):
    """POST a /mapping payload (list of {idType, idValue}). Returns
    the raw OpenFIGI response (list-of-result-objects, one per input).
    The caller decides how to map results back to the inputs and what
    to keep — ISIN mode wants one ticker per ISIN; LEI mode wants
    every equity instrument under the LEI."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-OPENFIGI-APIKEY"] = api_key
    try:
        resp = httpx.post(
            OPENFIGI_URL, json=payload, headers=headers, timeout=30,
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.exception("OpenFIGI API request failed")
        return []
    return resp.json()


def _isin_results(response, isins):
    """Pick the best ticker for each input ISIN. Mirrors the prior
    behaviour — first equity match wins."""
    results = []
    for i, entry in enumerate(response):
        if "data" not in entry or not entry["data"]:
            continue
        best = entry["data"][0]
        ticker = (best.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        results.append({
            "isin": isins[i],
            "ticker": ticker,
            "exchange_code": (best.get("exchCode") or "").strip(),
            "mic": (best.get("micCode") or "").strip() or None,
            "figi": (best.get("figi") or "").strip(),
        })
    return results


def _lei_results(response, leis):
    """For each LEI, return every (ticker, exchCode) the entity is
    listed under. Filters non-equity instruments (bonds, options,
    warrants, ...). De-dupes on (ticker, exchCode) so a re-run is
    idempotent at the Listing-key level."""
    results = []
    for i, entry in enumerate(response):
        if "data" not in entry or not entry["data"]:
            continue
        seen = set()
        for item in entry["data"]:
            if (item.get("marketSector") or "") not in _EQUITY_SECTORS:
                continue
            ticker = (item.get("ticker") or "").strip().upper()
            exch = (item.get("exchCode") or "").strip()
            if not ticker:
                continue
            key = (ticker, exch)
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "lei": leis[i],
                "ticker": ticker,
                "exchange_code": exch,
                "mic": (item.get("micCode") or "").strip() or None,
                "figi": (item.get("figi") or "").strip(),
            })
    return results


def emit_listing_events(log: EventLog, enriched: list[dict]) -> int:
    """Emit one UpsertListing event per enriched record. Each record
    must carry ``ticker`` and ``company_gmr_id``; ``isin``/``mic``/
    ``exchange_code`` are optional and pass through to the Listing."""
    if not enriched:
        return 0
    batch_id = uuid.uuid4()
    total = 0
    with log.batch(batch_id, producer="load_openfigi") as emit:
        for rec in enriched:
            emit.upsert(
                "UpsertListing",
                iri=f"http://data.fontem.eu/id/Listing/{rec['ticker']}",
                domain="listing",
                payload=builders.upsert_listing(
                    ticker=rec["ticker"],
                    company_gmr_id=rec["company_gmr_id"],
                    exchange=rec.get("exchange_code") or None,
                    isin=rec.get("isin"),
                    mic=rec.get("mic"),
                    active=True,
                ),
            )
            total += 1
    return total


# Per-mode wiring. Keeps _run_mode generic by table-driving the bits
# that actually differ between ISIN and LEI lookups: the upstream
# selector, the OpenFIGI idType, the response→ticker shaper, and the
# label used in progress logs.
_MODES = {
    "isin": {
        "fetch": fetch_isins,
        "id_field": "isin",
        "id_type": "ID_ISIN",
        "results": _isin_results,
        "log_label": "ISIN",
        "log_phrase": "Listings with ISIN but no ticker",
        "progress_phrase": "enriched so far",
    },
    "lei": {
        "fetch": fetch_leis_no_listing,
        "id_field": "lei",
        "id_type": "ID_LEI",
        "results": _lei_results,
        "log_label": "LEI",
        "log_phrase": "Companies with LEI but no Listing",
        "progress_phrase": "listings discovered so far",
    },
}


def _process_batch(cfg, batch, id_to_company, api_key):
    """Query OpenFIGI for one batch of IDs, return the enriched rows
    (with ``company_gmr_id`` filled in) or ``None`` on transport error."""
    payload = [{"idType": cfg["id_type"], "idValue": v} for v in batch]
    response = query_openfigi(payload, api_key)
    if not response and batch:
        return None
    results = cfg["results"](response, batch)
    for r in results:
        r["company_gmr_id"] = id_to_company[r[cfg["id_field"]]]
    return results


def _run_mode(mode, driver, log, limit, api_key):
    """Run one OpenFIGI mode end-to-end. Mode-specific wiring lives in
    ``_MODES`` so this body can stay generic."""
    cfg = _MODES[mode]
    rows = cfg["fetch"](driver, limit)
    logger.info("%s mode: %d %s",
                cfg["log_label"], len(rows), cfg["log_phrase"])
    if not rows:
        return {"queried": 0, "enriched": 0, "errors": 0, "emitted": 0}

    id_to_company = {r[cfg["id_field"]]: r["company_gmr_id"] for r in rows}
    ids = list(id_to_company.keys())

    all_enriched: list[dict] = []
    errors = 0
    for i in range(0, len(ids), API_BATCH_SIZE):
        batch = ids[i:i + API_BATCH_SIZE]
        results = _process_batch(cfg, batch, id_to_company, api_key)
        if results is None:
            errors += 1
        else:
            all_enriched.extend(results)
            if (i + API_BATCH_SIZE) % 1000 < API_BATCH_SIZE:
                logger.info(
                    "  %s: %d / %d queried, %d %s",
                    cfg["log_label"],
                    min(i + API_BATCH_SIZE, len(ids)), len(ids),
                    len(all_enriched), cfg["progress_phrase"],
                )
        time.sleep(RATE_LIMIT_SLEEP)

    emitted = emit_listing_events(log, all_enriched)
    return {
        "queried": len(ids), "enriched": len(all_enriched),
        "emitted": emitted, "errors": errors,
    }


def load_openfigi(driver, log: EventLog, *, mode: str, limit: int,
                  api_key: str | None) -> dict:
    """Run the requested mode(s) end to end. Returns a per-mode
    summary dict suitable for logging + the Kuma push-line."""
    summary: dict[str, dict] = {}
    t0 = time.time()
    if mode in ("isin", "both"):
        summary["isin"] = _run_mode("isin", driver, log, limit, api_key)
    if mode in ("lei", "both"):
        summary["lei"] = _run_mode("lei", driver, log, limit, api_key)
    elapsed = time.time() - t0
    for m, s in summary.items():
        logger.info(
            "OpenFIGI[%s]: %d queried, %d enriched, %d emitted, %d errors",
            m, s["queried"], s["enriched"], s["emitted"], s["errors"],
        )
    # Single-line "Done:" summary that the Kuma push picks up. Keep
    # it parseable: <mode>=<queried>q/<emitted>e per mode, comma
    # separated, ending with the elapsed seconds.
    pieces = [f"{m}={s['queried']}q/{s['emitted']}e" for m, s in summary.items()]
    logger.info("Done: %s in %.1fs", ",".join(pieces), elapsed)
    return summary


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Enrich Listing nodes with OpenFIGI ticker data",
    )
    parser.add_argument(
        "--mode", choices=("isin", "lei", "both"), default="both",
        help="Which enrichment path(s) to run (default: both)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENFIGI_API_KEY", ""),
        help="OpenFIGI API key (or OPENFIGI_API_KEY env var)",
    )
    parser.add_argument(
        "--limit", type=int, default=10000,
        help="Max IDs to process per mode (default: 10000)",
    )
    parser.add_argument(
        "--neo4j-uri",
        default=os.environ.get("NEO4J_URI", "bolt://neo4j:7687"),
    )
    parser.add_argument(
        "--neo4j-user",
        default=os.environ.get("NEO4J_USER", "neo4j"),
    )
    parser.add_argument(
        "--neo4j-password",
        default=os.environ.get("NEO4J_PASSWORD", ""),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password),
    )
    log = EventLog.from_env()

    try:
        load_openfigi(
            driver, log, mode=args.mode, limit=args.limit,
            api_key=args.api_key or None,
        )
    except httpx.HTTPError:
        logger.exception("Fatal HTTP error during OpenFIGI enrichment")
        sys.exit(1)
    finally:
        driver.close()
        log.close()


if __name__ == "__main__":
    main()
