# Writing a Strategy

Every strategy is a Python class inheriting from `BaseStrategy`. Place it in the `strategies/` package and the pipeline discovers it by class name at startup.

## Minimal Example

```python title="strategies/my_strategy.py"
from decimal import Decimal
from typing import ClassVar

from interfaces.strategy import BaseStrategy
from interfaces.signals import TargetPosition
from data.types import AnyBar


class MyStrategy(BaseStrategy):
    topics: ClassVar[list[str]] = ["futures.ETHUSDT.bars.8h"]
    max_loss: ClassVar[Decimal] = Decimal("500")

    def on_bar(self, bar: AnyBar) -> TargetPosition | None:
        if bar.close > bar.open:
            return TargetPosition(
                symbol="ETHUSDT",
                quantity=Decimal("1"),
                exchange="bybit_demo",
            )
        if bar.close < bar.open:
            return TargetPosition(
                symbol="ETHUSDT",
                quantity=Decimal("-1"),
                exchange="bybit_demo",
            )
        return None
```

Deploy with:

```bash
STRATEGY_NAME=MyStrategy python -m workers.strategy_worker
```

The data feed, consolidator, execution, and dashboard pick it up automatically.

## Class Attributes

| Attribute | Type | Description |
|---|---|---|
| `topics` | `list[str]` | NATS topics to subscribe to. The worker subscribes on startup. |
| `max_loss` | `Decimal` | Cumulative loss threshold. `StrategyGuard` halts signal generation when breached. |

### Topic Format

```
futures.{SYMBOL}.bars.{interval}
futures.{SYMBOL}.funding_rate
```

Example:

```python
topics = [
    "futures.ETHUSDT.bars.8h",
    "futures.ETHUSDT.funding_rate",
    "futures.LINKUSDT.bars.8h",
]
```

## Event Hooks

Override the hooks you need. All return either a `TargetPosition` or `None`.

### `on_bar(bar)`

Called for every new price bar. The primary signal-generation hook.

| Parameter | Type | Description |
|---|---|---|
| `bar` | `AnyBar` | Fields: `symbol`, `open`, `high`, `low`, `close`, `volume`, `timestamp`, `interval_seconds` |

### `on_funding_rate(rate)`

Called for every funding-rate update from the exchange.

| Parameter | Type | Description |
|---|---|---|
| `rate` | `FundingRate` | Fields: `symbol`, `funding_rate`, `mark_price`, `timestamp`, `next_funding_time` |

### `on_fill(fill)`

Called when the consolidator confirms a fill. Use this to place follow-up orders — for example, a spot hedge after a perp fill.

| Parameter | Type | Description |
|---|---|---|
| `fill` | `FillConfirmation` | Fields: `strategy_id`, `symbol`, `quantity` (signed), `fill_price`, `exchange` |

### `on_start()` · `on_stop()`

Lifecycle hooks for setup and teardown. Return `None`.

```python
def on_start(self) -> None:
    self._state = load_state()

def on_stop(self) -> None:
    save_state(self._state)
```

## Signal Semantics

```python
@dataclass(frozen=True)
class TargetPosition:
    symbol: str        # e.g. "ETHUSDT"
    quantity: Decimal  # signed delta: +1 = buy 1, -1 = sell 1
    exchange: str      # e.g. "bybit_demo", "bybit_spot"
    price: Decimal     # reference price — set by the runner if omitted
    strategy_id: str   # set by the runner — do not assign manually
```

!!! note "Quantity is a delta"
    `quantity` is how much you want to **change** your position by, not what you want your position to be. The consolidator accumulates these deltas to track each strategy's running position.

    - Flat → long 2 → emit `quantity=+2`
    - Long 2 → long 1 → emit `quantity=−1`
    - Long 1 → flat → emit `quantity=−1`

## Reference Implementation

The live strategy — `FundingArbBybitStrategy` in `strategies/funding_arb_bybit.py` — demonstrates the full pattern: entry and exit conditions, delta-neutral hedging via `on_fill`, volatility filters, and state management across 8-hour settlement windows. It is the recommended starting point for any strategy that needs coordinated multi-leg execution.
