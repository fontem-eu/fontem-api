# pylint: disable=too-many-lines
"""
TED Contract Awards → events.entity_events
=============================================
Parses TED notices (award + contract-modification) via the
eforms-parser library, matches companies via the TedMatcher (which
reads existing Companies from Neo4j to find a stable gmr_id), and
emits ``UpsertCompany`` + ``UpsertAuthority`` + ``UpsertContract``
events into the canonical event log.

There is ONE ingest path — :func:`ingest_notice`. Notices reach it
from two discovery mechanisms (a monthly archive, or TED's search API
day by day) that differ only in how the XML is found. Every identity
field on the event (publication number, procedure id, notice version,
the modification back-link) is read from the notice XML by the parser,
never from the search response, so the same notice produces the same
event whichever way it arrived. Until 2026-09 the two paths stamped
different keys (archive: notice UUID; search: procedure id) and an
award and its later modification became two contracts — see
gitops/docs/roadmap/contract-modifications-single-path.md.

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
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date as _date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fontem_event_schemas import builders
from fontem_events import EventLog
from neo4j import GraphDatabase
from neo4j.exceptions import TransientError

from eforms.filters import awards_and_modifications
from eforms.parser import parse as parse_notice_xml
from eforms.stream import stream_notices

from src.data.ted_raw_store import TedRawStore, TedPackageStore
from src.data_quality.peer_value_stats import load_peer_stats_from_env
from src.etl.data_description import DataDescription
from ..services.currency.client import CurrencyClient
from ..services.location_service import LocationService
from ._http import HTTP_HEADERS
from ._http_retry import call_with_retry
from .cleaning import (
    CleaningReport, Lookups, Quarantine, Rescale, ValueFacts, run_stage,
)
from .cleaning.adapter import facts_from_notice, integer, number, text
from .cleaning.rules.dates import is_placeholder_day
from .contract_confidence import score_contract_value
from .dry_run_log import DryRunEventLog
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
    coverage=(
        "EU-threshold tenders only. National below-threshold procurement is not published to TED "
        "and is therefore absent here, which is a gap in the source, not in the world."
    ),
    upstream="TED (Tenders Electronic Daily)",
    update_freq="daily",
    answers=(
        "Which companies won public contracts, and for how much",
        "What a public authority bought and from whom",
        "How often a tender attracted only one bidder",
    ),
)


logger = logging.getLogger(__name__)


def _as_day(raw: str | None) -> str | None:
    """Normalise a TED date to a bare ISO day.

    TED stamps dates with an offset ('2026-08-25+02:00', sometimes 'Z').
    The offset is the publishing timezone, not information we model, and
    :Contract.publication_date is compared as a plain 'YYYY-MM-DD' string
    everywhere (range index, era guards, feed windows), so a value that
    kept its suffix would sort and filter wrongly. Sentinel dates TED uses
    for 'unknown' are dropped rather than stored as fact.
    """
    if not isinstance(raw, str) or not raw:
        return None
    day = raw[:10]
    # Must be a real ISO day, not merely ten characters: a malformed or
    # non-string value silently coerced into a date would corrupt every
    # window and index that reads this field.
    try:
        _date.fromisoformat(day)
    except ValueError:
        return None
    # Counted per notice by the cleaning stage's generic.date_placeholder
    # rule; the typed field still drops the sentinel exactly as before.
    if is_placeholder_day(day):
        return None
    return day


def _coalesce_date(award, notice) -> tuple[str | None, str]:
    """Coalesce the best available date for a contract award.

    Returns (date_str, source) where source is one of:
    'award', 'conclusion', 'dispatch', 'publication', 'issue', 'none'.

    'publication' is when TED published the notice; 'issue' is when the
    buyer wrote it. They are days apart, so they are reported separately
    rather than both being called publication.
    """
    if award.award_date:
        return award.award_date, "award"
    if getattr(award, "conclusion_date", None):
        return award.conclusion_date, "conclusion"
    if getattr(notice, "dispatch_date", None):
        return notice.dispatch_date, "dispatch"
    if getattr(notice, "publication_date", None):
        return notice.publication_date, "publication"
    if notice.issue_date:
        return notice.issue_date, "issue"
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


_LEGACY_OJS_ID = re.compile(r"^\d{4}/S \d")


def notice_key(notice) -> str:
    """The per-notice key (``ted_notice_id``): the eForms notice UUID, or
    the publication number for a legacy TED_EXPORT notice, whose
    ``notice_id`` is the human OJS reference (``2022/S 081-217109``) that
    nothing else keys on."""
    if notice.publication_number and _LEGACY_OJS_ID.match(notice.notice_id or ""):
        return notice.publication_number
    return notice.notice_id


def derive_contract_key(
    *,
    procedure_id: str | None,
    notice_kind: str,
    modifies_publication_number: str | None,
    ted_publication_number: str | None,
    ted_notice_id: str,
) -> str:
    """The contract identity a notice groups under.

    eForms: the procedure id (BT-04), which the award and every
    modification of one procedure share. Legacy: a modification groups
    under the publication number of the award it modifies, an award
    under its own. The notice id is the last resort and means the XML
    carried no identity at all. The sink may still move a modification
    onto its root award's entity when the back-link resolves to an award
    keyed differently (an eForms modification of a pre-eForms award).
    """
    if procedure_id:
        return procedure_id
    if notice_kind == "modification" and modifies_publication_number:
        return modifies_publication_number
    return ted_publication_number or ted_notice_id


def _version_num(raw) -> int | None:
    """``"01"`` (XML), ``1`` (search API) and ``None`` compare as one
    thing: an integer version, or None when the notice has none."""
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


_INGEST_STATE = """
OPTIONAL MATCH (n:Notice {ted_notice_id: $nid})
OPTIONAL MATCH (c:Contract {ted_notice_id: $nid})
WITH coalesce(n, c) AS x
RETURN x IS NOT NULL AS present,
       x.notice_version AS version,
       x.procedure_id IS NOT NULL AS has_procedure_id,
       x.ted_publication_number IS NOT NULL AS has_publication_number,
       n.title_lang IS NOT NULL AS has_title_lang
"""


def _should_ingest(
    session, ted_notice_id: str, notice_version, identity: str | None,
    *, title_lang: str | None = None,
) -> bool:
    """Idempotency gate, one indexed lookup per notice.

    A notice is re-ingested when it is not in the graph, when the graph
    copy predates identity stamping (``identity`` names the property the
    XML can give — ``procedure_id`` for eForms, ``ted_publication_number``
    for legacy — and the node lacks it), when a versioned notice's graph
    copy carries no version (written by the pre-single-path loader), or
    when the incoming notice is a NEWER version than the stored one. Same
    or older version: skip. So a re-run of an archive is O(1) per notice
    this loader already wrote, and the identity repair is simply a
    re-run — no ``rescore`` needed, and a restarted range Job resumes.

    ``title_lang`` works the same way: when the XML gives the title's
    language and the graph's :Notice lacks it, the notice is ingested
    again, so backfilling it is a plain re-run. Only the :Notice counts:
    the :Contract may carry a title_lang the translation enrichment
    guessed, and a guess must not stop the notice's own statement landing.
    The pre-download caller cannot know it yet and passes nothing."""
    row = session.run(_INGEST_STATE, nid=ted_notice_id).single()
    if row is None or not row["present"]:
        return True
    if identity == "procedure_id" and not row["has_procedure_id"]:
        return True
    if identity == "ted_publication_number" and not row["has_publication_number"]:
        return True
    if title_lang is not None and not row["has_title_lang"]:
        return True
    stored, incoming = _version_num(row["version"]), _version_num(notice_version)
    if incoming is not None and stored is None:
        # A versioned (eForms) notice whose graph copy carries no version
        # was written before the single ingest path: its stamps may be
        # right by accident (the search-API path stamped procedure_id)
        # but its back-link is in the wrong field. Re-stamp it. This is
        # what makes a range Job resumable and the repair a plain re-run:
        # notices this loader wrote carry the version and are skipped.
        return True
    return stored is not None and incoming is not None and incoming > stored


def _identity_property(procedure_id, publication_number) -> str | None:
    if procedure_id:
        return "procedure_id"
    if publication_number:
        return "ted_publication_number"
    return None


@dataclass
class IngestContext:  # pylint: disable=too-many-instance-attributes
    """What every notice of one run shares: the matcher (reads Neo4j for
    stable gmr_ids), the currency service, the per-run parent dedup
    sets, whether already-loaded notices are re-emitted, and the
    cleaning stage's injected data + report accumulator."""
    matcher: TedMatcher
    currency_svc: CurrencyClient | None = None
    seen_authorities: set = field(default_factory=set)
    seen_companies: set = field(default_factory=set)
    rescore: bool = False
    lookups: Lookups | None = None
    report: CleaningReport | None = None
    # A dry run writes nothing anywhere: no review-queue rows either.
    dry_run: bool = False
    # UpsertFrameworkAgreement is emitted only once the sink that
    # understands it is deployed (EMIT_FRAMEWORK_AGREEMENTS).
    emit_frameworks: bool = False


_TRANSIENT_RETRIES = 4
_TRANSIENT_BACKOFF_S = 2.0


def ingest_notice(notice, session, log: EventLog, ctx: IngestContext) -> str:
    """The one ingest path. Returns ``"emitted"`` or ``"skipped"``.

    Identity is whatever the parser read off the XML; the caller only
    found the notice. Each notice is its own ``log.batch`` (TED's own
    publish boundary), so committed rows are visible immediately and a
    pod restart loses at most one notice.

    Neo4j's transient errors (BookmarkTimeout under a busy sink, a
    deadlock, a leader switch) are retried with backoff rather than
    ending a multi-day range Job on one notice: the reads are the
    idempotency gate and the matcher, and the per-notice batch rolls
    back on the way out, so a retry starts clean."""
    ted_notice_id = notice_key(notice)
    for attempt in range(1, _TRANSIENT_RETRIES + 1):
        try:
            if not ctx.rescore and not _should_ingest(
                session, ted_notice_id, notice.notice_version,
                _identity_property(notice.procedure_id, notice.publication_number),
                title_lang=notice.title_lang,
            ):
                return "skipped"
            with log.batch(uuid.uuid4(), producer="load_ted_contracts") as emit:
                _emit_notice(
                    notice, emit, ctx.matcher,
                    ctx.seen_authorities, ctx.seen_companies, ctx.currency_svc,
                    lookups=ctx.lookups, report=ctx.report,
                    dry_run=ctx.dry_run, emit_frameworks=ctx.emit_frameworks,
                )
            return "emitted"
        except TransientError as exc:
            if attempt == _TRANSIENT_RETRIES:
                raise
            wait = _TRANSIENT_BACKOFF_S * 2 ** (attempt - 1)
            logger.warning(
                "notice %s: transient Neo4j error (%s), retry %d/%d in %.0fs",
                ted_notice_id, exc.code, attempt, _TRANSIENT_RETRIES - 1, wait,
            )
            time.sleep(wait)
    return "emitted"  # unreachable; keeps the type checker honest


def load_contracts(  # pylint: disable=too-many-arguments,too-many-locals
    driver,
    log: EventLog,
    archive_path: Path,
    currency_svc: CurrencyClient | None = None,
    rescore: bool = False,
    *,
    lookups: Lookups | None = None,
    report: CleaningReport | None = None,
    dry_run: bool = False,
    emit_frameworks: bool = False,
):
    """Discover notices in a TED monthly archive and ingest each one.

    Awards AND modifications: a modification found in an archive keys
    its contract exactly as one found through the search API would,
    because both go through :func:`ingest_notice`. ``rescore`` bypasses
    the already-loaded skip so every notice is re-emitted (the sinks
    MERGE, so values overwrite in place). A ``dry_run`` implies it: the
    point of a dry run is to report what the cleaning stage would do to
    notices that are already in the graph."""
    total = 0
    skipped = 0
    errors = 0
    t0 = time.time()
    with driver.session() as session:
        ctx = IngestContext(
            matcher=TedMatcher(session), currency_svc=currency_svc,
            rescore=rescore or dry_run, lookups=lookups, report=report,
            dry_run=dry_run, emit_frameworks=emit_frameworks,
        )
        for notice in awards_and_modifications(stream_notices(archive_path)):
            try:
                outcome = ingest_notice(notice, session, log, ctx)
            except Exception:  # pylint: disable=broad-except
                # One bad notice must not end a range Job that took days;
                # it is logged with its id, counted, and the next run
                # (no skip stamp for it) picks it up again.
                errors += 1
                logger.exception("FAILED notice %s", notice_key(notice))
                continue
            if outcome == "skipped":
                skipped += 1
                continue
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
        "Done: %d notices emitted, %d skipped, %d failed in %.0fs",
        total, skipped, errors, elapsed,
    )
    logger.info("Match quality: %s", ctx.matcher.stats.summary())
    return {"total": total, "skipped": skipped, "errors": errors,
            "elapsed_s": elapsed, "match_stats": ctx.matcher.stats.summary()}


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


