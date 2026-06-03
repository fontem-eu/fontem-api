"""Fetch the current truthy RDF for a Wikidata entity.

Wikidata exposes a per-entity dump at
``https://www.wikidata.org/wiki/Special:EntityData/{id}.ttl?flavor=dump``.
``flavor=dump`` is critical: it returns only the *truthy* statements
(best-rank claims), the same shape we bulk-loaded from the
``latest-truthy.nt`` file. Without that parameter we'd get every claim
at every rank plus reification, which is roughly 8× the volume and
mixes the right answers with deprecated and historical ones.

This module is intentionally I/O-only — it does not filter or write
to Virtuoso. Those happen downstream. Callers feed each returned
graph through ``apply_language_filter`` and then to the Virtuoso
writer.

Failure modes:

  * 404 / 410: entity exists in our buffer but Wikidata says it's
    gone. We do *not* trust this as a deletion signal (that's why the
    relay tombstones from the SSE event payload, not from here);
    return ``FetchResult.NOT_FOUND`` so the worker can decide.
  * 301/302: Wikidata redirects, typically from a merged entity to
    its survivor. We follow exactly one hop and return the
    redirected-to id alongside the graph so the caller can record an
    `owl:sameAs` and update its bookkeeping.
  * 5xx, timeouts, conn-reset: retry with exponential backoff, up to
    ``MAX_RETRIES``. After that, raise — the worker should leave the
    dirty_entities row in place and let the next batch try.
"""
from __future__ import annotations

import dataclasses
import enum
import logging
import time

import httpx
from rdflib import Graph, URIRef

logger = logging.getLogger(__name__)

# Same UA as the relay so Wikimedia ops can see both sides of our
# traffic under one identity, with a deliverable contact.
USER_AGENT = "Fontem-WikidataConsumer/1.0 (+https://fontem.eu; team@fontem.eu)"

# Wikimedia is generous with reads but rate-limits aggressive clients
# fairly hard — bursting 1000 entities at 10-way parallelism with 20
# in-flight connections triggered ~90% 429 in our first prod run.
# Cut it to 5 in-flight + 3 consumer workers (default) for a sustainable
# request rate; the 429 path now has proper Retry-After/backoff so a
# brief overshoot is recoverable rather than a silent drop.
MAX_INFLIGHT = 5

# Retries on 5xx and network errors. 5 attempts with a 2^n second
# backoff means ~30s of grace on the worst transient.
MAX_RETRIES = 5
BASE_BACKOFF_S = 1.0

# Per-request socket timeouts. Wikidata's entity-data endpoint
# typically responds in <500ms but the larger entities (Q5, Q35120,
# meta-classes) can take several seconds.
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=10.0)


class FetchOutcome(enum.Enum):
    OK = "ok"
    REDIRECT = "redirect"
    NOT_FOUND = "not_found"


@dataclasses.dataclass(frozen=True)
class FetchResult:
    """Outcome of one entity fetch. ``graph`` is None for NOT_FOUND.
    ``redirected_to`` is the survivor entity id when ``outcome ==
    REDIRECT`` — the caller writes an ``owl:sameAs`` and re-fetches
    the survivor. Otherwise None."""

    outcome: FetchOutcome
    entity_id: str
    graph: Graph | None
    redirected_to: str | None


def _entity_url(entity_id: str) -> str:
    # flavor=simple gives us the truthy-equivalent (direct wdt:Pxx
    # claims + labels/descriptions/aliases in all languages + sitelink
    # cards), no reification nodes — matches the shape of the
    # latest-truthy.nt bulk-load. flavor=dump (the alternative) returns
    # ~4× the volume by including statement/reference/value nodes for
    # qualifiers + references, which we don't query and which would
    # accumulate forever because our subject-scoped DELETE can't reach
    # them on re-fetch.
    return f"https://www.wikidata.org/wiki/Special:EntityData/{entity_id}.ttl?flavor=simple"


def _parse_ttl(content: bytes) -> Graph:
    graph = Graph()
    graph.parse(data=content, format="turtle")
    return graph


# Both constants below are RDF IRIs (W3C OWL vocabulary + Wikidata
# entity namespace) — not network endpoints. The schemes are spec-
# defined; swapping to https would break every comparison.
_OWL_SAMEAS = URIRef("http://www.w3.org/2002/07/owl#sameAs")  # NOSONAR
_WIKIDATA_ENTITY_PREFIX = "http://www.wikidata.org/entity/"  # NOSONAR


