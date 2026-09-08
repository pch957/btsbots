import asyncio
import math
from typing import Dict, Any, List, Optional, Tuple
from btsbots.tradebots import TradeBots

class UniversalArbitrageBot(TradeBots):
    """
    通用 N 环路原子无损套利机器人 (融入资产市场手续费折损与负对数图算法):
    - 自动从 asset info 提取 'f' 字段扣除市场交易手续费 (默认 8/10000)
    - 净汇率转换: r_net = r * (1 - fee_target)
    - 负对数算法寻优: 探测全网负权回路 Sum(-ln(r_net)) < 0
    - 单笔 Transaction 原子化打包全部 N 步吃单，零滑点无单边敞口风险
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_cycle_length: int = 4

    def parse_config(self, config_dict: Dict[str, Any]):
        super().parse_config(config_dict)
        self.max_cycle_length = int(config_dict.get("max_cycle_length", 4))

    def build_net_rate_graph(self) -> Tuple[Dict[str, Dict[str, float]], Dict[str, Dict[str, float]]]:
        """
        构建包含扣除手续费后的净兑换汇率图:
        - raw_graph[u][v] = 盘口原始单价
        - net_graph[u][v] = 扣除 v 资产交易手续费后的净到手汇率
        """
        raw_graph: Dict[str, Dict[str, float]] = {}
        net_graph: Dict[str, Dict[str, float]] = {}

        for (a_s, a_b) in self.markets_config.keys():
            # 卖 a_s 买 a_b -> 寻找对手盘卖 a_b 买 a_s 的买单深度 (我们作为 Taker 吃单)
            opp_orders = [o for o in self.get_market_orders(a_b, a_s) if o.get("u") != self.account_name]
            if opp_orders:
                best_opp_price = float(opp_orders[0].get("p", 0.0))
                if best_opp_price > 0:
                    # 获取买入目标资产 a_b 的市场手续费率 (如 0.0008)
                    fee_b = self.get_asset_market_fee(a_b)
                    
                    # 扣除手续费后的净到手汇率
                    net_rate = best_opp_price * (1.0 - fee_b)
                    
                    raw_graph.setdefault(a_s, {})[a_b] = best_opp_price
                    net_graph.setdefault(a_s, {})[a_b] = net_rate

        return raw_graph, net_graph

    def find_negative_log_cycles(
        self,
        net_graph: Dict[str, Dict[str, float]],
        min_profit_pct: float
    ) -> List[Tuple[List[str], float]]:
        """
        基于负对数图算法，搜索净收益率大于 min_profit_pct 的所有套利闭环
        """
        nodes = list(net_graph.keys())
        opportunities = []

        def dfs(start_node: str, curr_node: str, path: List[str], current_log_weight: float, current_net_rate: float):
            if len(path) > self.max_cycle_length:
                return

            for neighbor, net_rate in net_graph.get(curr_node, {}).items():
                if net_rate <= 0:
                    continue

                weight = -math.log(net_rate)
                next_log_weight = current_log_weight + weight
                next_net_rate = current_net_rate * net_rate

                if neighbor == start_node and len(path) >= 2:
                    # 闭环完成，计算纯净利润率 (已扣除全程所有手续费)
                    net_profit_pct = (next_net_rate - 1.0) * 100.0
                    if net_profit_pct >= (min_profit_pct * 100.0):
                        opportunities.append((path + [start_node], next_net_rate))
                elif neighbor not in path:
                    dfs(start_node, neighbor, path + [neighbor], next_log_weight, next_net_rate)

        for node in nodes:
            dfs(node, node, [node], 0.0, 1.0)

        # 按扣费后的净利润率降序排列
        opportunities.sort(key=lambda x: x[1], reverse=True)
        return opportunities

    async def calculate_strategy(self, block_num: int) -> Optional[List[Dict[str, Any]]]:
        min_profit_pct = float(self.config.get("min_profit_pct", 0.5)) / 100.0
        trade_cny = float(self.config.get("trade_amount_cny", 100.0))

        # 1. 构建考虑手续费扣减的汇率图
        raw_graph, net_graph = self.build_net_rate_graph()
        if not net_graph:
            return None

        # 2. 负对数算法搜索套利环路
        opportunities = self.find_negative_log_cycles(net_graph, min_profit_pct)
        if not opportunities:
            return None

        best_cycle_nodes, best_net_rate = opportunities[0]
        start_asset = best_cycle_nodes[0]
        net_profit_pct = (best_net_rate - 1.0) * 100.0

        p_start = self.get_price(start_asset)
        if p_start <= 0:
            return None

        start_amount = trade_cny / p_start
        free_bal = self.get_free_balance(start_asset)

        if free_bal < start_amount:
            return None

        path_str = " -> ".join(best_cycle_nodes)
        print(f"\n⚡ [发现 {len(best_cycle_nodes)-1} 环套利机会 (已扣手续费)!] 净纯收益率: {net_profit_pct:+.3f}%")
        print(f"   └─ 套利路径: {path_str} | 起始投入: {start_amount:.4f} {start_asset}")

        # 3. 构造精准扣费后的原子 Transaction 意向
        intents = []
        curr_in_amount = start_amount

        for i in range(len(best_cycle_nodes) - 1):
            u = best_cycle_nodes[i]
            v = best_cycle_nodes[i + 1]
            raw_rate = raw_graph[u][v]
            fee_v = self.get_asset_market_fee(v)
            
            # 卖 u 买 v，挂单单价为 1/raw_rate (单位: u/v)
            intents.append({
                "action": "create",
                "sell_asset": u,
                "receive_asset": v,
                "amount": curr_in_amount,
                "price": 1.0 / raw_rate,
                "reason": f"原子套利 Step {i+1}: 卖 {curr_in_amount:.4f} {u} -> 换 {v} (扣费率 {fee_v*100:.3f}%)"
            })
            
            # 精确流转：下一腿的卖出量严格等于本腿扣除手续费后的净到账量
            curr_in_amount = (curr_in_amount * raw_rate) * (1.0 - fee_v)

        return intents if intents else None

async def main():
    bot = UniversalArbitrageBot(config_path="trade_rules.json", strategy_name="arbitrage")
    try:
        await bot.run()
        while True:
            await asyncio.sleep(1)
    except Exception as e:
        print(f"🚨 [UniversalArbitrageBot] 运行退出: {e}")
    finally:
        await bot.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass