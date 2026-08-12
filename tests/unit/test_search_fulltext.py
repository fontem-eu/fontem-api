"""Company search: candidates come from the index, not from a scan.

/search took ~8s for terms like "Siemens" or "Mészáros" and ~0.6s for
"Apple". The difference was not the term — it was whether the first branch
filled the result limit. When it did not, a second query ran that started
at (:Contract), walked 2.4M of them to reach companies, and filtered with
`toLower(name) CONTAINS`, which no index can serve.

A FULLTEXT index on Company.name already existed and nothing used it.
Measured on production across ten terms: 28.3s -> 0.3s, identical top-5.
"""
import pathlib

from src.api.routers.contracts import _fulltext_query


def test_a_single_term_gets_a_prefix_wildcard():
    """So "siemen" finds "Siemens" — users and models both under-type."""
    assert _fulltext_query("Siemens") == "+Siemens*"


def test_every_token_is_required():
    """Default Lucene OR would make "deutsche bahn" match every company
    containing either word, which is most of Germany."""
    assert _fulltext_query("siemens mob") == "+siemens +mob*"
    assert _fulltext_query("Deutsche Bahn") == "+Deutsche +Bahn*"


def test_metacharacters_split_rather_than_escape():
    """The regression this function was rewritten for.

    The analyzer stores "AT&T" as the tokens "at" and "t". Escaping the
    ampersand produces a single token that matches nothing — the query
    returned zero rows where CONTAINS found two. Splitting on it matches
    what is actually indexed.
    """
    assert _fulltext_query("AT&T") == "+AT +T*"
    assert _fulltext_query("Foo (Bar)") == "+Foo +Bar*"


def test_punctuation_only_input_yields_no_query():
    """Callers skip the branch instead of handing Lucene an empty string,
    which is a syntax error rather than an empty result."""
    assert _fulltext_query("***") == ""
    assert _fulltext_query("   ") == ""
    assert _fulltext_query("") == ""


def test_whitespace_around_and_between_tokens_is_absorbed():
    assert _fulltext_query("  Deutsche   Bahn  ") == "+Deutsche +Bahn*"


def test_the_slow_shape_is_gone_from_both_branches():
    """Neither branch may start from (:Contract) again, and both must go
    through the index. This is the actual fix; the rest is escaping."""
    src = pathlib.Path("src/api/routers/contracts.py").read_text("utf-8")
    body = src[src.index('@router.get("/search")'):]
    assert "MATCH (ct:Contract)-[:AWARDED_TO]->(c:Company) " not in body, \
        "a branch still scans contracts to find companies"
    assert body.count("db.index.fulltext.queryNodes('company_name_ft'") == 2, \
        "both the procurement and cohesion branches should use the index"
