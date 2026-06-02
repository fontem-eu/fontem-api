"""Unit tests for the Wikidata graph filter + chunked SPARQL UPDATE
writer.

Three behaviours pinned:

  * ``filter_graph`` drops sitelink / metadata subjects and non-EU
    literals;
  * a single-chunk write keeps the DELETE + first-chunk INSERT atomic
    inside one transaction (a half-applied entity rewrite would leave
    the graph inconsistent);
  * a multi-chunk write fires the atomic first UPDATE, then one
    ``INSERT DATA`` per remaining chunk — the Virtuoso SP031 ceiling
    on a single statement is the entire reason the writer chunks at
    all, so the per-chunk wire shape is the contract being protected.
"""
from __future__ import annotations

from typing import Callable

import httpx
from rdflib import Graph, Literal, URIRef

from src.relay import wikidata_writer
from src.relay.wikidata_writer import (
    SPARQL_CHUNK_TRIPLES,
    WIKIDATA_GRAPH,
    _chunk_triples,
    _sparql_delete_only,
    _sparql_insert_chunk,
    _sparql_replace_with_first_chunk,
    filter_graph,
    write_entity,
)


WD = "http://www.wikidata.org/entity/"
WDT = "http://www.wikidata.org/prop/direct/"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"


# ----------------------- filter_graph -----------------------


def test_filter_keeps_eu_language_labels() -> None:
    g = Graph()
    g.add((URIRef(f"{WD}Q42"), URIRef(f"{RDFS}label"), Literal("Apple", lang="en")))
    g.add((URIRef(f"{WD}Q42"), URIRef(f"{RDFS}label"), Literal("Pomme", lang="fr")))
    out = filter_graph(g, "Q42")
    assert len(out) == 2


def test_filter_drops_non_eu_language_labels() -> None:
    g = Graph()
    g.add((URIRef(f"{WD}Q42"), URIRef(f"{RDFS}label"), Literal("আপেল", lang="bn")))
    g.add((URIRef(f"{WD}Q42"), URIRef(f"{RDFS}label"), Literal("Apple", lang="en")))
    out = filter_graph(g, "Q42")
    assert len(out) == 1


def test_filter_keeps_untagged_literals() -> None:
    # Numeric and IRI literals carry no language; they are statement
    # objects (e.g. dates, identifiers) that we always want.
    g = Graph()
    g.add((URIRef(f"{WD}Q42"), URIRef(f"{WDT}P569"),
           Literal("1952-03-11", datatype=URIRef("http://www.w3.org/2001/XMLSchema#date"))))
    g.add((URIRef(f"{WD}Q42"), URIRef(f"{WDT}P31"), URIRef(f"{WD}Q5")))
    out = filter_graph(g, "Q42")
    assert len(out) == 2


def test_filter_drops_non_entity_subjects() -> None:
    # flavor=simple emits sitelink cards (subject = wikipedia URL) and
    # an EntityData dataset block (subject = Special:EntityData/Qxxx).
    # None of those belong in our graph because we can only DELETE
    # triples whose subject is the entity on the next re-fetch.
    g = Graph()
    g.add((URIRef("https://en.wikipedia.org/wiki/Douglas_Adams"),
           URIRef("http://schema.org/about"),
           URIRef(f"{WD}Q42")))
    g.add((URIRef("https://www.wikidata.org/wiki/Special:EntityData/Q42"),
           URIRef("http://schema.org/about"),
           URIRef(f"{WD}Q42")))
    g.add((URIRef(f"{WD}Q42"), URIRef(f"{WDT}P31"), URIRef(f"{WD}Q5")))
    out = filter_graph(g, "Q42")
    assert len(out) == 1
    only = list(out)[0]
    assert str(only[0]) == f"{WD}Q42"


def test_filter_keeps_mul_literals() -> None:
    # 'mul' = Wikidata's "language-neutral" tag for binomials etc.
    g = Graph()
    g.add((URIRef(f"{WD}Q42"), URIRef(f"{RDFS}label"),
           Literal("H2O", lang="mul")))
    out = filter_graph(g, "Q42")
    assert len(out) == 1


GEO_WKT = "http://www.opengis.net/ont/geosparql#wktLiteral"
P625 = f"{WDT}P625"


