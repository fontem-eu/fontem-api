"""Translate an eForms internal notice UUID to TED's publication-number.

We ingest eForms notices via XML, where the only ID we have at parse
time is the ``cbc:ID`` root identifier — a UUID. TED itself assigns a
human-readable ``publication-number`` (e.g. ``295342-2026``) at
publication time, and TED's public detail URL is keyed by that
publication-number, not by the eForms UUID:

    https://ted.europa.eu/en/notice/-/detail/<publication-number>

If the URL above is hit with the eForms UUID instead, TED's portal
returns HTTP 202 with an empty body (the JS-rendered page can't
resolve the notice from its API and gives up silently — the user
sees a blank page). That's the user-reported "broken link".

This service calls TED's v3 search API to translate UUID → publication
-number on demand, with a small in-process LRU cache so we don't
hammer TED on every click. The cache is intentionally process-local
(one cache per pod, no Redis): the data is immutable once a notice
is published, and the cost of a missed lookup is one extra HTTP call.
"""
from __future__ import annotations

import logging
from functools import lru_cache

import httpx

logger = logging.getLogger(__name__)

# TED's documented public search endpoint.
_TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"

# Anti-bot timeout: TED's search responds in <500ms when warm. 10s is
# generous and ensures a stuck call doesn't tie up a worker.
_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=10.0)

# Cache size — a single fontem-api pod sees a small set of distinct
# notice IDs even under sustained dashboard usage; 4096 is plenty.
_CACHE_SIZE = 4096


class TedLookupError(Exception):
    """Raised when TED's search API returns no match for the UUID."""


@lru_cache(maxsize=_CACHE_SIZE)
def resolve_publication_number(notice_uuid: str) -> str:
    """Look up the TED publication-number for an eForms UUID.

    Raises ``TedLookupError`` when TED has no record of the UUID
    (notice not published, withdrawn, or the ID is malformed). All
    HTTP transport errors propagate to the caller — the router maps
    them to a 502.

    The decorator-cached function is one source of truth so any
    repeated lookup is O(1) for the lifetime of the process. Tests
    that need a fresh lookup must call
    ``resolve_publication_number.cache_clear()`` between cases.
    """
    payload = {
        "query": f'notice-identifier="{notice_uuid}"',
        "fields": ["publication-number"],
        "limit": 1,
        "checkQuerySyntax": False,
    }
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(_TED_SEARCH_URL, json=payload)
        resp.raise_for_status()
        body = resp.json()

    notices = body.get("notices") or []
    if not notices:
        logger.info("TED search returned no match for %s", notice_uuid)
        raise TedLookupError(f"TED has no published notice for {notice_uuid}")
    pub_num = notices[0].get("publication-number")
    if not pub_num:
        logger.warning(
            "TED search match for %s lacks publication-number: %s",
            notice_uuid, notices[0],
        )
        raise TedLookupError(
            f"TED match for {notice_uuid} has no publication-number",
        )
    return str(pub_num)


def detail_url_for(publication_number: str) -> str:
    """Build the canonical TED notice detail URL."""
    return f"https://ted.europa.eu/en/notice/-/detail/{publication_number}"
