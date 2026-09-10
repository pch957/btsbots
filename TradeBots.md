# 🤖 BTSBots TradeBots 开发者与量化策略指南

`TradeBots` 是专为 BitShares（比特股）DEX 量化交易与做市设计的 Python 自动化交易框架。它深度集成了 Meteor DDP 实时数据流、BitShares 原生高效订单修改操作（OP 77，无需撤单即可直改价格与数量）以及零信任密钥管理。

---

## 🚀 快速上手

### 启动量化与 AI Agent 策略机器人

```bash
# 1. 启动 AI 自主交易 Agent 机器人 (支持 Google AI Studio / OpenAI / DeepSeek / Ollama)
GEMINI_API_KEY="AIzaSy..." uv run trade_bots_ai.py --pass btsbots/my_account --strategy ai_agent

# 2. 启动简单法定做市策略 (SimpleMaker)
uv run trade_bots.py --pass btsbots/my_account --strategy simple

# 3. 启动动态自适应多档网格策略 (DynamicGrid - 震荡收割机)
uv run trade_bots_grid.py --pass btsbots/my_account --strategy grid

# 4. 启动通用 N 环路原子无损套利策略 (UniversalArbitrage - 负对数图算法)
uv run trade_bots_arbitrage.py --pass btsbots/my_account --strategy arbitrage

# 5. 启动布林带均值回归做市策略 (BollingerMeanReversion)
uv run trade_bots_bollinger.py --pass btsbots/my_account --strategy bollinger
```

---

## 🧠 AI 自主交易 Agent (`trade_bots_ai.py`)

`AITradingAgent` 将大语言模型（LLM）与去中心化区块链高频交易引擎深度结合，为 AI 提供 360° 结构化全景市场决策上下文，并由底层的零信任硬风控护栏（Guardrails）确保每一笔链上操作的安全合规。

### 1. 核心架构与决策流

```text
       ┌────────────────────────────────────────────────────────┐
       │               Meteor DDP 实时行情与成交流               │
       └───────────────────────────┬────────────────────────────┘
                                   │
       ┌───────────────────────────▼────────────────────────────┐
       │            360° 全景市场上下文构建器 (Context)           │
       │  • 负责市场资产画像、余额与持仓上限 (Max Holding CNY)  │
       │  • Top 5 盘口深度、微观流动性与单价物理单位 (Price Unit)│
       │  • 历史成交记录与法定 CNY 实时盈亏看板 (PnL Tracker)   │
       │  • 市场双向缺口与在单残缺度诊断 (Coverage & Health)    │
       │  • 动态库存倾斜加价指导 (Dynamic Inventory Skew)       │
       └───────────────────────────┬────────────────────────────┘
                                   │ JSON 提示词
       ┌───────────────────────────▼────────────────────────────┐
       │               主流 LLM 决策推理引擎                    │
       │     (Gemini 2.0 / GPT-4o / DeepSeek / Ollama)          │
       └───────────────────────────┬────────────────────────────┘
                                   │ 输出 JSON 意向 (Intents)
       ┌───────────────────────────▼────────────────────────────┐
       │             🛡️ 零信任硬风控护栏 (Guardrails)            │
       │  • 市场范围锁 (Market Scoping Guard: 彻底隔离无关市场) │
       │  • 法币下单额度硬截断熔断 (target_order_cny Cap)        │
       │  • 最大持仓上限拦截 (max_holding_cny)                  │
       │  • 双向价差保底锁 (Anti Self-Match: 严禁平价自成交)    │
       │  • 冗余重复挂单自动清理撤单                            │
       └───────────────────────────┬────────────────────────────┘
                                   │ 原子打包提交
       ┌───────────────────────────▼────────────────────────────┐
       │         BitShares 区块链原生广播 (OP 77 / OP 1)        │
       └────────────────────────────────────────────────────────┘
```

### 2. AI Agent 五大核心技术优势
- **免费 Google AI Studio 原生支持**：使用 `gemini-2.0-flash` 模型，高吞吐低延迟，免去高昂 API 费用。
- **智能自主门控与静默心跳（Gating & Pulse Heartbeat）**：仅在发生缺单、在单被吃超 50%、价格变动等实质信号时才唤醒 AI；平稳时终端保持单行脉冲心跳，绝不刷屏。
- **动态库存倾斜（Dynamic Inventory Skew）**：越接近设定的资产持仓上限，买入挂单价格向下偏离折价越多；未设上限则视为无上限。
- **多资产法定盈亏看板（PnL Dashboard）**：启动即自动汇总结算负责市场的撮合成交笔数、累计换手额与净盈亏。
- **单价物理单位防混淆**：明确区分 `A/B` 市场单价单位为 `B per 1 A`，彻底避免买卖单数值颠倒错单。

