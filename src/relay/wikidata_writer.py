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

  * ``write_entity`` — apply the filtered graph to Virtuoso by issuing
    a single SPARQL UPDATE that DELETEs every existing triple for the
    entity in our named graph, then INSERTs the new ones. Atomic per
    entity. The Virtuoso `Update` endpoint is used over HTTP — no
    direct isql dependency in the worker pod.

The named graph is hard-coded to ``https://fontem.eu/graph/wikidata``,
matching the bulk-load. Don't parameterise — drifting from the
bulk-load graph means the worker silently writes to a separate slice
that none of our SPARQL endpoints query.
"""
from __future__ import annotations

import logging

import httpx
from rdflib import Graph, Literal

from src.relay.eu_languages import EU_LANGUAGES

logger = logging.getLogger(__name__)

WIKIDATA_GRAPH = "https://fontem.eu/graph/wikidata"
WIKIDATA_ENTITY_PREFIX = "http://www.wikidata.org/entity/"


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
        out.add((subj, pred, obj))
    return out


def _entity_uri(entity_id: str) -> str:
    return f"{WIKIDATA_ENTITY_PREFIX}{entity_id}"


def _serialise_for_insert(graph: Graph) -> str:
    """Turtle is convenient locally but SPARQL UPDATE needs N-Triples
    in an INSERT DATA block. rdflib's ``nt`` serializer gives us
    exactly that — one triple per line, IRIs in angle brackets."""
    return graph.serialize(format="nt").strip()


def _sparql_update_replace(entity_id: str, filtered: Graph) -> str:
    """Build the SPARQL UPDATE that atomically replaces an entity's
    triples in our named graph.

    Three statements separated by `;` — Virtuoso executes them as one
    transaction:

      1. DELETE every triple in the entity's named graph where the
         subject is the entity. We don't try to be clever and DELETE
         only the diff — at ~50–200 triples per entity it's cheaper
         to drop and rewrite than to compute the set difference.
      2. (Implicit) The INSERT DATA below.

    The entity URI is interpolated as a literal IRI, not bound — the
    UPDATE only fires for one entity per call so a bound variable
    would be an unnecessary indirection."""
    entity_iri = _entity_uri(entity_id)
    nt_body = _serialise_for_insert(filtered)
    return (
        f"WITH <{WIKIDATA_GRAPH}>\n"
        f"DELETE {{ <{entity_iri}> ?p ?o }}\n"
        f"WHERE  {{ <{entity_iri}> ?p ?o }} ;\n"
        f"INSERT DATA {{ GRAPH <{WIKIDATA_GRAPH}> {{\n{nt_body}\n}} }}"
    )


def _sparql_update_delete_only(entity_id: str) -> str:
    """Tombstone path: just DELETE everything for the entity."""
    entity_iri = _entity_uri(entity_id)
    return (
        f"WITH <{WIKIDATA_GRAPH}>\n"
        f"DELETE {{ <{entity_iri}> ?p ?o }}\n"
        f"WHERE  {{ <{entity_iri}> ?p ?o }}"
    )


def write_entity(
    entity_id: str, filtered: Graph,
    client: httpx.Client, sparql_update_url: str,
    auth: tuple[str, str] | None = None,
) -> None:
    """Apply the filtered graph for one entity to Virtuoso via SPARQL
    UPDATE. Raises on non-2xx response; caller decides retry policy.

    ``sparql_update_url`` is Virtuoso's ``/sparql-auth`` endpoint
    (typically ``http://virtuoso:8890/sparql-auth``). It requires
    Digest-auth using the DBA credential. The read-only ``/sparql``
    endpoint cannot mutate."""
    update = _sparql_update_replace(entity_id, filtered)
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


def tombstone_entity(
    entity_id: str, client: httpx.Client, sparql_update_url: str,
    auth: tuple[str, str] | None = None,
) -> None:
    """Remove all triples for ``entity_id`` from our named graph. Used
    when the relay marked the entity ``is_deleted=true`` from a
    Wikidata page-delete event."""
    update = _sparql_update_delete_only(entity_id)
    resp = client.post(
        sparql_update_url,
        data={"query": update},
        auth=httpx.DigestAuth(*auth) if auth else None,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Virtuoso DELETE for {entity_id} failed {resp.status_code}: "
            f"{resp.text[:300]}"
        )
