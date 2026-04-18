"""Unit tests for GraphDataQualitySource — focus on wiring from mocked
Neo4j responses into the dict shape the API layer expects. Cypher
correctness is validated live against staging/prod."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.data.graph.graph_data_quality import GraphDataQualitySource


def _make_source(session_answers):
    """Build a GraphDataQualitySource whose session.run() returns answers
    from the given list in order. Each answer is a dict; Session .single()
    returns it, .data() returns [it]."""
    neo4j = MagicMock()
    session = MagicMock()
    neo4j.session.return_value.__enter__ = MagicMock(return_value=session)
    neo4j.session.return_value.__exit__ = MagicMock(return_value=False)

    answers = iter(session_answers)

    def _run(*_args, **_kwargs):
        answer = next(answers)
        result = MagicMock()
        if isinstance(answer, list):
            result.data.return_value = answer
            result.single.return_value = answer[0] if answer else None
        else:
            result.single.return_value = answer
            result.data.return_value = [answer]
        return result

    session.run.side_effect = _run
    return GraphDataQualitySource(neo4j), session


def test_get_connectedness_shape():
    """Distribution fills in zeros for empty buckets; stats + hubs
    bubble straight through."""
    # Query 1: bucketed distribution (only some buckets populated)
    distribution_rows = [
        {"bucket": 0, "nodes": 40},
        {"bucket": 1, "nodes": 35},
        {"bucket": 3, "nodes": 15},
        {"bucket": 10, "nodes": 8},
        {"bucket": 30, "nodes": 2},
    ]
    # Query 2: summary stats (single row)
    stats_row = {
        "total_nodes": 100,
        "mean_degree": 0.8,
        "median_degree": 1.0,
        "max_degree": 20,
    }
    # Query 3: total edges count (single row)
    edges_row = {"n": 80}
    # Query 4: top hubs (list)
    hubs_rows = [
        {"labels": ["NUTSRegion"], "id": "Italia", "degree": 20},
    ]

    source, _ = _make_source([
        distribution_rows,
        stats_row,
        edges_row,
        hubs_rows,
    ])

    result = source.get_connectedness()

    assert result["stats"]["total_nodes"] == 100
    assert result["stats"]["total_edges"] == 80
    assert result["stats"]["orphan_count"] == 40
    assert result["stats"]["mean_degree"] == 0.8
    assert result["stats"]["median_degree"] == 1.0
    assert result["stats"]["max_degree"] == 20

    # All 10 buckets present, zero-filled where source didn't return a row
    assert len(result["distribution"]) == 10
    buckets = {b["bucket"]: b for b in result["distribution"]}
    assert buckets[0]["nodes"] == 40
    assert buckets[1]["nodes"] == 35
    assert buckets[100]["nodes"] == 0
    assert buckets[999999]["nodes"] == 0
    assert buckets[999999]["label"] == "10000+"

    assert len(result["hubs"]) == 1
    assert result["hubs"][0]["id"] == "Italia"


def test_get_connectedness_empty_graph():
    """Empty graph returns zeros everywhere, no division-by-zero."""
    source, _ = _make_source([
        [],  # distribution rows
        {"total_nodes": 0, "mean_degree": None, "median_degree": None, "max_degree": None},
        {"n": 0},
        [],  # hubs
    ])

    result = source.get_connectedness()

    assert result["stats"]["total_nodes"] == 0
    assert result["stats"]["total_edges"] == 0
    assert result["stats"]["orphan_count"] == 0
    assert result["stats"]["mean_degree"] == 0
    assert result["stats"]["median_degree"] == 0
    assert result["stats"]["max_degree"] == 0
    assert all(b["nodes"] == 0 for b in result["distribution"])
    assert result["hubs"] == []
