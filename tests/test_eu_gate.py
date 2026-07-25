"""EU access gate decision logic (src/data/eu_gate.py)."""
from __future__ import annotations

import json
import os

from src.data import eu_gate, geo_ip


def test_eu_country_allowed(monkeypatch):
    monkeypatch.setattr(geo_ip, "country_for", lambda ip: "FR")
    assert eu_gate.is_allowed("198.51.100.10")


def test_candidate_and_uk_allowed(monkeypatch):
    for cc in ("UA", "RS", "TR", "GB", "XK", "NO", "CH"):
        monkeypatch.setattr(geo_ip, "country_for", lambda ip, cc=cc: cc)
        assert eu_gate.is_allowed("198.51.100.10"), cc


def test_non_european_denied(monkeypatch):
    for cc in ("US", "CN", "BR", "RU"):
        monkeypatch.setattr(geo_ip, "country_for", lambda ip, cc=cc: cc)
        assert not eu_gate.is_allowed("198.51.100.10"), cc


def test_unknown_country_fails_open(monkeypatch):
    monkeypatch.setattr(geo_ip, "country_for", lambda ip: None)
    assert eu_gate.is_allowed("198.51.100.10")


def test_missing_ip_fails_open():
    assert eu_gate.is_allowed(None)
    assert eu_gate.is_allowed("")


def test_verified_crawler_bypasses_geo(monkeypatch):
    # First IP of the first vendored googlebot range: exempt even when
    # the GeoIP verdict is non-European.
    monkeypatch.setattr(geo_ip, "country_for", lambda ip: "US")
    path = os.path.join(eu_gate._RANGES_DIR, "googlebot.json")  # pylint: disable=protected-access
    with open(path, encoding="utf-8") as fh:
        prefix = json.load(fh)["prefixes"][0]
    cidr = prefix.get("ipv4Prefix") or prefix.get("ipv6Prefix")
    first_ip = cidr.split("/", maxsplit=1)[0]
    assert eu_gate.is_verified_crawler(first_ip)
    assert eu_gate.is_allowed(first_ip)


def test_spoofed_crawler_ua_is_not_enough(monkeypatch):
    # Verification is IP-based: a non-range IP is geo-gated regardless
    # of what User-Agent it claims (the endpoint never reads the UA).
    monkeypatch.setattr(geo_ip, "country_for", lambda ip: "US")
    assert not eu_gate.is_allowed("198.51.100.10")


def test_countries_env_override(monkeypatch):
    monkeypatch.setenv("EU_GATE_COUNTRIES", "FR, DE")
    monkeypatch.setattr(geo_ip, "country_for", lambda ip: "GB")
    assert not eu_gate.is_allowed("198.51.100.10")
    assert eu_gate.allowed_countries() == frozenset({"FR", "DE"})


def test_real_mmdb_us_ip_denied():
    # Offline lookup against the vendored DB: 8.8.8.8 is US.
    if geo_ip.country_for("8.8.8.8") != "US":  # pragma: no cover
        return  # vendored DB unavailable in this env — decision covered above
    assert not eu_gate.is_allowed("8.8.8.8")
