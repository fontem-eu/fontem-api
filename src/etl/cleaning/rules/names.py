"""C2 — the supplier name field holds something that is not a name.

Four rules, cheapest and most specific first. A supplier hit by any of
them is WITHHELD: no UpsertCompany, no party, no company_gmr_id; the
raw text travels in ``suppliers_withheld`` as a pointer to where the
real award is published (the notice itself never carries the real
name — verified on 184512-2013, where the sentence sits inside
``<OFFICIALNAME>`` and nothing else names the operator).

Layer 3 of the plan (a small classifier over the sentence embedding
for the ambiguous middle) is NOT implemented here. Extension point:
add a rule class in this module that reads ``facts.suppliers`` and
returns ``SupplierWithheld`` outcomes, and append it to ``NAME_RULES``
in ``rules/__init__.py`` AFTER the four hard-signal rules; its scores
must arrive through ``Lookups`` (precomputed), never by calling a
service from inside the rule.
"""
from __future__ import annotations

import re
from typing import Sequence

from ..facts import NoticeFacts, OrgFacts
from ..lexicon import (
    LEGAL_FORM_PHRASES, LEGAL_FORM_SHORT_TOKENS, LEGAL_FORM_TOKENS,
    NOTICE_LANGUAGE_TO_LIST, PLACEHOLDER_NAMES, STOPWORDS,
)
from ..lookups import Lookups
from ..outcomes import Outcome, SupplierWithheld

# The real strings, from the "Gara aggiudicata come da determina n. 543
# del 2013 pubblicata sul sito www.csc.sanita.fvg.it" family (419 such
# :Company names in prod, 2026-09-22).
#
# Every phrase is word-bounded, and the plan's bare "vedi " is NOT here.
# Measured on prod: "vedi " as a substring matches 116 real companies —
# TRIVEDI, MEDVEDI, CHATURVEDI, Arvedi, MARAVEDI, ProVedi, and the
# Hungarian UGYVEDI IRODA ("law office") family. Even word-bounded,
# "vedi" alone is a real name (VEDI TOURGROUP s.r.o., CZ). Only the
# unambiguous imperative "si veda" survives; the junk it would have
# caught is caught by the decree and sentence signals anyway.
_ITALIAN_NOTICE_TEXT = re.compile(
    r"\bgara aggiudicata\b|\bcome da determina\b|\bdetermina n\.\s*\d"
    r"|\bpubblicat[ao] sul sito\b|\bsi veda\b|\baggiudicat[ao] con\b"
    r"|\blotto n\.\s*\d",
    re.IGNORECASE,
)
# A URL with a scheme is a pointer to a page — no company is named
# "https://contractaciopublica.cat/ca/detall-publicacio/300750688".
_URL_SCHEME = re.compile(r"https?://", re.IGNORECASE)
# A bare domain is NOT that signal: WWW.KOTVA.CZ a.s., www.heymountain.com
# GmbH, WWW.THEARTISANCO.CO.UK LIMITED and HTTPS OÜ are real companies
# trading under their domain (226 prod names contain "http" at all). A
# domain only withholds when it sits inside a phrase and no legal form
# vouches for the name.
_URL_DOMAIN = re.compile(r"\bwww\.", re.IGNORECASE)
URL_PHRASE_MIN_TOKENS = 4

# "Varios adjudicatarios", "DIFERENTES ADJUDICATARIOS", "DIVERSOS
# HOMOLOGADOS/HOMOLOGATS" — the Spanish and Catalan gateways' way of
# saying "several winners, see the resolution", with or without a link.
# Unambiguous: no company trades under any of these.
_MULTIPLE_AWARDEES_ES = re.compile(
    r"\b(varios|diversos|diferentes|distintos) adjudicatari|"
    r"\bdiversos homologa[dt]", re.IGNORECASE,
)
# "n. 543 del 2013": a decree reference — the shape of a pointer to a
# published decision, never of a name.
_DECREE_CLAUSE = re.compile(r"\bn\.?\s*\d+\s+del\s+\d{4}\b", re.IGNORECASE)

SENTENCE_MIN_CHARS = 80
SENTENCE_MIN_TOKENS = 8
SENTENCE_FUNCTION_WORD_RATIO = 0.35

_TOKEN_SPLIT = re.compile(r"[\s,;()\[\]\"]+")
_TOKEN_TRIM = ".,;:-–—'\""
_ALL_STOPWORDS = frozenset().union(*STOPWORDS.values())
_LEGAL_PHRASES = tuple(re.compile(p) for p in LEGAL_FORM_PHRASES)


def _tokens(name: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(name) if t]


def _norm(token: str) -> str:
    return re.sub(r"[./]", "", token.strip(_TOKEN_TRIM)).casefold()


