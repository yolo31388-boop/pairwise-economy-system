"""
pairwise-economy-system - EconomySystem

A self-consistent economic model:

- Prices respond to supply/demand imbalances, scaled by elasticity
  (necessities are inelastic, luxuries are elastic) plus stochastic
  volatility.
- Supply and demand are driven by production and consumption, tracked
  through inventory so nothing can be sold that was never produced.
- Trade prices reflect comparative advantage (relative production
  costs), tariffs, and exchange rates.
- Inflation follows the money supply (quantity theory), is measured
  through a CPI basket, and feeds a Taylor-rule interest rate.
- Market simulation models monopoly markups (via concentration),
  competitive entry/exit, and business-cycle booms and recessions.
"""

import math
import random


# Default price elasticities by good type. Necessities barely react to
# shortages; luxuries swing hard.
DEFAULT_ELASTICITY = {
    "necessity": 0.3,
    "normal": 1.0,
    "luxury": 1.8,
}

# Taylor-rule coefficients for the central bank's interest-rate response.
TAYLOR_INFLATION_GAP_WEIGHT = 0.5
TAYLOR_OUTPUT_GAP_WEIGHT = 0.5
NEUTRAL_REAL_RATE = 0.02
DEFAULT_INFLATION_TARGET = 0.02

# Market-structure constants.
MONOPOLY_MARKUP_SCALE = 0.5  # max extra markup in a fully monopolized market
ENTRY_PROFIT_THRESHOLD = 0.10  # profit margin that attracts new entrants
EXIT_LOSS_THRESHOLD = -0.05  # margin below which firms leave
CYCLE_PERIOD = 8  # business-cycle length in simulation periods
CYCLE_AMPLITUDE = 0.15  # demand swing over the cycle

MIN_PRICE = 0.01


