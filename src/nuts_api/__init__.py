"""NUTS reference API — public read surface for EU region names.

Self-contained module, like ``src.atlas_api``: the import boundary runs one
way,

    src.nuts_api  ─reads→  src.data.nuts_gazetteer

and nothing in ``src.api`` is imported, so this lifts out into its own
service when it deserves one. See ``README.md`` here.
"""
from src.nuts_api.app import build_app, build_router

__all__ = ["build_app", "build_router"]
