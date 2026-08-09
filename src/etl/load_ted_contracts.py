# pylint: disable=too-many-lines
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
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date as _date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fontem_event_schemas import builders
from fontem_events import EventLog
from neo4j import GraphDatabase

from eforms.filters import awards_only
from eforms.parser import parse as parse_notice_xml
from eforms.stream import stream_notices

from src.data.ted_raw_store import TedRawStore, TedPackageStore
from src.etl.data_description import DataDescription
from ..services.currency.client import CurrencyClient
from ..services.location_service import LocationService
from ..services.ted_lookup import TedLookupError, resolve_publication_number
from ._http import HTTP_HEADERS
from ._http_retry import call_with_retry
from .collapse_modifications import derive_contract_key
from .contract_confidence import score_contract_value
from .scale_normalization import normalize_scale
from . import value_review_queue
from .ted_matcher import TedMatcher
from . import ted_search

DESCRIPTION = DataDescription(
    producer="load_ted_contracts",
    label="TED Contracts",
    theme="procurement",
    summary="Public tenders and contract awards published by EU public bodies.",
    entities=(
        "Contract",
        "Authority",
        "Company",
    ),
    coverage="EU-threshold tenders only. National below-threshold procurement is not published to TED and is therefore absent here, which is a gap in the source, not in the world.",
    upstream="TED (Tenders Electronic Daily)",
    update_freq="daily",
    answers=(
        "Which companies won public contracts, and for how much",
        "What a public authority bought and from whom",
        "How often a tender attracted only one bidder",
    ),
)


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


def _download_monthly(year: int, month: int, dest: Path,
                      package_store=None) -> Path:
    """Fetch a TED monthly package, preferring cached copies.

    Resolution order: local disk (this pod) -> the durable package store
    (in-cluster minio, shared across runs) -> TED's CDN. A CDN download
    is uploaded to the store so the next re-parse never hits TED again.
    """
    url = TED_MONTHLY_URL.format(year=year, month=month)
    out = dest / f"ted-{year}-{month:02d}.tar.gz"
    if out.exists():
        logger.info("Using local cached %s", out)
        return out
    if package_store is not None and package_store.has(year, month):
        if package_store.fetch_to(year, month, out):
            logger.info("Fetched %d-%02d from package store", year, month)
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

    result = call_with_retry(_do_download)
    if package_store is not None:
        if package_store.save(year, month, result):
            logger.info("Cached %d-%02d to package store", year, month)
    return result


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
    logger.info("Match quality: %s", matcher.stats.summary())
    return {"total": total, "skipped": skipped, "elapsed_s": elapsed,
            "match_stats": matcher.stats.summary()}


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


def _supplier_vat(contractor) -> str | None:
    """Canonical VAT (or national/EORI identifier) for a supplier, or
    None. eforms-parser 0.2.0+ returns ``legal_id`` as a LegalIdentifier."""
    # pylint: disable=import-outside-toplevel
    from src.etl.identifiers import canon_vat

    if contractor.legal_id is None:
        return None
    scheme = (contractor.legal_id.scheme_name or "").upper()
    if scheme in ("VAT", "NATIONAL", "EORI", ""):
        return canon_vat(contractor.legal_id.value)
    return None


@dataclass
class _ResolvedSupplier:
    """One named supplier on a notice, resolved to a gmr_id."""

    award: Any
    contractor: Any
    match: Any
    is_winner: bool


def _match_provenance(match) -> "tuple[str | None, float | None]":
    """(match_tier, match_confidence) for a MatchResult. Layer 1 is the
    local VAT cache (a deterministic VAT match); a created-new node
    (layer 5) has no resolved tier or confidence against an existing
    entity, only the layer is recorded."""
    tier = match.resolver_tier or ("vat" if match.layer == 1 else None)
    confidence = None if match.created_new else match.confidence
    return tier, confidence


def _resolve_suppliers(notice, matcher, emit, seen_companies) -> list:
    """Resolve EVERY named supplier — winners AND named tenderers (the
    losing bidders some eForms dialects publish) — through the
    consolidator: same tiers, same confidence capture, and the same
    create-if-not-found minting as the historical single-winner path.
    Emits UpsertCompany once per first-seen gmr_id (per-run dedup; the
    sink would MERGE either way)."""
    resolved: list[_ResolvedSupplier] = []
    for supplier_award in notice.awards:
        contractor = notice.organizations.get(supplier_award.contractor_org_id)
        if not contractor:
            continue
        raw_vat = _supplier_vat(contractor)
        match = matcher.match_company(
            contractor.name, contractor.country, raw_vat,
        )
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
        resolved.append(_ResolvedSupplier(
            award=supplier_award, contractor=contractor, match=match,
            is_winner=bool(getattr(supplier_award, "is_winner", True)),
        ))
    return resolved


def _build_parties(resolved) -> "list[dict] | None":
    """The ``parties[]`` payload: one entry per distinct (company, role).

    A supplier that won several lots appears once; a company can appear
    as both 'winner' and 'named_tenderer' when it lost one lot and won
    another. A supplier with no published name is still resolved (its
    UpsertCompany went out) but is not representable — the schema
    requires a non-empty name — so it is omitted from the list."""
    parties: list[dict] = []
    seen: set = set()
    for entry in resolved:
        if not entry.contractor.name:
            continue
        role = "winner" if entry.is_winner else "named_tenderer"
        key = (str(entry.match.gmr_id), role)
        if key in seen:
            continue
        seen.add(key)
        tier, confidence = _match_provenance(entry.match)
        parties.append(builders.contract_party(
            company_gmr_id=str(entry.match.gmr_id),
            name=entry.contractor.name,
            role=role,
            rank=getattr(entry.award, "rank", None),
            is_consortium_member=bool(
                getattr(entry.award, "is_consortium_member", False)),
            tendering_party_id=getattr(
                entry.award, "tendering_party_id", None),
            match_tier=tier,
            match_confidence=confidence,
            match_layer=entry.match.layer,
        ))
    return parties or None


def _sum_raw(values):
    """Sum raw (pre-conversion) amounts. A single element passes
    through unchanged so non-numeric raw amounts still reach the
    currency parser exactly as the notice published them."""
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    try:
        return sum(float(v) for v in values)
    except (TypeError, ValueError):
        return None


def _winner_value_inputs(notice, resolved):
    """The three raw money signals, from WINNER awards only.

    A named tenderer's ``Award.value`` is its losing BID amount — never
    contract money — so non-winners contribute nothing here. Consortium
    members of one tendering party all restate the SAME undivided
    tender value, so amounts are counted once per (lot, tendering
    party), never once per member. The notice-level TotalAmount is only
    attributable when there is exactly one winning party; with several
    winners it is an aggregate we cannot split. Returns
    ``(estimate_raw, total_raw, payable_raw)``."""
    party_awards: dict = {}
    for entry in resolved:
        if not entry.is_winner:
            continue
        key = (
            getattr(entry.award, "lot_id", None),
            getattr(entry.award, "tendering_party_id", None)
            or entry.award.contractor_org_id,
        )
        party_awards.setdefault(key, entry.award)
    payable_raw = _sum_raw(
        [a.value for a in party_awards.values() if a.value is not None])
    total_raw = notice.total_value if len(party_awards) == 1 else None
    estimates = []
    seen_lots: set = set()
    for winner_award in party_awards.values():
        lot_id = getattr(winner_award, "lot_id", None)
        if lot_id in seen_lots:
            continue
        seen_lots.add(lot_id)
        estimate = _award_lot_estimate(notice, winner_award)
        if estimate is not None:
            estimates.append(estimate)
    return _sum_raw(estimates), total_raw, payable_raw


