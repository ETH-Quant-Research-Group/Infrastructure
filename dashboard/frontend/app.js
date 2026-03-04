const wsStatus = document.getElementById("ws-status");

// --- WebSocket live feed ---
function connectWS() {
  const ws = new WebSocket(`ws://${location.host}/ws/live`);

  ws.onopen = () => {
    wsStatus.textContent = "Connected";
    wsStatus.className = "status online";
  };

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    handleMessage(msg);
  };

  ws.onclose = () => {
    wsStatus.textContent = "Disconnected";
    wsStatus.className = "status offline";
    setTimeout(connectWS, 3000); // reconnect
  };
}

function handleMessage(msg) {
  // Route incoming NATS messages to panels by subject prefix.
  // msg = { subject: "futures.BTCUSDT.bars.1m", data: { ... } }
  const { subject, data } = msg;

  if (subject.startsWith("futures.") || subject.startsWith("spot.")) {
    updatePanel("market-content", subject, data);
  } else if (subject.startsWith("signals.")) {
    updatePanel("strategies-content", subject, data);
  }
}

function updatePanel(panelId, label, data) {
  const el = document.getElementById(panelId);
  if (!el) return;
  const row = document.createElement("pre");
  row.textContent = `[${label}] ${JSON.stringify(data)}`;
  el.prepend(row);
  // Keep last 50 rows
  while (el.children.length > 50) el.removeChild(el.lastChild);
}

// --- REST polling ---
async function fetchPanel(url, panelId, render) {
  try {
    const res = await fetch(url);
    const data = await res.json();
    const el = document.getElementById(panelId);
    if (el) el.innerHTML = render(data);
  } catch (e) {
    console.error(url, e);
  }
}

function pollAll() {
  fetchPanel("/api/positions/", "positions-content", (d) =>
    `<pre>${JSON.stringify(d, null, 2)}</pre>`
  );
  fetchPanel("/api/orders/active", "orders-content", (d) =>
    `<pre>${JSON.stringify(d, null, 2)}</pre>`
  );
  fetchPanel("/api/strategies/", "strategies-content", (d) =>
    `<pre>${JSON.stringify(d, null, 2)}</pre>`
  );
  fetchPanel("/api/performance/metrics", "performance-content", (d) =>
    `<pre>${JSON.stringify(d, null, 2)}</pre>`
  );
}

connectWS();
pollAll();
setInterval(pollAll, 5000);
