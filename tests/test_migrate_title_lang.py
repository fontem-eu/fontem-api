"""Step 2 of the title-language rollout: the migration's own logic.

What it must never do is change anything but the language: the SQL adds one
key with jsonb_set, only where it is absent; the graph writes set title_lang
and nothing else. The stage loops are exercised against small fakes here;
the SQL and Cypher run for real against the shared environment.
"""
from __future__ import annotations

import gzip
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from src.data.ted_raw_store import TedRawStore
from src.etl import migrate_title_lang as mig


def _notice(notice_id, publication_number=None, title_lang="fr"):
    return SimpleNamespace(notice_id=notice_id, publication_number=publication_number,
                           title_lang=title_lang)


# ── ids and reduction ───────────────────────────────────────────────


def test_an_eforms_notice_is_keyed_by_its_uuid():
    assert mig.notice_ids(_notice("912f1717-1ace-413d-aa61-cd21cd6b95e7",
                                  "24047-2024")) == ["912f1717-1ace-413d-aa61-cd21cd6b95e7"]


def test_a_legacy_notice_matches_both_the_padded_and_unpadded_publication_number():
    """The current loader writes 000123-2020; an older one wrote 123-2020."""
    ids = mig.notice_ids(_notice("2020/S 001-000123", "000123-2020"))
    assert ids == ["000123-2020", "123-2020"]


def test_latest_per_id_keeps_the_last_language_in_seq_order():
    rows = [("a", "fr"), ("b", "de"), ("a", "en"), ("c", None), (None, "it")]
    assert mig.latest_per_id(rows) == [{"id": "a", "lang": "en"}, {"id": "b", "lang": "de"}]


# ── SQL shape ───────────────────────────────────────────────────────


@pytest.mark.parametrize("domain", ["contract", "eu_cohesion"])
def test_the_update_adds_title_lang_and_nothing_else_where_it_is_absent(domain):
    sql = mig.events_sql(domain, dry_run=False)
    assert "jsonb_set(e.payload, '{title_lang}', to_jsonb(m.title_lang))" in sql
    assert "NOT (e.payload ? 'title_lang')" in sql
    assert sql.count("SET ") == 1


def test_a_kohesio_event_is_english_only_when_its_title_is_kohesios_english_name():
    assert "m.title = e.payload->>'title'" in mig.events_sql("eu_cohesion", dry_run=False)


def test_a_notice_that_states_no_language_is_never_given_one():
    assert "m.title_lang IS NOT NULL" in mig.events_sql("contract", dry_run=False)


def test_the_dry_run_counts_the_same_rows():
    sql = mig.events_sql("contract", dry_run=True)
    assert sql.startswith("SELECT count(*)") and "UPDATE" not in sql
    assert "NOT (e.payload ? 'title_lang')" in sql


# ── fakes ───────────────────────────────────────────────────────────


@dataclass
class _Cursor:
    rows: list = field(default_factory=list)
    rowcount: int = 0

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


@dataclass
class _Conn:
    """Answers progress reads, max(seq), counts and graph reads; records the rest."""
    max_seq: int = 50
    progress: dict = field(default_factory=dict)
    graph_rows: dict = field(default_factory=dict)   # lo -> rows
    executed: list = field(default_factory=list)
    many: list = field(default_factory=list)
    commits: int = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "FROM migrations.title_lang_progress" in sql:
            return _Cursor([(self.progress[params[0]],)] if params[0] in self.progress else [])
        if "INSERT INTO migrations.title_lang_progress" in sql:
            self.progress[params[0]] = params[1]
            return _Cursor()
        if "max(seq)" in sql:
            return _Cursor([(self.max_seq,)])
        if sql.startswith("SELECT count(*)"):
            return _Cursor([(3,)])
        if sql.lstrip().startswith("SELECT payload"):
            return _Cursor(self.graph_rows.get(params["lo"], []))
        return _Cursor(rowcount=2)

    def cursor(self):
        conn = self

        class _C:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def executemany(self, sql, rows):
                conn.many.append((sql, list(rows)))
        return _C()

    def commit(self):
        self.commits += 1


