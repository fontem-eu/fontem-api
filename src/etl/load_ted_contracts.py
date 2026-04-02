"""
TED Contract Awards → Neo4j
=============================
Downloads TED monthly/daily packages, parses eForms XML via the
eforms-parser library, matches companies, and MERGEs Contract +
Authority + Company nodes into Neo4j.

Usage:
    python -m src.etl.load_ted_contracts --year 2024 --month 6
    python -m src.etl.load_ted_contracts --from 2024-01 --to 2026-03
    python -m src.etl.load_ted_contracts --file /tmp/ted-2024-06.tar
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import httpx
from neo4j import GraphDatabase

from eforms.filters import awards_and_modifications
from eforms.stream import stream_notices

from .ted_matcher import TedMatcher

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
TED_MONTHLY_URL = "https://ted.europa.eu/packages/monthly/{year}-{month}"


def _download_monthly(year: int, month: int, dest: Path) -> Path:
    """Download a TED monthly package."""
    url = TED_MONTHLY_URL.format(year=year, month=month)
    out = dest / f"ted-{year}-{month:02d}.tar.gz"
    if out.exists():
        logger.info("Using cached %s", out)
        return out
    logger.info("Downloading %s ...", url)
    with httpx.stream("GET", url, timeout=600, follow_redirects=True) as r:
        r.raise_for_status()
        with open(out, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=256 * 1024):
                f.write(chunk)
    logger.info("Downloaded %s (%.0f MB)", out, out.stat().st_size / 1e6)
    return out


def load_contracts(driver, archive_path: Path):  # pylint: disable=too-many-locals
    """Parse a TED archive and load contracts into Neo4j."""
    merge_contract = """
    UNWIND $batch AS row
    MERGE (co:Company {gmr_id: row.company_gmr_id})
    ON CREATE SET co.name    = row.company_name,
                  co.country = row.company_country,
                  co.vat     = row.company_vat,
                  co.active  = true
    MERGE (auth:Authority {authority_id: row.authority_id})
    ON CREATE SET auth.name    = row.authority_name,
                  auth.country = row.authority_country
    MERGE (ct:Contract {ted_notice_id: row.notice_id})
    SET ct.bt701             = row.bt701,
        ct.ted_url           = row.ted_url,
        ct.title             = row.title,
        ct.description       = row.description,
        ct.value_eur         = row.value,
        ct.value_currency    = row.currency,
        ct.cpv_main          = row.cpv,
        ct.procedure_type    = row.procedure_type,
        ct.notice_type       = row.notice_type,
        ct.publication_date  = row.pub_date,
        ct.award_date        = row.award_date,
        ct.country           = row.authority_country,
        ct.loaded_at         = row.loaded_at
    MERGE (auth)-[:AWARDED]->(ct)
    MERGE (ct)-[:AWARDED_TO]->(co)
    WITH ct, row
    WHERE row.cpv IS NOT NULL
    MERGE (cpv:CPV {code: row.cpv})
    MERGE (ct)-[:CATEGORIZED_AS]->(cpv)
    """

    batch = []
    total = 0
    t0 = time.time()
    loaded_at = datetime.now().astimezone().isoformat()

    with driver.session() as session:
        # Create constraints
        session.run(
            "CREATE CONSTRAINT contract_notice_id IF NOT EXISTS "
            "FOR (ct:Contract) REQUIRE ct.ted_notice_id IS UNIQUE"
        )
        session.run(
            "CREATE CONSTRAINT authority_id IF NOT EXISTS "
            "FOR (a:Authority) REQUIRE a.authority_id IS UNIQUE"
        )

        matcher = TedMatcher(session)

        for notice in awards_and_modifications(
            stream_notices(archive_path)
        ):
            buyer = notice.buyer()
            if not buyer:
                continue

            authority_id = matcher.match_authority(
                buyer.name, buyer.country, buyer.legal_id,
            )

            for award in notice.awards:
                contractor = notice.organizations.get(
                    award.contractor_org_id
                )
                if not contractor:
                    continue

                # Normalize VAT (eforms may return a list in older formats)
                raw_vat = contractor.legal_id
                if isinstance(raw_vat, list):
                    raw_vat = raw_vat[0] if raw_vat else None

                match = matcher.match_company(
                    contractor.name, contractor.country, raw_vat,
                )

                pub_num = notice.publication_number or notice.notice_id
                batch.append({
                    "notice_id": pub_num,
                    "bt701": notice.notice_id,
                    "ted_url": (
                        f"https://ted.europa.eu/en/notice/{pub_num}"
                    ),
                    "title": notice.title or "",
                    "description": notice.description,
                    "value": award.value or notice.total_value,
                    "currency": award.currency or notice.currency,
                    "cpv": notice.cpv_main,
                    "procedure_type": notice.procedure_type,
                    "notice_type": notice.notice_type,
                    "pub_date": notice.issue_date,
                    "award_date": award.award_date,
                    "loaded_at": loaded_at,
                    "company_gmr_id": match.gmr_id,
                    "company_name": contractor.name,
                    "company_country": contractor.country or "",
                    "company_vat": raw_vat,
                    "authority_id": authority_id,
                    "authority_name": buyer.name,
                    "authority_country": buyer.country or "",
                })

                if len(batch) >= BATCH_SIZE:
                    session.run(merge_contract, batch=batch)
                    total += len(batch)
                    batch = []
                    elapsed = time.time() - t0
                    rate = total / elapsed if elapsed else 0
                    if total % 2000 < BATCH_SIZE:
                        logger.info(
                            "  %d contracts loaded (%.0f/s)", total, rate
                        )

        if batch:
            session.run(merge_contract, batch=batch)
            total += len(batch)

    elapsed = time.time() - t0
    logger.info(
        "Done: %d contracts in %.1fs", total, elapsed,
    )
    logger.info("Match stats: %s", json.dumps(matcher.stats.summary()))
    return {"total": total, "elapsed_s": round(elapsed, 1),
            "match_stats": matcher.stats.summary()}


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Load TED contract awards into Neo4j",
    )
    parser.add_argument("--file", help="Path to a local TED archive")
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
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
        default=os.environ.get("NEO4J_PASSWORD", "gmr-neo4j-2026"),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password),
    )

    try:
        if args.file:
            archive = Path(args.file)
        elif args.year and args.month:
            archive = _download_monthly(
                args.year, args.month, Path("/tmp"),
            )
        else:
            parser.error("Provide --file or --year + --month")
            return

        # Load CPV first
        from .load_cpv import load_cpv_divisions  # pylint: disable=import-outside-toplevel
        load_cpv_divisions(driver)

        load_contracts(driver, archive)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
