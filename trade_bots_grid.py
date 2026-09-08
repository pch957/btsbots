import asyncio
from typing import Dict, Any, List, Optional, Tuple
from btsbots.tradebots import TradeBots

class MultiGridBot(TradeBots):
    """
    自适应动态多档网格机器人 (Dynamic Multi-Grid):
    - 真实盘口中轴锚定: 优先取盘口 (Ask1 + Bid1)/2 作为中轴，无深度时平滑降级到指导价
    - 双向全自动网格: 自动为市场铺设买单 (低吸) 与卖单 (高抛)
    - 智能 Maker 价格保护: 若网格档位与对手盘穿透，自动调高卖价/调低买价并【正常下单】
    - 基于总持仓平滑分配资金，防止 Insufficient Balance
    - 结合 OP 77 动态平移与更新已有网格单
    """
    async def calculate_strategy(self, block_num: int) -> Optional[List[Dict[str, Any]]]:
        # 1. 汇总总持仓 (空闲余额 + 现有订单已占用的数量)
        total_holdings: Dict[str, float] = {}
        configured_demands: Dict[str, float] = {}
        active_legs: List[Tuple[str, str, Dict[str, Any], float, float]] = []

        # 双向展开所有市场 (A->B 与 B->A)
        for (a_s, a_b), m_cfg in self.markets_config.items():
            freq = int(m_cfg.get("freq", 2))
            if (block_num % freq) != 0:
                continue

            p_s = self.get_price(a_s)
            p_b = self.get_price(a_b)
            if p_s <= 0 or p_b <= 0:
                continue

            grid_levels = int(m_cfg.get("grid_levels", 3))
            target_order_cny = float(m_cfg.get("target_order_cny", m_cfg.get("target_order_cny_per_grid", 30.0)))
            total_demand_for_leg = (target_order_cny * grid_levels) / p_s

            if a_s not in total_holdings:
                free_amt = self.get_free_balance(a_s)
                my_orders_b = sum(float(o.get("b", 0.0)) for o in self.get_my_orders(sell_asset=a_s))
                total_holdings[a_s] = free_amt + my_orders_b

            configured_demands[a_s] = configured_demands.get(a_s, 0.0) + total_demand_for_leg
            active_legs.append((a_s, a_b, m_cfg, p_s, p_b))

        # 2. 稳定的总持仓缩放比例
        asset_scale: Dict[str, float] = {}
        remaining_free_balances: Dict[str, float] = {}
        for a_s, demand in configured_demands.items():
            holding = total_holdings.get(a_s, 0.0)
            remaining_free_balances[a_s] = self.get_free_balance(a_s)
            if demand > holding and demand > 0:
                asset_scale[a_s] = max(0.0, holding / demand)
            else:
                asset_scale[a_s] = 1.0

        intents = []

        # 3. 逐个方向执行多档网格铺设
        for (a_s, a_b, m_cfg, p_s, p_b) in active_legs:
            scale = asset_scale.get(a_s, 1.0)
            await self._execute_grid_leg(
                intents, a_s, a_b, p_s, p_b, m_cfg, block_num, scale, remaining_free_balances
            )

        return intents if intents else None

    async def _execute_grid_leg(
        self,
        intents: List[Dict[str, Any]],
        a_s: str,
        a_b: str,
        p_s: float,
        p_b: float,
        config: Dict[str, Any],
        block_num: int,
        scale: float,
        remaining_free_balances: Dict[str, float]
    ):
        grid_levels = int(config.get("grid_levels", 3))
        grid_step_pct = float(config.get("grid_step_pct", 1.2)) / 100.0
        
        target_order_cny = float(config.get("target_order_cny", config.get("target_order_cny_per_grid", 30.0)))
        order_amount_per_grid = (target_order_cny / p_s) * scale
        
        fair_mid_price = p_s / p_b
        curr_avail_free = remaining_free_balances.get(a_s, 0.0)

        # 1. 探测盘口最优价格
        same_orders = [o for o in self.get_market_orders(a_s, a_b) if o.get("u") != self.account_name]
        opp_orders = [o for o in self.get_market_orders(a_b, a_s) if o.get("u") != self.account_name]

        best_ask_price = float(same_orders[0].get("p", 0.0)) if same_orders else 0.0
        best_opp_bid_raw = float(opp_orders[0].get("p", 0.0)) if opp_orders else 0.0
        best_bid_price = (1.0 / best_opp_bid_raw) if best_opp_bid_raw > 0 else 0.0

        # 2. 计算网格基准中轴 (以盘口真实成交/买卖中间价为最高优先级)
        if best_ask_price > 0 and best_bid_price > 0 and best_ask_price > best_bid_price:
            anchor_mid_price = (best_ask_price + best_bid_price) / 2.0
        elif best_ask_price > 0:
            anchor_mid_price = best_ask_price
        elif best_bid_price > 0:
            anchor_mid_price = best_bid_price
        else:
            anchor_mid_price = fair_mid_price

        # 3. 严格 Maker 保护底线 (卖单价必须高于买一)
        taker_boundary = (best_bid_price * 1.0002) if best_bid_price > 0 else 0.0

        # 4. 获取现有订单
        my_orders = self.get_my_orders(a_s, a_b)
        # 按价格从低到高排序
        my_orders.sort(key=lambda x: float(x.get("p", 0.0)))

        # 5. 铺设/调优各档网格
        for level in range(1, grid_levels + 1):
            # 计算理论网格价格
            raw_grid_price = anchor_mid_price * ((1.0 + grid_step_pct) ** level)
            
            # 🛡️ 价格保护：若低于对手盘买一价，自动拉升到保护价之上并【正常下单】
            if raw_grid_price <= taker_boundary:
                grid_price = taker_boundary * (1.0001 ** level)
            else:
                grid_price = raw_grid_price

            order_idx = level - 1
            if order_idx < len(my_orders):
                # 更新已有档位订单
                existing = my_orders[order_idx]
                oid = existing["order_id"]

                if (block_num - self.order_recent_updates.get(oid, 0)) < 3:
                    continue

                curr_p = float(existing.get("p", 0.0))
                curr_b = float(existing.get("b", 0.0))

                if order_amount_per_grid <= 1e-4:
                    intents.append({
                        "action": "cancel",
                        "order_id": oid,
                        "reason": f"网格 #{level} 档资金归零撤单"
                    })
                    continue

                price_dev = abs(curr_p - grid_price) / grid_price
                delta = order_amount_per_grid - curr_b

                if delta > 0:
                    delta = min(delta, curr_avail_free)
                    target_amt = curr_b + delta
                else:
                    target_amt = order_amount_per_grid

                needs_update = (price_dev > 0.003) or (abs(delta) > max(1.0, target_amt * 0.02))

                if needs_update and target_amt > 1e-4:
                    intents.append({
                        "action": "update",
                        "order_id": oid,
                        "price": grid_price,
                        "amount": target_amt,
                        "reason": f"网格 #{level} 档调优: 偏离 {price_dev*100:.2f}% (中轴: {anchor_mid_price:.6f}), 量: {curr_b:.2f} -> {target_amt:.2f}"
                    })
                    if delta > 0:
                        remaining_free_balances[a_s] = max(0.0, curr_avail_free - delta)
            else:
                # 新建网格单
                actual_amt = min(curr_avail_free, order_amount_per_grid)
                if actual_amt >= (order_amount_per_grid * 0.1) and actual_amt > 1e-4:
                    intents.append({
                        "action": "create",
                        "sell_asset": a_s,
                        "receive_asset": a_b,
                        "amount": actual_amt,
                        "price": grid_price,
                        "reason": f"新建网格 #{level} 档: {actual_amt:.4f} {a_s} @ {grid_price:.8f} (中轴: {anchor_mid_price:.6f})"
                    })
                    remaining_free_balances[a_s] = max(0.0, curr_avail_free - actual_amt)

        # 6. 超出网格档位数量的冗余订单自动清理
        if len(my_orders) > grid_levels:
            for redundant in my_orders[grid_levels:]:
                intents.append({
                    "action": "cancel",
                    "order_id": redundant["order_id"],
                    "reason": f"清理超出网格层数的订单 ({redundant['order_id']})"
                })

async def main():
    bot = MultiGridBot(config_path="trade_rules.json", strategy_name="grid")
    try:
        await bot.run()
        while True:
            await asyncio.sleep(1)
    except Exception as e:
        print(f"🚨 [MultiGridBot] 运行退出: {e}")
    finally:
        await bot.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass