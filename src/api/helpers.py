"""
Shared helpers for API router modules.
"""
from __future__ import annotations

import math
from typing import Optional


def _f(value: float) -> Optional[float]:
    """Convert NaN / Inf to None for JSON serialisation."""
    if value is None:
        return None
    try:
        return None if (math.isnan(value) or math.isinf(value)) else value
    except (TypeError, ValueError):
        return None
