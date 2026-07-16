"""The CELLAR mirror is verbatim-or-nothing: windowed bounded exports,
artifact-first durability, paced loading (gitops#290)."""
import gzip
import json
from datetime import date

from src.etl.legislative import mirror_cellar as mc


def test_month_windows_half_open_and_year_rollover():
    w = mc.month_windows(date(2024, 11, 1), date(2025, 2, 1))
    assert w == [("2024-11-01", "2024-12-01"), ("2024-12-01", "2025-01-01"),
                 ("2025-01-01", "2025-02-01"), ("2025-02-01", "2025-03-01")]


def test_work_list_query_is_windowed_and_paged():
    q = mc.work_list_query("2024-05-01", "2024-06-01", 400)
    assert 'cdm:resource_legal' in q
    assert '"2024-05-01"' in q and '"2024-06-01"' in q
    assert f"LIMIT {mc.WORK_PAGE} OFFSET 400" in q


def test_closure_queries_walk_frbr_without_reshaping_or_union():
    """Verbatim emission (?x ?p ?o templates), one plain CONSTRUCT per
    FRBR level — no UNION: a FILTER in a UNION branch can't see the
    outer VALUES binding and silently empties the branch (the bug that
    dropped all work-level triples in the first MVP run)."""
    qs = mc.closure_queries(["http://x/w1", "http://x/w2"])
    assert len(qs) == 3
    assert "?w ?p ?o }" in qs[0]
    assert "expression_belongs_to_work" in qs[1]
    assert "manifestation_manifests_expression" in qs[2]
    for q in qs:
        assert "UNION" not in q and "FILTER(?s" not in q
        assert "<http://x/w1> <http://x/w2>" in q
        assert "eli:" not in q


def test_write_artifact_manifest_and_checksum(tmp_path):
    lines = ["<http://x/s> <http://x/p> <http://x/o> ."] * 3
    m = mc.write_artifact(iter(lines), tmp_path, "2024-05")
    assert m["triples"] == 3
    art = tmp_path / m["artifact"]
    assert art.exists() and m["bytes"] == art.stat().st_size
    with gzip.open(art, "rt") as gz:
        assert len(gz.read().splitlines()) == 3
    manifest = json.loads((tmp_path / "cellar-cdm-2024-05.manifest.json").read_text())
    assert manifest["sha256_uncompressed"] == m["sha256_uncompressed"]
    assert manifest["graph"] == mc.MIRROR_GRAPH


def test_load_artifact_chunks_and_paces(tmp_path, monkeypatch):
    art = tmp_path / "a.nt.gz"
    with gzip.open(art, "wt") as gz:
        gz.write("# Empty NT\n")   # CELLAR zero-result comment: never loaded
        for i in range(5):
            gz.write(f"<http://x/s{i}> <http://x/p> <http://x/o> .\n")
    monkeypatch.setattr(mc, "LOAD_CHUNK_TRIPLES", 2)
    calls = []

    class _R:
        status_code = 200

        def raise_for_status(self):
            pass

    class _C:
        def post(self, _url, data=None):
            calls.append(data["query"])
            return _R()

    n = mc.load_artifact(_C(), "http://v/sparql-auth", art, pause_s=0)
    assert n == 5                     # comment line not counted
    assert len(calls) == 3            # 2 + 2 + 1
    assert not any("# Empty NT" in q for q in calls)
    assert all(q.startswith("define sql:big-data-const 1\nINSERT DATA { GRAPH <"
                            + mc.MIRROR_GRAPH) for q in calls)


def test_recent_mode_windows(monkeypatch, tmp_path):
    """--recent = previous + current month, computed at run time — the
    daily cron needs no date templating."""

    class _FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 7, 12)

    monkeypatch.setattr(mc, "date", _FixedDate)
    captured = {}
    monkeypatch.setattr(mc, "fetch_window",
                        lambda *a, **k: iter([]))
    real_write = mc.write_artifact

    def spy_write(lines, out_dir, tag):
        captured.setdefault("tags", []).append(tag)
        return real_write(lines, out_dir, tag)
    monkeypatch.setattr(mc, "write_artifact", spy_write)
    rc = mc.main(["--recent", "--skip-load", "--out", str(tmp_path)])
    assert rc == 0
    assert captured["tags"] == ["2026-06", "2026-07"]
