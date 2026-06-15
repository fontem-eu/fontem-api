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
import logging
import os
import time
import uuid
from datetime import date as _date, datetime
from pathlib import Path

import httpx
from fontem_event_schemas import builders
from fontem_events import EventLog
from neo4j import GraphDatabase

from eforms.filters import awards_only
from eforms.stream import stream_notices

from ..services.currency.client import CurrencyClient
from ..services.location_service import LocationService
from ..services.ted_lookup import TedLookupError, resolve_publication_number
from ._http import HTTP_HEADERS
from ._http_retry import call_with_retry
from .contract_confidence import score_contract_value
from .ted_matcher import TedMatcher

logger = logging.getLogger(__name__)


def _resolve_pub_num_or_none(notice_uuid: str) -> str | None:
    """Resolve UUID → TED publication-number, returning None on any
    miss instead of raising. Notices that are queued but not yet
    published — or that TED's search returns no match for — get a
    ``None`` so the row persists without a stored pub-num. A later
    ETL pass (or the backfill) can refill it once TED has the data.

    Transport-level errors (TED API down, DNS, timeout) are also
    swallowed to None and logged at WARNING so a TED outage doesn't
    poison the whole ETL run; the contracts still land with a null
    pub-num and the runtime /api/contracts/<id>/ted-link redirector
    becomes the path of last resort.

    ``resolve_publication_number`` is LRU-cached, so this is O(1) for
    notice UUIDs already resolved this run (multi-award notices
    inside a single batch don't pay extra TED calls)."""
    try:
        return resolve_publication_number(notice_uuid)
    except TedLookupError as exc:
        logger.debug("no TED publication-number for %s: %s", notice_uuid, exc)
        return None
    except httpx.HTTPError as exc:
        logger.warning(
            "TED search lookup transport error for %s (%s) — "
            "storing null pub-num, runtime redirector will retry",
            notice_uuid, exc,
        )
        return None


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

# Contract value handling is delegated to ``contract_confidence``. The
# eForms notice carries three money signals — the lot/notice estimate
# (``EstimatedOverallContractAmount``), the awarded total
# (``NoticeResult/cbc:TotalAmount``), and the per-award payable
# (``LegalMonetaryTotal/cbc:PayableAmount``). The loader stores all
# three (the chosen value preferring the total), plus a [0,1] confidence
# and a quality flag. Low-confidence values are kept but flagged so
# downstream queries can exclude them from default aggregates. This
# replaced three hard-coded guards (a 100x estimate-mismatch check, a
# €100B absolute cap, and a €1B audit log) which (a) silently nulled
# values rather than flagging them and (b) could not fire when no lot
# estimate was parsed — exactly the gap that let the Forca Aerea
# aircraft ship at €7.27B.


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
        # Per-chunk read timeout = 60s; if the CDN goes silent for a
        # full minute mid-transfer we abort fast rather than letting
        # the cronjob deadline (2h) run out. A naive `timeout=600`
        # applies the 600s to inactivity between chunks, which can
        # tolerate hours of trickle on a misbehaving upstream — that
        # was the trap that bit Eurostat (see stats_etl PR #138).
        # Stream-to-file rather than buffer-in-memory because monthly
        # TED packages run >1 GB.
        timeout = httpx.Timeout(connect=10.0, read=60.0,
                                write=10.0, pool=10.0)
        with httpx.stream("GET", url, timeout=timeout,
                          follow_redirects=True,
                          headers=HTTP_HEADERS) as r:
            r.raise_for_status()
            with open(out, "wb") as f:
                for chunk in r.iter_bytes(chunk_size=256 * 1024):
                    f.write(chunk)
        logger.info("Downloaded %s (%.0f MB)", out, out.stat().st_size / 1e6)
        return out

    return call_with_retry(_do_download)


def _already_loaded(session, ted_notice_id: str) -> bool:
    """Return True if a Contract with this ``ted_notice_id`` already
    exists in Neo4j. Cheap O(1) check thanks to the
    ``Contract.ted_notice_id`` index. Used by ``load_contracts`` to
    skip notices that were ingested in a prior run — turns a re-run
    into a no-op for already-loaded notices instead of the previous
    per-notice TED-search + per-notice transaction cost."""
    row = session.run(
        "MATCH (c:Contract {ted_notice_id: $nid}) "
        "RETURN c.ted_notice_id LIMIT 1",
        nid=ted_notice_id,
    ).single()
    return row is not None


