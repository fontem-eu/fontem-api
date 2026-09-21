"""FastAPI factory for the NUTS reference API.

Mounted inside fontem-api today; `build_app()` runs it standalone, the same
two-path arrangement as ``src.atlas_api``.
"""
from __future__ import annotations

from fastapi import APIRouter, FastAPI

from src.nuts_api.routers import regions

DESCRIPTION = """
Names for every NUTS region, in the 24 official languages of the EU, with the
provenance of each name and a search that resolves a name in any of those
languages to its code.

Eurostat publishes two names per region — the national-language one and a
Latin transliteration — and nobody publishes twenty-four. This assembles them
from Eurostat, the EU Publications Office and Wikidata, keeps the two Eurostat
forms alongside, and says per region where its names came from. Coverage is
uneven and `GET /nuts` reports exactly how uneven.
""".strip()


def build_router() -> APIRouter:
    """The whole surface, prefix included.

    The prefix lives here rather than at the mount point: `/nuts` is part of
    the published contract, and a caller's URL should not depend on how this
    module happens to be hosted.
    """
    router = APIRouter()
    router.include_router(regions.router, prefix="/nuts", tags=["nuts"])
    return router


def build_app() -> FastAPI:
    app = FastAPI(title="Fontem NUTS reference API", description=DESCRIPTION,
                  version="1.0.0")
    app.include_router(build_router())
    return app
