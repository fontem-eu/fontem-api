"""Unit tests for the Mistral batch authority-translation ETL.

Covers the two pieces that must be correct without a live Mistral key or
Neo4j: request compilation (source-language inference, target exclusion,
shape) and output parsing / write-row construction (defensive against junk
codes, empty values, fenced JSON, and the source language leaking back in).
"""
from unittest.mock import MagicMock

from src.etl.batch_translate_authorities import (
    EU_LANGS,
    _build_request,
    _cost_report,
    _extract_translations,
    _rows_from_lines,
    _source_lang,
    _targets,
    integrate,
)


def test_source_lang_prefers_explicit_then_country_then_en():
    assert _source_lang({"name_lang": "fr", "country": "DEU"}) == "fr"
    assert _source_lang({"name_lang": None, "country": "HUN"}) == "hu"
    assert _source_lang({"name_lang": "", "country": "USA"}) == "en"
    assert _source_lang({"name_lang": "xx", "country": None}) == "en"


def test_targets_exclude_source_and_cover_23():
    tgt = _targets("hu")
    assert "hu" not in tgt
    assert len(tgt) == len(EU_LANGS) - 1 == 23


def test_build_request_shape():
    rec = {"id": "auth-1", "name": "Nemzeti Adóhivatal", "country": "HUN",
           "name_lang": None}
    req = _build_request(rec)
    assert req["custom_id"] == "auth-1"
    body = req["body"]
    assert body["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    # source language named in the prompt, source code not offered as a target
    assert "Hungarian" in body["messages"][1]["content"]
    assert "hu (" not in body["messages"][1]["content"]


def test_extract_drops_junk_empty_and_bad_codes():
    line = {"custom_id": "a", "response": {"body": {"choices": [{"message": {
        "content": '{"en":"Tax Office","de":"Finanzamt","xx":"nope","fr":""}'}}]}}}
    out = _extract_translations(line)
    assert out == {"en": "Tax Office", "de": "Finanzamt"}


def test_extract_tolerates_json_fence():
    line = {"custom_id": "a", "response": {"body": {"choices": [{"message": {
        "content": "```json\n{\"de\":\"Test\"}\n```"}}]}}}
    assert _extract_translations(line) == {"de": "Test"}


def test_extract_returns_none_on_malformed():
    assert _extract_translations({"custom_id": "a"}) is None
    assert _extract_translations({"custom_id": "a", "response": {"body": {
        "choices": [{"message": {"content": "not json"}}]}}}) is None


def test_rows_from_lines_filters_source_and_unknown_ids():
    by_id = {"a": {"id": "a", "name": "Nemzeti", "country": "HUN",
                   "name_lang": "hu"}}
    lines = [
        # source lang 'hu' present in the map must be dropped from props
        {"custom_id": "a", "response": {"body": {"choices": [{"message": {
            "content": '{"en":"National","hu":"Nemzeti"}'}}]}}},
        # unknown custom_id -> skipped
        {"custom_id": "ghost", "response": {"body": {"choices": [{"message": {
            "content": '{"en":"x"}'}}]}}},
    ]
    rows, skipped = _rows_from_lines(lines, by_id)
    assert skipped == 1
    assert len(rows) == 1
    assert rows[0]["props"] == {"name_en": "National"}
    assert "name_hu" not in rows[0]["props"]
    assert rows[0]["src"] == "hu"


def test_integrate_writes_in_chunks_and_counts():
    by_id = {"a": {"id": "a", "name": "X", "country": "FRA", "name_lang": "fr"}}
    lines = [{"custom_id": "a", "response": {"body": {"choices": [{"message": {
        "content": '{"en":"X-en","de":"X-de"}'}}]}}}]
    session = MagicMock()
    session.run.return_value.single.return_value = {"written": 1}
    driver = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    summary = integrate(driver, lines, by_id)
    assert summary["parsed"] == 1
    assert summary["written"] == 1
    assert summary["langs_total"] == 2


def test_cost_report_sums_usage_and_estimates():
    lines = [
        {"response": {"body": {"usage": {"prompt_tokens": 300, "completion_tokens": 400}}}},
        {"response": {"body": {"usage": {"prompt_tokens": 100, "completion_tokens": 200}}}},
        {"response": {"body": {}}},  # no usage -> ignored
    ]
    rep = _cost_report(lines)
    assert rep["responses_with_usage"] == 2
    assert rep["prompt_tokens"] == 400
    assert rep["completion_tokens"] == 600
    assert rep["total_tokens"] == 1000
    # est_usd = 400/1e6*rate_in + 600/1e6*rate_out, both rates > 0
    assert rep["est_usd"] > 0
