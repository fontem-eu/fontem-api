"""EU access gate: is this client IP allowed to browse fontem.eu?

Backs ``GET /geo/eu-gate``, the Traefik forwardAuth target for the
public fontem.eu ingress. Policy (owner decision 2026-07-25): the
European statistical space — EU-27, EEA/EFTA, the enlargement
countries on Eurostat's coverage (incl. Kosovo), the European
micro-states, and the UK (config toggle; drop ``GB`` from
``EU_GATE_COUNTRIES`` to exclude).

Verified search crawlers are exempt so the platform stays indexable:
exemption is by source IP against the engines' published ranges
(vendor/crawler_ranges/), never by User-Agent.

Fail-open by design: unknown IP, missing header or an unreadable
GeoIP database admits the request — an mmdb hiccup must degrade to
"open", never take the site down (same philosophy as geo_ip).
"""
from __future__ import annotations

import ipaddress
import json
import os
import threading

from src.data import geo_ip

_RANGES_DIR = os.environ.get(
    "CRAWLER_RANGES_DIR", os.path.join("vendor", "crawler_ranges"))

# EU-27
_EU27 = (
    "AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL "
    "PL PT RO SK SI ES SE")
# EEA/EFTA + European micro-states
_EFTA_MICRO = "IS LI NO CH AD MC SM VA"
# Enlargement space (Eurostat coverage), incl. Kosovo (XK)
_CANDIDATES = "AL BA GE MD ME MK RS TR UA XK"
# UK: deliberate inclusion (owner-flagged toggle)
_DEFAULT_COUNTRIES = f"{_EU27} {_EFTA_MICRO} {_CANDIDATES} GB"


def allowed_countries() -> frozenset[str]:
    raw = os.environ.get("EU_GATE_COUNTRIES", _DEFAULT_COUNTRIES)
    return frozenset(c.strip().upper() for c in raw.replace(",", " ").split())


_networks: list | None = None  # pylint: disable=invalid-name  # cache slot
_networks_lock = threading.Lock()


def _crawler_networks() -> list:
    global _networks  # pylint: disable=global-statement
    if _networks is not None:
        return _networks
    with _networks_lock:
        if _networks is None:
            nets = []
            for fname in ("googlebot.json", "bingbot.json"):
                try:
                    with open(os.path.join(_RANGES_DIR, fname),
                              encoding="utf-8") as fh:
                        data = json.load(fh)
                    for prefix in data.get("prefixes", []):
                        cidr = (prefix.get("ipv4Prefix")
                                or prefix.get("ipv6Prefix"))
                        if cidr:
                            nets.append(ipaddress.ip_network(cidr))
                except (OSError, ValueError):
                    continue  # a missing list narrows the exemption, only
            _networks = nets
    return _networks


def is_verified_crawler(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _crawler_networks())


def is_allowed(ip: str | None) -> bool:
    """Gate decision for one client IP. None/unknown fails open."""
    if not ip:
        return True
    if is_verified_crawler(ip):
        return True
    country = geo_ip.country_for(ip)
    if country is None:
        return True
    return country in allowed_countries()
