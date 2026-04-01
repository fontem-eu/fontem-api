"""
Graph Explorer API Router
==========================
Endpoints for traversing the entity graph and finding paths
between entities. Used by the Cytoscape.js graph explorer UI.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..dependencies import get_neo4j_client
from ..schemas.graph import (
    GraphEdge,
    GraphNode,
    GraphResponse,
    PathDetail,
    PathResponse,
)

router = APIRouter(prefix="/graph", tags=["graph"])

NODE_CAP = 500
_EXCLUDED_RELS = {"REPORTED", "LISTED_AS", "CATEGORIZED_AS", "SAME_AS"}
_LABEL_ID = {
    "Company": "gmr_id",
    "Authority": "authority_id",
    "Person": "person_id",
    "Contract": "ted_notice_id",
}


# ── Neo4j helpers ─────────────────────────────────────────────


def _detect_entity(session, entity_id: str) -> tuple[str, str] | None:
    """Return (label, id_property) or None if entity not found."""
    for label, id_prop in _LABEL_ID.items():
        result = session.run(
            f"MATCH (n:{label} {{{id_prop}: $eid}}) "
            f"RETURN labels(n)[0] AS label LIMIT 1",
            eid=entity_id,
        ).single()
        if result:
            return label, id_prop
    return None


def _fetch_node(session, label: str, id_prop: str, eid: str) -> GraphNode:
    """Fetch a single node and convert to GraphNode."""
    rec = session.run(
        f"MATCH (n:{label} {{{id_prop}: $eid}}) RETURN n LIMIT 1",
        eid=eid,
    ).single()
    return _node_to_graph_node(rec["n"])


def _node_id(node) -> str:
    """Extract the canonical ID from a Neo4j node."""
    props = dict(node)
    return (
        props.get("gmr_id")
        or props.get("authority_id")
        or props.get("person_id")
        or props.get("ted_notice_id")
        or str(node.element_id)
    )


def _clean_props(props: dict) -> dict:
    """Remove None values and internal IDs from properties."""
    skip = {"gmr_id", "authority_id", "person_id", "ted_notice_id"}
    return {
        k: v for k, v in props.items()
        if v is not None and k not in skip
    }


def _node_to_graph_node(node) -> GraphNode:
    """Convert a raw Neo4j node to a GraphNode."""
    labels = list(node.labels)
    label = labels[0] if labels else "Unknown"
    props = dict(node)
    nid = _node_id(node)
    display = props.get("name") or props.get("title") or nid
    return GraphNode(
        id=nid, label=display, type=label,
        properties=_clean_props(props),
    )


def _edge_to_graph_edge(rel) -> GraphEdge:
    """Convert a raw Neo4j relationship to a GraphEdge."""
    props = dict(rel)
    return GraphEdge(
        source=_node_id(rel.start_node),
        target=_node_id(rel.end_node),
        type=rel.type,
        properties={k: v for k, v in props.items() if v is not None},
    )


def _path_to_detail(path) -> PathDetail:
    """Convert a Neo4j path to a PathDetail."""
    return PathDetail(
        length=len(path.relationships),
        node_ids=[_node_id(n) for n in path.nodes],
        edges=[_edge_to_graph_edge(r) for r in path.relationships],
    )


# ── Traversal core ───────────────────────────────────────────


def _collect_paths(result, center_node):
    """Extract deduplicated nodes and edges from path records."""
    nodes_map: dict[str, GraphNode] = {center_node.id: center_node}
    edges_set: set[tuple] = set()
    edges_list: list[GraphEdge] = []

    for record in result:
        path = record["path"]
        for node in path.nodes:
            nd = _node_to_graph_node(node)
            if nd.id not in nodes_map:
                nodes_map[nd.id] = nd
        for rel in path.relationships:
            ed = _edge_to_graph_edge(rel)
            edge_key = (ed.source, ed.target, ed.type)
            if edge_key not in edges_set:
                edges_set.add(edge_key)
                edges_list.append(ed)

    return nodes_map, edges_list


def _apply_filters(nodes_map, edges_list, type_filter, center_label):
    """Apply type filter and node cap, return final (nodes, edges, truncated, total)."""
    if type_filter:
        allowed = type_filter | {center_label}
        nodes_map = {
            nid: nd for nid, nd in nodes_map.items()
            if nd.type in allowed
        }
        edges_list = [
            e for e in edges_list
            if e.source in nodes_map and e.target in nodes_map
        ]

    total = len(nodes_map)
    truncated = total > NODE_CAP

    if truncated:
        kept = set(list(nodes_map.keys())[:NODE_CAP])
        nodes_map = {
            nid: nd for nid, nd in nodes_map.items() if nid in kept
        }
        edges_list = [
            e for e in edges_list
            if e.source in nodes_map and e.target in nodes_map
        ]

    return nodes_map, edges_list, truncated, total


# ── Path-finding core ────────────────────────────────────────


def _find_shortest(session, endpoints, max_depth):
    """Find the shortest path between two detected entities.

    endpoints: dict with from_label, from_prop, from_id, to_label, to_prop, to_id
    Returns (PathDetail, length) or (None, None).
    """
    result = session.run(
        f"MATCH (a:{endpoints['from_label']} "
        f"  {{{endpoints['from_prop']}: $fid}}), "
        f"      (b:{endpoints['to_label']} "
        f"  {{{endpoints['to_prop']}: $tid}}) "
        f"MATCH path = shortestPath((a)-[*..{max_depth}]-(b)) "
        f"WHERE NONE(r IN relationships(path) "
        f"  WHERE type(r) IN $excluded) "
        f"RETURN path",
        fid=endpoints["from_id"], tid=endpoints["to_id"],
        excluded=list(_EXCLUDED_RELS),
    ).single()
    if not result:
        return None, None
    return _path_to_detail(result["path"]), len(result["path"].relationships)


def _find_extra_paths(session, endpoints, shortest_len, search_depth):
    """Find additional paths longer than shortest up to search_depth."""
    results = session.run(
        f"MATCH (a:{endpoints['from_label']} "
        f"  {{{endpoints['from_prop']}: $fid}}), "
        f"      (b:{endpoints['to_label']} "
        f"  {{{endpoints['to_prop']}: $tid}}) "
        f"MATCH path = (a)-[*{shortest_len + 1}..{search_depth}]-(b) "
        f"WHERE NONE(r IN relationships(path) "
        f"  WHERE type(r) IN $excluded) "
        f"  AND ALL(n IN nodes(path) "
        f"    WHERE single(x IN nodes(path) WHERE x = n)) "
        f"RETURN path ORDER BY length(path) LIMIT 9",
        fid=endpoints["from_id"], tid=endpoints["to_id"],
        excluded=list(_EXCLUDED_RELS),
    ).data()
    return [_path_to_detail(rec["path"]) for rec in results]


# ── Endpoints ─────────────────────────────────────────────────


@router.get(
    "/paths/find",
    response_model=PathResponse,
    summary="Find paths between two entities",
)
def graph_paths(
    from_id: str = Query(..., alias="from", description="Source entity ID"),
    to_id: str = Query(..., alias="to", description="Target entity ID"),
    max_depth: int = Query(5, ge=1, le=10, description="Max path length"),
    extra: int = Query(2, ge=0, le=5, description="Extra hops beyond shortest"),
    neo4j=Depends(get_neo4j_client),
):
    """Find shortest and near-shortest paths between two entities."""
    with neo4j.session() as session:
        from_det = _detect_entity(session, from_id)
        to_det = _detect_entity(session, to_id)

        if not from_det:
            raise HTTPException(404, f"Entity not found: {from_id}")
        if not to_det:
            raise HTTPException(404, f"Entity not found: {to_id}")

        from_node = _fetch_node(session, *from_det, from_id)
        to_node = _fetch_node(session, *to_det, to_id)

        ep = {
            "from_label": from_det[0], "from_prop": from_det[1],
            "from_id": from_id,
            "to_label": to_det[0], "to_prop": to_det[1],
            "to_id": to_id,
        }
        shortest_detail, shortest_len = _find_shortest(session, ep, max_depth)
        if not shortest_detail:
            return PathResponse(
                from_node=from_node, to_node=to_node,
                paths=[], shortest_length=None,
            )

        paths = [shortest_detail]
        search_depth = min(shortest_len + extra, max_depth)
        if search_depth > shortest_len:
            paths.extend(_find_extra_paths(
                session, ep, shortest_len, search_depth,
            ))

        return PathResponse(
            from_node=from_node, to_node=to_node,
            paths=paths, shortest_length=shortest_len,
        )


@router.get(
    "/{entity_id}",
    response_model=GraphResponse,
    summary="Traverse the entity graph from any starting node",
)
def graph_traverse(
    entity_id: str,
    depth: int = Query(1, ge=0, le=3, description="Traversal depth (0-3)"),
    types: str | None = Query(
        None,
        description="Comma-separated node types to include",
    ),
    neo4j=Depends(get_neo4j_client),
):
    """Variable-depth graph traversal starting from any entity type."""
    type_filter = (
        {t.strip() for t in types.split(",") if t.strip()}
        if types
        else None
    )

    with neo4j.session() as session:
        detected = _detect_entity(session, entity_id)
        if not detected:
            return GraphResponse(
                center=GraphNode(
                    id=entity_id, label=entity_id, type="Unknown",
                ),
                nodes=[], edges=[],
                truncated=False, total_available=0,
            )

        center_label, center_id_prop = detected
        center_node = _fetch_node(session, center_label, center_id_prop, entity_id)

        if depth == 0:
            return GraphResponse(
                center=center_node, nodes=[center_node], edges=[],
                truncated=False, total_available=1,
            )

        result = session.run(
            f"MATCH (start:{center_label} {{{center_id_prop}: $eid}}) "
            f"MATCH path = (start)-[*1..{depth}]-(neighbor) "
            f"WHERE NONE(r IN relationships(path) "
            f"  WHERE type(r) IN $excluded) "
            f"RETURN path",
            eid=entity_id,
            excluded=list(_EXCLUDED_RELS),
        )

        nodes_map, edges_list = _collect_paths(result, center_node)
        nodes_map, edges_list, truncated, total = _apply_filters(
            nodes_map, edges_list, type_filter, center_label,
        )

        return GraphResponse(
            center=center_node,
            nodes=list(nodes_map.values()),
            edges=edges_list,
            truncated=truncated,
            total_available=total,
        )
