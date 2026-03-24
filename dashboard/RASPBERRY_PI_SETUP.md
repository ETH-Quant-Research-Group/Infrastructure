# Raspberry Pi Setup

The Pi runs everything locally:
- `nats-server` — message bus (port 4222)
- `workers.datafeed_server` — publishes Binance data to NATS
- `workers.strategy_worker` — runs strategies, emits signals via NATS
- `workers.consolidator_worker` — nets signals, places orders via broker
- `workers.dashboard` — FastAPI + NATS bridge + WebSocket (port 8000)
- `cloudflared` — tunnel to qrfzurich.com

The dashboard connects directly to the local NATS server and relays
events to the browser via WebSocket. No remote HTTP push needed.

---

## First-time setup

```bash
# 1. Clone repo
git clone <your-repo> && cd Infrastructure

# 2. Python deps
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync

# 3. Node deps + build frontend
cd dashboard/frontend && npm install && npm run build && cd ../..

# 4. Install nats-server (ARM64)
curl -L https://github.com/nats-io/nats-server/releases/latest/download/nats-server-v2.10.24-linux-arm64.tar.gz \
  -o /tmp/nats.tar.gz
tar -xzf /tmp/nats.tar.gz -C /usr/local/bin --strip-components=1
chmod +x /usr/local/bin/nats-server

# 5. Install cloudflared (ARM64)
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 \
  -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared
```

## Copy tunnel credentials from your Mac

```bash
# Run on your Mac (from the Infrastructure directory):
ssh carloteufel@raspberrypi "mkdir -p ~/.cloudflared"
scp ~/.cloudflared/cert.pem carloteufel@raspberrypi:~/.cloudflared/cert.pem
scp ~/.cloudflared/89279296-baf5-4a40-b2f2-62373d950574.json \
    carloteufel@raspberrypi:~/.cloudflared/89279296-baf5-4a40-b2f2-62373d950574.json
scp dashboard/cloudflared-config.yml \
    carloteufel@raspberrypi:~/Infrastructure/dashboard/cloudflared-config.yml
```

---

## Running

Open terminals (or use tmux):

```bash
# Terminal 1 — NATS
nats-server

# Terminal 2 — data feed
uv run python -m workers.datafeed_server

# Terminal 3 — strategy worker
STRATEGY_NAME=Example uv run python -m workers.strategy_worker

# Terminal 4 — consolidator
uv run python -m workers.consolidator_worker

# Terminal 5 — dashboard
uv run uvicorn workers.dashboard:app --port 8000

# Terminal 6 — tunnel
cloudflared tunnel --config dashboard/cloudflared-config.yml run my-dashboard
```

Site is live at https://qrfzurich.com
