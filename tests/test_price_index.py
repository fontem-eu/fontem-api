"""Tests for the price-layer DQ stats (index parser + summary)."""
import json
from datetime import date
from pathlib import Path

from src.data_quality.price_index import get_price_stats, parse_index

INDEX = """4IG.BD:
  earliest_date: '2004-09-22'
  latest_date: '2026-07-01'
  status: in_progress
  error_count: 0
OLD.PA:
  earliest_date: '2010-01-04'
  latest_date: '2026-05-01'
  status: in_progress
  error_count: 0
DEAD.L:
  earliest_date: null
  latest_date: null
  status: no_data
  error_count: 1
"""


def _write(tmp_path: Path, index: str | None = INDEX,
           universe: dict | None = None):
    if index is not None:
        (tmp_path / "_index.yml").write_text(index, encoding="utf-8")
    if universe is not None:
        (tmp_path / "universe_graph.json").write_text(
            json.dumps(universe), encoding="utf-8")


def test_parse_index_reads_flat_two_level_yaml(tmp_path: Path):
    _write(tmp_path)
    idx = parse_index(str(tmp_path / "_index.yml"))
    assert idx["4IG.BD"]["latest_date"] == "2026-07-01"
    assert idx["DEAD.L"]["status"] == "no_data"
    assert idx["DEAD.L"]["latest_date"] is None


def test_parse_index_missing_file_is_empty(tmp_path: Path):
    assert not parse_index(str(tmp_path / "_index.yml"))


def test_stats_classify_fresh_stale_no_data(tmp_path: Path):
    _write(tmp_path, universe={
        "g1": {"ticker": "4IG.BD"},        # tracked
        "g2": {"ticker": "NEW.PA"},        # backlog
    })
    stats = get_price_stats(str(tmp_path), today=date(2026, 7, 4))
    assert stats["tracked"] == 3
    assert stats["fresh_7d"] == 1          # 4IG.BD (2026-07-01)
    assert stats["stale_30d"] == 1         # OLD.PA (2026-05-01)
    assert stats["no_data"] == 1           # DEAD.L
    assert stats["universe_symbols"] == 2
    assert stats["universe_covered"] == 1
    assert stats["universe_backlog"] == 1
    assert stats["index_present"] is True
    assert stats["universe_present"] is True


def test_stats_survive_missing_files(tmp_path: Path):
    stats = get_price_stats(str(tmp_path), today=date(2026, 7, 4))
    assert stats["tracked"] == 0
    assert stats["index_present"] is False
    assert stats["universe_present"] is False
    assert stats["fresh_ratio"] == 0.0
