"""Post-ETL consolidator hooks + entity-resolution client.

Two distinct uses for the gmr-consolidator service from ETL code:

1. `notify_consolidator()` — fire-and-forget /consolidate/batch after a
   write. Best-effort, never breaks the ETL.
2. `resolve_entity()` — synchronous /resolve lookup BEFORE writing a
   REPRESENTS / SANCTIONED / SAME_AS edge. Returns a tier-tagged
   match, or candidates, or no_match. Also best-effort: if the
   service is down, callers receive None and skip the edge — silent
   miss > silent corruption.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Iterable, Literal

import httpx

# Namespace-relative DNS: the bare `fontem-consolidator` name is
# completed by the in-cluster resolver to
# `fontem-consolidator.<pod-namespace>.svc.cluster.local`. That gives
# us one default that works for every env (fontem-shared / fontem-prod
# / fontem-staging) without per-env overrides. The old default was
# `gmr-consolidator.gmr.svc.cluster.local` from the pre-rename layout;
# in the current cluster that name doesn't resolve, so /resolve calls
# silently degraded to "no_match" and entity de-duplication stopped
# working in every ETL that uses the resolver (sanctions, TED,
# lobbying, FIRDS).
CONSOLIDATOR_URL = os.environ.get(
    "CONSOLIDATOR_URL",
    "http://fontem-consolidator:8000",
)
# 30s default — the consolidator pod runs both the rule engine and the
# resolver, and the rule engine is sometimes saturated by long sweeps.
# A 5s ceiling caused every /resolve call from the sanctions ETL to
# time out during the first manual trigger; 30s tolerates the wait
# without making transient consolidator hiccups fatal to the ETL.
HOOK_TIMEOUT = float(os.environ.get("CONSOLIDATOR_HOOK_TIMEOUT", "30"))

log = logging.getLogger(__name__)


def notify_consolidator(entity_type: str, ids: Iterable[str]) -> None:
    """Fire a /consolidate/batch call. Swallow any error."""
    ids = [i for i in ids if i]  # drop None / empty
    if not ids:
        return
    if not CONSOLIDATOR_URL:
        return
    try:
        with httpx.Client(timeout=HOOK_TIMEOUT) as client:
            r = client.post(
                f"{CONSOLIDATOR_URL}/consolidate/batch",
                json={"entity_type": entity_type, "ids": ids},
            )
            r.raise_for_status()
            log.info(
                "consolidator: notified %s ids=%d → %s", entity_type, len(ids), r.status_code
            )
    except Exception as exc:  # pylint: disable=broad-exception-caught  # pragma: no cover
        # Notify is best-effort: a 5xx from the consolidator or a network
        # blip must never fail the ETL — the consolidator periodically
        # re-scans, so the worst case is a delayed merge.
        log.warning("consolidator: notify failed for %s: %s", entity_type, exc)


# ─────────────────────────────────────────────────────────────────────
# /resolve client
# ─────────────────────────────────────────────────────────────────────

ResolveTier = Literal["lei", "vat", "cik", "name_country", "fuzzy"]
ResolveHint = Literal["matched", "ambiguous", "no_match"]


@dataclass
class ResolveMatch:
    gmr_id: str
    name: str
    country: str | None
    lei: str | None
    tier: ResolveTier
    confidence: float


@dataclass
class ResolveResult:
    hint: ResolveHint
    match: ResolveMatch | None
    candidates: list[ResolveMatch]
    normalised_country: str | None


# Each kwarg here is a distinct identifier the resolver accepts (lei/vat/cik/
# name+country). Bundling into a single dict erases the static type hints.
def resolve_entity(  # pylint: disable=too-many-arguments
    *,
    entity_type: Literal["Company", "Authority"],
    name: str | None = None,
    country: str | None = None,
    lei: str | None = None,
    vat: str | None = None,
    cik: str | None = None,
    client: httpx.Client | None = None,
) -> ResolveResult | None:
    """POST /resolve. Returns the resolver result, or None on transport
    failure. Caller decides how to act on each hint."""
    if not CONSOLIDATOR_URL:
        return None
    body = {
        "entity_type": entity_type,
        "name": name, "country": country,
        "lei": lei, "vat": vat, "cik": cik,
    }
    body = {k: v for k, v in body.items() if v is not None or k == "entity_type"}
    try:
        owns = client is None
        if owns:
            client = httpx.Client(timeout=HOOK_TIMEOUT)
        r = client.post(f"{CONSOLIDATOR_URL}/resolve", json=body)
        if owns:
            client.close()
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # pylint: disable=broad-exception-caught  # pragma: no cover
        # /resolve is best-effort; the ETL has its own deterministic fallback
        # path (LEI-only, then name+country). A consolidator outage must not
        # break a sync.
        log.warning("consolidator: /resolve failed: %s", exc)
        return None
    return _parse_resolve_response(data)


def _parse_resolve_response(data: dict) -> ResolveResult:
    return ResolveResult(
        hint=data.get("hint", "no_match"),
        match=_parse_match(data.get("match")),
        candidates=[_parse_match(c) for c in data.get("candidates") or [] if c],
        normalised_country=data.get("normalised_country"),
    )


def _parse_match(d: dict | None) -> ResolveMatch | None:
    if not d:
        return None
    return ResolveMatch(
        gmr_id=d["gmr_id"], name=d["name"],
        country=d.get("country"), lei=d.get("lei"),
        tier=d["tier"], confidence=float(d["confidence"]),
    )
