"""
TED Contract Awards → events.entity_events
=============================================
Downloads TED monthly/daily packages, parses eForms XML via the
eforms-parser library, matches companies via the TedMatcher (which
reads existing Companies from Neo4j to find a stable gmr_id), and
emits ``UpsertCompany`` + ``UpsertAuthority`` + ``UpsertContract``
events into the canonical event log.

The CATEGORIZED_AS → CPV edge is dropped from this loader for now;
``cpv`` rides along as a property on the Contract event. A follow-up
introduces an UpsertTaxonomyCode schema and the relationship event
once the generic schemas land.

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
import uuid
from datetime import date as _date, datetime
from decimal import Decimal as _Decimal
from pathlib import Path

import httpx
from fontem_event_schemas import builders
from fontem_events import EventLog
from neo4j import GraphDatabase

from eforms.filters import awards_only
from eforms.stream import stream_notices

from ..services.currency import CurrencyService
from ._http import HTTP_HEADERS
from ._http_retry import call_with_retry
from .ted_matcher import TedMatcher

logger = logging.getLogger(__name__)

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

    def _do_download() -> Path:
        # Clear any partial bytes left by a previous attempt so each
        # retry starts from zero — the upstream tar.gz is not
        # resume-friendly (no Range support on TED's CDN).
        if out.exists():
            out.unlink()
        logger.info("Downloading %s ...", url)
        with httpx.stream("GET", url, timeout=600, follow_redirects=True,
                          headers=HTTP_HEADERS) as r:
            r.raise_for_status()
            with open(out, "wb") as f:
                for chunk in r.iter_bytes(chunk_size=256 * 1024):
                    f.write(chunk)
        logger.info("Downloaded %s (%.0f MB)", out, out.stat().st_size / 1e6)
        return out

    return call_with_retry(_do_download)


def load_contracts(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    driver,
    log: EventLog,
    archive_path: Path,
    currency_svc: CurrencyService | None = None,
):
    """Parse a TED archive and emit Authority/Contract/Company events.

    The Neo4j driver is used READ-ONLY by ``TedMatcher`` to resolve
    each contractor to a stable gmr_id; the actual writes go through
    the event log."""
    batch_id = uuid.uuid4()
    total = 0
    t0 = time.time()
    loaded_at = datetime.now().astimezone().isoformat()

    with driver.session() as session, log.batch(
        batch_id, producer="load_ted_contracts",
    ) as emit:
        matcher = TedMatcher(session)
        seen_authorities: set[str] = set()
        seen_companies: set[str] = set()

        for notice in awards_only(stream_notices(archive_path)):
            buyer = notice.buyer()
            if not buyer:
                continue
            buyer_legal_value = (
                buyer.legal_id.value if buyer.legal_id else None
            )
            authority_id = matcher.match_authority(
                buyer.name, buyer.country, buyer_legal_value,
            )

            # Emit the Authority once per run, even if it appears on
            # many notices (the sink would MERGE either way, but this
            # keeps the event log compact and replay-faster).
            if authority_id not in seen_authorities:
                emit.upsert(
                    "UpsertAuthority",
                    iri=f"http://data.fontem.eu/id/Authority/{authority_id}",
                    domain="authority",
                    payload=builders.upsert_authority(
                        authority_id=authority_id,
                        name=buyer.name,
                        country=(buyer.country or "").upper() or None,
                        authority_type="contracting",
                        national_id=buyer_legal_value,
                    ),
                )
                seen_authorities.add(authority_id)

            for award in notice.awards:
                contractor = notice.organizations.get(
                    award.contractor_org_id
                )
                if not contractor:
                    continue

                # eforms-parser 0.2.0 returns `legal_id` as a LegalIdentifier.
                # Same VAT-routing logic as the pre-migration loader.
                from src.etl.identifiers import canon_vat  # pylint: disable=import-outside-toplevel

                raw_vat: str | None = None
                if contractor.legal_id is not None:
                    value = contractor.legal_id.value
                    scheme = (contractor.legal_id.scheme_name or "").upper()
                    if scheme == "VAT":
                        raw_vat = canon_vat(value)
                    elif scheme in ("NATIONAL", "EORI", ""):
                        raw_vat = canon_vat(value)

                match = matcher.match_company(
                    contractor.name, contractor.country, raw_vat,
                )

                pub_num = notice.publication_number or notice.notice_id
                declared_currency = award.currency or notice.currency
                raw_value = award.value or notice.total_value
                effective_date, _date_source = _coalesce_date(award, notice)

                # Parse + currency-resolve via the currency service.
                if currency_svc:
                    parsed_value, _was_sentinel = currency_svc.parse_value(
                        raw_value,
                    )
                else:
                    parsed_value = (
                        _Decimal(str(raw_value))
                        if raw_value is not None else None
                    )
                resolved_currency = None
                value_eur_decimal = None
                if currency_svc:
                    rate_date_str = effective_date or notice.issue_date
                    try:
                        rate_date_obj = (
                            _date.fromisoformat(rate_date_str[:10])
                            if rate_date_str else None
                        )
                    except (ValueError, TypeError):
                        rate_date_obj = None
                    resolved_currency, _inferred = currency_svc.resolve_currency(
                        declared_currency,
                        country=(buyer.country or "").upper(),
                        on=rate_date_obj,
                    )
                    if parsed_value is not None and resolved_currency:
                        value_eur_decimal = currency_svc.to_eur(
                            parsed_value, resolved_currency, rate_date_obj,
                        )
                else:
                    resolved_currency = declared_currency

                value_original_float = (
                    float(parsed_value) if parsed_value is not None else None
                )
                value_eur_float = (
                    float(value_eur_decimal)
                    if value_eur_decimal is not None else None
                )

                # Emit Company once per run too.
                if match.gmr_id not in seen_companies:
                    emit.upsert(
                        "UpsertCompany",
                        iri=f"http://data.fontem.eu/id/Company/{match.gmr_id}",
                        domain="company",
                        payload=builders.upsert_company(
                            gmr_id=str(match.gmr_id),
                            name=contractor.name or None,
                            country=(contractor.country or "").upper() or None,
                            vat=raw_vat,
                            active=True,
                        ),
                    )
                    seen_companies.add(match.gmr_id)

                emit.upsert(
                    "UpsertContract",
                    iri=f"http://data.fontem.eu/id/Contract/{pub_num}",
                    domain="contract",
                    payload=builders.upsert_contract(
                        ted_notice_id=pub_num,
                        title=notice.title or None,
                        authority_id=authority_id,
                        company_gmr_id=str(match.gmr_id),
                        publication_date=notice.issue_date or None,
                        value_eur=value_eur_float,
                        value_currency=resolved_currency,
                        value_original=value_original_float,
                        cpv=notice.cpv_main,
                        nuts=getattr(notice, "place_nuts", None),
                        language=getattr(notice, "language", None),
                    ),
                )
                total += 1
                if total % 2000 == 0:
                    elapsed = time.time() - t0
                    rate = total / elapsed if elapsed else 0
                    logger.info(
                        "  %d contracts emitted (%.0f/s)", total, rate,
                    )

    elapsed = time.time() - t0
    logger.info(
        "Done: %d contracts (%d authorities, %d companies) "
        "in %.1fs (loaded_at=%s)",
        total, len(seen_authorities), len(seen_companies),
        elapsed, loaded_at,
    )
    logger.info("Match stats: %s", json.dumps(matcher.stats.summary()))
    return {
        "total": total,
        "authorities": len(seen_authorities),
        "companies": len(seen_companies),
        "elapsed_s": round(elapsed, 1),
        "match_stats": matcher.stats.summary(),
    }


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Emit UpsertAuthority + UpsertContract events for TED awards",
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
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    currency_svc = None
    currency_dir = Path(args.currency_dir)
    if currency_dir.exists():
        currency_svc = CurrencyService.load(currency_dir)
        logger.info("CurrencyService loaded from %s", currency_dir)
    else:
        logger.warning(
            "No currency data at %s — EUR conversion skipped", currency_dir,
        )

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password),
    )
    log = EventLog.from_env()

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

        # CPV bootstrap: emits UpsertTaxonomyCode events. Idempotent;
        # re-runs are MERGE on (system='cpv', code) at the sink.
        from .load_cpv import load_cpv_divisions  # pylint: disable=import-outside-toplevel
        load_cpv_divisions(log)

        load_contracts(
            driver, log, archive, currency_svc=currency_svc,
        )
    finally:
        log.close()
        driver.close()


if __name__ == "__main__":
    main()
