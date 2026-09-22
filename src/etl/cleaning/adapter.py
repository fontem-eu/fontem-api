"""From the parser's notice to ``NoticeFacts``.

The one place that knows the eforms-parser object shape. Everything is
coerced defensively: a field that is not a plain string / number (an
older wheel without the attribute, a test double) becomes None rather
than a surprise inside a rule.
"""
from __future__ import annotations

from .facts import (
    ROLE_BUYER, ROLE_NAMED_TENDERER, ROLE_WINNER, NoticeFacts, OrgFacts,
    ValueFacts,
)

BUYER_FALLBACK_ORG_ID = "buyer"


def text(value) -> str | None:
    """A non-empty string, else None."""
    return value if isinstance(value, str) and value else None


def number(value) -> float | None:
    """A float, else None (bools are not numbers here)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def integer(value) -> int | None:
    """A non-negative int, else None."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def org_facts(org, org_id: str, role: str) -> OrgFacts:
    legal = getattr(org, "legal_id", None)
    return OrgFacts(
        org_id=org_id,
        name=text(getattr(org, "name", None)),
        country=text(getattr(org, "country", None)),
        legal_id=text(getattr(legal, "value", None)) if legal is not None else None,
        legal_scheme=(text(getattr(legal, "scheme_name", None))
                      if legal is not None else None),
        role=role,
    )


def _supplier_roles(notice) -> dict[str, str]:
    """org_id -> role, one entry per named supplier; a supplier that won
    any lot is a winner even if it also lost another."""
    roles: dict[str, str] = {}
    for award in notice.awards:
        org_id = award.contractor_org_id
        if org_id not in notice.organizations:
            continue
        is_winner = bool(getattr(award, "is_winner", True))
        if is_winner or org_id not in roles:
            roles[org_id] = ROLE_WINNER if is_winner else ROLE_NAMED_TENDERER
    return roles


def facts_from_notice(notice, *, value: ValueFacts, context_award=None) -> NoticeFacts:
    """Build the facts of one notice. ``context_award`` is the award the
    loader takes its dates and currency from (the primary winner's)."""
    orgs: list[OrgFacts] = []
    buyer = notice.buyer()
    if buyer is not None:
        buyer_id = text(getattr(notice, "buyer_org_id", None)) or BUYER_FALLBACK_ORG_ID
        orgs.append(org_facts(buyer, buyer_id, ROLE_BUYER))
    for org_id, role in _supplier_roles(notice).items():
        orgs.append(org_facts(notice.organizations[org_id], org_id, role))
    return NoticeFacts(
        notice_id=text(getattr(notice, "publication_number", None))
        or text(getattr(notice, "notice_id", None)) or "",
        organizations=tuple(orgs),
        value=value,
        notice_language=text(getattr(notice, "notice_language", None)),
        award_date_raw=text(getattr(context_award, "award_date_raw", None)),
        tender_result_award_date_raw=text(
            getattr(notice, "tender_result_award_date_raw", None)),
        tender_reference=text(getattr(context_award, "tender_reference", None)),
        publication_date_raw=text(getattr(notice, "publication_date", None)),
        issue_date_raw=text(getattr(notice, "issue_date", None)),
    )
