"""
pairwise-economy-system - EconomySystem

A working economy market system:

- update_price: supply/demand driven pricing with elasticity and volatility
- calculate_supply_demand: production, consumption and inventory dynamics
- calculate_trade_price: comparative advantage, tariffs and exchange rates
- calculate_inflation: money supply, CPI basket and interest rates
- run_market_simulation: monopoly, competition and business cycles
"""

import math
import random


class PriceResult(float):
    """A price that also exposes the details of how it was computed."""

    def __new__(cls, value, **details):
        obj = super().__new__(cls, value)
        obj._details = dict(details)
        return obj

    def __getitem__(self, key):
        return self._details[key]

    def get(self, key, default=None):
        return self._details.get(key, default)

    def keys(self):
        return self._details.keys()

    def items(self):
        return self._details.items()

    def __getattr__(self, name):
        try:
            return self._details[name]
        except KeyError:
            raise AttributeError(name)


class MarketResult(dict):
    """A dict result that also supports attribute access."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


class EconomySystem:
    # Elasticity by good type: necessities barely react, luxuries swing a lot.
    ELASTICITY = {"necessity": 0.3, "normal": 1.0, "luxury": 2.0}

    def __init__(self, seed=None):
        self.state = {}
        self._rng = random.Random(seed)

    # ------------------------------------------------------------------
    # 1. Pricing: supply/demand + elasticity + volatility
    # ------------------------------------------------------------------
    def update_price(self, base_price=None, supply=None, demand=None,
                     elasticity=None, good_type=None, volatility=None,
                     **kwargs):
        """Update a price from supply and demand.

        - Higher demand than supply pushes the price up (and vice versa).
        - Elasticity scales the reaction: necessities are inelastic,
          luxuries are elastic.
        - A bounded random volatility term keeps prices from being fixed.
        """
        if isinstance(base_price, dict):
            params = base_price
            base_price = params.get("base_price", params.get("price"))
            supply = params.get("supply", supply)
            demand = params.get("demand", demand)
            elasticity = params.get("elasticity", elasticity)
            good_type = params.get("good_type", good_type)
            volatility = params.get("volatility", volatility)

        base_price = float(base_price if base_price is not None else 100.0)
        supply = float(supply if supply is not None else 100.0)
        demand = float(demand if demand is not None else 100.0)

        if elasticity is None:
            elasticity = self.ELASTICITY.get(good_type, 1.0)
        elasticity = float(elasticity)
        if volatility is None:
            volatility = 0.02
        volatility = float(volatility)

        if supply <= 0:
            # Scarcity: no supply at all sends the price sharply upward.
            imbalance = 1.0
        else:
            imbalance = (demand - supply) / supply

        change = elasticity * math.tanh(imbalance)
        noise = self._rng.uniform(-volatility, volatility)
        new_price = max(0.01, base_price * (1.0 + change + noise))

        return PriceResult(
            new_price,
            price=new_price,
            base_price=base_price,
            supply=supply,
            demand=demand,
            elasticity=elasticity,
            volatility=volatility,
            change=new_price - base_price,
        )

    # ------------------------------------------------------------------
    # 2. Supply & demand: production + consumption + inventory
    # ------------------------------------------------------------------
    def calculate_supply_demand(self, inventory=None, production=None,
                                consumption=None, production_capacity=None,
                                demand_growth=None, **kwargs):
        """Advance supply/demand by one tick.

        - Production adds to supply (capped by capacity).
        - Consumption removes from demand-side stock.
        - Inventory carries over; you cannot sell what you do not have.
        """
        if isinstance(inventory, dict):
            params = inventory
            inventory = params.get("inventory")
            production = params.get("production", production)
            consumption = params.get("consumption", consumption)
            production_capacity = params.get("production_capacity",
                                             production_capacity)
            demand_growth = params.get("demand_growth", demand_growth)

        inventory = float(inventory if inventory is not None else 100.0)
        production = float(production if production is not None else 20.0)
        consumption = float(consumption if consumption is not None else 15.0)
        capacity = float(production_capacity
                         if production_capacity is not None else 50.0)
        demand_growth = float(demand_growth
                              if demand_growth is not None else 0.01)

        produced = min(max(production, 0.0), capacity)
        available = inventory + produced
        sold = min(max(consumption, 0.0), available)  # no selling from empty stock
        new_inventory = available - sold

        base_demand = self.state.get("demand", consumption)
        demand = base_demand * (1.0 + demand_growth)
        supply = new_inventory

        self.state["inventory"] = new_inventory
        self.state["supply"] = supply
        self.state["demand"] = demand

        return MarketResult(
            supply=supply,
            demand=demand,
            production=produced,
            consumption=sold,
            inventory=new_inventory,
            shortage=max(0.0, consumption - available),
        )

    # ------------------------------------------------------------------
    # 3. Trade: comparative advantage + tariffs + exchange rates
    # ------------------------------------------------------------------
    def calculate_trade_price(self, domestic_price=None, foreign_price=None,
                              tariff_rate=None, exchange_rate=None,
                              transport_cost=None, **kwargs):
        """Price an imported good.

        - Comparative advantage: cheaper foreign production flows through
          to the trade price instead of every place costing the same.
        - Tariffs raise the import price.
        - Exchange rates convert foreign currency into domestic currency.
        """
        if isinstance(domestic_price, dict):
            params = domestic_price
            domestic_price = params.get("domestic_price")
            foreign_price = params.get("foreign_price")
            tariff_rate = params.get("tariff_rate", tariff_rate)
            exchange_rate = params.get("exchange_rate", exchange_rate)
            transport_cost = params.get("transport_cost", transport_cost)

        domestic_price = float(domestic_price
                               if domestic_price is not None else 100.0)
        foreign_price = float(foreign_price
                              if foreign_price is not None else 80.0)
        tariff_rate = float(tariff_rate if tariff_rate is not None else 0.1)
        exchange_rate = float(exchange_rate
                              if exchange_rate is not None else 1.0)
        transport_cost = float(transport_cost
                               if transport_cost is not None else 0.0)

        import_price = (foreign_price * exchange_rate
                        * (1.0 + tariff_rate) + transport_cost)
        # Comparative advantage: buyers pay the cheaper of local vs import.
        trade_price = min(domestic_price, import_price)
        source = "domestic" if domestic_price <= import_price else "import"

        return PriceResult(
            trade_price,
            price=trade_price,
            domestic_price=domestic_price,
            foreign_price=foreign_price,
            import_price=import_price,
            tariff_rate=tariff_rate,
            exchange_rate=exchange_rate,
            source=source,
        )

    # ------------------------------------------------------------------
    # 4. Inflation: money supply + CPI basket + interest rates
    # ------------------------------------------------------------------
    def calculate_inflation(self, money_supply=None, previous_money_supply=None,
                            basket=None, previous_basket=None,
                            interest_rate=None, **kwargs):
        """Compute the inflation rate.

        - Money supply growth beyond output devalues the currency.
        - CPI is tracked from a basket of goods.
        - The central bank adjusts the interest rate in response.
        """
        if isinstance(money_supply, dict):
            params = money_supply
            money_supply = params.get("money_supply")
            previous_money_supply = params.get("previous_money_supply",
                                               previous_money_supply)
            basket = params.get("basket", basket)
            previous_basket = params.get("previous_basket", previous_basket)
            interest_rate = params.get("interest_rate", interest_rate)

        money_supply = float(money_supply if money_supply is not None else 1100.0)
        previous_money_supply = float(
            previous_money_supply
            if previous_money_supply is not None
            else self.state.get("money_supply", 1000.0))

        if basket is None:
            basket = {"food": 40.0, "housing": 60.0, "energy": 25.0}
        if previous_basket is None:
            previous_basket = self.state.get("basket",
                                             {k: v / 1.02
                                              for k, v in basket.items()})

        cpi = sum(basket.values())
        previous_cpi = sum(previous_basket.values())
        cpi_inflation = (cpi / previous_cpi - 1.0) if previous_cpi > 0 else 0.0

        money_growth = (money_supply / previous_money_supply - 1.0) \
            if previous_money_supply > 0 else 0.0

        # Printing money devalues the currency; CPI captures realized prices.
        inflation = 0.5 * money_growth + 0.5 * cpi_inflation

        # Taylor-style response: the bank raises rates when inflation runs hot.
        base_rate = float(interest_rate if interest_rate is not None else 0.02)
        new_rate = max(0.0, base_rate + 0.5 * (inflation - 0.02))

        self.state["money_supply"] = money_supply
        self.state["basket"] = dict(basket)
        self.state["cpi"] = cpi
        self.state["interest_rate"] = new_rate

        return MarketResult(
            inflation=inflation,
            inflation_rate=inflation,
            cpi=cpi,
            previous_cpi=previous_cpi,
            cpi_inflation=cpi_inflation,
            money_growth=money_growth,
            money_supply=money_supply,
            interest_rate=new_rate,
        )

    # ------------------------------------------------------------------
    # 5. Market simulation: monopoly + competition + business cycles
    # ------------------------------------------------------------------
    def run_market_simulation(self, firms=None, periods=None,
                              entry_threshold=None, **kwargs):
        """Simulate a market over several periods.

        - Monopoly: a dominant firm charges a markup over competitive price.
        - Competition: high profits attract new entrants, eroding markups.
        - Cycles: demand follows a boom/recession cycle instead of
          perpetual prosperity.
        """
        if isinstance(firms, dict) and any(
                isinstance(v, (dict, list)) for v in firms.values()):
            params = firms
            firms = params.get("firms")
            periods = params.get("periods", periods)
            entry_threshold = params.get("entry_threshold", entry_threshold)

        if firms is None:
            firms = {"Alpha": 0.6, "Beta": 0.25, "Gamma": 0.15}
        firms = {name: float(share) for name, share in firms.items()}
        periods = int(periods if periods is not None else 12)
        entry_threshold = float(entry_threshold
                                if entry_threshold is not None else 0.15)

        base_price = 100.0
        history = []
        entrant_id = 0
        initial_hhi = sum(s * s for s in firms.values())

        for t in range(periods):
            # Business cycle: demand oscillates between boom and recession.
            cycle = math.sin(2.0 * math.pi * t / max(periods, 1))
            demand_index = 1.0 + 0.3 * cycle

            # Monopoly power: Herfindahl index -> markup over cost.
            hhi = sum(s * s for s in firms.values())
            markup = 0.5 * hhi
            price = base_price * (1.0 + markup) * (1.0 + 0.1 * cycle)

            profit_margin = markup * demand_index

            # Competition: fat margins invite new entrants.
            if profit_margin > entry_threshold:
                entrant_id += 1
                firms[f"Entrant{entrant_id}"] = 0.05
                # Incumbents lose share to the newcomer.
                for name in firms:
                    if not name.startswith("Entrant"):
                        firms[name] *= 0.97

            total = sum(firms.values())
            firms = {name: s / total for name, s in firms.items()}

            history.append(MarketResult(
                period=t,
                price=price,
                demand_index=demand_index,
                hhi=hhi,
                markup=markup,
                num_firms=len(firms),
                phase="boom" if cycle >= 0 else "recession",
            ))

        final_hhi = sum(s * s for s in firms.values())
        return MarketResult(
            periods=periods,
            history=history,
            firms=dict(firms),
            num_firms=len(firms),
            final_price=history[-1]["price"],
            initial_hhi=initial_hhi,
            final_hhi=final_hhi,
            monopolized=initial_hhi > 0.25,
            cycle_phases=[h["phase"] for h in history],
        )
