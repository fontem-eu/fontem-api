"""Graph indexes the API depends on, declared rather than assumed.

`/search` generates candidates from a full-text index on Company.name. That
index existed in production and in no other environment — created by hand at
some point and guaranteed by nothing. The first deploy that used it returned
zero results in testing while looking perfectly healthy, because a missing
index is not a loud failure here.

An index a query depends on belongs next to the query. This runs at startup,
is idempotent, and is a no-op wherever the index already exists.

Deliberately non-fatal: an API that refuses to start because it could not
create an index is worse than one that starts and logs. The search path
degrades on its own if the index is genuinely absent.
"""
from __future__ import annotations

from loguru import logger

#: The index /search reads. Neo4j populates it in the background, so a fresh
#: environment answers with partial results for a short while rather than
#: blocking startup.
COMPANY_NAME_FULLTEXT = "company_name_ft"

#: The two indexes a feed query over procurement needs.
#:
#: A feed asks "what was published since my last visit, in my regions". Both
#: halves of that were unindexed: Contract had indexes on its identifiers only,
#: and Authority none on nuts. Measured on prod before adding these, an
#: EU-wide seven-day window took 51 seconds — a full scan of 1.65M Contract
#: nodes, which is well past the proxy's statement timeout and would simply
#: fail. A region-scoped window took 2-3 seconds by scanning just as much and
#: throwing most of it away.
#:
#: publication_date is stored as an ISO 'YYYY-MM-DD' string, so a range index
#: gives ordered seeks on `>` for free — lexicographic and chronological order
#: coincide for that format.
CONTRACT_PUBLICATION_DATE = "contract_publication_date"
AUTHORITY_NUTS = "authority_nuts"

_STATEMENTS = (
    f"CREATE FULLTEXT INDEX {COMPANY_NAME_FULLTEXT} IF NOT EXISTS "
    "FOR (c:Company) ON EACH [c.name]",
    f"CREATE INDEX {CONTRACT_PUBLICATION_DATE} IF NOT EXISTS "
    "FOR (c:Contract) ON (c.publication_date)",
    f"CREATE INDEX {AUTHORITY_NUTS} IF NOT EXISTS "
    "FOR (a:Authority) ON (a.nuts)",
)


def ensure_indexes(neo4j) -> list[str]:
    """Create any missing index. Returns the statements that ran."""
    ran = []
    try:
        with neo4j.session() as session:
            for statement in _STATEMENTS:
                session.run(statement)
                ran.append(statement)
    except Exception as exc:  # pylint: disable=broad-except
        # Logged, not raised: see the module docstring.
        logger.warning("could not ensure graph indexes: {}", exc)
    return ran
