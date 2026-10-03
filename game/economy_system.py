"""
经济与购买系统 - 核心模块（修复版）

修复内容：
- calculate_rewards(): 奖励覆盖击杀/助攻/下包/拆弹等行为，金额全部来自配置，并区分回合胜负
- buy_menu()/buy(): 价格由配置驱动，回合开始后购买窗口有限时冷却，购买前检查余额
- economy_reset(): 正确重置经济，区分半场换边与整局重开，新玩家发放起始金
- kill_rewards(): 按武器类型计价，支持连杀加成，并考虑对方经济（全装/ECO）
- team_economy(): 支持给队友购买、击杀奖励团队共享与团队经济统计
"""
import copy
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Config:
    pass


DEFAULT_CONFIG = {
    "starting_money": 800,        # 起始金 / 重置后金额
    "max_money": 16000,           # 经济上限
    "buy_time": 20.0,             # 回合开始后的购买窗口（秒）
    "prices": {                   # 购买菜单价格（可配置）
        "ak47": 2700,
        "m4a4": 3100,
        "awp": 4750,
        "deagle": 700,
        "vest": 650,
    },
    "action_rewards": {           # 行为奖励（可配置）
        "assist": 150,
        "plant": 300,
        "defuse": 300,
    },
    "round_rewards": {            # 回合胜负奖励（可配置）
        "win": 3250,
        "loss": 1900,
    },
    "kill_rewards": {             # 击杀奖励，按武器类型（可配置）
        "default": 300,
        "knife": 1500,
        "awp": 100,
    },
    "killstreak_bonus": 100,      # 每次连杀的额外奖励
    "full_buy_threshold": 4000,   # 对方经济达到该值视为全装
    "eco_bonus": 200,             # 击杀全装玩家的额外奖励
    "team_share_ratio": 0.25,     # 击杀奖励共享给队友的比例
}


def _merge_config(base: Dict, override: Optional[Dict]) -> Dict:
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass
class _Player:
    player_id: str
    team: str
    money: int = 0
    inventory: List[str] = field(default_factory=list)


