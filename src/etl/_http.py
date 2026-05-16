"""Shared HTTP request hygiene for the ETL loaders.

Most upstreams either require or strongly prefer a User-Agent that
carries a deliverable contact address — SEC EDGAR's rate-control policy
asks for it explicitly, and the EU Transparency Register / FSF / FIRDS
endpoints sit behind WAFs that silently drop default-UA bots. Sending a
single, recognisable header across every loader gives the upstream a
way to reach us if our traffic misbehaves and keeps us out of the
"unattributed scraper" bucket the WAFs filter on.

Use ``HTTP_HEADERS`` directly when a loader doesn't otherwise set
headers. Use ``with_headers(extra)`` to merge per-loader additions
(API keys, content-type, conditional GET fields, …) without losing
the UA + contact.

The ``_http_retry`` helpers default to these headers; callers only
need to supply ``headers=`` when they want to override or extend.
"""
from __future__ import annotations

from typing import Mapping

CONTACT_EMAIL = "team@fontem.eu"

HTTP_HEADERS: dict[str, str] = {
    "User-Agent": f"Fontem-ETL/1.0 (+https://fontem.eu; {CONTACT_EMAIL})",
    "From": CONTACT_EMAIL,
    "Accept": "*/*",
}


def with_headers(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a fresh headers dict combining ``HTTP_HEADERS`` with
    per-call additions. ``extra`` wins on key collision so callers can
    override Accept, set Authorization, swap the UA for a probe, etc."""
    merged = dict(HTTP_HEADERS)
    if extra:
        merged.update(extra)
    return merged
