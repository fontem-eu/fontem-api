"""Retry helper for upstream HTTP calls in the ETLs.

ETLs hit a mix of upstream services that occasionally misbehave in two
related ways:

  - TLS handshakes that hang past curl/httpx's read timeout. We've seen
    this against the ESMA Solr endpoint (Azure App Gateway warmup) and
    against the EU sanctions endpoint when their WAF rate-limits.
  - Transient 5xx responses from the upstream's own backend even after
    TLS works (Solr backend returning 500/502, TED returning 504 during
    publication windows).

Before this helper every loader did a single ``httpx.get()`` followed by
``sys.exit(1)`` on any HTTPError, which meant a single transient hiccup
killed the entire scheduled run. The retry budget below is deliberately
modest (3 attempts, 5s/15s/45s base backoff with jitter) so that an
upstream that's truly down still fails the run within a few minutes
rather than holding a CronJob slot for an hour.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_RETRY_STATUSES: frozenset[int] = frozenset({500, 502, 503, 504})


def _backoff(attempt: int, base_delay: float) -> float:
    return random.uniform(0, base_delay * (2 ** (attempt - 1)))


def get_with_retry(
    url: str,
    *,
    max_attempts: int = 3,
    base_delay: float = 5.0,
    retry_statuses: frozenset[int] = DEFAULT_RETRY_STATUSES,
    **httpx_kwargs: Any,
) -> httpx.Response:
    """GET ``url`` with retry on transient transport + 5xx errors.

    Retries on ``httpx.TransportError`` subclasses (ConnectError,
    ConnectTimeout, ReadTimeout, RemoteProtocolError, ...) and on any
    response whose status code is in ``retry_statuses``. Other
    HTTP errors (4xx, unexpected exceptions) propagate immediately —
    those are caller bugs or auth issues, not transient.

    Backoff is exponential with full jitter:
        sleep_n = random_uniform(0, base_delay * 2**n)

    so attempt 1 → up to base_delay, attempt 2 → up to 2x, attempt 3
    → up to 4x. With base_delay=5 that's max 5s + 10s + 20s = 35s worst
    case between attempts.
    """
    last_exc: BaseException | None = None
    last_resp: httpx.Response | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = httpx.get(url, **httpx_kwargs)
        except httpx.TransportError as exc:
            last_exc = exc
            last_resp = None
            log.warning(
                "GET %s attempt %d/%d failed (%s: %s); retrying",
                url, attempt, max_attempts, type(exc).__name__, exc,
            )
        else:
            if resp.status_code not in retry_statuses:
                return resp
            last_resp = resp
            last_exc = None
            log.warning(
                "GET %s attempt %d/%d returned %d; retrying",
                url, attempt, max_attempts, resp.status_code,
            )

        if attempt < max_attempts:
            time.sleep(_backoff(attempt, base_delay))

    if last_exc is not None:
        raise last_exc
    assert last_resp is not None
    return last_resp


def call_with_retry(
    fn,  # type: ignore[no-untyped-def]
    *,
    max_attempts: int = 3,
    base_delay: float = 5.0,
    retry_on: tuple[type[BaseException], ...] = (httpx.TransportError,),
):
    """Call ``fn()`` up to ``max_attempts`` times, retrying on transport
    errors. Use this for streaming downloads where ``get_with_retry``'s
    full-response model doesn't fit. ``fn`` is expected to handle its
    own cleanup (e.g. deleting partial output) on each failure."""
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except retry_on as exc:
            last_exc = exc
            log.warning(
                "%s attempt %d/%d failed (%s: %s); retrying",
                getattr(fn, "__name__", "call"),
                attempt, max_attempts, type(exc).__name__, exc,
            )
            if attempt < max_attempts:
                time.sleep(_backoff(attempt, base_delay))
    assert last_exc is not None
    raise last_exc
