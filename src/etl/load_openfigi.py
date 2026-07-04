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
# pylint: disable=too-many-lines  # cohesive single-mode loader; the
# concurrency path pushed it just over 1000 lines and splitting the
# ISIN/LEI runners into separate modules isn't worth the indirection.
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import httpx
from fontem_event_schemas import builders
from fontem_events import EventLog
from neo4j import GraphDatabase

from src.etl._http import with_headers

logger = logging.getLogger(__name__)

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
GLEIF_ISIN_URL = "https://api.gleif.org/api/v1/lei-records/{lei}/isins"
GLEIF_REQUEST_SLEEP = 0.05

# OpenFIGI enforces different per-request and per-window limits depending
# on whether the request carries an API key:
#                       per-request   per-minute   per-25-requests
#   keyless (anonymous):     10          25            -
#   with API key:           100         600           25 req/6 s
# Our keyless calls were sending 100 IDs and getting back HTTP 413 from
# every batch — see the failing staging run on 2026-05-26. We now pick
# the right pair at runtime based on whether the API key is set, so a
# fresh deploy without OPENFIGI_API_KEY still makes useful progress
# (just slower) instead of producing zero enriched listings.
API_BATCH_SIZE_KEYED = 100
API_BATCH_SIZE_ANON = 10
# Keyed: ~20 req / 6 s effective with 0.30 s sleep — well under the
# 25-req ceiling even with one retry. Anonymous: the ceiling is 25
# requests per minute, so each request needs ≥2.4 s of breathing
# room. Pick 3 s to leave headroom.
RATE_LIMIT_SLEEP_KEYED = 0.30
RATE_LIMIT_SLEEP_ANON = 3.0


def _api_limits(api_key: str | None) -> tuple[int, float]:
    """Return (batch_size, sleep_between_requests) for the current
    tier. Centralised so test asserts pin both pairs."""
    if api_key:
        return API_BATCH_SIZE_KEYED, RATE_LIMIT_SLEEP_KEYED
    return API_BATCH_SIZE_ANON, RATE_LIMIT_SLEEP_ANON


# Aggregate OpenFIGI request ceiling used when LEI resolution runs
# concurrently. The keyed tier allows 600 req/min; we pace to 500 to
# keep headroom, because query_openfigi does NOT retry 429s — it drops
# that LEI's enrichment — so overrunning the ceiling loses data rather
# than merely slowing down.
_CONCURRENT_RATE_PER_MIN = 500


class _RateLimiter:
    """Thread-safe global pacer for OpenFIGI calls under concurrency.

    Reserves evenly-spaced slots (one every ``60 / rate_per_min`` s) so
    the aggregate request rate across all worker threads stays under the
    keyed ceiling regardless of how many workers call it."""

    def __init__(self, rate_per_min: float):
        self._interval = 60.0 / rate_per_min
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            target = self._next if self._next > now else now
            self._next = target + self._interval
        delay = target - time.monotonic()
        if delay > 0:
            time.sleep(delay)

# Equity-ish market sectors. OpenFIGI returns ETFs, bonds, options,
# warrants, etc. against the same LEI; we only want shares so the
# Listing graph stays clean. "Equity" covers ordinary + preferred
# shares + depositary receipts; "Pref Equity" is preferred only on
# some venues. Anything else (Govt, Corp, MMkt, ...) is filtered out.
_EQUITY_SECTORS = {"Equity", "Pref Equity"}

# Within marketSector Equity, OpenFIGI's coarse ``securityType2`` cleanly
# separates operating-company equity from pooled-vehicle units (surveyed
# against prod cohorts 2026-07-04: FIRDS-era real listings are Common
# Stock / REIT / Depositary Receipt; the LEI-backfill noise was 100%
# securityType2 "Mutual Fund" — open/closed-end funds, ETPs, fund-of-
# funds all map there). Company classes go on the :Company listing
# path; fund classes are routed to the :InvestmentFund model; anything
# unrecognised is skipped AND counted so a new type shows up in the run
# summary instead of silently polluting the graph.
_COMPANY_SECURITY_TYPES2 = {
    "Common Stock", "Preferred Stock", "Depositary Receipt", "REIT",
    "Partnership Shares",
}
_FUND_SECURITY_TYPES2 = {"Mutual Fund"}


def _classify_instrument(inst) -> str | None:
    """``company`` / ``fund`` / None (= unknown, caller counts + skips)."""
    sec_type2 = (inst.get("securityType2") or "").strip()
    if sec_type2 in _COMPANY_SECURITY_TYPES2:
        return "company"
    if sec_type2 in _FUND_SECURITY_TYPES2:
        return "fund"
    return None


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
RETURN c.lei AS lei, c.gmr_id AS company_gmr_id,
       [] AS witness_isins
LIMIT $limit
"""


# ── LEI-REEVAL mode ────────────────────────────────────────────────
#
# Companies that already have at least one Listing whose ticker is not
# bound to an ISIN. The combination is suspicious — real exchange
# listings arrive with an ISIN attached (FIRDS keys by ISIN;
# OpenFIGI-ISIN-mode rewrites to a canonical ticker with the ISIN
# carried through). Tickers without an ISIN typically came from the
# pre-d9cb5b8 esef-data-fetcher fallback that synthesised a symbol
# from the company name (Mota-Engil SGPS S.A. → MOTA.LS) and never
# verified it against a real listing.
#
# This selector returns the suspect tickers alongside the LEI so the
# caller can, after the OpenFIGI lookup:
#   * emit UpsertListing for each canonical (ticker, exchCode, ISIN)
#     the way the regular LEI mode does;
#   * for any suspect ticker NOT in the canonical set, emit
#     UpsertListing(active=False) + AssertSameAs(suspect_iri,
#     canonical_iri) so the consolidator can retire the bad ticker and
#     redirect downstream lookups.
FETCH_LEIS_WITH_SUSPECT_LISTINGS = """
MATCH (c:Company)-[:LISTED_AS]->(suspect:Listing)
WHERE c.lei IS NOT NULL
  AND (suspect.isin IS NULL OR suspect.isin = '')
WITH c, collect(DISTINCT suspect.ticker) AS suspect_tickers
OPTIONAL MATCH (c)-[:LISTED_AS]->(witness:Listing)
WHERE witness.isin IS NOT NULL AND witness.isin <> ''
WITH c, suspect_tickers,
     collect(DISTINCT witness.isin) AS witness_isins
RETURN c.lei AS lei,
       c.gmr_id AS company_gmr_id,
       suspect_tickers,
       witness_isins
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
    """Get (lei, company_gmr_id, witness_isins) for Companies without
    any Listing. ``witness_isins`` is always empty for this selector —
    that's the whole point of the cohort (no Listing means no FIRDS-
    emitted sibling Listing with an ISIN to seed the lookup with)."""
    with driver.session() as session:
        result = session.run(FETCH_LEIS_NO_LISTING, limit=limit)
        return [
            {
                "lei": r["lei"],
                "company_gmr_id": r["company_gmr_id"],
                "witness_isins": list(r["witness_isins"]),
            }
            for r in result
        ]


def fetch_leis_with_suspect_listings(driver, limit):
    """Get (lei, company_gmr_id, suspect_tickers, witness_isins) for
    Companies whose Listings lack an ISIN.

    ``suspect_tickers`` is the list of ticker strings the caller will
    potentially retire after the OpenFIGI lookup confirms a canonical
    set. ``witness_isins`` is any ISIN attached to a sibling Listing
    on the same Company (typically from a FIRDS UpsertListing keyed
    by ISIN). Using those bypasses GLEIF entirely when present and
    avoids GLEIF's incomplete equity-ISIN coverage (e.g. Mota-Engil:
    GLEIF returns 15 bond ISINs and no equity, but FIRDS has the
    equity ISIN as a sibling Listing in our graph)."""
    with driver.session() as session:
        result = session.run(FETCH_LEIS_WITH_SUSPECT_LISTINGS, limit=limit)
        return [
            {
                "lei": r["lei"],
                "company_gmr_id": r["company_gmr_id"],
                "suspect_tickers": list(r["suspect_tickers"]),
                "witness_isins": list(r["witness_isins"]),
            }
            for r in result
        ]


def gleif_get_isins(lei: str, client=None) -> list[str]:
    """Resolve a LEI to its list of ISINs via GLEIF.

    Returns ``[]`` on 404 (LEI not registered) and on any transport
    error — silent failure so the caller can continue with the next
    LEI rather than aborting the whole run. ``client`` is optional;
    when None we use ``httpx.get`` directly so tests can monkeypatch
    just the GLEIF helper without touching OpenFIGI."""
    url = GLEIF_ISIN_URL.format(lei=lei)
    try:
        if client is not None:
            resp = client.get(url, timeout=20,
                              headers={"Accept": "application/json"})
        else:
            resp = httpx.get(url, timeout=20,
                             headers={"Accept": "application/json"})
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("GLEIF /isins failed for %s: %s", lei, exc)
        return []
    try:
        data = resp.json().get("data") or []
    except ValueError:
        logger.warning("GLEIF /isins non-JSON body for %s", lei)
        return []
    return [
        entry.get("attributes", {}).get("isin")
        for entry in data
        if entry.get("attributes", {}).get("isin")
    ]


