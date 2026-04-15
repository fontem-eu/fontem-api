"""Location service -- normalizes country codes and NUTS hierarchy.

Similar to CurrencyService: a single interface for all geographic
normalization, used by ETL scripts before writing to Neo4j.
"""
from __future__ import annotations

import logging
import re

import pycountry

logger = logging.getLogger(__name__)

# Pre-built lookups from pycountry (249 countries)
_A2_TO_A3: dict[str, str] = {}
_A3_TO_A2: dict[str, str] = {}
for _c in pycountry.countries:
    _A2_TO_A3[_c.alpha_2.upper()] = _c.alpha_3.upper()
    _A3_TO_A2[_c.alpha_3.upper()] = _c.alpha_2.upper()

# Greece uses "EL" in EU/NUTS contexts but ISO says "GR"
_A2_TO_A3["EL"] = "GRC"
_A3_TO_A2["GRC"] = "EL"  # keep standard mapping too

# Kosovo -- not in pycountry
_A2_TO_A3["XK"] = "XKX"
_A3_TO_A2["XKX"] = "XK"

# UK alias used in NUTS
_A2_TO_A3["UK"] = "GBR"

# NUTS code pattern: 2-letter country prefix + 1-3 alphanumeric chars
_NUTS_PATTERN = re.compile(r"^[A-Z]{2}[A-Z0-9]{1,3}$")


class LocationService:
    """Stateless service for geographic code normalization."""

    @staticmethod
    def alpha2_to_alpha3(code: str) -> str | None:
        """Convert an ISO alpha-2 code to alpha-3.

        Returns None if the code is not recognized.
        """
        if not code:
            return None
        code = code.strip().upper()
        if not code:
            return None
        return _A2_TO_A3.get(code)

    @staticmethod
    def alpha3_to_alpha2(code: str) -> str | None:
        """Convert an ISO alpha-3 code to alpha-2.

        Returns None if the code is not recognized.
        """
        if not code:
            return None
        code = code.strip().upper()
        if not code:
            return None
        return _A3_TO_A2.get(code)

    @staticmethod
    def to_alpha3(code: str | None) -> str | None:
        """Normalize any country code (alpha-2 or alpha-3) to alpha-3.

        Handles:
        - None / empty string -> None
        - Already alpha-3 (verified via pycountry) -> returned as-is
        - Alpha-2 -> converted to alpha-3
        - Unknown -> None
        """
        if not code:
            return None
        code = code.strip().upper()
        if not code:
            return None

        # Already alpha-3?
        if len(code) == 3 and code in _A3_TO_A2:
            return code

        # Alpha-2?
        if len(code) == 2:
            return _A2_TO_A3.get(code)

        return None

    @staticmethod
    def validate_nuts(code: str) -> bool:
        """Check whether a string is a valid NUTS code (levels 1-3).

        NUTS codes are: 2-letter country + 1-3 alphanumeric characters.
        Level 0 codes (just 2-letter country) are NOT validated here
        since they are just country codes.
        """
        if not code:
            return False
        code = code.strip().upper()
        return bool(_NUTS_PATTERN.match(code))

    @staticmethod
    def nuts_level(code: str) -> int | None:
        """Return the NUTS level (0-3) for a code, or None if invalid."""
        if not code:
            return None
        code = code.strip().upper()
        length = len(code)
        if length == 2:
            return 0
        if 3 <= length <= 5 and _NUTS_PATTERN.match(code):
            return length - 2
        return None

    @staticmethod
    def country_from_nuts(code: str) -> str | None:
        """Extract the alpha-3 country code from a NUTS code.

        The first 2 characters of any NUTS code are the country
        (alpha-2 in EU convention). This converts to alpha-3.
        """
        if not code or len(code) < 2:
            return None
        country_a2 = code.strip().upper()[:2]
        return _A2_TO_A3.get(country_a2)
