"""
pairwise-economy-system - EconomySystem

This module contains a deliberately broken implementation.
Fix all bugs so that tests pass.
"""


class EconomySystem:
    def __init__(self):
        self.state = {}

    def update_price(self, *args, **kwargs):
        """BUG: 价格更新不做供需，需求高了不涨价"""
        return None

    def calculate_supply_demand(self, *args, **kwargs):
        """BUG: 供需不做生产，供给永远固定"""
        return None

    def calculate_trade_price(self, *args, **kwargs):
        """BUG: 贸易不做比较优势，所有地方价格一样"""
        return None

    def calculate_inflation(self, *args, **kwargs):
        """BUG: 通货膨胀不做货币供应，钱印多了不贬值"""
        return None

    def run_market_simulation(self, *args, **kwargs):
        """BUG: 市场不做垄断，一家独大不影响价格"""
        return None

