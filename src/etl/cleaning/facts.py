"""The input shape of the cleaning stage: plain facts about one notice.

Rules never see the parser's objects, the matcher or a database — they
see these frozen dataclasses, built once per notice by the loader's
adapter. That is what makes the rule library pure: a fixture is a
``NoticeFacts`` literal, and a rule's verdict is a function of it and
of the injected ``Lookups`` alone.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

# Roles an organisation plays on a notice. Buyers are cleaned for their
# identifier only; the name rules run on suppliers.
ROLE_BUYER = "buyer"
ROLE_WINNER = "winner"
ROLE_NAMED_TENDERER = "named_tenderer"

# The watermark pair one gateway family stamps on its awards: the lot
# result's award date left at the epoch placeholder and the tender
# reference set to "0.0". A prior, not proof (data-backlog C4).
GATEWAY_PLACEHOLDER_DATE = "2000-01-01"
GATEWAY_PLACEHOLDER_TENDER_REFERENCE = "0.0"


@dataclass(frozen=True)
class OrgFacts:
    """One organisation as the notice publishes it."""

    org_id: str
    name: str | None
    country: str | None            # alpha-2 or alpha-3, as published
    legal_id: str | None           # cbc:CompanyID text, verbatim
    legal_scheme: str | None       # its @schemeName, verbatim
    role: str                      # ROLE_*


@dataclass(frozen=True)
class ValueFacts:
    """The notice's money signals in EUR (plus the original-currency
    figures the rescale rules must keep in step), after FX and before
    any scale rule. ``country`` is the buyer's alpha-3 and ``cpv`` the
    main CPV — the peer group key."""

    estimate_eur: float | None = None
    total_eur: float | None = None
    payable_eur: float | None = None
    total_original: float | None = None
    payable_original: float | None = None
    country: str | None = None
    cpv: str | None = None

    @property
    def chosen_eur(self) -> float | None:
        """The value the scorer will store: the total, else the payable
        (mirrors ``contract_confidence.score_contract_value``)."""
        if self.total_eur is not None and self.total_eur > 0:
            return self.total_eur
        if self.payable_eur is not None and self.payable_eur > 0:
            return self.payable_eur
        return None

    @property
    def cpv4(self) -> str | None:
        return self.cpv[:4] if self.cpv and len(self.cpv) >= 4 else None


@dataclass(frozen=True)
class NoticeFacts:  # pylint: disable=too-many-instance-attributes
    """Everything the rules may read about one notice."""

    notice_id: str
    organizations: tuple[OrgFacts, ...]
    value: ValueFacts
    notice_language: str | None = None
    # Raw date / reference text the date and scale rules read verbatim.
    award_date_raw: str | None = None
    tender_result_award_date_raw: str | None = None
    tender_reference: str | None = None
    publication_date_raw: str | None = None
    issue_date_raw: str | None = None

    @property
    def suppliers(self) -> tuple[OrgFacts, ...]:
        return tuple(o for o in self.organizations if o.role != ROLE_BUYER)

    @property
    def has_gateway_placeholder(self) -> bool:
        """True when the notice carries either half of the watermark
        pair, which lowers the evidence bar for a scale outlier."""
        date_raw = self.tender_result_award_date_raw or ""
        return (
            date_raw.startswith(GATEWAY_PLACEHOLDER_DATE)
            or self.tender_reference == GATEWAY_PLACEHOLDER_TENDER_REFERENCE
        )

    def with_value(self, value: ValueFacts) -> "NoticeFacts":
        return replace(self, value=value)
