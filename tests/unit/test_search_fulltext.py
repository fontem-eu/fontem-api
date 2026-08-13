"""Company search: candidates come from an index, not from a scan.

/search took ~8s for "Siemens" or "Mészáros" and ~0.6s for "Apple". The
difference was not the term — it was whether the first branch filled the
result limit. When it did not, a second query ran that started at
(:Contract), walked 2.4M of them to reach companies, and filtered with
`toLower(name) CONTAINS`, which no index can serve. A FULLTEXT index on
Company.name already existed and nothing used it. Measured on production
across ten terms: 28.3s -> 0.3s, identical top-5.

Everything below is a regression. Each case is one that was observed
failing against the real index, not one imagined at a desk:

  * "AT&T" returned zero rows where CONTAINS found two — the analyzer
    stores it as the tokens "at" and "t", so escaping the ampersand
    produced a single token that could never match.
  * `") OR (1=1` raised ProcedureCallFailed — OR is a reserved word to the
    query parser in uppercase, and one in a non-final position is a hard
    parse failure rather than an empty result.
  * The index existed in production and in no other environment, so the
    first deploy that used it returned nothing in testing while every
    health check stayed green.
"""
import pathlib

import pytest

from src.api import graph_schema
from src.api.routers import contracts
from src.api.routers.contracts import _fulltext_query


# ── the shape of the query ─────────────────────────────────────
def test_a_single_term_gets_a_prefix_wildcard():
    """So "siemen" finds "Siemens" — users and models both under-type."""
    assert _fulltext_query("Siemens") == "+siemens*"


def test_every_token_is_required():
    """Lucene's default OR would make "deutsche bahn" match every company
    containing either word, which is most of Germany."""
    assert _fulltext_query("siemens mob") == "+siemens +mob*"
    assert _fulltext_query("Deutsche Bahn AG") == "+deutsche +bahn +ag*"


def test_only_the_last_token_is_a_prefix():
    """The earlier tokens are complete words the name must contain; only
    what the user is still typing is open-ended."""
    q = _fulltext_query("deutsche bahn")
    assert q.count("*") == 1
    assert q.endswith("bahn*")


def test_whitespace_around_and_between_tokens_is_absorbed():
    assert _fulltext_query("  Deutsche   Bahn  ") == "+deutsche +bahn*"


# ── regression: AT&T returned nothing ──────────────────────────
@pytest.mark.parametrize("term, expected", [
    ("AT&T", "+at +t*"),
    ("Marks & Spencer", "+marks +spencer*"),
    ("Coca-Cola", "+coca +cola*"),
    ("Foo (Bar)", "+foo +bar*"),
    ("[Bracketed]", "+bracketed*"),
    ("{Braced}", "+braced*"),
    ('"Quoted"', "+quoted*"),
    ("col:on", "+col +on*"),
    ("sla/sh", "+sla +sh*"),
    ("back\\slash", "+back +slash*"),
    ("star*", "+star*"),
    ("quest?ion", "+quest +ion*"),
    ("til~de", "+til +de*"),
    ("car^et", "+car +et*"),
    ("ex!clam", "+ex +clam*"),
    ("pipe|d", "+pipe +d*"),
])
def test_metacharacters_split_rather_than_escape(term, expected):
    """The analyzer treats these as boundaries, so the query must too.

    Escaping them produces tokens that exist nowhere in the index: "AT&T"
    went to zero rows where CONTAINS found two.
    """
    assert _fulltext_query(term) == expected


# ── regression: ") OR (1=1 raised ProcedureCallFailed ──────────
@pytest.mark.parametrize("term", [
    '") OR (1=1', "AND Co", "OR Ltd", "NOT Ltd", "TO Group",
    "AND", "OR", "NOT", "Smith AND Sons",
])
def test_reserved_words_are_lowercased_so_the_parser_never_sees_them(term):
    """AND, OR and NOT are operators to the query parser in uppercase only.

    One in a non-final position is a parse failure, not an empty result —
    the whole call raises and the search 500s. The index analyzer
    lowercases what it stores, so this costs no matches.
    """
    q = _fulltext_query(term)
    for reserved in ("AND", "OR", "NOT", "TO"):
        assert f"+{reserved} " not in q + " "
        assert f"+{reserved}*" not in q
    assert q == q.lower()


