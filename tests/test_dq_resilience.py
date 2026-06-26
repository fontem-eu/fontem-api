"""The data-quality overview must degrade gracefully, never 500 wholesale."""
from src.api.routers.data_quality import _safe


def test_safe_passes_through_success():
    assert _safe("graph", lambda: {"nodes": 5}) == {"nodes": 5}


def test_safe_catches_failure_and_marks_unavailable():
    def boom():
        raise RuntimeError("neo4j unavailable")
    assert _safe("graph", boom) == {"error": "unavailable"}


def test_one_failing_section_does_not_sink_the_others():
    sections = {
        "graph": _safe("graph", lambda: {"ok": 1}),
        "matching": _safe("matching", lambda: (_ for _ in ()).throw(RuntimeError("boom"))),
        "freshness": _safe("freshness", lambda: {"ok": 2}),
    }
    assert sections["graph"] == {"ok": 1}
    assert sections["freshness"] == {"ok": 2}
    assert sections["matching"] == {"error": "unavailable"}
