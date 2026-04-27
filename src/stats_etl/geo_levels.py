"""NUTS code helpers.

Eurostat NUTS coding: 2-char country, 3-char NUTS-1, 4-char NUTS-2,
5-char NUTS-3. A handful of non-NUTS codes exist (EU27_2020, EA, etc.)
which we keep but don't classify as a level.
"""
from __future__ import annotations


def detect_nuts_level(code: str) -> int | None:
    """Return the NUTS level (0=country, 1, 2, 3) or None if non-NUTS."""
    if not code:
        return None
    if len(code) == 2 and code.isalpha():
        return 0
    if len(code) == 3 and code[:2].isalpha() and code[2].isalnum():
        return 1
    if len(code) == 4 and code[:2].isalpha():
        return 2
    if len(code) == 5 and code[:2].isalpha():
        return 3
    return None


def parent_code(code: str) -> str | None:
    """Return the parent region code, or None if at country level / unknown."""
    lvl = detect_nuts_level(code)
    if lvl is None or lvl == 0:
        return None
    return code[:-1]


def country_of(code: str) -> str | None:
    """First two chars if they're a plausible country code, else None."""
    if not code or len(code) < 2:
        return None
    cc = code[:2]
    if cc.isalpha():
        return cc
    return None
