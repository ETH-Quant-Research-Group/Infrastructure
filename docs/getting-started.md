# Getting Started

This guide walks through bringing the full pipeline online — from a fresh clone to a live dashboard displaying real-time strategy state.

## Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.11 or newer |
| Node.js | 20 or newer |
| `uv` package manager | latest |
| NATS server | 2.10 or newer |
| Bybit account | API key with trade permissions |

## Installation

Clone the repository and install the Python environment:

```bash
git clone https://github.com/ETH-Quant-Research-Group/Infrastructure.git
cd Infrastructure

uv venv
uv sync
```

Install the frontend dependencies:

```bash
cd dashboard/frontend
npm install
cd ../..
```

## Configuration

Create a `.env` file at the project root:

```bash title=".env"
BYBIT_API_KEY=your_key_here
BYBIT_API_SECRET=your_secret_here
BYBIT_DEMO=1
NATS_URL=nats://localhost:4222
```

!!! warning "Demo vs. Live"
    Set `BYBIT_DEMO=1` for the Bybit demo environment. Set `0` only when connecting to live capital. The API credentials must match the environment — demo keys will not authenticate against the live API.

## Starting the Pipeline

Each component runs as a separate process. Open one terminal per component and start them in the following order.

### 1. Message Bus

```bash
nats-server
```

Listens on `nats://localhost:4222`. If a NATS server is already running on the host, skip this step.

### 2. Data Feed

```bash
python -m workers.datafeed_server
```

Streams live bars and funding rates from Bybit onto the bus.

### 3. Strategy Worker

```bash
STRATEGY_NAME=FundingArbBybitStrategy python -m workers.strategy_worker
```

Runs the named strategy and publishes signals. Spawn one worker per strategy.

### 4. Consolidator

```bash
python -m workers.consolidator_worker
```

Nets signals and routes orders to the exchange.

### 5. Dashboard Backend

```bash
uvicorn workers.dashboard:app --port 8000
```

Interactive API documentation available at `http://localhost:8000/docs`.

### 6. Dashboard Frontend

```bash
cd dashboard/frontend
npm run dev
```

Opens at `http://localhost:5173`.

## Environment Variables

| Variable | Applies To | Default | Description |
|---|---|---|---|
| `NATS_URL` | all workers | `nats://localhost:4222` | NATS connection string |
| `STRATEGY_NAME` | strategy worker | *(required)* | Class name of a `BaseStrategy` subclass |
| `DEFAULT_EXCHANGE` | consolidator | `bybit_demo` | Fallback exchange for signals without an explicit exchange |
| `BYBIT_API_KEY` | strategy, consolidator | *(required)* | Bybit API key |
| `BYBIT_API_SECRET` | strategy, consolidator | *(required)* | Bybit API secret |
| `BYBIT_DEMO` | strategy, consolidator | `0` | Set to `1` for the Bybit demo environment |

## Verification

Once all six processes are running, confirm the pipeline is healthy:

1. Open `http://localhost:5173` — the dashboard should render.
2. Wait for the next 8-hour settlement boundary (`00:00`, `08:00`, or `16:00` UTC).
3. The strategy heartbeat should update and, if entry conditions are met, positions should appear in the dashboard within one settlement window.

## Deployment

The production deployment runs on a dedicated on-prem server, accessed via Tailscale VPN. The stack is managed with `systemd` — each worker is a service that auto-restarts on failure and on reboot. Cloudflare Tunnel exposes the dashboard at [qrfzurich.com](https://qrfzurich.com).
