# Raspberry Pi Setup

The Pi runs:
- `dashboard/frontend` — Vite dev server (port 5173)
- `dashboard/app.py` — API routes only (port 8000)
- `cloudflared` — tunnel to qrfzurich.com

The broker/engine/NATS runs remotely on a separate machine.
Vite proxies `/api` and `/ws` to `localhost:8000` (app.py).

**Note:** `nats_bridge.py` is not yet wired into `app.py`.
Until it is, the dashboard loads fine but all data panels are empty.
When wired up, set `NATS_URL=nats://<remote-ip>:4222` on the Pi
so the bridge connects to the remote NATS server.

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

# 4. Install cloudflared (ARM64)
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 \
  -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared
```

## Copy tunnel credentials from your Mac

```bash
# Run on your Mac (from the Infrastructure directory):
# Create the directory on the Pi first (scp won't create it automatically)
ssh carloteufel@raspberrypi "mkdir -p ~/.cloudflared"
scp ~/.cloudflared/cert.pem carloteufel@raspberrypi:~/.cloudflared/cert.pem
scp ~/.cloudflared/89279296-baf5-4a40-b2f2-62373d950574.json \
    carloteufel@raspberrypi:~/.cloudflared/89279296-baf5-4a40-b2f2-62373d950574.json
scp dashboard/cloudflared-config.yml \
    carloteufel@raspberrypi:~/Infrastructure/dashboard/cloudflared-config.yml
```

---

## Running

Open 3 terminals (or use tmux):

```bash
# Terminal 1 — backend API
uvicorn dashboard.app:app --port 8000

# Terminal 2 — tunnel
cloudflared tunnel --config dashboard/cloudflared-config.yml run my-dashboard
```

Site is live at https://qrfzurich.com
