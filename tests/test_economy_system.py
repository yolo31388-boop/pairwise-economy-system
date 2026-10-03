"""
经济与购买系统 - 验收测试
运行方式：python -m pytest tests/test_economy_system.py -q
共 13 个测试用例
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from game.economy_system import EconomySystem


class TestEconomySystem:
    def setup_method(self):
        self.system = EconomySystem()

    def test_case_01(self):
        # calculate_rewards 考虑击杀、助攻、下包、拆弹等所有行为
        s = self.system
        s.add_player("p1", "T")
        s.add_player("e1", "CT")
        s.start_round()
        s.record_kill("p1", "e1", "ak47")
        s.record_assist("p1")
        s.record_plant("p1")
        s.record_defuse("p1")
        cfg = s.config
        expected = (
            cfg["kill_rewards"]["default"]
            + cfg["action_rewards"]["assist"]
            + cfg["action_rewards"]["plant"]
            + cfg["action_rewards"]["defuse"]
            + cfg["round_rewards"]["win"]
        )
        assert s.calculate_rewards("p1", won=True) == expected

    def test_case_02(self):
        # 奖励金额来自配置而非写死
        s = EconomySystem({
            "action_rewards": {"assist": 500, "plant": 800},
            "round_rewards": {"win": 5000, "loss": 1000},
        })
        s.add_player("p1", "T")
        s.start_round()
        s.record_assist("p1")
        s.record_plant("p1")
        assert s.calculate_rewards("p1", won=True) == 500 + 800 + 5000
        assert s.calculate_rewards("p1", won=False) == 500 + 800 + 1000

    def test_case_03(self):
        # 奖励区分回合胜负，赢和输不一样
        s = self.system
        s.add_player("p1", "T")
        s.add_player("e1", "CT")
        s.start_round()
        assert s.calculate_rewards("p1", won=True) != s.calculate_rewards("p1", won=False)
        results = s.end_round("T")
        assert results["p1"] == s.config["round_rewards"]["win"]
        assert results["e1"] == s.config["round_rewards"]["loss"]

    def test_case_04(self):
        # 购买菜单价格可通过配置调整
        assert self.system.buy_menu()["ak47"] == 2700
        s = EconomySystem({"starting_money": 5000, "prices": {"ak47": 1000}})
        assert s.buy_menu()["ak47"] == 1000
        s.add_player("p1", "T")
        s.start_round()
        assert s.buy("p1", "ak47") is True
        assert s.get_money("p1") == 5000 - 1000

    def test_case_05(self):
        # 购买有冷却：购买窗口关闭后不能购买
        s = self.system
        s.add_player("p1", "T")
        s.set_money("p1", 10000)
        s.start_round()
        assert s.buy_open
        assert s.buy("p1", "ak47") is True
        s.update(s.config["buy_time"] + 1)
        assert not s.buy_open
        assert s.buy("p1", "awp") is False
        assert s.get_money("p1") == 10000 - 2700

    def test_case_06(self):
        # 购买检查经济：没钱不能买，余额不会变负
        s = self.system
        s.add_player("p1", "T")  # 起始金 800
        s.start_round()
        assert s.buy("p1", "awp") is False  # 4750 > 800
        assert s.get_money("p1") == 800
        assert s.buy("p1", "deagle") is True
        assert s.get_money("p1") == 100
        assert s.buy("p1", "vest") is False  # 650 > 100
        assert s.get_money("p1") == 100 >= 0

    def test_case_07(self):
        # 经济重置：不能带着上一局的钱打下一局
        s = self.system
        s.add_player("p1", "T")
        s.add_player("e1", "CT")
        s.start_round()
        s.end_round("T")
        assert s.get_money("p1") > s.config["starting_money"]
        s.economy_reset()
        assert s.get_money("p1") == s.config["starting_money"]
        assert s.get_money("e1") == s.config["starting_money"]

    def test_case_08(self):
        # 经济重置考虑半场：上下半场经济不连续
        s = self.system
        s.add_player("p1", "T")
        s.add_player("e1", "CT")
        s.start_round()
        s.end_round("CT")
        assert s.half == 1
        s.economy_reset(halftime=True)
        assert s.half == 2
        assert s.get_money("p1") == s.config["starting_money"]
        assert s.get_money("e1") == s.config["starting_money"]
        s.economy_reset()
        assert s.half == 1

    def test_case_09(self):
        # 新玩家加入发放起始金，且起始金可配置
        self.system.add_player("p1", "T")
        assert self.system.get_money("p1") == 800
        s = EconomySystem({"starting_money": 16000})
        s.add_player("p2", "CT")
        assert s.get_money("p2") == 16000

    def test_case_10(self):
        # 击杀奖励按武器类型区分：刀杀和枪杀不一样
        s = self.system
        s.add_player("k", "T")
        s.add_player("v", "CT")
        s.start_round()
        knife_reward = s.kill_rewards("k", "v", "knife")
        gun_reward = s.kill_rewards("k", "v", "ak47")
        awp_reward = s.kill_rewards("k", "v", "awp")
        assert knife_reward == s.config["kill_rewards"]["knife"]
        assert gun_reward == s.config["kill_rewards"]["default"]
        assert knife_reward > gun_reward > awp_reward

    def test_case_11(self):
        # 击杀奖励考虑连杀：连杀有额外奖励
        s = self.system
        s.add_player("k", "T")
        s.add_player("v1", "CT")
        s.add_player("v2", "CT")
        s.add_player("v3", "CT")
        s.start_round()
        first = s.record_kill("k", "v1", "ak47")
        second = s.record_kill("k", "v2", "ak47")
        third = s.record_kill("k", "v3", "ak47")
        bonus = s.config["killstreak_bonus"]
        assert second == first + bonus
        assert third == first + 2 * bonus

    def test_case_12(self):
        # 击杀奖励考虑对方经济：击杀全装玩家奖励更高
        s = self.system
        s.add_player("k", "T")
        s.add_player("rich", "CT")
        s.add_player("poor", "CT")
        s.set_money("rich", 10000)
        s.set_money("poor", 500)
        s.start_round()
        rich_reward = s.kill_rewards("k", "rich", "ak47")
        poor_reward = s.kill_rewards("k", "poor", "ak47")
        assert rich_reward == poor_reward + s.config["eco_bonus"]

    def test_case_13(self):
        # 团队经济：给队友购买、击杀奖励共享、团队经济统计
        s = self.system
        s.add_player("p1", "T")
        s.add_player("p2", "T")
        s.add_player("e1", "CT")
        s.set_money("p1", 10000)
        s.start_round()
        # 给队友买武器，不能给敌人买
        assert s.buy("p1", "ak47", target_id="p2") is True
        assert s.get_money("p1") == 10000 - 2700
        assert "ak47" in s.get_inventory("p2")
        assert s.buy("p1", "ak47", target_id="e1") is False
        # 击杀奖励共享给团队
        reward = s.record_kill("p1", "e1", "ak47")
        share = int(reward * s.config["team_share_ratio"])
        assert share > 0
        assert s.calculate_rewards("p2", won=False) == share + s.config["round_rewards"]["loss"]
        # 团队经济统计
        stats = s.team_economy("T")
        assert stats["count"] == 2
        assert stats["total"] == s.get_money("p1") + s.get_money("p2")
        assert stats["average"] == stats["total"] / 2
        assert stats["players"]["p1"] == s.get_money("p1")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
