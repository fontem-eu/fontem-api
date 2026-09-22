"""C3 — bare national identifiers.

eForms ``cbc:CompanyID`` often carries a national id with no
``schemeName`` (a PT NIF ``503536717``); ``canon_vat`` accepts only
country-prefixed VAT numbers, so the matcher saw no identifier,
name-matched and minted duplicates (three Visualforma nodes; 42,573 of
43,045 PT companies without a VAT, 2026-09-21).

This is the one rule that runs BEFORE identity assignment, because
identity comes from the matcher and the matcher needs the identifier.
It is deterministic — a NIF never changes — so re-running it never
re-keys anything. The per-country regex in ``canon_vat`` is the guard:
a DE Leitweg id (``053660036036-31001-86``) prefixed ``DE`` still
matches nothing and stays None.
"""
from __future__ import annotations

from typing import Sequence

from src.etl.identifiers import canon_vat

from ..facts import NoticeFacts, OrgFacts
from ..lookups import Lookups, vat_prefix_for
from ..outcomes import IdentifierNormalised, Outcome

# Schemes under which the legal id may be a VAT / national number (the
# same set the loader has always accepted).
IDENTIFIER_SCHEMES = frozenset({"VAT", "NATIONAL", "EORI", ""})


def scheme_accepted(org: OrgFacts) -> bool:
    return (org.legal_scheme or "").upper() in IDENTIFIER_SCHEMES


def base_identifier(org: OrgFacts) -> str | None:
    """What the loader always did: the canonical VAT of the legal id as
    published, or None."""
    if not org.legal_id or not scheme_accepted(org):
        return None
    return canon_vat(org.legal_id)


def prefixed_identifier(org: OrgFacts, lookups: Lookups) -> str | None:
    """The canonical VAT once the organisation's country prefix is put
    in front of the bare value, or None when that is no VAT either."""
    if not org.legal_id or not scheme_accepted(org):
        return None
    prefix = vat_prefix_for(org.country, lookups.iso3_to_vat_prefix)
    if not prefix:
        return None
    return canon_vat(prefix + org.legal_id.strip())


class NationalIdCountryPrefixedRule:
    """``generic.national_id_country_prefixed``: fires when prefixing
    the country turned a non-canonical legal id into a VAT."""

    id = "generic.national_id_country_prefixed"

    def applies(self, facts: NoticeFacts, lookups: Lookups) -> bool:  # pylint: disable=unused-argument
        return any(o.legal_id for o in facts.organizations)

    def decide(self, facts: NoticeFacts, lookups: Lookups) -> Sequence[Outcome]:
        out: list[Outcome] = []
        for org in facts.organizations:
            if not org.legal_id or base_identifier(org) is not None:
                continue
            canonical = prefixed_identifier(org, lookups)
            if canonical is None:
                continue
            out.append(IdentifierNormalised(
                rule_id=self.id, subject=org.org_id, raw=org.legal_id,
                canonical=canonical,
            ))
        return out
