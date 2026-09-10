import asyncio
from typing import List, Dict, Any, Optional
from btsbots.tradebots import TradeBots

class SimpleTradeBot(TradeBots):
    """
    使用 TradeBots 基础框架的简单做市策略机器人 (含 50% 补单检测)
    """
    async def calculate_strategy(self, block_num: int) -> Optional[List[Dict[str, Any]]]:
        intents = []
        total_holdings: Dict[str, float] = {}
        configured_demands: Dict[str, float] = {}
        active_markets = []

        for (a_s, a_b), m_cfg in self.markets_config.items():
            freq = int(m_cfg.get("freq", 1))
            if (block_num % freq) != 0:
                continue

            p_s = self.get_price(a_s)
            p_b = self.get_price(a_b)
            if p_s <= 0 or p_b <= 0:
                continue

            if a_s not in total_holdings:
                free_amt = self.get_free_balance(a_s)
                my_orders_b = sum(float(o.get("b", 0.0)) for o in self.get_my_orders(sell_asset=a_s))
                total_holdings[a_s] = free_amt + my_orders_b

            target_order_cny = float(m_cfg.get("target_order_cny", 50.0))
            demand_amt = target_order_cny / p_s
            configured_demands[a_s] = configured_demands.get(a_s, 0.0) + demand_amt
            active_markets.append((a_s, a_b, m_cfg, p_s, p_b))

        asset_scale: Dict[str, float] = {}
        remaining_free_balances = {a_s: self.get_free_balance(a_s) for a_s in total_holdings}
        for a_s, demand in configured_demands.items():
            holding = total_holdings.get(a_s, 0.0)
            if demand > holding and demand > 0:
                asset_scale[a_s] = max(0.0, holding / demand)
            else:
                asset_scale[a_s] = 1.0

        for (a_s, a_b, m_cfg, p_s, p_b) in active_markets:
            maker_spread_pct = float(m_cfg.get("maker_spread", 1.5)) / 100.0
            scale = asset_scale.get(a_s, 1.0)
            target_order_cny = float(m_cfg.get("target_order_cny", 50.0))
            target_amount_sell = (target_order_cny / p_s) * scale

            fair_price = p_s / p_b
            target_sell_price = fair_price * (1.0 + maker_spread_pct)

            # Maker 防吃单保护
            opp_orders = [o for o in self.get_market_orders(a_b, a_s) if o.get("u") != self.account_name]
            if opp_orders:
                best_opp_price = float(opp_orders[0].get("p", 0.0))
                if best_opp_price > 0:
                    taker_boundary = 1.0 / best_opp_price
                    if target_sell_price <= taker_boundary:
                        target_sell_price = taker_boundary * 1.0001

            my_orders = self.get_my_orders(a_s, a_b)
            curr_avail_free = remaining_free_balances.get(a_s, 0.0)

            if my_orders:
                for extra in my_orders[1:]:
                    intents.append({"action": "cancel", "order_id": extra["order_id"], "reason": "清理冗余订单"})

                primary = my_orders[0]
                oid = primary["order_id"]

                if (block_num - self.order_recent_updates.get(oid, 0)) < 3:
                    continue

                curr_p = float(primary.get("p", 0.0))
                curr_b = float(primary.get("b", 0.0))
                
                if target_amount_sell <= 1e-4:
                    intents.append({
                        "action": "cancel",
                        "order_id": oid,
                        "reason": "目标量归零撤单"
                    })
                    continue

                price_dev = abs(curr_p - target_sell_price) / target_sell_price
                delta = target_amount_sell - curr_b

                if delta > 0:
                    delta = min(delta, curr_avail_free)
                    target_amount_sell = curr_b + delta

                # 🌟 50% 订单被吃补单触发器
                is_half_filled = (curr_b > 0 and (abs(target_amount_sell - curr_b) / target_amount_sell) >= 0.5)
                is_amount_diff_large = abs(delta) > max(1.0, target_amount_sell * 0.01)
                needs_update = (price_dev > 0.003) or is_amount_diff_large or is_half_filled

                if needs_update and target_amount_sell > 1e-4:
                    reason_str = "在单被吃达50%触发补单" if is_half_filled else f"策略调优: 偏离 {price_dev*100:.2f}%"
                    intents.append({
                        "action": "update",
                        "order_id": oid,
                        "price": target_sell_price,
                        "amount": target_amount_sell,
                        "reason": f"{reason_str} (指导价: {fair_price:.8f}), 量: {curr_b:.4f} -> {target_amount_sell:.4f}"
                    })
                    if delta > 0:
                        remaining_free_balances[a_s] = max(0.0, curr_avail_free - delta)
            else:
                actual_amt = min(curr_avail_free, target_amount_sell)
                if actual_amt >= (target_amount_sell * 0.1) and actual_amt > 1e-4:
                    intents.append({
                        "action": "create",
                        "sell_asset": a_s,
                        "receive_asset": a_b,
                        "amount": actual_amt,
                        "price": target_sell_price,
                        "reason": f"建立做市单: {actual_amt:.4f} {a_s} @ {target_sell_price:.8f} (指导价: {fair_price:.8f})"
                    })
                    remaining_free_balances[a_s] = max(0.0, curr_avail_free - actual_amt)

        return intents if intents else None

async def main():
    bot = SimpleTradeBot(config_path="trade_rules.json", strategy_name="simple")
    try:
        await bot.run()
        while True:
            await asyncio.sleep(1)
    except Exception as e:
        print(f"🚨 [TradeBots] 运行退出: {e}")
    finally:
        await bot.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass