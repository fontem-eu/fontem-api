"""TED v3 search-API client for incremental notice loading.

Replaces the monthly-package download for the daily cron. The old path
re-fetched a >1 GB month tarball every run and 404-ed on the in-progress
month (TED only publishes a month's package after month-end), so the
cron could never succeed mid-month. Instead we query the search API by
``publication-date``, page through the results, and download each
notice's XML individually.

Bonus: the search response hands us the ``publication-number`` and
``procedure-identifier`` for free, so the loader no longer pays the
per-notice UUID->pub-num lookup, and we get the ``procedure-identifier``
+ ``modification-previous-notice-identifier`` needed to link contract
modifications back to the original award.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterator

import httpx

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
SEARCH_TIMEOUT = _TIMEOUT  # public alias for callers

# Awards + contract modifications — the two families we ingest. Mirrors
# eforms.filters._AWARD_TYPES | _MODIFICATION_TYPES (kept in sync by a test).
NOTICE_TYPES: tuple[str, ...] = (
    "can-standard", "can-social", "can-desg", "can-tran", "can-modif",
)

# Per-notice fields: the XML link to download + the identifiers we stamp
# (publication-number, procedure-identifier) + the modification back-link.
_FIELDS = [
    "notice-identifier", "publication-number", "publication-date",
    "notice-type", "procedure-identifier",
    "modification-previous-notice-identifier", "links",
]

_PAGE_SIZE = 100

# TED rate-limits sustained querying: a multi-year modification backfill
# reliably trips a 429 after hours of day-by-day requests. Retry transient
# statuses with backoff so a single rate-limit doesn't kill the whole run
# (a hard daily quota still surfaces after the retries — the loader's
# watermark lets a killed backfill resume where it left off).
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 6
_BACKOFF_CAP = 300.0


def _backoff_seconds(resp: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retrying: honor ``Retry-After`` when TED
    sends one, else exponential backoff (5, 10, 20, 40, 80 s), capped."""
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            return min(float(retry_after), _BACKOFF_CAP)
        except ValueError:
            pass
    return min(5.0 * (2 ** attempt), _BACKOFF_CAP)


def _send_with_retry(
    client: httpx.Client, method: str, url: str, **kwargs
) -> httpx.Response:
    """Issue a request, retrying on 429/5xx with backoff. A raised 429
    would otherwise abort a long backfill mid-flight."""
    last = None
    for attempt in range(_MAX_ATTEMPTS):
        resp = client.request(method, url, **kwargs)
        if resp.status_code not in _RETRY_STATUSES:
            resp.raise_for_status()
            return resp
        last = resp
        if attempt < _MAX_ATTEMPTS - 1:
            wait = _backoff_seconds(resp, attempt)
            logger.warning(
                "TED %s -> %d (attempt %d/%d); backing off %.0fs",
                method, resp.status_code, attempt + 1, _MAX_ATTEMPTS, wait,
            )
            time.sleep(wait)
    last.raise_for_status()  # retries exhausted — surface the last error
    return last


def _day_query(day: str, notice_types: tuple[str, ...]) -> str:
    """Expert query: award/modification notices published on ``day`` (YYYYMMDD)."""
    types = " OR ".join(f'notice-type="{t}"' for t in notice_types)
    return f"publication-date>={day} AND publication-date<={day} AND ({types})"


def search_day(
    day: str,
    notice_types: tuple[str, ...] = NOTICE_TYPES,
    client: httpx.Client | None = None,
) -> Iterator[dict]:
    """Yield every award/modification search record published on ``day``
    (YYYYMMDD), paging through the result set."""
    own = client is None
    client = client or httpx.Client(timeout=_TIMEOUT)
    try:
        query = _day_query(day, notice_types)
        page, seen, total = 1, 0, None
        while True:
            resp = _send_with_retry(client, "POST", SEARCH_URL, json={
                "query": query, "fields": _FIELDS,
                "limit": _PAGE_SIZE, "page": page,
                "paginationMode": "PAGE_NUMBER",
            })
            body = resp.json()
            notices = body.get("notices", [])
            if total is None:
                total = body.get("totalNoticeCount", 0)
                logger.info("TED search %s: %d award/modification notices", day, total)
            yield from notices
            seen += len(notices)
            if not notices or seen >= total:
                break
            page += 1
    finally:
        if own:
            client.close()


def xml_url(record: dict) -> str | None:
    """The multilingual XML download URL for a search record, or None."""
    return ((record.get("links") or {}).get("xml") or {}).get("MUL")


def fetch_xml(url: str, client: httpx.Client | None = None) -> bytes:
    """Download one notice's XML."""
    own = client is None
    client = client or httpx.Client(timeout=_TIMEOUT)
    try:
        resp = _send_with_retry(client, "GET", url, follow_redirects=True)
        return resp.content
    finally:
        if own:
            client.close()


def modifies_publication_number(record: dict) -> str | None:
    """The original award's publication-number this notice modifies, if any.
    The API returns a list; we take the first (modifications reference one
    prior notice in practice)."""
    vals = record.get("modification-previous-notice-identifier")
    if isinstance(vals, list) and vals:
        return str(vals[0])
    return str(vals) if vals else None
