import os
import json
import asyncio
from typing import Dict, Any, List, Optional, Tuple
from btsbots.tradebots import TradeBots

# AI Agent 决策系统提示词 (强制精简思考，重点输出 intents 数组)
AI_SYSTEM_PROMPT = """你是一个在 BitShares 去中心化交易所 (DEX) 运行的高级自主做市与交易 Agent。
你的核心任务是维护账户所负责市场的双向流动性并盈利。

【🚨 核心指令 (严禁光分析不下单)】:
1. 检查 `market_order_coverage_status`：对于标记为 "MISSING_ORDER" 且余额充足的市场，你【必须在 intents 数组中输出具体的 create 动作对象】！
2. 报价参考：卖出 A 换 B 时报价必须使用 `suggested_maker_price_with_spread`，数量必须严格等于 `exact_target_amount`！
3. 单价物理单位：在 `A/B` 市场中，单价单位是 B per 1 A。

【必须输出的纯 JSON 格式示例】:
{
  "thought": "简要说明（限20字以内）",
  "intents": [
    {
      "action": "create",
      "sell_asset": "BTS",
      "receive_asset": "XBTSX.USDT",
      "amount": 4959.219,
      "price": 0.00156449,
      "reason": "补齐做市卖单"
    }
  ]
}
如果所有负责市场均已有正常挂单且无需调整，返回: {"thought": "挂单完备无需调整", "intents": []}
"""