---

## 📊 经典交易策略与数学原理

### 1. 简单法定做市 (`SimpleMaker` / `trade_bots.py`)
- **场景**：日常做市、提供双向流动性。
- **原理**：基于资产的法定公允价格（或自定义锚定比率）计算理论中间价，向上加价 `maker_spread%` 挂单，同时设置严格的 Maker 防吃单保护。

### 2. 动态多档网格策略 (`DynamicGrid` / `trade_bots_grid.py`)
- **场景**：宽幅震荡行情。
- **原理**：自适应盘口中间价 $P_{\text{mid}} = \frac{P_{\text{ask1}} + P_{\text{bid1}}}{2}$ 作为中轴，按等比步长铺设 $N$ 档网格，买卖双向严格受 Maker 边界保护（买单不高过卖一，卖单不低过买一），利用 **OP 77 动态平移网格档位**。

### 3. 通用 $N$ 环路原子无损套利 (`UniversalArbitrage` / `trade_bots_arbitrage.py`)
- **场景**：跨币种环路价差套利（自动发现 2 环、3 环、4 环机会）。
- **负对数图算法优化**：
  $$\prod_{i=1}^k P_i > 1 \iff \sum_{i=1}^k -\ln(P_i) < 0$$
  将各市场的最优兑换汇率转换为有向图边权重 $-\ln(P)$，通过**负权环路探测算法**在毫秒级内自动寻优。
- **原子事务**：将整个闭环的 $N$ 笔操作打包在同一个区块链 Transaction 内原子提交，零滑点零单边风险。

### 4. 布林带均值回归做市 (`BollingerMeanReversion` / `trade_bots_bollinger.py`)
- **场景**：波动率扩张与均值回归行情。
- **原理**：实时统计滑动采样均线（MA）与标准差 $\sigma$，在上轨超买区与下轨超卖区挂单做市，赚取波动率收缩利润。

---

## ⚙️ 配置文件说明 (`trade_rules.json` / `trade_rules-example.json`)

```json
{
  "strategies": {
    "ai_agent": {
      "strategy_name": "AITradingAgent",
      "description": "基于 Google AI Studio / OpenAI 的全自主量化交易 Agent",
      "block_delay": 1.0,
      "keep_bts_fees": 30.0,
      "max_holding_cny": {
        "XBTSX.USDT": 2000.0,
        "CNY": 5000.0,
        "BTS": 10000.0
      },
      "ai_agent": {
        "provider": "gemini",
        "model": "gemini-2.0-flash",
        "api_base": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key": "YOUR_GEMINI_API_KEY",
        "max_price_deviation_pct": 15.0,
        "min_maker_spread_pct": 1.5
      },
      "custom_price": { "CNY": [13.597625854, "BTS"] },
      "default": { "target_order_cny": 50.0 },
      "markets": [["BTS", "CNY"], ["BTS", "XBTSX.USDT"]]
    },
    "simple": {
      "strategy_name": "SimpleMaker",
      "description": "基于公允法定价值 (CNY) 的简单做市策略",
      "block_delay": 1.0,
      "keep_bts_fees": 20.0,
      "custom_price": { "CNY": [16.0, "BTS"] },
      "default": { "freq": 2, "maker_spread": 1.5, "target_order_cny": 50.0 },
      "markets": [["BTS", "CNY"], ["BTS", "USD"]]
    },
    "grid": {
      "strategy_name": "DynamicGrid",
      "description": "多档动态等比网格策略",
      "block_delay": 1.0,
      "keep_bts_fees": 20.0,
      "custom_price": { "CNY": [16.0, "BTS"], "USD": [7.0, "CNY"] },
      "default": {
        "freq": 2,
        "grid_levels": 3,
        "grid_step_pct": 1.2,
        "target_order_cny_per_grid": 30.0
      },
      "markets": [["BTS", "CNY"], ["BTS", "USD"]]
    },
    "arbitrage": {
      "strategy_name": "UniversalArbitrage",
      "description": "通用 N 环路原子无损套利策略",
      "block_delay": 0.5,
      "keep_bts_fees": 30.0,
      "min_profit_pct": 0.5,
      "trade_amount_cny": 100.0,
      "max_cycle_length": 4,
      "markets": [["BTS", "CNY"], ["BTS", "USD"], ["CNY", "USD"]]
    },
    "bollinger": {
      "strategy_name": "BollingerMeanReversion",
      "description": "布林带均值回归做市策略",
      "block_delay": 1.0,
      "keep_bts_fees": 20.0,
      "custom_price": { "CNY": [16.0, "BTS"] },
      "default": { "freq": 2, "window_size": 20, "k_std": 2.0, "target_order_cny": 50.0 },
      "markets": [["BTS", "CNY"], ["BTS", "USD"]]
    }
  }
}
```

