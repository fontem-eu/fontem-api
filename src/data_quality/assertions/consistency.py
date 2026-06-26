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