def _emit_notice(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements,too-many-arguments,too-many-positional-arguments
    notice, emit, matcher, seen_authorities, seen_companies,
    currency_svc, skip_pub_num_lookup: bool,
    *, pub_num_override: str | None = None,
    notice_id_override: str | None = None,
    extra_props: dict | None = None,
):
    """Process a single TED notice within an already-open
    ``log.batch(...)`` context — the caller owns commit semantics.

    Per-call side effects:
      * Mutates ``seen_authorities`` / ``seen_companies`` for
        per-run dedup of repeated parents within a single archive.
      * Calls ``emit.upsert`` zero or more times: UpsertAuthority /
        UpsertCompany for first-seen parents, then ONE UpsertContract
        per notice (notice-grain). Every named supplier the notice
        publishes is resolved and listed in ``parties[]``; the
        top-level company/match fields stay the primary winner's.

    ``skip_pub_num_lookup``: when True, skip the per-notice TED v3
    search call and emit Contracts with ``ted_publication_number=None``.
    The backfill (``src.etl.backfill_ted_publication_numbers``) fills
    it in later, in parallel, in 6–12 hours for the whole graph.
    Without skip, each notice pays ~500ms in the TED API and the
    loader is bottlenecked by that.
    """
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
                nuts=buyer.nuts,
            ),
        )
        seen_authorities.add(authority_id)

    # Legacy TED notices carry no eForms UUID; the incremental loader
    # passes the machine publication-number as the key so the Contract
    # IRI + ted_notice_id stay stable and match the _already_loaded
    # pre-check. For eForms this override equals notice.notice_id, so
    # it is a no-op there.
    ted_notice_id = notice_id_override or notice.notice_id
    # Pub-num lookup is per-notice, not per-award — the LRU cache
    # would coalesce repeated awards anyway, but doing it here also
    # avoids paying it before the first award when skip_pub_num_lookup
    # is False. With the flag, this is always None and the backfill
    # picks it up later.
    if pub_num_override is not None:
        # The incremental search-API path already carries the
        # publication-number from the search response — no per-notice
        # UUID->pub-num lookup needed.
        ted_publication_number = pub_num_override
    elif skip_pub_num_lookup:
        ted_publication_number = None
    else:
        ted_publication_number = _resolve_pub_num_or_none(ted_notice_id)

    # Every named supplier — winners AND named tenderers — resolves
    # through the consolidator; unmatched ones mint a new node
    # (create-if-not-found), exactly like the old single-winner path.
    resolved = _resolve_suppliers(notice, matcher, emit, seen_companies)
    if not resolved:
        return

    # The primary winner (first is_winner award) drives the top-level
    # company/match fields (backward compat) and the date/currency
    # context. A notice that names tenderers but resolves no winner
    # (rare, eForms SettledContract-era) still emits — its named
    # tenderers matter — but carries no company attribution and no
    # awarded value (the winner-only aggregation below yields None).
    primary = next((e for e in resolved if e.is_winner), None)
    context_award = (primary or resolved[0]).award

    declared_currency = context_award.currency or notice.currency
    effective_date, _date_source = _coalesce_date(context_award, notice)

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
    # TED uses non-currency placeholders (UNPUBLISHED, OP_DATPRO) in the
    # currency field when no value is published. Null them so the contract
    # carries no spurious currency (and no value to convert downstream).
    if resolved_currency and not re.fullmatch(r"[A-Z]{3}", resolved_currency):
        resolved_currency = None

    # The three money signals — WINNER-only, one undivided value per
    # winning tendering party (see _winner_value_inputs). Loser bids
    # and consortium-member restatements never reach the contract value.
    estimate_raw, total_raw, payable_raw = _winner_value_inputs(
        notice, resolved,
    )

    _est_orig, est_eur = _amount_to_eur(
        currency_svc, resolved_currency, rate_date_obj, estimate_raw,
    )
    tot_orig, tot_eur = _amount_to_eur(
        currency_svc, resolved_currency, rate_date_obj, total_raw,
    )
    pay_orig, pay_eur = _amount_to_eur(
        currency_svc, resolved_currency, rate_date_obj, payable_raw,
    )
    # Pre-modification total: legacy F20 modification notices
    # self-contain before+after, so a modification self-describes its
    # value change. Convert at the same rate as the after-value so the
    # before->after delta is a pure value change, free of FX drift.
    before_orig, before_eur = _amount_to_eur(
        currency_svc, resolved_currency, rate_date_obj,
        getattr(notice, "modification_value_before", None),
    )

    # Undo the national-gateway milli-euro leak (x1000) BEFORE scoring,
    # so the confidence scorer sees the corrected magnitudes. The marker
    # is carried onto the payload for dashboards + upstream reporting.
    buyer_country_a3 = (
        LocationService.to_alpha3(buyer.country) if buyer else None
    )
    scale = normalize_scale(
        estimate_eur=est_eur, total_eur=tot_eur, payable_eur=pay_eur,
        total_original=tot_orig, payable_original=pay_orig,
        country=buyer_country_a3,
    )
    if scale.corrected:
        logger.warning(
            "TED notice %s: monetary fields rescaled /1000 (%s): %s",
            ted_notice_id, scale.tier, scale.detail,
        )
        est_eur = scale.estimate_eur
        tot_eur, tot_orig = scale.total_eur, scale.total_original
        pay_eur, pay_orig = scale.payable_eur, scale.payable_original

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

    # A no-awarded-value contract must not carry a (stray, often
    # sign-flipped) monetary value; keep value_eur clean.
    if score.flag.value == "no_awarded_value":
        value_eur_float, value_original_float = None, None

    if score.is_low_confidence:
        logger.warning(
            "TED notice %s value EUR %.3g flagged '%s' "
            "(confidence %.2f) — stored but excluded from default "
            "aggregates: %s",
            ted_notice_id, value_eur_float or 0.0,
            score.flag.value, score.confidence, score.reason,
        )

    # ── Value quarantine ────────────────────────────────────
    # A value that fails hard sanity checks is WITHHELD, not
    # flagged-and-hoped: the event carries no monetary fields (the
    # sinks also clear any previously rendered ones) plus the
    # quarantine marker + reason. Review-tier claims go to
    # events.value_review for a human decision; a published 0
    # (zero_value) is auto-withheld — non-disclosure in costume —
    # and keeps the independent estimate. The claimed numbers are
    # never lost: event log + queue snapshot hold them.
    if score.quarantined:
        if score.needs_review:
            value_review_queue.enqueue_default(
                ted_notice_id=ted_notice_id,
                reason=score.flag.value,
                claimed_value_eur=value_eur_float,
                claimed_value_original=value_original_float,
                claimed_currency=resolved_currency,
                claimed_estimated_eur=est_eur,
                claimed_payable_eur=pay_eur,
                detail=score.reason,
            )
        value_eur_float = value_original_float = None
        resolved_currency = None
        if score.flag.value != "zero_value":
            est_eur = pay_eur = None
            before_eur = before_orig = None

    # Match provenance — lets exact (lei/vat/cik) and name-based
    # (name_country/fuzzy) attributions be told apart on the
    # AWARDED_TO edge downstream. The top-level fields stay the
    # PRIMARY winner's for backward compat; per-party provenance
    # rides each parties[] entry.
    if primary is not None:
        match_tier, match_confidence = _match_provenance(primary.match)
        company_gmr_id = str(primary.match.gmr_id)
        match_layer = primary.match.layer
    else:
        match_tier = match_confidence = None
        company_gmr_id = match_layer = None

    # Incremental stamps (procedure_id / notice_type /
    # modifies_publication_number) arrive via ``extra_props`` from the
    # search-API path; the bulk-archive path falls back to what the
    # parser read off the notice itself. contract_key / notice_kind
    # are derived with the shared helper so the producer stamp, the
    # sink's native Contract/Notice model and collapse_modifications'
    # Cypher grouping all agree on contract identity. (Bulk historical
    # loads with skip_pub_num_lookup stamp the notice UUID; the
    # collapse pass re-derives from node props once the
    # publication-number backfill has run.)
    stamps = extra_props or {}
    procedure_id = stamps.get("procedure_id")
    notice_type = (
        stamps.get("notice_type") or getattr(notice, "notice_type", None)
    )
    notice_kind = (
        "modification" if notice_type == _MODIFICATION_NOTICE_TYPE
        else "award"
    )
    modifies_publication_number = None
    if notice_kind == "modification":
        modifies_publication_number = (
            stamps.get("modifies_publication_number")
            or getattr(notice, "modifies_publication_number", None)
        )
    contract_key = derive_contract_key(
        procedure_id=procedure_id,
        notice_type=notice_type,
        modifies_publication_number=modifies_publication_number,
        ted_publication_number=ted_publication_number,
        ted_notice_id=ted_notice_id,
    )

    contract_payload = builders.upsert_contract(
        ted_notice_id=ted_notice_id,
        ted_publication_number=ted_publication_number,
        title=notice.title or None,
        authority_id=authority_id,
        company_gmr_id=company_gmr_id,
        match_tier=match_tier,
        match_confidence=match_confidence,
        match_layer=match_layer,
        publication_date=notice.issue_date or None,
        value_eur=value_eur_float,
        value_currency=resolved_currency,
        value_original=value_original_float,
        value_before_eur=before_eur,
        value_before_original=before_orig,
        estimated_value_eur=est_eur,
        value_payable_eur=pay_eur,
        value_confidence=score.confidence,
        value_confidence_consistency=score.consistency,
        value_confidence_plausibility=score.plausibility,
        value_quality_flag=score.flag.value,
        value_low_confidence=score.is_low_confidence,
        value_payable_discrepancy=score.has_payable_discrepancy,
        value_quarantined=score.quarantined or None,
        value_quarantine_reason=(score.flag.value
                                 if score.quarantined else None),
        value_scale_corrected=scale.tier if scale.corrected else None,
        cpv=notice.cpv_main,
        nuts=notice.nuts,
        language=getattr(notice, "language", None),
        # Country of the contracting authority (the buyer /
        # acquirer). Cascaded onto the Contract because TED
        # contracts are jurisdictionally grouped by the
        # procuring entity, not the awarded vendor.
        country=LocationService.to_alpha3(buyer.country),
        # Tender-integrity fields (eForms) — inputs to the SMSB
        # single-bidder / non-open indicators + the CRI red flags.
        # tenders_received stays the notice's published bidder COUNT;
        # parties[] (the named subset) must never redefine it. A COUNT is
        # >= 1 by definition; a 0/negative is corrupt parsing (some
        # non-eForms notices carry it), so withhold it rather than emit a
        # bidder count the graph must then reject
        # (values.contract_bidder_count_positive).
        procedure_type=notice.procedure_type,
        tenders_received=(
            context_award.tenders_received
            if (context_award.tenders_received or 0) > 0
            else None
        ),
        award_criterion_type=notice.award_criterion_type,
        submission_deadline=notice.submission_deadline,
        is_framework=notice.is_framework,
        eu_funded=notice.eu_funded,
        funding_programme=notice.funding_programme,
        procedure_id=procedure_id,
        notice_type=notice_type,
        notice_kind=notice_kind,
        modifies_publication_number=modifies_publication_number,
        contract_key=contract_key,
        parties=_build_parties(resolved),
    )
    emit.upsert(
        "UpsertContract",
        # IRI keyed by the stable UUID (notice_id) so it doesn't
        # change once TED assigns / revises a publication-number
        # after first ingest.
        iri=f"http://data.fontem.eu/id/Contract/{ted_notice_id}",
        domain="contract",
        payload=contract_payload,
    )