---

## 🛠️ 二次开发指南

### 1. 核心数据查询 API

| 接口名称 | 参数 | 返回值 | 说明 |
| :--- | :--- | :--- | :--- |
| `self.get_price(asset)` | `asset: str` | `float` | 获取资产公允定价（支持 `custom_price` 链式锚定换算） |
| `self.get_free_balance(asset)` | `asset: str` | `float` | 获取资产可用余额（若为 BTS 已自动扣除保留的手续费） |
| `self.get_total_balance(asset)` | `asset: str` | `float` | 获取资产总持仓（空闲余额 + 在单资产） |
| `self.get_my_orders(sell, recv)`| `sell, recv: Optional[str]` | `List[dict]` | 获取当前账号在指定方向上的有效挂单（已清洗 order_id） |
| `self.get_market_orders(sell, recv)` | `sell, recv: str` | `List[dict]` | 获取市场上除自身以外的盘口深度订单列表（按单价升序） |
| `self.calculate_market_pnl(markets)` | `markets: Optional[list]` | `dict` | 统计指定市场的成交笔数、换手额及法定净盈亏 |
| `self.get_order_by_id(order_id)` | `order_id: str` | `Optional[dict]` | 根据订单 ID 检索订单完整详情 |
| `self.is_chain_synced()` | 无 | `Tuple[bool, delay, ts]`| 检查区块链时钟是否正常同步（30 秒容差） |

---

### 2. 极简订单意向提交接口 (`apply_order_intents`)

#### ① 取消订单 (`cancel`)
```python
{"action": "cancel", "order_id": "1.7.573627825", "reason": "清理冗余订单"}
```

#### ② 修改订单 (`update` - 原生 OP 77，极简入参)
```python
# 仅需传入 order_id 与期望的目标新值，底层自动计算 Delta 与在单基数
{"action": "update", "order_id": "1.7.573174054", "price": 0.0754, "amount": 1000.0}

# 或仅改价格：
{"action": "update", "order_id": "1.7.573174054", "price": 0.0754}

# 或仅改数量：
{"action": "update", "order_id": "1.7.573174054", "amount": 1000.0}
```

#### ③ 新建挂单 (`create`)
```python
{
    "action": "create",
    "sell_asset": "BTS",
    "receive_asset": "CNY",
    "amount": 500.0,
    "price": 0.0754,
    "reason": "初始流动性挂单"
}
```

---

### 3. 自定义策略完整编写范例

```python
import asyncio
from typing import List, Dict, Any, Optional
from btsbots.tradebots import TradeBots

class MyCustomBot(TradeBots):
    async def calculate_strategy(self, block_num: int) -> Optional[List[Dict[str, Any]]]:
        intents = []
        
        for (a_s, a_b), m_cfg in self.markets_config.items():
            freq = int(m_cfg.get("freq", 1))
            if (block_num % freq) != 0:
                continue

            p_s = self.get_price(a_s)
            p_b = self.get_price(a_b)
            if p_s <= 0 or p_b <= 0:
                continue

            fair_price = p_s / p_b
            spread_pct = float(m_cfg.get("maker_spread", 1.5)) / 100.0
            target_price = fair_price * (1.0 + spread_pct)

            target_cny = float(m_cfg.get("target_order_cny", 50.0))
            target_amount = target_cny / p_s

            my_orders = self.get_my_orders(a_s, a_b)
            if my_orders:
                primary = my_orders[0]
                curr_p = float(primary.get("p", 0.0))
                
                # 偏离超过 0.5% 则极简更新订单
                if abs(curr_p - target_price) / target_price > 0.005:
                    intents.append({
                        "action": "update",
                        "order_id": primary["order_id"],
                        "price": target_price,
                        "amount": target_amount,
                        "reason": "跟随行情调价"
                    })
            else:
                free_b = self.get_free_balance(a_s)
                if free_b >= (target_amount * 0.1):
                    intents.append({
                        "action": "create",
                        "sell_asset": a_s,
                        "receive_asset": a_b,
                        "amount": min(free_b, target_amount),
                        "price": target_price,
                        "reason": "初始做市挂单"
                    })

        return intents if intents else None

async def main():
    bot = MyCustomBot(config_path="trade_rules.json", strategy_name="simple")
    try:
        await bot.run()
        while True:
            await asyncio.sleep(1)
    finally:
        await bot.close()

if __name__ == "__main__":
    asyncio.run(main())
```