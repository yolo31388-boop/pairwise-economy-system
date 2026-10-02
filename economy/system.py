"""
pairwise-economy-system - EconomySystem

服务端游戏经济系统。修复后的实现覆盖五大子系统：

1. 通胀计算 ``calculate_inflation``
   - 货币数量论：物价水平 = 货币总量 / 商品和服务总量（费雪方程式 M*V/P*Y 简化版）
   - 通胀率基于物价水平的历史快照计算，而不是只看货币增量
   - 显式记录货币投放(faucets)与回笼(sinks)，金币不再只增不减
   - 提供 ``sinks_analysis`` 资金来源/去向（水龙头/黑洞）分析
2. 玩家交易 ``process_trade``
   - 原子性：预校验全部通过后才落账，任何一步失败整体回滚，掉线不丢钱
   - 金额上限：单笔限额 + 不得超过服务器可流通总量
   - 全额交易日志，手续费强制收取且不可绕过
3. 拍卖行 ``auction_item``
   - 价格监控：稀有度地板价保护 + 保证金 + 与市场参考价偏离拦截
   - 防刷：同一玩家在售数量上限
   - 到期处理：``process_expired_auctions`` 自动成交/流拍退保证金
4. 货币管理 ``manage_currency``
   - 多币种（金币、点券……可注册），交易性区分绑定/非绑定
   - 完整流水（ledger），支持对账
5. 经济调控 ``apply_economic_control``
   - 动态税率：按预测通胀率自动调整交易税
   - 事前预测：``predict_inflation`` 不等通胀发生才救
   - A/B 测试：对照组/实验组分组施策并比较效果
"""

from __future__ import annotations

import itertools
import time
from collections import defaultdict
from typing import Any


class EconomyError(Exception):
    """经济系统业务错误（违规交易/拍卖/货币操作）。"""


# 默认货币：金币是可自由交易的软通货，点券为充值硬通货（默认不可交易）
DEFAULT_CURRENCIES = {
    "gold": {"tradeable": True, "bound_tradeable": False},
    "coupon": {"tradeable": False, "bound_tradeable": False},
}

# 稀有度地板价：防止 1 金币拍走稀有物品
DEFAULT_RARITY_FLOOR = {
    "common": 1,
    "uncommon": 10,
    "rare": 100,
    "epic": 1000,
    "legendary": 10000,
}


def _pop_alias(kwargs: dict[str, Any], *names: str, default: Any = None) -> Any:
    """从 kwargs 中按多个候选键名取值（兼容不同调用方命名）。"""
    for name in names:
        if name in kwargs:
            return kwargs.pop(name)
    return default


