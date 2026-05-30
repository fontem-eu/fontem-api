"""Unit tests for the Wikidata graph filter + SPARQL UPDATE builder.

We pin two behaviours:

  * the language filter drops non-EU literals but keeps untagged ones
    (numbers, dates, IRIs, language-neutral strings);
  * the SPARQL UPDATE is shaped so Virtuoso treats DELETE+INSERT as
    one transaction — a half-applied entity rewrite would leave the
    graph inconsistent.
"""
from __future__ import annotations

from rdflib import Graph, Literal, URIRef

from src.relay.wikidata_writer import (
    WIKIDATA_GRAPH,
    _sparql_update_delete_only,
    _sparql_update_replace,
    filter_graph,
)


WD = "http://www.wikidata.org/entity/"
WDT = "http://www.wikidata.org/prop/direct/"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"


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
    # flavor=simple emits sitelink cards (subject = wikipedia URL),
    # an EntityData dataset block (subject = Special:EntityData/Qxxx),
    # and in flavor=dump it would also emit statement/reference/value
    # subjects. None of those belong in our graph because we can only
    # DELETE triples whose subject is the entity on the next re-fetch.
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


def test_sparql_update_replace_uses_named_graph_and_delete_then_insert() -> None:
    g = Graph()
    g.add((URIRef(f"{WD}Q42"), URIRef(f"{WDT}P31"), URIRef(f"{WD}Q5")))
    update = _sparql_update_replace("Q42", g)
    # The graph clause is shared by DELETE and INSERT (WITH + INSERT
    # DATA into GRAPH) so both target the same named-graph slice.
    assert WIKIDATA_GRAPH in update
    # DELETE must come before INSERT or we'd lose the new triples.
    assert update.index("DELETE") < update.index("INSERT")
    # Semicolon separates the two updates into a single transaction.
    assert ";" in update
    # The new triples appear in the INSERT DATA block.
    assert f"<{WD}Q42> <{WDT}P31> <{WD}Q5>" in update


def test_sparql_update_replace_handles_empty_graph() -> None:
    # If the filter strips everything (a now-empty entity), we still
    # need the DELETE to run — otherwise stale triples persist.
    out = _sparql_update_replace("Q42", Graph())
    assert "DELETE" in out
    assert "INSERT DATA" in out


def test_sparql_update_delete_only_for_tombstone() -> None:
    update = _sparql_update_delete_only("Q42")
    assert "DELETE" in update
    assert "INSERT" not in update
    assert WIKIDATA_GRAPH in update
    assert f"<{WD}Q42>" in update
