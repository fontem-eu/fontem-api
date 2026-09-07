"""Finding and purging subjects stranded by the IRI encoding change.

The sink percent-encodes every subject IRI it writes (since 37af28e,
2026-06-07). quote() is idempotent on the encoded form and no input
maps back to the raw form, so a subject written before that commit is
unreachable: a Delete* naming it encodes to the LIVE subject and
deletes that instead. These stale twins never expire.

The dangerous mistake this script could make is purging a stranded
subject that has no live twin — that deletes the only copy of the data
rather than a duplicate. Most of what follows pins that it doesn't.
"""
# pylint: disable=protected-access
from unittest.mock import MagicMock
from urllib.parse import quote

import pytest

from src.etl import purge_stranded_subjects as psub

RAW = "http://data.fontem.eu/id/Listing/BJÖRN.ST"
ENC = "http://data.fontem.eu/id/Listing/BJ%C3%96RN.ST"
ASCII_IRI = "http://data.fontem.eu/id/Listing/BORG"
G = "http://data.fontem.eu/graph/listing"


class _Virtuoso:
    def __init__(self, subjects):
        self._subjects = subjects

    def query(self, _q):
        return [{"s": s} for s in self._subjects]


def _patch_sources(monkeypatch, subjects, on_event_log=None):
    """Swap the module's VirtuosoClient/EventLog names, not the classes.

    Patching `EventLog.from_env` on the real class mutates a type other
    test modules share. pylint then infers that attribute as our lambda
    everywhere, and test_load_eu_listings/test_load_gleif/
    test_load_us_financials start failing E1101 on their own
    `.call_count` assertions — eight findings in files this change never
    touched. Rebinding the names inside purge_stranded_subjects keeps
    the blast radius to this module.
    """
    virtuoso_cls = MagicMock()
    virtuoso_cls.from_env.return_value = _Virtuoso(subjects)
    monkeypatch.setattr(psub, "VirtuosoClient", virtuoso_cls)

    event_log_cls = MagicMock()
    if on_event_log is None:
        event_log_cls.from_env.return_value = MagicMock()
    else:
        event_log_cls.from_env.side_effect = lambda *a, **k: on_event_log()
    monkeypatch.setattr(psub, "EventLog", event_log_cls)


def test_is_stranded_only_for_unreachable_subjects():
    """The definition that everything else rests on: stranded means
    'quote() changes it', i.e. the sink can never write it again."""
    assert psub.is_stranded(RAW) is True
    assert psub.is_stranded(ENC) is False
    assert psub.is_stranded(ASCII_IRI) is False


def test_encoded_form_is_a_fixed_point():
    """Why a Delete* event cannot do this job: encoding the raw IRI
    yields the LIVE subject, so such an event would delete the record
    it was meant to preserve."""
    assert quote(RAW, safe=psub._IRI_SAFE) == ENC
    assert quote(ENC, safe=psub._IRI_SAFE) == ENC


def test_splits_stranded_by_whether_a_live_twin_exists():
    lone = "http://data.fontem.eu/id/Listing/ØRPHAN.CO"
    with_twin, without = psub.find_stranded(
        _Virtuoso([RAW, ENC, ASCII_IRI, lone]), G,
    )
    assert with_twin == [RAW]
    assert without == [lone]


def test_orphans_are_skipped_unless_explicitly_allowed(monkeypatch):
    """A stranded subject with no twin holds the only copy of its data.
    Purging it is a deletion, not a de-duplication, so it needs an
    explicit flag."""
    lone = "http://data.fontem.eu/id/Listing/ØRPHAN.CO"
    _patch_sources(monkeypatch, [RAW, ENC, lone])
    emitted = []
    monkeypatch.setattr(psub, "emit_purges",
                        lambda _log, _g, subs, **_k: emitted.extend(subs))

    psub.main(["--apply"])
    assert emitted == [RAW], "purged an orphan without --allow-orphans"

    emitted.clear()
    psub.main(["--apply", "--allow-orphans"])
    assert sorted(emitted) == sorted([RAW, lone])


def test_dry_run_emits_nothing(monkeypatch):
    """Default must be inert. This script deletes data by an IRI the
    normal rules cannot produce; running it by accident should cost
    nothing."""
    _patch_sources(monkeypatch, [RAW, ENC], on_event_log=lambda: pytest.fail(
        "dry run opened the event log"))
    called = []
    monkeypatch.setattr(psub, "emit_purges",
                        lambda *a, **k: called.append(a))
    psub.main([])
    assert not called


def test_reason_names_the_live_subject():
    """The audit trail should let a reader confirm the purge was a
    de-duplication without re-deriving the encoding."""
    log = MagicMock()
    batch = log.batch.return_value.__enter__.return_value

    psub.emit_purges(log, G, [RAW])

    # Read the mock's own call record rather than assigning a lambda to
    # batch.control. A raw lambda bound onto a mock attribute makes
    # pylint infer `.control` as that lambda for the whole run, and
    # test_load_eu_listings/test_load_gleif/test_load_us_financials —
    # which assert emit.control.call_count — then fail E1101 in files
    # this change never touched.
    assert batch.control.call_count == 1
    event_type, payload = batch.control.call_args.args
    assert event_type == "PurgeSubject"
    assert payload["subject_iri"] == RAW
    assert ENC in payload["reason"]
