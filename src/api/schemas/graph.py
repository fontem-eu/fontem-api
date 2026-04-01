"""
Pydantic response models for the graph explorer API.
"""
from __future__ import annotations

from pydantic import BaseModel


class GraphNode(BaseModel):
    """A node in the graph response."""
    id: str
    label: str
    type: str
    properties: dict = {}


class GraphEdge(BaseModel):
    """An edge in the graph response."""
    source: str
    target: str
    type: str
    properties: dict = {}


class GraphResponse(BaseModel):
    """Full graph traversal response."""
    center: GraphNode
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool = False
    total_available: int = 0


class PathResponse(BaseModel):
    """Path finding response."""
    from_node: GraphNode
    to_node: GraphNode
    paths: list[PathDetail]
    shortest_length: int | None = None


class PathDetail(BaseModel):
    """A single path between two entities."""
    length: int
    node_ids: list[str]
    edges: list[GraphEdge]


# Rebuild PathResponse now that PathDetail is defined
PathResponse.model_rebuild()
