"""Data sources for the Atlas API.

One file per source, each implementing the `Source` protocol. Adding
a new overlay (Neo4j procurement, reports DB, third-party API) means:

1. Drop a new `xxx.py` next to fontem_stats.py.
2. Wire it into `app.py`'s factory.
3. Reference it from whichever router needs the data.

The protocol is intentionally minimal — every source must report its
health, nothing else. Read methods are source-specific and live on
the concrete classes.
"""
from __future__ import annotations

from typing import Protocol

from src.atlas_api.schemas import SourceHealth


class Source(Protocol):  # pylint: disable=too-few-public-methods
    name: str

    def health(self) -> SourceHealth:
        ...