def query_openfigi(payload, api_key=None):
    """POST a /mapping payload (list of {idType, idValue}). Returns
    the raw OpenFIGI response (list-of-result-objects, one per input).
    The caller decides how to map results back to the inputs and what
    to keep — ISIN mode wants one ticker per ISIN; LEI mode wants
    every equity instrument under the LEI."""
    extra = {"Content-Type": "application/json"}
    if api_key:
        extra["X-OPENFIGI-APIKEY"] = api_key
    headers = with_headers(extra)
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


def _retires_for_suspects(
    rows: list[dict], enriched: list[dict],
) -> list[dict]:
    """For each LEI-reeval row, diff its suspect_tickers against the
    canonical (ticker, exchange_code) set OpenFIGI returned.

    Returns one retire record per suspect ticker that's NOT in the
    canonical set, of shape:

      {"ticker": suspect, "company_gmr_id": ...,
       "replacement_ticker": canonical_or_None}

    The replacement is the canonical with the same exchange suffix
    when one matches; otherwise the only canonical (if exactly one);
    otherwise None — the bad Listing is deactivated but no AssertSameAs
    fires, so the consolidator won't blindly redirect to an unrelated
    venue."""
    by_lei_canon: dict[str, list[dict]] = {}
    for rec in enriched:
        by_lei_canon.setdefault(rec["lei"], []).append(rec)

    retires: list[dict] = []
    for row in rows:
        canon = by_lei_canon.get(row["lei"], [])
        if not canon:
            # We have no canonical to compare against — most often
            # because OpenFIGI / GLEIF returned nothing for this LEI
            # (private company, unregistered LEI, bond-only issuer
            # with no FIRDS witness). Keep the suspect Listing as-is:
            # silently retiring on absence-of-evidence would dewire
            # legitimate-but-unverifiable companies on every run.
            continue
        canon_tickers = {c["ticker"] for c in canon}
        for suspect in row["suspect_tickers"]:
            if suspect in canon_tickers:
                continue  # already canonical, leave alone
            replacement = _pick_replacement(suspect, canon)
            if replacement is None:
                continue
            retires.append({
                "ticker": suspect,
                "company_gmr_id": row["company_gmr_id"],
                "replacement_ticker": replacement,
            })
    return retires


# OpenFIGI exchCode -> Yahoo-style suffix. Ported from
# esef-data-fetcher/src/exchange_map.py so the retire matcher can
# treat "GSK on LN" and "GSK.L" as the same venue. Without this,
# every legitimate Yahoo-notation ticker for a Company OpenFIGI
# knows about gets retired with no AssertSameAs redirect (the
# suspect's ".L" suffix never matches the raw exchCode "LN").
_EXCH_ALIASES: dict[str, str] = {
    "GY": "DE", "GF": "DE",
    "SM": "MC", "SQ": "MC",
    "IM": "MI",
    "NA": "AS",
    "FP": "PA",
    "LN": "L",
    "NO": "OL",
    "SS": "ST",
    "DC": "CO",
    "FH": "HE",
    "ID": "IR",
    "PW": "WA",
    "PL": "LS",
}


def _canon_suffixes(c: dict) -> set[str]:
    """Yahoo-style suffixes a canonical listing can stand in for.
    Both the raw OpenFIGI ``exchCode`` and the aliased form."""
    raw = (c.get("exchange_code") or "").strip()
    return {s for s in (raw, _EXCH_ALIASES.get(raw, raw)) if s}


def _pick_replacement(suspect_ticker: str,
                      canon: list[dict]) -> str | None:
    """Pick the canonical ticker to AssertSameAs the suspect to.

    Matches, in order of confidence:

      1. Suspect's bare symbol == canonical's ticker. Covers the
         GSK.L / RIO.L case — Yahoo-style notation against
         OpenFIGI's bare ``GSK``/``RIO`` for the same Company.
         This is the most common shape: the legacy fabricator's
         "first word + country suffix" output where the first
         word happened to be the real ticker.
      2. Suspect's suffix matches a canonical's exchange code,
         directly or via ``_EXCH_ALIASES``. Pins the venue when
         the symbol differs.
      3. Exactly one canonical exists -> return it. Best guess
         for the one-Listing-per-Company case.
      4. Otherwise None — we don't invent a cross-venue redirect.
    """
    if not canon:
        return None
    bare = (suspect_ticker.rsplit(".", 1)[0]
            if "." in suspect_ticker else suspect_ticker)
    for c in canon:
        if c.get("ticker") == bare:
            return c["ticker"]
    suffix = (suspect_ticker.rsplit(".", 1)[-1]
              if "." in suspect_ticker else "")
    if suffix:
        for c in canon:
            if suffix in _canon_suffixes(c):
                return c["ticker"]
    if len(canon) == 1:
        return canon[0]["ticker"]
    return None