def load_contracts(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements,too-many-arguments,too-many-positional-arguments
    driver,
    log: EventLog,
    archive_path: Path,
    currency_svc: CurrencyClient | None = None,
    skip_pub_num_lookup: bool = False,
    rescore: bool = False,
):
    """Parse a TED archive and emit Authority/Contract/Company events.

    The Neo4j driver is used by ``TedMatcher`` to resolve each
    contractor to a stable gmr_id, and by an idempotency check to
    skip notices already ingested in a prior run; the actual writes
    go through the event log.

    Two commit-granularity changes from the previous shape:

    1. **Per-notice transactions.** The whole-archive batch was
       converted to one ``log.batch(...)`` per notice so committed
       rows are visible immediately (sinks + dashboards see progress
       in real time, not at end-of-month). The old shape held a
       single Postgres transaction open for hours, blocked
       autovacuum, hid progress from sinks, and lost everything if
       the pod restarted mid-archive. Each notice is now its own
       atomic unit, which matches TED's own publish boundary.

    2. **Skip-already-loaded.** Before processing a notice's awards
       we look up ``Contract.ted_notice_id`` in Neo4j. If the
       contract exists, the notice was loaded in a prior run and
       we skip it entirely — no TED-search call, no eForms parse
       awards loop, no emit. Re-runs of the same archive are O(1)
       per existing notice.

    ``skip_pub_num_lookup=True`` short-circuits the per-notice
    TED v3 search call (~500ms each) — useful for bulk historical
    loads where the publication-number is backfilled later by
    ``src.etl.backfill_ted_publication_numbers``. Without it, the
    loader rate is bounded by TED's API; with it, by archive
    parse + Neo4j sink throughput (~10x faster)."""
    total = 0
    skipped = 0
    t0 = time.time()
    loaded_at = datetime.now().astimezone().isoformat()  # pylint: disable=unused-variable
    seen_authorities: set[str] = set()
    seen_companies: set[str] = set()

    with driver.session() as session:
        matcher = TedMatcher(session)

        for notice in awards_only(stream_notices(archive_path)):
            # Idempotency gate — cheap pre-check before any TED
            # call or event emission. Notices already in Neo4j get
            # skipped wholesale; re-running an archive is now safe
            # AND fast.
            # rescore re-ingests notices already in the graph so the
            # confidence scorer (and any other loader change) re-runs
            # over them via the normal ETL+sink flow; the sink MERGEs,
            # so values overwrite in place.
            if not rescore and _already_loaded(session, notice.notice_id):
                skipped += 1
                continue

            # Per-notice transaction: events for THIS notice land
            # atomically. The whole-archive batch was hiding hours
            # of work in one open transaction.
            with log.batch(
                uuid.uuid4(), producer="load_ted_contracts",
            ) as emit:
                _emit_notice(
                    notice, emit, matcher,
                    seen_authorities, seen_companies,
                    currency_svc, skip_pub_num_lookup,
                )
                total += 1
            if total % 200 == 0:
                elapsed = time.time() - t0
                rate = total / elapsed if elapsed else 0
                logger.info(
                    "  %d notices emitted, %d skipped (%.0f notices/s)",
                    total, skipped, rate,
                )

    elapsed = time.time() - t0
    logger.info(
        "Done: %d notices emitted, %d skipped in %.0fs",
        total, skipped, elapsed,
    )
    return {"total": total, "skipped": skipped, "elapsed_s": elapsed}


def _award_lot_estimate(notice, award):
    """The ``EstimatedOverallContractAmount`` of the lot this award
    belongs to, or None. Falls back to the sole lot's estimate when the
    award carries no lot_id (single-lot notices, the common case)."""
    lots = notice.lots or []
    lot_id = getattr(award, "lot_id", None)
    if lot_id:
        for lot in lots:
            if getattr(lot, "lot_id", None) == lot_id:
                return getattr(lot, "estimated_value", None)
    if len(lots) == 1:
        return getattr(lots[0], "estimated_value", None)
    return None


def _amount_to_eur(currency_svc, resolved_currency, rate_date_obj, raw):
    """Return ``(original_float, eur_float)`` for one raw amount in the
    notice currency. Without a currency service (unit tests / degraded
    mode) the original doubles as the EUR proxy so scoring still runs."""
    if raw is None:
        return None, None
    if not currency_svc:
        return float(raw), float(raw)
    parsed, _ = currency_svc.parse_value(raw)
    if parsed is None:
        return None, None
    original = float(parsed)
    eur = None
    if resolved_currency:
        dec = currency_svc.to_eur(parsed, resolved_currency, rate_date_obj)
        eur = float(dec) if dec is not None else None
    return original, eur


