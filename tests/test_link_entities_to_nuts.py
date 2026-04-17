"""Tests for the entity→NUTS linker."""
from unittest.mock import MagicMock

from src.etl.link_entities_to_nuts import (
    ENTITY_LABELS,
    LINK_LABEL_TEMPLATE,
    link_label,
    run,
)


def _mock_session(counts_per_label):
    """Build a mock session that simulates ``count(r)`` growing by
    ``counts_per_label[i]`` after each label's link query runs.

    Per label we expect 3 session.run() calls:
      1. count(r) BEFORE    → returns 0
      2. link query         → no counter needed
      3. count(r) AFTER     → returns counts_per_label[i]
    """
    session = MagicMock()
    state = {"label_idx": 0, "step": 0}

    def _run(query, *_args, **_kwargs):
        result = MagicMock()
        if "count(r)" in query:
            if state["step"] % 2 == 0:  # BEFORE → 0
                result.single.return_value = {"n": 0}
            else:  # AFTER → label's count
                result.single.return_value = {"n": counts_per_label[state["label_idx"]]}
                state["label_idx"] += 1
            state["step"] += 1
        # Link queries return a result that supports .consume()
        return result

    session.run.side_effect = _run
    return session


def _mock_driver(counts_per_label):
    driver = MagicMock()
    session = _mock_session(counts_per_label)
    driver.session.return_value.__enter__ = MagicMock(return_value=session)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return driver, session


def test_entity_labels_covers_company_authority_lobbyist():
    """The linker must cover the three non-CohesionProject entity types."""
    assert set(ENTITY_LABELS) == {"Company", "Authority", "Lobbyist"}


def test_link_label_runs_cypher_and_returns_count_diff():
    """link_label diffs count(r) before/after to compute created edges."""
    session = _mock_session([42])
    created = link_label(session, "Company")
    assert created == 42
    # Queries used: count(before), link, count(after)
    queries = [c.args[0] for c in session.run.call_args_list]
    assert len(queries) == 3
    assert "count(r)" in queries[0]
    assert "(e:Company)" in queries[1]
    assert "LOCATED_IN" in queries[1]
    assert "count(r)" in queries[2]


def test_link_label_is_idempotent_in_cypher():
    """Generated Cypher skips entities already LOCATED_IN a NUTSRegion."""
    assert "NOT (e)-[:LOCATED_IN]->(:NUTSRegion)" in LINK_LABEL_TEMPLATE


def test_link_label_matches_by_country_alpha3():
    """Join key is country_alpha3 on NUTSRegion vs country on entity."""
    assert "e.country AS a3" in LINK_LABEL_TEMPLATE
    assert "country_alpha3: a3" in LINK_LABEL_TEMPLATE


def test_link_label_uses_in_transactions_batching():
    """Query must use CALL ... IN TRANSACTIONS so large datasets don't
    blow past the per-tx memory cap (256 MB default)."""
    assert "CALL" in LINK_LABEL_TEMPLATE
    assert "IN TRANSACTIONS OF" in LINK_LABEL_TEMPLATE


def test_run_links_each_entity_label_once():
    """run() iterates through the three labels in order."""
    driver, session = _mock_driver([100, 50, 5])
    summary = run(driver)
    assert summary["counts"] == {"Company": 100, "Authority": 50, "Lobbyist": 5}
    # 3 labels × 3 queries each = 9 session.run calls
    assert session.run.call_count == 9
    queries = [c.args[0] for c in session.run.call_args_list]
    assert any("(e:Company)" in q for q in queries)
    assert any("(e:Authority)" in q for q in queries)
    assert any("(e:Lobbyist)" in q for q in queries)


def test_run_reports_elapsed_time():
    """Summary includes a non-negative elapsed_s field."""
    driver, _ = _mock_driver([1, 1, 1])
    summary = run(driver)
    assert "elapsed_s" in summary
    assert summary["elapsed_s"] >= 0


def test_run_handles_zero_matches_gracefully():
    """If nothing gets linked, the summary reports zeros (not an error)."""
    driver, _ = _mock_driver([0, 0, 0])
    summary = run(driver)
    assert summary["counts"] == {"Company": 0, "Authority": 0, "Lobbyist": 0}
