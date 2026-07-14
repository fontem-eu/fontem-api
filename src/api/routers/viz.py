"""Per-visualization data endpoints.

Each visualization has its **own** route: it takes params, queries the graph, and
returns **plot-ready** data — and validates its own params (FastAPI). There is no
central viz-type registry: the client maps a viz ``type`` to one of these
endpoints, and nothing is ever stored as rendered data. That's what makes faked
plots impossible — the numbers can only ever come from here.
"""
from __future__ import annotations

from typing import Annotated

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, Query

from src.data.graph.neo4j_client import Neo4jClient

from src.data.graph._value_quality import canonical_predicate

router = APIRouter(prefix="/viz", tags=["viz"])


# Only canonical contracts (collapse_modifications): a modification notice
# restates the same contract, so counting it again would inflate a bidder
# bucket. Non-modification / stamped-canonical nodes only.
_BIDDER_BREAKDOWN = f"""
MATCH (co:Company {{gmr_id: $entity_id}})-[:AWARDED_TO]-(c:Contract)
WHERE {canonical_predicate('c')}
RETURN c.tenders_received AS bidders, count(c) AS n
"""


def _bucket_label(bidders) -> str:
    if bidders is None:
        return "Not disclosed"
    if bidders == 1:
        return "1 (single bidder)"
    return str(bidders)


def _bucket_sort(bidders) -> tuple[int, int]:
    # numeric counts ascending; "Not disclosed" always last
    return (1, 0) if bidders is None else (0, int(bidders))


@router.get("/company-bidder-breakdown")
@inject
def company_bidder_breakdown(
    entity_id: Annotated[str, Query(min_length=1, max_length=64)],
    *,
    neo4j: FromDishka[Neo4jClient],
) -> dict:
    """A company's contracts grouped by number of bidders received — the
    single-bidder competition signal. Returns plot-ready data for a `bar_h`
    chart: ``{title, chart, format, bars:[{label, value}]}``."""
    rows = []
    with neo4j.session() as session:
        for rec in session.run(_BIDDER_BREAKDOWN, entity_id=entity_id):
            rows.append((rec["bidders"], rec["n"]))
    bars = [
        {"label": _bucket_label(b), "value": n}
        for b, n in sorted(rows, key=lambda r: _bucket_sort(r[0]))
    ]
    return {
        "title": "Contracts by number of bidders",
        "chart": "bar_h",
        "format": "number",
        "bars": bars,
    }