@dataclass
class _Driver:
    runs: list = field(default_factory=list)
    cleared: list = field(default_factory=lambda: [2, 0])

    def session(self):
        drv = self

        class _S:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def run(self, cypher, **params):
                drv.runs.append((cypher, params))
                if "RETURN count(d) AS cleared" in cypher:
                    return SimpleNamespace(single=lambda: {"cleared": drv.cleared.pop(0)})
                return SimpleNamespace(consume=lambda: None)
        return _S()


# ── stages ──────────────────────────────────────────────────────────


def test_map_records_every_id_with_its_language_even_none():
    conn = _Conn()
    n = mig.map_notices(conn, [_notice("2020/S 001-000123", "000123-2020", "cs"),
                               _notice("uuid-1", None, None)], source="ted-2020-01")
    rows = [r for _sql, batch in conn.many for r in batch]
    assert n == 2
    assert ("000123-2020", "cs", "ted-2020-01") in rows
    assert ("123-2020", "cs", "ted-2020-01") in rows
    assert ("uuid-1", None, "ted-2020-01") in rows


def test_events_run_window_by_window_from_the_saved_cursor(monkeypatch):
    slept = []
    monkeypatch.setattr(mig.time, "sleep", slept.append)
    conn = _Conn(max_seq=50, progress={"events:contract": "10"})
    changed = mig.migrate_events(conn, "contract", batch=20, pause_s=0.1, dry_run=False)
    windows = [(p["lo"], p["hi"]) for sql, p in conn.executed if sql.startswith("UPDATE")]
    assert windows == [(10, 30), (30, 50)] and changed == 4
    assert conn.progress["events:contract"] == "50" and len(slept) == 2


def test_an_events_dry_run_writes_nothing_but_still_ends_each_transaction(monkeypatch):
    monkeypatch.setattr(mig.time, "sleep", lambda _s: pytest.fail("dry run must not pause"))
    conn = _Conn(max_seq=40)
    assert mig.migrate_events(conn, "contract", batch=20, pause_s=1, dry_run=True) == 6
    assert not any(sql.startswith("UPDATE") for sql, _ in conn.executed)
    assert "events:contract" not in conn.progress and conn.commits == 2


def test_the_graph_takes_each_ids_latest_language_from_the_events():
    conn = _Conn(max_seq=40, graph_rows={0: [("n1", "fr"), ("n1", "en")], 20: [("n2", "de")]})
    drv = _Driver()
    assert mig.migrate_graph(conn, drv, "contract", batch=20, dry_run=False) == 2
    sent = [p["rows"] for _c, p in drv.runs]
    assert sent == [[{"id": "n1", "lang": "en"}], [{"id": "n2", "lang": "de"}]]
    assert conn.progress["graph:contract"] == "40"


def test_cohesion_guesses_are_cleared_before_the_events_set_what_the_source_states():
    conn = _Conn(max_seq=20, graph_rows={0: [("Q7", "en")]})
    drv = _Driver()
    mig.migrate_graph(conn, drv, "eu_cohesion", batch=20, dry_run=False)
    kinds = ["clear" if "REMOVE" in c else "set" for c, _p in drv.runs]
    assert kinds == ["clear", "clear", "set"]


def test_a_graph_dry_run_touches_no_node():
    conn = _Conn(max_seq=20, graph_rows={0: [("n1", "fr")]})
    drv = _Driver()
    assert mig.migrate_graph(conn, drv, "eu_cohesion", batch=20, dry_run=True) == 1
    assert not drv.runs


def test_the_raw_store_yields_every_notice_in_key_order_decompressed():
    blobs = {"1-2026.xml.gz": gzip.compress(b"<a/>"), "2-2026.xml": b"<b/>"}

    class _Obj(SimpleNamespace):
        pass

    class _Client:
        def list_objects(self, _bucket, recursive, start_after):
            assert recursive and start_after is None
            return [_Obj(object_name=k) for k in sorted(blobs)]

        def get_object(self, _bucket, name):
            return SimpleNamespace(read=lambda: blobs[name], close=lambda: None,
                                   release_conn=lambda: None)

    assert list(TedRawStore(_Client(), "ted-raw").iter_xml()) == [
        ("1-2026.xml.gz", b"<a/>"), ("2-2026.xml", b"<b/>")]


def test_months_walk_across_a_year_boundary():
    assert mig._months("2018-11", "2019-02") == [(2018, 11), (2018, 12), (2019, 1), (2019, 2)]  # pylint: disable=protected-access
