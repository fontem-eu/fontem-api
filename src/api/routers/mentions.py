"""
Mentions API Router
===================

Resolves a Fontem IRI (`http://data.fontem.eu/id/{Class}/{uuid}`) to a
side-panel payload — label, country, summary, top facts. The IRI scheme
is the contract: today this router parses the IRI and looks up the
matching node in Neo4j by `(class, gmr_id)`; once Virtuoso lands, the
implementation swaps to `DESCRIBE <iri>` against SPARQL with no
frontend churn.

The point is to give every `@`-mention a stable identifier the editor
stores once and any caller can re-resolve. Full entity profiles still
live at their existing per-class endpoints (`/companies/:id`, etc.) —
this endpoint is the *summary* for hover / side-panel use.
"""
from __future__ import annotations

import re
from typing import Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, HTTPException, Query

from src.data.graph.neo4j_client import Neo4jClient


router = APIRouter(prefix="/mentions", tags=["mentions"])


# Locked in fontem-ontology Phase 0. The classes below are the ones we
# can resolve today via Neo4j; classes not in this set 404 cleanly so a
# malformed IRI in a story body never surfaces stale data.
_RESOLVABLE_CLASSES = frozenset({
    "Company",
    "Authority",
    "Person",
    "Lobbyist",
    "NUTSRegion",
    "CohesionProject",
    "SanctionedEntity",
})

# Strict shape: matches exactly the IRI scheme committed in
# fontem-ontology/ontology/uri-scheme.md so we don't have to second-guess
# what the assistant or editor has emitted.
_IRI_RE = re.compile(
    r"^http://data\.fontem\.eu/id/(?P<cls>[A-Za-z]+)/"
    r"(?P<uid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)


def _parse_iri(iri: str) -> tuple[str, str]:
    """Return (class, uuid) or raise 400."""
    m = _IRI_RE.match(iri)
    if not m:
        raise HTTPException(
            status_code=400,
            detail="iri must look like http://data.fontem.eu/id/<Class>/<uuid>",
        )
    cls = m.group("cls")
    if cls not in _RESOLVABLE_CLASSES:
        raise HTTPException(
            status_code=400,
            detail=f"class '{cls}' is not resolvable; one of {sorted(_RESOLVABLE_CLASSES)}",
        )
    return cls, m.group("uid")


# Each branch handles a distinct mention class (Company, Person, Authority,
# Contract, Country, Sanction, Listing, etc.). Splitting per class would
# scatter a single projection that the side panel reads top-to-bottom.
def _node_to_panel(cls: str, node: dict[str, Any]) -> dict[str, Any]:  # pylint: disable=too-many-branches
    """Project a Neo4j node onto the side-panel response.

    The shape stays small on purpose: the side panel is a hover/aside
    surface, not a full profile view. Authors with deeper questions
    follow the `links.profile` link to the full route.
    """
    label = node.get("name") or node.get("display_name") or node.get("ref") or ""
    facts: list[dict[str, str]] = []

    if "country" in node and node["country"]:
        facts.append({"key": "country", "value": str(node["country"])})

    # Class-specific salient facts. Keep this list short — anything
    # bigger belongs in the full profile route, not the side panel.
    if cls == "Company":
        if node.get("lei"):
            facts.append({"key": "LEI", "value": node["lei"]})
        if node.get("vat"):
            facts.append({"key": "VAT", "value": node["vat"]})
    elif cls == "Authority":
        if node.get("authority_id"):
            facts.append({"key": "authority_id", "value": node["authority_id"]})
    elif cls == "NUTSRegion":
        if node.get("nuts_code"):
            facts.append({"key": "NUTS code", "value": node["nuts_code"]})
        if node.get("level") is not None:
            facts.append({"key": "level", "value": str(node["level"])})
    elif cls == "Lobbyist":
        if node.get("transparency_register_id"):
            facts.append({"key": "TR ID", "value": node["transparency_register_id"]})
    elif cls == "CohesionProject":
        if node.get("project_id"):
            facts.append({"key": "project_id", "value": node["project_id"]})

    # Best-effort profile link. `entity_id` is the primary key the
    # frontend already routes by; clients that want to swap to IRI-
    # based routing can re-derive it from the request iri.
    entity_id = (
        node.get("gmr_id")
        or node.get("authority_id")
        or node.get("nuts_code")
        or ""
    )

    # `iri` is the canonical RDF identifier for the entity in the
    # knowledge graph — a stable IRI, not a URL we fetch. The W3C-
    # recommended scheme for entity IRIs is `http://`.
    iri = f"http://data.fontem.eu/id/{cls}/{node.get('gmr_id', '')}"  # NOSONAR
    return {
        "iri": iri,
        "class": cls,
        "label": label,
        "facts": facts,
        "links": {"profile": f"/{_route_for_class(cls)}/{entity_id}" if entity_id else None},
    }


def _route_for_class(cls: str) -> str:
    return {
        "Company": "company",
        "Authority": "authority",
        "Person": "person",
        "Lobbyist": "lobbyist",
        "NUTSRegion": "nuts",
        "CohesionProject": "cohesion",
        "SanctionedEntity": "sanctioned",
    }.get(cls, cls.lower())


@router.get("/resolve")
@inject
def resolve_mention(
    iri: str = Query(..., description="Full Fontem IRI to resolve"),
    *,
    neo4j: FromDishka[Neo4jClient],
) -> dict[str, Any]:
    """Resolve an IRI to a side-panel summary."""
    cls, uid = _parse_iri(iri)
    with neo4j.session() as session:
        # Single-node lookup keyed by `gmr_id` — the convention the
        # whole graph already uses for stable, content-addressable
        # identifiers (see src/etl/gmr_id.py). For NUTSRegion we fall
        # back to `nuts_code` since regions historically did not get a
        # gmr_id stamped during the early loaders; the new loader does
        # both, so this fallback can be retired post-migration.
        result = session.run(
            f"MATCH (n:{cls}) "
            "WHERE n.gmr_id = $uid OR n.id = $uid OR n.nuts_code = $uid "
            "RETURN n LIMIT 1",
            uid=uid,
        ).single()
        if not result:
            raise HTTPException(status_code=404, detail="mention target not found")
        node = dict(result["n"])
    return _node_to_panel(cls, node)
