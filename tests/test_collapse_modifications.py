"""The modification-collapse pass emits current_value / is_current /
contract_key rollups so aggregations count each contract once."""
# pylint: disable=missing-function-docstring
from unittest.mock import MagicMock

from src.etl import collapse_modifications


def _mock_log():
    log = MagicMock()
    emit = MagicMock()
    log.batch.return_value.__enter__ = MagicMock(return_value=emit)
    log.batch.return_value.__exit__ = MagicMock(return_value=False)
    return log, emit


def _driver_returning(rows_by_query):
    """A driver whose session.run returns canned rows keyed by which cohort
    query ran (matched on a distinctive substring)."""
    session = MagicMock()

    def _run(query, **_kw):
        for needle, rows in rows_by_query.items():
            if needle in query:
                return rows
        return []
    session.run.side_effect = _run
    driver = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return driver


def test_emits_rollup_payloads_for_each_cohort():
    rows = {
        # cohort 1: award with modifications -> canonical, amended value
        "EXISTS { (:Contract)-[:MODIFIES]->(a) }": [
            {"id": "award-1", "contract_key": "proc:P1",
             "current_value": 1500.0, "is_current": True},
        ],
        # cohort 2: linked modification -> superseded
        "MODIFIES]->(a:Contract)\n": [
            {"id": "mod-1", "contract_key": "proc:P1",
             "current_value": 1500.0, "is_current": False},
        ],
        # cohort 3: orphan modification group -> one canonical
        "NOT (m)-[:MODIFIES]->(:Contract)": [
            {"id": "orphan-new", "contract_key": "modpub:X",
             "current_value": 900.0, "is_current": True},
            {"id": "orphan-old", "contract_key": "modpub:X",
             "current_value": 800.0, "is_current": False},
        ],
    }
    log, emit = _mock_log()
    n = collapse_modifications.collapse_modifications(
        _driver_returning(rows), log, batch_size=500)

    assert n == 4
    by_id = {c.kwargs["payload"]["ted_notice_id"]: c.kwargs["payload"]
             for c in emit.upsert.call_args_list}
    # every emit is an UpsertContract rollup carrying exactly the 3 fields
    for c in emit.upsert.call_args_list:
        assert c.args[0] == "UpsertContract"
        assert set(c.kwargs["payload"]) == {
            "ted_notice_id", "contract_key", "current_value", "is_current"}
    # canonical award carries the amended (current) value
    assert by_id["award-1"]["is_current"] is True
    assert by_id["award-1"]["current_value"] == 1500.0
    # the linked modification is superseded
    assert by_id["mod-1"]["is_current"] is False
    # exactly one orphan sibling is canonical (the latest)
    assert by_id["orphan-new"]["is_current"] is True
    assert by_id["orphan-old"]["is_current"] is False


def test_no_contracts_emits_nothing():
    log, emit = _mock_log()
    n = collapse_modifications.collapse_modifications(_driver_returning({}), log)
    assert n == 0
    emit.upsert.assert_not_called()


# ── derive_contract_key: the Python mirror of the Cypher grouping ──
# The loader stamps this at emit time; the three cohort queries above
# re-derive it from node props. These pin the coalesce order so the
# two derivations cannot drift apart silently.


def test_key_award_prefers_procedure_id():
    assert collapse_modifications.derive_contract_key(
        procedure_id="PROC-1", notice_type=None,
        modifies_publication_number=None,
        ted_publication_number="100-2026", ted_notice_id="uuid-1",
    ) == "PROC-1"


def test_key_award_without_procedure_uses_publication_number():
    assert collapse_modifications.derive_contract_key(
        procedure_id=None, notice_type="can-standard",
        modifies_publication_number=None,
        ted_publication_number="100-2026", ted_notice_id="uuid-1",
    ) == "100-2026"


def test_key_modification_uses_modifies_ref_not_own_pubnum():
    # A can-modif groups under the notice it modifies — its OWN
    # publication-number must never become a contract identity.
    assert collapse_modifications.derive_contract_key(
        procedure_id=None, notice_type="can-modif",
        modifies_publication_number="111-2024",
        ted_publication_number="555-2026", ted_notice_id="uuid-2",
    ) == "111-2024"


def test_key_modification_prefers_procedure_id():
    assert collapse_modifications.derive_contract_key(
        procedure_id="PROC-9", notice_type="can-modif",
        modifies_publication_number="111-2024",
        ted_publication_number="555-2026", ted_notice_id="uuid-2",
    ) == "PROC-9"


def test_key_falls_back_to_notice_id():
    # Award with no pub-num yet (bulk skip_pub_num_lookup path)...
    assert collapse_modifications.derive_contract_key(
        procedure_id=None, notice_type=None,
        modifies_publication_number=None,
        ted_publication_number=None, ted_notice_id="uuid-3",
    ) == "uuid-3"
    # ...and an orphan modification with no modifies ref.
    assert collapse_modifications.derive_contract_key(
        procedure_id=None, notice_type="can-modif",
        modifies_publication_number=None,
        ted_publication_number="555-2026", ted_notice_id="uuid-4",
    ) == "uuid-4"


def test_award_and_modification_converge_when_pubnum_known():
    """An award and the modification that restates it MUST derive the same
    contract_key, or the sink projects them onto two :Contract entities.
    A modification references the award via modifies_publication_number =
    the award's ted_publication_number, so once the award's pub-number is
    known both sides land on it."""
    award_key = collapse_modifications.derive_contract_key(
        procedure_id=None, notice_type="can-standard",
        modifies_publication_number=None,
        ted_publication_number="734888-2023", ted_notice_id="award-uuid",
    )
    mod_key = collapse_modifications.derive_contract_key(
        procedure_id=None, notice_type="can-modif",
        modifies_publication_number="734888-2023",
        ted_publication_number="900-2024", ted_notice_id="mod-uuid",
    )
    assert award_key == mod_key == "734888-2023"


def test_award_key_diverges_from_its_modification_without_pubnum():
    """Regression pin for the duplicate-contract root cause: when the
    award is loaded with skip_pub_num_lookup its ted_publication_number is
    null, so its key falls back to ted_notice_id — a key the modification
    (which uses the award's publication-number) can NEVER reference. The
    two derive different keys, which is exactly what splits one real
    contract into a duplicate ted_notice_id pair. backfill_ted_publication
    _numbers converges the award onto the pub-number key once it is
    resolved; this test documents the divergence that convergence fixes."""
    award_key = collapse_modifications.derive_contract_key(
        procedure_id=None, notice_type="can-standard",
        modifies_publication_number=None,
        ted_publication_number=None, ted_notice_id="award-uuid",
    )
    mod_key = collapse_modifications.derive_contract_key(
        procedure_id=None, notice_type="can-modif",
        modifies_publication_number="734888-2023",
        ted_publication_number=None, ted_notice_id="mod-uuid",
    )
    assert award_key == "award-uuid"
    assert mod_key == "734888-2023"
    assert award_key != mod_key
