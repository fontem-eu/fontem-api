"""Wildcard CORS is safe on the NUTS reference API only while it stays there.

``Access-Control-Allow-Origin: *`` on ``/nuts/*`` is deliberate — see
``_public`` in ``src/nuts_api/routers/regions.py``: public reference data,
identical for every caller, readable by any browser app. What makes it safe is
not the header. It is where the header is NOT — on anything that answers per
user — and that it is never paired with credentials.

Both of those were conventions, true by inspection on 2026-09-26 and held by
nothing. A DAST scan flags the wildcard as "Cross-Domain Misconfiguration" on
every run, and the ignore rule for it is scoped to ``/api/nuts/``; these tests
are what make that scoping honest, by failing the moment the header or a
credentialed CORS policy appears anywhere else.
"""
from __future__ import annotations

import ast
import pathlib

from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware

from src.nuts_api.app import build_app

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

#: The one module allowed to open cross-origin reads. Adding to this set is a
#: security decision, not a test fix — see the module docstring.
ALLOWED_ORIGIN_SETTERS = {"nuts_api/routers/regions.py"}


def _trees():
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8", errors="ignore")
        yield path.relative_to(SRC).as_posix(), ast.parse(source)


def _names_header(tree: ast.AST, header: str) -> bool:
    """True when a string literal in the code IS this header name.

    Code, not prose: the literal has to equal the name, so a docstring that
    explains why credentials are never sent does not count as sending them.
    An earlier draft grepped the text and failed on exactly that docstring.
    """
    return any(isinstance(node, ast.Constant) and isinstance(node.value, str)
               and node.value.strip().lower() == header
               for node in ast.walk(tree))


def _allows_credentials(tree: ast.AST) -> bool:
    return any(isinstance(node, ast.keyword) and node.arg == "allow_credentials"
               and not (isinstance(node.value, ast.Constant) and node.value.value is False)
               for node in ast.walk(tree))


def _uses_cors_middleware(tree: ast.AST) -> bool:
    return any((isinstance(node, ast.Name) and node.id == "CORSMiddleware")
               or (isinstance(node, ast.Attribute) and node.attr == "CORSMiddleware")
               for node in ast.walk(tree))


def test_only_the_nuts_reference_api_sets_a_cors_origin():
    setters = {name for name, tree in _trees()
               if _names_header(tree, "access-control-allow-origin")}
    assert setters == ALLOWED_ORIGIN_SETTERS


def test_nothing_allows_credentials_cross_origin():
    offenders = [name for name, tree in _trees()
                 if _names_header(tree, "access-control-allow-credentials")
                 or _allows_credentials(tree)]
    assert not offenders


def test_no_app_or_sub_app_installs_cors_middleware():
    """A blanket middleware would stamp every route, per-user ones included.
    Checked in the source because sub-apps (atlas, nuts, stats) build their
    own FastAPI instances that the main app's middleware list does not show."""
    assert not [name for name, tree in _trees() if _uses_cors_middleware(tree)]


def test_the_main_app_has_no_blanket_cors_middleware():
    # Imported here, not at module level: the main app wires every router and
    # its providers, which the source scans above do not need.
    from src.api.app import app  # pylint: disable=import-outside-toplevel

    assert not [m for m in app.user_middleware if m.cls is CORSMiddleware]


def test_a_credentialed_cross_origin_request_still_gets_no_credentials():
    """The browser attaches nothing to a wildcard request, but a hand-rolled
    client can send a cookie and an Origin anyway. The answer must not change."""
    client = TestClient(build_app())
    response = client.get(
        "/nuts/regions?limit=1",
        headers={"Origin": "https://attacker.example", "Cookie": "fontem_refresh=x"},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers
    assert "set-cookie" not in response.headers
