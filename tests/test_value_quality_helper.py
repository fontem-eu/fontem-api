"""The shared contract value-aggregation fragments: confidence gate +
modification collapse (current_value over canonical nodes only)."""
from src.data.graph._value_quality import (
    canonical_count,
    canonical_predicate,
    trusted_value_sum,
)


def test_default_binding_and_no_cast():
    frag = trusted_value_sum()
    assert "ct.value_low_confidence" in frag
    # sums the collapsed current_value, falling back to the raw value_eur
    assert "coalesce(ct.current_value, ct.value_eur)" in frag
    assert frag.startswith("sum(CASE WHEN")
    # only canonical, non-flagged rows contribute their value
    assert "ct.is_current" in frag
    assert "ELSE 0 END)" in frag


def test_custom_binding():
    frag = trusted_value_sum("contract")
    assert "contract.value_low_confidence" in frag
    assert "contract.current_value" in frag
    # the default "ct" binding is not used
    assert "coalesce(ct.value_eur" not in frag


def test_cast_wraps_in_tofloat():
    frag = trusted_value_sum("ct", cast=True)
    assert "toFloat(coalesce(ct.current_value, ct.value_eur))" in frag
    assert "coalesce(coalesce(ct.current_value, ct.value_eur), 0)" not in frag


def test_canonical_predicate_excludes_raw_modifications():
    pred = canonical_predicate("ct")
    # stamped canonical wins; else anything that is not a raw can-modif notice
    assert "ct.is_current" in pred
    assert "ct.notice_type <> 'can-modif'" in pred


def test_canonical_count_is_a_sum_over_canonical_nodes():
    frag = canonical_count("ct")
    assert frag.startswith("sum(CASE WHEN")
    assert canonical_predicate("ct") in frag
    assert "THEN 1 ELSE 0 END)" in frag
