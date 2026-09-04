"""Reading an entity through its owl:sameAs closure.

Virtuoso is where identity lives. When the consolidator approves an
equivalence it emits AssertSameAs, the sink writes ``owl:sameAs``, and
from then on the two subjects are one entity whose facts are spread
across both. A query that reads a bare subject sees only half of them.

Why not ``DEFINE input:same-as "yes"``
--------------------------------------
That is Virtuoso's built-in and it does the right thing semantically —
measured on prod, a subject went from 1 property to 20. But it cannot be
scoped. It expands over every ``owl:sameAs`` the instance can see, and
this instance mirrors external corpora that are mostly sameAs:

    graph/mirror/cellar/eu       91,126,805
    graph/wikidata/truthy         4,872,769
    graph/company                    27,452

A single-subject lookup costs 0.87s with it and 0.011s without, and
adding ``FROM <our-graph>`` does not help — the option is not
graph-scoped. 80x on the pattern an entity page uses is not a trade
worth making to avoid writing a property path.

The property path below is plain SPARQL 1.1, returns results identical
to the built-in (verified against prod), stays inside the graph it is
given so the mirrors are never walked, and runs in 0.010s.

``(owl:sameAs|^owl:sameAs)*`` is deliberate on all three counts:
  *   zero-or-more, so a subject with no equivalences still returns its
      own triples rather than nothing;
  ^   the inverse, because which side the consolidator recorded as the
      source is arbitrary and a one-directional walk would miss half;
  |   both together, so the walk is over the symmetric closure — which
      is what owl:sameAs means.
"""

from __future__ import annotations

OWL_SAME_AS = "http://www.w3.org/2002/07/owl#sameAs"


def same_as_closure_pattern(subject_iri: str, *, obj: str = "?o") -> str:
    """A graph pattern binding ?p / ``obj`` over the subject's closure.

    Callers wrap this in their own SELECT and GRAPH clause.
    """
    return (
        f"<{subject_iri}> (<{OWL_SAME_AS}>|^<{OWL_SAME_AS}>)* ?_same . "
        f"?_same ?p {obj}"
    )


def same_as_closure_query(subject_iri: str, *, graph_iri: str | None = None) -> str:
    """SELECT ?p ?o for a subject, expanded through its sameAs closure.

    ``graph_iri`` scopes the walk. Leaving it unset queries the default
    graph set, which on this instance includes the mirrored corpora —
    fine for a one-off, but pass the graph for anything user-facing.
    """
    pattern = same_as_closure_pattern(subject_iri)
    if graph_iri:
        return f"SELECT ?p ?o WHERE {{ GRAPH <{graph_iri}> {{ {pattern} }} }}"
    return f"SELECT ?p ?o WHERE {{ {pattern} }}"
