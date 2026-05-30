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
from rdflib import Graph

logger = logging.getLogger(__name__)

# Same UA as the relay so Wikimedia ops can see both sides of our
# traffic under one identity, with a deliverable contact.
USER_AGENT = "Fontem-WikidataConsumer/1.0 (+https://fontem.eu; team@fontem.eu)"

# Wikimedia is generous with reads but politely rate-limits aggressive
# clients. 5 in-flight requests at a time keeps us well under their
# soft cap (~50 conn/IP) and gives plenty of throughput for our
# expected ~10 entities/second.
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


def _extract_redirect_target(graph: Graph, source_id: str) -> str | None:
    """When Wikidata serves a redirect-target's RDF, the source
    entity is marked with ``owl:sameAs`` pointing to the survivor.
    We pull the target id out of that triple, fall back to None if
    the assertion isn't there (treat as a normal fetch)."""
    query = f"""
    PREFIX owl: <http://www.w3.org/2002/07/owl#>
    PREFIX wd:  <http://www.wikidata.org/entity/>
    SELECT ?target WHERE {{
        wd:{source_id} owl:sameAs ?target .
    }} LIMIT 1
    """
    for row in graph.query(query):
        target_iri = str(row.target)  # type: ignore[attr-defined]
        if target_iri.startswith("http://www.wikidata.org/entity/"):
            return target_iri.rsplit("/", 1)[-1]
    return None


def fetch_truthy(  # pylint: disable=too-many-return-statements
    entity_id: str, client: httpx.Client,
) -> FetchResult:
    """Fetch the current truthy RDF for one entity. The ``client`` is
    passed in so the caller can configure connection pooling +
    concurrency; we don't own it.

    Retries are internal; the caller sees a final outcome only."""
    url = _entity_url(entity_id)
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.get(url, follow_redirects=False)
        except httpx.HTTPError as exc:
            last_exc = exc
            sleep_for = BASE_BACKOFF_S * (2 ** attempt)
            logger.warning("fetch %s attempt %d failed: %s; sleeping %.1fs",
                           entity_id, attempt + 1, exc, sleep_for)
            time.sleep(sleep_for)
            continue

        if resp.status_code in (404, 410):
            return FetchResult(FetchOutcome.NOT_FOUND, entity_id, None, None)

        if resp.status_code in (301, 302, 303, 307, 308):
            # Wikidata redirects merged entities to their survivor.
            # We don't actually need to follow at the HTTP layer —
            # the redirected response usually contains an
            # owl:sameAs assertion we can use directly. But to be
            # robust we follow once and check.
            location = resp.headers.get("Location")
            if not location:
                logger.warning(
                    "redirect from %s with no Location header; treating as not found",
                    entity_id,
                )
                return FetchResult(FetchOutcome.NOT_FOUND, entity_id, None, None)
            try:
                follow = client.get(location, follow_redirects=False)
            except httpx.HTTPError as exc:
                last_exc = exc
                sleep_for = BASE_BACKOFF_S * (2 ** attempt)
                time.sleep(sleep_for)
                continue
            if follow.status_code != 200:
                logger.warning(
                    "redirect target %s for %s returned %d",
                    location, entity_id, follow.status_code,
                )
                return FetchResult(FetchOutcome.NOT_FOUND, entity_id, None, None)
            graph = _parse_ttl(follow.content)
            target = _extract_redirect_target(graph, entity_id)
            return FetchResult(FetchOutcome.REDIRECT, entity_id, graph, target)

        if 500 <= resp.status_code < 600:
            sleep_for = BASE_BACKOFF_S * (2 ** attempt)
            logger.warning("fetch %s got %d; sleeping %.1fs",
                           entity_id, resp.status_code, sleep_for)
            time.sleep(sleep_for)
            continue

        if resp.status_code != 200:
            # 4xx other than 404/410 (e.g. 403 — would be surprising).
            # Log + treat as not-found rather than retry forever.
            logger.warning("fetch %s got unexpected %d", entity_id, resp.status_code)
            return FetchResult(FetchOutcome.NOT_FOUND, entity_id, None, None)

        graph = _parse_ttl(resp.content)
        # The response may itself encode a redirect via owl:sameAs
        # even with a 200 — that happens when Wikidata serves the
        # survivor's RDF in response to the merged id.
        target = _extract_redirect_target(graph, entity_id)
        if target is not None and target != entity_id:
            return FetchResult(FetchOutcome.REDIRECT, entity_id, graph, target)
        return FetchResult(FetchOutcome.OK, entity_id, graph, None)

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
