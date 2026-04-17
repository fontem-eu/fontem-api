"""Tests for the entity→NUTS linker."""
from unittest.mock import MagicMock

from src.etl.link_entities_to_nuts import (
    ENTITY_LABELS,
    LINK_LABEL_TEMPLATE,
    link_label,
    run,
)


def _mock_session(counts_per_query):
    """Build a mock session that returns ``counts_per_query[i]`` relationships
    created on the i-th ``run()`` call."""
    session = MagicMock()
    call = {"i": 0}

    def _run(*_args, **_kwargs):
        result = MagicMock()
        created = counts_per_query[call["i"]]
        call["i"] += 1
        result.consume.return_value.counters.relationships_created = created
        return result

    session.run.side_effect = _run
    return session


def _mock_driver(counts_per_query):
    driver = MagicMock()
    session = _mock_session(counts_per_query)
    driver.session.return_value.__enter__ = MagicMock(return_value=session)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return driver, session


def test_entity_labels_covers_company_authority_lobbyist():
    """The linker must cover the three non-CohesionProject entity types."""
    assert set(ENTITY_LABELS) == {"Company", "Authority", "Lobbyist"}


def test_link_label_runs_cypher_and_returns_count():
    """link_label executes the template and returns the created count."""
    session = _mock_session([42])
    created = link_label(session, "Company")
    assert created == 42
    used_query = session.run.call_args.args[0]
    assert "(e:Company)" in used_query
    assert "LOCATED_IN" in used_query
    assert "{level: 0, country_alpha3: a3}" in used_query


def test_link_label_is_idempotent_in_cypher():
    """Generated Cypher skips entities already LOCATED_IN a NUTSRegion."""
    # Not runtime, structural: the template must filter on NOT (e)-[:LOCATED_IN]
    assert "NOT (e)-[:LOCATED_IN]->(:NUTSRegion)" in LINK_LABEL_TEMPLATE


def test_link_label_matches_by_country_alpha3():
    """Join key is country_alpha3 on NUTSRegion vs country on entity."""
    # Both sides must agree on alpha-3 — verify the template binds a3 properly
    assert "e.country AS a3" in LINK_LABEL_TEMPLATE
    assert "country_alpha3: a3" in LINK_LABEL_TEMPLATE


def test_run_links_each_entity_label_once():
    """run() iterates through the three labels in order."""
    driver, session = _mock_driver([100, 50, 5])
    summary = run(driver)
    assert summary["counts"] == {"Company": 100, "Authority": 50, "Lobbyist": 5}
    # 3 run() calls, one per label
    assert session.run.call_count == 3
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