def emit_retire_events(log: EventLog, retires: list[dict]) -> int:
    """Emit one UpsertListing(active=False) and (when a replacement is
    known) one AssertSameAs per suspect ticker. The consolidator drops
    the LISTED_AS edge for active=False Listings and follows AssertSameAs
    to surface the canonical ticker in the API."""
    if not retires:
        return 0
    batch_id = uuid.uuid4()
    total = 0
    with log.batch(batch_id, producer="load_openfigi") as emit:
        for rec in retires:
            suspect_iri = f"http://data.fontem.eu/id/Listing/{rec['ticker']}"
            emit.upsert(
                "UpsertListing",
                iri=suspect_iri,
                domain="listing",
                payload=builders.upsert_listing(
                    ticker=rec["ticker"],
                    company_gmr_id=rec["company_gmr_id"],
                    active=False,
                ),
            )
            total += 1
            if rec["replacement_ticker"]:
                canon_iri = (
                    f"http://data.fontem.eu/id/Listing/"
                    f"{rec['replacement_ticker']}"
                )
                emit.upsert(
                    "AssertSameAs",
                    iri=suspect_iri,
                    domain="listing",
                    payload=builders.assert_same_as(
                        a_iri=suspect_iri,
                        b_iri=canon_iri,
                        confidence=0.9,
                        method="openfigi_lei_reeval",
                    ),
                )
                total += 1
    return total


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
        "log_label": "LEI",
        "log_phrase": "Companies with LEI but no Listing",
        "progress_phrase": "listings discovered so far",
        # Mark this mode as routed through the LEI→ISIN→OpenFIGI
        # path. OpenFIGI's /mapping does NOT accept ID_LEI; the
        # ISIN-mediated path is the only working option. See memory:
        # openfigi-no-id-lei.
        "via_lei": True,
    },
    "lei-reeval": {
        "fetch": fetch_leis_with_suspect_listings,
        "log_label": "LEI-REEVAL",
        "log_phrase": "Companies with suspect (ISIN-less) Listings",
        "progress_phrase": "canonicals discovered so far",
        "via_lei": True,
        # Marker for the runner to invoke the retire-suspect step
        # after the canonical UpsertListings have been emitted.
        "retire_suspects": True,
    },
}


def _equity_canonicals_from_response(
    response, isins: list[str], lei: str, company_gmr_id: str,
    unknown_types: dict | None = None,
) -> list[dict]:
    """Reshape an OpenFIGI ID_ISIN batch response into per-instrument
    canonical Listing records. Inputs are positional so we walk
    ``zip(response, isins)`` to attach the original ISIN to each
    instrument. De-dupes on ``(ticker, exchange_code)`` so one issuer
    listed under several FIGIs on the same venue is one record.

    Each record carries ``entity_class`` from ``_classify_instrument``:
    ``company`` (operating-company equity) or ``fund`` (pooled-vehicle
    units). Unrecognised securityType2s are skipped and tallied into
    ``unknown_types`` (when given) so new types surface in the run
    summary instead of silently entering the graph."""
    canonicals: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for entry, isin in zip(response, isins):
        for inst in entry.get("data") or []:
            if (inst.get("marketSector") or "") not in _EQUITY_SECTORS:
                continue
            entity_class = _classify_instrument(inst)
            if entity_class is None:
                if unknown_types is not None:
                    label = (inst.get("securityType2")
                             or inst.get("securityType") or "?")
                    unknown_types[label] = unknown_types.get(label, 0) + 1
                continue
            ticker = (inst.get("ticker") or "").strip().upper()
            exch = (inst.get("exchCode") or "").strip()
            if not ticker:
                continue
            key = (ticker, exch)
            if key in seen:
                continue
            seen.add(key)
            canonicals.append({
                "lei": lei,
                "ticker": ticker,
                "exchange_code": exch,
                "mic": (inst.get("micCode") or "").strip() or None,
                "figi": (inst.get("figi") or "").strip(),
                "isin": isin,
                "company_gmr_id": company_gmr_id,
                "entity_class": entity_class,
                "security_type": (inst.get("securityType") or "").strip(),
            })
    return canonicals


