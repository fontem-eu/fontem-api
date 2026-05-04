"""
EU Listings → Neo4j   |   ESEF Financials → Virtuoso
======================================================
Reads eu_entities.json and summaries/*.json from the ESEF data
directory.

  * Listings still live in Neo4j (Companies + Listings haven't
    migrated yet — that's Phase 4 work). load_listings creates
    the Listing nodes and LISTED_AS edges as before.

  * Financial filings move to Virtuoso. load_financials resolves
    each summary's LEI → gmr_id against Neo4j Company nodes,
    then emits fontem:Filing triples via RdfFilingsWriter into
    the ``http://data.fontem.eu/graph/financials/esef`` named
    graph.

Usage:
    python -m src.etl.load_eu_listings --esef-dir /esef-data/esef \\
        --virtuoso-sparql-endpoint http://virtuoso.gmr.svc.cluster.local:8890/sparql
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from neo4j import GraphDatabase

from . import gmr_id
from .rdf_filings_writer import RdfFilingsWriter

logger = logging.getLogger(__name__)

BATCH_SIZE = 500

# Simplified country → currency mapping for major EU/EEA markets
COUNTRY_CURRENCY = {
    "AT": "EUR", "BE": "EUR", "CY": "EUR", "DE": "EUR",
    "EE": "EUR", "ES": "EUR", "FI": "EUR", "FR": "EUR",
    "GR": "EUR", "IE": "EUR", "IT": "EUR", "LT": "EUR",
    "LU": "EUR", "LV": "EUR", "MT": "EUR", "NL": "EUR",
    "PT": "EUR", "SI": "EUR", "SK": "EUR", "HR": "EUR",
    "GB": "GBP", "CH": "CHF", "SE": "SEK", "DK": "DKK",
    "NO": "NOK", "PL": "PLN", "CZ": "CZK", "HU": "HUF",
    "RO": "RON", "BG": "BGN", "IS": "ISK", "UA": "UAH",
    "LI": "CHF",
}


def load_listings(driver, entities: dict):
    """Create Company nodes (always) and Listing nodes (only when ticker is not null)."""
    company_query = """
    UNWIND $batch AS row
    MERGE (c:Company {gmr_id: row.gmr_id})
    ON CREATE SET c.lei = row.lei, c.name = row.name,
                  c.country = row.country, c.active = true
    """
    listing_query = """
    UNWIND $batch AS row
    MATCH (c:Company {gmr_id: row.gmr_id})
    MERGE (l:Listing {ticker: row.ticker})
    SET l.exchange = row.exchange,
        l.currency = row.currency,
        l.active   = true
    MERGE (c)-[:LISTED_AS]->(l)
    """
    company_batch = []
    listing_batch = []
    total_companies = 0
    total_listings = 0

    with driver.session() as session:
        session.run(
            "CREATE CONSTRAINT listing_ticker IF NOT EXISTS "
            "FOR (l:Listing) REQUIRE l.ticker IS UNIQUE"
        )
        for _key, meta in entities.items():
            lei = meta.get("lei", "")
            gid = gmr_id.from_lei(lei) if len(lei) == 20 else (
                gmr_id.from_name(
                    meta.get("country", "XX"),
                    meta.get("name", _key),
                )
            )
            company_batch.append({
                "gmr_id": gid,
                "lei": lei if len(lei) == 20 else None,
                "name": meta.get("name", ""),
                "country": meta.get("country", ""),
            })

            ticker = meta.get("ticker")
            if ticker is not None:
                listing_batch.append({
                    "gmr_id": gid,
                    "ticker": ticker,
                    "exchange": meta.get("exchange", ""),
                    "currency": COUNTRY_CURRENCY.get(
                        meta.get("country", ""), "EUR"
                    ),
                })

            if len(company_batch) >= BATCH_SIZE:
                session.run(company_query, batch=company_batch)
                total_companies += len(company_batch)
                company_batch = []
            if len(listing_batch) >= BATCH_SIZE:
                session.run(listing_query, batch=listing_batch)
                total_listings += len(listing_batch)
                listing_batch = []

        if company_batch:
            session.run(company_query, batch=company_batch)
            total_companies += len(company_batch)
        if listing_batch:
            session.run(listing_query, batch=listing_batch)
            total_listings += len(listing_batch)

    logger.info("Companies: %d created/updated, Listings: %d (skipped %d without ticker)",
                total_companies, total_listings, total_companies - total_listings)
    return total_companies


_FILING_FIELDS = (
    "filing_date", "revenue", "gross_profit",
    "operating_income", "net_income", "eps",
    "total_assets", "equity", "cash_and_equivalents",
    "capex", "operating_cashflow", "free_cashflow",
    "total_liabilities", "current_assets",
    "current_liabilities", "inventory",
    "shares_outstanding", "long_term_debt",
    "interest_expense", "income_tax_expense",
    "depreciation_amortization",
)


def _build_lei_to_gmr_index(driver, leis: list[str]) -> dict[str, str]:
    """Resolve LEI → gmr_id for the LEIs we actually care about.

    The previous "full Company scan" form blew the server-side
    transaction timeout against a 3.5M-row Company table. Pass
    in just the LEIs that have summaries on disk and the
    `c.lei IN $leis` predicate uses the existing
    company_lei index for an indexed lookup.
    """
    if not leis:
        return {}
    out: dict[str, str] = {}
    with driver.session() as session:
        for row in session.run(
            "MATCH (c:Company) WHERE c.lei IN $leis "
            "RETURN c.lei AS lei, c.gmr_id AS gmr_id",
            leis=leis,
        ):
            out[row["lei"]] = row["gmr_id"]
    return out


def load_financials(driver, summaries_dir: Path, writer: RdfFilingsWriter):
    """Read summary JSON files, resolve LEI→gmr_id from Neo4j,
    and PUT a fontem:Filing batch to the ESEF named graph.

    Matches Companies by LEI (the same key the Neo4j-era loader
    used) so financial data is preserved for entities without a
    resolved Listing ticker.
    """
    if not summaries_dir.exists():
        logger.warning("Summaries dir not found: %s", summaries_dir)
        return 0

    # Prefetch the LEI list from disk so the Neo4j lookup is
    # bounded — full Company scans timed out in prod.
    summaries = sorted(summaries_dir.glob("*.json"))
    needed_leis: list[str] = []
    docs: list[tuple[Path, dict]] = []
    for path in summaries:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping %s: %s", path.name, exc)
            continue
        lei = doc.get("lei")
        if not lei or len(lei) != 20:
            continue
        needed_leis.append(lei)
        docs.append((path, doc))

    lei_to_gmr = _build_lei_to_gmr_index(driver, list(set(needed_leis)))
    logger.info(
        "Built LEI→gmr_id index for %d distinct LEIs (resolved %d)",
        len(set(needed_leis)), len(lei_to_gmr),
    )

    all_records: list[dict] = []
    files_processed = 0
    skipped_no_lei = len(summaries) - len(docs)
    skipped_no_company = 0

    for path, doc in docs:
        lei = doc["lei"]
        company_gmr = lei_to_gmr.get(lei)
        if not company_gmr:
            skipped_no_company += 1
            continue

        for filing in doc.get("filings", []):
            year = filing.get("year")
            if year is None:
                continue
            rec = {"gmr_id": company_gmr, "year": int(year)}
            for key in _FILING_FIELDS:
                if (val := filing.get(key)) is not None:
                    rec[key] = val
            all_records.append(rec)

        files_processed += 1
        if files_processed % 1000 == 0:
            logger.info(
                "  %d files, %d filings",
                files_processed, len(all_records),
            )

    if not all_records:
        logger.warning("no ESEF records found; skipping write")
        return 0

    res = writer.write(all_records)
    logger.info(
        "ESEF: wrote %d filings (%d triples) from %d files "
        "(skipped %d without LEI, %d without resolvable Company)",
        res.written, res.triples_pushed, files_processed,
        skipped_no_lei, skipped_no_company,
    )
    return res.written


def main(argv=None):
    """CLI entry point.

    Listings continue to land in Neo4j; financials land in
    Virtuoso. Both stores have to be reachable for a full run.
    """
    parser = argparse.ArgumentParser(
        description="Load EU listings (Neo4j) + ESEF financials (Virtuoso)",
    )
    parser.add_argument(
        "--esef-dir",
        default=os.environ.get("GMR_ESEF_DATA_DIR", "/esef-data/esef"),
    )
    parser.add_argument("--neo4j-uri", default="bolt://neo4j:7687")
    parser.add_argument("--neo4j-user", default="neo4j")
    parser.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", ""))
    parser.add_argument(
        "--virtuoso-sparql-endpoint",
        default=os.environ.get("VIRTUOSO_SPARQL_ENDPOINT", ""),
    )
    parser.add_argument(
        "--virtuoso-dba-user",
        default=os.environ.get("VIRTUOSO_DBA_USER", "dba"),
    )
    parser.add_argument(
        "--virtuoso-dba-password",
        default=os.environ.get("VIRTUOSO_DBA_PASSWORD", ""),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not args.virtuoso_sparql_endpoint:
        logger.error(
            "VIRTUOSO_SPARQL_ENDPOINT must be set; ESEF financials "
            "now write to Virtuoso (FinancialYear cutover)."
        )
        sys.exit(2)

    esef_dir = Path(args.esef_dir)
    entities_path = esef_dir / "eu_entities.json"
    if not entities_path.exists():
        logger.error("eu_entities.json not found at %s", entities_path)
        return

    entities = json.loads(entities_path.read_text(encoding="utf-8"))
    logger.info("Loaded %d EU entities from %s", len(entities), entities_path)

    writer = RdfFilingsWriter(
        source="esef",
        sparql_endpoint=args.virtuoso_sparql_endpoint,
        dba_user=args.virtuoso_dba_user,
        dba_password=args.virtuoso_dba_password,
    )
    driver = GraphDatabase.driver(
        args.neo4j_uri,
        auth=(args.neo4j_user, args.neo4j_password),
    )
    t0 = time.time()
    try:
        load_listings(driver, entities)
        load_financials(driver, esef_dir / "summaries", writer)
    finally:
        driver.close()

    logger.info("EU load complete in %.1fs", time.time() - t0)


if __name__ == "__main__":
    main()
