"""The cross-store check must address the IRI the sink actually wrote.

consistency.contract_neo4j_virtuoso builds a SPARQL subject from a Neo4j
key. TED notice ids look like "2011/S 1-000181", and a raw space inside
<...> makes the query unparseable — Virtuoso answers 400 and the
assertion reports an HTTP error instead of a result. 1,598,916 Contract
keys contain such a character, so a 12-row random sample hit one on
essentially every run, and cross-store contract consistency has been
unmeasured rather than passing.

Encoding also has to match the virtuoso sink exactly. Querying a
differently-encoded IRI returns no triples, which the caller reads as
"every field mismatches" — a red result that looks like real drift but
is an addressing bug.
"""
from src.data_quality.assertions.consistency import SPECS, _encode_iri_tail


def test_space_is_encoded():
    """The exact shape that produced the 400."""
    assert _encode_iri_tail("2011/S 1-000181") == "2011/S%201-000181"


def test_encoding_matches_the_virtuoso_sink_safe_set():
    """Pinned against virtuoso_sink.triples._percent_encode_iri.

    The sink keeps sub-delims and path separators unescaped; if these
    diverge, every lookup silently addresses a subject that does not
    exist.
    """
    assert _encode_iri_tail("a/b") == "a/b"          # path sep kept
    assert _encode_iri_tail("a:b") == "a:b"          # scheme sep kept
    assert _encode_iri_tail("a(b)") == "a(b)"        # sub-delims kept
    assert _encode_iri_tail("a%20b") == "a%20b"      # already-encoded not doubled


def test_non_ascii_is_encoded():
    """Virtuoso's parser does not fully implement RFC 3987."""
    assert _encode_iri_tail("Ünïcode") == "%C3%9Cn%C3%AFcode"


def test_angle_brackets_cannot_escape_the_iri():
    """A key containing '>' would otherwise terminate the IRI early and
    let the rest of the key be parsed as SPARQL."""
    out = _encode_iri_tail("a>b <c")
    assert ">" not in out and "<" not in out and " " not in out


def test_accepts_non_string_keys():
    """Neo4j returns ints for some keys; the old code str()'d them."""
    assert _encode_iri_tail(12345) == "12345"


def test_contract_addresses_both_grains():
    """A Contract key can address two subjects and both are live.

    Legacy notice-id-keyed events put every fact on
    .../Contract/<ted_notice_id>. Notice-grain events split them, keeping
    aggregates OFF the Contract subject and writing them to
    .../Notice/<ted_notice_id>.

    Measured in prod: 2011/S 1-000181 has 9 triples on Contract and 0 on
    Notice; 2020/S 090-214531 has 2 on Contract (rdf:type only) and 4 on
    Notice. Querying only the Contract form finds nothing for every
    notice-grain record, and the caller reads "no triples" as "every
    field disagrees" — the 12-of-12 false positive.
    """
    forms = SPECS["Contract"]["iri_forms"]
    assert any(f.endswith("/Notice/") for f in forms)
    assert any(f.endswith("/Contract/") for f in forms)


def test_single_form_specs_still_work():
    """Company has one subject form and must not need iri_forms."""
    spec = SPECS["Company"]
    forms = spec.get("iri_forms") or (spec["iri"],)
    assert forms == (spec["iri"],)
