"""Tests for GMRSettings default values — kills mutmut mutations on threshold defaults."""
# pylint: disable=missing-function-docstring,missing-class-docstring
from src.analysis.gmr_data_source import GMRSettings


class TestGMRSettingsDefaults:
    """Verify every GMRSettings default is exactly as documented."""

    def test_pb_value(self):
        assert GMRSettings().pb_value == 1.5

    def test_pe(self):
        assert GMRSettings().pe == 15

    def test_dividend_yield(self):
        assert GMRSettings().dividend_yield == 0.035

    def test_debt_equity(self):
        assert GMRSettings().debt_equity == 1.5

    def test_roe(self):
        assert GMRSettings().roe == 15

    def test_net_profit_margin(self):
        assert GMRSettings().net_profit_margin == 15

    def test_years_for_avg(self):
        assert GMRSettings().years_for_avg == 5

    def test_win_probability(self):
        assert GMRSettings().win_probability == 0.50

    def test_diff_mat(self):
        assert GMRSettings().diff_mat == -0.025

    def test_trigger_v_up(self):
        assert GMRSettings().trigger_v_up == 0.30

    def test_trigger_v_down(self):
        assert GMRSettings().trigger_v_down == -0.30

    def test_min_volume(self):
        assert GMRSettings().min_volume == 1_000_000

    def test_min_price(self):
        assert GMRSettings().min_price == 0.40

    def test_max_price(self):
        assert GMRSettings().max_price == 2.50