class EconomySystem:
    def __init__(self, seed=None):
        self.state = {}
        self._rng = random.Random(seed)
        # Per-good market state: price, supply, demand, inventory.
        self.goods = {}
        # Macroeconomic state.
        self.money_supply = 1_000_000.0
        self.interest_rate = 0.03
        self.cpi = 100.0
        self.price_history = {}
        self.inflation_history = []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _good_state(self, good):
        return self.goods.setdefault(
            good,
            {
                "price": 100.0,
                "supply": 100.0,
                "demand": 100.0,
                "inventory": 100.0,
                "good_type": "normal",
            },
        )

    def _elasticity_for(self, good_type, elasticity):
        if elasticity is not None:
            return float(elasticity)
        return DEFAULT_ELASTICITY.get(good_type, DEFAULT_ELASTICITY["normal"])

    # ------------------------------------------------------------------
    # 1. Price formation: supply/demand + elasticity + volatility
    # ------------------------------------------------------------------

    def update_price(
        self,
        good="default",
        supply=None,
        demand=None,
        good_type=None,
        elasticity=None,
        volatility=0.05,
    ):
        """Update a good's price from its supply/demand imbalance.

        - Excess demand pushes the price up, excess supply pushes it down.
        - The response is scaled by the price elasticity of the good:
          necessities (inelastic) move little, luxuries (elastic) move a lot.
        - A bounded random shock adds realistic volatility.
        """
        state = self._good_state(good)
        old_price = state["price"]
        supply = state["supply"] if supply is None else float(supply)
        demand = state["demand"] if demand is None else float(demand)
        good_type = good_type or state["good_type"]
        elast = self._elasticity_for(good_type, elasticity)

        # Bounded imbalance in [-1, 1]: tanh keeps extreme shortages sane.
        scale = max(supply, demand, 1e-9)
        imbalance = (demand - supply) / scale
        pressure = math.tanh(imbalance)

        shock = self._rng.uniform(-volatility, volatility)
        change = elast * pressure + shock
        new_price = max(MIN_PRICE, old_price * (1.0 + change))

        state["price"] = new_price
        state["supply"] = supply
        state["demand"] = demand
        state["good_type"] = good_type
        self.price_history.setdefault(good, []).append(new_price)

        return {
            "good": good,
            "old_price": old_price,
            "new_price": new_price,
            "change_pct": (new_price - old_price) / old_price,
            "elasticity": elast,
            "good_type": good_type,
            "supply": supply,
            "demand": demand,
            "shock": shock,
        }

    # ------------------------------------------------------------------
    # 2. Supply & demand: production + consumption + inventory
    # ------------------------------------------------------------------

    def calculate_supply_demand(
        self,
        good="default",
        production_rate=10.0,
        consumption_rate=10.0,
        price=None,
        reference_price=100.0,
        supply_price_sensitivity=0.5,
        demand_price_sensitivity=0.5,
    ):
        """Advance production/consumption and reconcile through inventory.

        - Producers make more when the price is above the reference price.
        - Consumers buy less when the price is high.
        - Inventory absorbs the gap; sales are capped by what is in stock,
          so a sold-out good cannot keep selling.
        """
        state = self._good_state(good)
        price = state["price"] if price is None else float(price)
        price_ratio = price / max(reference_price, 1e-9)

        produced = production_rate * price_ratio ** supply_price_sensitivity
        wanted = consumption_rate * price_ratio ** (-demand_price_sensitivity)

        state["inventory"] += produced
        # Consumption cannot exceed what is actually on the shelf.
        sold = min(wanted, state["inventory"])
        state["inventory"] -= sold
        unmet_demand = wanted - sold

        state["supply"] = state["inventory"]
        state["demand"] = wanted

        return {
            "good": good,
            "produced": produced,
            "demanded": wanted,
            "sold": sold,
            "unmet_demand": unmet_demand,
            "inventory": state["inventory"],
            "supply": state["supply"],
            "demand": state["demand"],
            "price": price,
        }

    # ------------------------------------------------------------------
    # 3. Trade: comparative advantage + tariffs + exchange rates
    # ------------------------------------------------------------------

    def calculate_trade_price(
        self,
        base_price=100.0,
        exporter_cost=None,
        importer_cost=None,
        tariff_rate=0.0,
        exchange_rate=1.0,
        transport_cost=0.0,
    ):
        """Price an imported good.

        - Comparative advantage: the export price follows the exporter's
          relative production cost, so cheap producers sell cheap.
        - Tariffs raise the landed price by ``tariff_rate``.
        - The exchange rate converts exporter-currency prices into the
          importer's currency (units of importer currency per exporter
          currency), so money is no longer stuck at 1:1.
        """
        exporter_cost = base_price if exporter_cost is None else float(exporter_cost)
        importer_cost = base_price if importer_cost is None else float(importer_cost)

        # Ricardian edge: how much cheaper the exporter is than the importer.
        comparative_advantage = (importer_cost - exporter_cost) / max(
            importer_cost, 1e-9
        )

        export_price = exporter_cost  # in exporter currency
        converted = export_price * exchange_rate  # in importer currency
        tariff = converted * tariff_rate
        trade_price = converted + tariff + transport_cost

        return {
            "trade_price": trade_price,
            "export_price": export_price,
            "converted_price": converted,
            "tariff": tariff,
            "tariff_rate": tariff_rate,
            "exchange_rate": exchange_rate,
            "transport_cost": transport_cost,
            "comparative_advantage": comparative_advantage,
            "worth_importing": trade_price < importer_cost,
        }

    # ------------------------------------------------------------------
    # 4. Inflation: money supply + CPI basket + interest rates
    # ------------------------------------------------------------------

    def calculate_inflation(
        self,
        money_supply=None,
        previous_money_supply=None,
        output_growth=0.0,
        basket=None,
        base_basket=None,
        interest_rate=None,
        inflation_target=DEFAULT_INFLATION_TARGET,
    ):
        """Compute inflation and the central bank's policy response.

        - Quantity theory: printing money faster than output grows
          devalues it (monetary inflation).
        - CPI: a weighted basket of goods measures the realized change in
          the cost of living.
        - Taylor rule: the interest rate moves with the inflation gap.
        """
        previous_money_supply = (
            self.money_supply if previous_money_supply is None else previous_money_supply
        )
        money_supply = (
            previous_money_supply if money_supply is None else float(money_supply)
        )
        money_growth = money_supply / max(previous_money_supply, 1e-9) - 1.0
        monetary_inflation = money_growth - output_growth

        # CPI basket: {name: (weight, current_price)} vs base prices.
        cpi_inflation = 0.0
        if basket:
            base_basket = base_basket or {}
            total_weight = 0.0
            for name, entry in basket.items():
                weight, current_price = entry
                base_price = base_basket.get(name, current_price)
                cpi_inflation += weight * (current_price / max(base_price, 1e-9) - 1.0)
                total_weight += weight
            if total_weight > 0:
                cpi_inflation /= total_weight

        inflation = (monetary_inflation + cpi_inflation) / 2.0 if basket else monetary_inflation

        # Taylor rule: r = neutral + inflation + gaps.
        interest_rate = self.interest_rate if interest_rate is None else interest_rate
        new_rate = max(
            0.0,
            NEUTRAL_REAL_RATE
            + inflation
            + TAYLOR_INFLATION_GAP_WEIGHT * (inflation - inflation_target)
            + TAYLOR_OUTPUT_GAP_WEIGHT * output_growth,
        )

        self.money_supply = money_supply
        self.interest_rate = new_rate
        self.cpi *= 1.0 + inflation
        self.inflation_history.append(inflation)

        return {
            "inflation": inflation,
            "monetary_inflation": monetary_inflation,
            "cpi_inflation": cpi_inflation,
            "cpi": self.cpi,
            "money_supply": money_supply,
            "money_growth": money_growth,
            "interest_rate": new_rate,
            "previous_interest_rate": interest_rate,
        }

    # ------------------------------------------------------------------
    # 5. Market simulation: monopoly + competition + business cycles
    # ------------------------------------------------------------------

    def run_market_simulation(
        self,
        firms=None,
        periods=12,
        base_demand=100.0,
        base_price=100.0,
        cycle_period=CYCLE_PERIOD,
        cycle_amplitude=CYCLE_AMPLITUDE,
        seed=None,
    ):
        """Simulate a market over several periods.

        - Monopoly: concentration (HHI) lets dominant firms charge a
          markup, so one big player does move prices.
        - Competition: high profits attract entrants, losses push firms
          out, so the player count is endogenous.
        - Business cycle: demand rides a sine wave with noise, giving
          booms and recessions instead of permanent prosperity.
        """
        rng = random.Random(seed) if seed is not None else self._rng
        firms = list(firms) if firms else ["firm_1"]
        shares = {name: 1.0 / len(firms) for name in firms}
        entrant_counter = len(firms)

        price = float(base_price)
        history = []

        for period in range(periods):
            # Business cycle: demand booms and busts around the trend.
            phase = 2.0 * math.pi * period / max(cycle_period, 1)
            cycle = cycle_amplitude * math.sin(phase)
            noise = rng.uniform(-0.02, 0.02)
            demand = base_demand * (1.0 + cycle + noise)

            # Monopoly power: HHI in [0, 1]; 1 means a single firm.
            hhi = sum(s * s for s in shares.values())
            markup = 1.0 + MONOPOLY_MARKUP_SCALE * hhi
            price = base_price * markup * (demand / max(base_demand, 1e-9))

            total_revenue = price * demand
            total_cost = base_price * demand * 0.8  # 20% baseline margin
            profit_margin = (total_revenue - total_cost) / max(total_revenue, 1e-9)

            # Competition: entry when incumbents are fat, exit on losses.
            entrants = 0
            exits = 0
            if profit_margin > ENTRY_PROFIT_THRESHOLD and len(shares) < 20:
                entrant_counter += 1
                entrant = f"firm_{entrant_counter}"
                # Entrants dilute every incumbent's share.
                shares = {k: v * (1.0 - 0.1) for k, v in shares.items()}
                shares[entrant] = 0.1
                entrants = 1
            elif profit_margin < EXIT_LOSS_THRESHOLD and len(shares) > 1:
                weakest = min(shares, key=shares.get)
                freed = shares.pop(weakest)
                total = sum(shares.values())
                shares = {k: v / total for k, v in shares.items()}
                exits = 1

            history.append(
                {
                    "period": period,
                    "phase": "boom" if cycle > 0 else "recession",
                    "demand": demand,
                    "price": price,
                    "hhi": hhi,
                    "markup": markup,
                    "num_firms": len(shares),
                    "profit_margin": profit_margin,
                    "entrants": entrants,
                    "exits": exits,
                }
            )

        final_hhi = sum(s * s for s in shares.values())
        return {
            "periods": periods,
            "history": history,
            "final_num_firms": len(shares),
            "final_hhi": final_hhi,
            "final_price": price,
            "market_structure": (
                "monopoly"
                if final_hhi > 0.8
                else "oligopoly"
                if final_hhi > 0.25
                else "competitive"
            ),
            "firms": dict(shares),
        }
