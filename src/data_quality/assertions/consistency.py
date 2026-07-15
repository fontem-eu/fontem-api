"""Cross-store consistency spot-check.

Both sinks (Neo4j + Virtuoso) project the SAME ``events.entity_events``
stream, so the fields a loader emits must render identically in both. This
samples a random handful of entities and compares them field-by-field; a
sink that drops, lags, or mis-renders an event surfaces as a mismatch.

Only fields *both* sinks derive from the same loader event are compared --
store-specific enrichment (e.g. ``Company.lei`` from a GLEIF load that
reached one store and not the other) is intentionally excluded so the check
measures *sink* consistency, not load coverage.
"""
from __future__ import annotations

from typing import Any

# RDF namespace identifiers (not network endpoints) -- http:// is required to
# match the predicates Virtuoso actually stores.
_ONT = "http://data.fontem.eu/ontology#"
_ID = "http://data.fontem.eu/id"
_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
_P17 = "http://www.wikidata.org/prop/direct/P17"

# entity_type -> {label, key, iri prefix, fields: neo4j_prop -> virtuoso_predicate}
SPECS: dict[str, dict] = {
    "Contract": {
        "label": "Contract", "key": "ted_notice_id", "iri": _ID + "/Contract/",
        "fields": {
            "value_eur": _ONT + "valueEur",
            "value_currency": _ONT + "valueCurrency",
            "procedure_type": _ONT + "procedureType",
            "tenders_received": _ONT + "tendersReceived",
            "publication_date": _ONT + "publicationDate",
            "cpv": _ONT + "cpv",
        },
    },
    "Company": {
        "label": "Company", "key": "gmr_id", "iri": _ID + "/Company/",
        "fields": {"name": _LABEL, "country": _P17},
    },
}


def _norm(v: Any) -> str | None:
    """Comparable form: numbers by trimmed float, bools as 1/0, else trimmed str."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return f"{float(v):.4f}".rstrip("0").rstrip(".")
    s = str(v).strip()
    try:
        return f"{float(s):.4f}".rstrip("0").rstrip(".")
    except ValueError:
        return s


def _virtuoso_map(triples: list) -> dict:
    vmap: dict[str, Any] = {}
    for t in triples:
        if t.get("p") is not None:
            vmap.setdefault(str(t["p"]), t.get("o"))
    return vmap


def _first_mismatch(key: Any, row: dict, fields: dict, vmap: dict) -> str | None:
    """Return a description of the first field that disagrees, or None."""
    if not vmap:
        return f"{key}: absent in virtuoso"
    for nprop, vpred in fields.items():
        nval = _norm(row.get(nprop))
        if nval is not None and nval != _norm(vmap.get(vpred)):
            return f"{key}.{nprop}: neo4j={nval!r} virtuoso={_norm(vmap.get(vpred))!r}"
    return None


def check(neo4j_client, virtuoso, entity_type: str, n: int = 12) -> dict:
    """Sample n random entities from Neo4j, compare each to its Virtuoso twin.
    Returns {violations, total, detail} for the standard evaluators."""
    spec = SPECS[entity_type]
    fields = spec["fields"]
    proj = ", ".join(f"c.`{f}` AS `{f}`" for f in fields)
    sample_q = (
        f"MATCH (c:`{spec['label']}`) WHERE c.`{spec['key']}` IS NOT NULL "
        f"WITH c, rand() AS r ORDER BY r LIMIT {int(n)} "
        f"RETURN c.`{spec['key']}` AS _key, {proj}"
    )
    with neo4j_client.session() as session:
        sampled = [dict(r) for r in session.run(sample_q)]
    mismatches: list[str] = []
    for row in sampled:
        triples = virtuoso.query(
            "SELECT ?p ?o WHERE { <" + spec["iri"] + str(row["_key"]) + "> ?p ?o }")
        m = _first_mismatch(row["_key"], row, fields, _virtuoso_map(triples))
        if m:
            mismatches.append(m)
    return {"violations": len(mismatches), "total": len(sampled),
            "detail": "; ".join(mismatches[:5])}


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


def petition_parity_check(neo4j_client, virtuoso) -> dict:
    """Petition counts must match across Neo4j and Virtuoso.

    Both sinks project the same UpsertPetition stream (full-register
    upserts, no graph-replace bracket), so a count drift means one sink
    dropped or lagged events.
    """
    with neo4j_client.session() as session:
        graph_n = session.run(
            "MATCH (p:Petition) RETURN count(p) AS n"
        ).single()["n"]
    rows = virtuoso.query(
        "SELECT (COUNT(?s) AS ?n) WHERE { ?s a "
        "<http://data.fontem.eu/ontology#Petition> }"
    )
    virt_n = int(rows[0].get("n") or 0) if rows else 0
    diff = abs(int(graph_n) - virt_n)
    return {"violations": diff,
            "detail": f"neo4j={graph_n} virtuoso={virt_n}"}


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
        "}"
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
        "}"
    )
    n = int(rows[0].get("n") or 0) if rows else 0
    return {"violations": 0 if n > 0 else 1,
            "detail": f"ft matches for canary term: {n}"}
