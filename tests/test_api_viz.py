"""Per-viz data endpoint: /viz/company-bidder-breakdown returns plot-ready bars.

The per-test fake mirrors Neo4j's ``session().run(query, **params)`` shape; the
stub args are the protocol, not always read — disabled file-wide like the graph
suite to keep the signatures honest.
"""
# pylint: disable=missing-class-docstring,missing-function-docstring
# pylint: disable=unused-argument,too-few-public-methods
from __future__ import annotations

from tests.dishka_fixtures import make_test_client, cleanup_dishka


class _Session:
    def __init__(self, rows):
        self._rows = rows

    def run(self, query, **kwargs):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _Neo4j:
    def __init__(self, rows):
        self._rows = rows

    def session(self):
        return _Session(self._rows)

    def close(self):
        pass


def test_bidder_breakdown_is_plot_ready_and_ordered():
    rows = [{"bidders": 2, "n": 13}, {"bidders": None, "n": 32}, {"bidders": 1, "n": 22}]
    client = make_test_client(neo4j_client=_Neo4j(rows))
    try:
        resp = client.get("/viz/company-bidder-breakdown?entity_id=abc")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["chart"] == "bar_h" and body["format"] == "number"
        # numeric counts ascending, "Not disclosed" last
        assert [b["label"] for b in body["bars"]] == ["1 (single bidder)", "2", "Not disclosed"]
        assert [b["value"] for b in body["bars"]] == [22, 13, 32]
    finally:
        cleanup_dishka()


def test_entity_id_is_required():
    client = make_test_client(neo4j_client=_Neo4j([]))
    try:
        assert client.get("/viz/company-bidder-breakdown").status_code == 422
    finally:
        cleanup_dishka()
