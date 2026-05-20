"""Tests for the NUTS reference-hierarchy loader (post-event-log)."""
from unittest.mock import MagicMock

import pytest
import httpx

from src.etl import load_nuts
from src.etl.load_nuts import (
    _parent_code,
    emit_nuts,
    main,
    parse_nuts_csv,
)


SAMPLE_CSV = """NUTS_ID,NUTS_NAME,LEVL_CODE,CNTR_CODE
FR,France,0,FR
FR1,Île-de-France,1,FR
FR10,Île-de-France,2,FR
FR101,Paris,3,FR
DE,Germany,0,DE
DE1,Baden-Württemberg,1,DE
DE11,Stuttgart,2,DE
DE111,Stuttgart Stadtkreis,3,DE
"""


# ── Parsing ────────────────────────────────────────────────────────


def test_parse_yields_all_levels():
    rows = list(parse_nuts_csv(SAMPLE_CSV))
    levels = sorted({r["level"] for r in rows})
    assert levels == [0, 1, 2, 3]
    assert len(rows) == 8


def test_parse_level_is_derived_from_code_length():
    rows = {r["code"]: r for r in parse_nuts_csv(SAMPLE_CSV)}
    assert rows["FR"]["level"] == 0
    assert rows["FR1"]["level"] == 1
    assert rows["FR10"]["level"] == 2
    assert rows["FR101"]["level"] == 3


def test_parse_parent_code_traces_hierarchy():
    rows = {r["code"]: r for r in parse_nuts_csv(SAMPLE_CSV)}
    assert rows["FR"]["parent"] is None
    assert rows["FR1"]["parent"] == "FR"
    assert rows["FR10"]["parent"] == "FR1"
    assert rows["FR101"]["parent"] == "FR10"


def test_parse_rejects_rows_with_invalid_code_length():
    bad_csv = """NUTS_ID,NUTS_NAME
,empty
X,too short
ABCDEF,too long
FR,France
"""
    rows = list(parse_nuts_csv(bad_csv))
    assert [r["code"] for r in rows] == ["FR"]


def test_parse_falls_back_to_code_when_name_missing():
    csv_no_name = "NUTS_ID\nFR\n"
    rows = list(parse_nuts_csv(csv_no_name))
    assert rows[0]["name"] == "FR"


def test_parse_raises_when_code_column_missing():
    with pytest.raises(ValueError):
        list(parse_nuts_csv("FOO,BAR\na,b\n"))


# ── Parent-code derivation ─────────────────────────────────────────


def test_parent_code_for_level_0_is_none():
    assert _parent_code("FR") is None


def test_parent_code_strips_last_char():
    assert _parent_code("FR101") == "FR10"
    assert _parent_code("FR10") == "FR1"
    assert _parent_code("FR1") == "FR"


# ── Event emit ─────────────────────────────────────────────────────


def _mock_log():
    log = MagicMock()
    emit = MagicMock()
    log.batch.return_value.__enter__ = MagicMock(return_value=emit)
    log.batch.return_value.__exit__ = MagicMock(return_value=False)
    return log, emit


def test_emit_one_event_per_region():
    log, emit = _mock_log()
    regions = list(parse_nuts_csv(SAMPLE_CSV))
    summary = emit_nuts(log, regions)
    assert summary["total"] == 8
    assert emit.upsert.call_count == 8
    assert all(c.args[0] == "UpsertTaxonomyCode"
               for c in emit.upsert.call_args_list)


def test_emit_payload_carries_parent_and_level():
    log, emit = _mock_log()
    regions = list(parse_nuts_csv(SAMPLE_CSV))
    emit_nuts(log, regions)
    payloads = {
        c.kwargs["payload"]["code"]: c.kwargs["payload"]
        for c in emit.upsert.call_args_list
    }
    assert payloads["FR"]["system"] == "nuts"
    assert payloads["FR"]["level"] == 0
    assert "parent_code" not in payloads["FR"]
    assert payloads["FR101"]["parent_code"] == "FR10"
    assert payloads["FR101"]["level"] == 3


def test_emit_summary_breaks_down_by_level():
    log, _emit = _mock_log()
    regions = list(parse_nuts_csv(SAMPLE_CSV))
    summary = emit_nuts(log, regions)
    assert summary["by_level"] == {0: 2, 1: 2, 2: 2, 3: 2}


# ── CLI ────────────────────────────────────────────────────────────


def test_main_aborts_when_download_fails(monkeypatch):
    def fake_download():
        raise httpx.HTTPError("network down")

    monkeypatch.setattr(load_nuts, "download_nuts_csv", fake_download)
    monkeypatch.setattr(load_nuts.EventLog, "from_env",
                        classmethod(lambda cls: MagicMock()))
    with pytest.raises(httpx.HTTPError):
        main(argv=[])


def test_main_aborts_when_parsed_zero_regions(tmp_path, monkeypatch):
    empty_file = tmp_path / "empty.csv"
    empty_file.write_text("NUTS_ID,NUTS_NAME\n")
    monkeypatch.setattr(load_nuts.EventLog, "from_env",
                        classmethod(lambda cls: MagicMock()))
    with pytest.raises(SystemExit) as exc:
        main(argv=["--file", str(empty_file)])
    assert exc.value.code == 1


def test_main_loads_from_file(tmp_path, monkeypatch):
    csv_file = tmp_path / "nuts.csv"
    csv_file.write_text(SAMPLE_CSV)
    log, emit = _mock_log()
    monkeypatch.setattr(load_nuts.EventLog, "from_env",
                        classmethod(lambda cls: log))
    main(argv=["--file", str(csv_file)])
    # 8 events emitted for 8 regions in the sample CSV.
    assert emit.upsert.call_count == 8
