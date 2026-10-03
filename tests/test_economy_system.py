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


def make_system(config=None, teams=(("p1", "T"), ("p2", "CT"))):
    system = EconomySystem(config)
    for pid, team in teams:
        system.add_player(pid, team)
    system.start_round()
    return system


class TestEconomySystem:
    def setup_method(self):
        self.system = EconomySystem()

    # 击杀奖励：按武器类型区分（刀杀 != 枪杀）
    def test_case_01(self):
        knife = self.system.kill_rewards("knife", streak=1, victim_money=2000)
        rifle = self.system.kill_rewards("rifle", streak=1, victim_money=2000)
        awp = self.system.kill_rewards("awp", streak=1, victim_money=2000)
        assert knife != rifle
        assert knife == 1500
        assert rifle == 300
        assert awp == 100

    # 击杀奖励：连杀有额外奖励
    def test_case_02(self):
        first = self.system.kill_rewards("rifle", streak=1, victim_money=2000)
        third = self.system.kill_rewards("rifle", streak=3, victim_money=2000)
        assert third > first
        assert third == first + 2 * 100

    # 击杀奖励：考虑对方经济（全装 vs eco）
    def test_case_03(self):
        rich = self.system.kill_rewards("rifle", streak=1, victim_money=10000)
        normal = self.system.kill_rewards("rifle", streak=1, victim_money=2000)
        eco = self.system.kill_rewards("rifle", streak=1, victim_money=100)
        assert rich > normal
        assert eco < normal

    # 奖励计算：考虑击杀/助攻/下包/拆弹等行为
    def test_case_04(self):
        s = make_system(teams=[("a", "T"), ("b", "T"), ("e1", "CT"), ("e2", "CT")])
        s._players["e1"]["money"] = 2000  # 正常经济，不触发eco减益
        s.record_kill("a", "e1", "rifle")
        s.record_assist("a")
        s.record_plant("a")
        s.record_defuse("b")
        rewards = s.calculate_rewards("T", won=False)
        loss = s.config["rewards"]["loss"]
        share = int(300 * s.config["rewards"]["team_kill_share"])
        # a: 击杀300 + 助攻150 + 下包300 + 失败1900
        assert rewards["a"] == 300 + 150 + 300 + loss
        # b: 拆弹300 + 失败1900 + 团队分成
        assert rewards["b"] == 300 + loss + share

    # 奖励计算：胜负奖励不同
    def test_case_05(self):
        s1 = make_system()
        win = s1.calculate_rewards("T", won=True)
        s2 = make_system()
        loss = s2.calculate_rewards("T", won=False)
        assert win["p1"] != loss["p1"]
        assert win["p1"] == s1.config["rewards"]["win"]
        assert loss["p1"] == s2.config["rewards"]["loss"]

    # 奖励计算：金额按配置，可调整
    def test_case_06(self):
        config = {"rewards": {"win": 5000, "kill": 999,
                              "kill_by_weapon": {"rifle": 999},
                              "team_kill_share": 0}}
        s = make_system(config)
        s._players["p2"]["money"] = 2000
        s.record_kill("p1", "p2", "rifle")
        rewards = s.calculate_rewards("T", won=True)
        assert rewards["p1"] == 999 + 5000

    # 购买菜单：价格可配置
    def test_case_07(self):
        default_menu = self.system.buy_menu()
        assert default_menu["rifle"] == 2700
        custom = EconomySystem({"prices": {"rifle": 3100, "awp": 5000}})
        menu = custom.buy_menu()
        assert menu["rifle"] == 3100
        assert menu["awp"] == 5000

    # 购买：检查经济，没钱不能买，余额不为负
    def test_case_08(self):
        s = make_system()  # 起始金 800
        ok, _ = s.buy("p1", "pistol")  # 500
        assert ok
        assert s.get_money("p1") == 300
        ok, reason = s.buy("p1", "rifle")  # 2700 > 300
        assert not ok
        assert reason == "insufficient_funds"
        assert s.get_money("p1") == 300 >= 0

    # 购买：有冷却，回合开始超过冻结时间后不能买
    def test_case_09(self):
        s = make_system()
        s.update(10.0)  # 冻结时间 20s 内
        ok, _ = s.buy("p1", "pistol")
        assert ok
        s.update(15.0)  # 累计 25s，超过冷却
        ok, reason = s.buy("p1", "smg")
        assert not ok
        assert reason == "buy_cooldown_expired"

    # 经济重置：整局重置，不带入上一局的钱
    def test_case_10(self):
        s = make_system()
        s._players["p1"]["money"] = 16000  # 上一局攒的钱
        s.economy_reset()
        assert s.get_money("p1") == s.config["starting_money"]
        assert s.get_money("p2") == s.config["starting_money"]

    # 经济重置：半场重置，上下半场经济不连续
    def test_case_11(self):
        s = make_system()
        s._players["p1"]["money"] = 12000  # 上半场攒的钱
        s.economy_reset(half=True)
        assert s.get_money("p1") == s.config["starting_money"]
        assert s._half == 2

    # 经济重置：新玩家有起始金；团队经济：可为队友购买
    def test_case_12(self):
        s = make_system(config={"starting_money": 800})
        s.add_player("newbie", "T")
        assert s.get_money("newbie") == 800
        # p1 给队友 newbie 买枪，扣 p1 的钱，装备进 newbie 背包
        s._players["p1"]["money"] = 5000
        ok, _ = s.buy("p1", "rifle", target_id="newbie")
        assert ok
        assert s.get_money("p1") == 5000 - 2700
        assert "rifle" in s.get_inventory("newbie")
        # 不能给敌人买
        ok, reason = s.buy("p1", "rifle", target_id="p2")
        assert not ok
        assert reason == "not_teammate"

    # 团队经济：击杀奖励团队分成 + 团队经济统计
    def test_case_13(self):
        s = make_system(teams=[("a", "T"), ("b", "T"), ("e1", "CT")])
        s._players["e1"]["money"] = 2000
        s.record_kill("a", "e1", "rifle")
        rewards = s.calculate_rewards("T", won=True)
        share = int(300 * s.config["rewards"]["team_kill_share"])
        assert share > 0
        # 队友 b 也分到击杀奖励
        assert rewards["b"] == s.config["rewards"]["win"] + share
        # 团队统计
        stats = s.team_economy("T")
        assert stats["count"] == 2
        assert stats["total"] == s.get_money("a") + s.get_money("b")
        assert stats["average"] == stats["total"] / 2
        assert stats["players"]["a"] == s.get_money("a")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