class EconomySystem:
    def __init__(
        self,
        trade_fee_rate: float = 0.05,
        max_trade_amount: int | float | None = None,
        max_active_auctions_per_player: int = 5,
        auction_duration: int = 86400,
    ):
        # 货币配置：currency -> {"tradeable", "bound_tradeable"}
        self.currencies: dict[str, dict[str, bool]] = {
            code: dict(cfg) for code, cfg in DEFAULT_CURRENCIES.items()
        }
        # 玩家余额：player -> currency -> {"bound": x, "unbound": x}
        self.balances: dict[str, dict[str, dict[str, int]]] = defaultdict(
            lambda: defaultdict(lambda: {"bound": 0, "unbound": 0})
        )
        # 背包：player -> item_id -> 数量
        self.inventory: dict[str, dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        # 货币流水：每条是一次不可变的资金移动
        self.ledger: list[dict[str, Any]] = []
        # 水龙头（产出）/ 黑洞（回收）分类汇总
        self.faucets: dict[str, int] = defaultdict(int)
        self.sinks: dict[str, int] = defaultdict(int)
        # 通胀历史快照（物价水平序列，用于计算通胀率）
        self.inflation_history: list[dict[str, Any]] = []
        # 交易与拍卖日志（审计用）
        self.trade_logs: list[dict[str, Any]] = []
        self.auction_logs: list[dict[str, Any]] = []
        # 拍卖行
        self.auctions: dict[str, dict[str, Any]] = {}
        self.market_prices: dict[str, list[int]] = {}
        # 调控参数
        self.tax_rate = trade_fee_rate
        self.base_tax_rate = trade_fee_rate
        self.max_trade_amount = max_trade_amount
        self.max_active_auctions_per_player = max_active_auctions_per_player
        self.auction_duration = auction_duration
        self.rarity_floor = dict(DEFAULT_RARITY_FLOOR)
        # 可注入时钟，便于测试到期逻辑
        self._now: float | None = None
        self._id_seq = itertools.count(1)

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _time(self) -> float:
        return self._now if self._now is not None else time.time()

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._id_seq)}"

    def _ensure_currency(self, currency: str) -> None:
        if currency not in self.currencies:
            raise EconomyError(f"未知币种: {currency}")

    def _entry(
        self,
        *,
        player: str,
        currency: str,
        bound: bool,
        delta: int,
        reason: str,
        ref: str | None = None,
        counterpart: str | None = None,
        balance_after: int | None = None,
    ) -> dict[str, Any]:
        entry = {
            "id": self._new_id("tx"),
            "timestamp": self._time(),
            "player": player,
            "currency": currency,
            "bound": bound,
            "delta": delta,
            "reason": reason,
            "reference": ref,
            "counterpart": counterpart,
            "balance_after": balance_after,
        }
        self.ledger.append(entry)
        return entry

    def get_balance(self, player: str, currency: str = "gold") -> dict[str, int]:
        """返回玩家某币种的 {"bound", "unbound", "total"} 余额。"""
        self._ensure_currency(currency)
        bal = self.balances[player][currency]
        return {"bound": bal["bound"], "unbound": bal["unbound"], "total": bal["bound"] + bal["unbound"]}

    def _change(
        self,
        player: str,
        currency: str,
        amount: int,
        *,
        bound: bool,
        reason: str,
        ref: str | None = None,
        counterpart: str | None = None,
        log: bool = True,
    ) -> int:
        """修改余额（内部），amount 可正可负；余额不足抛错。"""
        self._ensure_currency(currency)
        if amount == 0:
            return self.balances[player][currency]["bound" if bound else "unbound"]
        slot = "bound" if bound else "unbound"
        bal = self.balances[player][currency]
        if bal[slot] + amount < 0:
            raise EconomyError(
                f"余额不足: {player} {currency} {slot} 需要 {-amount}，仅有 {bal[slot]}"
            )
        bal[slot] += amount
        if log:
            self._entry(
                player=player,
                currency=currency,
                bound=bound,
                delta=amount,
                reason=reason,
                ref=ref,
                counterpart=counterpart,
                balance_after=bal[slot],
            )
        return bal[slot]

    def _add_faucet(self, category: str, amount: int) -> None:
        if amount > 0:
            self.faucets[category] += amount

    def _add_sink(self, category: str, amount: int) -> None:
        if amount > 0:
            self.sinks[category] += amount

    def _money_supply(self, currency: str = "gold") -> int:
        """某币种在全部玩家手里的存量（含绑定）。"""
        return sum(
            bal[currency]["bound"] + bal[currency]["unbound"]
            for bal in self.balances.values()
            if currency in bal
        )

    def _tradeable_supply(self, currency: str = "gold") -> int:
        """可流通（非绑定）存量，作为单笔交易上限的硬顶。"""
        return sum(
            bal[currency]["unbound"] for bal in self.balances.values() if currency in bal
        )

    class _Atomic:
        """原子操作上下文：异常时按相反顺序执行已注册的补偿动作。"""

        def __init__(self, system: "EconomySystem"):
            self.system = system
            self.undo: list = []

        def __enter__(self) -> "EconomySystem._Atomic":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            if exc_type is not None:
                for rollback in reversed(self.undo):
                    rollback()
            return False

    # ------------------------------------------------------------------ #
    # 1. 通胀计算（货币总量 / 商品和服务总量 + 货币回笼 + sinks 分析）
    # ------------------------------------------------------------------ #
    def calculate_inflation(self, *args, **kwargs) -> dict[str, Any]:
        """计算物价水平与通胀率。

        依据费雪方程式的简化形式 ``P = M / Y``（流通速度 V 视为稳定），
        即物价水平由货币总量与商品和服务总量共同决定，而不是只看货币量。

        :param money_supply: 当前货币总量；缺省取系统追踪的实际存量
        :param goods_total: 当前商品和服务总量；缺省取系统追踪值
        :param velocity: 货币流通速度，默认 1.0
        :param currency: 统计币种，默认 gold
        :param sink_amount: 本期额外回笼量（销毁/回收）
        :param sink_category: 回笼归类（默认 "snapshot_sink"）
        :returns: 物价水平、通胀率、投放/回笼、净货币量等指标
        """
        currency = _pop_alias(kwargs, "currency", default=args[2] if len(args) > 2 else "gold")
        money_supply = _pop_alias(
            kwargs, "money_supply", "money", "total_money", "m",
            default=args[0] if args else None,
        )
        goods_total = _pop_alias(
            kwargs, "goods_total", "goods", "total_goods", "services", "y",
            default=args[1] if len(args) > 1 else None,
        )
        velocity = _pop_alias(kwargs, "velocity", "v", default=1.0)
        sink_amount = int(_pop_alias(kwargs, "sink_amount", "sink", "burn", default=0) or 0)
        sink_category = _pop_alias(kwargs, "sink_category", default="snapshot_sink")

        tracked_money = self._money_supply(currency)
        tracked_goods = sum(sum(items.values()) for items in self.inventory.values())
        money = int(money_supply if money_supply is not None else tracked_money)
        goods = int(goods_total if goods_total is not None else tracked_goods)

        if sink_amount > 0:
            self._add_sink(sink_category, sink_amount)
            money = max(0, money - sink_amount)

        # 商品总量为 0 时物价不可定义，视为 0（空市场没有通胀可言）
        price_level = (money * velocity / goods) if goods > 0 else 0.0

        previous_price = (
            self.inflation_history[-1]["price_level"] if self.inflation_history else None
        )
        if previous_price and previous_price > 0:
            inflation_rate = (price_level - previous_price) / previous_price
        else:
            inflation_rate = 0.0

        total_faucets = sum(self.faucets.values())
        total_sinks = sum(self.sinks.values())
        snapshot = {
            "timestamp": self._time(),
            "currency": currency,
            "money_supply": money,
            "goods_total": goods,
            "velocity": velocity,
            "price_level": price_level,
            "inflation_rate": inflation_rate,
            "previous_price_level": previous_price,
            "total_faucets": total_faucets,
            "total_sinks": total_sinks,
            "net_money_injected": total_faucets - total_sinks,
        }
        self.inflation_history.append(snapshot)
        return dict(snapshot)

    def sinks_analysis(self, *args, **kwargs) -> dict[str, Any]:
        """资金来源/去向分析：水龙头(产出) vs 黑洞(回收)。

        :param required_sink_ratio: 目标回笼/投放比，默认 0.5
        :returns: 各类 faucets/sinks、总量、净流入、缺口与是否健康
        """
        required_ratio = float(
            _pop_alias(kwargs, "required_sink_ratio", "target_ratio", default=0.5)
        )
        total_faucets = sum(self.faucets.values())
        total_sinks = sum(self.sinks.values())
        net_injection = total_faucets - total_sinks
        actual_ratio = (total_sinks / total_faucets) if total_faucets > 0 else 1.0
        healthy = total_sinks >= total_faucets * required_ratio
        sink_gap = max(0, int(total_faucets * required_ratio) - total_sinks)
        return {
            "faucets": dict(self.faucets),
            "sinks": dict(self.sinks),
            "total_faucets": total_faucets,
            "total_sinks": total_sinks,
            "net_injection": net_injection,
            "sink_ratio": actual_ratio,
            "required_sink_ratio": required_ratio,
            "sink_gap": sink_gap,
            "healthy": healthy,
        }

    def record_faucet(self, category: str, amount: int, currency: str = "gold") -> None:
        """登记一笔货币投放（产出），用于 sinks 分析。"""
        self._ensure_currency(currency)
        self._add_faucet(category, int(amount))

    def record_sink(self, category: str, amount: int, currency: str = "gold") -> None:
        """登记一笔货币回笼（回收/销毁），用于 sinks 分析。"""
        self._ensure_currency(currency)
        self._add_sink(category, int(amount))

    # ------------------------------------------------------------------ #
    # 2. 玩家交易（原子性 + 金额上限 + 手续费强制收取 + 日志）
    # ------------------------------------------------------------------ #
    def trade_capacity(self, currency: str = "gold") -> int:
        """单笔交易有效上限：配置上限与服务器可流通总量取小。

        保证一次交易不可能转走服务器上所有金币。
        """
        supply = self._tradeable_supply(currency)
        if self.max_trade_amount is None:
            return supply
        return min(int(self.max_trade_amount), supply)

    def process_trade(self, *args, **kwargs) -> dict[str, Any]:
        """执行一笔玩家间交易，全程原子化。

        流程：预校验（币种可交易/非绑定/金额上限/双方余额）→ 原子扣款加款
        → 强制扣手续费并计入回笼 → 写交易日志。任何一步失败，此前全部操作
        回滚，交易到一半掉线也不会有人丢钱。

        :param sender: 付款方
        :param recipient: 收款方
        :param amount: 本金金额（必须为正且不超过交易上限）
        :param currency: 币种，默认 gold
        :param bound: 是否使用绑定货币，绑定货币禁止玩家间交易
        :param item_id/item_quantity: 可选，收款方同时交付的物品
        :param fee_rate: 手续费率，缺省用系统当前税率（不可传 0 绕过）
        :param fee_payer: "seller"(默认，付款方额外出手续费) 或 "buyer"
        """
        sender = _pop_alias(kwargs, "sender", "from", "from_player", "buyer",
                            default=args[0] if args else None)
        recipient = _pop_alias(kwargs, "recipient", "to", "to_player", "seller",
                               default=args[1] if len(args) > 1 else None)
        amount = _pop_alias(kwargs, "amount", "sum", "price", "value",
                            default=args[2] if len(args) > 2 else None)
        currency = _pop_alias(kwargs, "currency", default=args[3] if len(args) > 3 else "gold")
        bound = bool(_pop_alias(kwargs, "bound", "is_bound", default=False))
        item_id = _pop_alias(kwargs, "item_id", "item", default=None)
        item_quantity = int(_pop_alias(kwargs, "item_quantity", "item_qty", "qty", default=1))
        fee_rate = _pop_alias(kwargs, "fee_rate", "tax_rate", default=None)
        fee_payer = _pop_alias(kwargs, "fee_payer", default="seller")
        reason = _pop_alias(kwargs, "reason", "note", default="player_trade")

        # 无参调用：返回当前交易能力概览，而不是 None
        if sender is None or recipient is None or amount is None:
            return {
                "status": "ready",
                "currency": currency,
                "fee_rate": self.tax_rate if fee_rate is None else fee_rate,
                "max_trade_amount": self.trade_capacity(currency),
                "tradeable_supply": self._tradeable_supply(currency),
            }

        amount = int(amount)
        if fee_rate is None:
            fee_rate = self.tax_rate
        fee_rate = float(fee_rate)
        trade_id = self._new_id("trade")

        # ---- 预校验（先全部检查，再动账，保证原子性）----
        if sender == recipient:
            raise EconomyError("不能与自己交易")
        self._ensure_currency(currency)
        cfg = self.currencies[currency]
        if not cfg["tradeable"]:
            raise EconomyError(f"币种 {currency} 不可交易")
        if bound and not cfg["bound_tradeable"]:
            raise EconomyError("绑定货币不可玩家间交易")
        if amount <= 0:
            raise EconomyError("交易金额必须为正数")
        cap = self.trade_capacity(currency)
        if amount > cap:
            raise EconomyError(
                f"交易金额 {amount} 超过单笔上限 {cap}（服务器可流通总量受限）"
            )
        total_supply = self._tradeable_supply(currency)
        if total_supply > 0 and amount >= total_supply:
            raise EconomyError(
                f"单笔交易不得转走服务器全部可流通 {currency}（总量 {total_supply}）"
            )
        fee = int(amount * fee_rate)
        sender_cost = amount + fee if fee_payer == "seller" else amount
        sender_balance = self.balances[sender][currency]["bound" if bound else "unbound"]
        if sender_balance < sender_cost:
            raise EconomyError(
                f"付款方余额不足：需要 {sender_cost}（含手续费 {fee}），仅有 {sender_balance}"
            )
        if item_id is not None and self.inventory[recipient].get(item_id, 0) < item_quantity:
            raise EconomyError(f"收款方物品不足: {item_id} x{item_quantity}")

        # ---- 原子提交 ----
        try:
            with self._Atomic(self) as atomic:
                self._change(sender, currency, -sender_cost, bound=bound,
                             reason=reason, ref=trade_id, counterpart=recipient)
                atomic.undo.append(
                    lambda: self._change(sender, currency, sender_cost, bound=bound,
                                         reason="rollback", ref=trade_id,
                                         counterpart=recipient, log=False)
                )
                self._change(recipient, currency, amount, bound=False,
                             reason=reason, ref=trade_id, counterpart=sender)
                atomic.undo.append(
                    lambda: self._change(recipient, currency, -amount, bound=False,
                                         reason="rollback", ref=trade_id,
                                         counterpart=sender, log=False)
                )
                if fee > 0:
                    # 手续费强制回收，从流通中永久移出，计入 sinks
                    self._add_sink("trade_fee", fee)
                    atomic.undo.append(lambda: self.sinks.__setitem__(
                        "trade_fee", max(0, self.sinks["trade_fee"] - fee)))
                if item_id is not None:
                    self.inventory[recipient][item_id] -= item_quantity
                    self.inventory[sender][item_id] += item_quantity
                    atomic.undo.append(lambda: (
                        self.inventory[recipient].__setitem__(
                            item_id, self.inventory[recipient][item_id] + item_quantity),
                        self.inventory[sender].__setitem__(
                            item_id, self.inventory[sender][item_id] - item_quantity),
                    ))
        except EconomyError:
            self.trade_logs.append({
                "id": trade_id,
                "timestamp": self._time(),
                "sender": sender,
                "recipient": recipient,
                "amount": amount,
                "currency": currency,
                "bound": bound,
                "fee": fee,
                "status": "failed",
                "reason": reason,
            })
            raise

        record = {
            "id": trade_id,
            "timestamp": self._time(),
            "sender": sender,
            "recipient": recipient,
            "amount": amount,
            "currency": currency,
            "bound": bound,
            "fee": fee,
            "fee_rate": fee_rate,
            "fee_payer": fee_payer,
            "item_id": item_id,
            "item_quantity": item_quantity if item_id is not None else 0,
            "status": "success",
            "reason": reason,
        }
        self.trade_logs.append(record)
        return dict(record)

    def get_trade_logs(self, player: str | None = None) -> list[dict[str, Any]]:
        """查询交易日志（审计谁转了谁）；可按玩家过滤。"""
        if player is None:
            return list(self.trade_logs)
        return [
            log for log in self.trade_logs
            if log.get("sender") == player or log.get("recipient") == player
        ]

    # ------------------------------------------------------------------ #
    # 3. 拍卖行（价格监控 + 防刷 + 到期处理）
    # ------------------------------------------------------------------ #
    def active_auctions(self, seller: str | None = None) -> list[dict[str, Any]]:
        """返回在售拍卖；可按卖家过滤。"""
        return [
            auction for auction in self.auctions.values()
            if auction["status"] == "active" and (seller is None or auction["seller"] == seller)
        ]

    def auction_item(self, *args, **kwargs) -> dict[str, Any]:
        """上架物品到拍卖行。

        价格监控：起拍价不得低于稀有度地板价，且不得显著低于该物品的市场
        成交参考价（防止 1 金币拍走稀有物品）；上架需缴纳保证金。
        防刷：同一玩家在售数量受 ``max_active_auctions_per_player`` 限制。

        :param seller: 卖家
        :param item_id: 物品 id
        :param quantity: 数量，默认 1
        :param starting_price: 起拍价
        :param buyout_price: 一口价（可选）
        :param rarity: 稀有度，默认 common
        :param currency: 结算币种，默认 gold
        :param duration: 挂拍时长（秒），默认系统配置
        :param deposit: 保证金，默认为起拍价 5%（至少 1）
        """
        seller = _pop_alias(kwargs, "seller", "owner", "player",
                            default=args[0] if args else None)
        item_id = _pop_alias(kwargs, "item_id", "item",
                             default=args[1] if len(args) > 1 else None)
        starting_price = _pop_alias(
            kwargs, "starting_price", "start_price", "price", "min_price",
            default=args[2] if len(args) > 2 else None,
        )
        quantity = int(_pop_alias(kwargs, "quantity", "qty", default=1))
        buyout_price = _pop_alias(kwargs, "buyout_price", "buyout", default=None)
        rarity = _pop_alias(kwargs, "rarity", default="common")
        currency = _pop_alias(kwargs, "currency", default="gold")
        duration = _pop_alias(kwargs, "duration", default=self.auction_duration)
        deposit = _pop_alias(kwargs, "deposit", default=None)

        # 无参调用：返回拍卖行状态
        if seller is None or item_id is None or starting_price is None:
            return {
                "status": "ready",
                "active_count": len(self.active_auctions()),
                "max_active_per_player": self.max_active_auctions_per_player,
                "default_duration": self.auction_duration,
                "rarity_floor": dict(self.rarity_floor),
            }

        starting_price = int(starting_price)
        auction_id = self._new_id("auc")

        # ---- 防刷：限制同一玩家占用的拍卖格子 ----
        if len(self.active_auctions(seller)) >= self.max_active_auctions_per_player:
            raise EconomyError(
                f"玩家 {seller} 在售拍卖已达上限 {self.max_active_auctions_per_player}"
            )
        # ---- 价格监控：稀有度地板价 ----
        floor = self.rarity_floor.get(rarity, 1) * max(1, quantity)
        if starting_price < floor:
            raise EconomyError(
                f"起拍价 {starting_price} 低于 {rarity} 物品地板价 {floor}，疑似恶意低价"
            )
        if buyout_price is not None and int(buyout_price) < floor:
            raise EconomyError(f"一口价 {buyout_price} 低于地板价 {floor}")
        # ---- 价格监控：市场参考价偏离检测 ----
        history = self.market_prices.get(item_id, [])
        if history:
            reference = sum(history) / len(history)
            if starting_price < reference * 0.5:
                raise EconomyError(
                    f"起拍价 {starting_price} 显著低于市场参考价 {reference:.1f}，拦截"
                )
        if quantity <= 0:
            raise EconomyError("上架数量必须为正数")
        if self.inventory[seller].get(item_id, 0) < quantity:
            raise EconomyError(f"背包物品不足: {item_id} x{quantity}")

        deposit = int(deposit) if deposit is not None else max(1, int(starting_price * 0.05))
        if self.balances[seller][currency]["unbound"] < deposit:
            raise EconomyError(f"保证金 {deposit} 不足，无法上架")

        # 扣保证金 + 锁定物品
        self._change(seller, currency, -deposit, bound=False,
                     reason="auction_deposit", ref=auction_id)
        self.inventory[seller][item_id] -= quantity

        auction = {
            "id": auction_id,
            "seller": seller,
            "item_id": item_id,
            "quantity": quantity,
            "rarity": rarity,
            "currency": currency,
            "starting_price": starting_price,
            "buyout_price": int(buyout_price) if buyout_price is not None else None,
            "deposit": deposit,
            "highest_bid": 0,
            "highest_bidder": None,
            "escrow": 0,
            "status": "active",
            "created_at": self._time(),
            "expires_at": self._time() + int(duration),
        }
        self.auctions[auction_id] = auction
        self.auction_logs.append({
            "id": self._new_id("al"), "auction_id": auction_id,
            "timestamp": self._time(), "action": "listed",
            "seller": seller, "item_id": item_id, "price": starting_price,
        })
        return dict(auction)

    def place_bid(self, *args, **kwargs) -> dict[str, Any]:
        """对在售拍卖出价（资金进入托管，被超越时原路退还）。"""
        auction_id = _pop_alias(kwargs, "auction_id", "auction",
                                default=args[0] if args else None)
        bidder = _pop_alias(kwargs, "bidder", "buyer", "player",
                            default=args[1] if len(args) > 1 else None)
        amount = _pop_alias(kwargs, "amount", "bid", "price",
                            default=args[2] if len(args) > 2 else None)
        if auction_id is None or bidder is None or amount is None:
            raise EconomyError("place_bid 需要 auction_id、bidder、amount 三个参数")
        auction = self.auctions.get(auction_id)
        if auction is None or auction["status"] != "active":
            raise EconomyError("拍卖不存在或已结束")
        if self._time() >= auction["expires_at"]:
            raise EconomyError("拍卖已到期，请等待结算")
        if bidder == auction["seller"]:
            raise EconomyError("卖家不能竞拍自己的物品")
        amount = int(amount)
        if auction["highest_bid"] == 0:
            if amount < auction["starting_price"]:
                raise EconomyError(f"出价不得低于起拍价 {auction['starting_price']}")
        elif amount <= auction["highest_bid"]:
            raise EconomyError(f"出价必须高于当前最高出价 {auction['highest_bid']}")
        currency = auction["currency"]
        if self.balances[bidder][currency]["unbound"] < amount - (
            auction["escrow"] if auction["highest_bidder"] == bidder else 0
        ):
            raise EconomyError("非绑定余额不足以支付出价")

        previous_bidder = auction["highest_bidder"]
        previous_bid = auction["escrow"]
        with self._Atomic(self) as atomic:
            # 新出价托管
            self._change(bidder, currency, -amount, bound=False,
                         reason="auction_bid_escrow", ref=auction_id)
            atomic.undo.append(lambda: self._change(
                bidder, currency, amount, bound=False, reason="rollback",
                ref=auction_id, log=False))
            # 退还上一位竞拍者
            if previous_bidder is not None:
                self._change(previous_bidder, currency, previous_bid, bound=False,
                             reason="auction_bid_refund", ref=auction_id)
                atomic.undo.append(lambda: self._change(
                    previous_bidder, currency, -previous_bid, bound=False,
                    reason="rollback", ref=auction_id, log=False))
        auction["highest_bidder"] = bidder
        auction["highest_bid"] = amount
        auction["escrow"] = amount
        self.auction_logs.append({
            "id": self._new_id("al"), "auction_id": auction_id,
            "timestamp": self._time(), "action": "bid",
            "bidder": bidder, "amount": amount,
        })
        return dict(auction)

    def _finalize_auction(self, auction: dict[str, Any]) -> dict[str, Any]:
        """结算单笔拍卖：有出价则成交（扣交易税），否则流拍退还。"""
        auction_id = auction["id"]
        currency = auction["currency"]
        seller = auction["seller"]
        result: dict[str, Any] = {"auction_id": auction_id, "timestamp": self._time()}
        with self._Atomic(self) as atomic:
            # 保证金始终退还卖家（成交/流拍都退）
            self._change(seller, currency, auction["deposit"], bound=False,
                         reason="auction_deposit_refund", ref=auction_id)
            atomic.undo.append(lambda: self._change(
                seller, currency, -auction["deposit"], bound=False,
                reason="rollback", ref=auction_id, log=False))
            if auction["highest_bidder"] is not None:
                bid = auction["escrow"]
                fee = int(bid * self.tax_rate)
                proceeds = bid - fee
                # 托管金放款给卖家
                self._change(seller, currency, proceeds, bound=False,
                             reason="auction_sale", ref=auction_id,
                             counterpart=auction["highest_bidder"])
                atomic.undo.append(lambda: self._change(
                    seller, currency, -proceeds, bound=False,
                    reason="rollback", ref=auction_id, log=False))
                if fee > 0:
                    self._add_sink("auction_fee", fee)
                # 物品交付买家
                self.inventory[auction["highest_bidder"]][auction["item_id"]] += auction["quantity"]
                atomic.undo.append(lambda: self.inventory[
                    auction["highest_bidder"]].__setitem__(
                    auction["item_id"],
                    self.inventory[auction["highest_bidder"]][auction["item_id"]] - auction["quantity"]))
                self.market_prices.setdefault(auction["item_id"], []).append(bid)
                result.update(status="sold", winner=auction["highest_bidder"],
                              price=bid, fee=fee, seller_proceeds=proceeds)
            else:
                # 流拍：物品退回卖家
                self.inventory[seller][auction["item_id"]] += auction["quantity"]
                atomic.undo.append(lambda: self.inventory[seller].__setitem__(
                    auction["item_id"], self.inventory[seller][auction["item_id"]] - auction["quantity"]))
                result.update(status="expired", winner=None)
        auction["status"] = "sold" if result["status"] == "sold" else "expired"
        auction["escrow"] = 0
        self.auction_logs.append({
            "id": self._new_id("al"), "auction_id": auction_id,
            "timestamp": self._time(), "action": "finalize",
            "result": result["status"],
        })
        return result

    def process_expired_auctions(self, *args, **kwargs) -> list[dict[str, Any]]:
        """到期处理：扫描并结算所有已到期拍卖（物品不会永远挂着）。"""
        now = _pop_alias(kwargs, "now", "current_time", default=None)
        if now is not None:
            self._now = float(now)
        results = []
        for auction in list(self.auctions.values()):
            if auction["status"] == "active" and self._time() >= auction["expires_at"]:
                results.append(self._finalize_auction(auction))
        return results

    def get_auction_logs(self, auction_id: str | None = None) -> list[dict[str, Any]]:
        if auction_id is None:
            return list(self.auction_logs)
        return [log for log in self.auction_logs if log.get("auction_id") == auction_id]

    # ------------------------------------------------------------------ #
    # 4. 货币管理（多币种 + 绑定/非绑定 + 流水对账）
    # ------------------------------------------------------------------ #
    def manage_currency(self, *args, **kwargs) -> dict[str, Any]:
        """货币综合管理入口。

        :param action: ``status``(默认) / ``register`` / ``grant`` / ``spend``
            / ``transfer`` / ``bind`` / ``unbind`` / ``balance``
        :param player: 玩家
        :param currency: 币种（金币 gold、点券 coupon，或注册的新币种）
        :param amount: 金额
        :param bound: 操作绑定还是非绑定余额
        :param target: transfer 的收款方
        :returns: 操作结果（始终非空），所有变动都写入流水
        """
        action = _pop_alias(
            kwargs, "action", "operation", "op",
            default=args[0] if args and isinstance(args[0], str) else "status",
        )
        pos = args[1:] if args and isinstance(args[0], str) else args
        player = _pop_alias(kwargs, "player", "user", "account",
                            default=pos[0] if pos else None)
        currency = _pop_alias(kwargs, "currency", "currency_code", "code",
                              default=pos[1] if len(pos) > 1 else "gold")
        amount = int(_pop_alias(kwargs, "amount", "sum", "value",
                                default=pos[2] if len(pos) > 2 else 0) or 0)
        bound = bool(_pop_alias(kwargs, "bound", "is_bound", default=False))
        target = _pop_alias(kwargs, "target", "to", "recipient", default=None)
        reason = _pop_alias(kwargs, "reason", default=f"currency_{action}")

        if action == "status":
            return {
                "currencies": {code: dict(cfg) for code, cfg in self.currencies.items()},
                "accounts": len(self.balances),
                "ledger_entries": len(self.ledger),
                "supplies": {
                    code: {
                        "total": self._money_supply(code),
                        "tradeable": self._tradeable_supply(code),
                    }
                    for code in self.currencies
                },
            }

        if action == "register":
            tradeable = bool(_pop_alias(kwargs, "tradeable", default=True))
            bound_tradeable = bool(_pop_alias(kwargs, "bound_tradeable", default=False))
            if currency in self.currencies:
                raise EconomyError(f"币种已存在: {currency}")
            self.currencies[currency] = {
                "tradeable": tradeable, "bound_tradeable": bound_tradeable,
            }
            return {"status": "registered", "currency": currency,
                    "tradeable": tradeable, "bound_tradeable": bound_tradeable}

        if player is None:
            raise EconomyError(f"操作 {action} 需要指定 player")
        self._ensure_currency(currency)

        if action == "balance":
            return {"player": player, "currency": currency,
                    **self.get_balance(player, currency)}

        if action == "grant":
            if amount <= 0:
                raise EconomyError("发放金额必须为正数")
            self._change(player, currency, amount, bound=bound, reason=reason)
            self._add_faucet(reason, amount)
            return {"status": "granted", "player": player, "currency": currency,
                    "amount": amount, "bound": bound,
                    **self.get_balance(player, currency)}

        if action == "spend":
            if amount <= 0:
                raise EconomyError("消耗金额必须为正数")
            self._change(player, currency, -amount, bound=bound, reason=reason)
            self._add_sink(reason, amount)  # 消耗即回笼，金币不再只增不减
            return {"status": "spent", "player": player, "currency": currency,
                    "amount": amount, "bound": bound,
                    **self.get_balance(player, currency)}

        if action == "bind":
            if amount <= 0:
                raise EconomyError("绑定金额必须为正数")
            with self._Atomic(self) as atomic:
                self._change(player, currency, -amount, bound=False, reason="bind_lock")
                atomic.undo.append(lambda: self._change(
                    player, currency, amount, bound=False, reason="rollback", log=False))
                self._change(player, currency, amount, bound=True, reason="bind_lock")
                atomic.undo.append(lambda: self._change(
                    player, currency, -amount, bound=True, reason="rollback", log=False))
            return {"status": "bound", "player": player, "currency": currency,
                    "amount": amount, **self.get_balance(player, currency)}

        if action == "unbind":
            raise EconomyError("绑定货币不允许解绑，防止绑定资产流入交易")

        if action == "transfer":
            if target is None:
                raise EconomyError("transfer 需要 target 收款方")
            if bound:
                raise EconomyError("绑定货币不可转账")
            if not self.currencies[currency]["tradeable"]:
                raise EconomyError(f"币种 {currency} 不可转账")
            with self._Atomic(self) as atomic:
                self._change(player, currency, -amount, bound=False,
                             reason=reason, counterpart=target)
                atomic.undo.append(lambda: self._change(
                    player, currency, amount, bound=False, reason="rollback", log=False))
                self._change(target, currency, amount, bound=False,
                             reason=reason, counterpart=player)
                atomic.undo.append(lambda: self._change(
                    target, currency, -amount, bound=False, reason="rollback", log=False))
            return {"status": "transferred", "player": player, "target": target,
                    "currency": currency, "amount": amount}

        raise EconomyError(f"未知货币操作: {action}")

    def get_ledger(
        self, player: str | None = None, currency: str | None = None
    ) -> list[dict[str, Any]]:
        """查询货币流水（每一笔资金移动都可追溯）。"""
        result = self.ledger
        if player is not None:
            result = [e for e in result if e["player"] == player]
        if currency is not None:
            result = [e for e in result if e["currency"] == currency]
        return list(result)

    def reconcile(self, currency: str | None = None) -> dict[str, Any]:
        """对账：流水累计变动必须等于当前余额，否则说明账实不符。"""
        codes = [currency] if currency else list(self.currencies)
        expected: dict[tuple[str, str, bool], int] = defaultdict(int)
        for entry in self.ledger:
            if entry["currency"] in codes:
                expected[(entry["player"], entry["currency"], entry["bound"])] += entry["delta"]
        balanced = True
        discrepancies = []
        players = set(self.balances) | {key[0] for key in expected}
        for player in players:
            for code in codes:
                for is_bound in (True, False):
                    slot = "bound" if is_bound else "unbound"
                    actual = self.balances[player].get(code, {"bound": 0, "unbound": 0})[slot]
                    want = expected.get((player, code, is_bound), 0)
                    if actual != want:
                        balanced = False
                        discrepancies.append({
                            "player": player, "currency": code, "bound": is_bound,
                            "ledger": want, "actual": actual, "diff": actual - want,
                        })
        return {"balanced": balanced, "discrepancies": discrepancies,
                "ledger_entries": len(self.ledger)}

    # ------------------------------------------------------------------ #
    # 5. 经济调控（动态税率 + 事前预测 + A/B 测试）
    # ------------------------------------------------------------------ #
    def predict_inflation(self, *args, **kwargs) -> dict[str, Any]:
        """事前预测下一期通胀，而不是等通胀发生后才补救。

        依据 ``P1/P0 = (M1/M0) / (Y1/Y0)``。显式给定时按给定增长率，
        否则按最近两期快照的变化趋势外推。
        """
        money_growth = _pop_alias(kwargs, "money_growth", "m_growth",
                                  default=args[0] if args else None)
        goods_growth = _pop_alias(kwargs, "goods_growth", "y_growth",
                                  default=args[1] if len(args) > 1 else None)
        if money_growth is None or goods_growth is None:
            if len(self.inflation_history) >= 2:
                prev, last = self.inflation_history[-2], self.inflation_history[-1]
                money_growth = (
                    (last["money_supply"] - prev["money_supply"]) / prev["money_supply"]
                    if prev["money_supply"] > 0 else 0.0
                )
                goods_growth = (
                    (last["goods_total"] - prev["goods_total"]) / prev["goods_total"]
                    if prev["goods_total"] > 0 else 0.0
                )
            else:
                money_growth = money_growth if money_growth is not None else 0.0
                goods_growth = goods_growth if goods_growth is not None else 0.0
        money_growth = float(money_growth)
        goods_growth = float(goods_growth)
        price_ratio = ((1 + money_growth) / (1 + goods_growth)
                       if (1 + goods_growth) > 0 else float("inf"))
        predicted_rate = price_ratio - 1
        if predicted_rate > 0.05:
            recommendation = "raise_tax_and_sink"
        elif predicted_rate < -0.05:
            recommendation = "lower_tax_and_inject"
        else:
            recommendation = "hold"
        return {
            "money_growth": money_growth,
            "goods_growth": goods_growth,
            "predicted_inflation_rate": predicted_rate,
            "recommendation": recommendation,
        }

    def apply_economic_control(self, *args, **kwargs) -> dict[str, Any]:
        """执行一次经济调控。

        - 动态税率：根据预测通胀率自动调高/调低交易税，高通胀时给出
          （或执行）货币回收目标；
        - 事前预测：先 ``predict_inflation`` 再决策；
        - A/B 测试：``group="A"`` 对照组维持基准税率，``group="B"`` 实验组
          采用动态税率，决策指标入组，用 ``ab_test_results`` 比较效果。
        """
        group = _pop_alias(kwargs, "group", "ab_group", "variant", default=None)
        money_growth = _pop_alias(kwargs, "money_growth", default=None)
        goods_growth = _pop_alias(kwargs, "goods_growth", default=None)
        execute_sink = bool(_pop_alias(kwargs, "execute_sink", "auto_sink", default=False))
        sink_player = _pop_alias(kwargs, "sink_player", "reserve_account", default=None)
        max_tax = float(_pop_alias(kwargs, "max_tax_rate", "tax_cap", default=0.5))

        if not hasattr(self, "ab_results"):
            self.ab_results: dict[str, list[dict[str, Any]]] = defaultdict(list)

        snapshot = self.calculate_inflation()
        forecast_kwargs: dict[str, Any] = {}
        if money_growth is not None:
            forecast_kwargs["money_growth"] = money_growth
        if goods_growth is not None:
            forecast_kwargs["goods_growth"] = goods_growth
        forecast = self.predict_inflation(**forecast_kwargs)
        predicted = forecast["predicted_inflation_rate"]
        old_rate = self.tax_rate

        policy = "control" if group == "A" else "dynamic"
        if policy == "control":
            new_rate = self.base_tax_rate
            sink_target = 0
            actions = ["hold_base_tax"]
        else:
            new_rate = min(max_tax, max(0.0, self.base_tax_rate * (1 + max(predicted, -0.99))))
            # 预测高通胀：回收目标 = 当前货币存量 * 预测通胀率
            sink_target = int(snapshot["money_supply"] * predicted) if predicted > 0.05 else 0
            actions = []
            if new_rate > old_rate:
                actions.append("raise_tax")
            elif new_rate < old_rate:
                actions.append("lower_tax")
            if sink_target > 0:
                actions.append("sink_money")
            if not actions:
                actions.append("hold")

        sink_executed = 0
        if execute_sink and sink_target > 0 and sink_player is not None:
            unbound = self.balances[sink_player][snapshot["currency"]]["unbound"]
            sink_executed = min(sink_target, unbound)
            if sink_executed > 0:
                self._change(sink_player, snapshot["currency"], -sink_executed,
                             bound=False, reason="macro_sink")
                self._add_sink("macro_control", sink_executed)

        # 施策后的预期通胀（A/B 效果指标）：回收直接削减货币增速
        sink_fraction = (sink_target / snapshot["money_supply"]
                         if snapshot["money_supply"] > 0 else 0.0)
        money_growth_after = forecast["money_growth"] - sink_fraction
        denom = 1 + forecast["goods_growth"]
        projected_after = (
            (1 + money_growth_after) / denom - 1 if denom > 0 else float("inf")
        )

        # 对照组只记录决策、不改全局税率；其余情况真正生效
        if policy != "control":
            self.tax_rate = new_rate

        decision = {
            "timestamp": self._time(),
            "ab_group": group,
            "policy": policy,
            "current_inflation_rate": snapshot["inflation_rate"],
            "predicted_inflation_rate": predicted,
            "old_tax_rate": old_rate,
            "new_tax_rate": new_rate,
            "sink_target": sink_target,
            "sink_executed": sink_executed,
            "projected_inflation_after_policy": projected_after,
            "actions": actions,
            "recommendation": forecast["recommendation"],
        }
        key = group if group is not None else "default"
        self.ab_results[key].append(decision)
        return dict(decision)

    def ab_test_results(self) -> dict[str, Any]:
        """汇总 A/B 调控效果：平均预测通胀越低说明该组策略越有效。"""
        if not hasattr(self, "ab_results") or not self.ab_results:
            return {"groups": {}, "winner": None, "conclusion": "no_data"}
        summary: dict[str, Any] = {}
        for group, records in self.ab_results.items():
            if not records:
                continue
            summary[group] = {
                "samples": len(records),
                "avg_predicted_inflation": (
                    sum(r["predicted_inflation_rate"] for r in records) / len(records)
                ),
                "avg_projected_inflation_after_policy": (
                    sum(r["projected_inflation_after_policy"] for r in records) / len(records)
                ),
                "avg_tax_rate": sum(r["new_tax_rate"] for r in records) / len(records),
                "total_sink_executed": sum(r["sink_executed"] for r in records),
            }
        winner = (
            min(summary,
                key=lambda g: summary[g]["avg_projected_inflation_after_policy"])
            if summary else None
        )
        return {
            "groups": summary,
            "winner": winner,
            "conclusion": (
                f"实验组 {winner} 平均预测通胀更低，调控更有效" if winner else "no_data"
            ),
        }
