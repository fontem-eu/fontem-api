"""The ingest cleaning stage (data-backlog Part 5, C2-C5).

A pure rule library: ``run_stage(facts, lookups)`` takes plain facts
about one notice and injected data, and returns what the loader must
apply — suppliers to withhold, identifiers to hand the matcher, a value
decision — plus the list of rule ids that fired. No I/O anywhere in
this package. See README.md for the rule table.
"""
from .facts import (
    ROLE_BUYER, ROLE_NAMED_TENDERER, ROLE_WINNER, NoticeFacts, OrgFacts,
    ValueFacts,
)
from .lookups import InMemoryPeerStats, Lookups, PeerBand, PeerStats
from .outcomes import Keep, Quarantine, Rescale, ValueDecision
from .report import CleaningReport
from .stage import CleaningResult, run_stage

__all__ = [
    "ROLE_BUYER", "ROLE_NAMED_TENDERER", "ROLE_WINNER",
    "NoticeFacts", "OrgFacts", "ValueFacts",
    "InMemoryPeerStats", "Lookups", "PeerBand", "PeerStats",
    "Keep", "Quarantine", "Rescale", "ValueDecision",
    "CleaningReport", "CleaningResult", "run_stage",
]
