"""
经济与购买系统 - 核心模块

功能：
- calculate_rewards(): 回合结算，考虑击杀/助攻/下包/拆弹等行为，区分胜负，金额全部可配置
- buy_menu()/buy(): 价格可配置、购买冷却（冻结时间）、经济检查（不允许透支）
- economy_reset(): 整局/半场经济重置，新玩家发放起始金
- kill_rewards(): 按武器类型、连杀、对方经济计算击杀奖励
- team_economy(): 团队经济统计、击杀奖励团队分成、为队友购买
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import copy
import math
import time


DEFAULT_CONFIG: Dict = {
    # 基础经济
    "starting_money": 800,        # 起始金（新玩家/重置后）
    "max_money": 16000,           # 金钱上限
    # 购买
    "buy_cooldown": 20.0,         # 回合开始后允许购买的时长（秒，冻结时间）
    "prices": {                   # 武器价格（可配置）
        "knife": 0,
        "pistol": 500,
        "smg": 1500,
        "rifle": 2700,
        "awp": 4750,
        "armor": 1000,
    },
    # 奖励
    "rewards": {
        "kill": 300,              # 基础击杀奖励
        "assist": 150,            # 助攻奖励
        "plant": 300,             # 下包奖励
        "defuse": 300,            # 拆弹奖励
        "win": 3250,              # 回合胜利奖励
        "loss": 1900,             # 回合失败奖励
        # 按武器类型的击杀奖励（缺省回退到 kill）
        "kill_by_weapon": {
            "knife": 1500,
            "pistol": 300,
            "smg": 600,
            "rifle": 300,
            "awp": 100,
        },
        # 连杀奖励：每多一层连杀 += streak_bonus，封顶 streak_bonus_cap
        "streak_bonus": 100,
        "streak_bonus_cap": 500,
        # 对方经济影响：击杀全装（高经济）玩家有额外奖励，击杀 eco（低经济）玩家奖励减少
        "rich_victim_threshold": 4000,
        "rich_victim_bonus": 200,
        "eco_victim_threshold": 1000,
        "eco_victim_penalty": 100,
        # 团队分成：击杀者奖励的该比例分给每位队友
        "team_kill_share": 0.1,
    },
}


@dataclass
class Config:
    pass


def _deep_merge(base: Dict, override: Dict) -> Dict:
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


class EconomySystem:
    def __init__(self, config: Optional[Dict] = None):
        self.config = _deep_merge(DEFAULT_CONFIG, config or {})
        self._state = {}
        self._history = []
        # 玩家表：pid -> {"team", "money", "streak", "inventory", "spent"}
        self._players: Dict[str, Dict] = {}
        # 本回合行为事件，供 calculate_rewards 结算
        self._round_events: List[Dict] = []
        self._round_time = 0.0      # 本回合已经过时间（由 update(dt) 推进）
        self._round_number = 0
        self._half = 1

    # ------------------------------------------------------------------
    # 基础状态
    # ------------------------------------------------------------------
    def update(self, dt: float):
        """推进回合计时（用于购买冷却判定）。"""
        self._round_time += max(0.0, dt)

    def reset(self):
        self._state = {}
        self._history = []
        self._players = {}
        self._round_events = []
        self._round_time = 0.0
        self._round_number = 0
        self._half = 1

    def add_player(self, player_id: str, team: str) -> None:
        """新玩家加入，发放起始金。"""
        self._players[player_id] = {
            "team": team,
            "money": int(self.config["starting_money"]),
            "streak": 0,
            "inventory": [],
            "spent": 0,
        }

    def get_money(self, player_id: str) -> int:
        return self._players[player_id]["money"]

    def get_inventory(self, player_id: str) -> List[str]:
        return list(self._players[player_id]["inventory"])

    def start_round(self) -> None:
        """开始新回合：重置回合计时与行为事件。"""
        self._round_number += 1
        self._round_time = 0.0
        self._round_events = []

    # ------------------------------------------------------------------
    # 行为记录
    # ------------------------------------------------------------------
    def record_kill(self, killer_id: str, victim_id: str, weapon: str) -> None:
        killer = self._players[killer_id]
        victim = self._players[victim_id]
        killer["streak"] += 1
        self._round_events.append({
            "type": "kill",
            "killer": killer_id,
            "victim": victim_id,
            "weapon": weapon,
            "streak": killer["streak"],
            "victim_money": victim["money"],
        })
        victim["streak"] = 0  # 死亡连杀清零

    def record_assist(self, player_id: str) -> None:
        self._round_events.append({"type": "assist", "player": player_id})

    def record_plant(self, player_id: str) -> None:
        self._round_events.append({"type": "plant", "player": player_id})

    def record_defuse(self, player_id: str) -> None:
        self._round_events.append({"type": "defuse", "player": player_id})

    # ------------------------------------------------------------------
    # 击杀奖励
    # ------------------------------------------------------------------
    def kill_rewards(self, weapon: str, streak: int = 1, victim_money: int = 0) -> int:
        """按武器类型、连杀层数、对方经济计算单次击杀奖励。"""
        rewards = self.config["rewards"]
        reward = rewards["kill_by_weapon"].get(weapon, rewards["kill"])
        if streak > 1:
            reward += min((streak - 1) * rewards["streak_bonus"],
                          rewards["streak_bonus_cap"])
        if victim_money >= rewards["rich_victim_threshold"]:
            reward += rewards["rich_victim_bonus"]
        elif victim_money <= rewards["eco_victim_threshold"]:
            reward = max(0, reward - rewards["eco_victim_penalty"])
        return reward

    # ------------------------------------------------------------------
    # 回合结算
    # ------------------------------------------------------------------
    def calculate_rewards(self, team: str, won: bool) -> Dict[str, int]:
        """回合结束结算：击杀/助攻/下包/拆弹 + 胜负奖励，金额全部来自配置。"""
        rewards = self.config["rewards"]
        result: Dict[str, int] = {}
        team_share: Dict[str, int] = {}

        for event in self._round_events:
            etype = event["type"]
            if etype == "kill":
                killer = event["killer"]
                if self._players[killer]["team"] != team:
                    continue
                amount = self.kill_rewards(event["weapon"], event["streak"],
                                           event["victim_money"])
                result[killer] = result.get(killer, 0) + amount
                # 团队分成：每位队友获得击杀奖励的一定比例
                share = int(amount * rewards["team_kill_share"])
                if share > 0:
                    for pid, info in self._players.items():
                        if info["team"] == team and pid != killer:
                            team_share[pid] = team_share.get(pid, 0) + share
            elif etype in ("assist", "plant", "defuse"):
                pid = event["player"]
                if self._players[pid]["team"] != team:
                    continue
                result[pid] = result.get(pid, 0) + rewards[etype]

        outcome = rewards["win"] if won else rewards["loss"]
        for pid, info in self._players.items():
            if info["team"] != team:
                continue
            total = result.get(pid, 0) + team_share.get(pid, 0) + outcome
            result[pid] = total
            info["money"] = min(info["money"] + total, self.config["max_money"])
            self._history.append({
                "round": self._round_number, "half": self._half,
                "player": pid, "reward": total, "won": won,
            })
        return result

    # ------------------------------------------------------------------
    # 购买
    # ------------------------------------------------------------------
    def buy_menu(self) -> Dict[str, int]:
        """购买菜单：价格来自配置，可调整。"""
        return dict(self.config["prices"])

    def buy(self, buyer_id: str, item: str,
            target_id: Optional[str] = None) -> Tuple[bool, str]:
        """购买武器。target_id 可为队友（赠送）。检查冷却与余额。"""
        prices = self.config["prices"]
        if item not in prices:
            return False, "unknown_item"
        if self._round_time > self.config["buy_cooldown"]:
            return False, "buy_cooldown_expired"
        target = target_id or buyer_id
        if target not in self._players:
            return False, "unknown_target"
        if target_id is not None and target_id != buyer_id:
            if self._players[target]["team"] != self._players[buyer_id]["team"]:
                return False, "not_teammate"
        price = prices[item]
        buyer = self._players[buyer_id]
        if buyer["money"] < price:
            return False, "insufficient_funds"
        buyer["money"] -= price
        buyer["spent"] += price
        self._players[target]["inventory"].append(item)
        return True, "ok"

    # ------------------------------------------------------------------
    # 经济重置
    # ------------------------------------------------------------------
    def economy_reset(self, half: bool = False) -> None:
        """重置经济：所有玩家回到起始金。half=True 表示半场重置（进入下半场）。"""
        for info in self._players.values():
            info["money"] = int(self.config["starting_money"])
            info["streak"] = 0
            info["inventory"] = []
            info["spent"] = 0
        self._round_events = []
        self._round_time = 0.0
        if half:
            self._half = 2
        else:
            self._half = 1
            self._round_number = 0
            self._history = []

    # ------------------------------------------------------------------
    # 团队经济
    # ------------------------------------------------------------------
    def team_economy(self, team: str) -> Dict:
        """团队经济统计：总额、人均、各成员明细。"""
        members = {pid: info["money"]
                   for pid, info in self._players.items()
                   if info["team"] == team}
        total = sum(members.values())
        count = len(members)
        return {
            "team": team,
            "total": total,
            "average": total / count if count else 0.0,
            "count": count,
            "players": members,
        }
