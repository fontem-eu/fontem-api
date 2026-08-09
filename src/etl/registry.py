"""Discover every producer's DataDescription without importing producers.

The API serves this to describe the platform's holdings, so it runs inside a
request handler. Importing ``src/etl/load_*.py`` there would drag
``fontem_event_schemas``, HTTP clients and parsers into the web process, and
one loader with an expensive module-level import would become a latency
regression on an endpoint that has nothing to do with it.

So the modules are parsed, not executed. ``ast`` finds the module-level
``DESCRIPTION = DataDescription(...)`` assignment and evaluates its keyword
arguments as literals. A loader that cannot be parsed is skipped rather than
raised: a syntax error in one pipeline must not blank the whole catalogue.

The consequence — every DataDescription field must be a literal — is stated in
``data_description``. It is a real constraint and worth the trade: the
description sits in the same file as the code it describes, so the two change
together, and nothing has to be imported to read it.
"""
from __future__ import annotations

import ast
import pathlib

from src.etl.data_description import DataDescription

_ETL_DIR = pathlib.Path(__file__).resolve().parent
_CONST = "DESCRIPTION"


def _literal_kwargs(call: ast.Call) -> dict | None:
    """Evaluate a DataDescription(...) call's kwargs, or give up cleanly."""
    out: dict = {}
    for kw in call.keywords:
        if kw.arg is None:  # **kwargs — not literal, refuse to guess
            return None
        try:
            out[kw.arg] = ast.literal_eval(kw.value)
        except (ValueError, SyntaxError):
            return None
    return out


def _describe_module(path: pathlib.Path) -> DataDescription | None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return None
    for node in tree.body:  # module level only; nested ones are not the contract
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if _CONST not in targets:
            continue
        func = node.value.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "DataDescription":
            continue
        kwargs = _literal_kwargs(node.value)
        if not kwargs:
            return None
        try:
            return DataDescription(**kwargs)
        except TypeError:
            # An unknown or missing field. Skipping beats serving a half-built
            # record that a reader would take as authoritative.
            return None
    return None


def discover(etl_dir: pathlib.Path | None = None) -> list[DataDescription]:
    """Every producer that declares itself, sorted by theme then label."""
    root = etl_dir or _ETL_DIR
    found: list[DataDescription] = []
    for path in sorted(root.glob("*.py")):
        described = _describe_module(path)
        if described is not None:
            found.append(described)
    return sorted(found, key=lambda d: (d.theme, d.label))


def undescribed(etl_dir: pathlib.Path | None = None) -> list[str]:
    """Loaders with no DESCRIPTION yet.

    Exposed so the gap is measurable rather than invisible. A loader missing
    from the catalogue is data the assistant will deny having, which is the
    failure this whole mechanism exists to prevent.
    """
    root = etl_dir or _ETL_DIR
    return sorted(
        path.stem for path in root.glob("load_*.py")
        if _describe_module(path) is None
    )