@dataclass
class _Candidate:
    """One award whose contractor the notice names — before the
    cleaning stage has said whether it becomes an entity and before
    the matcher has assigned it an id."""

    award: Any
    contractor: Any
    is_winner: bool

    @property
    def org_id(self) -> str:
        return self.award.contractor_org_id

    @property
    def role(self) -> str:
        return "winner" if self.is_winner else "named_tenderer"


def _candidate_suppliers(notice) -> list[_Candidate]:
    """Every award with a named contractor — winners AND named tenderers
    (the losing bidders some eForms dialects publish)."""
    return [
        _Candidate(
            award=award, contractor=notice.organizations[award.contractor_org_id],
            is_winner=bool(getattr(award, "is_winner", True)),
        )
        for award in notice.awards
        if notice.organizations.get(award.contractor_org_id)
    ]


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


def _resolve_suppliers(candidates, matcher, emit, seen_companies,
                       identifiers) -> list:
    """Resolve every supplier the cleaning stage let through — winners
    AND named tenderers — via the consolidator: same tiers, same
    confidence capture, and the same create-if-not-found minting as the
    historical single-winner path. ``identifiers`` (org id -> canonical
    VAT or None) comes from the stage: the C3 rule prefixes a bare
    national id with the organisation's country BEFORE the matcher sees
    it, which is where identity comes from. Emits UpsertCompany once per
    first-seen gmr_id (per-run dedup; the sink would MERGE either way)."""
    resolved: list[_ResolvedSupplier] = []
    for candidate in candidates:
        contractor = candidate.contractor
        raw_vat = identifiers.get(candidate.org_id)
        match = matcher.match_company(
            contractor.name, contractor.country, raw_vat,
        )
        if match.gmr_id not in seen_companies:
            emit.upsert(
                "UpsertCompany",
                iri=f"{_IRI_BASE}Company/{match.gmr_id}",
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
            award=candidate.award, contractor=contractor, match=match,
            is_winner=candidate.is_winner,
        ))
    return resolved


def _withheld_payload(candidates, withheld: dict) -> "list[dict] | None":
    """``suppliers_withheld``: one item per withheld organisation, its
    raw text kept as the pointer it is. Never in ``parties`` and never
    the top-level company — the neo4j sink stubs a :Company for any
    company_gmr_id it has not seen an UpsertCompany for, which would
    re-create exactly the junk node the rule refused."""
    items: list[dict] = []
    seen: set = set()
    for candidate in candidates:
        reason = withheld.get(candidate.org_id)
        if reason is None or candidate.org_id in seen:
            continue
        seen.add(candidate.org_id)
        is_winner = any(c.is_winner for c in candidates if c.org_id == candidate.org_id)
        items.append(builders.withheld_supplier(
            name_raw=candidate.contractor.name or "", reason=reason,
            role="winner" if is_winner else "named_tenderer",
            org_id=candidate.org_id,
        ))
    return items or None


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


def _winner_awards(entries) -> dict:
    """One award per winning (lot, tendering party). A named tenderer's
    ``Award.value`` is its losing BID amount — never contract money — so
    non-winners are left out. Consortium members of one tendering party
    all restate the SAME undivided tender value, so a party's award is
    kept once, never once per member."""
    party_awards: dict = {}
    for entry in entries:
        if not entry.is_winner:
            continue
        key = (
            getattr(entry.award, "lot_id", None),
            getattr(entry.award, "tendering_party_id", None)
            or entry.award.contractor_org_id,
        )
        party_awards.setdefault(key, entry.award)
    return party_awards


def _winner_value_inputs(notice, party_awards: dict):
    """The three raw money signals from the winner awards
    (``_winner_awards``). The notice-level TotalAmount is only
    attributable when there is exactly one winning party; with several
    winners it is an aggregate we cannot split. Returns
    ``(estimate_raw, total_raw, payable_raw)``."""
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


_IRI_BASE = "http://data.fontem.eu/id/"
_CURRENCY_CODE = re.compile(r"[A-Z]{3}")
FRAMEWORKS_ENV = "EMIT_FRAMEWORK_AGREEMENTS"


def _framework_notice_key(notice) -> tuple[str | None, str | None]:
    """The framework grouping key this notice publishes, and which
    element it was read off.

    OPT-100 (``efac:NoticeResult/efac:SettledContract/
    cac:NoticeDocumentReference/cbc:ID``, "Framework Notice
    Identifier") is carried IDENTICALLY by the framework-establishing
    award notice and by every call-off under it, which is the whole
    reason it can group them. eforms-parser 0.13 extracts it, falls back
    to BT-125 on a framework procedure, and normalises the two published
    forms: the zero-padded publication number (``00536632-2024``, which
    TED's own framework-notice-id index returns 0 results for while
    ``536632-2024`` returns 2) and the eForms UUID's ``-NN`` version
    suffix, which differs between two notices of the same framework.

    It is a KEY, not a pointer: ~80% of the time it names a call for
    competition, and this platform ingests award and modification
    notices only, so the referenced notice resolves to a :Contract we
    hold about 13.6% of the time. Nothing downstream may assume
    otherwise, and nothing may read an establishment-then-call-off ORDER
    out of it — both ends carry the same value.

    ``text()`` rather than a bare attribute read so a wheel older than
    0.13, or a test double, degrades to None instead of putting a
    MagicMock on the event.
    """
    key = text(getattr(notice, "framework_notice_id", None))
    if not key:
        return None, None
    return key, text(getattr(notice, "framework_notice_id_source", None))


