"""
Comprehensive tests for the CurrencyService.

Covers:
- Sentinel detection (-1, 0.01)
- Alias resolution (USN -> USD)
- Country -> currency lookup with date boundaries
- Locked rate conversion (DEM, BGN, EEK, etc.)
- Daily rate conversion with weekend fallback
- Decimal precision
- Edge cases (None inputs, unknown currencies, far-future dates)
"""
# `svc` and `temp_rates_dir` are pytest fixtures — every test function
# legitimately re-binds their name as a local parameter. That's how pytest's
# DI works; flagging it as `redefined-outer-name` is noise. The JSON rate
# files are written with the platform-default encoding on purpose (the test
# never reads non-ASCII so an explicit encoding= adds no value).
# pylint: disable=redefined-outer-name,unspecified-encoding
from __future__ import annotations

import json
import shutil
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src.services.currency import CurrencyService


@pytest.fixture(scope="module")
def temp_rates_dir():
    """Create a temporary rates directory with realistic test data."""
    tmp = tempfile.mkdtemp()
    rates_dir = Path(tmp) / "rates"
    rates_dir.mkdir(parents=True)

    # PLN rates spanning 2010-2026
    pln_rates = {
        "2010-01-04": "4.0843", "2010-01-05": "4.0850",
        "2023-07-31": "4.4658", "2023-08-01": "4.4720",
        "2025-10-01": "4.28",   "2025-10-02": "4.30", "2025-10-03": "4.29",
        "2025-10-06": "4.31",   "2025-10-07": "4.32",
    }
    with open(rates_dir / "PLN.json", "w") as f:
        json.dump(pln_rates, f)

    # SEK rates
    sek_rates = {
        "2025-10-01": "11.28", "2025-10-02": "11.30", "2025-10-03": "11.25",
        "2025-10-06": "11.32",
    }
    with open(rates_dir / "SEK.json", "w") as f:
        json.dump(sek_rates, f)

    # CZK rates with gap to test fallback
    czk_rates = {
        "2025-10-01": "25.10", "2025-10-02": "25.15",
    }
    with open(rates_dir / "CZK.json", "w") as f:
        json.dump(czk_rates, f)

    # USD rates (alias target)
    usd_rates = {
        "2025-11-24": "1.10", "2025-11-25": "1.10",
    }
    with open(rates_dir / "USD.json", "w") as f:
        json.dump(usd_rates, f)

    yield tmp

    shutil.rmtree(tmp)


@pytest.fixture(scope="module")
def svc(temp_rates_dir):
    return CurrencyService.load(temp_rates_dir)


# ── Sentinel detection ────────────────────────────────────────────


class TestSentinelDetection:
    def test_minus_one(self):
        v, sentinel = CurrencyService.parse_value("-1")
        assert v is None
        assert sentinel is True

    def test_minus_one_float(self):
        v, sentinel = CurrencyService.parse_value("-1.0")
        assert v is None
        assert sentinel is True

    def test_one_cent(self):
        """0.01 is the most common 'placeholder' value in TED."""
        v, sentinel = CurrencyService.parse_value("0.01")
        assert v is None
        assert sentinel is True

    def test_normal_value(self):
        v, sentinel = CurrencyService.parse_value("1234567.89")
        assert v == Decimal("1234567.89")
        assert sentinel is False

    def test_negative_real_value(self):
        """Real negative values (contract reductions) should NOT be sentinels."""
        v, sentinel = CurrencyService.parse_value("-1588.18")
        assert v == Decimal("-1588.18")
        assert sentinel is False

    def test_zero(self):
        """Zero is not a sentinel — but a real zero value."""
        v, sentinel = CurrencyService.parse_value("0")
        assert v == Decimal("0")
        assert sentinel is False

    def test_none(self):
        v, sentinel = CurrencyService.parse_value(None)
        assert v is None
        assert sentinel is False

    def test_garbage(self):
        v, sentinel = CurrencyService.parse_value("not a number")
        assert v is None
        assert sentinel is False


