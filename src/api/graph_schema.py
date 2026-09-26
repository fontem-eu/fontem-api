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

#: The key /lobbyists/{disclosure_id} looks up on.
#:
#: Lobbyist carried no index at all, so the profile route was a
#: NodeByLabelScan over 18,195 nodes. Small next to Company, but this
#: backs one page per lobbyist and the sitemap advertises every one of
#: them, so a crawler turns it into 18,195 scans.
#:
#: disclosure_id is also the ONLY identifier these nodes actually have:
#: gmr_id, tr_id and transparency_register_id are each present on zero
#: of them, despite code having referenced all three.
LOBBYIST_DISCLOSURE_ID = "lobbyist_disclosure_id"

#: The key every authority page, contract list and map looks an authority up by.
#:
#: Authority had indexes on name_clean, nuts and (national_id, country) — and
#: none on authority_id, the one property the API actually addresses it by. The
#: planner, with nothing to seek on, starts these queries from the other end.
#: /geo/entity/{authority}/aggregate scanned every NUTSRegion, walked down to
#: 1.7M located Companies and their 65k contracts, and only then filtered for
#: the one authority: 8.1M db hits to aggregate 66 contracts. On the shared
#: graph (1G of page cache over NFS at the time) that was 26-31 s, which is the
#: PROC-MAP-COLORIZE e2e failure; prod's 16G cache hid the same 8M hits behind
#: 0.08 s. Forced to start from the authority it was 135k hits, nearly all of
#: them the label scan this index removes.
AUTHORITY_ID = "authority_authority_id"

_STATEMENTS = (
    f"CREATE FULLTEXT INDEX {COMPANY_NAME_FULLTEXT} IF NOT EXISTS "
    "FOR (c:Company) ON EACH [c.name]",
    f"CREATE INDEX {CONTRACT_PUBLICATION_DATE} IF NOT EXISTS "
    "FOR (c:Contract) ON (c.publication_date)",
    f"CREATE INDEX {AUTHORITY_NUTS} IF NOT EXISTS "
    "FOR (a:Authority) ON (a.nuts)",
    f"CREATE INDEX {LOBBYIST_DISCLOSURE_ID} IF NOT EXISTS "
    "FOR (l:Lobbyist) ON (l.disclosure_id)",
    f"CREATE INDEX {AUTHORITY_ID} IF NOT EXISTS "
    "FOR (a:Authority) ON (a.authority_id)",
)


def ensure_indexes(neo4j) -> list[str]:
    """Create any missing index. Returns the statements that ran.

    Each statement stands on its own. They used to share one ``try``, so a
    single refusal — a name clash, a permission, a transient error — silently
    skipped every index declared after it, and the list only ever grows at the
    end.
    """
    ran = []
    try:
        session_cm = neo4j.session()
    except Exception as exc:  # pylint: disable=broad-except
        # Logged, not raised: see the module docstring.
        logger.warning("could not ensure graph indexes: {}", exc)
        return ran
    with session_cm as session:
        for statement in _STATEMENTS:
            try:
                session.run(statement)
                ran.append(statement)
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("could not ensure graph index ({}): {}", statement, exc)
    return ran