def _resolve_lei_to_canonicals(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    row: dict, batch_size: int, api_key: str | None,
    bulk_isins: dict[str, list[str]] | None = None,
    sleep_between_batches: float = 0.0,
    rate_limiter: "_RateLimiter | None" = None,
) -> tuple[list[dict], str]:
    """Resolve one LEI to its canonical equity Listings.

    Witness ISINs (from a FIRDS-emitted sibling Listing on the same
    Company) take priority over GLEIF — they're more reliable for EU
    equities since GLEIF's ``/isins`` endpoint sometimes returns only
    bond ISINs for an issuer (Mota-Engil being the canonical example).
    Returns ``(records, source_label, unknown_type_counts)``; the label
    is used by the runner for the progress log, the counts feed the
    run-summary "skipped unknown securityType2" line.

    ``bulk_isins`` is the LEI→ISINs dict prebuilt from GLEIF's daily
    bulk file (see ``_gleif_isin_bulk.load_isin_mapping``). When
    provided, it short-circuits the per-LEI REST call to GLEIF —
    looking up locally is O(1) and pays no rate-limit budget. When
    ``None`` the function falls back to the REST endpoint so callers
    that don't want to download 30 MB of CSV up-front (one-off
    diagnostic runs, narrow CLI invocations) still work.

    ``sleep_between_batches`` paces multiple OpenFIGI calls for the
    same LEI when the ISIN list exceeds the per-request batch size.
    A mass-issuer LEI (Mota-Engil has 48 ISINs → 5 anonymous-tier
    batches) would otherwise fire 5 POSTs in <1 s and trip the
    25 req/min anonymous limit, losing the whole LEI's enrichment to
    429s. The outer LEI loop already paces between LEIs; this paces
    inside one LEI when it spans multiple batches.
    """
    isins = list(row.get("witness_isins") or [])
    source = "witness"
    if not isins:
        if bulk_isins is not None:
            isins = list(bulk_isins.get(row["lei"], []))
            source = "gleif_bulk"
        else:
            isins = gleif_get_isins(row["lei"])
            source = "gleif"
            time.sleep(GLEIF_REQUEST_SLEEP)
    if not isins:
        return [], "none", {}

    canonicals: list[dict] = []
    unknown: dict[str, int] = {}
    # OpenFIGI batch limit varies by tier — split here so a LEI with
    # many ISINs (15+ for Mota) still respects the per-request cap.
    # ``sleep_between_batches`` keeps the per-batch cadence inside
    # this loop, not just between LEIs in the outer loop — without
    # it a multi-batch issuer fires N POSTs in <1 s and 429s the
    # whole sequence.
    for i in range(0, len(isins), batch_size):
        if rate_limiter is not None:
            rate_limiter.wait()
        elif i > 0 and sleep_between_batches > 0:
            time.sleep(sleep_between_batches)
        chunk = isins[i:i + batch_size]
        payload = [{"idType": "ID_ISIN", "idValue": v} for v in chunk]
        response = query_openfigi(payload, api_key)
        if not response:
            continue
        canonicals.extend(_equity_canonicals_from_response(
            response, chunk, row["lei"], row["company_gmr_id"],
            unknown_types=unknown,
        ))
    return canonicals, source, unknown


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