def _extract_redirect_target(graph: Graph, source_id: str) -> str | None:
    """When Wikidata serves a redirect-target's RDF, the source
    entity is marked with ``owl:sameAs`` pointing to the survivor.
    We pull the target id out of that triple, fall back to None if
    the assertion isn't there (treat as a normal fetch).

    Uses graph.objects() rather than graph.query() because rdflib's
    SPARQL parser drags in pyparsing — and a CI build picked up a
    pyparsing version where the SPARQL grammar throws
    ``Param.postParse2() missing 1 required positional argument:
    'tokenList'`` on every parse. Single-hop lookups don't need a
    query engine; iterate the triples directly."""
    source_iri = URIRef(f"{_WIKIDATA_ENTITY_PREFIX}{source_id}")
    for target in graph.objects(source_iri, _OWL_SAMEAS):
        target_iri = str(target)
        if target_iri.startswith(_WIKIDATA_ENTITY_PREFIX):
            return target_iri.rsplit("/", 1)[-1]
    return None


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff in seconds for retry attempt N (0-indexed)."""
    return BASE_BACKOFF_S * (2 ** attempt)


def _retry_after_seconds(resp: httpx.Response, attempt: int) -> float:
    """Parse the 429 Retry-After header, falling back to exp backoff.

    Wikimedia's varnish layer sends the seconds form in practice; the
    HTTP-date form is also valid per RFC 9110 but unused here, so a
    parse failure falls through to backoff."""
    retry_after = resp.headers.get("Retry-After")
    if not retry_after:
        return _backoff_seconds(attempt)
    try:
        return float(retry_after)
    except ValueError:
        return _backoff_seconds(attempt)


def _follow_redirect(
    resp: httpx.Response, entity_id: str, client: httpx.Client,
) -> FetchResult:
    """Follow a 3xx the server returned for `entity_id` and turn it
    into a REDIRECT FetchResult. Raises httpx.HTTPError on transport
    failure so the outer retry loop sleeps + retries; returns a
    NOT_FOUND FetchResult on missing Location or non-200 target."""
    location = resp.headers.get("Location")
    if not location:
        logger.warning(
            "redirect from %s with no Location header; treating as not found",
            entity_id,
        )
        return FetchResult(FetchOutcome.NOT_FOUND, entity_id, None, None)
    follow = client.get(location, follow_redirects=False)
    if follow.status_code != 200:
        logger.warning(
            "redirect target %s for %s returned %d",
            location, entity_id, follow.status_code,
        )
        return FetchResult(FetchOutcome.NOT_FOUND, entity_id, None, None)
    graph = _parse_ttl(follow.content)
    target = _extract_redirect_target(graph, entity_id)
    return FetchResult(FetchOutcome.REDIRECT, entity_id, graph, target)


def _classify_response(  # pylint: disable=too-many-return-statements
    resp: httpx.Response, entity_id: str, client: httpx.Client, attempt: int,
) -> FetchResult | None:
    """Translate a single response into a FetchResult, or return None
    to signal "please retry". Raises httpx.HTTPError on the inner
    redirect follow-up so the outer loop catches + retries."""
    if resp.status_code in (404, 410):
        return FetchResult(FetchOutcome.NOT_FOUND, entity_id, None, None)
    if resp.status_code == 429:
        # We never treat 429 as "not found" — that would silently
        # lose the entity from our drain.
        sleep_for = _retry_after_seconds(resp, attempt)
        logger.warning("fetch %s rate-limited (429); sleeping %.1fs",
                       entity_id, sleep_for)
        time.sleep(sleep_for)
        return None
    if resp.status_code in (301, 302, 303, 307, 308):
        return _follow_redirect(resp, entity_id, client)
    if 500 <= resp.status_code < 600:
        sleep_for = _backoff_seconds(attempt)
        logger.warning("fetch %s got %d; sleeping %.1fs",
                       entity_id, resp.status_code, sleep_for)
        time.sleep(sleep_for)
        return None
    if resp.status_code != 200:
        # 4xx other than 404/410 (e.g. 403 — would be surprising).
        # Log + treat as not-found rather than retry forever.
        logger.warning("fetch %s got unexpected %d", entity_id, resp.status_code)
        return FetchResult(FetchOutcome.NOT_FOUND, entity_id, None, None)
    # 200 OK. The response may still encode a redirect via owl:sameAs
    # — Wikidata serves the survivor's RDF in response to a merged id.
    graph = _parse_ttl(resp.content)
    target = _extract_redirect_target(graph, entity_id)
    if target is not None and target != entity_id:
        return FetchResult(FetchOutcome.REDIRECT, entity_id, graph, target)
    return FetchResult(FetchOutcome.OK, entity_id, graph, None)


def fetch_truthy(entity_id: str, client: httpx.Client) -> FetchResult:
    """Fetch the current truthy RDF for one entity. The ``client`` is
    passed in so the caller can configure connection pooling +
    concurrency; we don't own it.

    Retries are internal; the caller sees a final outcome only."""
    url = _entity_url(entity_id)
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.get(url, follow_redirects=False)
            result = _classify_response(resp, entity_id, client, attempt)
        except httpx.HTTPError as exc:
            last_exc = exc
            sleep_for = _backoff_seconds(attempt)
            logger.warning("fetch %s attempt %d failed: %s; sleeping %.1fs",
                           entity_id, attempt + 1, exc, sleep_for)
            time.sleep(sleep_for)
            continue
        if result is not None:
            return result

    # Exhausted retries.
    raise RuntimeError(
        f"failed to fetch {entity_id} after {MAX_RETRIES} attempts: {last_exc!r}"
    )


def make_client() -> httpx.Client:
    """Construct a connection-pooled httpx Client preconfigured with
    our User-Agent, timeouts, and concurrency limits. Callers should
    use one per worker process and close it on exit."""
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "text/turtle"},
        timeout=DEFAULT_TIMEOUT,
        limits=httpx.Limits(max_connections=MAX_INFLIGHT,
                            max_keepalive_connections=MAX_INFLIGHT),
    )