# ── Alias resolution ──────────────────────────────────────────────


class TestAliases:
    def test_usn_to_usd(self, svc):
        assert svc.normalize_currency("USN") == "USD"

    def test_uss_to_usd(self, svc):
        assert svc.normalize_currency("USS") == "USD"

    def test_xdr_to_usd(self, svc):
        assert svc.normalize_currency("XDR") == "USD"

    def test_xeu_to_eur(self, svc):
        """XEU was the European Currency Unit, predecessor to EUR."""
        assert svc.normalize_currency("XEU") == "EUR"

    def test_lowercase(self, svc):
        assert svc.normalize_currency("eur") == "EUR"

    def test_whitespace(self, svc):
        assert svc.normalize_currency(" PLN ") == "PLN"

    def test_unknown_passthrough(self, svc):
        """Unknown codes pass through unchanged after normalization."""
        assert svc.normalize_currency("XYZ") == "XYZ"

    def test_none(self, svc):
        assert svc.normalize_currency(None) is None

    def test_empty(self, svc):
        assert svc.normalize_currency("") is None


# ── Country → currency history ────────────────────────────────────


class TestCountryHistory:
    def test_germany_today(self, svc):
        assert svc.currency_for("DEU", date(2025, 5, 1)) == "EUR"

    def test_germany_pre_euro(self, svc):
        assert svc.currency_for("DEU", date(1995, 6, 15)) == "DEM"

    def test_germany_eur_introduction(self, svc):
        """Jan 1, 1999 is the first day of EUR."""
        assert svc.currency_for("DEU", date(1999, 1, 1)) == "EUR"
        assert svc.currency_for("DEU", date(1998, 12, 31)) == "DEM"

    def test_estonia_pre_euro(self, svc):
        """Estonia adopted EUR on 2011-01-01."""
        assert svc.currency_for("EST", date(2010, 12, 31)) == "EEK"

    def test_estonia_post_euro(self, svc):
        assert svc.currency_for("EST", date(2011, 1, 1)) == "EUR"

    def test_bulgaria_2025(self, svc):
        """Bulgaria still uses BGN through 2025."""
        assert svc.currency_for("BGR", date(2025, 12, 31)) == "BGN"

    def test_bulgaria_2026(self, svc):
        """Bulgaria adopted EUR on 2026-01-01."""
        assert svc.currency_for("BGR", date(2026, 1, 1)) == "EUR"

    def test_croatia_2022(self, svc):
        assert svc.currency_for("HRV", date(2022, 12, 31)) == "HRK"

    def test_croatia_2023(self, svc):
        assert svc.currency_for("HRV", date(2023, 1, 1)) == "EUR"

    def test_poland_today(self, svc):
        """Poland never adopted EUR — still PLN."""
        assert svc.currency_for("POL", date(2025, 5, 1)) == "PLN"

    def test_uk_today(self, svc):
        assert svc.currency_for("GBR", date(2025, 5, 1)) == "GBP"

    def test_liechtenstein_uses_chf(self, svc):
        """Liechtenstein has no own currency — uses CHF."""
        assert svc.currency_for("LIE", date(2025, 5, 1)) == "CHF"

    def test_montenegro_uses_eur(self, svc):
        """Montenegro unilaterally uses EUR (not in eurozone)."""
        assert svc.currency_for("MNE", date(2025, 5, 1)) == "EUR"

    def test_unknown_country(self, svc):
        assert svc.currency_for("XYZ", date(2025, 5, 1)) is None

    def test_empty_country(self, svc):
        assert svc.currency_for("", date(2025, 5, 1)) is None


# ── resolve_currency: declared + country fallback ─────────────────