def _run_mode_via_lei(  # pylint: disable=too-many-locals,too-many-branches,too-many-arguments,too-many-positional-arguments,too-many-statements
    mode: str, driver, log: EventLog, limit: int, api_key: str | None,
    bulk_isins_enabled: bool = True,
    bulk_isins: dict[str, list[str]] | None = None,
    concurrency: int = 1,
) -> dict:
    """Runner for LEI-routed modes (``lei`` + ``lei-reeval``).

    Differs from the ISIN-direct runner: each row is one LEI which
    expands to one-or-many ISINs (witness or GLEIF), and each ISIN is
    queried via OpenFIGI ID_ISIN. Per-LEI rate-pacing is the same
    sleep used by the ISIN runner so the keyed-vs-anonymous budgets
    are honoured.

    **Per-LEI commit boundary** (was: batched-at-end). Each LEI's
    canonical Listings — and, for ``lei-reeval``, the retire events for
    that LEI's stale suspect tickers — are emitted in their own
    ``log.batch(...)`` as soon as ``_resolve_lei_to_canonicals``
    returns, BEFORE the next LEI is queried. The old shape accumulated
    everything in memory and emitted at the very end of the LEI loop,
    which on a 10 000-row bulk run held nine hours of work in one
    Postgres transaction — invisible to the sinks until the loop
    finished, and lost entirely on a pod restart. ``_retires_for_suspects``
    already operates per-row (groups canonicals by LEI internally), so
    calling it with one row + that row's canonicals returns the same
    retires it would have for that row in the batched shape.
    """
    # pylint: disable=import-outside-toplevel
    from . import _gleif_isin_bulk

    cfg = _MODES[mode]
    rows = cfg["fetch"](driver, limit)
    logger.info("%s mode: %d %s",
                cfg["log_label"], len(rows), cfg["log_phrase"])
    if not rows:
        return {"queried": 0, "enriched": 0, "errors": 0, "emitted": 0}

    batch_size, sleep_s = _api_limits(api_key)
    logger.info("OpenFIGI tier: %s (batch=%d, sleep=%.2fs)",
                "keyed" if api_key else "anonymous",
                batch_size, sleep_s)

    # Build the GLEIF ISIN→LEI mapping from the daily bulk file
    # (~30 MB zip → 285 MB CSV → ~96 k LEIs total) so each LEI's ISIN
    # lookup is an O(1) local dict get instead of an HTTP GET. The
    # high-level ``load_openfigi`` entry point hoists this build up so
    # ``mode=both`` only pays for one stream of the file across the
    # lei + lei-reeval passes; when a caller invokes ``_run_mode_via_lei``
    # directly (test fixtures, ad-hoc scripts) with ``bulk_isins=None``
    # we build it inline here.
    #
    # Witness ISINs (from a sibling FIRDS-emitted Listing) keep their
    # precedence — we only consult the bulk mapping when a row has no
    # witness. So the bulk download only matters when at least one row
    # lacks witness ISINs.
    if bulk_isins is None and bulk_isins_enabled and any(
        not r.get("witness_isins") for r in rows
    ):
        target_leis = {
            r["lei"] for r in rows if not r.get("witness_isins")
        }
        bulk_isins = _gleif_isin_bulk.load_isin_mapping(target_leis)

    st = {"witness": 0, "gleif": 0, "none": 0,
          "enriched": 0, "emitted": 0, "retired": 0, "processed": 0,
          "funds": 0, "unknown": {}}

    def _consume(row, records, source, unknown):
        """Update counters + emit for one resolved LEI. Runs only on the
        calling (main) thread, so the EventLog stays single-writer even
        when resolution is parallelised. Fund-class records are counted
        but NOT emitted as Company listings — they belong to the
        :InvestmentFund model (see UpsertInvestmentFund)."""
        if source == "witness":
            st["witness"] += 1
        elif source in ("gleif", "gleif_bulk"):
            st["gleif"] += 1
        else:
            st["none"] += 1
        for k, v in unknown.items():
            st["unknown"][k] = st["unknown"].get(k, 0) + v
        canonicals = [r for r in records
                      if r.get("entity_class", "company") == "company"]
        st["funds"] += len(records) - len(canonicals)
        st["enriched"] += len(canonicals)
        if canonicals:
            st["emitted"] += emit_listing_events(log, canonicals)
        if cfg.get("retire_suspects"):
            # Per-row retire computation. Identical result to the batched
            # call as long as the row is the source-of-truth for its own
            # suspect_tickers, which it is. Fund-class records are kept
            # out so a fund unit can never become a retire replacement.
            retires = _retires_for_suspects([row], canonicals)
            if retires:
                st["retired"] += emit_retire_events(log, retires)
        st["processed"] += 1
        if st["processed"] % 100 == 0:
            logger.info(
                "  %s: %d / %d processed, %d %s "
                "(witness=%d gleif=%d none=%d funds=%d)",
                cfg["log_label"], st["processed"], len(rows),
                st["enriched"], cfg["progress_phrase"],
                st["witness"], st["gleif"], st["none"], st["funds"],
            )

    if concurrency > 1 and api_key:
        # Parallelise the OpenFIGI round-trips (I/O-bound) across a
        # bounded pool; a shared limiter keeps the aggregate under the
        # keyed ceiling. Emission stays on this thread via _consume, so
        # the EventLog is never written concurrently. Completion order
        # (not row order) drives emission — fine, each LEI's Listings are
        # independent and idempotent.
        limiter = _RateLimiter(_CONCURRENT_RATE_PER_MIN)

        def _resolve(row):
            return row, _resolve_lei_to_canonicals(
                row, batch_size, api_key, bulk_isins=bulk_isins,
                sleep_between_batches=0.0, rate_limiter=limiter,
            )

        rows_it = iter(rows)
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            inflight = set()
            for _ in range(concurrency * 2):
                nxt = next(rows_it, None)
                if nxt is None:
                    break
                inflight.add(pool.submit(_resolve, nxt))
            while inflight:
                done, pending = wait(inflight, return_when=FIRST_COMPLETED)
                inflight = set(pending)
                for fut in done:
                    row, (records, source, unknown) = fut.result()
                    _consume(row, records, source, unknown)
                    nxt = next(rows_it, None)
                    if nxt is not None:
                        inflight.add(pool.submit(_resolve, nxt))
    else:
        for row in rows:
            records, source, unknown = _resolve_lei_to_canonicals(
                row, batch_size, api_key, bulk_isins=bulk_isins,
                sleep_between_batches=sleep_s,
            )
            _consume(row, records, source, unknown)
            # Only pace when we actually called OpenFIGI. With the
            # bulk-file path most rows resolve to "no ISINs" and would
            # otherwise burn a sleep each for zero rate-limit benefit
            # (the sleep paces OpenFIGI; we didn't call it).
            if source != "none":
                time.sleep(sleep_s)

    if cfg.get("retire_suspects"):
        logger.info("  %s: retired %d suspect tickers",
                    cfg["log_label"], st["retired"])
    logger.info("  %s: source mix witness=%d gleif=%d none=%d",
                cfg["log_label"], st["witness"], st["gleif"], st["none"])
    if st["funds"]:
        logger.info(
            "  %s: %d fund-class instruments (securityType2 Mutual "
            "Fund) held for the :InvestmentFund path",
            cfg["log_label"], st["funds"])
    if st["unknown"]:
        logger.info("  %s: skipped unknown securityType2s: %s",
                    cfg["log_label"], st["unknown"])
    return {
        "queried": len(rows), "enriched": st["enriched"],
        "emitted": st["emitted"] + st["retired"], "errors": 0,
        "funds": st["funds"],
    }


