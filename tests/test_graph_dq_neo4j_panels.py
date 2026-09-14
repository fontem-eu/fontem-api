"""The sanctions, filings, overview and completeness panels read Neo4j."""
from src.data.graph.graph_data_quality import GraphDataQualitySource


class _Result:
    def __init__(self, single=None, data=None):
        self._single = single
        self._data = data or []

    def single(self):
        return self._single

    def data(self):
        return self._data


class _Session:
    def __init__(self, answer, calls):
        self._answer = answer
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, **params):
        self._calls.append((query, params))
        return self._answer(query)


class _Neo4j:
    def __init__(self, answer):
        self.calls = []
        self._answer = answer

    def session(self):
        return _Session(self._answer, self.calls)


def _filings_answer(query):
    if "f.year AS yr" in query:
        return _Result(data=[{"yr": 2023, "n": 3}, {"yr": 2024, "n": 1}])
    if "count(f.revenue)" in query:
        return _Result(single={"revenue": 2, "net_income": 4, "total_assets": 3,
                               "equity": 4, "operating_cashflow": 0})
    if "RETURN count(f) AS n" in query:
        return _Result(single={"n": 4})
    return None


def test_sanctions_stats_split_persons_from_organisations():
    def answer(query):
        if "subject_type = 'person'" in query:
            return _Result(single={"total": 10, "persons": 7})
        if "sanction_regime AS regime" in query:
            return _Result(data=[{"regime": "UKR", "n": 6}])
        if "[:SANCTIONED]" in query:
            return _Result(single={"n": 2})
        raise AssertionError(query)
    src = GraphDataQualitySource(neo4j_client=_Neo4j(answer), virtuoso_client=None)
    assert src.get_sanctions_stats() == {
        "total": 10, "persons": 7, "entities": 3,
        "matched_to_companies": 2, "top_regimes": [{"regime": "UKR", "n": 6}],
    }


def test_edgar_stats_read_financial_years_of_that_source():
    def answer(query):
        if "LISTED_AS" in query:
            return _Result(single={"n": 5})
        found = _filings_answer(query)
        if found is None:
            raise AssertionError(query)
        return found
    neo = _Neo4j(answer)
    out = GraphDataQualitySource(neo4j_client=neo, virtuoso_client=None).get_edgar_stats()
    assert out["companies"] == 5
    assert out["financial_years"] == 4
    assert out["by_year"] == [{"date": "2023-01-01", "value": 3},
                              {"date": "2024-01-01", "value": 1}]
    assert out["field_coverage"] == {"revenue": 50.0, "net_income": 100.0,
                                     "total_assets": 75.0, "equity": 100.0,
                                     "operating_cashflow": 0.0}
    sources = {params.get("source") for query, params in neo.calls
               if "FinancialYear" in query}
    assert sources == {"edgar"}


def test_esef_company_counts_use_alpha3_eu_members():
    def answer(query):
        if "c.country AS country" in query:
            return _Result(data=[{"country": "SWE", "count": 2}])
        if "RETURN count(c) AS n" in query:
            return _Result(single={"n": 3})
        found = _filings_answer(query)
        if found is None:
            raise AssertionError(query)
        return found
    neo = _Neo4j(answer)
    out = GraphDataQualitySource(neo4j_client=neo, virtuoso_client=None).get_esef_stats()
    assert out["companies"] == 3
    assert out["by_country"] == [{"country": "SWE", "count": 2}]
    assert out["financial_years"] == 4
    eu = next(params["eu"] for query, params in neo.calls if "eu" in params)
    assert len(eu) == 27 and "SWE" in eu
    # Company countries are alpha-3; neither alpha-2 codes nor the UK belong.
    assert "SE" not in eu and "GBR" not in eu


def test_graph_stats_count_sanctions_and_financial_years_in_neo4j():
    def answer(query):
        if query.startswith("MATCH (n:"):
            label = query[len("MATCH (n:"):query.index(")")]
            return _Result(single={"n": len(label)})
        return _Result(single={"n": 99})
    stats = GraphDataQualitySource(neo4j_client=_Neo4j(answer),
                                   virtuoso_client=None).get_graph_stats()
    assert stats["nodes"]["SanctionedEntity"] == len("SanctionedEntity")
    assert stats["nodes"]["FinancialYear"] == len("FinancialYear")
    assert stats["relationships"] == 99


def test_field_completeness_sanctions_come_from_neo4j():
    def answer(query):
        if query.startswith("MATCH (s:SanctionedEntity)"):
            if "s.name IS NOT NULL" in query:
                return _Result(single={"n": 5})
            return _Result(single={"n": 10})
        return _Result(single={"n": 0}, data=[])
    out = GraphDataQualitySource(neo4j_client=_Neo4j(answer),
                                 virtuoso_client=None).get_field_completeness()
    assert out["sanctions"] == {"total": 10, "name_pct": 50.0, "regime_pct": 100.0}
