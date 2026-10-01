"""``--notices``: re-ingest named notices only (gitops#480)."""
from unittest.mock import MagicMock, patch

from src.etl.load_ted_contracts import load_notices, read_publication_numbers

from .test_load_ted_contracts import _mock_driver_and_session, _mock_log


def test_numbers_come_from_a_file_or_a_list(tmp_path):
    listing = tmp_path / "notices.txt"
    listing.write_text("148462-2026\n\n# placeholder counts\n556267-2024  # 999\n148462-2026\n")
    assert read_publication_numbers(str(listing)) == ["148462-2026", "556267-2024"]
    assert read_publication_numbers("148462-2026, 154038-2026") == ["148462-2026", "154038-2026"]


def test_each_notice_is_rescored_from_the_raw_store_or_ted():
    driver, _ = _mock_driver_and_session()
    log, _ = _mock_log()
    store = MagicMock()
    store.get.side_effect = lambda number: b"<held/>" if number == "148462-2026" else None
    seen = []
    with patch("src.etl.load_ted_contracts.TedRawStore.from_env", return_value=store), \
         patch("src.etl.load_ted_contracts.ted_search.fetch_xml",
               return_value=b"<fetched/>") as fetch, \
         patch("src.etl.load_ted_contracts.parse_notice_xml", side_effect=lambda xml: xml), \
         patch("src.etl.load_ted_contracts.TedMatcher"), \
         patch("src.etl.load_ted_contracts.ingest_notice",
               side_effect=lambda notice, _s, _l, ctx: seen.append((notice, ctx.rescore))):
        totals = load_notices(driver, log, ["148462-2026", "154038-2026"])
    assert seen == [(b"<held/>", True), (b"<fetched/>", True)]
    assert fetch.call_args.args[0] == "https://ted.europa.eu/en/notice/154038-2026/xml"
    assert totals == {"emitted": 2, "errors": 0}


def test_one_bad_notice_does_not_stop_the_rest():
    driver, _ = _mock_driver_and_session()
    log, _ = _mock_log()
    with patch("src.etl.load_ted_contracts.TedRawStore.from_env", return_value=None), \
         patch("src.etl.load_ted_contracts.ted_search.fetch_xml",
               side_effect=[RuntimeError("TED down"), b"<ok/>"]), \
         patch("src.etl.load_ted_contracts.parse_notice_xml", side_effect=lambda xml: xml), \
         patch("src.etl.load_ted_contracts.TedMatcher"), \
         patch("src.etl.load_ted_contracts.ingest_notice"):
        totals = load_notices(driver, log, ["1-2026", "2-2026"])
    assert totals == {"emitted": 1, "errors": 1}