def _run_mode(  # pylint: disable=too-many-locals,too-many-arguments,too-many-positional-arguments
    mode, driver, log, limit, api_key,
    bulk_isins_enabled: bool = True,
    bulk_isins: dict[str, list[str]] | None = None,
    concurrency: int = 1,
):
    """Run one OpenFIGI mode end-to-end. Mode-specific wiring lives in
    ``_MODES`` so this body can stay generic.

    **Per-batch commit boundary** (was: batched-at-end). Each OpenFIGI
    request's enriched rows are emitted in their own ``log.batch(...)``
    as soon as the response is back, BEFORE the next batch is queried.
    The old shape held everything in memory until the loop finished;
    on a multi-hour run that hid all enriched rows from sinks until
    the end and lost them entirely on a pod restart.

    The ``retire_suspects`` branch is intentionally absent here: it
    only applies to ``lei-reeval`` mode, which dispatches to
    ``_run_mode_via_lei`` above. Keeping it out of this function
    avoids carrying dead code that would also need the per-batch
    treatment if anything ever flipped its routing."""
    cfg = _MODES[mode]
    if cfg.get("via_lei"):
        return _run_mode_via_lei(
            mode, driver, log, limit, api_key,
            bulk_isins_enabled=bulk_isins_enabled,
            bulk_isins=bulk_isins,
            concurrency=concurrency,
        )
    rows = cfg["fetch"](driver, limit)
    logger.info("%s mode: %d %s",
                cfg["log_label"], len(rows), cfg["log_phrase"])
    if not rows:
        return {"queried": 0, "enriched": 0, "errors": 0, "emitted": 0}

    batch_size, sleep_s = _api_limits(api_key)
    logger.info("OpenFIGI tier: %s (batch=%d, sleep=%.2fs)",
                "keyed" if api_key else "anonymous", batch_size, sleep_s)

    id_to_company = {r[cfg["id_field"]]: r["company_gmr_id"] for r in rows}
    ids = list(id_to_company.keys())

    errors = 0
    enriched_total = 0
    emitted_total = 0
    for i in range(0, len(ids), batch_size):
        batch = ids[i:i + batch_size]
        results = _process_batch(cfg, batch, id_to_company, api_key)
        if results is None:
            errors += 1
        else:
            enriched_total += len(results)
            if results:
                emitted_total += emit_listing_events(log, results)
            if (i + batch_size) % 1000 < batch_size:
                logger.info(
                    "  %s: %d / %d queried, %d %s",
                    cfg["log_label"],
                    min(i + batch_size, len(ids)), len(ids),
                    enriched_total, cfg["progress_phrase"],
                )
        time.sleep(sleep_s)

    return {
        "queried": len(ids), "enriched": enriched_total,
        "emitted": emitted_total, "errors": errors,
    }


