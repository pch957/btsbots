import asyncio
import math
from typing import Dict, Any, List, Optional
from collections import deque
from btsbots.tradebots import TradeBots

class BollingerMeanReversionBot(TradeBots):
    """
    布林带通道与波动率均值回归做市策略 (Bollinger Mean Reversion):
    - 实时统计滑动窗口历史价格的中轨 (MA) 与上下轨 (+/- k*std)
    - 当价格超买/超卖偏离均线时逆向挂单，赚取均值回归与波动率收缩收益
    - 结合 OP 77 原生平移订单
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.price_history: Dict[Tuple[str, str], deque] = {}

    async def calculate_strategy(self, block_num: int) -> Optional[List[Dict[str, Any]]]:
        intents = []

        for (a_s, a_b), m_cfg in self.markets_config.items():
            freq = int(m_cfg.get("freq", 2))
            if (block_num % freq) != 0:
                continue

            # 获取盘口最新卖单作为市场当前成交/报价采样
            market_orders = [o for o in self.get_market_orders(a_s, a_b) if o.get("u") != self.account_name]
            if not market_orders:
                continue

            current_p = float(market_orders[0].get("p", 0.0))
            if current_p <= 0:
                continue

            # 维护历史采样窗口
            window_size = int(m_cfg.get("window_size", 20))
            k_std = float(m_cfg.get("k_std", 2.0))
            target_order_cny = float(m_cfg.get("target_order_cny", 50.0))

            p_s = self.get_price(a_s)
            if p_s <= 0:
                continue
            target_amount = target_order_cny / p_s

            history = self.price_history.setdefault((a_s, a_b), deque(maxlen=window_size))
            history.append(current_p)

            if len(history) < max(5, window_size // 2):
                continue

            # 计算布林带均线与标准差
            mean_p = sum(history) / len(history)
            variance = sum((x - mean_p) ** 2 for x in history) / len(history)
            std_dev = math.sqrt(variance)

            upper_band = mean_p + (k_std * std_dev)
            lower_band = max(mean_p * 0.5, mean_p - (k_std * std_dev))

            # 目标卖单价挂在上轨上方（高抛），买单挂在下轨下方（低吸）
            target_sell_price = max(upper_band, mean_p * 1.01)

            # 防吃单保护
            opp_orders = [o for o in self.get_market_orders(a_b, a_s) if o.get("u") != self.account_name]
            if opp_orders:
                best_opp_price = float(opp_orders[0].get("p", 0.0))
                if best_opp_price > 0:
                    taker_boundary = 1.0 / best_opp_price
                    if target_sell_price <= taker_boundary:
                        target_sell_price = taker_boundary * 1.0001

            my_orders = self.get_my_orders(a_s, a_b)
            free_b = self.get_free_balance(a_s)

            if my_orders:
                primary = my_orders[0]
                oid = primary["order_id"]
                curr_p_order = float(primary.get("p", 0.0))
                curr_b_order = float(primary.get("b", 0.0))

                if (block_num - self.order_recent_updates.get(oid, 0)) < 3:
                    continue

                price_dev = abs(curr_p_order - target_sell_price) / target_sell_price
                delta = target_amount - curr_b_order

                if price_dev > 0.003 or abs(delta) > max(1.0, target_amount * 0.02):
                    intents.append({
                        "action": "update",
                        "order_id": oid,
                        "price": target_sell_price,
                        "amount": target_amount,
                        "reason": f"布林带均值回归调优: 上轨 {upper_band:.6f} | 均线 {mean_p:.6f} | 目标价 {target_sell_price:.6f}"
                    })
            else:
                if free_b >= (target_amount * 0.1):
                    intents.append({
                        "action": "create",
                        "sell_asset": a_s,
                        "receive_asset": a_b,
                        "amount": min(free_b, target_amount),
                        "price": target_sell_price,
                        "reason": f"布林带挂单做市: 上轨 {upper_band:.6f} @ {target_sell_price:.6f}"
                    })

        return intents if intents else None

async def main():
    bot = BollingerMeanReversionBot(config_path="trade_rules.json", strategy_name="bollinger")
    try:
        await bot.run()
        while True:
            await asyncio.sleep(1)
    except Exception as e:
        print(f"🚨 [BollingerBot] 运行退出: {e}")
    finally:
        await bot.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass