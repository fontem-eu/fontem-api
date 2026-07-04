"""Export the graph's listed entities as a Yahoo-Finance ticker universe.

The price layer is file-driven: ``usa-stock-price-fetcher`` downloads
EOD OHLCV for every symbol in its ticker sources and the API serves
them straight off NFS. Historically those sources were a static SEC
list plus ESEF filers — the graph's own listings (OpenFIGI + FIRDS)
were invisible to it. This exporter closes the loop: one primary
Yahoo-format symbol per listed entity (Company or InvestmentFund),
written in the same ``{key: {"ticker": ...}}`` shape as
``eu_entities.json`` so the fetcher consumes it verbatim.

Symbol mapping is deliberately conservative and self-surfacing:

* venue MIC first (ISO 10383 → Yahoo suffix; the FIRDS-era listings
  carry MIC but no exchange code),
* then the OpenFIGI/Bloomberg exchange code for the codes we're sure
  about (the OpenFIGI-era listings carry exchange but rarely MIC),
* entities whose venues all stay unmapped are SKIPPED and the code is
  counted in the run summary — unknown venues must show up in the
  logs, not turn into silently-wrong Yahoo symbols.

Ordering matters: the fetcher starts NEW tickers in file order, so
entities that have won public contracts come first — price history for
procurement-relevant issuers lands before the long tail.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter

from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

# ── venue → Yahoo suffix ──────────────────────────────────────────
#
# MIC (ISO 10383) → Yahoo Finance suffix. Only venues whose Yahoo
# suffix is well established; extend from the run summary's unmapped
# counts, never by guessing.
MIC_TO_YAHOO: dict[str, str] = {
    # pan-EU main markets
    "XPAR": "PA", "XLON": "L", "XAMS": "AS", "XBRU": "BR", "XLIS": "LS",
    "XMAD": "MC", "XMIL": "MI", "MTAA": "MI", "XETR": "DE", "XSWX": "SW",
    "XVTX": "SW", "XSTO": "ST", "XCSE": "CO", "XHEL": "HE", "XOSL": "OL",
    "XICE": "IC", "XWAR": "WA", "XPRA": "PR", "XBUD": "BD", "XVIE": "VI",
    "XATH": "AT", "XDUB": "IR", "XLUX": "LU", "XBUC": "RO", "XZAG": "ZSE",
    # German regional venues (Yahoo has distinct suffixes for each)
    "STUB": "SG", "STUH": "SG", "XSTU": "SG",   # Stuttgart
    "MUND": "MU", "XMUN": "MU",                  # Munich
    "FRAB": "F", "FRAV": "F", "XFRA": "F",       # Frankfurt floor
    "HAND": "HA", "XHAN": "HA",                  # Hanover
    "HAMN": "HM", "XHAM": "HM",                  # Hamburg
    "DUSD": "DU", "XDUS": "DU",                  # Düsseldorf
    "XBER": "BE", "BERB": "BE", "BERA": "BE",    # Berlin
    # North America / rest of world
    "XNYS": "", "XNAS": "", "ARCX": "", "BATS": "", "XASE": "",
    "XTSE": "TO", "XTSX": "V", "XASX": "AX", "XTKS": "T", "XHKG": "HK",
}

# OpenFIGI/Bloomberg exchange code → Yahoo suffix. Sourced from
# esef-data-fetcher's exchange_map plus the unambiguous classics.
# "" means a bare US symbol (share-class dots become dashes downstream).
EXCH_TO_YAHOO: dict[str, str] = {
    "US": "", "UN": "", "UW": "", "UA": "", "UP": "",
    "LN": "L", "FP": "PA", "GY": "DE", "GF": "F", "IM": "MI",
    "NA": "AS", "SM": "MC", "SQ": "MC", "BB": "BR", "PL": "LS",
    "NO": "OL", "SS": "ST", "DC": "CO", "FH": "HE", "ID": "IR",
    "PW": "WA", "AV": "VI", "SW": "SW", "SE": "SW", "CN": "TO",
    "CT": "TO", "AU": "AX", "JT": "T", "JP": "T", "HK": "HK",
    "GA": "AT", "PX": "PR", "HB": "BD", "RO": "RO", "LX": "LU",
}

FETCH_LISTED_ENTITIES = """
MATCH (e)-[:LISTED_AS]->(l:Listing)
WHERE (e:Company OR e:InvestmentFund) AND l.active <> false
WITH e, collect({ticker: l.ticker, exchange: l.exchange, mic: l.mic}) AS ls
RETURN e.gmr_id AS gmr_id, e.name AS name, e.country AS country,
       (e:InvestmentFund) AS is_fund,
       EXISTS { (e)<-[:AWARDED_TO]-() } AS has_contracts,
       ls AS listings
