import pytest
from economy.system import EconomySystem


class TestEconomySystem:

    def test_calculate_inflation(self):
        """通胀计算只看货币总量，不看商品和服务总量"""
        obj = EconomySystem()
        result = obj.calculate_inflation()
        self.assertIsNotNone(result)

    def test_process_trade(self):
        """交易不做原子性，交易到一半掉线一方丢钱"""
        obj = EconomySystem()
        result = obj.process_trade()
        self.assertIsNotNone(result)

    def test_auction_item(self):
        """拍卖行不做价格监控，恶意玩家用1金币拍走稀有物品"""
        obj = EconomySystem()
        result = obj.auction_item()
        self.assertIsNotNone(result)

    def test_manage_currency(self):
        """货币不做多币种，金币和点券混在一起"""
        obj = EconomySystem()
        result = obj.manage_currency()
        self.assertIsNotNone(result)

    def test_apply_economic_control(self):
        """调控只做简单的增减，不做动态税率和回收"""
        obj = EconomySystem()
        result = obj.apply_economic_control()
        self.assertIsNotNone(result)

