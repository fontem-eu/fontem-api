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
from datetime import date as _date, datetime
from decimal import Decimal as _Decimal
from pathlib import Path

import httpx
from neo4j import GraphDatabase

from eforms.filters import awards_only
from eforms.stream import stream_notices

from ..services.currency import CurrencyService
from .ted_matcher import TedMatcher

logger = logging.getLogger(__name__)

BATCH_SIZE = 500

# Path to currency data directory (per-currency JSON files)
_DEFAULT_CURRENCY_DIR = os.environ.get(
    "CURRENCY_DATA_DIR",
    "/srv/nfs/currency-data",
)


def _coalesce_date(award, notice) -> tuple[str | None, str]:
    """Coalesce the best available date for a contract award.

    Returns (date_str, source) where source is one of:
    'award', 'conclusion', 'dispatch', 'publication'.
    """
    if award.award_date:
        return award.award_date, "award"
    if getattr(award, "conclusion_date", None):
        return award.conclusion_date, "conclusion"
    if getattr(notice, "dispatch_date", None):
        return notice.dispatch_date, "dispatch"
    if notice.issue_date:
        return notice.issue_date, "publication"
    return None, "none"


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


def load_contracts(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    driver,
    archive_path: Path,
    currency_svc: CurrencyService | None = None,
):
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
    SET ct.bt701              = row.bt701,
        ct.ted_url            = row.ted_url,
        ct.title              = row.title,
        ct.description        = row.description,
        ct.value_original     = row.value_original,
        ct.value_original_str = row.value_original_str,
        ct.value_eur          = row.value_eur,
        ct.value_eur_str      = row.value_eur_str,
        ct.value_currency     = row.currency,
        ct.value_undisclosed  = row.value_undisclosed,
        ct.currency_inferred  = row.currency_inferred,
        ct.cpv_main           = row.cpv,
        ct.procedure_type     = row.procedure_type,
        ct.notice_type        = row.notice_type,
        ct.publication_date   = row.pub_date,
        ct.award_date         = row.award_date,
        ct.award_date_source  = row.award_date_source,
        ct.country            = row.authority_country,
        ct.loaded_at          = row.loaded_at
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
    touched_companies: set[str] = set()
    touched_authorities: set[str] = set()

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

        for notice in awards_only(
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
                declared_currency = award.currency or notice.currency
                raw_value = award.value or notice.total_value

                # Coalesce best available date
                effective_date, date_source = _coalesce_date(award, notice)

                # Parse value with sentinel detection
                if currency_svc:
                    parsed_value, was_sentinel = currency_svc.parse_value(raw_value)
                else:
                    parsed_value = (
                        _Decimal(str(raw_value)) if raw_value is not None else None
                    )
                    was_sentinel = False

                # Resolve currency: declared, then country fallback
                currency_inferred = False
                resolved_currency = None
                if currency_svc:
                    rate_date_str = effective_date or notice.issue_date
                    try:
                        rate_date_obj = (
                            _date.fromisoformat(rate_date_str[:10])
                            if rate_date_str else None
                        )
                    except (ValueError, TypeError):
                        rate_date_obj = None
                    resolved_currency, currency_inferred = currency_svc.resolve_currency(
                        declared_currency,
                        country=(buyer.country or "").upper(),
                        on=rate_date_obj,
                    )
                else:
                    resolved_currency = declared_currency

                # Convert to EUR
                value_eur_decimal = None
                if currency_svc and parsed_value is not None and resolved_currency:
                    rate_date_str = effective_date or notice.issue_date
                    try:
                        rate_date_obj = (
                            _date.fromisoformat(rate_date_str[:10])
                            if rate_date_str else None
                        )
                    except (ValueError, TypeError):
                        rate_date_obj = None
                    value_eur_decimal = currency_svc.to_eur(
                        parsed_value, resolved_currency, rate_date_obj,
                    )

                # Float for fast aggregation, string for lossless display
                value_original_float = (
                    float(parsed_value) if parsed_value is not None else None
                )
                value_original_str = (
                    str(parsed_value) if parsed_value is not None else None
                )
                value_eur_float = (
                    float(value_eur_decimal) if value_eur_decimal is not None else None
                )
                value_eur_str = (
                    str(value_eur_decimal) if value_eur_decimal is not None else None
                )

                # Capture for the post-ETL consolidator hook
                touched_companies.add(match.gmr_id)
                touched_authorities.add(authority_id)

                batch.append({
                    "notice_id": pub_num,
                    "bt701": notice.notice_id,
                    "ted_url": (
                        f"https://ted.europa.eu/en/notice/{pub_num}"
                    ),
                    "title": notice.title or "",
                    "description": notice.description,
                    "value_original": value_original_float,
                    "value_original_str": value_original_str,
                    "value_eur": value_eur_float,
                    "value_eur_str": value_eur_str,
                    "currency": resolved_currency,
                    "value_undisclosed": was_sentinel,
                    "currency_inferred": currency_inferred,
                    "cpv": notice.cpv_main,
                    "procedure_type": notice.procedure_type,
                    "notice_type": notice.notice_type,
                    "pub_date": notice.issue_date,
                    "award_date": effective_date,
                    "award_date_source": date_source,
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
    # Notify the consolidator about touched Company + Authority nodes.
    from src.etl._hooks import notify_consolidator
    notify_consolidator("Company", list(touched_companies))
    notify_consolidator("Authority", list(touched_authorities))
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
        default=os.environ.get("NEO4J_PASSWORD", ""),
    )
    parser.add_argument(
        "--currency-dir",
        default=os.environ.get("CURRENCY_DATA_DIR", _DEFAULT_CURRENCY_DIR),
        help="Path to currency data directory (per-currency rate files)",
    )
    # Legacy alias for backward compat
    parser.add_argument("--rates-file", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Load currency service
    currency_svc = None
    currency_dir = Path(args.currency_dir)
    if currency_dir.exists():
        currency_svc = CurrencyService.load(currency_dir)
        logger.info("CurrencyService loaded from %s", currency_dir)
    else:
        logger.warning(
            "No currency data at %s — EUR conversion will be skipped", currency_dir,
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

        load_contracts(driver, archive, currency_svc=currency_svc)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
