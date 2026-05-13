"""Standalone runner — `python -m src.atlas_api`.

Useful for local dev and as the entry point when the service is
extracted from fontem-api. In the consolidated build we serve Atlas
mounted under /atlas in the main app instead (see src/api/app.py).
"""
from __future__ import annotations

import os

import uvicorn

from src.atlas_api.app import build_app

if __name__ == "__main__":
    uvicorn.run(
        build_app(),
        host="0.0.0.0",  # noqa: S104 — bind for in-cluster access
        port=int(os.environ.get("ATLAS_PORT", "8001")),
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )
