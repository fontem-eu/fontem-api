"""Filter Wikidata truthy RDF + apply to Virtuoso.

Two concerns separated:

  * ``filter_graph`` — keep only triples that describe the requested
    entity directly. Two rules:

      - Subject must equal the entity URI. The Wikidata
        ``flavor=simple`` response includes sitelink cards (subject
        is a wikipedia page URL) and a metadata block (subject is the
        EntityData/<id> dataset URL); both are dropped here.
      - Literals with a language tag outside EU_LANGUAGES are dropped.
        Untagged literals (numbers, dates, IRIs) are kept.

  * ``write_entity`` — apply the filtered graph to Virtuoso. For
    small entities (≤ ``SPARQL_CHUNK_TRIPLES``) we issue one combined
    DELETE+INSERT-DATA UPDATE so the swap is atomic. For larger
    entities — a "popular" Q-id can carry hundreds to thousands of
    triples — Virtuoso refuses the SPARQL with
    ``SP031: SPARQL: Internal error: The length of generated SQL
    text has exceeded 10000 lines of code``. We avoid that by
    chunking: one initial UPDATE does the DELETE plus the first
    chunk's INSERT atomically, then subsequent UPDATEs append the
    remaining chunks. Per-entity atomicity is lost across chunks
    but RDF is set-semantic so the only externally-visible effect
    is a brief window where the entity has fewer-than-final
    triples. On a mid-chunk failure the entity stays dirty and the
    next consumer pass retries from scratch.

The named graph is hard-coded to ``https://fontem.eu/graph/wikidata``,
matching the bulk-load. Don't parameterise — drifting from the
bulk-load graph means the worker silently writes to a separate slice
that none of our SPARQL endpoints query.
"""
from __future__ import annotations

import logging
import os

import httpx
from rdflib import Graph, Literal

from src.relay.eu_languages import EU_LANGUAGES

logger = logging.getLogger(__name__)

WIKIDATA_GRAPH = "https://fontem.eu/graph/wikidata"
WIKIDATA_ENTITY_PREFIX = "http://www.wikidata.org/entity/"
GEO_WKT_DATATYPE = "http://www.opengis.net/ont/geosparql#wktLiteral"

# Max triples per INSERT DATA call. Each triple expands to a handful
# of SQL lines in Virtuoso; 500 stays comfortably under the SP031
# 10k-line ceiling even for triples with long literal values.
SPARQL_CHUNK_TRIPLES = int(os.environ.get("WIKIDATA_SPARQL_CHUNK", "500"))

# Virtuoso's /sparql-auth endpoint silently prepends
# ``define sql:big-data-const 0`` before our UPDATE — that variant
# of the inline-constant path tries to resolve large literal/IRI
# hashes against pre-existing RDF_OBJ rows, and entities whose
# previous-write history left stale hash-cache entries blow up with
# SR580 ("RDF box refers to row with RO_ID = X of table RDF_OBJ,
# but no such row in the table"). Setting it back to 1 forces the
# fresh-insertion path that doesn't consult the hash cache. The
# endpoint's prepend goes first; ours lands after; Virtuoso honors
# the last define for any given directive.
_BIG_DATA_CONST_OVERRIDE = "define sql:big-data-const 1\n"


def filter_graph(graph: Graph, entity_id: str) -> Graph:
    """Return a new graph containing only triples that describe
    ``entity_id`` directly, with non-EU-language literals stripped.
    The input is not mutated.

    Subject-scoped: our SPARQL UPDATE's DELETE clause only reaches
    triples whose subject is the entity. If we admitted triples with
    other subjects (sitelinks, statement-reification nodes, dataset
    metadata) they would accumulate forever on re-fetch because the
    DELETE clause couldn't see them. So we drop them at the filter."""
    entity_uri = f"{WIKIDATA_ENTITY_PREFIX}{entity_id}"
    out = Graph()
    for prefix, ns in graph.namespaces():
        out.bind(prefix, ns)
    for subj, pred, obj in graph:
        if str(subj) != entity_uri:
            continue
        if isinstance(obj, Literal):
            lang = obj.language
            if lang is not None and lang.lower() not in EU_LANGUAGES:
                continue
            # Wikidata serialises extraterrestrial coordinates as
            # ``<http://www.wikidata.org/entity/Q111> Point(lon lat)``
            # (Q111=Mars, Q405=Moon, Q308=Mercury, ...) — Earth coords
            # have no globe prefix because Earth is the default. Virtuoso's
            # GeoSPARQL parser only accepts pure WKT for Earth CRS and
            # rejects the prefixed form with ``RDFGE: RDF box with a
            # geometry RDF type and a non-geometry content`` → entity
            # gets stuck in dirty_entities forever. Drop these triples;
            # we have no use for off-world coordinates in the EU-scoped
            # graph anyway.
            if str(obj.datatype) == GEO_WKT_DATATYPE \
                    and str(obj).startswith("<"):
                continue
        out.add((subj, pred, obj))
    return out


def _entity_uri(entity_id: str) -> str:
    return f"{WIKIDATA_ENTITY_PREFIX}{entity_id}"


