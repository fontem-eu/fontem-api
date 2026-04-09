"""Test that DataQualitySource enforces abstract methods — kills @abstractmethod removal mutants."""
# pylint: disable=missing-function-docstring,missing-class-docstring,abstract-class-instantiated
import pytest
from src.analysis.data_quality_source import DataQualitySource


class TestDataQualitySourceAbstract:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError, match="abstract method"):
            DataQualitySource()  # pylint: disable=abstract-class-instantiated

    def test_requires_get_graph_stats(self):
        class Partial(DataQualitySource):  # pylint: disable=abstract-class-instantiated
            def get_matching_stats(self):
                return {}
            def get_data_freshness(self):
                return {}
            def get_coverage_stats(self):
                return {}
        with pytest.raises(TypeError):
            Partial()

    def test_requires_get_matching_stats(self):
        class Partial(DataQualitySource):  # pylint: disable=abstract-class-instantiated
            def get_graph_stats(self):
                return {}
            def get_data_freshness(self):
                return {}
            def get_coverage_stats(self):
                return {}
        with pytest.raises(TypeError):
            Partial()

    def test_requires_get_data_freshness(self):
        class Partial(DataQualitySource):  # pylint: disable=abstract-class-instantiated
            def get_graph_stats(self):
                return {}
            def get_matching_stats(self):
                return {}
            def get_coverage_stats(self):
                return {}
        with pytest.raises(TypeError):
            Partial()

    def test_requires_get_coverage_stats(self):
        class Partial(DataQualitySource):  # pylint: disable=abstract-class-instantiated
            def get_graph_stats(self):
                return {}
            def get_matching_stats(self):
                return {}
            def get_data_freshness(self):
                return {}
        with pytest.raises(TypeError):
            Partial()

    def test_concrete_subclass_can_instantiate(self):
        class Complete(DataQualitySource):
            def get_graph_stats(self):
                return {}
            def get_matching_stats(self):
                return {}
            def get_data_freshness(self):
                return {}
            def get_coverage_stats(self):
                return {}
        instance = Complete()
        assert not instance.get_graph_stats()
