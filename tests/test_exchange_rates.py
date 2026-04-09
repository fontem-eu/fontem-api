"""Tests for ECB exchange rate loading and conversion."""
import json
import os
import tempfile

import pytest

from src.etl.load_exchange_rates import to_eur, save_rates, load_rates


# Realistic sample rates (units of CCY per 1 EUR)
SAMPLE_RATES = {
    "SEK": {
        "2025-10-01": 11.28,
        "2025-10-02": 11.30,
        "2025-10-03": 11.25,
        # No weekend entries (Oct 4-5 are Sat-Sun)
        "2025-10-06": 11.32,
    },
    "PLN": {
        "2025-10-01": 4.28,
        "2025-10-02": 4.30,
    },
    "HUF": {
        "2025-10-01": 402.5,
    },
}


class TestToEur:
    """Test the to_eur conversion function."""

    def test_eur_passthrough(self):
        """EUR values returned as-is (just rounded)."""
        assert to_eur(1234567.89, "EUR", "2025-10-01", SAMPLE_RATES) == 1234567.89

    def test_eur_none_value(self):
        assert to_eur(None, "EUR", "2025-10-01", SAMPLE_RATES) is None

    def test_sek_conversion(self):
        """1000 SEK at rate 11.28 = 1000/11.28 = 88.65 EUR."""
        result = to_eur(1000.0, "SEK", "2025-10-01", SAMPLE_RATES)
        assert result == pytest.approx(88.65, abs=0.01)

    def test_pln_conversion(self):
        """5000 PLN at rate 4.28 = 5000/4.28 = 1168.22 EUR."""
        result = to_eur(5000.0, "PLN", "2025-10-01", SAMPLE_RATES)
        assert result == pytest.approx(1168.22, abs=0.01)

    def test_huf_conversion(self):
        """1000000 HUF at rate 402.5 = 2484.47 EUR (not millions)."""
        result = to_eur(1_000_000.0, "HUF", "2025-10-01", SAMPLE_RATES)
        assert result == pytest.approx(2484.47, abs=0.01)
        # This was the bug: old hardcoded rate produced insane values
        assert result < 10_000  # sanity check

    def test_weekend_fallback(self):
        """Weekend dates fall back to the preceding Friday's rate."""
        # Oct 4 is Saturday, should use Oct 3 rate (11.25)
        result = to_eur(1125.0, "SEK", "2025-10-04", SAMPLE_RATES)
        assert result == pytest.approx(100.0, abs=0.01)

    def test_sunday_fallback(self):
        """Sunday also falls back."""
        result = to_eur(1125.0, "SEK", "2025-10-05", SAMPLE_RATES)
        assert result == pytest.approx(100.0, abs=0.01)

    def test_unknown_currency(self):
        """Unknown currency returns None."""
        assert to_eur(1000.0, "XYZ", "2025-10-01", SAMPLE_RATES) is None

    def test_none_date(self):
        """None date returns None for non-EUR currencies."""
        assert to_eur(1000.0, "SEK", None, SAMPLE_RATES) is None

    def test_invalid_date(self):
        """Invalid date string returns None."""
        assert to_eur(1000.0, "SEK", "not-a-date", SAMPLE_RATES) is None

    def test_far_future_date_no_rate(self):
        """Date with no rate within 5-day window returns None."""
        assert to_eur(1000.0, "SEK", "2030-01-01", SAMPLE_RATES) is None

    def test_large_sek_value_realistic(self):
        """A large SEK contract should convert to reasonable EUR.

        The 'Umeå lokaltrafik' bug was 185 trillion EUR from SEK.
        A realistic large Swedish transport contract might be 2 billion SEK.
        """
        result = to_eur(2_000_000_000.0, "SEK", "2025-10-01", SAMPLE_RATES)
        assert result == pytest.approx(177_304_964.54, abs=1.0)
        assert result < 1e9  # under 1 billion EUR — realistic


class TestSaveLoad:
    """Test rates file persistence."""

    def test_roundtrip(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            save_rates(SAMPLE_RATES, path)
            loaded = load_rates(path)
            assert loaded["SEK"]["2025-10-01"] == 11.28
            assert loaded["PLN"]["2025-10-02"] == 4.30
        finally:
            os.unlink(path)
