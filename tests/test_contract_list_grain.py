"""The contract lists are one row per CONTRACT.

They used to be one row per (contract x winner) on the buyer's page and
one per (contract x buyer) on the supplier's, because the winner's and
buyer's columns rode into a RETURN DISTINCT. Camara Municipal de Pombal
showed its three-insurer framework three times over. Measured on prod:
1,026,029 contracts have more than one winner (5,358,640 rows, one
contract 802 times) and 22,156 carry more than one :AWARDED edge.
"""
# pylint: disable=protected-access
from unittest.mock import MagicMock

from src.data.graph.graph_contract_source import GraphContractSource

from .test_contract_supplier_withheld import (
    _authority_row, _authority_source, _run_result,
)


def _company_source(rows):
    """get_company_contracts: the company, the page of rows, the totals."""
    src = GraphContractSource(MagicMock())
    session = MagicMock()
    session.run.side_effect = [
        _run_result(single={"name": "Alfa S.p.A.", "country": "ITA"}),
        _run_result(data=rows),
        _run_result(single={"total": 0, "cnt": len(rows)}),
    ]
    src._neo4j.session.return_value.__enter__ = MagicMock(return_value=session)
    src._neo4j.session.return_value.__exit__ = MagicMock(return_value=False)
    return src, session


def _queries(session):
    return [c.args[0] for c in session.run.call_args_list]


class TestAuthorityListGrain:
    def test_the_winners_are_aggregated_not_fanned_out(self):
        src, session = _authority_source([_authority_row()])
        src.get_authority_contracts("auth-1")
        rows_query = _queries(session)[1]
        assert "collect(DISTINCT c) AS winners" in rows_query
        assert "head(winners) AS c" in rows_query
        # the winner's columns must not ride into a DISTINCT again
        assert "RETURN DISTINCT" not in rows_query

    def test_the_row_says_how_many_suppliers_won(self):
        src, _ = _authority_source([_authority_row(contractor_count=3)])
        out = src.get_authority_contracts("auth-1")
        assert out["contracts"][0]["contractor_count"] == 3

    def test_a_missing_count_is_zero_not_none(self):
        """Rows written before the field existed must not render 'null
        suppliers'."""
        row = _authority_row()
        row.pop("contractor_count", None)
        src, _ = _authority_source([row])
        assert src.get_authority_contracts("auth-1")["contracts"][0][
            "contractor_count"] == 0

    def test_the_totals_still_count_each_contract_once(self):
        """The totals always took DISTINCT ct; the list now agrees with
        them instead of showing more rows than the count."""
        src, session = _authority_source([_authority_row()])
        src.get_authority_contracts("auth-1")
        totals_query = _queries(session)[2]
        assert "WITH DISTINCT ct" in totals_query


class TestCompanyListGrain:
    def test_the_buyers_are_aggregated_not_fanned_out(self):
        src, session = _company_source([])
        src.get_company_contracts("gmr-1")
        rows_query = _queries(session)[1]
        assert "collect(DISTINCT a) AS buyers" in rows_query
        assert "head(buyers) AS a" in rows_query
        assert "RETURN DISTINCT" not in rows_query

    def test_the_row_says_how_many_buyers_procured_jointly(self):
        src, _ = _company_source([{
            "notice_id": "n1", "publication_number": None, "title": "t",
            "value_eur": 1.0, "award_date": None, "cpv": None,
            "authority": "Ministry", "authority_country": "HUN",
            "authority_id": "auth-1", "procedure_type": "open",
            "ted_url": None, "buyer_count": 2,
        }])
        out = src.get_company_contracts("gmr-1")
        assert out["contracts"][0]["buyer_count"] == 2