def _emit_notice(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements,too-many-arguments,too-many-positional-arguments
    notice, emit, matcher, seen_authorities, seen_companies,
    currency_svc, skip_pub_num_lookup: bool,
):
    """Process a single TED notice within an already-open
    ``log.batch(...)`` context — the caller owns commit semantics.

    Per-call side effects:
      * Mutates ``seen_authorities`` / ``seen_companies`` for
        per-run dedup of repeated parents within a single archive.
      * Calls ``emit.upsert`` zero or more times depending on how
        many awards the notice has and how many distinct
        (authority, contractor) pairs are encountered.

    ``skip_pub_num_lookup``: when True, skip the per-notice TED v3
    search call and emit Contracts with ``ted_publication_number=None``.
    The backfill (``src.etl.backfill_ted_publication_numbers``) fills
    it in later, in parallel, in 6–12 hours for the whole graph.
    Without skip, each notice pays ~500ms in the TED API and the
    loader is bottlenecked by that.
    """
    # pylint: disable=import-outside-toplevel
    from src.etl.identifiers import canon_vat

    buyer = notice.buyer()
    if not buyer:
        return
    buyer_legal_value = (
        buyer.legal_id.value if buyer.legal_id else None
    )
    authority_id = matcher.match_authority(
        buyer.name, buyer.country, buyer_legal_value,
    )

    # Authority dedup is per-archive (the caller threads
    # ``seen_authorities`` through). Once an authority has appeared in
    # the archive we skip the redundant UpsertAuthority — the sink
    # would MERGE either way, but eliding it keeps the event log
    # compact and replay-faster.
    if authority_id not in seen_authorities:
        emit.upsert(
            "UpsertAuthority",
            iri=f"http://data.fontem.eu/id/Authority/{authority_id}",
            domain="authority",
            payload=builders.upsert_authority(
                authority_id=authority_id,
                name=buyer.name,
                country=LocationService.to_alpha3(buyer.country),
                authority_type="contracting",
                national_id=buyer_legal_value,
            ),
        )
        seen_authorities.add(authority_id)

    ted_notice_id = notice.notice_id
    # Pub-num lookup is per-notice, not per-award — the LRU cache
    # would coalesce repeated awards anyway, but doing it here also
    # avoids paying it before the first award when skip_pub_num_lookup
    # is False. With the flag, this is always None and the backfill
    # picks it up later.
    if skip_pub_num_lookup:
        ted_publication_number = None
    else:
        ted_publication_number = _resolve_pub_num_or_none(ted_notice_id)

    for award in notice.awards:
        contractor = notice.organizations.get(award.contractor_org_id)
        if not contractor:
            continue

        # eforms-parser 0.2.0 returns ``legal_id`` as a LegalIdentifier.
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

        declared_currency = award.currency or notice.currency
        effective_date, _date_source = _coalesce_date(award, notice)

        # Resolve currency + FX-rate date once; the estimate, the awarded
        # total, and the payable all convert at the same rate.
        resolved_currency = None
        rate_date_obj = None
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
        else:
            resolved_currency = declared_currency

        # The three money signals. The notice-level TotalAmount is only
        # attributable to one award; for multi-award notices it is an
        # aggregate, so we omit it and rely on the payable + estimate.
        estimate_raw = _award_lot_estimate(notice, award)
        total_raw = (
            notice.total_value if len(notice.awards or []) == 1 else None
        )
        payable_raw = award.value

        _est_orig, est_eur = _amount_to_eur(
            currency_svc, resolved_currency, rate_date_obj, estimate_raw,
        )
        tot_orig, tot_eur = _amount_to_eur(
            currency_svc, resolved_currency, rate_date_obj, total_raw,
        )
        pay_orig, pay_eur = _amount_to_eur(
            currency_svc, resolved_currency, rate_date_obj, payable_raw,
        )

        score = score_contract_value(
            estimate_eur=est_eur, total_eur=tot_eur, payable_eur=pay_eur,
            total_original=tot_orig, payable_original=pay_orig,
        )
        # Store the chosen value (TotalAmount-preferred) in both
        # currencies. Low-confidence values are kept but flagged.
        if score.chosen_field == "total":
            value_eur_float, value_original_float = tot_eur, tot_orig
        elif score.chosen_field == "payable":
            value_eur_float, value_original_float = pay_eur, pay_orig
        else:
            value_eur_float, value_original_float = None, None

        if score.is_low_confidence:
            logger.warning(
                "TED notice %s value EUR %.3g flagged '%s' "
                "(confidence %.2f) — stored but excluded from default "
                "aggregates: %s",
                ted_notice_id, value_eur_float or 0.0,
                score.flag.value, score.confidence, score.reason,
            )

        # Emit Company once per archive (per-run dedup); the sink
        # would MERGE either way.
        if match.gmr_id not in seen_companies:
            emit.upsert(
                "UpsertCompany",
                iri=f"http://data.fontem.eu/id/Company/{match.gmr_id}",
                domain="company",
                payload=builders.upsert_company(
                    gmr_id=str(match.gmr_id),
                    name=contractor.name or None,
                    country=LocationService.to_alpha3(contractor.country),
                    vat=raw_vat,
                    active=True,
                ),
            )
            seen_companies.add(match.gmr_id)

        emit.upsert(
            "UpsertContract",
            # IRI keyed by the stable UUID (notice_id) so it doesn't
            # change once TED assigns / revises a publication-number
            # after first ingest.
            iri=f"http://data.fontem.eu/id/Contract/{ted_notice_id}",
            domain="contract",
            payload=builders.upsert_contract(
                ted_notice_id=ted_notice_id,
                ted_publication_number=ted_publication_number,
                title=notice.title or None,
                authority_id=authority_id,
                company_gmr_id=str(match.gmr_id),
                publication_date=notice.issue_date or None,
                value_eur=value_eur_float,
                value_currency=resolved_currency,
                value_original=value_original_float,
                estimated_value_eur=est_eur,
                value_payable_eur=pay_eur,
                value_confidence=score.confidence,
                value_confidence_consistency=score.consistency,
                value_confidence_plausibility=score.plausibility,
                value_quality_flag=score.flag.value,
                value_low_confidence=score.is_low_confidence,
                value_payable_discrepancy=score.has_payable_discrepancy,
                cpv=notice.cpv_main,
                nuts=getattr(notice, "place_nuts", None),
                language=getattr(notice, "language", None),
                # Country of the contracting authority (the buyer /
                # acquirer). Cascaded onto the Contract because TED
                # contracts are jurisdictionally grouped by the
                # procuring entity, not the awarded vendor.
                country=LocationService.to_alpha3(buyer.country),
            ),
        )


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Emit UpsertAuthority + UpsertContract events for TED awards",
    )
    parser.add_argument("--file", help="Path to a local TED archive")
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument(
        "--rescore", action="store_true",
        help="Re-ingest notices already in the graph (bypass the "
             "already-loaded skip) so the value confidence scorer "
             "re-runs over them. Used for backfills.",
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
    parser.add_argument(
        "--currency-service-url",
        default=os.environ.get(
            "CURRENCY_SERVICE_URL",
            "http://fontem-currency.currency-service.svc.cluster.local",
        ),
        help="Base URL of the fontem-currency HTTP service",
    )
    parser.add_argument(
        "--skip-pub-num-lookup",
        action="store_true",
        default=os.environ.get("TED_SKIP_PUB_NUM_LOOKUP", "").lower()
        in ("1", "true", "yes"),
        help=(
            "Skip the per-notice TED v3 search call that resolves "
            "ted_publication_number. Contracts are emitted with "
            "ted_publication_number=None; backfill via "
            "src.etl.backfill_ted_publication_numbers later. ~10x "
            "faster for bulk historical loads."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Currency conversion is now a remote HTTP call to the singleton
    # fontem-currency service (currency-service ns), not a local PVC
    # read. Construct unconditionally — the client degrades to "value
    # unknown" on network failure rather than crashing, so the loader
    # still produces Authority/Contract events even when the service
    # is briefly unavailable. Only EUR conversion is skipped in that
    # window; the contracts re-process cleanly on the next run.
    currency_svc = CurrencyClient(base_url=args.currency_service_url)
    logger.info("CurrencyClient → %s", args.currency_service_url)

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password),
    )
    log = EventLog.from_env()

    try:
        if args.file:
            archive = Path(args.file)
        else:
            # Default to the current calendar month when --year/--month
            # are omitted. The cronjob used to bake `--year $(date +%Y)
            # --month $(date +%m)` into argv expecting shell expansion,
            # but the container entrypoint runs `python` directly (no
            # /bin/sh wrap) so the literal string `$(date +%Y)` reached
            # argparse and aborted with `invalid int value`. Defaulting
            # in code makes the cronjob args empty + the daily run
            # always picks the current month, which is exactly what
            # the previous shape was trying to achieve.
            today = datetime.now().astimezone()
            year = args.year or today.year
            month = args.month or today.month
            archive = _download_monthly(year, month, Path("/tmp"))

        # CPV bootstrap: emits UpsertTaxonomyCode events. Idempotent;
        # re-runs are MERGE on (system='cpv', code) at the sink.
        from .load_cpv import load_cpv  # pylint: disable=import-outside-toplevel
        load_cpv(log, lang="en")

        load_contracts(
            driver, log, archive, currency_svc=currency_svc,
            skip_pub_num_lookup=args.skip_pub_num_lookup,
            rescore=args.rescore,
        )
    finally:
        currency_svc.close()
        log.close()
        driver.close()


if __name__ == "__main__":
    main()
