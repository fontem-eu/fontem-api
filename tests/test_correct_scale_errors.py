"""History-correction script: corrected payloads carry rescaled values,
the marker, and re-scored confidence; sane payloads pass through."""
from src.etl.correct_scale_errors import _corrected_payload

SCHOOL = {
    "ted_notice_id": "201a2b4a", "country": "PRT",
    "title": "ESCOLA SECUNDARIA BERNARDINO MACHADO",
    "value_eur": 9281922790.0, "value_original": 9281922790.0,
    "value_payable_eur": 9281922790.0,
    "estimated_value_eur": 9289549170.0,
    "value_quality_flag": "ok", "value_low_confidence": False,
    "value_confidence": 0.61,
}


def test_school_payload_rescaled_and_rescored():
    out = _corrected_payload(SCHOOL)
    assert out is not None
    assert out["value_eur"] == 9281922.79
    assert out["estimated_value_eur"] == 9289549.17
    assert out["value_scale_corrected"] == "country_prior"
    # re-scored on sane magnitudes: high confidence, no low-conf mark
    assert out["value_quality_flag"] == "ok"
    assert out["value_low_confidence"] is False
    assert out["value_confidence"] > 0.9


def test_sane_payload_untouched():
    sane = dict(SCHOOL, value_eur=9281922.79, value_original=9281922.79,
                value_payable_eur=9281922.79,
                estimated_value_eur=9289549.17)
    assert _corrected_payload(sane) is None


def test_type_a_ratio_case():
    p = {
        "ted_notice_id": "35ecc2b8", "country": "PRT",
        "value_eur": 7274615930.0, "value_payable_eur": 7274615930.0,
        "estimated_value_eur": 7317073.17,
    }
    out = _corrected_payload(p)
    assert out is not None
    assert out["value_eur"] == 7274615.93
    assert out["value_scale_corrected"] == "ratio"
