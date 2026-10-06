# Consolidator

The Consolidator is the fund's Order Management System. It is the only service authorised to submit orders to the exchange, and it holds the sole responsibility for cross-strategy netting, lot-precision enforcement, and broker reconciliation.

**Module** — `workers.consolidator_worker`

## Responsibilities

- Consume `TargetPosition` signals from all strategy workers
- Track each strategy's running position per `(symbol, exchange)`
- Compute the fund-level net position across all strategies
- Reconcile against the broker's live position
- Submit a single net-delta order per instrument when needed
- Publish fill confirmations back to the originating strategy
- Publish atomic position and P&L snapshots to the dashboard

## Signal Netting

The netting layer is the reason the infrastructure scales with the number of strategies rather than duplicating exchange traffic.

| Strategy | Symbol | Target Delta |
|---|---|---|
| Strategy A | ETHUSDT | `+3` |
| Strategy B | ETHUSDT | `−1` |
| **Net order to exchange** | ETHUSDT | **`+2`** |

Both strategies still receive their expected fills for internal PnL attribution — the fills are synthesised from the single broker execution, split proportionally.

## Broker Registry

The consolidator holds one broker instance per configured venue. Each broker adapter conforms to `BaseBroker`.

| Registry Key | Broker Class | Product |
|---|---|---|
| `bybit_demo` | `BybitBroker(demo=True)` | Bybit perpetual futures |
| `bybit_spot` | `BybitSpotBroker(demo=True)` | Bybit spot |
| `paper` | `PaperBroker` | Simulated fills |

Signals specify an `exchange` field to select the broker. Signals without an explicit exchange are routed to the value of `DEFAULT_EXCHANGE`.

## Lot Precision

Every order is rounded to the exchange's declared precision before submission. Sub-precision residuals are dropped rather than raised as errors — this is standard behaviour for exchanges that reject invalid lot sizes.

| Exchange | Symbol | Precision |
|---|---|---|
| `bybit_demo` | `ETHUSDT` | `0.01` contracts |
| `bybit_demo` | `LINKUSDT` | `1` contract |
| `bybit_spot` | `ETHUSDT` | `0.00001` ETH |
| `bybit_spot` | `LINKUSDT` | `0.001` LINK |

## Startup Reconciliation

On startup, the consolidator invokes `seed_from_broker()` — a synchronous query to every configured broker for its current live position. Internal state is initialised from these values.

This guarantees that after any restart — planned or unplanned — the consolidator's view of the fund's book matches the exchange's view. No signal is processed until reconciliation completes.

## Consumed Topics

| Topic | Type | Handler |
|---|---|---|
| `signals.targets.*` | `TargetPosition` | Signal netting and order routing |

## Published Topics

| Topic | Type | Cadence |
|---|---|---|
| `fills.{strategy_id}` | `FillConfirmation` | On every fill or synthetic net-out |
| `positions.snapshot` | Full book snapshot | Every 1 second |
| `broker.pnl` | Fund-level P&L | Every 1 second |
| `broker.pnl.{exchange}` | Per-venue P&L | Every 1 second |

## Persistence

The consolidator maintains a small SQLite store at `~/.qrf/consolidator.db` containing the realised-P&L baseline required for atomic delta-neutral accounting. The database is a projection of the event stream — deleting it forces a reseed from broker truth on next startup.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `NATS_URL` | `nats://localhost:4222` | NATS connection string |
| `DEFAULT_EXCHANGE` | `bybit_demo` | Fallback exchange for signals without an explicit exchange |
| `BYBIT_API_KEY` | *(required)* | Bybit API key |
| `BYBIT_API_SECRET` | *(required)* | Bybit API secret |
| `BYBIT_DEMO` | `0` | Set to `1` for the Bybit demo environment |

## Operation

```bash
python -m workers.consolidator_worker
```
