# Infrastructure
General Fund Infrastructure with most important features.



### UV

This project uses [uv](https://docs.astral.sh/uv/) for dependency management. No other package manager is needed.

**Install uv:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Set up the project:**
```bash
uv venv          # create virtual environment
uv sync          # install dependencies
```

**Run scripts:**

To run python scripts, you can either use the following:
```bash
uv run script.py
```
or more conventionally:
```bash
source .venv/bin/activate
python script.py
```

**Add/remove dependencies:**
```bash
uv add <package>
uv remove <package>
```

---

## Running the System

Start each component in a separate terminal in the order listed below.

### 1. NATS Server

```bash
nats-server
```

Default port: `4222`. If already running, skip this step.

### 2. Data Feed Server

Streams live market data (bars, funding rates) to NATS.

```bash
python -m workers.datafeed_server
```

Environment variables:
- `NATS_URL` (default: `nats://localhost:4222`)

### 3. Strategy Worker

Runs a single strategy and publishes `TargetPosition` signals to NATS.

```bash
STRATEGY_NAME=ExampleStrategy python -m workers.strategy_worker
```

Environment variables:
- `STRATEGY_NAME` **(required)** — class name of a `BaseStrategy` subclass in the `strategies` package
- `NATS_URL` (default: `nats://localhost:4222`)

> Spawn one process per strategy. Each instance selects its own strategy via `STRATEGY_NAME`.

### 4. Consolidator Worker

Nets signals across all strategies and routes orders to the broker.

```bash
python -m workers.consolidator_worker
```

Environment variables:
- `NATS_URL` (default: `nats://localhost:4222`)
- `DEFAULT_EXCHANGE` (default: `bybit_paper`) — `bybit_paper` or `paper`

### 5. Dashboard Backend

Serves the REST API and WebSocket feed.

```bash
uv run uvicorn workers.dashboard:app --reload --port 8000
```

Environment variables:
- `NATS_URL` (default: `nats://localhost:4222`)

API docs available at `http://localhost:8000/docs`.

### 6. Dashboard Frontend

```bash
cd dashboard/frontend
npm run dev
```

Runs the Vite dev server (default: `http://localhost:5173`).

For a production build:

```bash
npm run build
```
