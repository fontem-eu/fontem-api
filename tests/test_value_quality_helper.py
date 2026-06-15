"""The shared confidence-gated value-sum fragment."""
from src.data.graph._value_quality import trusted_value_sum


def test_default_binding_and_no_cast():
    frag = trusted_value_sum()
    assert "ct.value_low_confidence" in frag
    assert "coalesce(ct.value_eur, 0)" in frag
    assert frag.startswith("sum(CASE WHEN")
    # flagged rows contribute 0
    assert "THEN 0 ELSE" in frag


def test_custom_binding():
    frag = trusted_value_sum("contract")
    assert "contract.value_low_confidence" in frag
    assert "contract.value_eur" in frag
    # the default "ct" binding is not used
    assert "coalesce(ct.value_eur" not in frag


def test_cast_wraps_in_tofloat():
    frag = trusted_value_sum("ct", cast=True)
    assert "toFloat(ct.value_eur)" in frag
    assert "coalesce(ct.value_eur, 0)" not in frag