def test_lowercasing_does_not_change_which_names_match():
    """Same tokens either way; the index is case-folded. Measured against
    production: +Siemens* and +siemens* both match 722 companies."""
    assert _fulltext_query("SIEMENS") == _fulltext_query("siemens")
    assert _fulltext_query("Deutsche Bahn") == _fulltext_query("DEUTSCHE BAHN")


# ── input that yields nothing to search for ────────────────────
@pytest.mark.parametrize("term", ["***", "&&&", "()", "   ", "", "+-|!", "\\"])
def test_punctuation_only_input_yields_no_query(term):
    """Callers skip the branch rather than hand Lucene an empty string,
    which is a syntax error rather than an empty result."""
    assert _fulltext_query(term) == ""


def test_both_branches_skip_when_there_is_nothing_to_search():
    """A guard that exists only in one branch is a 500 waiting for the
    query that reaches the other."""
    src = pathlib.Path(contracts.__file__).read_text("utf-8")
    body = src[src.index('@router.get("/search")'):]
    assert body.count("and _fulltext_query(q):") == 2


# ── names that must keep working ───────────────────────────────
@pytest.mark.parametrize("term, expected", [
    ("Mészáros", "+mészáros*"),
    ("Ørsted", "+ørsted*"),
    ("L'Oréal", "+l'oréal*"),
    ("E.ON", "+e.on*"),
    ("S.A.R.L.", "+s.a.r.l.*"),
    ("3M", "+3m*"),
    ("Ünïcödé Ltd", "+ünïcödé +ltd*"),
])
def test_real_company_names_survive_intact(term, expected):
    """Accents, apostrophes, periods and digits are part of names, not
    syntax. All of these were verified against the live index."""
    assert _fulltext_query(term) == expected


# ── regression: the slow shape, and the undeclared index ───────
def test_neither_branch_scans_contracts_to_find_companies():
    """The actual fix. Starting at (:Contract) walked 2.4M rows to reach
    the companies; the rest is escaping."""
    src = pathlib.Path(contracts.__file__).read_text("utf-8")
    body = src[src.index('@router.get("/search")'):]
    assert "MATCH (ct:Contract)-[:AWARDED_TO]->(c:Company) " not in body
    assert body.count("db.index.fulltext.queryNodes('company_name_ft'") == 2


def test_the_index_the_search_depends_on_is_declared_in_code():
    """It existed in production and nowhere else — created by hand and
    guaranteed by nothing, so the first deploy that used it returned zero
    results in testing while every health check stayed green."""
    used = pathlib.Path(contracts.__file__).read_text("utf-8")
    assert f"'{graph_schema.COMPANY_NAME_FULLTEXT}'" in used


def test_ensuring_indexes_never_takes_the_api_down():
    """An API that refuses to start because it could not create an index is
    worse than one that starts and logs."""
    class Broken:
        def session(self):
            raise RuntimeError("neo4j unreachable")

    assert graph_schema.ensure_indexes(Broken()) == []


def test_the_index_statement_is_idempotent():
    """It runs on every start, in environments that already have it."""
    assert all("IF NOT EXISTS" in s for s in graph_schema._STATEMENTS)  # noqa: SLF001


def test_the_feed_query_indexes_are_declared_too():
    """A feed asks "what was published since my last visit, in my regions".
    Both halves were unindexed: Contract carried indexes on its identifiers
    only, and Authority none on nuts. An EU-wide seven-day window measured 51
    seconds on prod before these existed — a full scan of 1.65M nodes, past
    the proxy's statement timeout, so the query would simply fail."""
    statements = " ".join(graph_schema._STATEMENTS)  # noqa: SLF001
    assert "(c:Contract) ON (c.publication_date)" in statements
    assert "(a:Authority) ON (a.nuts)" in statements