class TestResolveCurrency:
    def test_declared_takes_precedence(self, svc):
        """If a currency is declared, use it (after alias normalization)."""
        ccy, inferred = svc.resolve_currency("PLN", "DEU", date(2025, 1, 1))
        assert ccy == "PLN"
        assert inferred is False

    def test_alias_resolved(self, svc):
        ccy, inferred = svc.resolve_currency("USN", "POL", date(2025, 1, 1))
        assert ccy == "USD"
        assert inferred is False

    def test_inferred_from_country(self, svc):
        ccy, inferred = svc.resolve_currency(None, "DEU", date(2025, 1, 1))
        assert ccy == "EUR"
        assert inferred is True

    def test_inferred_for_estonia_pre_euro(self, svc):
        ccy, inferred = svc.resolve_currency(None, "EST", date(2008, 5, 1))
        assert ccy == "EEK"
        assert inferred is True

    def test_no_declared_no_country(self, svc):
        ccy, inferred = svc.resolve_currency(None, None, None)
        assert ccy is None
        assert inferred is False


# ── Conversion: locked rates ──────────────────────────────────────


class TestLockedRates:
    def test_dem_after_lock(self, svc):
        """1000 DEM after 1999-01-01 = 1000 / 1.95583 = 511.29 EUR."""
        eur = svc.to_eur(Decimal("1000"), "DEM", date(2000, 5, 15))
        assert eur == Decimal("511.29")

    def test_dem_before_lock_uses_approx(self, svc):
        """Pre-lock DEM uses the lock rate as fallback (no daily rates loaded)."""
        eur = svc.to_eur(Decimal("1000"), "DEM", date(1995, 6, 1))
        # Should be approx the locked rate
        assert eur == Decimal("511.29")

    def test_bgn_after_2026_lock(self, svc):
        """1000 BGN after 2026-01-01 = 1000 / 1.95583 = 511.29 EUR."""
        eur = svc.to_eur(Decimal("1000"), "BGN", date(2026, 5, 1))
        assert eur == Decimal("511.29")

    def test_eek_after_lock(self, svc):
        """1000 EEK after 2011-01-01 = 1000 / 15.6466 = 63.91 EUR."""
        eur = svc.to_eur(Decimal("1000"), "EEK", date(2012, 1, 1))
        assert eur == Decimal("63.91")

    def test_hrk_after_lock(self, svc):
        """Croatian Kuna after 2023-01-01."""
        eur = svc.to_eur(Decimal("7534.50"), "HRK", date(2023, 6, 1))
        assert eur == Decimal("1000.00")

    def test_locked_provenance(self, svc):
        """convert_detailed should report 'locked' source for fixed rates."""
        result = svc.convert_detailed(Decimal("1000"), "DEM", date(2010, 1, 1))
        assert result.source == "locked"
        assert result.rate_used == Decimal("1.95583")

    def test_xof_pegged(self, svc):
        """CFA franc is pegged to EUR at 655.957."""
        eur = svc.to_eur(Decimal("655957"), "XOF", date(2024, 1, 1))
        assert eur == Decimal("1000.00")


# ── Conversion: daily rates ───────────────────────────────────────