def load_openfigi(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    driver, log: EventLog, *, mode: str, limit: int,
    api_key: str | None, bulk_isins_enabled: bool = True,
    concurrency: int = 1,
) -> dict:
    """Run the requested mode(s) end to end. Returns a per-mode
    summary dict suitable for logging + the Kuma push-line.

    When ``bulk_isins_enabled`` is True (default) and any LEI-routed
    mode is going to run, we stream GLEIF's daily bulk file ONCE for
    the union of LEI cohorts across ``lei`` + ``lei-reeval`` and pass
    the resulting dict down to each ``_run_mode`` call. Before this
    hoist, ``mode=both`` triggered two independent streams of the
    285 MB CSV — pure waste because the file is identical across
    those passes.
    """
    # pylint: disable=import-outside-toplevel
    from . import _gleif_isin_bulk

    summary: dict[str, dict] = {}
    bulk_isins: dict[str, list[str]] | None = None
    if bulk_isins_enabled and mode in ("lei", "lei-reeval", "both"):
        union_leis: set[str] = set()
        for lei_mode in ("lei", "lei-reeval"):
            if mode in (lei_mode, "both"):
                rows = _MODES[lei_mode]["fetch"](driver, limit)
                # Only LEIs without a witness ISIN need GLEIF — witness
                # rows short-circuit before consulting the bulk mapping.
                union_leis.update(
                    r["lei"] for r in rows if not r.get("witness_isins")
                )
        if union_leis:
            bulk_isins = _gleif_isin_bulk.load_isin_mapping(union_leis)

    t0 = time.time()
    if mode in ("isin", "both"):
        summary["isin"] = _run_mode(
            "isin", driver, log, limit, api_key,
            bulk_isins_enabled=bulk_isins_enabled,
            bulk_isins=bulk_isins,
            concurrency=concurrency,
        )
    if mode in ("lei", "both"):
        summary["lei"] = _run_mode(
            "lei", driver, log, limit, api_key,
            bulk_isins_enabled=bulk_isins_enabled,
            bulk_isins=bulk_isins,
            concurrency=concurrency,
        )
    if mode in ("lei-reeval", "both"):
        summary["lei-reeval"] = _run_mode(
            "lei-reeval", driver, log, limit, api_key,
            bulk_isins_enabled=bulk_isins_enabled,
            bulk_isins=bulk_isins,
            concurrency=concurrency,
        )
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
        "--mode",
        choices=("isin", "lei", "lei-reeval", "both"),
        default="both",
        help=("Which enrichment path(s) to run (default: both — runs "
              "isin + lei + lei-reeval)"),
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
        "--concurrency", type=int,
        default=int(os.environ.get("OPENFIGI_CONCURRENCY", "1") or "1"),
        help=("Parallel OpenFIGI requests for LEI-routed modes "
              "(default: 1 = serial). Only applied with an API key; a "
              "shared limiter keeps the aggregate under the keyed "
              "600 req/min ceiling. OPENFIGI_CONCURRENCY sets the "
              "env default."),
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
    # ``--bulk-isins`` / ``--no-bulk-isins`` paired via BooleanOptionalAction
    # so an operator can override the env in either direction from the CLI.
    # Default is "bulk on" unless the env var explicitly disables it.
    parser.add_argument(
        "--bulk-isins",
        action=argparse.BooleanOptionalAction,
        dest="bulk_isins_enabled",
        default=os.environ.get(
            "OPENFIGI_NO_BULK_ISINS", "",
        ).lower() not in ("1", "true", "yes"),
        help=(
            "Download GLEIF's daily ISIN-to-LEI bulk file once (default), "
            "or pass --no-bulk-isins to fall back to the per-LEI REST "
            "endpoint (rate-limited, only for diagnostics or when the "
            "bulk endpoint is unreachable). "
            "OPENFIGI_NO_BULK_ISINS=1 flips the env default to off."
        ),
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
            bulk_isins_enabled=args.bulk_isins_enabled,
            concurrency=args.concurrency,
        )
    except httpx.HTTPError:
        logger.exception("Fatal HTTP error during OpenFIGI enrichment")
        sys.exit(1)
    finally:
        driver.close()
        log.close()


if __name__ == "__main__":
    main()
