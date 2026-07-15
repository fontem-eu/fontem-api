"""The Contract/Notice projection: drains each phase and runs all four."""
# pylint: disable=missing-function-docstring,protected-access
from unittest.mock import MagicMock

from src.etl import project_contracts


def _driver_returning(done_sequences):
    """A driver whose execute_write returns successive 'done' counts, keyed by
    which phase query runs (matched on a distinctive substring)."""
    session = MagicMock()
    order = []

    def _execute_write(fn):
        # fn is: lambda tx: tx.run(cypher, batch=..).single()["done"]
        tx = MagicMock()
        captured = {}

        def _run(cypher, **_kw):
            captured["cypher"] = cypher
            res = MagicMock()
            # pick the sequence for whichever phase this cypher belongs to
            for needle, seq in done_sequences.items():
                if needle in cypher:
                    idx = order.count(needle)
                    order.append(needle)
                    val = seq[min(idx, len(seq) - 1)]
                    res.single.return_value = {"done": val}
                    return res
            res.single.return_value = {"done": 0}
            return res
        tx.run.side_effect = _run
        return fn(tx)

    session.execute_write.side_effect = _execute_write
    driver = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return driver


def test_migrate_runs_all_four_phases_and_reports_totals():
    driver = _driver_returning({
        "SET n:Notice": [5000, 5000, 200],           # relabel: two full + a partial
        "MERGE (c:Contract {contract_key": [5000, 0],  # project
        "c.notice_count IS NULL": [3000, 0],          # finalize
        "r:AWARDED_TO|AWARDED": [1000, 0],            # strip
    })
    out = project_contracts.migrate(driver, batch=5000)
    assert out["relabelled"] == 10200
    assert out["projected"] == 5000
    assert out["finalized"] == 3000
    assert out["stripped"] == 1000


def test_run_until_drained_stops_on_partial_batch():
    driver = _driver_returning({"SET n:Notice": [5000, 4999]})
    n = project_contracts._run_until_drained(  # pylint: disable=protected-access
        driver, project_contracts._RELABEL, "relabel", 5000)  # pylint: disable=protected-access
    assert n == 9999  # stops when a batch returns < batch size