class EconomySystem:
    def __init__(self, config: Optional[Dict] = None):
        self.config = _merge_config(DEFAULT_CONFIG, config)
        self._players: Dict[str, _Player] = {}
        self._round_actions: Dict[str, List] = {}
        self._killstreaks: Dict[str, int] = {}
        self._buy_remaining = 0.0
        self._round_number = 0
        self._half = 1
        self._state = {}
        self._history = []

    # ---------- 玩家管理 ----------

    def add_player(self, player_id: str, team: str) -> None:
        """新玩家加入，发放起始金。"""
        if player_id in self._players:
            raise ValueError("玩家已存在: %s" % player_id)
        self._players[player_id] = _Player(
            player_id=player_id,
            team=team,
            money=int(self.config["starting_money"]),
        )
        self._round_actions[player_id] = []
        self._killstreaks[player_id] = 0

    def _require_player(self, player_id: str) -> _Player:
        if player_id not in self._players:
            raise ValueError("未知玩家: %s" % player_id)
        return self._players[player_id]

    def get_money(self, player_id: str) -> int:
        return self._require_player(player_id).money

    def set_money(self, player_id: str, amount: int) -> None:
        self._require_player(player_id).money = max(0, int(amount))

    def get_inventory(self, player_id: str) -> List[str]:
        return list(self._require_player(player_id).inventory)

    # ---------- 回合与计时 ----------

    @property
    def round_number(self) -> int:
        return self._round_number

    @property
    def half(self) -> int:
        return self._half

    @property
    def buy_open(self) -> bool:
        return self._buy_remaining > 0

    def start_round(self) -> None:
        """开始新回合：打开购买窗口，清空回合行为与连杀。"""
        self._round_number += 1
        self._buy_remaining = float(self.config["buy_time"])
        for pid in self._players:
            self._round_actions[pid] = []
            self._killstreaks[pid] = 0

    def update(self, dt: float):
        """推进计时，购买窗口随时间关闭。"""
        if self._buy_remaining > 0:
            self._buy_remaining = max(0.0, self._buy_remaining - max(0.0, dt))

    # ---------- 购买系统 ----------

    def buy_menu(self) -> Dict[str, int]:
        """购买菜单，价格完全来自配置。"""
        return dict(self.config["prices"])

    def buy(self, player_id: str, weapon: str, target_id: Optional[str] = None) -> bool:
        """购买武器。可指定 target_id 为同队队友代购。

        购买窗口关闭、余额不足、给敌人代购时返回 False，余额不会变负。
        """
        buyer = self._require_player(player_id)
        prices = self.config["prices"]
        if weapon not in prices:
            raise ValueError("未知武器: %s" % weapon)
        if not self.buy_open:
            return False
        target_id = target_id or player_id
        target = self._require_player(target_id)
        if target.team != buyer.team:
            return False
        price = int(prices[weapon])
        if buyer.money < price:
            return False
        buyer.money -= price
        target.inventory.append(weapon)
        return True

    # ---------- 行为记录与击杀奖励 ----------

    def kill_rewards(self, killer_id: str, victim_id: str, weapon: str = "default") -> int:
        """击杀奖励：按武器类型计价，叠加连杀加成与对方经济加成。"""
        self._require_player(killer_id)
        victim = self._require_player(victim_id)
        table = self.config["kill_rewards"]
        base = int(table.get(weapon, table.get("default", 300)))
        streak = self._killstreaks.get(killer_id, 0)
        streak_bonus = streak * int(self.config["killstreak_bonus"])
        eco_bonus = 0
        if victim.money >= int(self.config["full_buy_threshold"]):
            eco_bonus = int(self.config["eco_bonus"])
        return base + streak_bonus + eco_bonus

    def record_kill(self, killer_id: str, victim_id: str, weapon: str = "default") -> int:
        """记录击杀：奖励计入击杀者，并按比例共享给同队队友。"""
        killer = self._require_player(killer_id)
        self._require_player(victim_id)
        reward = self.kill_rewards(killer_id, victim_id, weapon)
        self._round_actions.setdefault(killer_id, []).append(("kill", reward))
        share = int(reward * float(self.config["team_share_ratio"]))
        if share > 0:
            for pid, player in self._players.items():
                if pid != killer_id and player.team == killer.team:
                    self._round_actions.setdefault(pid, []).append(("team_share", share))
        self._killstreaks[killer_id] = self._killstreaks.get(killer_id, 0) + 1
        self._killstreaks[victim_id] = 0
        return reward

    def _record_action(self, player_id: str, action: str) -> int:
        self._require_player(player_id)
        amount = int(self.config["action_rewards"][action])
        self._round_actions.setdefault(player_id, []).append((action, amount))
        return amount

    def record_assist(self, player_id: str) -> int:
        return self._record_action(player_id, "assist")

    def record_plant(self, player_id: str) -> int:
        return self._record_action(player_id, "plant")

    def record_defuse(self, player_id: str) -> int:
        return self._record_action(player_id, "defuse")

    # ---------- 奖励结算 ----------

    def calculate_rewards(self, player_id: str, won: bool) -> int:
        """回合奖励 = 本回合所有行为奖励之和 + 胜负奖励，金额全部来自配置。"""
        self._require_player(player_id)
        total = sum(amount for _, amount in self._round_actions.get(player_id, []))
        key = "win" if won else "loss"
        total += int(self.config["round_rewards"][key])
        return total

    def end_round(self, winning_team: str) -> Dict[str, int]:
        """回合结束结算：按胜负发放奖励并封顶，清空回合行为。"""
        results = {}
        max_money = int(self.config["max_money"])
        for pid, player in self._players.items():
            reward = self.calculate_rewards(pid, player.team == winning_team)
            player.money = min(max_money, player.money + reward)
            results[pid] = reward
        for pid in self._players:
            self._round_actions[pid] = []
        self._buy_remaining = 0.0
        self._history.append((self._round_number, winning_team))
        return results

    # ---------- 经济重置 ----------

    def economy_reset(self, halftime: bool = False) -> None:
        """重置经济：所有玩家回到起始金，清空库存与回合状态。

        halftime=True 表示半场换边（进入下一个半场），否则整局重开。
        """
        for player in self._players.values():
            player.money = int(self.config["starting_money"])
            player.inventory.clear()
        for pid in self._players:
            self._round_actions[pid] = []
            self._killstreaks[pid] = 0
        self._buy_remaining = 0.0
        self._round_number = 0
        if halftime:
            self._half += 1
        else:
            self._half = 1
            self._history = []

    def reset(self):
        """完全重置：清空玩家与全部状态。"""
        self._players = {}
        self._round_actions = {}
        self._killstreaks = {}
        self._buy_remaining = 0.0
        self._round_number = 0
        self._half = 1
        self._state = {}
        self._history = []

    # ---------- 团队经济 ----------

    def team_economy(self, team: str) -> Dict:
        """团队经济统计：总人数、总经济、平均经济与每人明细。"""
        members = {pid: p.money for pid, p in self._players.items() if p.team == team}
        total = sum(members.values())
        count = len(members)
        return {
            "team": team,
            "count": count,
            "total": total,
            "average": (total / count) if count else 0.0,
            "players": members,
        }
