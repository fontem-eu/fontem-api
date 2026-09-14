"""Consistency checks for the Virtuoso-hosted mirrors.

The CELLAR mirror must match its source term-for-term, its full-text index
must be built, and the :LegalAct spine in Neo4j must track it. Platform data
is no longer projected into Virtuoso live, so nothing here compares platform
entities across the two stores.
"""
from __future__ import annotations


# ── CELLAR mirror parity (gitops#290) ────────────────────────────────
#
# The mirror graph must be a VERBATIM copy of the source: for sampled
# works, every (predicate, object) of the work and its expression/
# manifestation closure must match CELLAR term-for-term. This is the
# permanent form of the check that caught the UNION-scoping bug which
# silently dropped all work-level triples in the first MVP export.

MIRROR_GRAPH = "http://data.fontem.eu/graph/mirror/cellar/eu"
CELLAR_SPARQL = "https://publications.europa.eu/webapi/rdf/sparql"
_CDM = "http://publications.europa.eu/ontology/cdm#"


def _sparql(url: str, query: str, http_get) -> list[dict]:
    """SELECT against a SPARQL endpoint -> list of binding dicts.
    `http_get(url, params) -> parsed-json` is injected for testability."""
    data = http_get(url, {"query": query,
                          "format": "application/sparql-results+json"})
    return data["results"]["bindings"]


def _term(b: dict) -> tuple:
    """Canonical comparable form of one SPARQL JSON term. Virtuoso 7
    emits SPARQL-1.0-style "typed-literal" where CELLAR emits 1.1-style
    "literal" + datatype — semantically identical, so normalise the
    node-type before comparing."""
    node_type = b.get("type")
    if node_type == "typed-literal":
        node_type = "literal"
    return (node_type, b.get("value"), b.get("datatype"),
            b.get("xml:lang"))


def _closure_subjects(url: str, work: str, http_get, graph: str | None) -> set:
    frm = f"FROM <{graph}> " if graph else ""
    q = (f"SELECT DISTINCT ?s {frm}WHERE {{ "
         f"{{ BIND(<{work}> AS ?s) }} UNION "
         f"{{ ?s <{_CDM}expression_belongs_to_work> <{work}> }} UNION "
         f"{{ ?e <{_CDM}expression_belongs_to_work> <{work}> . "
         f"?s <{_CDM}manifestation_manifests_expression> ?e }} }}")
    return {b["s"]["value"] for b in _sparql(url, q, http_get)}


def _subject_terms(url: str, subject: str, http_get,
                   graph: str | None) -> set:
    frm = f"FROM <{graph}> " if graph else ""
    q = f"SELECT ?p ?o {frm}WHERE {{ <{subject}> ?p ?o }}"
    return {(b["p"]["value"],) + _term(b["o"])
            for b in _sparql(url, q, http_get)}


def cellar_mirror_check(mirror_url: str, http_get, n: int = 8,
                        cellar_url: str = CELLAR_SPARQL) -> dict:
    """Sample n random works from the mirror graph and require their full
    FRBR closure to match CELLAR term-for-term. A difference means the
    mirror lost or corrupted something (or the record changed at source
    since the snapshot — the detail string lets a human tell which)."""
    sample_q = (
        f"SELECT ?w FROM <{MIRROR_GRAPH}> WHERE {{ "
        f"?w <{_CDM}work_date_document> ?d }} "
        f"ORDER BY RAND() LIMIT {int(n)}")
    works = [b["w"]["value"] for b in _sparql(mirror_url, sample_q, http_get)]
    mismatches: list[str] = []
    for w in works:
        ours = _closure_subjects(mirror_url, w, http_get, MIRROR_GRAPH)
        theirs = _closure_subjects(cellar_url, w, http_get, None)
        if ours != theirs:
            mismatches.append(
                f"{w}: closure differs (ours={len(ours)} cellar={len(theirs)})")
            continue
        for s in sorted(ours):
            a = _subject_terms(mirror_url, s, http_get, MIRROR_GRAPH)
            b = _subject_terms(cellar_url, s, http_get, None)
            if a != b:
                mismatches.append(
                    f"{s}: {len(b - a)} missing / {len(a - b)} extra terms")
                break
    return {"violations": len(mismatches), "total": len(works),
            "detail": "; ".join(mismatches[:5])}


def legal_act_spine_check(neo4j_client, virtuoso) -> dict:
    """The :LegalAct spine tracks the mirror's sector-3 corpus.

    The materializer sweeps daily after the mirror delta, so the graph may
    trail the mirror briefly; more than 5% shortfall (or any graph excess)
    counts as violations.
    """
    with neo4j_client.session() as session:
        graph_n = session.run(
            "MATCH (a:LegalAct) WHERE a.source = 'cellar-mirror' "
            "RETURN count(a) AS n"
        ).single()["n"]
    rows = virtuoso.query(
        "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#> "
        f"SELECT (COUNT(DISTINCT ?cx) AS ?n) FROM <{MIRROR_GRAPH}> WHERE {{ "
        "?w cdm:resource_legal_id_celex ?cx . "
        'FILTER(STRSTARTS(STR(?cx), "3")) }'
    )
    mirror_n = int(rows[0].get("n") or 0) if rows else 0
    graph_n = int(graph_n)
    shortfall = max(0, mirror_n - graph_n - int(mirror_n * 0.05))
    excess = max(0, graph_n - mirror_n)
    return {"violations": shortfall + excess,
            "detail": f"graph={graph_n} mirror_sector3={mirror_n}"}


def cellar_ft_index_check(virtuoso) -> dict:
    """The full-text index over the mirror is built and searchable.

    bif:contains on a term guaranteed present in legal titles must match.
    Zero matches means the VTLOG backlog is unprocessed — exactly the
    silent state that made legislation search return nothing for weeks
    before 2026-07-13 (index rule existed, batch never ran).
    """
    rows = virtuoso.query(
        "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#> "
        f"SELECT (COUNT(?e) AS ?n) FROM <{MIRROR_GRAPH}> WHERE {{ "
        "?e cdm:expression_title ?t . "
        '?t bif:contains "regulation" }'
    )
    n = int(rows[0].get("n") or 0) if rows else 0
    # 1000, not >0: an in-progress index build produces its first matches
    # long before search is usable (observed: prod at 1 match while ~95%
    # of titles were still unindexed). Shared floor reference: 14554.
    return {"violations": 0 if n >= 1000 else 1,
            "detail": f"ft matches for canary term: {n} (min 1000)"}
