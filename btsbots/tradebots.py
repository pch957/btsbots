import os
import json
import asyncio
import sys
import time
import shutil
import datetime
from typing import Optional, Dict, Any, List, Tuple
from btsbots.btsbots import BTSBots

MAX_SUBSCRIPTIONS_LIMIT: int = 50

class TradeBots(BTSBots):
    """
    TradeBots: 交易机器人统一框架基类
    - 自动双向市场展开与全双向成交记录订阅 (fillOrder)
    - 实时统计各市场多资产总成交量与法定 CNY 盈亏 (PnL)
    - 统一参数命名: 下单额度全盘采用法定 CNY 价值 (target_order_cny)
    - 终端 ANSI 强力原位刷新心跳 (\r\x1b[2K)，彻底杜绝刷屏
    - 自动时间戳日志注入: 所有交易日志统一附带 [时间 | 区块号]
    - 相对资产锚定定价支持 (如 "CNY": [16.0, "BTS"])
    - 区块流心跳驱动 (chainBlockHeadStream)
    - 本地订单乐观缓存 (Optimistic Update) + 3 区块提交冷却防重入
    """
    def __init__(
        self,
        db_path: str = "bots.sqlite",
        config_path: str = "trade_rules.json",
        strategy_name: Optional[str] = None
    ):
        super().__init__(db_path)
        self.config_path = config_path
        self.strategy_name = strategy_name
        self.max_subscriptions = MAX_SUBSCRIPTIONS_LIMIT
        self.current_subscription_count = 0
        self.last_config_mtime: float = 0.0
        self.config: Dict[str, Any] = {}
        
        self.markets_config: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.custom_price_map: Dict[str, Any] = {}
        self.default_market_params: Dict[str, Any] = {}
        self.asset_fee_cache: Dict[str, float] = {}
        
        # 节拍、同步与控制
        self.block_delay: float = 1.0
        self.keep_bts_fees: float = 20.0
        self.max_sync_delay_sec: float = 30.0
        self.last_processed_block: int = 0
        self.order_recent_updates: Dict[str, int] = {}
        self._strategy_lock = asyncio.Lock()

    def extend_arguments(self, parser):
        super().extend_arguments(parser)
        parser.add_argument(
            "--config",
            dest="config_path",
            type=str,
            default=self.config_path,
            help=f"交易策略 JSON 配置文件路径 (默认: {self.config_path})",
        )
        parser.add_argument(
            "--strategy",
            dest="strategy_name",
            type=str,
            default=self.strategy_name,
            help="指定载入配置文件中的具体策略名称",
        )

    async def run(self):
        args = self._parse_arguments()
        if hasattr(args, "config_path") and args.config_path:
            self.config_path = args.config_path
        if hasattr(args, "strategy_name") and args.strategy_name:
            self.strategy_name = args.strategy_name

        await super().run()
        await self.reload_config(force=True)
        await self._init_trading_subscriptions()
        self._listen_block_stream()

        print(f"\n🚀 [TradeBots] 机器人引擎就绪！策略: [{self.strategy_name}] | 监听账户: [{self.account_name}]")

    async def reload_config(self, force: bool = False) -> bool:
        try:
            if not os.path.exists(self.config_path):
                raise FileNotFoundError(f"未找到策略配置文件: {self.config_path}")

            mtime = os.path.getmtime(self.config_path)
            if not force and (mtime <= self.last_config_mtime):
                return False

            with open(self.config_path, "r", encoding="utf-8") as f:
                raw_json = json.load(f)

            strategies_map = raw_json.get("strategies", {})
            if not self.strategy_name:
                self.strategy_name = next(iter(strategies_map.keys()))
            if self.strategy_name not in strategies_map:
                raise KeyError(f"配置中未找到名为 [{self.strategy_name}] 的策略段")
            
            target_cfg = strategies_map[self.strategy_name]
            self.parse_config(target_cfg)
            self.config = target_cfg
            self.last_config_mtime = mtime
            self._print_clean(f"🔄 [配置更新] 成功载入策略 [{self.strategy_name}]: {self.config.get('description', '')}")
            return True
        except Exception as e:
            self._print_clean(f"❌ [配置异常] 加载失败: {e}")
            return False

    def parse_config(self, config_dict: Dict[str, Any]):
        self.block_delay = float(config_dict.get("block_delay", 1.0))
        self.keep_bts_fees = float(config_dict.get("keep_bts_fees", 20.0))
        self.max_sync_delay_sec = float(config_dict.get("max_sync_delay_sec", 30.0))

        # 自定义锚定价格
        self.custom_price_map = {
            k.upper().strip(): v for k, v in config_dict.get("custom_price", {}).items()
        }

        # 默认参数模板
        self.default_market_params = config_dict.get("default", {
            "freq": 1,
            "maker_spread": 1.5,
            "taker_profit_margin": 1.5,
            "taker_bid_deviation_limit": 2.0,
            "target_order_cny": 50.0,
            "min_depth_cny": 50.0,
            "min_single_order_cny": 10.0,
            "front_run_step": 0.0001
        })

        # 自动双向市场注册与参数继承
        self.markets_config = {}
        markets_raw = config_dict.get("markets", {})

        if isinstance(markets_raw, list):
            for item in markets_raw:
                cfg = dict(self.default_market_params)
                cfg.update(config_dict)
                if isinstance(item, list) and len(item) == 2:
                    a = item[0].upper().strip()
                    b = item[1].upper().strip()
                    self.markets_config[(a, b)] = dict(cfg)
                    self.markets_config[(b, a)] = dict(cfg)
                elif isinstance(item, dict):
                    base = item.get("base", "").upper().strip()
                    quote = item.get("quote", "").upper().strip()
                    if base and quote:
                        cfg.update(item)
                        self.markets_config[(base, quote)] = dict(cfg)
                        self.markets_config[(quote, base)] = dict(cfg)
        elif isinstance(markets_raw, dict):
            for a_s, sub_map in markets_raw.items():
                for a_b, m_cfg in sub_map.items():
                    pair = (a_s.upper().strip(), a_b.upper().strip())
                    cfg = dict(self.default_market_params)
                    cfg.update(config_dict)
                    cfg.update(m_cfg)
                    self.markets_config[pair] = dict(cfg)
                    
                    rev_pair = (pair[1], pair[0])
                    if rev_pair not in self.markets_config:
                        rev_cfg = dict(self.default_market_params)
                        rev_cfg.update(config_dict)
                        self.markets_config[rev_pair] = dict(rev_cfg)

    async def _init_trading_subscriptions(self):
        await self._safe_subscribe("balance", [{"u": self.account_name}])
        await self._safe_subscribe("orderBook", [self.account_name])
        await self._safe_subscribe("price", [])
        # 全局用户成交记录订阅
        await self._safe_subscribe("fillOrder", [{"u": self.account_name}])

        # 双向市场深度与各市场成交记录订阅
        subscribed_pairs = set()
        for (asset_a, asset_b) in self.markets_config.keys():
            await self._safe_subscribe("orderBook", [asset_a, asset_b])
            await self._safe_subscribe("orderBook", [asset_b, asset_a])

    async def _safe_subscribe(self, name: str, params: list):
        if self.current_subscription_count >= self.max_subscriptions:
            return
        await self.subscribe(name, params)
        self.current_subscription_count += 1

    def _listen_block_stream(self):
        prev_on_changed = self.on_data_changed

        def _on_meteor_event(action: str, collection: str, doc_id: str, fields: dict):
            if prev_on_changed:
                prev_on_changed(action, collection, doc_id, fields)

            if collection == "global_properties" and action in ["added", "changed"]:
                block_num = int(fields.get("B", 0))
                if block_num > 0 and block_num != self.last_processed_block:
                    self.last_processed_block = block_num
                    asyncio.create_task(self._on_block_beat(block_num))

        self.on_data_changed = _on_meteor_event

    def is_chain_synced(self) -> Tuple[bool, float, float]:
        try:
            chain_ts = self._get_chain_time()
            now_ts = time.time()
            delay = abs(now_ts - chain_ts)
            return (delay <= self.max_sync_delay_sec), delay, chain_ts
        except Exception:
            return False, 999.0, time.time()

    def format_time(self, ts: Optional[float] = None) -> str:
        t = ts if ts is not None else time.time()
        return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")

    async def _on_block_beat(self, block_num: int):
        if self._strategy_lock.locked():
            return

        async with self._strategy_lock:
            try:
                synced, delay_sec, chain_ts = self.is_chain_synced()
                chain_time_str = self.format_time(chain_ts)

                if not synced:
                    self.print_heartbeat(
                        f"⚠️ [警告] 时钟失步 | 区块 #{block_num} ({chain_time_str}) | 延迟 {delay_sec:.1f}s > {self.max_sync_delay_sec}s | 暂停调仓"
                    )
                    return

                if self.block_delay > 0:
                    await asyncio.sleep(self.block_delay)

                await self.reload_config(force=False)
                intents = await self.calculate_strategy(block_num)

                if intents:
                    await self.apply_order_intents(intents, block_num)
                else:
                    self.render_pulse_heartbeat(block_num, chain_time_str, delay_sec)
            except Exception as err:
                self._print_clean(f"🚨 [策略执行异常] 区块 #{block_num}: {err}")

    # ==========================
    # 资产市场手续费与价格计算
    # ==========================

    def get_asset_market_fee(self, asset_symbol: str) -> float:
        symbol = asset_symbol.upper().strip()
        if symbol in self.asset_fee_cache:
            return self.asset_fee_cache[symbol]

        fee_rate = 8.0 / 10000.0  # 默认万分之八

        try:
            info = self._get_asset_local(symbol)
            if info and "f" in info and info["f"] is not None:
                raw_f = float(info["f"])
                fee_rate = (raw_f / 10000.0) if raw_f >= 1.0 else raw_f
        except Exception:
            pass

        self.asset_fee_cache[symbol] = fee_rate
        return fee_rate

    def _get_raw_price(self, asset_symbol: str) -> float:
        price_coll = self.collections.get("price", {})
        for doc in price_coll.values():
            if str(doc.get("a")).upper() == asset_symbol.upper():
                return float(doc.get("p", 0.0))
        return 0.0

    def get_price(self, asset_symbol: str) -> float:
        symbol = asset_symbol.upper().strip()
        scale = 1.0
        curr_ref = symbol
        visited = set()

        while curr_ref in self.custom_price_map and curr_ref not in visited:
            visited.add(curr_ref)
            val = self.custom_price_map[curr_ref]
            if isinstance(val, (int, float)):
                return scale * float(val)
            elif isinstance(val, list) and len(val) == 2:
                scale *= float(val[0])
                curr_ref = str(val[1]).upper().strip()
            else:
                break

        return scale * self._get_raw_price(curr_ref)

    # ==========================
    # 成交记录与盈亏统计 (PnL Tracker)
    # ==========================

    def calculate_market_pnl(self, markets: Optional[List[Tuple[str, str]]] = None) -> Dict[str, Any]:
        """
        统计指定市场的总成交笔数、总换手额、各代币净流动及按当前公允价折算的总盈亏
        """
        target_markets = markets or list(self.markets_config.keys())
        target_pairs = set()
        for (a, b) in target_markets:
            target_pairs.add(tuple(sorted([a.upper().strip(), b.upper().strip()])))

        asset_net_flow: Dict[str, float] = {}
        total_trade_volume_cny = 0.0
        fill_count = 0

        fill_coll = self.collections.get("fill_order", {})
        for doc in fill_coll.values():
            users = doc.get("u", [])
            # 判断当前用户是否参与了此笔撮合成交
            if self.account_name not in users:
                continue

            assets = doc.get("a", [])
            balances = doc.get("b", [])
            if len(assets) != 2 or len(balances) != 2:
                continue

            a0, a1 = str(assets[0]).upper().strip(), str(assets[1]).upper().strip()
            pair_key = tuple(sorted([a0, a1]))
            if pair_key not in target_pairs:
                continue

            fill_count += 1
            idx = users.index(self.account_name)
            
            # idx == 0 表示卖出 a0 得到 a1; idx == 1 表示卖出 a1 得到 a0
            if idx == 0:
                sold_asset, sold_amount = a0, float(balances[0])
                bought_asset, bought_amount = a1, float(balances[1])
            else:
                sold_asset, sold_amount = a1, float(balances[1])
                bought_asset, bought_amount = a0, float(balances[0])

            asset_net_flow[sold_asset] = asset_net_flow.get(sold_asset, 0.0) - sold_amount
            asset_net_flow[bought_asset] = asset_net_flow.get(bought_asset, 0.0) + bought_amount

            p_sold_cny = self.get_price(sold_asset)
            total_trade_volume_cny += sold_amount * p_sold_cny

        net_pnl_cny = 0.0
        flows_detail = {}
        for asset, net_amt in asset_net_flow.items():
            p_cny = self.get_price(asset)
            cny_val = net_amt * p_cny
            net_pnl_cny += cny_val
            flows_detail[asset] = {
                "net_amount": round(net_amt, 4),
                "current_price_cny": round(p_cny, 8),
                "net_value_cny": round(cny_val, 2)
            }

        return {
            "total_fills_matched": fill_count,
            "total_trade_turnover_cny": round(total_trade_volume_cny, 2),
            "total_net_pnl_cny": round(net_pnl_cny, 2),
            "asset_position_flows": flows_detail
        }

    # ==========================
    # 终端输出与自适应心跳
    # ==========================

    def _print_clean(self, msg: str):
        sys.stdout.write("\r\x1b[2K")
        print(msg)
        sys.stdout.flush()

    def print_heartbeat(self, msg: str):
        try:
            terminal_width = shutil.get_terminal_size((80, 24)).columns
        except Exception:
            terminal_width = 80

        max_len = max(20, terminal_width - 2)
        if len(msg.encode("utf-8")) > max_len:
            msg = msg[:max_len - 3] + "..."

        sys.stdout.write(f"\r\x1b[2K{msg}")
        sys.stdout.flush()

    def render_pulse_heartbeat(self, block_num: int, chain_time_str: str, delay_sec: float):
        pulse_msg = f"💓 [Pulse] 区块 #{block_num} | {chain_time_str}"
        self.print_heartbeat(pulse_msg)

    def clean_order_id(self, raw_id: Any) -> str:
        s = str(raw_id).replace("~", "").strip()
        return s if s.startswith("1.7.") else f"1.7.{s}"

    def get_order_by_id(self, order_id: str) -> Optional[Dict[str, Any]]:
        clean_target_id = self.clean_order_id(order_id)
        raw_target_num = clean_target_id.replace("1.7.", "")
        order_coll = self.collections.get("order", {})

        for k, doc in order_coll.items():
            if str(k).replace("~", "").replace("1.7.", "") == raw_target_num:
                item = dict(doc)
                item["order_id"] = clean_target_id
                return item
        return None

    def get_free_balance(self, asset_symbol: str) -> float:
        symbol = asset_symbol.upper().strip()
        balance_coll = self.collections.get("balance", {})
        free_amt = 0.0
        for doc in balance_coll.values():
            if doc.get("u") == self.account_name and str(doc.get("a")).upper() == symbol:
                free_amt = float(doc.get("f", 0.0))
                break

        if symbol == "BTS":
            free_amt = max(0.0, free_amt - self.keep_bts_fees)
        return free_amt

    def get_total_balance(self, asset_symbol: str) -> float:
        symbol = asset_symbol.upper().strip()
        balance_coll = self.collections.get("balance", {})
        for doc in balance_coll.values():
            if doc.get("u") == self.account_name and str(doc.get("a")).upper() == symbol:
                return float(doc.get("b", 0.0))
        return 0.0

    def get_my_orders(self, sell_asset: Optional[str] = None, receive_asset: Optional[str] = None) -> List[Dict[str, Any]]:
        order_coll = self.collections.get("order", {})
        my_orders = []
        for doc_id, doc in order_coll.items():
            if doc.get("u") == self.account_name:
                item = dict(doc)
                item["order_id"] = self.clean_order_id(doc_id)
                a = doc.get("a", {})
                if sell_asset and a.get("s") != sell_asset.upper().strip():
                    continue
                if receive_asset and a.get("b") != receive_asset.upper().strip():
                    continue
                my_orders.append(item)
        return my_orders

    def get_market_orders(self, sell_asset: str, receive_asset: str) -> List[Dict[str, Any]]:
        sell_s = sell_asset.upper().strip()
        recv_b = receive_asset.upper().strip()
        order_coll = self.collections.get("order", {})
        matched = []
        for doc_id, doc in order_coll.items():
            a = doc.get("a", {})
            if a.get("s") == sell_s and a.get("b") == recv_b:
                item = dict(doc)
                item["order_id"] = self.clean_order_id(doc_id)
                matched.append(item)
        matched.sort(key=lambda x: float(x.get("p", 0.0)))
        return matched

    # ==========================
    # 核心订单意向执行接口
    # ==========================

    async def apply_order_intents(self, intents: List[Dict[str, Any]], block_num: int = 0) -> Optional[int]:
        if not intents:
            return None

        raw_ops = []
        optimistic_updates = []
        time_tag = f"[{self.format_time()} | 区块 #{block_num}]"

        self._print_clean(f"\n==================================================")
        print(f"📋 [订单解析引擎] {time_tag} 处理 {len(intents)} 个策略意向...")
        print(f"==================================================")

        for intent in intents:
            action = intent.get("action")
            reason = intent.get("reason", "策略调仓")

            if action == "cancel":
                oid = self.clean_order_id(intent["order_id"])
                print(f" ❌ [取消订单] {time_tag} ID: {oid} | 原因: {reason}")
                raw_ops.append({
                    "type": "limit_order_cancel",
                    "params": {"order_id": oid}
                })
                optimistic_updates.append(("cancel", oid, None))

            elif action == "update":
                oid = self.clean_order_id(intent["order_id"])
                cached_order = self.get_order_by_id(oid)
                if not cached_order:
                    print(f" ⚠️ [跳过更新] {time_tag} 未在本地缓存中找到订单 {oid}，可能已成交或撤销")
                    continue

                sell_ast = intent.get("sell_asset") or cached_order.get("a", {}).get("s")
                recv_ast = intent.get("receive_asset") or cached_order.get("a", {}).get("b")
                curr_b = float(cached_order.get("b", 0.0))
                
                new_price = float(intent.get("price", cached_order.get("p", 0.0)))
                target_amount = float(intent.get("amount", curr_b))

                if target_amount <= 1e-6:
                    print(f" ❌ [转为撤单] {time_tag} ID: {oid} 目标挂单量归零")
                    raw_ops.append({
                        "type": "limit_order_cancel",
                        "params": {"order_id": oid}
                    })
                    optimistic_updates.append(("cancel", oid, None))
                    continue

                delta_val = target_amount - curr_b
                delta_amt = delta_val if abs(delta_val) >= 1e-4 else None

                print(f" 📝 [更新订单 (OP 77)] {time_tag} ID: {oid} | 市场: {sell_ast}/{recv_ast}")
                print(f"    └─ 原因: {reason}")
                print(f"    └─ 目标挂单量: {target_amount:.4f} {sell_ast} | 新单价: {new_price:.8f} {recv_ast}/{sell_ast}")
                if delta_amt is not None:
                    print(f"    └─ 数量调整 Delta: {delta_amt:+.4f} {sell_ast} (原量: {curr_b:.4f})")

                raw_ops.append({
                    "type": "limit_order_update",
                    "params": {
                        "order_id": oid,
                        "sell_asset": sell_ast,
                        "receive_asset": recv_ast,
                        "amount": target_amount,
                        "base_for_sale": curr_b,
                        "price": new_price,
                        "delta_amount_to_sell": delta_amt
                    }
                })
                optimistic_updates.append(("update", oid, {"p": new_price, "b": target_amount}))

            elif action == "create":
                sell_ast = intent["sell_asset"]
                recv_ast = intent["receive_asset"]
                amount = float(intent["amount"])
                price = float(intent["price"])

                if amount <= 1e-6:
                    continue

                print(f" ✨ [新增挂单] {time_tag} 市场: {sell_ast}/{recv_ast} | 数量: {amount:.4f} {sell_ast} | 单价: {price:.8f} {recv_ast}/{sell_ast}")
                print(f"    └─ 原因: {reason}")

                raw_ops.append({
                    "type": "limit_order_create",
                    "params": {
                        "sell_asset": sell_ast,
                        "receive_asset": recv_ast,
                        "amount": amount,
                        "price": price
                    }
                })

        if not raw_ops:
            return None

        block_resp = await self.make_transaction(raw_ops)
        print(f"🎉 [执行成功] {time_tag} 已打包至区块: {block_resp}\n")

        curr_block = block_resp or block_num
        order_coll = self.collections.setdefault("order", {})

        for op_type, oid, val in optimistic_updates:
            self.order_recent_updates[oid] = curr_block
            raw_id = oid.replace("1.7.", "")
            matched_key = next((k for k in order_coll if str(k).replace("~", "").replace("1.7.", "") == raw_id), None)
            
            if op_type == "cancel" and matched_key:
                order_coll.pop(matched_key, None)
            elif op_type == "update" and matched_key and val:
                order_coll[matched_key]["p"] = val["p"]
                order_coll[matched_key]["b"] = val["b"]

        return block_resp

    async def calculate_strategy(self, block_num: int) -> Optional[List[Dict[str, Any]]]:
        return None