def has_legal_form(name: str) -> bool:
    """True when the name carries a legal-form token (S.p.A., Lda, GmbH,
    Sp. z o.o., ...). Two-letter forms count only dotted ("S.A.") or as
    the upper-case last token ("Vattenfall AB") — see lexicon."""
    tokens = _tokens(name)
    if not tokens:
        return False
    norms = [_norm(t) for t in tokens]
    for raw, norm in zip(tokens, norms):
        if norm in LEGAL_FORM_TOKENS:
            return True
        if norm in LEGAL_FORM_SHORT_TOKENS and ("." in raw or "/" in raw):
            return True
    last_raw, last_norm = tokens[-1], norms[-1]
    if last_norm in LEGAL_FORM_SHORT_TOKENS and last_raw.strip(_TOKEN_TRIM).isupper():
        return True
    joined = " ".join(n for n in norms if n)
    return any(p.search(joined) for p in _LEGAL_PHRASES)


def function_word_ratio(name: str, notice_language: str | None) -> float:
    """Share of tokens that are function words of the notice's language
    (all lists when the language is unknown)."""
    tokens = _tokens(name)
    if not tokens:
        return 0.0
    key = NOTICE_LANGUAGE_TO_LIST.get((notice_language or "").upper())
    words = STOPWORDS[key] if key else _ALL_STOPWORDS
    hits = sum(1 for t in tokens if _norm(t) in words)
    return hits / len(tokens)


def is_sentence(name: str, notice_language: str | None) -> bool:
    """Long, many-tokened, and either a decree clause or mostly function
    words. A legal-form token exempts the name from this rule only."""
    if len(name) <= SENTENCE_MIN_CHARS or len(_tokens(name)) < SENTENCE_MIN_TOKENS:
        return False
    if has_legal_form(name):
        return False
    if _DECREE_CLAUSE.search(name):
        return True
    return function_word_ratio(name, notice_language) >= SENTENCE_FUNCTION_WORD_RATIO


class _NameRule:
    """Shared shape: run ``hits(name, facts)`` over every named supplier."""

    id = ""

    def applies(self, facts: NoticeFacts, lookups: Lookups) -> bool:  # pylint: disable=unused-argument
        return any(o.name for o in facts.suppliers)

    def hits(self, name: str, facts: NoticeFacts) -> bool:
        raise NotImplementedError

    def decide(self, facts: NoticeFacts, lookups: Lookups) -> Sequence[Outcome]:  # pylint: disable=unused-argument
        return [
            self._withhold(org) for org in facts.suppliers
            if org.name and self.hits(org.name, facts)
        ]

    def _withhold(self, org: OrgFacts) -> SupplierWithheld:
        return SupplierWithheld(
            rule_id=self.id, subject=org.org_id, name_raw=org.name or "",
            role=org.role,
        )


class ItalianNoticeTextRule(_NameRule):
    """``it.notice_text_in_supplier_name``: the Italian award-decree
    boilerplate. Language-specific by content, so it runs on every
    notice — the phrases exist in no other language."""

    id = "it.notice_text_in_supplier_name"

    def hits(self, name: str, facts: NoticeFacts) -> bool:
        return bool(_ITALIAN_NOTICE_TEXT.search(name))


class NameContainsUrlRule(_NameRule):
    """``generic.name_contains_url``: the name points at a web page
    instead of naming anyone.

    A scheme URL always withholds. A bare ``www.`` domain withholds only
    when the name reads as a phrase (>= 4 tokens) and carries no legal
    form — otherwise it is a company trading under its domain."""

    id = "generic.name_contains_url"

    def hits(self, name: str, facts: NoticeFacts) -> bool:
        if _URL_SCHEME.search(name):
            return True
        if not _URL_DOMAIN.search(name):
            return False
        return (len(_tokens(name)) >= URL_PHRASE_MIN_TOKENS
                and not has_legal_form(name))


class MultipleAwardeesRule(_NameRule):
    """``es.multiple_awardees_placeholder``: the ES/CA gateways' "several
    winners, see the resolution" in the supplier name field."""

    id = "es.multiple_awardees_placeholder"

    def hits(self, name: str, facts: NoticeFacts) -> bool:
        return bool(_MULTIPLE_AWARDEES_ES.search(name))


class NameIsPlaceholderRule(_NameRule):
    """``generic.name_is_placeholder``: n/a, diversi, vários, -, ..."""

    id = "generic.name_is_placeholder"

    def hits(self, name: str, facts: NoticeFacts) -> bool:
        return name.strip().casefold() in PLACEHOLDER_NAMES


class NameIsSentenceRule(_NameRule):
    """``generic.name_is_sentence``: shape features, language-aware."""

    id = "generic.name_is_sentence"

    def hits(self, name: str, facts: NoticeFacts) -> bool:
        return is_sentence(name, facts.notice_language)
