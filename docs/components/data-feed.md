# Data Feed

The Data Feed is the sole ingress point for external market data into the pipeline. It connects to Bybit's WebSocket API, normalises incoming events into internal types, and publishes them onto the message bus.

**Module** — `workers.datafeed_server`

## Responsibilities

- Maintain a persistent WebSocket connection to Bybit
- Subscribe to price klines and funding-rate streams for all configured symbols
- Aggregate 1-hour klines into 8-hour bars at settlement boundaries
- Normalise messages into `TimeBar` and `FundingRate` types
- Publish normalised events to the NATS bus
- Reconnect automatically on disconnect with exponential backoff

## Published Topics

| Topic | Payload Type | Cadence |
|---|---|---|
| `futures.{SYMBOL}.bars.8h` | `TimeBar` | Once per 8-hour settlement window |
| `futures.{SYMBOL}.funding_rate` | `FundingRate` | On every rate update from the exchange |

## 8-Hour Bar Aggregation

Bybit does not natively expose an 8-hour kline interval. The feed subscribes to 1-hour klines and buckets them by settlement window:

$$
\text{window\_start} = \left\lfloor \frac{t}{8h} \right\rfloor \times 8h
$$

A `TimeBar` is emitted when the closing 1-hour kline of a settlement window is received — i.e., the klines opening at `07:00`, `15:00`, or `23:00` UTC.

## Data Types

### `TimeBar`

| Field | Type | Description |
|---|---|---|
| `symbol` | `str` | Instrument identifier (unified naming) |
| `open` | `Decimal` | Opening price |
| `high` | `Decimal` | Session high |
| `low` | `Decimal` | Session low |
| `close` | `Decimal` | Closing price |
| `volume` | `Decimal` | Traded base volume |
| `timestamp` | `datetime` | Bar open time (UTC) |
| `close_time` | `datetime` | Bar close time (UTC) |
| `interval_seconds` | `int` | Bar duration (28,800 for 8h) |

### `FundingRate`

| Field | Type | Description |
|---|---|---|
| `symbol` | `str` | Perpetual instrument |
| `funding_rate` | `Decimal` | Raw rate per 8h period (e.g. `0.0001` = `0.01%`) |
| `mark_price` | `Decimal` | Mark price at rate publication |
| `timestamp` | `datetime` | Rate publication time (UTC) |
| `next_funding_time` | `datetime` | Time of next scheduled settlement (UTC) |

## Configuration

| Variable | Default | Description |
|---|---|---|
| `NATS_URL` | `nats://localhost:4222` | NATS connection string |

## Operation

```bash
python -m workers.datafeed_server
```
