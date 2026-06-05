"""Tests for the CPV reference-data loader (Genericode pipeline)."""
# pylint: disable=redefined-outer-name,protected-access
import gzip
from xml.etree import ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.etl.load_cpv import (
    ISO3_TO_ISO1, _level_from_code, _row_values, load_cpv,
    parse_genericode,
)


def _mock_log():
    log = MagicMock()
    emit = MagicMock()
    log.batch.return_value.__enter__ = MagicMock(return_value=emit)
    log.batch.return_value.__exit__ = MagicMock(return_value=False)
    return log, emit


# A minimal Genericode fragment covering the two shapes that matter:
# top-level division (no parentCode) and a detail row with parent +
# labels in two languages. Real file has 27 columns; tests don't
# need them all -- the loader keys by ColumnRef so unused columns
# are silently ignored.
_FIXTURE_GC = b"""<?xml version="1.0" encoding="UTF-8"?>
<gc:CodeList xmlns:gc="http://docs.oasis-open.org/codelist/ns/genericode/1.0/">
  <Identification><ShortName>cpv_test</ShortName></Identification>
  <ColumnSet>
    <Column Id="code" Use="required"><ShortName>Code</ShortName>
      <Data Lang="eng" Type="normalizedString"/></Column>
    <Column Id="parentCode" Use="optional"><ShortName>ParentCode</ShortName>
      <Data Lang="eng" Type="normalizedString"/></Column>
    <Column Id="eng_label"><ShortName>en</ShortName>
      <Data Lang="eng" Type="string"/></Column>
    <Column Id="por_label"><ShortName>pt</ShortName>
      <Data Lang="por" Type="string"/></Column>
  </ColumnSet>
  <SimpleCodeList>
    <Row>
      <Value ColumnRef="code"><SimpleValue>45000000</SimpleValue></Value>
      <Value ColumnRef="eng_label"><SimpleValue>Construction work</SimpleValue></Value>
      <Value ColumnRef="por_label"><SimpleValue>Trabalhos de construcao</SimpleValue></Value>
    </Row>
    <Row>
      <Value ColumnRef="code"><SimpleValue>45233000</SimpleValue></Value>
      <Value ColumnRef="parentCode"><SimpleValue>45200000</SimpleValue></Value>
      <Value ColumnRef="eng_label"><SimpleValue>Construction, foundation and surface works for highways, roads</SimpleValue></Value>
      <Value ColumnRef="por_label"><SimpleValue>Obras de construcao, fundacoes e superficies para autoestradas e estradas</SimpleValue></Value>
    </Row>
  </SimpleCodeList>
</gc:CodeList>
"""


@pytest.fixture
def gc_fixture(tmp_path) -> Path:
    """Gzipped Genericode fixture on disk. The loader reads .gz
    transparently so tests cover the same code path as prod."""
    path = tmp_path / "cpv.gc.gz"
    with gzip.open(path, "wb") as f:
        f.write(_FIXTURE_GC)
    return path


# ----------------------- pure helpers -----------------------


def test_level_from_code_div():
    # 45000000 -> "45" -> len 2 (depth = the division)
    assert _level_from_code("45000000") == 2


def test_level_from_code_subdiv():
    # 45200000 -> "452" -> 3
    assert _level_from_code("45200000") == 3
    # 45234110 -> "4523411" -> 7 (fully qualified detail)
    assert _level_from_code("45234110") == 7


def test_level_from_code_all_zeros_floor_is_one():
    # The all-zero edge case shouldn't happen in real CPV but the
    # helper must never return zero (downstream uses it as a 1-indexed
    # depth).
    assert _level_from_code("00000000") == 1


def test_iso3_to_iso1_covers_24_eu_langs():
    """All 24 EU official languages must map. en/fr/pt sanity checks
    catch typos in the table."""
    assert len(ISO3_TO_ISO1) == 24
    assert ISO3_TO_ISO1["eng"] == "en"
    assert ISO3_TO_ISO1["fra"] == "fr"
    assert ISO3_TO_ISO1["por"] == "pt"
    # All 2-letter codes are unique (no two ISO 639-3 codes collide
    # on the same ISO 639-1).
    assert len(set(ISO3_TO_ISO1.values())) == 24


# ----------------------- _row_values -----------------------


