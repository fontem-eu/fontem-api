"""Unit tests for src/api/lang.py — whitelist + Cypher fragment builder."""
from __future__ import annotations

import pytest

from src.api.lang import (
    EU_LANGS, authority_name_expr, contract_title_expr, safe_lang,
)


class TestSafeLang:
    def test_accepts_each_eu_code(self):
        for code in EU_LANGS:
            assert safe_lang(code) == code

    @pytest.mark.parametrize("raw,expected", [
        ("EN", "en"),
        ("en", "en"),
        (" de ", "de"),
        ("Fr", "fr"),
        ("pt-BR", "pt"),    # region suffix stripped
        ("en_GB", "en"),    # POSIX-style suffix stripped
        ("pl-PL-u-va-posix", "pl"),
    ])
    def test_normalises_case_and_region(self, raw, expected):
        assert safe_lang(raw) == expected

    @pytest.mark.parametrize("raw", [
        None,
        "",
        "   ",
        "ja",            # not an EU language
        "zh-CN",
        "xx",
        "x",
        "<script>",
        "1;MATCH",
        "', 'x",
        123,             # wrong type
        object(),
    ])
    def test_rejects_invalid(self, raw):
        assert safe_lang(raw) is None


class TestAuthorityNameExpr:
    def test_returns_plain_name_when_no_lang(self):
        assert authority_name_expr("a", None) == "a.name"
        assert authority_name_expr("authority", None) == "authority.name"

    def test_returns_coalesce_for_valid_lang(self):
        assert authority_name_expr("a", "pl") == "coalesce(a.name_pl, a.name)"
        assert authority_name_expr("dup", "de") == "coalesce(dup.name_de, dup.name)"

    def test_alias_is_inlined_verbatim(self):
        # Callers pass trusted aliases; we don't escape them.
        out = authority_name_expr("canonical", "fr")
        assert out == "coalesce(canonical.name_fr, canonical.name)"

    def test_empty_string_lang_falls_back(self):
        assert authority_name_expr("a", "") == "a.name"


class TestContractTitleExpr:
    def test_returns_plain_title_when_no_lang(self):
        assert contract_title_expr("ct", None) == "ct.title"

    def test_returns_coalesce_for_valid_lang(self):
        assert contract_title_expr("ct", "de") == "coalesce(ct.title_de, ct.title)"
        assert contract_title_expr("x", "pl") == "coalesce(x.title_pl, x.title)"

    def test_empty_string_lang_falls_back(self):
        assert contract_title_expr("ct", "") == "ct.title"


class TestIntegrationContract:
    """safe_lang → authority_name_expr is the only path callers should
    wire. Any input survives safe_lang first."""

    @pytest.mark.parametrize("user_input", [
        "EN", "pt-BR", "malicious'; drop", "", None, "zh", "IT",
    ])
    def test_end_to_end_never_produces_dangerous_fragment(self, user_input):
        lang = safe_lang(user_input)
        expr = authority_name_expr("a", lang)
        # Either 'a.name' (safe constant) or 'coalesce(a.name_<2-letter-ISO>, a.name)'
        assert expr == "a.name" or expr.startswith("coalesce(a.name_") and " " not in expr.split("coalesce(")[1].split(",")[0][len("a.name_"):]
        # And should never contain SQL/Cypher injection characters
        assert ";" not in expr
        assert "'" not in expr
        assert '"' not in expr
