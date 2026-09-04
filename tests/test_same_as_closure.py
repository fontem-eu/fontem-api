"""Reading an entity through its owl:sameAs closure.

Identity lives in Virtuoso. Once an equivalence is approved the two
subjects are one entity whose facts are spread across both, so a query
reading a bare subject sees half of them — and the cross-store
consistency check reads that as Virtuoso missing fields it actually has.

The property path is used instead of Virtuoso's `DEFINE input:same-as`
because the built-in cannot be scoped: it expands over every owl:sameAs
the instance can see, and this instance mirrors 91.1M of them from
cellar plus 4.9M from wikidata against 27k of our own. Measured on prod,
that is 0.87s vs 0.010s for the same result.
"""

from src.data.sparql.same_as import (
    OWL_SAME_AS,
    same_as_closure_pattern,
    same_as_closure_query,
)

IRI = "http://data.fontem.eu/id/Company/abc"
G = "http://data.fontem.eu/graph/company"


def test_walks_both_directions():
    """Which side the consolidator recorded as source is arbitrary, so a
    one-directional walk would miss half the equivalences."""
    pattern = same_as_closure_pattern(IRI)
    assert f"<{OWL_SAME_AS}>|^<{OWL_SAME_AS}>" in pattern


def test_closure_is_zero_or_more():
    """A subject with no equivalences must still return its own triples.
    `+` would return nothing at all for the overwhelming majority of
    entities, which have no duplicate."""
    assert ")*" in same_as_closure_pattern(IRI)
    assert ")+" not in same_as_closure_pattern(IRI)


def test_graph_scoping_is_available():
    """The whole reason for not using DEFINE input:same-as: the walk has
    to stay out of the mirrored corpora."""
    q = same_as_closure_query(IRI, graph_iri=G)
    assert f"GRAPH <{G}>" in q
    assert q.count("GRAPH") == 1


def test_unscoped_form_is_still_valid_sparql():
    q = same_as_closure_query(IRI)
    assert q.startswith("SELECT ?p ?o WHERE {")
    assert "GRAPH" not in q
    assert q.rstrip().endswith("}")


def test_subject_is_embedded_as_an_iri():
    q = same_as_closure_query(IRI)
    assert f"<{IRI}>" in q


def test_binds_predicate_and_object():
    """The caller's contract: ?p and ?o, same as a bare `?s ?p ?o` read,
    so switching a query onto the closure changes only its coverage."""
    q = same_as_closure_query(IRI)
    assert "?p" in q and "?o" in q


def test_custom_object_variable():
    assert "?value" in same_as_closure_pattern(IRI, obj="?value")