_WATERMARK_ID = "ted-incremental"
_MODIFICATION_NOTICE_TYPE = "can-modif"


def _read_watermark(session, watermark_id: str = _WATERMARK_ID) -> str | None:
    """Last publication-date (YYYY-MM-DD) this watermark's incremental
    loader fully ingested, or None if it has never run."""
    row = session.run(
        "MATCH (w:TedWatermark {id: $id}) RETURN w.last_publication_date AS d",
        id=watermark_id,
    ).single()
    return row["d"] if row and row["d"] else None


def _advance_watermark(session, watermark_id: str, day_iso: str) -> None:
    """Record ``day_iso`` (YYYY-MM-DD) as this watermark's latest
    fully-loaded date. Sticky-forward: never moves backwards."""
    session.run(
        "MERGE (w:TedWatermark {id: $id}) "
        "SET w.last_publication_date = CASE "
        "  WHEN coalesce(w.last_publication_date, '') < $day THEN $day "
        "  ELSE w.last_publication_date END",
        id=watermark_id, day=day_iso,
    )


def load_contracts_incremental(  # pylint: disable=too-many-locals,too-many-arguments,too-many-positional-arguments,too-many-statements
    driver,
    log: EventLog,
    since: _date,
    until: _date,
    currency_svc: CurrencyClient | None = None,
    notice_types: tuple[str, ...] = ted_search.NOTICE_TYPES,
    watermark_id: str = _WATERMARK_ID,
):
    """Incrementally load award + modification notices via TED's search
    API, one calendar day at a time from ``since`` to ``until`` inclusive.

    Per day: query the search API, skip notices already in the graph
    (cheap pre-check by notice-identifier), download + parse the rest, and
    emit Authority/Company/Contract events. Each contract is stamped with
    its publication-number (free from the search response), procedure_id,
    and notice_type; modifications also carry modifies_publication_number.
    The watermark advances one day at a time, only after that day fully
    loads, so an interrupted run resumes from the next unfinished day. A
    day that errors on *every* notice (e.g. API outage) stops the run
    without advancing, so we never silently skip a date.
    """
    totals = {"days": 0, "emitted": 0, "skipped": 0, "modifications": 0, "errors": 0}
    raw_store = TedRawStore.from_env()
    http = httpx.Client(timeout=ted_search.SEARCH_TIMEOUT)
    try:
        with driver.session() as session:
            matcher = TedMatcher(session)
            seen_authorities: set[str] = set()
            seen_companies: set[str] = set()
            day = since
            while day <= until:
                ymd = day.strftime("%Y%m%d")
                iso = day.isoformat()
                t0 = time.time()
                d_emit = d_skip = d_mod = d_err = 0
                for rec in ted_search.search_day(ymd, notice_types, client=http):
                    # Legacy TED notices have no notice-identifier (UUID);
                    # fall back to the publication-number as the stable key.
                    nid = (
                        rec.get("notice-identifier")
                        or rec.get("publication-number")
                    )
                    if nid and _already_loaded(session, nid):
                        d_skip += 1
                        continue
                    url = ted_search.xml_url(rec)
                    if not url:
                        continue
                    is_mod = rec.get("notice-type") == _MODIFICATION_NOTICE_TYPE
                    try:
                        xml_bytes = ted_search.fetch_xml(url, client=http)
                        # Persist raw XML (full-fidelity backstop) BEFORE
                        # parsing, keyed by publication-number, so any
                        # future field is a local re-parse, never a
                        # TED re-fetch. No-op if the store is unconfigured.
                        if raw_store is not None:
                            raw_store.put(
                                rec.get("publication-number") or nid, xml_bytes,
                            )
                        notice = parse_notice_xml(xml_bytes)
                        extra = {
                            "procedure_id": rec.get("procedure-identifier"),
                            "notice_type": rec.get("notice-type"),
                            "modifies_publication_number": (
                                (ted_search.modifies_publication_number(rec)
                                 or getattr(
                                     notice, "modifies_publication_number",
                                     None,
                                 ))
                                if is_mod else None
                            ),
                        }
                        with log.batch(
                            uuid.uuid4(), producer="load_ted_contracts",
                        ) as emit:
                            _emit_notice(
                                notice, emit, matcher,
                                seen_authorities, seen_companies, currency_svc,
                                skip_pub_num_lookup=True,
                                pub_num_override=rec.get("publication-number"),
                                notice_id_override=nid,
                                extra_props=extra,
                            )
                        d_emit += 1
                        d_mod += 1 if is_mod else 0
                    except Exception:  # pylint: disable=broad-except
                        d_err += 1
                        logger.exception(
                            "FAILED notice %s (pub=%s) on %s",
                            nid, rec.get("publication-number"), iso,
                        )
                day_all_errored = d_err > 0 and d_emit == 0 and d_skip == 0
                logger.info(
                    "Day %s: %d emitted (%d modif), %d skipped, %d errors in %.0fs",
                    iso, d_emit, d_mod, d_skip, d_err, time.time() - t0,
                )
                totals["days"] += 1
                totals["emitted"] += d_emit
                totals["skipped"] += d_skip
                totals["modifications"] += d_mod
                totals["errors"] += d_err
                if day_all_errored:
                    logger.error(
                        "Day %s errored on every notice — stopping without "
                        "advancing the watermark (will retry next run)", iso,
                    )
                    break
                _advance_watermark(session, watermark_id, iso)
                day += timedelta(days=1)
    finally:
        http.close()
    logger.info(
        "Incremental done: %d days, %d emitted (%d modifications), "
        "%d skipped, %d errors",
        totals["days"], totals["emitted"], totals["modifications"],
        totals["skipped"], totals["errors"],
    )
    logger.info("Match quality: %s", matcher.stats.summary())
    totals["match_stats"] = matcher.stats.summary()
    return totals


