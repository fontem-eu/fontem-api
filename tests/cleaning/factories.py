"""Builders for cleaning-stage fixtures.

A fixture here is a ``NoticeFacts`` literal — the whole point of the
pure rule library is that a test needs no parser, no database and no
matcher to state a case.
"""
from src.etl.cleaning.facts import (
    ROLE_BUYER, ROLE_WINNER, NoticeFacts, OrgFacts, ValueFacts,
)


def org(name=None, *, org_id="ORG-1", country="ITA", legal_id=None,  # pylint: disable=too-many-arguments
        legal_scheme=None, role=ROLE_WINNER):
    """One organisation as a notice publishes it."""
    return OrgFacts(org_id=org_id, name=name, country=country,
                    legal_id=legal_id, legal_scheme=legal_scheme, role=role)


def buyer(name="Comune di Trieste", *, country="ITA", legal_id=None,
          legal_scheme=None, org_id="BUYER-1"):
    return org(name, org_id=org_id, country=country, legal_id=legal_id,
               legal_scheme=legal_scheme, role=ROLE_BUYER)


def facts(*organizations, value=None, notice_id="646890-2026", **kwargs):  # pylint: disable=redefined-outer-name
    """A notice's facts; every date/language field defaults to absent."""
    return NoticeFacts(
        notice_id=notice_id,
        organizations=tuple(organizations),
        value=value if value is not None else ValueFacts(),
        **kwargs,
    )


def value(total_eur=None, **kwargs):
    return ValueFacts(total_eur=total_eur, **kwargs)
