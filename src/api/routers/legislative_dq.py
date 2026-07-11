"""Legislative-mirror data quality surface (gitops#290).

Reports what the CELLAR CDM mirror actually holds — coverage by decade,
FRBR-level totals, identifier completeness, freshness — straight from
our Virtuoso via SPARQL. Term-for-term correctness vs the source is the
dq-assert job's business (consistency.cellar_mirror_parity); this
endpoint is the observability face for the dashboard.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.data.sparql.virtuoso_client import VirtuosoClient

router = APIRouter(prefix="/data-quality", tags=["data-quality"])

GRAPH = "http://data.fontem.eu/graph/mirror/cellar/eu"
_CDM = "http://publications.europa.eu/ontology/cdm#"


def _one(rows: list[dict], key: str, default=0):
    if not rows:
        return default
    v = rows[0].get(key)
    return v if v is not None else default


def get_legislative_stats(virtuoso: VirtuosoClient) -> dict:
    """All queries are graph-scoped (never a full-store scan — the
    Virtuoso OOM lessons apply to reads too)."""
    frm = f"FROM <{GRAPH}>"
    totals = virtuoso.query(
        f"SELECT (COUNT(*) AS ?triples) {frm} WHERE {{ ?s ?p ?o }}")
    frbr = virtuoso.query(
        f"PREFIX cdm: <{_CDM}> "
        f"SELECT (COUNT(DISTINCT ?w) AS ?works) {frm} "
        f"WHERE {{ ?w cdm:work_date_document ?d }}")
    expr = virtuoso.query(
        f"PREFIX cdm: <{_CDM}> "
        f"SELECT (COUNT(DISTINCT ?e) AS ?expressions) {frm} "
        f"WHERE {{ ?e cdm:expression_belongs_to_work ?w }}")
    mani = virtuoso.query(
        f"PREFIX cdm: <{_CDM}> "
        f"SELECT (COUNT(DISTINCT ?m) AS ?manifestations) {frm} "
        f"WHERE {{ ?m cdm:manifestation_manifests_expression ?e }}")
    span = virtuoso.query(
        f"PREFIX cdm: <{_CDM}> "
        f"SELECT (MIN(?d) AS ?earliest) (MAX(?d) AS ?latest) {frm} "
        f"WHERE {{ ?w cdm:work_date_document ?d }}")
    eli = virtuoso.query(
        f"PREFIX cdm: <{_CDM}> "
        f"SELECT (COUNT(DISTINCT ?w) AS ?with_eli) {frm} "
        f"WHERE {{ ?w cdm:work_date_document ?d ; cdm:resource_legal_eli ?u }}")
    by_decade = virtuoso.query(
        f"PREFIX cdm: <{_CDM}> "
        f"SELECT ?decade (COUNT(DISTINCT ?w) AS ?works) {frm} WHERE {{ "
        f"?w cdm:work_date_document ?d . "
        f"BIND(CONCAT(SUBSTR(STR(?d), 1, 3), '0s') AS ?decade) }} "
        f"GROUP BY ?decade ORDER BY ?decade")
    works = int(_one(frbr, "works"))
    return {
        "graph": GRAPH,
        "triples": int(_one(totals, "triples")),
        "works": works,
        "expressions": int(_one(expr, "expressions")),
        "manifestations": int(_one(mani, "manifestations")),
        "earliest_work_date": _one(span, "earliest", None),
        "latest_work_date": _one(span, "latest", None),
        "works_with_eli": int(_one(eli, "with_eli")),
        "eli_coverage": (int(_one(eli, "with_eli")) / works) if works else None,
        "works_by_decade": [
            {"decade": r.get("decade"), "works": int(r.get("works", 0))}
            for r in by_decade
        ],
    }


@router.get(
    "/legislative",
    responses={
        503: {"description": "VIRTUOSO_SPARQL_URL not configured"},
    },
)
def legislative_stats():
    """CELLAR mirror coverage + freshness for the DQ hub."""
    virtuoso = VirtuosoClient.from_env()
    if virtuoso is None:
        raise HTTPException(
            status_code=503,
            detail="legislative mirror stats need VIRTUOSO_SPARQL_URL")
    return get_legislative_stats(virtuoso)