def main(argv=None):  # pylint: disable=too-many-statements,too-many-locals,too-many-branches
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Emit UpsertAuthority + UpsertContract events for TED awards",
    )
    parser.add_argument("--file", help="Path to a local TED archive")
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--from", dest="from_month",
                        help="Bulk reprocess start month YYYY-MM (walks to --to)")
    parser.add_argument("--to", dest="to_month",
                        help="Bulk reprocess end month YYYY-MM (default: --from)")
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
    parser.add_argument(
        "--since", help="Incremental start date YYYY-MM-DD (overrides watermark)",
    )
    parser.add_argument(
        "--until", help="Incremental end date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--lookback-days", type=int, default=7,
        help="Initial incremental window (days) when no watermark exists yet",
    )
    parser.add_argument(
        "--modifications-only", action="store_true",
        help="Incremental: load only can-modif notices (the modification backfill)",
    )
    parser.add_argument(
        "--watermark-id", default=_WATERMARK_ID,
        help="Watermark node id. Use a distinct id for backfills so they "
             "don't move the forward daily cron's watermark.",
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
        # CPV bootstrap: emits UpsertTaxonomyCode events. Idempotent;
        # re-runs are MERGE on (system='cpv', code) at the sink.
        from .load_cpv import load_cpv  # pylint: disable=import-outside-toplevel
        load_cpv(log, lang="en")

        if args.file or args.year or args.month or args.from_month:
            # Bulk path: a local archive, a single monthly package, or a
            # month range (historical reprocess). TED only publishes a
            # month's package after the month ends. Downloaded packages
            # are cached to the durable package store so a re-parse never
            # re-downloads from TED.
            package_store = TedPackageStore.from_env()
            if args.file:
                months = [None]
            elif args.from_month:
                start = _date.fromisoformat(args.from_month + "-01")
                end_s = args.to_month or args.from_month
                end = _date.fromisoformat(end_s + "-01")
                months = []
                cur = start
                while cur <= end:
                    months.append((cur.year, cur.month))
                    cur = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
            else:
                today = datetime.now().astimezone()
                months = [(args.year or today.year, args.month or today.month)]

            for ym in months:
                if args.file:
                    archive = Path(args.file)
                else:
                    yr, mo = ym
                    logger.info("=== reprocess month %d-%02d ===", yr, mo)
                    archive = _download_monthly(
                        yr, mo, Path("/tmp"), package_store=package_store,
                    )
                load_contracts(
                    driver, log, archive, currency_svc=currency_svc,
                    skip_pub_num_lookup=args.skip_pub_num_lookup,
                    rescore=args.rescore,
                )
                # Free disk between months (packages are >1 GB); the
                # durable copy lives in the package store.
                if not args.file and archive.exists():
                    archive.unlink()
        else:
            # Daily/incremental default: search-API by publication-date
            # from the watermark forward. Replaces the old current-month
            # monthly-package default, which 404-ed every day because TED
            # doesn't publish a month's package until the month is over.
            notice_types = (
                (_MODIFICATION_NOTICE_TYPE,) if args.modifications_only
                else ted_search.NOTICE_TYPES
            )
            until = _date.fromisoformat(args.until) if args.until else _date.today()
            if args.since:
                since = _date.fromisoformat(args.since)
            else:
                with driver.session() as session:
                    wm = _read_watermark(session, args.watermark_id)
                since = (
                    _date.fromisoformat(wm) + timedelta(days=1) if wm
                    else until - timedelta(days=args.lookback_days)
                )
            if since > until:
                logger.info(
                    "TED incremental: watermark already current (%s) — "
                    "nothing to load", since.isoformat(),
                )
            else:
                logger.info(
                    "TED incremental: %s..%s (%s)",
                    since.isoformat(), until.isoformat(),
                    "modifications-only" if args.modifications_only
                    else "awards+modifications",
                )
                load_contracts_incremental(
                    driver, log, since, until,
                    currency_svc=currency_svc, notice_types=notice_types,
                    watermark_id=args.watermark_id,
                )
                # Link the freshly-loaded modifications to their awards.
                from .link_ted_modifications import (  # pylint: disable=import-outside-toplevel
                    link_modifications,
                )
                link_modifications(driver, log)
                # collapse_modifications / project_contracts are retired as
                # post-load hooks: the neo4j sink writes the Contract/Notice
                # model natively (contract_key + notice_kind travel in the
                # event payload). Running the batch projection concurrently
                # with the native sink could relabel a Contract entity whose
                # first NOTICE_OF edge has not landed yet. project_contracts
                # remains available as a manual one-time converter for graphs
                # ingested before the native sink.
    finally:
        currency_svc.close()
        log.close()
        driver.close()


if __name__ == "__main__":
    main()
