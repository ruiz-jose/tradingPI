const REFRESH_MS = 5000;
const fmt = (n, d = 2) => (n === null || n === undefined ? "—" : Number(n).toFixed(d));
const pnlClass = (n) => (n > 0 ? "pos" : n < 0 ? "neg" : "");
const fmtTime = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString();
};

let equityChart = null;

async function getJSON(url) {
  const res = await fetch(url);
  return res.json();
}

function renderOverview(o) {
  const badge = document.getElementById("conn-badge");
  if (o.connected) {
    badge.textContent = `conectado (${o.mode})`;
    badge.className = "badge ok";
  } else {
    badge.textContent = `desconectado: ${o.error || "sin credenciales"}`;
    badge.className = "badge bad";
  }

  document.getElementById("balance").textContent = o.connected ? `${fmt(o.balance)} USDT` : "—";
  document.getElementById("mode").textContent = o.connected
    ? `${o.symbols.join(", ")} · ${o.interval} · leverage ${o.leverage}x`
    : "esperando conexión";

  const upnlEl = document.getElementById("upnl");
  upnlEl.textContent = o.connected ? `${fmt(o.unrealized_pnl)} USDT` : "—";
  upnlEl.className = "big " + pnlClass(o.unrealized_pnl || 0);

  const tbody = document.querySelector("#positions-table tbody");
  tbody.innerHTML = "";
  const positions = o.open_positions || [];
  document.getElementById("positions-empty").style.display = positions.length ? "none" : "block";
  for (const p of positions) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${p.symbol}</td><td>${p.side}</td><td>${fmt(p.qty, 4)}</td>
      <td>${fmt(p.entry_price)}</td><td>${fmt(p.mark_price)}</td>
      <td class="${pnlClass(p.unrealized_pnl)}">${fmt(p.unrealized_pnl)}</td><td>${p.leverage}x</td>`;
    tbody.appendChild(tr);
  }
}

function renderStats(s) {
  const totalPnlEl = document.getElementById("total-pnl");
  totalPnlEl.textContent = `${fmt(s.total_pnl)} USDT`;
  totalPnlEl.className = "big " + pnlClass(s.total_pnl);

  document.getElementById("total-trades").textContent = `${s.total_trades} operaciones cerradas`;
  document.getElementById("win-rate").textContent = `${fmt(s.win_rate, 1)}%`;
  document.getElementById("pf").textContent = s.profit_factor === null ? "—" : fmt(s.profit_factor);
  document.getElementById("losses-streak").textContent = s.consecutive_losses > 0
    ? `${s.consecutive_losses} pérdidas consecutivas`
    : "sin racha de pérdidas activa";

  const symBody = document.querySelector("#symbol-table tbody");
  symBody.innerHTML = "";
  for (const [symbol, v] of Object.entries(s.by_symbol || {})) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${symbol}</td><td>${v.trades}</td><td>${fmt(v.win_rate, 1)}%</td>
      <td class="${pnlClass(v.pnl)}">${fmt(v.pnl)}</td>`;
    symBody.appendChild(tr);
  }

  const closedBody = document.querySelector("#closed-table tbody");
  closedBody.innerHTML = "";
  for (const c of s.recent_closed || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${fmtTime(c.timestamp)}</td><td>${c.symbol}</td><td>${c.side}</td>
      <td>${fmt(c.entry)}</td><td>${fmt(c.exit)}</td>
      <td class="${pnlClass(c.pnl)}">${fmt(c.pnl)}</td><td>${c.closed_by}</td>`;
    closedBody.appendChild(tr);
  }

  const labels = s.equity_curve.map((p) => fmtTime(p.timestamp));
  const data = s.equity_curve.map((p) => p.cumulative_pnl);
  if (!equityChart) {
    const ctx = document.getElementById("equity-chart").getContext("2d");
    equityChart = new Chart(ctx, {
      type: "line",
      data: { labels, datasets: [{ label: "PnL acumulado (USDT)", data, borderColor: "#3fb950", tension: 0.15, pointRadius: 0 }] },
      options: {
        responsive: true,
        scales: {
          x: { ticks: { color: "#8b949e", maxTicksLimit: 8 }, grid: { color: "#21262d" } },
          y: { ticks: { color: "#8b949e" }, grid: { color: "#21262d" } },
        },
        plugins: { legend: { labels: { color: "#c9d1d9" } } },
      },
    });
  } else {
    equityChart.data.labels = labels;
    equityChart.data.datasets[0].data = data;
    equityChart.update();
  }
}

function renderLog(l) {
  const box = document.getElementById("log-box");
  const wasAtBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 5;
  box.textContent = l.lines.join("\n");
  if (wasAtBottom) box.scrollTop = box.scrollHeight;
}

async function refresh() {
  try {
    const [overview, stats, log] = await Promise.all([
      getJSON("/api/overview"),
      getJSON("/api/stats"),
      getJSON("/api/log"),
    ]);
    renderOverview(overview);
    renderStats(stats);
    renderLog(log);
    document.getElementById("last-update").textContent = new Date().toLocaleTimeString();
  } catch (err) {
    console.error("Error refrescando dashboard:", err);
  }
}

refresh();
setInterval(refresh, REFRESH_MS);
