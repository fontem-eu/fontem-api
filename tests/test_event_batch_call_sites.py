"""Every `log.batch(...)` in the ETL must match the real library.

The loaders' own tests stub the event log with `def batch(self, *_a,
**_k)`, which accepts anything — so a keyword the real
`fontem_events.EventLog.batch` does not take passes the whole suite and
fails in production, at the top of a run that had already downloaded
several hundred megabytes. fontem-events is cloned at main by CI rather
than pinned, so its signature can move under us between one image and
the next.

This binds each call site against the real signature instead of trusting
the stub.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

from fontem_events import EventLog

_ETL = pathlib.Path("src/etl")


def _batch_calls() -> list[tuple[str, int, list[str], int]]:
    """(file, line, keyword names, positional count) per log.batch(...)."""
    calls = []
    for path in sorted(_ETL.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "batch":
                calls.append((
                    str(path), node.lineno,
                    [kw.arg for kw in node.keywords if kw.arg],
                    len(node.args),
                ))
    return calls


def test_every_etl_batch_call_matches_the_library_signature():
    sig = inspect.signature(EventLog.batch)
    calls = _batch_calls()
    # If this drops to zero the test has stopped testing anything --
    # a rename of the method would otherwise look like a clean pass.
    assert len(calls) >= 20, f"expected the ETL's batch call sites, found {len(calls)}"

    bad = []
    for path, line, keywords, positional in calls:
        try:
            # `self` is the bound instance at the call site.
            sig.bind_partial(None, *range(positional), **dict.fromkeys(keywords))
        except TypeError as exc:
            bad.append(f"{path}:{line} {keywords} -- {exc}")
    assert not bad, "batch() call sites the library would reject:\n  " + "\n  ".join(bad)


def test_the_chunk_size_is_one_the_library_accepts():
    """chunk must be a positive int; the library raises on anything else,
    and it raises when the batch opens -- i.e. after the download."""
    chunks = []
    for path in sorted(_ETL.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "batch"):
                for kw in node.keywords:
                    if kw.arg == "chunk":
                        chunks.append((str(path), node.lineno, kw.value))
    assert chunks, "no chunked batches left -- delete this test or restore them"
    for path, line, value in chunks:
        assert isinstance(value, ast.Constant), f"{path}:{line} chunk is not a literal"
        assert isinstance(value.value, int) and value.value >= 1, f"{path}:{line}"