def test_row_values_handles_genericode_xml_quirk(gc_fixture):
    """The Genericode <Row> wraps <Value ColumnRef="..."><SimpleValue>
    text</SimpleValue></Value>. The helper unwraps to {ColumnRef:
    text}."""
    with gzip.open(gc_fixture, "rb") as f:
        root = ET.parse(f).getroot()
    rows = []
    for child in root:
        if child.tag.endswith("SimpleCodeList"):
            rows = list(child)
            break
    vals = _row_values(rows[0])
    assert vals["code"] == "45000000"
    assert "Construction" in vals["eng_label"]
    assert "Trabalhos" in vals["por_label"]


# ----------------------- parse_genericode -----------------------


def test_parse_yields_one_tuple_per_code_language(gc_fixture):
    # 2 rows x 2 languages = 4 tuples.
    tuples = list(parse_genericode(gc_fixture))
    assert len(tuples) == 4
    codes = {t[0] for t in tuples}
    assert codes == {"45000000", "45233000"}
    langs = {t[2] for t in tuples}
    assert langs == {"en", "pt"}


def test_parse_passes_through_parent_code(gc_fixture):
    tuples = list(parse_genericode(gc_fixture))
    by_code = {(t[0], t[2]): t for t in tuples}
    division = by_code["45000000", "en"]
    detail = by_code["45233000", "en"]
    # Division has no parent (top of the tree).
    assert division[1] is None
    # Detail carries its parent verbatim from the source.
    assert detail[1] == "45200000"


def test_parse_skips_unmapped_languages(tmp_path):
    """If the source ever adds a language we don't have in
    ISO3_TO_ISO1, we silently skip it rather than fail the run."""
    gc_bytes = _FIXTURE_GC.replace(
        b'<Column Id="por_label">',
        b'<Column Id="zzz_label">',
    ).replace(b"por_label", b"zzz_label")
    path = tmp_path / "cpv-unmapped.gc.gz"
    with gzip.open(path, "wb") as f:
        f.write(gc_bytes)
    tuples = list(parse_genericode(path))
    # Only the eng_label entries pass through.
    assert all(t[2] == "en" for t in tuples)
    assert len(tuples) == 2


# ----------------------- load_cpv (emit pipeline) -----------------------


def test_load_cpv_emits_one_event_per_code_lang(gc_fixture):
    log, emit = _mock_log()
    total = load_cpv(log, gc_path=gc_fixture)
    assert total == 4
    assert emit.upsert.call_count == 4
    types = {c.args[0] for c in emit.upsert.call_args_list}
    assert types == {"UpsertTaxonomyCode"}


def test_load_cpv_lang_filter(gc_fixture):
    log, emit = _mock_log()
    total = load_cpv(log, gc_path=gc_fixture, lang="en")
    assert total == 2
    payloads = [c.kwargs["payload"] for c in emit.upsert.call_args_list]
    assert all(p["label_lang"] == "en" for p in payloads)
    assert {p["code"] for p in payloads} == {"45000000", "45233000"}


def test_load_cpv_payload_shape(gc_fixture):
    log, emit = _mock_log()
    load_cpv(log, gc_path=gc_fixture, lang="en")
    payloads = [c.kwargs["payload"] for c in emit.upsert.call_args_list]
    division = next(p for p in payloads if p["code"] == "45000000")
    assert division["system"] == "cpv"
    assert division["label_lang"] == "en"
    assert "Construction" in division["label"]
    # No parent on the division.
    assert "parent_code" not in division
    # Level == "first non-zero run" depth, which is 2 for "45..."
    assert division["level"] == 2

    detail = next(p for p in payloads if p["code"] == "45233000")
    assert detail["parent_code"] == "45200000"
    assert detail["level"] == 5


def test_load_cpv_iri_is_keyed_by_code(gc_fixture):
    log, emit = _mock_log()
    load_cpv(log, gc_path=gc_fixture, lang="en")
    calls = emit.upsert.call_args_list
    division = next(c for c in calls
                    if c.kwargs["payload"]["code"] == "45000000")
    # IRI shape must stay stable: downstream sinks key Cpv nodes by
    # this path. Drift here would orphan every existing CATEGORIZED_AS
    # edge on the next consolidator pass.
    assert (division.kwargs["iri"]
            == "http://data.fontem.eu/id/Cpv/45000000")
    assert division.kwargs["domain"] == "cpv"


def test_load_cpv_raises_when_source_missing(tmp_path):
    """Clear error so an operator doesn't silently emit zero events
    when ``--download`` was forgotten on a fresh clone."""
    log, _emit = _mock_log()
    missing = tmp_path / "nope.gc.gz"
    with pytest.raises(FileNotFoundError, match="Run with --download"):
        load_cpv(log, gc_path=missing)
