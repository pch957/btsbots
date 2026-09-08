# 🤖 BTSBots TradeBots Developer & Quantitative Strategies Guide

`TradeBots` is a modern Python quantitative trading and market-making framework designed specifically for the BitShares DEX. It features deep integration with Meteor DDP real-time streams, native BitShares order update operations (OP 77, modifying price and quantity in-place without cancel-replace cycles), and zero-trust key security.

---

## 🚀 Quick Start

### Launching Quantitative Strategy Bots

```bash
# 1. Run the simple maker strategy
uv run trade_bots.py --pass btsbots/my_account --strategy simple

# 2. Run the dynamic multi-grid strategy
uv run trade_bots_grid.py --pass btsbots/my_account --strategy grid

# 3. Run the universal N-cycle atomic arbitrage strategy
uv run trade_bots_arbitrage.py --pass btsbots/my_account --strategy arbitrage

# 4. Run the Bollinger Bands mean reversion strategy
uv run trade_bots_bollinger.py --pass btsbots/my_account --strategy bollinger
```

---

## 📊 Classic Strategies & Mathematical Principles

### 1. Simple Market Maker (`SimpleMaker` / `trade_bots.py`)
- **Use Case**: Daily liquidity provision and spread capture.
- **Principle**: Calculates fair mid-price based on fiat pricing or peg ratios, applies a `maker_spread%` markup, and enforces strict maker non-taker boundary checks.

### 2. Dynamic Multi-Grid Strategy (`DynamicGrid` / `trade_bots_grid.py`)
- **Use Case**: Ranging and oscillating market conditions.
- **Principle**: Places $N$ grid levels anchored to the real market mid-price $P_{\text{mid}} = \frac{P_{\text{bid1}} + P_{\text{ask1}}}{2}$. Both sides strictly observe Maker protections (buys never cross asks, sells never cross bids) and use **OP 77 to dynamically shift grid levels in-place**.

### 3. Universal $N$-Cycle Atomic Arbitrage (`UniversalArbitrage` / `trade_bots_arbitrage.py`)
- **Use Case**: Cross-currency multi-hop arbitrage (supports 2-hop, 3-hop, 4-hop cycles).
- **Negative-Log Graph Algorithm**:
  $$\prod_{i=1}^k P_i > 1 \iff \sum_{i=1}^k -\ln(P_i) < 0$$
  Transforms exchange rates into directed graph edge weights $-\ln(P)$, finding optimal arbitrage paths via **Negative Cycle Detection**.
- **Atomic Execution**: Batches all $N$ swaps into a single atomic BitShares transaction with zero execution risk.

### 4. Bollinger Mean Reversion (`BollingerMeanReversion` / `trade_bots_bollinger.py`)
- **Use Case**: Mean-reverting and volatility contraction markets.
- **Principle**: Computes rolling moving average (MA) and standard deviation $\sigma$, placing orders at upper/lower band extremities.

---

## ⚙️ Configuration Reference (`trade_rules.json`)

```json
{
  "strategies": {
    "simple": {
      "strategy_name": "SimpleMaker",
      "description": "Simple market maker strategy based on Fiat CNY valuation",
      "block_delay": 1.0,
      "keep_bts_fees": 20.0,
      "custom_price": { "CNY": [16.0, "BTS"] },
      "default": { "freq": 2, "maker_spread": 1.5, "target_order_cny": 50.0 },
      "markets": [["BTS", "CNY"], ["BTS", "USD"]]
    },
    "grid": {
      "strategy_name": "DynamicGrid",
      "description": "Multi-level dynamic geometric grid strategy",
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
      "description": "Universal N-cycle atomic arbitrage strategy",
      "block_delay": 0.5,
      "keep_bts_fees": 30.0,
      "min_profit_pct": 0.5,
      "trade_amount_cny": 100.0,
      "max_cycle_length": 4,
      "markets": [["BTS", "CNY"], ["BTS", "USD"], ["CNY", "USD"]]
    },
    "bollinger": {
      "strategy_name": "BollingerMeanReversion",
      "description": "Bollinger Bands mean reversion strategy",
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

## 🛠️ Developer Guide

### 1. Data Query APIs

| API | Parameters | Returns | Description |
| :--- | :--- | :--- | :--- |
| `self.get_price(asset)` | `asset: str` | `float` | Gets asset valuation (supports chained `custom_price` ratios) |
| `self.get_free_balance(asset)` | `asset: str` | `float` | Gets free usable balance (BTS automatically excludes `keep_bts_fees`) |
| `self.get_total_balance(asset)` | `asset: str` | `float` | Gets total holding (free + in-order locked) |
| `self.get_my_orders(sell, recv)`| `sell, recv: Optional[str]` | `List[dict]` | Gets active orders belonging to this account |
| `self.get_market_orders(sell, recv)` | `sell, recv: str` | `List[dict]` | Gets orderbook depth orders excluding own account (sorted by price asc) |
| `self.get_order_by_id(order_id)` | `order_id: str` | `Optional[dict]` | Retrieves full order details from local cache |
| `self.is_chain_synced()` | None | `Tuple[bool, delay, ts]`| Checks if blockchain head stream timestamp is synchronized |

---

### 2. Streamlined Order Intent API (`apply_order_intents`)

#### ① Cancel Order (`cancel`)
```python
{"action": "cancel", "order_id": "1.7.573627825", "reason": "Order cleanup"}
```

#### ② Update Order (`update` - Native OP 77, Streamlined)
```python
# Update both price and amount:
{"action": "update", "order_id": "1.7.573174054", "price": 0.0754, "amount": 1000.0}

# Update price only:
{"action": "update", "order_id": "1.7.573174054", "price": 0.0754}

# Update amount only:
{"action": "update", "order_id": "1.7.573174054", "amount": 1000.0}
```

#### ③ Create Order (`create`)
```python
{
    "action": "create",
    "sell_asset": "BTS",
    "receive_asset": "CNY",
    "amount": 500.0,
    "price": 0.0754,
    "reason": "Initial liquidity placement"
}
```

---

### 3. Custom Strategy Implementation Example

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
                
                # Streamlined update call
                if abs(curr_p - target_price) / target_price > 0.005:
                    intents.append({
                        "action": "update",
                        "order_id": primary["order_id"],
                        "price": target_price,
                        "amount": target_amount,
                        "reason": "Price adjustment"
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
                        "reason": "Initial order placement"
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
