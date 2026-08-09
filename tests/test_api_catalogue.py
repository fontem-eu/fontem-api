"""Tests for /catalogue — what the platform says it holds.

This endpoint exists because the assistant told a user Fontem holds no
demographic data while the platform carried eight population datasets. The
properties below are the ones that make that answer impossible again, so
they are asserted rather than assumed.
"""
from __future__ import annotations

# pylint: disable=missing-function-docstring

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.api.routers import catalogue as catalogue_router
from src.etl.data_description import DataDescription
from src.etl.registry import discover, undescribed


def _client(datasets=None, raises=None, configured=True):
    """An app with just the catalogue router and a stubbed stats source."""
    from fastapi import FastAPI  # pylint: disable=import-outside-toplevel

    app = FastAPI()
    app.include_router(catalogue_router.router)
    source = MagicMock()
    source.configured = configured
    if raises is not None:
        source.list_datasets = MagicMock(side_effect=raises)
    else:
        source.list_datasets = MagicMock(return_value=datasets or [])
    app.state.fontem_stats_source = source
    return TestClient(app)


DATASETS = [
    {"code": "demo_r_births", "label": "Live births", "theme": "population",
     "nuts_levels": [3], "time_unit": "year", "update_freq": "1 year",
     "enabled": True},
    {"code": "off_by_default", "label": "Disabled", "theme": "population",
     "nuts_levels": [2], "time_unit": "year", "update_freq": "1 year",
     "enabled": False},
]


def test_every_producer_is_described():
    """A loader missing from here is data the assistant will deny holding."""
    body = _client().get("/catalogue").json()
    assert body["counts"]["producers"] >= 15
    assert body["undescribed_producers"] == []


def test_undescribed_detects_a_loader_with_no_description(tmp_path):
    """Exercises the detector, not just the field.

    Asserting `undescribed_producers == []` on the live endpoint passes
    whether the check works or is hardcoded empty — which it did, until this
    test existed. A gap that cannot be detected is a gap that will grow.
    """
    (tmp_path / "load_described.py").write_text(
        "from src.etl.data_description import DataDescription\n"
        'DESCRIPTION = DataDescription(producer="p", label="L",'
        ' theme="t", summary="s")\n', encoding="utf-8")
    (tmp_path / "load_forgotten.py").write_text(
        '"""A loader nobody described."""\n', encoding="utf-8")

    assert undescribed(tmp_path) == ["load_forgotten"]
    assert [d.producer for d in discover(tmp_path)] == ["p"]


def test_registry_skips_a_description_it_cannot_evaluate(tmp_path):
    """Non-literal fields are refused rather than guessed at."""
    (tmp_path / "load_computed.py").write_text(
        "from src.etl.data_description import DataDescription\n"
        "NAME = 'x'\n"
        'DESCRIPTION = DataDescription(producer=NAME, label="L",'
        ' theme="t", summary="s")\n', encoding="utf-8")
    assert discover(tmp_path) == []
    assert undescribed(tmp_path) == ["load_computed"]


def test_producers_carry_the_fields_an_assistant_needs():
    body = _client().get("/catalogue").json()
    ted = next(p for p in body["producers"] if p["producer"] == "load_ted_contracts")
    assert ted["theme"] == "procurement"
    assert ted["summary"]
    # Coverage is what stops "0 results" being reported as "absent from the
    # world" — the single most load-bearing field here.
    assert "threshold" in ted["coverage"].lower()
    assert ted["answers"], "a producer with no answerable questions cannot be routed to"


def test_demographic_data_is_discoverable():
    """The regression that motivated the endpoint."""
    body = _client(datasets=DATASETS).get("/catalogue").json()
    themes = {d["theme"] for d in body["datasets"]}
    assert "population" in themes


def test_disabled_datasets_are_omitted():
    """Advertising a disabled dataset is the mirror-image failure."""
    body = _client(datasets=DATASETS).get("/catalogue").json()
    codes = {d["code"] for d in body["datasets"]}
    assert "demo_r_births" in codes
    assert "off_by_default" not in codes


def test_unconfigured_stats_store_still_returns_producers():
    """Half a catalogue beats a 503 that reads as 'the platform holds nothing'."""
    resp = _client(configured=False).get("/catalogue")
    assert resp.status_code == 200
    body = resp.json()
    assert body["datasets"] == []
    assert body["counts"]["producers"] >= 15


def test_stats_store_error_degrades_instead_of_500ing():
    resp = _client(raises=OSError("connection refused")).get("/catalogue")
    assert resp.status_code == 200
    assert resp.json()["datasets"] == []


def test_unexpected_error_is_not_swallowed():
    """The handler names what it tolerates; anything else must surface.

    A bug inside the stats source should fail loudly in CI, not degrade to
    "no datasets" and ship a silently smaller catalogue.
    """
    client = _client(raises=ZeroDivisionError("bug in the source"))
    try:
        client.get("/catalogue")
    except ZeroDivisionError:
        return
    raise AssertionError("unexpected exception was swallowed")


def test_description_rejects_unknown_fields():
    """The registry skips a malformed record rather than serving half of one."""
    try:
        DataDescription(producer="x", label="X", theme="t", summary="s",
                        nonsense="boom")
    except TypeError:
        return
    raise AssertionError("DataDescription accepted an unknown field")