def _serialise_nt(triples) -> str:
    """N-Triples serialise the provided iterable of (s, p, o). Builds
    a fresh Graph because rdflib's serializer needs one."""
    tmp = Graph()
    for triple in triples:
        tmp.add(triple)
    return tmp.serialize(format="nt").strip()


def _chunk_triples(graph: Graph, chunk_size: int) -> list[list]:
    """Split the graph's triples into chunks of at most ``chunk_size``
    each. Returns a list of triple-lists so the caller can iterate
    without needing the Graph machinery again."""
    triples = list(graph)
    return [triples[i:i + chunk_size]
            for i in range(0, len(triples), chunk_size)]


def _sparql_replace_with_first_chunk(entity_id: str, first_chunk) -> str:
    """Atomic DELETE + INSERT-DATA(first chunk) — runs as one
    transaction so the swap of old → new is visible all at once for
    the slice of triples that fits in the first chunk."""
    entity_iri = _entity_uri(entity_id)
    nt_body = _serialise_nt(first_chunk)
    return (
        f"{_BIG_DATA_CONST_OVERRIDE}"
        f"WITH <{WIKIDATA_GRAPH}>\n"
        f"DELETE {{ <{entity_iri}> ?p ?o }}\n"
        f"WHERE  {{ <{entity_iri}> ?p ?o }} ;\n"
        f"INSERT DATA {{ GRAPH <{WIKIDATA_GRAPH}> {{\n{nt_body}\n}} }}"
    )


def _sparql_insert_chunk(chunk) -> str:
    """Subsequent INSERT-DATA-only UPDATEs for chunks 2..N."""
    nt_body = _serialise_nt(chunk)
    return (
        f"{_BIG_DATA_CONST_OVERRIDE}"
        f"INSERT DATA {{ GRAPH <{WIKIDATA_GRAPH}> {{\n{nt_body}\n}} }}"
    )


def _sparql_delete_only(entity_id: str) -> str:
    """Tombstone path: DELETE every triple for the entity, no INSERT.
    Also the "the entity has zero triples after filtering" path
    inside ``write_entity``."""
    entity_iri = _entity_uri(entity_id)
    return (
        f"{_BIG_DATA_CONST_OVERRIDE}"
        f"WITH <{WIKIDATA_GRAPH}>\n"
        f"DELETE {{ <{entity_iri}> ?p ?o }}\n"
        f"WHERE  {{ <{entity_iri}> ?p ?o }}"
    )


def _post_update(
    client: httpx.Client, sparql_update_url: str, update: str,
    entity_id: str, auth: tuple[str, str] | None,
) -> None:
    """POST a single SPARQL UPDATE to Virtuoso. Raise on non-2xx so
    the caller's optimistic-delete doesn't fire for this entity and
    it stays in dirty_entities for retry."""
    resp = client.post(
        sparql_update_url,
        data={"query": update},
        auth=httpx.DigestAuth(*auth) if auth else None,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Virtuoso UPDATE for {entity_id} failed {resp.status_code}: "
            f"{resp.text[:300]}"
        )


def write_entity(
    entity_id: str, filtered: Graph,
    client: httpx.Client, sparql_update_url: str,
    auth: tuple[str, str] | None = None,
) -> None:
    """Apply the filtered graph for one entity to Virtuoso. One UPDATE
    if the graph fits in a single chunk; otherwise an atomic
    DELETE+first-chunk-INSERT followed by N-1 INSERT-DATA UPDATEs for
    the remaining chunks. Raises on the first failing UPDATE.

    ``sparql_update_url`` is Virtuoso's ``/sparql-auth`` endpoint
    (typically ``http://virtuoso:8890/sparql-auth``). It requires
    Digest-auth using the DBA credential. The read-only ``/sparql``
    endpoint cannot mutate."""
    chunks = _chunk_triples(filtered, SPARQL_CHUNK_TRIPLES)

    if not chunks:
        # Filter stripped everything (rare — entity with only non-EU
        # labels and no claims). Clear what's currently in Virtuoso
        # so we don't keep stale state.
        _post_update(client, sparql_update_url,
                     _sparql_delete_only(entity_id), entity_id, auth)
        return

    # First UPDATE is the atomic swap.
    _post_update(
        client, sparql_update_url,
        _sparql_replace_with_first_chunk(entity_id, chunks[0]),
        entity_id, auth,
    )

    # Remaining chunks append (INSERT DATA is set-semantic so it's
    # idempotent if a retry re-runs them).
    for chunk in chunks[1:]:
        _post_update(
            client, sparql_update_url,
            _sparql_insert_chunk(chunk), entity_id, auth,
        )


def tombstone_entity(
    entity_id: str, client: httpx.Client, sparql_update_url: str,
    auth: tuple[str, str] | None = None,
) -> None:
    """Remove all triples for ``entity_id`` from our named graph. Used
    when the relay marked the entity ``is_deleted=true`` from a
    Wikidata page-delete event."""
    _post_update(
        client, sparql_update_url,
        _sparql_delete_only(entity_id), entity_id, auth,
    )