def test_filter_keeps_earth_wkt_coords() -> None:
    # Earth coords have no globe prefix — Wikidata's default.
    g = Graph()
    g.add((URIRef(f"{WD}Q243"), URIRef(P625),
           Literal("Point(2.294479 48.858296)",
                   datatype=URIRef(GEO_WKT))))
    out = filter_graph(g, "Q243")
    assert len(out) == 1


def test_filter_drops_extraterrestrial_wkt_coords() -> None:
    # Olympus Mons on Mars: globe IRI prefix in the lexical form
    # → Virtuoso rejects with RDFGE error → entity stuck dirty forever.
    g = Graph()
    g.add((URIRef(f"{WD}Q520"), URIRef(P625),
           Literal("<http://www.wikidata.org/entity/Q111> Point(226.2 18.65)",
                   datatype=URIRef(GEO_WKT))))
    g.add((URIRef(f"{WD}Q520"), URIRef(f"{RDFS}label"),
           Literal("Olympus Mons", lang="en")))
    out = filter_graph(g, "Q520")
    # Drop the WKT, keep the label.
    assert len(out) == 1
    only = list(out)[0]
    assert only[1] == URIRef(f"{RDFS}label")


# ----------------------- SPARQL UPDATE shape -----------------------


def test_sparql_replace_uses_named_graph_and_delete_then_insert() -> None:
    g = Graph()
    g.add((URIRef(f"{WD}Q42"), URIRef(f"{WDT}P31"), URIRef(f"{WD}Q5")))
    update = _sparql_replace_with_first_chunk("Q42", list(g))
    # Both clauses target the same named-graph slice.
    assert WIKIDATA_GRAPH in update
    # DELETE must come before INSERT or we'd lose the new triples.
    assert update.index("DELETE") < update.index("INSERT")
    # Semicolon separates the two updates into a single transaction.
    assert ";" in update
    # The new triples appear in the INSERT DATA block.
    assert f"<{WD}Q42> <{WDT}P31> <{WD}Q5>" in update
    # Must include the big-data-const override so the HTTP endpoint's
    # silently-prepended `define sql:big-data-const 0` doesn't take
    # effect (it triggers SR580 on the inline-constant path for
    # entities whose hash cache has stale entries).
    assert "define sql:big-data-const 1" in update


def test_sparql_replace_override_comes_after_endpoint_prepend_order() -> None:
    # Order matters: Virtuoso honors the LAST `define <directive>`
    # for any given directive. The endpoint's prepend is implicit
    # (we can't see it), but our override must be the FIRST line of
    # what we send so it lands after that prepend in the parser's
    # view.
    update = _sparql_replace_with_first_chunk("Q42", [])
    first_line = update.splitlines()[0]
    assert first_line == "define sql:big-data-const 1"


def test_sparql_replace_handles_empty_chunk() -> None:
    # Edge: the chunker yielded an empty list. Builder must still
    # emit a syntactically-valid UPDATE; the DELETE clause does the
    # work and the INSERT DATA block is just empty.
    out = _sparql_replace_with_first_chunk("Q42", [])
    assert "DELETE" in out
    assert "INSERT DATA" in out


def test_sparql_insert_chunk_has_no_delete() -> None:
    # Subsequent chunks are pure INSERT — the DELETE happened in the
    # first UPDATE and replaying it would wipe the work-in-progress.
    g = Graph()
    g.add((URIRef(f"{WD}Q42"), URIRef(f"{WDT}P31"), URIRef(f"{WD}Q5")))
    out = _sparql_insert_chunk(list(g))
    assert "INSERT DATA" in out
    assert "DELETE" not in out
    assert WIKIDATA_GRAPH in out
    # The directive override applies to chunks 2..N too — without it
    # the INSERT-alone path still triggers SR580 for hash-cache
    # entries left over from previous failed writes.
    assert out.splitlines()[0] == "define sql:big-data-const 1"


def test_sparql_delete_only_for_tombstone() -> None:
    update = _sparql_delete_only("Q42")
    assert "DELETE" in update
    assert "INSERT" not in update
    assert WIKIDATA_GRAPH in update
    assert f"<{WD}Q42>" in update
    # And the tombstone path too.
    assert update.splitlines()[0] == "define sql:big-data-const 1"


# ----------------------- chunker -----------------------


