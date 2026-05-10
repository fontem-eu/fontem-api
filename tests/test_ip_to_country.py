"""Tests for IpToCountryService — header parsing + graceful fallback
when the GeoIP DB is missing."""
from __future__ import annotations

import pytest

from src.services.ip_to_country import (
    IpToCountryService,
    client_ip_from_request,
)


# ── client_ip_from_request — pure header parsing ─────────────


@pytest.mark.parametrize(
    "headers,remote,expected",
    [
        ({"X-Real-IP": "1.2.3.4"}, "10.0.0.1", "1.2.3.4"),
        # XFF beats remote_addr only if no X-Real-IP.
        ({"X-Forwarded-For": "5.6.7.8, 10.0.0.1"}, "10.0.0.1", "5.6.7.8"),
        # Both present → X-Real-IP wins.
        (
            {"X-Real-IP": "1.2.3.4", "X-Forwarded-For": "5.6.7.8"},
            "10.0.0.1", "1.2.3.4",
        ),
        # Case-insensitive.
        ({"x-real-ip": "9.9.9.9"}, None, "9.9.9.9"),
        # Nothing → fall back to socket peer.
        ({}, "10.0.0.1", "10.0.0.1"),
        # Nothing at all → None.
        ({}, None, None),
    ],
)
def test_client_ip_extraction(headers, remote, expected):
    assert client_ip_from_request(headers, remote) == expected


def test_client_ip_handles_xff_with_whitespace():
    # XFF often arrives with random whitespace from intermediate proxies.
    out = client_ip_from_request({"X-Forwarded-For": "  1.2.3.4  ,  5.6.7.8"}, None)
    assert out == "1.2.3.4"


# ── IpToCountryService — fallback when DB is missing ─────────


def test_lookup_returns_none_when_db_missing(tmp_path):
    """No DB at the path → graceful None, no exception, no boot crash."""
    svc = IpToCountryService(db_path=str(tmp_path / "missing.mmdb"))
    assert svc.available is False
    assert svc.unavailable_reason  # populated
    assert svc.lookup("8.8.8.8") is None


def test_lookup_returns_none_when_ip_is_none():
    svc = IpToCountryService(db_path="/dev/null")
    assert svc.lookup(None) is None
    assert svc.lookup("") is None


def test_lookup_strips_port_from_xff_style_value(tmp_path):
    """X-Forwarded-For values sometimes include `ip:port`. The lookup
    should cope without throwing — we strip the port and end up
    looking up a plain IP (which still returns None here because
    the DB is missing, but the path doesn't crash)."""
    svc = IpToCountryService(db_path=str(tmp_path / "missing.mmdb"))
    # `1.2.3.4:5678` — single colon → strip
    assert svc.lookup("1.2.3.4:5678") is None
