"""backfill_authority_multilingual: chunking + HTTP posting + env plumbing."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.etl import backfill_authority_multilingual as bf


def test_chunks_splits_evenly():
    assert list(bf._chunks([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(bf._chunks([], 10)) == []
    assert list(bf._chunks([1], 100)) == [[1]]


def test_enrich_batch_posts_expected_payload():
    client = MagicMock()
    response = MagicMock()
    response.json.return_value = {
        "processed": 2, "merged": 0, "linked": 0, "flagged": 0, "conflicts": 0,
    }
    client.post.return_value = response

    out = bf._enrich_batch(client, "http://cons", ["a", "b"])
    client.post.assert_called_once()
    _, kwargs = client.post.call_args
    assert kwargs["json"] == {
        "entity_type": "Authority", "ids": ["a", "b"],
        "triggered_by": "backfill_multilingual",
    }
    assert out["processed"] == 2


def test_consolidator_url_default(monkeypatch):
    monkeypatch.delenv("CONSOLIDATOR_URL", raising=False)
    assert bf._consolidator_url().endswith(":8000")


def test_consolidator_url_override(monkeypatch):
    monkeypatch.setenv("CONSOLIDATOR_URL", "http://x")
    assert bf._consolidator_url() == "http://x"


def test_run_chunks_and_aggregates(monkeypatch):
    # Fake Neo4j driver returning 5 ids
    records = [{"authority_id": f"A{i}"} for i in range(5)]
    session = MagicMock()
    session.run.return_value = iter(records)
    session.__enter__.return_value = session
    session.__exit__.return_value = None

    driver = MagicMock()
    driver.session.return_value = session

    monkeypatch.setattr(bf, "_driver", lambda: driver)
    monkeypatch.setenv("CONSOLIDATOR_URL", "http://cons")

    # Fake the httpx client to return a fixed per-batch response
    class _FakeClient:
        def __init__(self, timeout):
            self.posts = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, **_):
            self.posts.append((url, json))
            resp = MagicMock()
            resp.json.return_value = {
                "processed": len(json["ids"]), "merged": 1, "linked": 0,
                "flagged": 0, "conflicts": 0,
            }
            resp.raise_for_status = lambda: None
            return resp

    with patch.object(bf.httpx, "Client", _FakeClient):
        summary = bf.run(batch=2, limit=0)

    # 5 items, batch=2 → 3 HTTP calls (2 + 2 + 1)
    assert summary["processed"] == 5
    assert summary["total_pending"] == 5
    assert summary["merged"] == 3     # one per batch


def test_run_tolerates_http_failures(monkeypatch):
    records = [{"authority_id": f"A{i}"} for i in range(4)]
    session = MagicMock()
    session.run.return_value = iter(records)
    session.__enter__.return_value = session
    session.__exit__.return_value = None

    driver = MagicMock()
    driver.session.return_value = session
    monkeypatch.setattr(bf, "_driver", lambda: driver)

    class _FakeClient:
        def __init__(self, timeout):
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise httpx.ConnectError("down")
            resp = MagicMock()
            resp.json.return_value = {
                "processed": 2, "merged": 0, "linked": 0,
                "flagged": 0, "conflicts": 0,
            }
            resp.raise_for_status = lambda: None
            return resp

    with patch.object(bf.httpx, "Client", _FakeClient):
        summary = bf.run(batch=2, limit=0)

    # Only the second batch completed — script continues on failure
    assert summary["processed"] == 2
    assert summary["total_pending"] == 4


def test_run_respects_limit(monkeypatch):
    records = [{"authority_id": f"A{i}"} for i in range(100)]
    session = MagicMock()
    session.run.return_value = iter(records)
    session.__enter__.return_value = session
    session.__exit__.return_value = None

    driver = MagicMock()
    driver.session.return_value = session
    monkeypatch.setattr(bf, "_driver", lambda: driver)

    class _FakeClient:
        def __init__(self, timeout): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *_args, **kwargs):
            resp = MagicMock()
            resp.json.return_value = {
                "processed": len(kwargs["json"]["ids"]),
                "merged": 0, "linked": 0, "flagged": 0, "conflicts": 0,
            }
            resp.raise_for_status = lambda: None
            return resp

    with patch.object(bf.httpx, "Client", _FakeClient):
        summary = bf.run(batch=10, limit=5)
    assert summary["total_pending"] == 5
    assert summary["processed"] == 5
