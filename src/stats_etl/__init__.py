"""Eurostat / regional-statistics ETL package.

Drives every dataset from the `fontem_stats.dataset` catalog row rather
than from per-dataset script. Adding a dataset is *insert + run*, no
new code.

Public CLI entry point::

    python -m src.stats_etl sync <code>
    python -m src.stats_etl sync --all
    python -m src.stats_etl sync --stale-after 7d
    python -m src.stats_etl register --code <code> --label "..."
    python -m src.stats_etl nuts-polygons --version 2024
"""
from __future__ import annotations

__version__ = "0.1.0"
