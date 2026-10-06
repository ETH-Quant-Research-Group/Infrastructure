# Strategies

Strategies live in the `strategies/` package. Each is a Python class inheriting from `BaseStrategy` — a single interface that the pipeline discovers by class name at runtime.

## Registry

| Class | Status | Description |
|---|---|---|
| `FundingArbBybitStrategy` | Live | Delta-neutral funding rate arbitrage on Bybit — short perpetual, long spot |
| `ExampleStrategy` | Reference | Minimal implementation for learning the interface |

## Contract

Every strategy exposes the same shape:

- **Input** — market events (`TimeBar`, `FundingRate`, `FillConfirmation`) delivered to typed hooks
- **Output** — a `TargetPosition` signal (signed quantity delta) or `None`
- **State** — held privately in the strategy instance; reconstructed from broker positions on restart

A strategy has no knowledge of order routing, execution, or persistence. It emits intents.

## Adding a Strategy

Follow the [Writing a Strategy](writing-a-strategy.md) guide. In summary:

1. Create a new file in `strategies/`
2. Define a class inheriting from `BaseStrategy`
3. Declare the NATS topics you need in `topics`
4. Implement `on_bar` and any other relevant hooks
5. Run the strategy worker with `STRATEGY_NAME=YourClassName`

No changes to any other component are required.
