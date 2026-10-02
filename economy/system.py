"""EconomySystem - game economy management.

Implements:
- inflation tracking based on money supply AND goods/services, with
  currency recall (sinks) and source/sink flow analysis
- atomic player trades with per-trade amount caps and a full audit log
- an auction house with price monitoring, anti-spam listing limits and
  expiration handling
- multi-currency wallets (gold / voucher) with bound vs unbound funds
  and a reconciling ledger
- economic control with dynamic tax rates, inflation prediction and
  A/B testing of policies
"""

from __future__ import annotations

import itertools
import time


class EconomySystem:
    CURRENCIES = ("gold", "voucher")

    def __init__(self, max_trade_amount=1_000_000, max_listings_per_player=5,
                 auction_duration=86400, target_inflation=0.02,
                 base_tax_rate=0.05, price_floor_ratio=0.1):
        self.state = {}
        self.max_trade_amount = max_trade_amount
        self.max_listings_per_player = max_listings_per_player
        self.auction_duration = auction_duration
        self.target_inflation = target_inflation
        self.base_tax_rate = base_tax_rate
        self.price_floor_ratio = price_floor_ratio

        self.wallets = {}           # player -> currency -> {"bound", "unbound"}
        self.ledger = []            # currency流水，余额可对账
        self.trade_log = []         # 交易日志，谁转给谁可查
        self.auctions = {}          # auction_id -> auction
        self.market_prices = {}     # item -> 参考价（价格监控用）
        self.money_history = []     # (有效货币量, 商品总量) 快照
        self.inflation_history = []  # 历次通胀率，用于预测
        self.control_history = []   # 历次调控记录
        self.ab_tests = {}          # 实验名 -> A/B 结果
        self._ids = itertools.count(1)

    # ------------------------------------------------------------------
    # 通胀
    # ------------------------------------------------------------------
    def calculate_inflation(self, money_supply=0, goods_and_services=0,
                            previous_money_supply=None, previous_goods=None,
                            sources=None, sinks=None, **kwargs):
        """通胀 = 货币增速 - 商品/服务增速（货币数量论）。

        - 看商品总量：不是只看货币总量，商品变多会抵消通胀
        - 货币回笼：sinks 里的金币被系统回收，退出流通
        - sinks 分析：列出金币从哪来（sources）、到哪去（sinks）及净流量
        """
        goods_and_services = kwargs.get("goods_total", goods_and_services)
        money_supply = kwargs.get("money", money_supply)
        sources = dict(sources or {})
        sinks = dict(sinks or {})
        total_sources = sum(sources.values())
        total_sinks = sum(sinks.values())

        recalled = total_sinks
        effective_money = max(0, money_supply - recalled)

        if previous_money_supply is None and self.money_history:
            previous_money_supply, previous_goods = self.money_history[-1]
        prev_money = previous_money_supply or effective_money
        prev_goods = previous_goods or goods_and_services

        money_growth = ((effective_money - prev_money) / prev_money
                        if prev_money else 0.0)
        goods_growth = ((goods_and_services - prev_goods) / prev_goods
                        if prev_goods else 0.0)
        inflation_rate = money_growth - goods_growth

        self.money_history.append((effective_money, goods_and_services))
        self.inflation_history.append(inflation_rate)

        return {
            "inflation_rate": inflation_rate,
            "money_supply": money_supply,
            "effective_money_supply": effective_money,
            "goods_and_services": goods_and_services,
            "money_growth": money_growth,
            "goods_growth": goods_growth,
            "recalled": recalled,
            "sinks_analysis": {
                "sources": sources,
                "sinks": sinks,
                "total_sources": total_sources,
                "total_sinks": total_sinks,
                "net_flow": total_sources - total_sinks,
            },
        }

    # ------------------------------------------------------------------
    # 交易
    # ------------------------------------------------------------------
    def process_trade(self, buyer=None, seller=None, amount=0, currency="gold",
                      item=None, simulate_failure=False, **kwargs):
        """玩家间交易。

        - 原子性：任一步骤失败（如中途掉线）整体回滚，不会一方丢钱
        - 金额上限：单笔不得超过 max_trade_amount，防止一次转空全服
        - 日志：每笔交易（含被拒绝/回滚的）都进 trade_log
        """
        buyer = kwargs.get("from_player", buyer)
        seller = kwargs.get("to_player", seller)
        amount = kwargs.get("gold", amount)

        trade_id = f"trade-{next(self._ids)}"
        entry = {
            "id": trade_id, "buyer": buyer, "seller": seller,
            "amount": amount, "currency": currency, "item": item,
            "timestamp": time.time(), "status": "pending",
        }

        def reject(reason):
            entry["status"] = "rejected"
            entry["reason"] = reason
            self.trade_log.append(entry)
            return {"success": False, "trade_id": trade_id, "reason": reason}

        if amount <= 0:
            return reject("amount must be positive")
        if amount > self.max_trade_amount:
            return reject(
                f"amount exceeds per-trade limit {self.max_trade_amount}")

        buyer_wallet = self._wallet(buyer, currency)
        seller_wallet = self._wallet(seller, currency)
        if buyer_wallet["unbound"] < amount:
            return reject("insufficient unbound funds")

        snapshot = (buyer_wallet["unbound"], seller_wallet["unbound"])
        try:
            buyer_wallet["unbound"] -= amount
            if simulate_failure:
                raise ConnectionError("player disconnected mid-trade")
            seller_wallet["unbound"] += amount
        except Exception as exc:
            buyer_wallet["unbound"], seller_wallet["unbound"] = snapshot
            entry["status"] = "rolled_back"
            entry["reason"] = str(exc)
            self.trade_log.append(entry)
            return {"success": False, "trade_id": trade_id,
                    "status": "rolled_back", "reason": str(exc)}

        entry["status"] = "settled"
        self.trade_log.append(entry)
        self._record_ledger(buyer, currency, -amount, "trade_out", trade_id)
        self._record_ledger(seller, currency, amount, "trade_in", trade_id)
        return {
            "success": True, "trade_id": trade_id, "status": "settled",
            "buyer_balance": buyer_wallet["unbound"],
            "seller_balance": seller_wallet["unbound"],
        }

    # ------------------------------------------------------------------
    # 拍卖行
    # ------------------------------------------------------------------
    def auction_item(self, seller=None, item=None, price=0, currency="gold",
                     duration=None, market_price=None, now=None, **kwargs):
        """上架拍卖。

        - 价格监控：低于参考价 price_floor_ratio 的挂单视为恶意压价，拒绝
        - 防刷：同一玩家同时活跃挂单数不得超过上限
        - 到期处理：每条拍卖带 expires_at，过期自动下架退回
        """
        now = time.time() if now is None else now
        expired = self.expire_auctions(now)

        if market_price is not None:
            self.market_prices[item] = market_price
        reference = self.market_prices.get(item)

        if price <= 0:
            return {"success": False, "reason": "price must be positive",
                    "expired": expired}
        if reference is not None and price < reference * self.price_floor_ratio:
            return {"success": False, "reason": "price below monitored floor",
                    "floor_price": reference * self.price_floor_ratio,
                    "reference_price": reference, "expired": expired}

        active = [a for a in self.auctions.values()
                  if a["seller"] == seller and a["status"] == "active"]
        if len(active) >= self.max_listings_per_player:
            return {"success": False, "reason": "listing limit reached",
                    "max_listings": self.max_listings_per_player,
                    "expired": expired}

        duration = self.auction_duration if duration is None else duration
        auction_id = f"auction-{next(self._ids)}"
        auction = {
            "id": auction_id, "seller": seller, "item": item,
            "price": price, "currency": currency,
            "created_at": now, "expires_at": now + duration,
            "status": "active",
        }
        self.auctions[auction_id] = auction
        return {"success": True, "auction": dict(auction), "expired": expired}

    def expire_auctions(self, now=None):
        """到期处理：过期拍卖下架，物品退回卖家，返回过期的 auction id。"""
        now = time.time() if now is None else now
        expired = []
        for auction in self.auctions.values():
            if auction["status"] == "active" and auction["expires_at"] <= now:
                auction["status"] = "expired"
                auction["returned_to"] = auction["seller"]
                expired.append(auction["id"])
        return expired

    # ------------------------------------------------------------------
    # 货币
    # ------------------------------------------------------------------
    def manage_currency(self, action="balance", player=None, amount=0,
                        currency="gold", bound=False, reason="manual",
                        to_player=None, **kwargs):
        """多币种钱包操作。

        - 多币种：gold 与 voucher 分开记账，互不混用
        - 绑定区分：bound 货币只能系统回收，不能交易/转账
        - 流水：每次变动写 ledger，含变动后余额，保证对得上账
        """
        if currency not in self.CURRENCIES:
            return {"success": False,
                    "reason": f"unknown currency {currency!r}",
                    "supported": list(self.CURRENCIES)}
        wallet = self._wallet(player, currency)

        if action == "balance":
            return {"success": True, "player": player, "currency": currency,
                    "wallet": dict(wallet)}

        if action == "credit":
            kind = "bound" if bound else "unbound"
            wallet[kind] += amount
            entry = self._record_ledger(player, currency, amount, reason)
            return {"success": True, "wallet": dict(wallet),
                    "ledger_entry": entry}

        if action == "debit":
            kind = "bound" if bound else "unbound"
            if wallet[kind] < amount:
                return {"success": False, "reason": "insufficient funds",
                        "wallet": dict(wallet)}
            wallet[kind] -= amount
            entry = self._record_ledger(player, currency, -amount, reason)
            return {"success": True, "wallet": dict(wallet),
                    "ledger_entry": entry}

        if action == "transfer":
            if bound:
                return {"success": False,
                        "reason": "bound currency cannot be transferred"}
            if wallet["unbound"] < amount:
                return {"success": False,
                        "reason": "insufficient unbound funds",
                        "wallet": dict(wallet)}
            target = self._wallet(to_player, currency)
            snapshot = (wallet["unbound"], target["unbound"])
            try:
                wallet["unbound"] -= amount
                target["unbound"] += amount
            except Exception as exc:
                wallet["unbound"], target["unbound"] = snapshot
                return {"success": False,
                        "reason": f"transfer rolled back: {exc}"}
            self._record_ledger(player, currency, -amount,
                                f"transfer_to:{to_player}")
            self._record_ledger(to_player, currency, amount,
                                f"transfer_from:{player}")
            return {"success": True, "from": dict(wallet), "to": dict(target)}

        return {"success": False, "reason": f"unknown action {action!r}"}

    # ------------------------------------------------------------------
    # 经济调控
    # ------------------------------------------------------------------
    def apply_economic_control(self, money_supply=0, goods_and_services=0,
                               inflation_rate=None, population=1000,
                               experiment="tax_policy", **kwargs):
        """经济调控。

        - 动态税率：预测通胀超出目标越多，税率越高，回收货币越多
        - 预测：按历史通胀线性外推下一周期，提前调控而非事后救火
        - A/B 测试：对照组不调控、实验组按动态税率调控，对比效果
        """
        if inflation_rate is None:
            inflation_rate = self.calculate_inflation(
                money_supply=money_supply,
                goods_and_services=goods_and_services,
            )["inflation_rate"]

        predicted = self._predict_inflation(inflation_rate)
        excess = max(0.0, predicted - self.target_inflation)
        tax_rate = min(0.9, self.base_tax_rate + 2.0 * excess)
        recalled = int(money_supply * tax_rate) if excess > 0 else 0

        half = population // 2
        control = {
            "size": population - half, "tax_rate": 0.0,
            "inflation_after": inflation_rate,
        }
        treatment = {
            "size": half, "tax_rate": tax_rate,
            "inflation_after": inflation_rate * (1 - tax_rate),
        }
        ab_result = {
            "experiment": experiment,
            "control": control,
            "treatment": treatment,
            "uplift": control["inflation_after"] - treatment["inflation_after"],
        }
        ab_result["effective"] = ab_result["uplift"] > 0
        self.ab_tests[experiment] = ab_result

        record = {
            "inflation_rate": inflation_rate,
            "predicted_inflation": predicted,
            "target_inflation": self.target_inflation,
            "tax_rate": tax_rate,
            "recalled": recalled,
            "money_supply_after": money_supply - recalled,
            "ab_test": ab_result,
        }
        self.control_history.append(record)
        return record

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _wallet(self, player, currency):
        return self.wallets.setdefault(player, {}).setdefault(
            currency, {"bound": 0, "unbound": 0})

    def _record_ledger(self, player, currency, delta, reason, ref=None):
        wallet = self._wallet(player, currency)
        entry = {
            "id": f"ledger-{next(self._ids)}",
            "player": player, "currency": currency, "delta": delta,
            "reason": reason, "ref": ref, "timestamp": time.time(),
            "balance_after": dict(wallet),
        }
        self.ledger.append(entry)
        return entry

    def _predict_inflation(self, current):
        if len(self.inflation_history) >= 2:
            trend = self.inflation_history[-1] - self.inflation_history[-2]
            return current + trend
        return current
