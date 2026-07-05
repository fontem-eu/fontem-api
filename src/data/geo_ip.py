"""Coarse IP → country → preferred-language inference.

Backs ``GET /geo/client-language``: a first-visit hint for the SPA's
language picker when the visitor has no stored preference. Country
comes from the vendored DB-IP Country Lite database (CC BY 4.0, see
vendor/geoip/README.md); the language map covers countries whose
dominant official language is one of the 24 EU languages the UI
ships. Everything else returns ``None`` and the frontend falls back
to the browser's own language.

Privacy: the IP is looked up in-process against a local file and is
neither logged nor stored.
"""
from __future__ import annotations

import ipaddress
import os
import threading

import maxminddb

_DB_PATH = os.environ.get(
    "GEOIP_DB_PATH", os.path.join("vendor", "geoip", "dbip-country-lite.mmdb"))
_reader: maxminddb.Reader | None = None  # pylint: disable=invalid-name  # cache slot, not a constant
_reader_lock = threading.Lock()
_reader_failed = False  # pylint: disable=invalid-name  # cache slot, not a constant


def _get_reader() -> maxminddb.Reader | None:
    global _reader, _reader_failed  # pylint: disable=global-statement
    if _reader is not None or _reader_failed:
        return _reader
    with _reader_lock:
        if _reader is None and not _reader_failed:
            try:
                _reader = maxminddb.open_database(_DB_PATH)
            except (OSError, maxminddb.InvalidDatabaseError):
                # Missing/corrupt DB degrades to "no hint", never to a 500.
                _reader_failed = True
    return _reader


# Dominant official language per country, restricted to the UI's 24 EU
# languages. Multilingual countries take the majority language (BE→nl,
# CH→de, LU→fr); countries whose language the UI doesn't ship map to a
# sensible EU-24 neighbour only when that is what most visitors from
# there would pick anyway (Latin America→es/pt, anglosphere→en).
# Anything unmapped -> None (frontend falls back to the browser).
COUNTRY_TO_LANG: dict[str, str] = {
    # EU-27 (+ EEA/EFTA where an EU-24 language fits)
    "AT": "de", "BE": "nl", "BG": "bg", "HR": "hr", "CY": "el", "CZ": "cs",
    "DK": "da", "EE": "et", "FI": "fi", "FR": "fr", "DE": "de", "GR": "el",
    "HU": "hu", "IE": "en", "IT": "it", "LV": "lv", "LT": "lt", "LU": "fr",
    "MT": "mt", "NL": "nl", "PL": "pl", "PT": "pt", "RO": "ro", "SK": "sk",
    "SI": "sl", "ES": "es", "SE": "sv",
    "CH": "de", "LI": "de", "MC": "fr", "SM": "it", "VA": "it", "AD": "es",
    # anglosphere
    "GB": "en", "US": "en", "CA": "en", "AU": "en", "NZ": "en",
    # lusophone / hispanophone / francophone majors
    "BR": "pt", "AO": "pt", "MZ": "pt", "CV": "pt",
    "MX": "es", "AR": "es", "CO": "es", "CL": "es", "PE": "es", "VE": "es",
    "EC": "es", "UY": "es", "PY": "es", "BO": "es", "CR": "es", "PA": "es",
    "DO": "es", "GT": "es", "HN": "es", "SV": "es", "NI": "es", "CU": "es",
    "MA": "fr", "DZ": "fr", "TN": "fr", "SN": "fr", "CI": "fr", "CM": "fr",
    "CD": "fr", "BF": "fr", "ML": "fr", "NE": "fr", "TG": "fr", "BJ": "fr",
    "GA": "fr", "CG": "fr", "HT": "fr",
}


def client_ip_from(forwarded_for: str | None, real_ip: str | None,
                   peer: str | None) -> str | None:
    """First public address in X-Forwarded-For, else X-Real-IP, else the
    socket peer. Private/reserved hops (our own proxies) are skipped so
    the lookup sees the visitor, not the ingress."""
    candidates: list[str] = []
    if forwarded_for:
        candidates.extend(p.strip() for p in forwarded_for.split(","))
    if real_ip:
        candidates.append(real_ip.strip())
    if peer:
        candidates.append(peer.strip())
    for raw in candidates:
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if addr.is_global:
            return raw
    return None


def country_for(ip: str) -> str | None:
    reader = _get_reader()
    if reader is None:
        return None
    try:
        hit = reader.get(ip)
    except (ValueError, maxminddb.InvalidDatabaseError):
        return None
    if not hit:
        return None
    return (hit.get("country") or {}).get("iso_code")


def language_for_country(country: str | None) -> str | None:
    if not country:
        return None
    return COUNTRY_TO_LANG.get(country.upper())