def test_chunk_triples_groups_by_size() -> None:
    g = Graph()
    for i in range(1250):
        g.add((URIRef(f"{WD}Q42"), URIRef(f"{WDT}P{i}"), URIRef(f"{WD}Q{i}")))
    chunks = _chunk_triples(g, 500)
    assert len(chunks) == 3
    assert [len(c) for c in chunks] == [500, 500, 250]


def test_chunk_triples_empty_graph_yields_empty_list() -> None:
    assert _chunk_triples(Graph(), 500) == []


# ----------------------- write_entity wire behaviour -----------------------


def _captured_client(handler: Callable[[httpx.Request], httpx.Response]
                     ) -> tuple[httpx.Client, list[str]]:
    """Returns a client whose POSTs we can inspect. The list grows
    with each request body so the test can assert on counts + shapes."""
    captured: list[str] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        captured.append(request.content.decode())
        return handler(request)
    client = httpx.Client(transport=httpx.MockTransport(wrapped))
    return client, captured


def test_write_entity_small_graph_uses_single_combined_update() -> None:
    g = Graph()
    g.add((URIRef(f"{WD}Q42"), URIRef(f"{WDT}P31"), URIRef(f"{WD}Q5")))
    client, captured = _captured_client(
        lambda _r: httpx.Response(200, content=b""))
    write_entity("Q42", g, client, "http://v/sparql-auth")
    assert len(captured) == 1
    body = captured[0]
    assert "DELETE" in body and "INSERT+DATA" in body.replace("%20", "+")


def test_write_entity_large_graph_chunks_into_multiple_updates(
    monkeypatch,
) -> None:
    # Force a tiny chunk size so we can exercise the chunked path
    # without building thousands of triples in the test.
    monkeypatch.setattr(wikidata_writer, "SPARQL_CHUNK_TRIPLES", 2)
    g = Graph()
    for i in range(5):  # 5 triples, chunk 2 → 3 chunks
        g.add((URIRef(f"{WD}Q42"), URIRef(f"{WDT}P{i}"), URIRef(f"{WD}Q{i}")))
    client, captured = _captured_client(
        lambda _r: httpx.Response(200, content=b""))
    write_entity("Q42", g, client, "http://v/sparql-auth")
    # 3 POSTs: one DELETE+INSERT and two INSERT-only chunks.
    assert len(captured) == 3
    first = captured[0]
    assert "DELETE" in first
    for rest in captured[1:]:
        assert "DELETE" not in rest
        assert "INSERT" in rest


def test_write_entity_empty_graph_just_deletes() -> None:
    client, captured = _captured_client(
        lambda _r: httpx.Response(200, content=b""))
    write_entity("Q42", Graph(), client, "http://v/sparql-auth")
    assert len(captured) == 1
    assert "DELETE" in captured[0]
    assert "INSERT" not in captured[0]


def test_write_entity_raises_on_500_response() -> None:
    client, _ = _captured_client(
        lambda _r: httpx.Response(500, content=b"server boom"))
    g = Graph()
    g.add((URIRef(f"{WD}Q42"), URIRef(f"{WDT}P31"), URIRef(f"{WD}Q5")))
    try:
        write_entity("Q42", g, client, "http://v/sparql-auth")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "Q42" in str(exc) and "500" in str(exc)


def test_write_entity_stops_chunking_when_first_update_fails(
    monkeypatch,
) -> None:
    # SP031 is what we're avoiding — if the first UPDATE still fails
    # we must NOT keep firing follow-up INSERTs (would leave Virtuoso
    # in a half-state and waste API time).
    monkeypatch.setattr(wikidata_writer, "SPARQL_CHUNK_TRIPLES", 2)
    g = Graph()
    for i in range(5):
        g.add((URIRef(f"{WD}Q42"), URIRef(f"{WDT}P{i}"), URIRef(f"{WD}Q{i}")))
    posts = [0]

    def handler(_r: httpx.Request) -> httpx.Response:
        posts[0] += 1
        return httpx.Response(400, content=b"SP031")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        write_entity("Q42", g, client, "http://v/sparql-auth")
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass
    # Just the one failing POST, no chunked follow-ups.
    assert posts[0] == 1


def test_write_entity_respects_module_chunk_constant() -> None:
    # Sanity: the chunk size hasn't drifted from the documented
    # "stay under SP031" budget.
    assert SPARQL_CHUNK_TRIPLES <= 1000
    assert SPARQL_CHUNK_TRIPLES >= 100
