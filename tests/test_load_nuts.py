"""Tests for the NUTS reference hierarchy loader."""
from unittest.mock import MagicMock, patch

import pytest
import httpx

from src.etl import load_nuts
from src.etl.load_nuts import (
    _parent_code,
    load_into_neo4j,
    main,
    parse_nuts_csv,
)


# ── Parsing ────────────────────────────────────────────────────────


SAMPLE_CSV = """NUTS_ID,NUTS_NAME,LEVL_CODE,CNTR_CODE
FR,France,0,FR
FR1,Île-de-France,1,FR
FR10,Île-de-France,2,FR
FR101,Paris,3,FR
DE,Germany,0,DE
DE1,Baden-Württemberg,1,DE
DE11,Stuttgart,2,DE
DE111,Stuttgart Stadtkreis,3,DE
"""


def test_parse_yields_all_levels():
    """Parser must emit rows for NUTS 0, 1, 2, and 3."""
    rows = list(parse_nuts_csv(SAMPLE_CSV))
    levels = sorted({r["level"] for r in rows})
    assert levels == [0, 1, 2, 3]
    assert len(rows) == 8


def test_parse_level_is_derived_from_code_length():
    """Level = len(code) - 2."""
    rows = {r["code"]: r for r in parse_nuts_csv(SAMPLE_CSV)}
    assert rows["FR"]["level"] == 0
    assert rows["FR1"]["level"] == 1
    assert rows["FR10"]["level"] == 2
    assert rows["FR101"]["level"] == 3


def test_parse_parent_code_traces_hierarchy():
    """Each row's parent is the code with its last char removed (or None for L0)."""
    rows = {r["code"]: r for r in parse_nuts_csv(SAMPLE_CSV)}
    assert rows["FR"]["parent"] is None
    assert rows["FR1"]["parent"] == "FR"
    assert rows["FR10"]["parent"] == "FR1"
    assert rows["FR101"]["parent"] == "FR10"


def test_parse_rejects_rows_with_invalid_code_length():
    """Codes outside [2, 5] characters are skipped (e.g., empty or malformed)."""
    bad_csv = """NUTS_ID,NUTS_NAME
,empty
X,too short
ABCDEF,too long
FR,France
"""
    rows = list(parse_nuts_csv(bad_csv))
    assert [r["code"] for r in rows] == ["FR"]


def test_parse_accepts_bom_and_alt_column_names():
    """Handles BOM and CODE/LABEL column name variants."""
    csv_with_bom = "\ufeffCODE,LABEL\nFR,France\n"
    rows = list(parse_nuts_csv(csv_with_bom))
    assert rows == [
        {
            "code": "FR",
            "name": "France",
            "level": 0,
            "parent": None,
            "country_alpha3": "FRA",
        }
    ]


def test_parse_falls_back_to_code_when_name_missing():
    """If name column is absent or empty, use the code as the name."""
    csv_no_name = "NUTS_ID\nFR\n"
    rows = list(parse_nuts_csv(csv_no_name))
    assert rows[0]["name"] == "FR"


def test_parse_raises_when_code_column_missing():
    """Must raise ValueError if neither NUTS_ID nor CODE is present."""
    with pytest.raises(ValueError):
        list(parse_nuts_csv("FOO,BAR\na,b\n"))


# ── Parent-code derivation ─────────────────────────────────────────


def test_parent_code_for_level_0_is_none():
    """NUTS 0 codes have no parent."""
    assert _parent_code("FR") is None


def test_parent_code_strips_last_char():
    """Parent of NUTS N is the code with its last character removed."""
    assert _parent_code("FR101") == "FR10"
    assert _parent_code("FR10") == "FR1"
    assert _parent_code("FR1") == "FR"


# ── Neo4j loading ──────────────────────────────────────────────────


def _mock_driver():
    driver = MagicMock()
    session = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return driver, session


def test_load_creates_constraint_and_merges_regions():
    """Loader runs the constraint first, then MERGEs regions and PART_OF."""
    driver, session = _mock_driver()
    regions = list(parse_nuts_csv(SAMPLE_CSV))

    summary = load_into_neo4j(driver, regions)

    assert summary["total"] == 8
    # Constraint must be the first call
    first_call = session.run.call_args_list[0].args[0]
    assert "CONSTRAINT" in first_call
    # At least one MERGE (NUTSRegion) and one MERGE (PART_OF)
    all_cypher = " ".join(c.args[0] for c in session.run.call_args_list)
    assert "NUTSRegion" in all_cypher
    assert "PART_OF" in all_cypher


def test_load_reports_counts_per_level():
    """Summary must break down counts by level (0-3)."""
    driver, _ = _mock_driver()
    regions = list(parse_nuts_csv(SAMPLE_CSV))
    summary = load_into_neo4j(driver, regions)
    assert summary["by_level"] == {0: 2, 1: 2, 2: 2, 3: 2}


def test_load_does_not_link_entities():
    """Entity→region linking lives in a separate ETL; this one stays focused."""
    driver, session = _mock_driver()
    regions = list(parse_nuts_csv(SAMPLE_CSV))
    load_into_neo4j(driver, regions)
    all_cypher = " ".join(c.args[0] for c in session.run.call_args_list)
    assert "Company" not in all_cypher
    assert "Authority" not in all_cypher
    assert "LOCATED_IN" not in all_cypher


# ── CLI behaviour ──────────────────────────────────────────────────


def test_main_aborts_when_download_fails(monkeypatch):
    """CLI must exit non-zero when the CSV download fails — no silent fallback."""
    def fake_download():
        raise httpx.HTTPError("network down")

    monkeypatch.setattr(load_nuts, "download_nuts_csv", fake_download)
    with pytest.raises(httpx.HTTPError):
        main(argv=["--neo4j-uri", "bolt://fake:7687"])


def test_main_aborts_when_parsed_zero_regions(tmp_path):
    """CLI must exit 1 if the CSV parsed to zero usable rows."""
    empty_file = tmp_path / "empty.csv"
    empty_file.write_text("NUTS_ID,NUTS_NAME\n")  # header only, no rows

    with pytest.raises(SystemExit) as exc:
        main(argv=["--file", str(empty_file)])
    assert exc.value.code == 1


def test_main_loads_from_file(tmp_path):
    """CLI reads a local CSV file when --file is given."""
    csv_file = tmp_path / "nuts.csv"
    csv_file.write_text(SAMPLE_CSV)

    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)

    with patch("src.etl.load_nuts.GraphDatabase.driver", return_value=mock_driver):
        main(argv=["--file", str(csv_file), "--neo4j-uri", "bolt://fake:7687"])

    # Constraint + MERGE statements executed
    cypher_used = " ".join(c.args[0] for c in mock_session.run.call_args_list)
    assert "NUTSRegion" in cypher_used