class TestDailyRates:
    def test_pln_exact_date(self, svc):
        """5000 PLN at 4.28 = 1168.22 EUR."""
        eur = svc.to_eur(Decimal("5000"), "PLN", date(2025, 10, 1))
        assert eur == Decimal("1168.22")

    def test_sek_exact(self, svc):
        eur = svc.to_eur(Decimal("11280"), "SEK", date(2025, 10, 1))
        assert eur == Decimal("1000.00")

    def test_eur_passthrough(self, svc):
        eur = svc.to_eur(Decimal("1234.56"), "EUR", date(2025, 10, 1))
        assert eur == Decimal("1234.56")

    def test_weekend_fallback(self, svc):
        """Saturday Oct 4: should use Friday Oct 3 rate."""
        eur = svc.to_eur(Decimal("11250"), "SEK", date(2025, 10, 4))
        # Oct 3 SEK rate = 11.25
        assert eur == Decimal("1000.00")

    def test_sunday_fallback(self, svc):
        """Sunday Oct 5: still falls back to Friday Oct 3."""
        eur = svc.to_eur(Decimal("11250"), "SEK", date(2025, 10, 5))
        assert eur == Decimal("1000.00")

    def test_unknown_date_returns_none(self, svc):
        """Date with no rate within 7-day window returns None."""
        eur = svc.to_eur(Decimal("1000"), "SEK", date(2030, 1, 1))
        assert eur is None

    def test_unknown_currency_returns_none(self, svc):
        eur = svc.to_eur(Decimal("1000"), "XYZ", date(2025, 10, 1))
        assert eur is None

    def test_none_value(self, svc):
        assert svc.to_eur(None, "PLN", date(2025, 10, 1)) is None

    def test_none_date_for_non_eur(self, svc):
        assert svc.to_eur(Decimal("1000"), "PLN", None) is None

    def test_none_date_for_eur_works(self, svc):
        """EUR conversion doesn't need a date."""
        assert svc.to_eur(Decimal("1000"), "EUR", None) == Decimal("1000.00")

    def test_provenance_ecb(self, svc):
        result = svc.convert_detailed(Decimal("5000"), "PLN", date(2025, 10, 1))
        assert result.source == "ecb"
        assert result.rate_used == Decimal("4.28")
        assert result.rate_date == date(2025, 10, 1)


# ── Decimal precision ────────────────────────────────────────────


class TestDecimalPrecision:
    def test_no_float_drift(self, svc):
        """Repeated conversions on a value above rounding threshold sum cleanly."""
        total = Decimal("0")
        for _ in range(1000):
            eur = svc.to_eur(Decimal("100"), "PLN", date(2025, 10, 1))
            total += eur
        # 100 / 4.28 = 23.36 EUR per conversion (rounded to 2dp)
        # 1000 * 23.36 = 23360.00 — exact integer math, no float drift
        assert total == Decimal("23360.00")

    def test_huge_value(self, svc):
        """Very large values don't lose precision."""
        eur = svc.to_eur(Decimal("999999999999.99"), "EUR", date(2025, 10, 1))
        assert eur == Decimal("999999999999.99")

    def test_input_as_string(self, svc):
        """String inputs work."""
        eur = svc.to_eur("5000", "PLN", date(2025, 10, 1))
        assert eur == Decimal("1168.22")

    def test_input_as_int(self, svc):
        eur = svc.to_eur(5000, "PLN", date(2025, 10, 1))
        assert eur == Decimal("1168.22")

    def test_input_as_float_works_but_lossy(self, svc):
        """Float input works but is converted via str to avoid binary drift."""
        eur = svc.to_eur(5000.0, "PLN", date(2025, 10, 1))
        assert eur == Decimal("1168.22")


# ── Edge cases ────────────────────────────────────────────────────


class TestEdgeCases:
    def test_garbage_value(self, svc):
        eur = svc.to_eur("not a number", "PLN", date(2025, 10, 1))
        assert eur is None

    def test_empty_currency(self, svc):
        eur = svc.to_eur(Decimal("1000"), "", date(2025, 10, 1))
        assert eur is None

    def test_known_currencies_lists_all(self, svc):
        known = svc.known_currencies()
        assert "EUR" in known
        assert "PLN" in known
        assert "SEK" in known
        assert "DEM" in known  # locked
        assert "BGN" in known  # locked
        assert "EEK" in known  # locked

    def test_currency_coverage(self, svc):
        earliest, latest = svc.currency_coverage("PLN")
        assert earliest is not None
        assert latest is not None
        assert earliest <= latest

    def test_currency_coverage_unknown(self, svc):
        earliest, latest = svc.currency_coverage("XYZ")
        assert earliest is None
        assert latest is None
