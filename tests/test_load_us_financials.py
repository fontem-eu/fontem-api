"""Tests for the US EDGAR financials loader.

Post-event-log: the loader emits ``BeginGraphReplace`` →
``UpsertFiling`` × N → ``EndGraphReplace`` events into the event
log against the EDGAR financials graph. The Virtuoso + Neo4j
sinks own the projection.
"""
from pathlib import Path
from unittest.mock import MagicMock

from src.etl.load_us_financials import (
    GRAPH_IRI, _filing_uuid, load_us_financials,
)


def _mock_log():
    log = MagicMock()
    emit = MagicMock()
    log.batch.return_value.__enter__ = MagicMock(return_value=emit)
    log.batch.return_value.__exit__ = MagicMock(return_value=False)
    return log, emit


def _seed_edgar_dir(tmp_path: Path, *, cik: int, years: list[int]) -> Path:
    """Lay out a minimal /edgar-data/full structure: company_tickers.json
    + a single CIK*.json with annual revenue facts for the requested years."""
    ref = tmp_path / "reference"
    ref.mkdir(parents=True)
    (ref / "company_tickers.json").write_text(
        '{"0": {"cik_str": ' + str(cik) + ', "ticker": "AAPL", "title": "Apple Inc."}}'
    )
    facts = tmp_path / "companyfacts"
    facts.mkdir()
    cik_padded = str(cik).zfill(10)
    units = [
        {"form": "10-K", "fp": "FY", "end": f"{y}-12-31", "val": 100000 + y}
        for y in years
    ]
    facts_doc = {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": units}},
            },
        },
    }
    import json
    (facts / f"CIK{cik_padded}.json").write_text(json.dumps(facts_doc))
    return tmp_path


def test_emits_begin_filings_end_bracket(tmp_path: Path):
    edgar_dir = _seed_edgar_dir(tmp_path, cik=320193, years=[2022, 2023])
    log, emit = _mock_log()
    res = load_us_financials(log, edgar_dir)
    assert res == {"total": 2, "companies": 1}
    # First call is BeginGraphReplace; last is EndGraphReplace.
    assert emit.control.call_count == 2
    begin = emit.control.call_args_list[0]
    end = emit.control.call_args_list[1]
    assert begin.args[0] == "BeginGraphReplace"
    assert end.args[0] == "EndGraphReplace"
    assert begin.args[1]["graph_iri"] == GRAPH_IRI
    assert begin.args[1]["label"] == "FinancialYear"
    # Two filing events between the bracket markers.
    assert emit.upsert.call_count == 2
    assert all(c.args[0] == "UpsertFiling" for c in emit.upsert.call_args_list)


def test_filing_payload_carries_year_source_and_value(tmp_path: Path):
    edgar_dir = _seed_edgar_dir(tmp_path, cik=320193, years=[2023])
    log, emit = _mock_log()
    load_us_financials(log, edgar_dir)
    payload = emit.upsert.call_args.kwargs["payload"]
    assert payload["year"] == 2023
    assert payload["source"] == "edgar"
    assert payload["revenue"] == 100000 + 2023


def test_iri_is_deterministic_per_company_year_source():
    a = _filing_uuid("abc-123", 2023, "edgar")
    b = _filing_uuid("abc-123", 2023, "edgar")
    different = _filing_uuid("abc-123", 2023, "esef")
    assert a == b
    assert a != different


def test_skips_companies_without_known_cik(tmp_path: Path):
    """If a CIK*.json file has no matching ticker entry, the loader
    skips that company silently rather than emitting orphan filings."""
    edgar_dir = _seed_edgar_dir(tmp_path, cik=999999, years=[2023])
    # Replace tickers index so 999999 has no gmr_id mapping
    (edgar_dir / "reference" / "company_tickers.json").write_text("{}")
    log, emit = _mock_log()
    res = load_us_financials(log, edgar_dir)
    assert res == {"total": 0, "companies": 0}
    # Bracket markers still emit (PUT-replace semantics): the
    # graph gets wiped to empty rather than left with stale data.
    assert emit.control.call_count == 2
    assert emit.upsert.call_count == 0
