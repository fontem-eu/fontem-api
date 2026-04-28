"""Atlas API — read surface for the Fontem Atlas frontend.

Self-contained module designed to be extracted into a standalone
service when load justifies it. Import boundary is one-way:

    src.atlas_api  ─reads→  src.stats_etl  (DB models, schema)

Nothing in src.api or src.analysis should import from here, and
nothing here should import from src.api or src.analysis. The seam at
that boundary is the contract you'll preserve when extracting.

Two integration paths:

1. Mount inside the main gmr-api FastAPI app (current production path)::

       from src.atlas_api import build_router
       app.include_router(build_router(), prefix="/atlas")

2. Run as a standalone service::

       python -m src.atlas_api          # uvicorn server on :8001

See `README.md` in this directory for the extraction checklist.
"""
from src.atlas_api.app import build_app, build_router

__all__ = ["build_app", "build_router"]
