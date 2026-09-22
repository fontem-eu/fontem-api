"""The nightly job that builds the (country, cpv4) value bands the
peer-outlier rule reads."""
from unittest.mock import MagicMock

import psycopg
import pytest

from src.data_quality import peer_value_stats as job

ROWS = [
    {"country": "PRT", "cpv4": "3242", "n": 412, "p10": 8000.0,
     "median": 45000.0, "p90": 180000.0, "p99": 900000.0},
    {"country": "ITA", "cpv4": "4500", "n": 12, "p10": 1000.0,
     "median": 5000.0, "p90": 20000.0, "p99": 90000.0},
]


def _session(records=None):
    session = MagicMock()
    session.run.return_value = records if records is not None else ROWS
    return session


class TestCompute:
    def test_reads_the_grouping_query_into_rows(self):
        rows = job.compute(_session())
        assert [(r.country, r.cpv4, r.n) for r in rows] == [
            ("PRT", "3242", 412), ("ITA", "4500", 12)]
        assert rows[0].p90 == 180000.0

    def test_the_query_only_counts_contracts_that_have_a_value(self):
        assert "c.value_eur > 0" in job.CYPHER
        assert "left(c.cpv, 4)" in job.CYPHER

    def test_an_empty_graph_yields_no_rows(self):
        assert job.compute(_session([])) == []


class TestSummarise:
    def test_separates_the_groups_big_enough_to_be_evidence(self):
        assert job.summarise(job.compute(_session())) == {
            "groups": 2, "groups_min_n": 1, "contracts": 424}


class TestUpsert:
    def test_creates_the_table_and_writes_every_row(self):
        conn, cur = MagicMock(), MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        written = job.upsert(conn, job.compute(_session()))
        assert written == 2
        cur.execute.assert_called_once_with(job.DDL)
        args = cur.executemany.call_args[0]
        assert args[0] == job.UPSERT
        assert args[1][0] == ("PRT", "3242", 412, 8000.0, 45000.0, 180000.0,
                              900000.0)
        conn.commit.assert_called_once()

    def test_the_ddl_is_idempotent(self):
        assert "CREATE TABLE IF NOT EXISTS" in job.DDL
        assert "CREATE SCHEMA IF NOT EXISTS" in job.DDL


class TestLoad:
    def test_builds_the_bands_the_rule_looks_up(self):
        conn, cur = MagicMock(), MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cur.fetchall.return_value = [
            ("PRT", "3242", 412, 8000.0, 45000.0, 180000.0, 900000.0)]
        stats = job.load_peer_stats(conn)
        band = stats.band("PRT", "3242")
        assert (band.n, band.p90) == (412, 180000.0)
        assert stats.band("PRT", "9999") is None

    def test_a_missing_table_leaves_the_rule_inactive_rather_than_failing(self):
        conn, cur = MagicMock(), MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cur.execute.side_effect = psycopg.errors.UndefinedTable("nope")
        assert job.load_peer_stats(conn) is None
        conn.rollback.assert_called_once()


class TestEnv:
    @pytest.mark.parametrize("given,expected", [
        ("postgresql+asyncpg://u@h/db", "postgresql://u@h/db"),
        ("postgresql+psycopg://u@h/db", "postgresql://u@h/db"),
        ("postgresql://u@h/db", "postgresql://u@h/db"),
    ])
    def test_the_dsn_is_normalised_for_psycopg(self, monkeypatch, given, expected):
        monkeypatch.setenv("EVENTS_DATABASE_URL", given)
        assert job.events_dsn() == expected

    @pytest.mark.parametrize("given", ["", "postgresql://u@$(HOST)/db"])
    def test_an_unset_or_unexpanded_dsn_is_no_dsn(self, monkeypatch, given):
        monkeypatch.setenv("EVENTS_DATABASE_URL", given)
        assert job.events_dsn() is None

    def test_without_a_dsn_the_rule_is_inactive_not_broken(self, monkeypatch):
        monkeypatch.delenv("EVENTS_DATABASE_URL", raising=False)
        assert job.load_peer_stats_from_env() is None


class TestMain:
    def test_a_dry_run_computes_and_writes_nothing(self, monkeypatch, capsys):
        driver = MagicMock()
        driver.session.return_value.__enter__.return_value = _session()
        monkeypatch.setattr(job.GraphDatabase, "driver", lambda *a, **k: driver)
        upserted = []
        monkeypatch.setattr(job, "upsert", lambda *a: upserted.append(a))
        assert job.main(["--dry-run"]) == 0
        assert '"groups": 2' in capsys.readouterr().out
        assert not upserted
        driver.close.assert_called_once()

    def test_without_a_dsn_a_real_run_refuses(self, monkeypatch):
        driver = MagicMock()
        driver.session.return_value.__enter__.return_value = _session()
        monkeypatch.setattr(job.GraphDatabase, "driver", lambda *a, **k: driver)
        monkeypatch.delenv("EVENTS_DATABASE_URL", raising=False)
        assert job.main([]) == 2