class AITradingAgent(TradeBots):
    """
    AI 交易 Agent 交易机器人:
    - 无动作时静默心跳: 评估无动作时不输出冗余日志，单行原位平滑刷新心跳，彻底消灭刷屏
    - 门控防刷屏退避机制 (Action Backoff): 避免 AI 漏发动作导致的连续死循环唤醒
    - 动态库存倾斜 (Dynamic Inventory Skew): 越接近持仓上限，买入报价偏离折价越多；未设上限则无上限
    - 严格限定负责市场 (Market Scoping): 彻底屏蔽无关市场 (如 USD/CNY)
    - 启动时自动打印负责市场历史成交统计与法定 CNY 盈亏总览看板 (PnL Dashboard)
    - 自动去重与冗余清理 (每个市场单向强制仅保留 1 个活动挂单)
    - 注入各市场单价物理单位与真实公允价 (USDT/BTS vs BTS/USDT 彻底防混淆)
    - 在单量健康度检测 (PARTIALLY_FILLED_NEEDS_REFILL): 挂单被吃超 50% 强制唤醒补单
    - 零自成交护栏 (Anti Self-Match Guardrail): 强制要求双向价差 (Price_A * Price_B) >= 1.025
    - 注入 target_order_cny 硬熔断截断 (严禁单笔下单超出法定额度，杜绝 All-in 余额)
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ai_provider = "gemini"
        self.ai_model = "gemini-2.0-flash"
        self.api_key = ""
        self.api_base = ""
        self.max_price_deviation_pct = 15.0
        self.min_maker_spread_pct = 1.5
        self.max_holding_cny_map: Dict[str, float] = {}
        
        # 门控与状态追踪缓存
        self.last_consult_block: int = 0
        self.last_fair_prices: Dict[str, float] = {}
        self.last_order_amounts: Dict[str, float] = {}
        self.last_order_ids: set = set()
        self.last_net_worth_cny: float = 0.0
        self.violation_history: List[str] = []
        self.has_printed_initial_pnl: bool = False

    def parse_config(self, config_dict: Dict[str, Any]):
        super().parse_config(config_dict)
        ai_cfg = config_dict.get("ai_agent", {})
        self.ai_provider = ai_cfg.get("provider", "gemini").lower().strip()
        self.ai_model = ai_cfg.get("model", "gemini-2.0-flash")
        
        env_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
        self.api_key = ai_cfg.get("api_key") or env_key
        
        default_base = "https://generativelanguage.googleapis.com/v1beta/openai/" if self.ai_provider == "gemini" else "https://api.openai.com/v1"
        self.api_base = ai_cfg.get("api_base") or default_base
        self.max_price_deviation_pct = float(ai_cfg.get("max_price_deviation_pct", 15.0))
        self.min_maker_spread_pct = float(ai_cfg.get("min_maker_spread_pct", 1.5))

        raw_max_holdings = config_dict.get("max_holding_cny", {})
        self.max_holding_cny_map = {
            k.upper().strip(): float(v) for k, v in raw_max_holdings.items()
        }

    async def run(self):
        await super().run()
        await asyncio.sleep(1.5)
        self.print_pnl_dashboard()

    def print_pnl_dashboard(self):
        """打印醒目的交易盈亏与成交统计看板 (仅限本机器人负责的市场)"""
        pnl = self.calculate_market_pnl()
        self._print_clean("\n" + "=" * 70)
        print(f"📊 [BTSBots AI Agent] 负责市场历史成交统计与盈亏看板 (PnL Dashboard)")
        print("=" * 70)
        print(f" • 负责市场范围: {list(self.markets_config.keys())}")
        print(f" • 撮合成交总笔数: {pnl.get('total_fills_matched', 0)} 笔")
        print(f" • 累计成交换手额: {pnl.get('total_trade_turnover_cny', 0.0):.2f} CNY")
        
        net_pnl = pnl.get("total_net_pnl_cny", 0.0)
        pnl_icon = "🟢" if net_pnl >= 0 else "🔴"
        print(f" • 累计法币净盈亏: {pnl_icon} {net_pnl:+.2f} CNY")
        
        flows = pnl.get("asset_position_flows", {})
        if flows:
            print(" • 各代币累计净流动明细:")
            for ast, detail in flows.items():
                amt = detail.get("net_amount", 0.0)
                val = detail.get("net_value_cny", 0.0)
                icon = "+" if amt >= 0 else ""
                print(f"    └─ {ast:<10}: {icon}{amt:.4f} (公允折合: {icon}{val:.2f} CNY)")
        print("=" * 70 + "\n")
        self.has_printed_initial_pnl = True

    def get_scoped_my_orders(self) -> List[Dict[str, Any]]:
        """仅获取属于当前策略负责的交易对方向的挂单，彻底排除其他无关市场"""
        all_my_orders = self.get_my_orders()
        scoped_orders = []
        for o in all_my_orders:
            a = o.get("a", {})
            s_sym = str(a.get("s", "")).upper().strip()
            r_sym = str(a.get("b", "")).upper().strip()
            if (s_sym, r_sym) in self.markets_config:
                scoped_orders.append(o)
        return scoped_orders

    def get_market_coverage_status(self) -> Tuple[Dict[str, Any], List[Tuple[str, str]], List[Dict[str, Any]], List[Dict[str, Any]]]:
        """诊断配置交易对的双向挂单覆盖情况、动态库存倾斜比率与残缺订单"""
        scoped_my_orders = self.get_scoped_my_orders()
        existing_legs = {}
        for o in scoped_my_orders:
            a = o.get("a", {})
            pair = (a.get("s", "").upper().strip(), a.get("b", "").upper().strip())
            existing_legs.setdefault(pair, []).append(o)

        coverage_report = {}
        missing_pairs = []
        needs_refill_orders = []
        redundant_orders_to_cancel = []

        for (a_s, a_b), m_cfg in self.markets_config.items():
            pair_key = f"{a_s}/{a_b}"
            orders_here = existing_legs.get((a_s, a_b), [])
            free_b = self.get_free_balance(a_s)
            p_s = self.get_price(a_s)
            p_b = self.get_price(a_b)
            
            target_cny = float(m_cfg.get("target_order_cny", self.default_market_params.get("target_order_cny", 50.0)))
            exact_target_amount = (target_cny / p_s) if p_s > 0 else 0.0
            max_exec_amt = min(free_b, exact_target_amount) if free_b > 0 else 0.0

            is_holding_capped = False
            holding_ratio_b = 0.0
            cap_desc = "无上限 (Unlimited)"

            if a_b in self.max_holding_cny_map:
                max_cny_b = self.max_holding_cny_map[a_b]
                current_total_b = self.get_total_balance(a_b)
                current_cny_b = current_total_b * p_b
                holding_ratio_b = min(1.0, current_cny_b / max_cny_b) if max_cny_b > 0 else 0.0
                cap_desc = f"{current_cny_b:.2f} / {max_cny_b:.2f} CNY ({holding_ratio_b*100:.1f}%)"
                if current_cny_b >= max_cny_b:
                    is_holding_capped = True

            if orders_here:
                primary = orders_here[0]
                curr_b = float(primary.get("b", 0.0))
                
                if len(orders_here) > 1:
                    for extra in orders_here[1:]:
                        redundant_orders_to_cancel.append(extra)

                if is_holding_capped:
                    redundant_orders_to_cancel.append(primary)
                    coverage_report[pair_key] = {
                        "status": "HOLDING_CAP_EXCEEDED",
                        "buy_target_holding_status": cap_desc,
                        "action_required": f"资产 [{a_b}] 持仓已达上限，正在自动撤销买单"
                    }
                    continue

                is_half_consumed = (curr_b > 0 and curr_b <= (exact_target_amount * 0.5))
                status_str = "PARTIALLY_FILLED_NEEDS_REFILL" if is_half_consumed else "ACTIVE_ORDER_EXISTS"
                coverage_report[pair_key] = {
                    "status": status_str,
                    "order_id": primary["order_id"],
                    "current_price": float(primary.get("p", 0.0)),
                    "current_remaining_amount": curr_b,
                    "exact_target_amount": round(exact_target_amount, 4),
                    "buy_target_holding_status": cap_desc,
                    "action_required": (f"挂单已被吃超50%，请使用 update 将数量补足至 {exact_target_amount:.4f}！" if is_half_consumed else "正常做市中")
                }
                if is_half_consumed:
                    needs_refill_orders.append(primary)
            else:
                if is_holding_capped:
                    coverage_report[pair_key] = {
                        "status": "HOLDING_CAP_REACHED",
                        "buy_target_holding_status": cap_desc,
                        "action_required": f"资产 [{a_b}] 持仓已达上限，暂停新建买单"
                    }
                    continue

                has_balance = free_b > 1e-4
                status_str = "MISSING_ORDER" if has_balance else "INSUFFICIENT_BALANCE"
                coverage_report[pair_key] = {
                    "status": status_str,
                    "sell_asset": a_s,
                    "target_order_cny": target_cny,
                    "exact_target_amount": round(exact_target_amount, 4),
                    "available_free_balance": round(free_b, 4),
                    "max_executable_amount": round(max_exec_amt, 4),
                    "buy_target_holding_status": cap_desc,
                    "action_required": (f"必须建立挂单: 卖出 {max_exec_amt:.4f} {a_s} 换取 {a_b}" if has_balance else f"可用余额不足 ({free_b:.4f} {a_s})，无法建立卖单")
                }
                if has_balance:
                    missing_pairs.append((a_s, a_b))

        return coverage_report, missing_pairs, needs_refill_orders, redundant_orders_to_cancel

    def should_consult_ai(self, block_num: int) -> Tuple[bool, str]:
        """智能门控判定：评估是否有必要发起 AI 决策请求 (内置防抖冷却)"""
        blocks_since_last = block_num - self.last_consult_block
        if blocks_since_last < 3:
            return False, "处于常规防抖冷却中"

        coverage_report, missing_pairs, needs_refill_orders, redundant_orders = self.get_market_coverage_status()

        if redundant_orders:
            return True, f"检测到负责市场存在 {len(redundant_orders)} 个多余重复挂单，强制唤醒清理"

        if missing_pairs:
            missing_names = [f"{s}/{b}" for s, b in missing_pairs]
            return True, f"检测到有充足余额的市场缺失挂单 {missing_names}，强制唤醒 AI 建立流动性"

        if needs_refill_orders:
            refill_ids = [o["order_id"] for o in needs_refill_orders]
            return True, f"检测到挂单深度被吃超 50% 需补单 (订单 ID: {refill_ids})，唤醒 AI 执行 update"

        current_scoped_orders = self.get_scoped_my_orders()
        current_order_ids = {o["order_id"] for o in current_scoped_orders}

        missing_orders = self.last_order_ids - current_order_ids
        if missing_orders:
            return True, f"检测到负责市场的挂单已撤销或完全成交 (缺失订单 ID: {list(missing_orders)})，唤醒 AI"

        if blocks_since_last >= 600:
            return True, "例行全局巡检 (超时兜底)"

        for (a_s, a_b) in self.markets_config.keys():
            p_s = self.get_price(a_s)
            p_b = self.get_price(a_b)
            if p_s > 0 and p_b > 0:
                fair_p = p_s / p_b
                key = f"{a_s}/{a_b}"
                last_p = self.last_fair_prices.get(key, 0.0)
                if last_p > 0 and (abs(fair_p - last_p) / last_p) >= 0.005:
                    return True, f"市场 [{key}] 公允价格变动 {((fair_p - last_p) / last_p)*100:+.2f}%"

        for o in current_scoped_orders:
            oid = o["order_id"]
            curr_b = float(o.get("b", 0.0))
            last_b = self.last_order_amounts.get(oid, curr_b)
            if last_b > 0 and (abs(last_b - curr_b) / last_b) >= 0.05:
                return True, f"订单 [{oid}] 深度被对手消耗 (在单量 {last_b:.2f} -> {curr_b:.2f})"

        return False, "负责市场平稳无实质变化"

    def build_market_context(self, block_num: int) -> Dict[str, Any]:
        """组装全景上下文"""
        chain_time_str = self.format_time()
        
        assets_summary = {}
        relevant_symbols = set()
        for (a_s, a_b) in self.markets_config.keys():
            relevant_symbols.add(a_s)
            relevant_symbols.add(a_b)

        total_net_worth_cny = 0.0
        for symbol in relevant_symbols:
            free_b = self.get_free_balance(symbol)
            total_b = self.get_total_balance(symbol)
            locked_b = max(0.0, total_b - free_b)
            p_cny = self.get_price(symbol)
            cny_val = total_b * p_cny
            total_net_worth_cny += cny_val

            fee_rate = self.get_asset_market_fee(symbol)
            has_cap = symbol in self.max_holding_cny_map
            max_cny_limit = self.max_holding_cny_map.get(symbol, None)
            holding_ratio = (cny_val / max_cny_limit) if (has_cap and max_cny_limit > 0) else None

            assets_summary[symbol] = {
                "free_balance_usable": round(free_b, 4),
                "locked_in_orders": round(locked_b, 4),
                "total_balance": round(total_b, 4),
                "fiat_price_cny": round(p_cny, 8),
                "market_trading_fee_pct": round(fee_rate * 100.0, 3),
                "total_value_cny": round(cny_val, 2),
                "max_allowed_holding_cny": max_cny_limit if has_cap else "无上限 (Unlimited)",
                "holding_ratio_to_cap": f"{holding_ratio*100:.1f}%" if holding_ratio is not None else "无上限",
                "is_holding_capped": (has_cap and cny_val >= max_cny_limit)
            }

        pnl_stats = self.calculate_market_pnl()
        coverage_report, missing_pairs, needs_refill_orders, redundant_orders = self.get_market_coverage_status()

        scoped_my_orders = self.get_scoped_my_orders()
        my_orders_detail = []
        for o in scoped_my_orders:
            a = o.get("a", {})
            curr_b = float(o.get("b", 0.0))
            price = float(o.get("p", 0.0))
            oid = o.get("order_id")
            s_sym, r_sym = a.get("s"), a.get("b")
            my_orders_detail.append({
                "order_id": oid,
                "market": f"{s_sym}/{r_sym}",
                "sell_asset": s_sym,
                "receive_asset": r_sym,
                "current_remaining_amount": curr_b,
                "price": price,
                "price_unit": f"{r_sym}/{s_sym}",
                "recent_updated": (block_num - self.order_recent_updates.get(oid, 0)) < 3
            })

        markets_snapshot = {}
        for (a_s, a_b), m_cfg in self.markets_config.items():
            pair_name = f"{a_s}/{a_b}"
            p_s = self.get_price(a_s)
            p_b = self.get_price(a_b)
            fair_rate = (p_s / p_b) if (p_s > 0 and p_b > 0) else 0.0

            extra_inventory_spread = 0.0
            if a_b in self.max_holding_cny_map:
                max_cny_b = self.max_holding_cny_map[a_b]
                current_cny_b = self.get_total_balance(a_b) * p_b
                if max_cny_b > 0:
                    u_ratio = min(1.0, current_cny_b / max_cny_b)
                    extra_inventory_spread = (u_ratio ** 2) * 0.05

            spread_pct = (self.min_maker_spread_pct / 100.0) + extra_inventory_spread
            suggested_maker_price = fair_rate * (1.0 + spread_pct)

            fee_s = self.get_asset_market_fee(a_s)
            fee_b = self.get_asset_market_fee(a_b)

            asks_raw = [o for o in self.get_market_orders(a_s, a_b) if o.get("u") != self.account_name][:5]
            asks = [
                {"price": round(float(o.get("p", 0.0)), 8), "volume_sell": round(float(o.get("b", 0.0)), 4)}
                for o in asks_raw
            ]

            bids_raw = [o for o in self.get_market_orders(a_b, a_s) if o.get("u") != self.account_name][:5]
            bids = []
            for o in bids_raw:
                p_opp = float(o.get("p", 0.0))
                if p_opp > 0:
                    bids.append({
                        "price": round(1.0 / p_opp, 8),
                        "volume_buy": round(float(o.get("b", 0.0)) * p_opp, 4)
                    })

            best_ask = asks[0]["price"] if asks else 0.0
            best_bid = bids[0]["price"] if bids else 0.0
            spread = (best_ask - best_bid) if (best_ask > 0 and best_bid > 0) else 0.0
            spread_pct_val = (spread / best_ask * 100.0) if (best_ask > 0) else 0.0

            target_cny = float(m_cfg.get("target_order_cny", self.default_market_params.get("target_order_cny", 50.0)))
            exact_target_amount = (target_cny / p_s) if p_s > 0 else 0.0
            free_b_usable = self.get_free_balance(a_s)
            max_exec_amount = min(free_b_usable, exact_target_amount) if free_b_usable > 0 else 0.0

            markets_snapshot[pair_name] = {
                "market_description": f"卖出 {a_s} 换入 {a_b}",
                "price_unit": f"{a_b} per 1 {a_s}",
                "fair_guidance_price": round(fair_rate, 8),
                "suggested_maker_price_with_spread": round(suggested_maker_price, 8),
                "inventory_extra_skew_pct": round(extra_inventory_spread * 100.0, 2),
                "trading_fees": {f"fee_{a_s}": round(fee_s * 100, 3), f"fee_{a_b}": round(fee_b * 100, 3)},
                "best_ask": best_ask,
                "best_bid": best_bid,
                "market_spread_pct": round(spread_pct_val, 3),
                "target_order_cny": target_cny,
                "exact_target_amount": round(exact_target_amount, 4),
                "max_executable_amount": round(max_exec_amount, 4)
            }

        return {
            "account_name": self.account_name,
            "monitored_markets": list(self.markets_config.keys()),
            "block_num": block_num,
            "timestamp": chain_time_str,
            "total_net_worth_cny": round(total_net_worth_cny, 2),
            "performance_pnl_tracker": pnl_stats,
            "market_order_coverage_status": coverage_report,
            "assets_portfolio": assets_summary,
            "my_active_orders": my_orders_detail,
            "markets_depth": markets_snapshot,
            "recent_guardrail_warnings": self.violation_history[-3:] if self.violation_history else ["无违规记录，风控良好"]
        }

    async def _query_llm_decision(self, market_context: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
        """调用 LLM API"""
        if not self.api_key and self.ai_provider != "ollama":
            return "", []

        prompt_user = f"当前负责市场上下文如下：\n{json.dumps(market_context, ensure_ascii=False, indent=2)}\n\n请评估并输出纯 JSON (若有 MISSING_ORDER 且余额充足，必须在 intents 中输出包含 action: create 的对象)。"

        try:
            import urllib.request
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"
            }
            payload = {
                "model": self.ai_model,
                "messages": [
                    {"role": "system", "content": AI_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt_user}
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.1
            }

            url = f"{self.api_base.rstrip('/')}/chat/completions"
            req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method="POST")

            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=15))
            raw_data = resp.read().decode('utf-8')
            resp_json = json.loads(raw_data)
            
            content_str = resp_json["choices"][0]["message"]["content"]
            parsed_decision = json.loads(content_str)
            
            thought = parsed_decision.get("thought", "")
            intents = parsed_decision.get("intents", [])

            # 🌟 仅在真正产生调仓意向或发生风控纠正时才输出思考日志，无动作时保持绝对静默
            if intents:
                print(f"\n🧠 [AI Agent 思考分析] [区块 #{market_context['block_num']}]")
                print(f"   └─ {thought}")

            return thought, intents

        except Exception as e:
            print(f"\n⚠️ [AI Agent 调用异常]: {e}")
            return "", []

    def _apply_safety_guardrails(self, raw_intents: List[Dict[str, Any]], market_context: Dict[str, Any], ai_thought: str) -> List[Dict[str, Any]]:
        """红线硬风控护栏 (含缺单智能自动补单兜底)"""
        safe_intents = []
        max_dev = self.max_price_deviation_pct / 100.0

        _, missing_pairs, _, redundant_orders = self.get_market_coverage_status()
        for red_o in redundant_orders:
            safe_intents.append({
                "action": "cancel",
                "order_id": red_o["order_id"],
                "reason": "🧹 [框架硬风控] 自动清理本市场多余重复挂单"
            })

        for it in raw_intents:
            action = it.get("action")
            if action not in ["cancel", "update", "create"]:
                self.violation_history.append(f"拦截非法动作类型: {action}")
                continue

            if action == "cancel":
                cached_o = self.get_order_by_id(it["order_id"])
                if cached_o:
                    a = cached_o.get("a", {})
                    pair = (a.get("s", "").upper().strip(), a.get("b", "").upper().strip())
                    if pair in self.markets_config:
                        safe_intents.append(it)
                else:
                    safe_intents.append(it)
                continue

            if action in ["create", "update"]:
                s_asset = it.get("sell_asset")
                r_asset = it.get("receive_asset")

                if (s_asset, r_asset) not in self.markets_config:
                    warn = f"🛑 拦截非负责市场操作: 意向为 {s_asset}/{r_asset}，不在当前监控市场 {list(self.markets_config.keys())} 中"
                    self.violation_history.append(warn)
                    print(f"   🛡️ [风控拦截] {warn}")
                    continue

                price = float(it.get("price", 0.0))
                amount = float(it.get("amount", 0.0))

                if price <= 0 or amount <= 1e-4:
                    self.violation_history.append(f"拦截非正价格/数量: price={price}, amount={amount}")
                    continue

                p_s = self.get_price(s_asset)
                p_r = self.get_price(r_asset)
                free_s = self.get_free_balance(s_asset)

                if r_asset in self.max_holding_cny_map:
                    max_cny_r = self.max_holding_cny_map[r_asset]
                    current_total_r = self.get_total_balance(r_asset)
                    current_cny_r = current_total_r * p_r
                    if current_cny_r >= max_cny_r:
                        warn = f"🛑 资产 [{r_asset}] 当前持仓 ({current_cny_r:.2f} CNY) 已达最大上限 ({max_cny_r:.2f} CNY)，拦截买单"
                        self.violation_history.append(warn)
                        print(f"   🛡️ [风控拦截] {warn}")
                        continue

                pair_cfg = self.markets_config.get((s_asset, r_asset), {})
                target_cny = float(pair_cfg.get("target_order_cny", self.default_market_params.get("target_order_cny", 50.0)))
                max_allowed_amt = (target_cny / p_s) if p_s > 0 else amount

                if amount > (max_allowed_amt * 1.10):
                    old_amt = amount
                    amount = min(max_allowed_amt, free_s)
                    it["amount"] = round(amount, 4)
                    warn = f"严重风控拦截: 下单数量 {old_amt:.2f} {s_asset} (价值 {old_amt*p_s:.2f} CNY) 远超限额 ({target_cny} CNY)，已强行截断为 {amount:.4f} {s_asset}"
                    self.violation_history.append(warn)
                    print(f"   🛡️ [风控截断] {warn}")

                if p_s > 0 and p_r > 0:
                    fair_p = p_s / p_r
                    min_p = fair_p * (1.0 - max_dev)
                    max_p = fair_p * (1.0 + max_dev)

                    if price < min_p or price > max_p:
                        warn = f"严重警告: 报价 {price:.8f} 偏离公允价 {fair_p:.8f} 超过 {self.max_price_deviation_pct}% 红线"
                        self.violation_history.append(warn)
                        print(f"   🛡️ [风控拦截] {warn} -> 自动修正到安全区间")
                        price = max(min_p, min(price, max_p))
                        it["price"] = price

                    min_maker_p = fair_p * (1.0 + (self.min_maker_spread_pct / 100.0))
                    if price < fair_p:
                        if "做市" in it.get("reason", "") or "流动性" in it.get("reason", ""):
                            it["price"] = min_maker_p
                            it["reason"] = f"[风控护栏注入做市价差] 原价 {price:.8f} -> 修正为保底做市价 {min_maker_p:.8f}"
                            print(f"   🛡️ [风控修正] 为做市单注入保底利润价差: {it['reason']}")

            safe_intents.append(it)

        covered_pairs_in_intents = set()
        for it in safe_intents:
            if it.get("action") == "create":
                covered_pairs_in_intents.add((it.get("sell_asset"), it.get("receive_asset")))

        for (m_s, m_r) in missing_pairs:
            if (m_s, m_r) not in covered_pairs_in_intents:
                m_key = f"{m_s}/{m_r}"
                if m_key in ai_thought or m_s in ai_thought:
                    m_snap = market_context.get("markets_depth", {}).get(m_key, {})
                    rec_price = m_snap.get("suggested_maker_price_with_spread", 0.0)
                    rec_amt = m_snap.get("max_executable_amount", 0.0)
                    if rec_price > 0 and rec_amt > 1e-4:
                        print(f"   🪄 [风控自动补齐] 捕获 AI 意图，自动生成缺失挂单: {rec_amt:.4f} {m_s} @ {rec_price:.8f}")
                        safe_intents.append({
                            "action": "create",
                            "sell_asset": m_s,
                            "receive_asset": m_r,
                            "amount": rec_amt,
                            "price": rec_price,
                            "reason": f"智能补齐市场 {m_key} 做市挂单"
                        })

        return safe_intents

    async def calculate_strategy(self, block_num: int) -> Optional[List[Dict[str, Any]]]:
        need_consult, consult_reason = self.should_consult_ai(block_num)
        if not need_consult:
            return None

        market_context = self.build_market_context(block_num)
        thought, raw_intents = await self._query_llm_decision(market_context)
        
        self.last_consult_block = block_num
        for (a_s, a_b) in self.markets_config.keys():
            p_s = self.get_price(a_s)
            p_b = self.get_price(a_b)
            if p_s > 0 and p_b > 0:
                self.last_fair_prices[f"{a_s}/{a_b}"] = p_s / p_b

        current_scoped_orders = self.get_scoped_my_orders()
        self.last_order_ids = {o["order_id"] for o in current_scoped_orders}
        self.last_order_amounts = {
            o["order_id"]: float(o.get("b", 0.0)) for o in current_scoped_orders
        }
        self.last_net_worth_cny = market_context.get("total_net_worth_cny", 0.0)

        safe_intents = self._apply_safety_guardrails(raw_intents or [], market_context, thought)
        
        # 🌟 只有当产生实际订单意向时才打印触发原因，否则保持静默单行脉冲
        if safe_intents:
            print(f"\n📡 [触发 AI 决策] [区块 #{block_num}] 原因: {consult_reason}")

        return safe_intents if safe_intents else None

async def main():
    bot = AITradingAgent(config_path="trade_rules.json", strategy_name="ai_agent")
    try:
        await bot.run()
        while True:
            await asyncio.sleep(1)
    except Exception as e:
        print(f"🚨 [AITradingAgent] 运行退出: {e}")
    finally:
        await bot.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
