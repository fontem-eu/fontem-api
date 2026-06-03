"""Probe Yahoo Finance for each Listing ticker and report which ones
resolve to a real instrument.

Yahoo's quote endpoint returns a populated record for any tradeable
ticker and an empty result for nonsense ones. That makes it a cheap
correctness check after the pre-d9cb5b8 name-fabricated tickers
(MOTA.LS, AIRBUS.PA, ...) got minted into our graph.

Modes:

  * ``--ticker EGL.LS`` (repeatable) — verify a specific shortlist.
  * ``--from-neo4j --where-no-isin`` — verify every Listing in Neo4j
    whose ``isin`` is null. That's the legacy-fabricated cohort we
    want to repair via load_openfigi --mode lei-reeval.
  * (default) — verify every Listing in Neo4j. Bounded by ``--limit``.

Use the consumer pod so neo4j + outbound HTTPS work::

    kubectl -n fontem-prod cp scripts/verify_listings_yahoo.py \\
        $POD:/tmp/v.py
    kubectl -n fontem-prod exec $POD -- bash -c \\
        "PYTHONPATH=/app python /tmp/v.py --from-neo4j --where-no-isin --limit 50"
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter

import httpx

logger = logging.getLogger(__name__)

YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

BATCH_SIZE = 50


def fetch_listings_no_isin(driver, limit: int) -> list[dict]:
    query = (
        "MATCH (l:Listing) "
        "WHERE l.isin IS NULL OR l.isin = '' "
        "OPTIONAL MATCH (c:Company)-[:LISTED_AS]->(l) "
        "RETURN l.ticker AS ticker, c.name AS company, "
        "       c.lei AS lei "
        "LIMIT $limit"
    )
    with driver.session() as s:
        return [
            {"ticker": r["ticker"], "company": r["company"],
             "lei": r["lei"], "isin": None}
            for r in s.run(query, limit=limit)
        ]


def fetch_all_listings(driver, limit: int) -> list[dict]:
    query = (
        "MATCH (l:Listing) "
        "OPTIONAL MATCH (c:Company)-[:LISTED_AS]->(l) "
        "RETURN l.ticker AS ticker, c.name AS company, "
        "       c.lei AS lei, l.isin AS isin "
        "LIMIT $limit"
    )
    with driver.session() as s:
        return [
            {"ticker": r["ticker"], "company": r["company"],
             "lei": r["lei"], "isin": r["isin"]}
            for r in s.run(query, limit=limit)
        ]


def probe_yahoo(client: httpx.Client,
                tickers: list[str]) -> dict[str, dict]:
    """Look up each ticker. Returns ``{ticker: quote_dict}`` for
    resolvable ones; missing tickers are absent from the result.
    Yahoo returns 200 with an empty quoteResponse.result when
    nothing matches, so we inspect the body, not the status code."""
    found: dict[str, dict] = {}
    for i in range(0, len(tickers), BATCH_SIZE):
        chunk = tickers[i:i + BATCH_SIZE]
        try:
            resp = client.get(
                YAHOO_QUOTE_URL,
                params={"symbols": ",".join(chunk)},
                headers=HEADERS,
                timeout=30,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("yahoo batch %d-%d failed: %s",
                           i, i + len(chunk), exc)
            continue
        data = resp.json().get("quoteResponse", {}).get("result", [])
        for entry in data:
            sym = entry.get("symbol")
            if sym:
                found[sym] = entry
    return found


def _rows_from_args(args, ap) -> list[dict]:
    if args.ticker:
        return [{"ticker": t, "company": None, "lei": None,
                 "isin": None}
                for t in args.ticker]
    if not args.from_neo4j:
        ap.error("specify either --ticker or --from-neo4j")
    from neo4j import GraphDatabase  # pylint: disable=import-outside-toplevel
    driver = GraphDatabase.driver(
        args.neo4j_uri,
        auth=(args.neo4j_user, args.neo4j_password),
    )
    try:
        if args.where_no_isin:
            return fetch_listings_no_isin(driver, args.limit)
        return fetch_all_listings(driver, args.limit)
    finally:
        driver.close()


def _print_table(rows: list[dict], found: dict[str, dict]) -> int:
    print()
    print(f"  {'ticker':>16s}  {'resolves':>8s}  "
          f"{'price':>10s}  {'name':<32s}")
    print(f"  {'-'*16}  {'-'*8}  {'-'*10}  {'-'*32}")
    resolved = 0
    for row in rows:
        ticker = row["ticker"] or ""
        entry = found.get(ticker)
        if entry:
            resolved += 1
            price = entry.get("regularMarketPrice")
            name = (entry.get("longName")
                    or entry.get("shortName") or "")[:32]
            price_s = str(price) if price is not None else ""
            print(f"  {ticker:>16s}  {'YES':>8s}  "
                  f"{price_s:>10s}  {name:<32s}")
        else:
            label = (row.get("company") or "")[:32]
            print(f"  {ticker:>16s}  {'no':>8s}  "
                  f"{'':>10s}  {label:<32s}")
    return resolved


def _print_summary(rows: list[dict], found: dict[str, dict],
                   resolved: int) -> None:
    total = max(len(rows), 1)
    print()
    print(f"resolved on Yahoo: {resolved}/{len(rows)} "
          f"({100 * resolved / total:.0f}%)")
    by_suffix: Counter[str] = Counter()
    miss_by_suffix: Counter[str] = Counter()
    for row in rows:
        ticker = row["ticker"] or ""
        suf = ticker.rsplit(".", 1)[-1] if "." in ticker else "(none)"
        by_suffix[suf] += 1
        if ticker not in found:
            miss_by_suffix[suf] += 1
    print()
    print("misses by exchange suffix:")
    for suf, count in by_suffix.most_common():
        miss = miss_by_suffix[suf]
        print(f"  .{suf:<6s} {miss}/{count} miss "
              f"({100 * miss / count:.0f}%)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", action="append", default=[],
                    help="probe a specific ticker (repeatable). "
                         "Bypasses Neo4j entirely.")
    ap.add_argument("--from-neo4j", action="store_true",
                    help="probe Listings pulled from Neo4j")
    ap.add_argument("--where-no-isin", action="store_true",
                    help="(with --from-neo4j) only Listings whose "
                         "isin is null/empty")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--neo4j-uri",
                    default=os.environ.get("NEO4J_URI",
                                           "bolt://neo4j:7687"))
    ap.add_argument("--neo4j-user",
                    default=os.environ.get("NEO4J_USER", "neo4j"))
    ap.add_argument("--neo4j-password",
                    default=os.environ.get("NEO4J_PASSWORD", ""))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    rows = _rows_from_args(args, ap)
    tickers = [r["ticker"] for r in rows if r["ticker"]]
    print(f"probing {len(tickers)} ticker(s) on Yahoo")

    client = httpx.Client()
    found = probe_yahoo(client, tickers)
    resolved = _print_table(rows, found)
    _print_summary(rows, found, resolved)
    return 0


if __name__ == "__main__":
    sys.exit(main())
