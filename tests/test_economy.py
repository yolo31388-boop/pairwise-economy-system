import pytest
from economy.system import EconomySystem


class TestEconomySystem:

    def test_update_price(self):
        """价格更新不做供需，需求高了不涨价"""
        obj = EconomySystem()
        result = obj.update_price()
        self.assertIsNotNone(result)

    def test_calculate_supply_demand(self):
        """供需不做生产，供给永远固定"""
        obj = EconomySystem()
        result = obj.calculate_supply_demand()
        self.assertIsNotNone(result)

    def test_calculate_trade_price(self):
        """贸易不做比较优势，所有地方价格一样"""
        obj = EconomySystem()
        result = obj.calculate_trade_price()
        self.assertIsNotNone(result)

    def test_calculate_inflation(self):
        """通货膨胀不做货币供应，钱印多了不贬值"""
        obj = EconomySystem()
        result = obj.calculate_inflation()
        self.assertIsNotNone(result)

    def test_run_market_simulation(self):
        """市场不做垄断，一家独大不影响价格"""
        obj = EconomySystem()
        result = obj.run_market_simulation()
        self.assertIsNotNone(result)

