"""Resolve a request IP → country alpha-3 code.

Backed by an optional MaxMind/DB-IP `.mmdb` file at runtime. The
file isn't bundled in the image — instead an init-container in the
deployment downloads the free DB-IP Country Lite database (CC BY
4.0) into an `emptyDir` shared with the main container. If the
file isn't present (init container failed, or it's a local dev
run), `lookup` returns `None` and callers fall back to a country
picker.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

try:
    import geoip2.database as _geoip2_database
    import geoip2.errors as _geoip2_errors
except ImportError:  # pragma: no cover — package guaranteed in prod, optional locally
    _geoip2_database = None
    _geoip2_errors = None

from src.services.location_service import LocationService

logger = logging.getLogger(__name__)

# Default path the init container writes to. Overridable for tests.
DEFAULT_DB_PATH = os.environ.get(
    "GEOIP_COUNTRY_DB_PATH", "/app/geoip/dbip-country-lite.mmdb",
)


class IpToCountryService:
    """Resolve an IP to its alpha-3 country code.

    The DB is opened lazily on the first lookup so app boot isn't
    coupled to file presence — mismatched startup ordering between
    the API and its init container shouldn't cascade into a crash.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or DEFAULT_DB_PATH
        self._reader = None
        self._tried_open = False
        self._unavailable_reason: str | None = None

    def _ensure_reader(self) -> None:
        if self._tried_open:
            return
        self._tried_open = True
        if _geoip2_database is None:
            self._unavailable_reason = "geoip2 package not installed"
            return
        if not Path(self._db_path).exists():
            self._unavailable_reason = (
                f"GeoIP DB not found at {self._db_path}; "
                "install via the init container in the Helm chart"
            )
            return
        try:
            self._reader = _geoip2_database.Reader(self._db_path)
            logger.info("GeoIP DB loaded from %s", self._db_path)
        except Exception as exc:  # pylint: disable=broad-except
            self._unavailable_reason = f"GeoIP open failed: {exc}"
            self._reader = None

    @property
    def available(self) -> bool:
        self._ensure_reader()
        return self._reader is not None

    @property
    def unavailable_reason(self) -> str | None:
        self._ensure_reader()
        return self._unavailable_reason

    def lookup(self, ip: str | None) -> str | None:
        """Return the alpha-3 country code for an IP, or None.

        None covers every "we don't know" case so the caller can
        fall back to a country picker without having to discriminate
        between "unconfigured", "DB missing", "private IP", and "IP
        not in DB" — they all reduce to the same UX.
        """
        if not ip:
            return None
        # Strip a possible port (X-Forwarded-For occasionally carries
        # `1.2.3.4:5678`).
        ip_clean = ip.split(":")[0].strip() if ":" in ip and ip.count(":") == 1 else ip.strip()
        self._ensure_reader()
        if self._reader is None:
            return None
        try:
            response = self._reader.country(ip_clean)
        except (_geoip2_errors.AddressNotFoundError, ValueError):
            return None
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("GeoIP lookup error for %s: %s", ip_clean, exc)
            return None
        alpha2 = (response.country.iso_code or "").upper()
        if not alpha2:
            return None
        return LocationService.alpha2_to_alpha3(alpha2)


def client_ip_from_request(headers: dict[str, str], remote_addr: str | None) -> str | None:
    """Extract the user's IP from request headers + the socket peer.

    Order of preference:
      1. ``X-Real-IP``     — set by the cluster ingress (single value).
      2. First entry of ``X-Forwarded-For`` — the original client IP
         the load balancer saw, before any internal hops.
      3. The socket peer (``remote_addr``).

    All header reads are case-insensitive — FastAPI's ``Headers`` is
    already case-insensitive, but bare ``dict`` callers can pass any
    capitalisation.
    """
    h = {k.lower(): v for k, v in headers.items()}
    real = h.get("x-real-ip")
    if real:
        return real.strip()
    fwd = h.get("x-forwarded-for")
    if fwd:
        # Comma-separated; the leftmost entry is the client.
        return fwd.split(",")[0].strip()
    return remote_addr