"""


def _suffix_for(listing: dict) -> str | None:
    """Yahoo suffix for one listing, or None when the venue is unknown.
    Empty string means a bare (US) symbol."""
    mic = (listing.get("mic") or "").strip().upper()
    if mic in MIC_TO_YAHOO:
        return MIC_TO_YAHOO[mic]
    exch = (listing.get("exchange") or "").strip().upper()
    if exch in EXCH_TO_YAHOO:
        return EXCH_TO_YAHOO[exch]
    return None


def _yahoo_symbol(ticker: str, suffix: str) -> str:
    """Ticker + suffix in Yahoo format. Bare US symbols convert
    share-class dots to dashes (BRK.B → BRK-B)."""
    if suffix:
        return f"{ticker}.{suffix}"
    return ticker.replace(".", "-")


def pick_symbol(listings: list[dict], unmapped: Counter) -> str | None:
    """One primary Yahoo symbol per entity: the first venue we can map,
    preferring main markets over German regional tape. Unmappable
    venues are tallied so new codes surface in the run summary."""
    mapped: list[tuple[str, str]] = []
    for listing in listings:
        ticker = (listing.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        suffix = _suffix_for(listing)
        if suffix is None:
            key = ((listing.get("mic") or "").strip().upper()
                   or (listing.get("exchange") or "").strip().upper()
                   or "?")
            unmapped[key] += 1
            continue
        mapped.append((ticker, suffix))
    if not mapped:
        return None
    # Prefer non-regional venues (regional German tape lists everything;
    # the home listing usually appears alongside it).
    regional = {"SG", "MU", "F", "HA", "HM", "DU", "BE"}
    primary = [m for m in mapped if m[1] not in regional]
    ticker, suffix = (primary or mapped)[0]
    return _yahoo_symbol(ticker, suffix)


def export_universe(driver, output_path: str) -> dict:
    """Query listed entities, pick one symbol each, write the JSON.
    Returns the summary dict (also logged)."""
    with driver.session() as session:
        rows = [dict(r) for r in session.run(FETCH_LISTED_ENTITIES)]
    # Contract winners first — the fetcher starts NEW tickers in file
    # order, and procurement-relevant issuers matter most here.
    rows.sort(key=lambda r: (not r["has_contracts"], r["gmr_id"]))

    unmapped: Counter = Counter()
    out: dict[str, dict] = {}
    seen_symbols: set[str] = set()
    skipped_entities = 0
    funds = 0
    for row in rows:
        symbol = pick_symbol(row["listings"], unmapped)
        if symbol is None:
            skipped_entities += 1
            continue
        if symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        out[row["gmr_id"]] = {"ticker": symbol, "name": row["name"]}
        if row["is_fund"]:
            funds += 1

    tmp = f"{output_path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=0)
    os.replace(tmp, output_path)

    summary = {
        "entities": len(rows), "symbols": len(out), "funds": funds,
        "skipped_no_mappable_venue": skipped_entities,
        "unmapped_venues": dict(unmapped.most_common(20)),
    }
    logger.info("price universe: %d entities → %d symbols (%d funds), "
                "%d skipped (no mappable venue)",
                summary["entities"], summary["symbols"], funds,
                skipped_entities)
    if unmapped:
        logger.info("unmapped venue codes (extend the maps from these, "
                    "top 20): %s", summary["unmapped_venues"])
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Export graph listings as a Yahoo ticker universe")
    parser.add_argument(
        "--output",
        default=os.environ.get("PRICE_UNIVERSE_PATH",
                               "/edgar-data/prices/universe_graph.json"),
    )
    parser.add_argument(
        "--neo4j-uri", default=os.environ.get("NEO4J_URI",
                                              "bolt://neo4j:7687"))
    parser.add_argument(
        "--neo4j-user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument(
        "--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", ""))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password))
    try:
        export_universe(driver, args.output)
    finally:
        driver.close()


if __name__ == "__main__":
    sys.exit(main())