def frameworks_enabled() -> bool:
    """``EMIT_FRAMEWORK_AGREEMENTS`` — default off. The neo4j sink that
    understands UpsertFrameworkAgreement must be deployed first: a sink
    skips an unknown event type and advances its offset, so an early
    emit would be lost, not queued."""
    return os.environ.get(FRAMEWORKS_ENV, "false").strip().lower() in (
        "1", "true", "yes",
    )


def _emit_authority(buyer, matcher, emit, seen_authorities) -> str:
    """Match the buyer and emit its UpsertAuthority once per run.

    Authority dedup is per-archive (the caller threads
    ``seen_authorities`` through). Once an authority has appeared in
    the archive we skip the redundant UpsertAuthority — the sink would
    MERGE either way, but eliding it keeps the event log compact and
    replay-faster. The legal id travels raw as ``national_id``; the
    stage's normalised form is counted but ``match_authority`` does not
    read it and the schema has no field for it."""
    buyer_legal_value = buyer.legal_id.value if buyer.legal_id else None
    authority_id = matcher.match_authority(
        buyer.name, buyer.country, buyer_legal_value,
    )
    if authority_id not in seen_authorities:
        emit.upsert(
            "UpsertAuthority",
            iri=f"{_IRI_BASE}Authority/{authority_id}",
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
    return authority_id


@dataclass(frozen=True)
class _Identity:  # pylint: disable=too-many-instance-attributes
    """Everything about the notice's identity, all from the XML: there
    is no override, no search-record stamp and no out-of-band lookup,
    so the event is the same whichever way the notice was discovered."""

    ted_notice_id: str
    ted_publication_number: str | None
    procedure_id: str | None
    publication_date: str | None
    notice_type: str | None
    notice_kind: str
    modifies_publication_number: str | None
    modifies_notice_id: str | None
    contract_key: str


def _identity(notice) -> _Identity:
    """Procedure id (BT-04), the notice version (BT-757) and, on a
    modification, the back-link (BT-1501) in whichever of its two forms
    the buyer wrote it. contract_key is derived here so the producer
    stamp and the sink's native Contract/Notice model agree on what a
    contract is."""
    # eForms UUID, or the publication number for a legacy notice (whose
    # notice_id is the human OJS reference). The publication number
    # itself is on the XML (efbc:NoticePublicationID / NO_DOC_OJS); a
    # notice TED has not published yet simply has none.
    ted_notice_id = notice_key(notice)
    ted_publication_number = notice.publication_number
    notice_type = getattr(notice, "notice_type", None)
    notice_kind = (
        "modification" if notice_type == _MODIFICATION_NOTICE_TYPE
        else "award"
    )
    modifies_publication_number = modifies_notice_id = None
    if notice_kind == "modification":
        modifies_publication_number = notice.modifies_publication_number
        modifies_notice_id = notice.modifies_notice_id
    return _Identity(
        ted_notice_id=ted_notice_id,
        ted_publication_number=ted_publication_number,
        procedure_id=notice.procedure_id,
        # When TED published the notice: efbc:PublicationDate off the
        # XML, then cbc:IssueDate as a last resort. issue_date is when
        # the buyer wrote it, which runs 1-3 days earlier and is the
        # wrong answer for anything ordering or windowing by recency.
        publication_date=(
            _as_day(getattr(notice, "publication_date", None))
            or _as_day(notice.issue_date)
        ),
        notice_type=notice_type,
        notice_kind=notice_kind,
        modifies_publication_number=modifies_publication_number,
        modifies_notice_id=modifies_notice_id,
        contract_key=derive_contract_key(
            procedure_id=notice.procedure_id,
            notice_kind=notice_kind,
            modifies_publication_number=modifies_publication_number,
            ted_publication_number=ted_publication_number,
            ted_notice_id=ted_notice_id,
        ),
    )


@dataclass
class _Money:  # pylint: disable=too-many-instance-attributes
    """The notice's money signals after FX and before any rule."""

    declared_currency: str | None
    resolved_currency: str | None
    rate_date_obj: _date | None
    estimate_eur: float | None
    total_eur: float | None
    total_original: float | None
    payable_eur: float | None
    payable_original: float | None
    before_eur: float | None
    before_original: float | None
    party_awards: dict

    def value_facts(self, country: str | None, cpv) -> ValueFacts:
        return ValueFacts(
            estimate_eur=self.estimate_eur, total_eur=self.total_eur,
            payable_eur=self.payable_eur, total_original=self.total_original,
            payable_original=self.payable_original, country=country,
            cpv=text(cpv),
        )


def _resolve_currency(currency_svc, declared_currency, buyer_country,
                      effective_date, issue_date):
    """Resolve the notice currency + FX-rate date once; the estimate,
    the awarded total, and the payable all convert at the same rate.
    Returns ``(resolved_currency, rate_date_obj)``."""
    rate_date_obj = None
    if currency_svc:
        rate_date_str = effective_date or issue_date
        try:
            rate_date_obj = (
                _date.fromisoformat(rate_date_str[:10])
                if rate_date_str else None
            )
        except (ValueError, TypeError):
            rate_date_obj = None
        resolved_currency, _inferred = currency_svc.resolve_currency(
            declared_currency,
            country=(buyer_country or "").upper(),
            on=rate_date_obj,
        )
    else:
        resolved_currency = declared_currency
    # TED uses non-currency placeholders (UNPUBLISHED, OP_DATPRO) in the
    # currency field when no value is published. Null them so the contract
    # carries no spurious currency (and no value to convert downstream).
    if resolved_currency and not _CURRENCY_CODE.fullmatch(resolved_currency):
        resolved_currency = None
    return resolved_currency, rate_date_obj


def _convert_money(  # pylint: disable=too-many-locals
    notice, buyer, candidates, context_award, currency_svc,
) -> _Money:
    """The three money signals — WINNER-only, one undivided value per
    winning tendering party (see ``_winner_awards``) — plus the
    pre-modification total, all converted at one rate. Loser bids and
    consortium-member restatements never reach the contract value."""
    declared_currency = context_award.currency or notice.currency
    effective_date, _date_source = _coalesce_date(context_award, notice)
    resolved_currency, rate_date_obj = _resolve_currency(
        currency_svc, declared_currency, buyer.country, effective_date,
        notice.issue_date,
    )
    party_awards = _winner_awards(candidates)
    estimate_raw, total_raw, payable_raw = _winner_value_inputs(
        notice, party_awards,
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
    return _Money(
        declared_currency=declared_currency,
        resolved_currency=resolved_currency, rate_date_obj=rate_date_obj,
        estimate_eur=est_eur, total_eur=tot_eur, total_original=tot_orig,
        payable_eur=pay_eur, payable_original=pay_orig,
        before_eur=before_eur, before_original=before_orig,
        party_awards=party_awards,
    )


@dataclass
class _Valued:  # pylint: disable=too-many-instance-attributes
    """What the contract event carries about money once the scorer and
    the cleaning stage's value decision have both spoken."""

    value_eur: float | None
    value_original: float | None
    currency: str | None
    estimate_eur: float | None
    payable_eur: float | None
    before_eur: float | None
    before_original: float | None
    score: Any
    scale_tier: str | None = None
    quarantined: bool = False
    quarantine_reason: str | None = None

    def withhold(self, *, keep_estimate: bool) -> None:
        """Strip the monetary fields the way every quarantine does."""
        self.value_eur = self.value_original = None
        self.currency = None
        if not keep_estimate:
            self.estimate_eur = self.payable_eur = None
            self.before_eur = self.before_original = None


def _chosen_value(score, tot_eur, tot_orig, pay_eur, pay_orig):
    """The chosen value (TotalAmount-preferred) in both currencies. A
    no-awarded-value contract must not carry a (stray, often
    sign-flipped) monetary value."""
    if score.flag.value == "no_awarded_value":
        return None, None
    if score.chosen_field == "total":
        return tot_eur, tot_orig
    if score.chosen_field == "payable":
        return pay_eur, pay_orig
    return None, None


def _scored(money: _Money, decision, ted_notice_id: str) -> _Valued:
    """Apply a rescale decision (the milli-euro tiers, unchanged) BEFORE
    scoring so the confidence scorer sees the corrected magnitudes,
    then score. Low-confidence values are kept but flagged."""
    est_eur, tot_eur, pay_eur = money.estimate_eur, money.total_eur, money.payable_eur
    tot_orig, pay_orig = money.total_original, money.payable_original
    scale_tier = None
    if isinstance(decision, Rescale):
        logger.warning(
            "TED notice %s: monetary fields rescaled x%g (%s): %s",
            ted_notice_id, decision.factor, decision.reason, decision.detail,
        )
        corrected = decision.corrected
        est_eur, tot_eur, pay_eur = (
            corrected.estimate_eur, corrected.total_eur, corrected.payable_eur,
        )
        tot_orig, pay_orig = corrected.total_original, corrected.payable_original
        scale_tier = decision.reason
    score = score_contract_value(
        estimate_eur=est_eur, total_eur=tot_eur, payable_eur=pay_eur,
        total_original=tot_orig, payable_original=pay_orig,
    )
    value_eur, value_original = _chosen_value(
        score, tot_eur, tot_orig, pay_eur, pay_orig,
    )
    if score.is_low_confidence:
        logger.warning(
            "TED notice %s value EUR %.3g flagged '%s' "
            "(confidence %.2f) — stored but excluded from default "
            "aggregates: %s",
            ted_notice_id, value_eur or 0.0,
            score.flag.value, score.confidence, score.reason,
        )
    return _Valued(
        value_eur=value_eur, value_original=value_original,
        currency=money.resolved_currency, estimate_eur=est_eur,
        payable_eur=pay_eur, before_eur=money.before_eur,
        before_original=money.before_original, score=score,
        scale_tier=scale_tier,
    )


def _quarantine(  # pylint: disable=too-many-arguments
    valued: _Valued, *, ted_notice_id: str, reason: str, detail: str,
    review: bool, keep_estimate: bool = False,
) -> None:
    """Withhold the value: the event carries no monetary fields (the
    sinks also clear any previously rendered ones) plus the quarantine
    marker + reason. The claim goes to events.value_review for a human
    decision when ``review``. The claimed numbers are never lost: event
    log + queue snapshot hold them."""
    if review:
        value_review_queue.enqueue_default(
            ted_notice_id=ted_notice_id,
            reason=reason,
            claimed_value_eur=valued.value_eur,
            claimed_value_original=valued.value_original,
            claimed_currency=valued.currency,
            claimed_estimated_eur=valued.estimate_eur,
            claimed_payable_eur=valued.payable_eur,
            detail=detail,
        )
    valued.withhold(keep_estimate=keep_estimate)
    valued.quarantined = True
    valued.quarantine_reason = reason


def _apply_value_decision(money: _Money, decision, ted_notice_id: str,
                          *, review: bool) -> _Valued:
    """Score, then quarantine on either ground.

    The scorer's own quarantine tiers come first (a value that fails
    hard sanity checks; a published 0 is auto-withheld — non-disclosure
    in costume — and keeps the independent estimate). Otherwise a
    cleaning-stage ``Quarantine`` (the peer outlier rule) withholds the
    value exactly the same way, with the candidate corrections in the
    review note. ``review`` is False on a dry run: nothing is written."""
    valued = _scored(money, decision, ted_notice_id)
    score = valued.score
    if score.quarantined:
        _quarantine(
            valued, ted_notice_id=ted_notice_id, reason=score.flag.value,
            detail=score.reason, review=review and score.needs_review,
            keep_estimate=score.flag.value == "zero_value",
        )
    elif isinstance(decision, Quarantine) and valued.value_eur is not None:
        logger.warning(
            "TED notice %s: value EUR %.3g withheld (%s): %s",
            ted_notice_id, valued.value_eur, decision.reason, decision.detail,
        )
        _quarantine(
            valued, ted_notice_id=ted_notice_id, reason=decision.reason,
            detail=decision.detail, review=review,
        )
    return valued


def _value_raw(notice, party_awards: dict, chosen_field: str | None,
               declared_currency) -> str | None:
    """The published amount text, verbatim, with the declared currency
    when it is a real code: the notice total where the total is the
    stored value (single winning party), else the winner awards' own
    ``PayableAmount`` text (several parties: joined with ' + '), else
    whatever total text there is. '0' stays '0'."""
    total_raw = text(getattr(notice, "total_value_raw", None))
    award_raws = [
        raw for raw in (text(getattr(a, "value_raw", None))
                        for a in party_awards.values())
        if raw
    ]
    if chosen_field == "total" and total_raw:
        raw = total_raw
    elif award_raws:
        raw = " + ".join(award_raws)
    else:
        raw = total_raw
    if raw is None:
        return None
    currency = (declared_currency
                if isinstance(declared_currency, str)
                and _CURRENCY_CODE.fullmatch(declared_currency) else None)
    return f"{raw} {currency}" if currency else raw


@dataclass(frozen=True)
class _FrameworkTerms:
    """The establishing notice's framework terms in EUR."""

    max_eur: float | None = None
    max_original: float | None = None
    max_currency: str | None = None
    reestimated_eur: float | None = None
    duration_months: int | None = None
    max_operators: int | None = None


def _currency_or(declared, fallback: str | None) -> str | None:
    if declared and _CURRENCY_CODE.fullmatch(declared):
        return declared
    return fallback


def _framework_terms(notice, money: _Money, currency_svc) -> _FrameworkTerms | None:
    """None unless the notice sets up a framework agreement. The ceiling
    (BT-118 / BT-709) and re-estimate (BT-660) convert with the currency
    client like every other amount, at the notice's rate date."""
    if notice.is_framework is not True:
        return None
    max_currency = _currency_or(
        text(getattr(notice, "framework_max_value_currency", None)),
        money.resolved_currency,
    )
    max_original, max_eur = _amount_to_eur(
        currency_svc, max_currency, money.rate_date_obj,
        number(getattr(notice, "framework_max_value", None)),
    )
    re_currency = _currency_or(
        text(getattr(notice, "framework_reestimated_value_currency", None)),
        money.resolved_currency,
    )
    _re_original, re_eur = _amount_to_eur(
        currency_svc, re_currency, money.rate_date_obj,
        number(getattr(notice, "framework_reestimated_value", None)),
    )
    return _FrameworkTerms(
        max_eur=max_eur, max_original=max_original,
        max_currency=max_currency if max_original is not None else None,
        reestimated_eur=re_eur,
        duration_months=integer(getattr(notice, "framework_duration_months", None)),
        max_operators=integer(getattr(notice, "framework_max_operators", None)),
    )


def _emit_framework_agreement(  # pylint: disable=too-many-arguments,too-many-locals
    emit, notice, ident: _Identity, terms: _FrameworkTerms, *,
    framework_id: str, authority_id: str, country: str | None,
    candidates, resolved,
) -> None:
    """The framework as a first-class entity, keyed by the OPT-100
    grouping key every notice of the framework carries, so the node and
    the ``UpsertContract.framework_id`` values pointing at it live in one
    key space. It was keyed on ``ident.contract_key`` (BT-04
    ContractFolderID), which is a different value space entirely: on
    notice 761784-2024 the folder is ``2f3cab57-...`` while OPT-100 is
    ``536632-2024``, so a contract referencing one could never meet a
    node keyed by the other.

    ``buyer_authority_id`` and ``establishing_notice_id`` are provenance
    — the notice that happened to carry the terms — not a claim that it
    established the framework; the data cannot tell an establishment
    from a call-off.

    Suppliers are the resolved (non-withheld) winners with lot/rank;
    ``supplier_count`` is what the notice published, withheld ones
    included."""
    suppliers: list[dict] = []
    seen: set = set()
    for entry in resolved:
        if not entry.is_winner:
            continue
        lot = text(getattr(entry.award, "lot_id", None))
        key = (str(entry.match.gmr_id), lot)
        if key in seen:
            continue
        seen.add(key)
        suppliers.append(builders.framework_supplier(
            company_gmr_id=str(entry.match.gmr_id), lot=lot,
            rank=integer(getattr(entry.award, "rank", None)),
        ))
    supplier_count = len({c.org_id for c in candidates if c.is_winner})
    lots = notice.lots if isinstance(notice.lots, list) else []
    payload = builders.upsert_framework_agreement(
        framework_id=framework_id,
        buyer_authority_id=authority_id,
        establishing_notice_id=ident.ted_notice_id,
        country=country,
        ceiling_eur=terms.max_eur,
        ceiling_currency=terms.max_currency,
        ceiling_original=terms.max_original,
        reestimated_value_eur=terms.reestimated_eur,
        duration_months=terms.duration_months,
        cpv=text(notice.cpv_main),
        lot_count=len(lots) or None,
        supplier_count=supplier_count or None,
        title=notice.title or None,
        suppliers=suppliers or None,
    )
    emit.upsert(
        "UpsertFrameworkAgreement",
        iri=f"{_IRI_BASE}FrameworkAgreement/{framework_id}",
        domain="contract",
        payload=payload,
    )


def _emit_notice(  # pylint: disable=too-many-locals,too-many-arguments,too-many-positional-arguments
    notice, emit, matcher, seen_authorities, seen_companies, currency_svc,
    *,
    lookups: Lookups | None = None,
    report: CleaningReport | None = None,
    dry_run: bool = False,
    emit_frameworks: bool = False,
):
    """Process a single TED notice within an already-open
    ``log.batch(...)`` context — the caller owns commit semantics.

    Order: parse (done) -> money + FX -> the cleaning stage -> identity
    (the matcher, fed the stage's normalised identifiers) -> payloads.

    Per-call side effects:
      * Mutates ``seen_authorities`` / ``seen_companies`` for
        per-run dedup of repeated parents within a single archive.
      * Calls ``emit.upsert`` zero or more times: UpsertAuthority /
        UpsertCompany for first-seen parents, then ONE UpsertContract
        per notice (notice-grain), then — behind ``emit_frameworks`` —
        an UpsertFrameworkAgreement for an establishing notice. Every
        named supplier the stage lets through is resolved and listed in
        ``parties[]``; a withheld one is carried in
        ``suppliers_withheld`` only. The top-level company/match fields
        stay the primary winner's.
      * Feeds ``report`` (per-rule accounting) when given.
    """
    buyer = notice.buyer()
    if not buyer:
        return
    # The buyer goes out before the awards are looked at (as it always
    # has): a notice whose awards name no known contractor still
    # describes its authority.
    authority_id = _emit_authority(buyer, matcher, emit, seen_authorities)
    # Every award whose contractor the notice names; nothing is matched
    # yet, and nothing has been cleaned yet.
    candidates = _candidate_suppliers(notice)
    if not candidates:
        return
    ident = _identity(notice)
    # Country of the contracting authority (the buyer / acquirer).
    # Cascaded onto the Contract because TED contracts are
    # jurisdictionally grouped by the procuring entity, not the vendor.
    buyer_country = LocationService.to_alpha3(buyer.country)
    # The primary winner's award (first is_winner) drives the
    # date/currency context — withheld or not: the award's value, dates
    # and currency are kept even when its supplier is not.
    context_award = next(
        (c.award for c in candidates if c.is_winner), candidates[0].award,
    )
    money = _convert_money(notice, buyer, candidates, context_award, currency_svc)

    # ── The cleaning stage (pure; see src/etl/cleaning/README.md) ──
    facts = facts_from_notice(
        notice, value=money.value_facts(buyer_country, notice.cpv_main),
        context_award=context_award,
    )
    cleaning = run_stage(facts, lookups)
    if report is not None:
        report.record(
            notice_id=ident.ted_notice_id, country=buyer_country,
            year=(ident.publication_date or "")[:4] or None, result=cleaning,
        )

    # Every supplier the stage let through — winners AND named tenderers
    # — resolves through the consolidator; unmatched ones mint a new
    # node (create-if-not-found), exactly like the old single-winner
    # path. A withheld supplier is never matched: no entity for it.
    kept = [c for c in candidates if c.org_id not in cleaning.withheld]
    resolved = _resolve_suppliers(
        kept, matcher, emit, seen_companies, cleaning.identifiers,
    )
    # The primary winner drives the top-level company/match fields
    # (backward compat). A notice that names tenderers but resolves no
    # winner still emits — its named tenderers matter — but carries no
    # company attribution; so does one whose winner was withheld.
    primary = next((e for e in resolved if e.is_winner), None)
    if primary is not None:
        match_tier, match_confidence = _match_provenance(primary.match)
        company_gmr_id = str(primary.match.gmr_id)
        match_layer = primary.match.layer
    else:
        match_tier = match_confidence = None
        company_gmr_id = match_layer = None

    valued = _apply_value_decision(
        money, cleaning.value, ident.ted_notice_id, review=not dry_run,
    )
    terms = _framework_terms(notice, money, currency_svc)
    framework_id, framework_id_source = _framework_notice_key(notice)

    contract_payload = builders.upsert_contract(
        ted_notice_id=ident.ted_notice_id,
        ted_publication_number=ident.ted_publication_number,
        title=notice.title or None,
        # The notice's own statement of its title's language (ISO 639-1);
        # translation starts from it. The verbatim code is in `facts`.
        title_lang=notice.title_lang,
        authority_id=authority_id,
        company_gmr_id=company_gmr_id,
        match_tier=match_tier,
        match_confidence=match_confidence,
        match_layer=match_layer,
        publication_date=ident.publication_date,
        value_eur=valued.value_eur,
        value_currency=valued.currency,
        value_original=valued.value_original,
        value_before_eur=valued.before_eur,
        value_before_original=valued.before_original,
        estimated_value_eur=valued.estimate_eur,
        value_payable_eur=valued.payable_eur,
        value_confidence=valued.score.confidence,
        value_confidence_consistency=valued.score.consistency,
        value_confidence_plausibility=valued.score.plausibility,
        value_quality_flag=valued.score.flag.value,
        value_low_confidence=valued.score.is_low_confidence,
        value_payable_discrepancy=valued.score.has_payable_discrepancy,
        value_quarantined=valued.quarantined or None,
        value_quarantine_reason=valued.quarantine_reason,
        value_scale_corrected=valued.scale_tier,
        # Cleaning-stage fields: the raw text stays, every rule that
        # fired is named, and a withheld supplier is here and nowhere
        # else on the event.
        value_raw=_value_raw(
            notice, money.party_awards, valued.score.chosen_field,
            money.declared_currency,
        ),
        cleaning_rules=list(cleaning.rules_fired),
        suppliers_withheld=_withheld_payload(candidates, cleaning.withheld),
        # Watermark / provenance fields, verbatim, so the gateway census
        # can run from the graph.
        award_date_raw=facts.award_date_raw,
        tender_result_award_date_raw=facts.tender_result_award_date_raw,
        tender_reference=facts.tender_reference,
        notice_language=facts.notice_language,
        eforms_sdk=text(getattr(notice, "customization_id", None)),
        cpv=notice.cpv_main,
        nuts=notice.nuts,
        language=getattr(notice, "language", None),
        country=buyer_country,
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
        # `is_framework` says the notice belongs to a framework
        # procedure (its lot's ContractingSystemTypeCode starts `fa`), NOT
        # that it established one: 344 of 351 sampled CALL-OFFS carry it
        # too, and nothing in the data separates the two.
        is_framework=notice.is_framework,
        # The grouping key rides on EVERY notice that publishes one,
        # establishment and call-off alike — that symmetry is the whole
        # mechanism by which they find each other. The terms below are
        # whatever THIS notice published, which may be either end.
        framework_id=framework_id,
        framework_id_source=framework_id_source,
        framework_max_value_eur=terms.max_eur if terms else None,
        framework_reestimated_value_eur=terms.reestimated_eur if terms else None,
        framework_duration_months=terms.duration_months if terms else None,
        framework_max_operators=terms.max_operators if terms else None,
        eu_funded=notice.eu_funded,
        funding_programme=notice.funding_programme,
        procedure_id=ident.procedure_id,
        legacy_procedure_id=notice.legacy_procedure_id,
        notice_type=ident.notice_type,
        notice_version=notice.notice_version,
        notice_kind=ident.notice_kind,
        modifies_publication_number=ident.modifies_publication_number,
        modifies_notice_id=ident.modifies_notice_id,
        contract_key=ident.contract_key,
        parties=_build_parties(resolved),
    )
    emit.upsert(
        "UpsertContract",
        # IRI keyed by the stable UUID (notice_id) so it doesn't
        # change once TED assigns / revises a publication-number
        # after first ingest.
        iri=f"{_IRI_BASE}Contract/{ident.ted_notice_id}",
        domain="contract",
        payload=contract_payload,
    )
    # No OPT-100 key, no agreement node: the old fallback minted one on
    # ident.contract_key, a key no contract could ever reference, so the
    # node was unreachable by construction. A notice with framework terms
    # but no key still carries them on its own contract.
    if terms is not None and framework_id and emit_frameworks:
        _emit_framework_agreement(
            emit, notice, ident, terms, framework_id=framework_id,
            authority_id=authority_id, country=buyer_country,
            candidates=candidates, resolved=resolved,
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


def load_contracts_incremental(  # pylint: disable=too-many-locals,too-many-arguments,too-many-positional-arguments,too-many-statements,too-many-branches
    driver,
    log: EventLog,
    since: _date,
    until: _date,
    currency_svc: CurrencyClient | None = None,
    notice_types: tuple[str, ...] = ted_search.NOTICE_TYPES,
    watermark_id: str = _WATERMARK_ID,
    *,
    lookups: Lookups | None = None,
    report: CleaningReport | None = None,
    dry_run: bool = False,
    emit_frameworks: bool = False,
):
    """Discover award + modification notices through TED's search API,
    one calendar day at a time from ``since`` to ``until`` inclusive,
    and ingest each one.

    The search API is DISCOVERY ONLY: its record tells us a notice
    exists, where its XML is, and — so we can skip without downloading —
    its identifier and version. Nothing from the record lands on the
    event; :func:`ingest_notice` reads identity from the XML like it
    does for an archive. The watermark advances one day at a time, only
    after that day fully loads, so an interrupted run resumes from the
    next unfinished day. A day that errors on *every* notice (e.g. API
    outage) stops the run without advancing, so we never silently skip
    a date.

    A ``dry_run`` reads TED and the graph but writes nothing: no raw
    XML, no watermark, and every notice is re-processed (the skip gate
    is bypassed) so the cleaning report covers what is already loaded.
    """
    totals = {"days": 0, "emitted": 0, "skipped": 0, "modifications": 0, "errors": 0}
    raw_store = None if dry_run else TedRawStore.from_env()
    http = httpx.Client(timeout=ted_search.SEARCH_TIMEOUT)
    try:
        with driver.session() as session:
            ctx = IngestContext(
                matcher=TedMatcher(session), currency_svc=currency_svc,
                rescore=dry_run, lookups=lookups, report=report,
                dry_run=dry_run, emit_frameworks=emit_frameworks,
            )
            day = since
            while day <= until:
                ymd = day.strftime("%Y%m%d")
                iso = day.isoformat()
                t0 = time.time()
                d_emit = d_skip = d_mod = d_err = 0
                for rec in ted_search.search_day(ymd, notice_types, client=http):
                    # Pre-download skip, on the same rule ingest_notice
                    # applies after parsing: legacy notices have no
                    # notice-identifier and key on the publication number.
                    nid = (
                        rec.get("notice-identifier")
                        or rec.get("publication-number")
                    )
                    if nid and not dry_run and not _should_ingest(
                        session, nid, rec.get("notice-version"),
                        _identity_property(
                            rec.get("procedure-identifier"),
                            rec.get("publication-number"),
                        ),
                    ):
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
                        if ingest_notice(notice, session, log, ctx) == "skipped":
                            d_skip += 1
                            continue
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
                if not dry_run:
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
    logger.info("Match quality: %s", ctx.matcher.stats.summary())
    totals["match_stats"] = ctx.matcher.stats.summary()
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
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run everything but write nothing: events go to an in-memory "
             "log (validated against their schemas, never stored); no "
             "watermark, raw-XML or review-queue writes; notices already "
             "in the graph are re-processed. Needs no EVENTS_DATABASE_URL. "
             "Prints the cleaning report as JSON unless --report is given.",
    )
    parser.add_argument(
        "--report",
        help="Write the cleaning report (JSON: totals, per rule id, per "
             "country, per publication year, first 20 examples per rule) "
             "to this path. Works with and without --dry-run.",
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
    if args.dry_run:
        logger.info("DRY RUN: no events, watermark, raw XML or review "
                    "rows will be written")
        log = DryRunEventLog()
    else:
        log = EventLog.from_env()
    # The cleaning stage's injected data + per-run accounting. Peer
    # stats come from dq.peer_value_stats when the table exists; the
    # loader function logs once when the rule is inactive.
    stage = {
        "lookups": Lookups(peer_stats=load_peer_stats_from_env()),
        "report": CleaningReport(),
        "dry_run": args.dry_run,
        "emit_frameworks": frameworks_enabled(),
    }

    try:
        # CPV bootstrap: emits UpsertTaxonomyCode events. Idempotent;
        # re-runs are MERGE on (system='cpv', code) at the sink.
        if not args.dry_run:
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
                    rescore=args.rescore, **stage,
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
                    watermark_id=args.watermark_id, **stage,
                )
                # Nothing runs after the load: the neo4j sink links each
                # modification to its award and maintains the contract
                # entity on write (link_ted_modifications and
                # collapse_modifications are retired).
    finally:
        currency_svc.close()
        log.close()
        driver.close()
    logger.info("%s", stage["report"].summary_line())
    if isinstance(log, DryRunEventLog):
        logger.info("DRY RUN: %d events in %d batches would have been "
                    "written: %s", log.total, log.batches, dict(log.counts))
    _write_report(stage["report"], args.report, to_stdout=args.dry_run)


def _write_report(report: CleaningReport, path: str | None,
                  *, to_stdout: bool) -> None:
    """The cleaning report as JSON: to ``path`` when given, else to
    stdout on a dry run, else nowhere (the summary line is logged)."""
    if not path and not to_stdout:
        return
    payload = json.dumps(report.as_dict(), indent=2, ensure_ascii=False)
    if path:
        Path(path).write_text(payload + "\n", encoding="utf-8")
        logger.info("cleaning report written to %s", path)
    else:
        print(payload)


if __name__ == "__main__":
    main()
