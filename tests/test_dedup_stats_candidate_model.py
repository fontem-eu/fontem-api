"""The dedup panel counts the entity-resolution funnel.

It used to count `SAME_AS {reviewed: false}` against `{reviewed: true}`,
from when a proposal and an assertion were the same edge distinguished
by a flag. They are now separate relationship types — :SAME_AS_CANDIDATE
for a proposal, :SAME_AS for an approved equivalence — so both of those
counts return 0 forever.

That is the failure mode worth a test: the panel would render an empty
queue rather than a broken one, and an empty review queue is exactly
what a working system looks like.
"""

from unittest.mock import MagicMock

from src.data.graph.graph_data_quality import GraphDataQualitySource


def _source(row):
    session = MagicMock()
    session.run.return_value.single.return_value = row
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=None)
    neo4j = MagicMock()
    neo4j.session = MagicMock(return_value=ctx)

    src = GraphDataQualitySource.__new__(GraphDataQualitySource)
    src._neo4j = neo4j  # pylint: disable=protected-access
    return src, session


def test_counts_the_whole_funnel():
    src, _ = _source(
        {"pending": 900_000, "declined": 12, "asserted": 40, "corrected": 3}
    )
    stats = src.get_dedup_stats()
    assert stats["pending"] == 900_000
    assert stats["declined"] == 12
    assert stats["asserted"] == 40
    assert stats["corrected"] == 3
    # total is what a human still has to get through plus what they have
    # settled — corrections are not part of the queue.
    assert stats["total"] == 900_052


def test_reviewed_means_everything_a_human_ruled_on():
    """The existing panel renders `reviewed`; it must keep meaning
    'decided', not 'approved'."""
    src, _ = _source(
        {"pending": 1, "declined": 7, "asserted": 5, "corrected": 0}
    )
    assert src.get_dedup_stats()["reviewed"] == 12


def test_queries_the_candidate_model_not_the_reviewed_flag():
    """The regression: `reviewed` no longer exists on any edge, so a
    query using it silently reports an empty queue."""
    src, session = _source(
        {"pending": 0, "declined": 0, "asserted": 0, "corrected": 0}
    )
    src.get_dedup_stats()
    cypher = session.run.call_args[0][0]
    assert "SAME_AS_CANDIDATE" in cypher
    assert "NOT_SAME_AS" in cypher
    assert "reviewed" not in cypher


def test_empty_graph_reports_zeros_not_an_error():
    src, _ = _source(
        {"pending": 0, "declined": 0, "asserted": 0, "corrected": 0}
    )
    stats = src.get_dedup_stats()
    assert stats["total"] == 0
    assert stats["pending"] == 0
