"""Flask dashboard at :8090 — portfolio chart, trade log, positions, decisions, backtests."""
from __future__ import annotations

import json
from flask import Flask, jsonify, render_template_string

from .store import get_store

app = Flask(__name__)


@app.after_request
def _cors(resp):
    # Cross-port fetch from Digital Intern dashboard (8080 → 8090).
    resp.headers.setdefault("Access-Control-Allow-Origin", "*")
    resp.headers.setdefault("Access-Control-Allow-Methods", "GET, OPTIONS")
    resp.headers.setdefault("Access-Control-Allow-Headers", "Content-Type")
    return resp


TEMPLATE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Paper Trader</title>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='5' fill='%230d0d0d'/%3E%3Cline x1='7' y1='15' x2='7' y2='18' stroke='%2300d4ff' stroke-width='1.5'/%3E%3Crect x='5.5' y='18' width='3' height='7' rx='0.5' fill='%2300d4ff'/%3E%3Cline x1='7' y1='25' x2='7' y2='27' stroke='%2300d4ff' stroke-width='1.5'/%3E%3Cline x1='15' y1='12' x2='15' y2='15' stroke='%23ff3c4c' stroke-width='1.5'/%3E%3Crect x='13.5' y='15' width='3' height='6' rx='0.5' fill='%23ff3c4c'/%3E%3Cline x1='15' y1='21' x2='15' y2='24' stroke='%23ff3c4c' stroke-width='1.5'/%3E%3Cline x1='23' y1='5' x2='23' y2='8' stroke='%2300ff9f' stroke-width='1.5'/%3E%3Crect x='21.5' y='8' width='3' height='12' rx='0.5' fill='%2300ff9f'/%3E%3Cline x1='23' y1='20' x2='23' y2='23' stroke='%2300ff9f' stroke-width='1.5'/%3E%3Cpolyline points='7,21 15,17 23,11' stroke='%23ffd700' stroke-width='1.2' fill='none' stroke-dasharray='2,1.5'/%3E%3C/svg%3E">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    body {
      margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
      background: #0b0f14; color: #cfd8dc; padding: 24px; font-size: 16px;
    }
    h1 { margin: 0 0 6px; font-size: 28px; }
    .sub { color: #78909c; font-size: 14px; margin-bottom: 20px; }
    nav.tabs {
      display: flex; gap: 4px; margin-bottom: 20px;
      border-bottom: 1px solid #1b2229;
    }
    nav.tabs a {
      padding: 10px 18px; color: #78909c; text-decoration: none;
      border-bottom: 2px solid transparent; font-size: 15px;
      cursor: pointer;
    }
    nav.tabs a.active { color: #42a5f5; border-bottom-color: #42a5f5; }
    nav.tabs a:hover { color: #cfd8dc; }
    .tab-pane { display: none; }
    .tab-pane.active { display: block; }
    .grid {
      display: grid; gap: 18px;
      grid-template-columns: 1fr 1fr;
    }
    @media (max-width: 980px) { .grid { grid-template-columns: 1fr; } }
    .card {
      background: #11161d; border: 1px solid #1b2229; border-radius: 12px;
      padding: 20px;
    }
    .card h2 {
      margin: 0 0 14px; font-size: 15px;
      color: #b0bec5; text-transform: uppercase;
    }
    .stat-row { display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 12px; }
    .stat { flex: 1 1 120px; }
    .stat .v { font-size: 26px; color: #eceff1; font-weight: 600; }
    .stat .l { color: #78909c; font-size: 13px; text-transform: uppercase; }
    .pos, .pl { color: #4caf50; }
    .neg { color: #ef5350; }
    table { width: 100%; border-collapse: collapse; font-size: 15px; }
    th, td {
      text-align: left; padding: 9px 10px;
      border-bottom: 1px solid #1b2229;
    }
    th { color: #78909c; font-weight: 600; }
    td.num { text-align: right; font-variant-numeric: tabular-nums; }
    .muted { color: #78909c; }
    canvas { max-height: 280px; }
    .pill {
      display: inline-block; padding: 2px 8px; border-radius: 100px;
      background: #1f2933; color: #b0bec5; font-size: 10px; letter-spacing: .5px;
    }
    .pill.buy { background: #1b3a2a; color: #66bb6a; }
    .pill.sell { background: #3a1b1b; color: #ef5350; }
    .pill.run { background: #20303f; color: #82b1ff; }
    .pill.status-running  { background: #1f3a55; color: #82b1ff; }
    .pill.status-complete { background: #1b3a2a; color: #66bb6a; }
    .pill.status-failed   { background: #3a1b1b; color: #ef5350; }
    .pill.status-pending  { background: #2a2a2a; color: #b0bec5; }
    .spinner {
      display: inline-block; width: 10px; height: 10px;
      border: 2px solid #1b2229; border-top-color: #82b1ff;
      border-radius: 50%; animation: spin 0.8s linear infinite;
      vertical-align: middle; margin-right: 6px;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    .progress-wrap {
      margin: 10px 0 16px; height: 6px; background: #1b2229;
      border-radius: 4px; overflow: hidden;
    }
    .progress-bar {
      height: 100%; background: linear-gradient(90deg, #42a5f5, #66bb6a);
      transition: width 0.4s ease;
    }
    .progress-label { font-size: 12px; color: #78909c; margin-bottom: 4px; }
    tr.bt-row { cursor: pointer; }
    tr.bt-row:hover td { background: #161d26; }
    tr.bt-row.best td { background: #143124; }
    tr.bt-row.beat td:first-child { border-left: 2px solid #4caf50; }
    tr.bt-row.miss td:first-child { border-left: 2px solid #ef5350; }
    #bt-trades { margin-top: 14px; display: none; }
    #bt-trades.show { display: block; }
    .bt-headline {
      display: flex; gap: 28px; flex-wrap: wrap; margin-bottom: 12px;
    }
    .bt-headline .stat .v { font-size: 22px; }
    .bt-layout {
      display: grid; grid-template-columns: 240px 1fr; gap: 14px; align-items: start;
    }
    @media (max-width: 980px) { .bt-layout { grid-template-columns: 1fr; } }
    .bt-sidebar { position: sticky; top: 14px; max-height: calc(100vh - 30px); overflow-y: auto; }
    .bt-sidebar h2 { margin: 0; }
    .bt-legend-row {
      display: flex; align-items: center; gap: 8px; padding: 6px 4px;
      border-bottom: 1px solid #1b2229; cursor: pointer; user-select: none;
      transition: background 0.15s;
    }
    .bt-legend-row:hover { background: #161d26; }
    .bt-legend-row.selected { background: #1b2937; }
    .bt-legend-row.hidden-run { opacity: 0.35; }
    .bt-legend-row input[type=checkbox] { accent-color: #82b1ff; margin: 0; }
    .bt-swatch {
      width: 12px; height: 12px; border-radius: 3px; flex: 0 0 12px;
    }
    .bt-legend-row .name { flex: 1; font-size: 13px; color: #cfd8dc; }
    .bt-legend-row .ret { font-size: 11px; font-variant-numeric: tabular-nums; }
    .bt-btn {
      background: #1b2937; color: #cfd8dc; border: 1px solid #2a3a4f;
      border-radius: 4px; padding: 3px 8px; font-size: 11px; cursor: pointer;
      text-transform: uppercase; letter-spacing: 0.5px;
    }
    .bt-btn:hover { background: #243349; }
    .bt-tabs {
      display: flex; gap: 4px; margin-bottom: 12px;
      border-bottom: 1px solid #1b2229;
    }
    .bt-tabs a {
      padding: 8px 14px; color: #78909c; cursor: pointer; font-size: 13px;
      border-bottom: 2px solid transparent;
    }
    .bt-tabs a.active { color: #82b1ff; border-bottom-color: #82b1ff; }
    .bt-subpane { display: none; }
    .bt-subpane.active { display: block; }
    tr.bt-row.selected td { background: #1b2937 !important; }
    .pill.status-running { animation: pulse 1.5s ease-in-out infinite; }
    @keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:0.55;} }
    .live-dot {
      display: inline-block; width: 8px; height: 8px; border-radius: 50%;
      background: #66bb6a; margin-right: 6px; animation: pulse 1.5s infinite;
    }
    th.sortable-h { cursor: pointer; user-select: none; }
    th.sortable-h:hover { color: #cfd8dc; }
    th.sortable-h.sort-asc::after  { content: " ▲"; font-size: 9px; }
    th.sortable-h.sort-desc::after { content: " ▼"; font-size: 9px; }
  </style>
</head>
<body>
  <nav style="background:#1a1a2e;padding:12px 24px;display:flex;gap:24px;align-items:center;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;font-size:16px;border-bottom:1px solid #333;margin:-24px -24px 18px -24px">
    <span style="color:#e94560;font-weight:bold;font-size:1.1em">◈ TRADING STACK</span>
    <a href="http://10.19.203.44:8888/" style="color:#00b4d8;text-decoration:none">Home</a>
    <a href="http://10.19.203.44:8888/intern/" style="color:#00b4d8;text-decoration:none">Digital Intern</a>
    <a href="http://10.19.203.44:8888/trader/" style="color:#fff;border-bottom:2px solid #e94560;text-decoration:none">Paper Trader</a>
    <a href="http://10.19.203.44:8888/trader/backtests" style="color:#00b4d8;text-decoration:none">Backtests</a>
    <span style="margin-left:auto;color:#666;font-size:0.8em">10.19.203.44</span>
  </nav>

  <h1>Paper Trader</h1>
  <div class="sub" id="hb">loading…</div>

  <div class="card" style="margin-bottom:18px;">
    <h2 style="display:flex;justify-content:space-between;align-items:center;">
      <span>Signal Feed — Digital Intern</span>
      <a href="http://10.19.203.44:8080" style="font-size:11px;color:#42a5f5;text-decoration:none;text-transform:none;letter-spacing:normal">View All Signals →</a>
    </h2>
    <ul id="signal-feed" style="margin:0;padding:0;list-style:none;font-size:12px;">
      <li class="muted">loading…</li>
    </ul>
  </div>

  <nav class="tabs">
    <a id="tab-trader-link"    onclick="showTab('trader')">Trader</a>
    <a id="tab-backtests-link" onclick="showTab('backtests')">Backtests</a>
  </nav>

  <!-- ────── Trader pane ────── -->
  <div id="tab-trader" class="tab-pane">
    <div class="card" style="margin-bottom:18px;">
      <h2>Equity curve</h2>
      <div class="stat-row">
        <div class="stat"><div class="l">total value</div><div class="v" id="tv">—</div></div>
        <div class="stat"><div class="l">cash</div><div class="v" id="cash">—</div></div>
        <div class="stat"><div class="l">P/L vs $1000</div><div class="v" id="pl">—</div></div>
        <div class="stat"><div class="l">S&amp;P 500</div><div class="v" id="sp">—</div></div>
      </div>
      <canvas id="eq"></canvas>
    </div>

    <div class="grid">
      <div class="card">
        <h2>Open positions</h2>
        <table id="pos-tbl">
          <thead><tr>
            <th>ticker</th><th>type</th><th class="num">qty</th>
            <th class="num">avg</th><th class="num">now</th><th class="num">P/L</th>
          </tr></thead><tbody></tbody>
        </table>
      </div>
      <div class="card">
        <h2>Recent trades</h2>
        <table id="trades-tbl">
          <thead><tr>
            <th>time</th><th>action</th><th>ticker</th>
            <th class="num">qty</th><th class="num">price</th><th>reason</th>
          </tr></thead><tbody></tbody>
        </table>
      </div>
    </div>

    <div class="card" style="margin-top:18px;">
      <h2>Decision log</h2>
      <table id="dec-tbl">
        <thead><tr>
          <th>time</th><th>open?</th><th class="num">signals</th>
          <th>action</th><th class="num">equity</th><th>reasoning</th>
        </tr></thead><tbody></tbody>
      </table>
    </div>
  </div>

  <!-- ────── Backtests pane ────── -->
  <div id="tab-backtests" class="tab-pane">
    <div class="bt-layout">
      <aside class="bt-sidebar card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
          <h2 style="margin:0;">Runs</h2>
          <div style="display:flex;gap:4px;">
            <button class="bt-btn" onclick="btToggleAll(true)">all</button>
            <button class="bt-btn" onclick="btToggleAll(false)">none</button>
          </div>
        </div>
        <div id="bt-legend"></div>
      </aside>

      <div class="bt-main">
        <div class="card" style="margin-bottom:14px;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;">
            <div>
              <h2 style="margin:0 0 4px;">Backtest equity curves</h2>
              <div class="progress-label" id="bt-progress-label">—</div>
            </div>
            <div style="text-align:right;font-size:12px;color:#78909c;">
              <div id="bt-live-indicator"></div>
              <div id="bt-last-updated">last update: —</div>
            </div>
          </div>
          <div class="progress-wrap" style="margin:8px 0 14px"><div class="progress-bar" id="bt-progress-bar" style="width:0%"></div></div>
          <div class="bt-headline">
            <div class="stat"><div class="l">avg return</div><div class="v" id="bt-avg">—</div></div>
            <div class="stat"><div class="l">avg final $</div><div class="v" id="bt-avg-final">—</div></div>
            <div class="stat"><div class="l">best</div><div class="v" id="bt-best">—</div></div>
            <div class="stat"><div class="l">worst</div><div class="v" id="bt-worst">—</div></div>
            <div class="stat"><div class="l">SPY</div><div class="v" id="bt-spy">—</div></div>
            <div class="stat"><div class="l">vs SPY</div><div class="v" id="bt-beat">—</div></div>
          </div>
          <div style="position:relative;height:420px;"><canvas id="bt-chart"></canvas></div>
        </div>

        <div class="card" style="margin-bottom:14px;">
          <h2>Runs table — click a row to highlight</h2>
          <table id="bt-tbl" class="sortable">
            <thead><tr>
              <th data-k="run_id">#</th>
              <th data-k="seed" class="num">seed</th>
              <th data-k="status">status</th>
              <th data-k="final_value" class="num">current $</th>
              <th data-k="total_return_pct" class="num">return %</th>
              <th data-k="vs_spy_pct" class="num">vs SPY</th>
              <th data-k="n_trades" class="num">trades</th>
              <th data-k="n_decisions" class="num">decisions</th>
              <th data-k="started_at">started</th>
            </tr></thead><tbody></tbody>
          </table>
        </div>

        <div class="card" id="bt-detail" style="display:none;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <h2 style="margin:0;">Run <span id="bt-detail-id">—</span> detail</h2>
            <button class="bt-btn" onclick="closeDetail()">close</button>
          </div>
          <div id="bt-detail-meta" class="muted" style="font-size:13px;margin-bottom:12px;"></div>
          <div class="bt-tabs">
            <a id="bt-tab-trades-link" class="active" onclick="showBtSubtab('trades')">Trades</a>
            <a id="bt-tab-decisions-link" onclick="showBtSubtab('decisions')">Decisions</a>
          </div>
          <div id="bt-tab-trades" class="bt-subpane active">
            <table id="bt-trades-tbl">
              <thead><tr>
                <th>date</th><th>action</th><th>ticker</th>
                <th class="num">qty</th><th class="num">price</th>
                <th class="num">value</th><th>reason</th>
              </tr></thead><tbody></tbody>
            </table>
          </div>
          <div id="bt-tab-decisions" class="bt-subpane">
            <table id="bt-decisions-tbl">
              <thead><tr>
                <th>date</th><th>action</th><th>ticker</th>
                <th>status</th><th>detail</th><th class="num">portfolio $</th>
              </tr></thead><tbody></tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  </div>

<script>
const fmt = (n, d=2) => (n == null ? "—" : Number(n).toLocaleString(undefined, {minimumFractionDigits:d, maximumFractionDigits:d}));
const dollar = n => (n == null ? "—" : "$" + fmt(n));
const dt = s => s ? s.replace("T", " ").slice(0,16) : "";

const INITIAL_TAB = "{{ initial_tab }}";
const RUN_COLORS = [
  "#00d4ff","#ff6b35","#7fff00","#ff3cac","#ffd700",
  "#00ff9f","#ff1744","#e040fb","#40c4ff","#ff9100"
];
const SPY_COLOR = "#888888";

function showTab(name) {
  document.querySelectorAll(".tab-pane").forEach(el => el.classList.remove("active"));
  document.querySelectorAll("nav.tabs a").forEach(el => el.classList.remove("active"));
  document.getElementById("tab-" + name).classList.add("active");
  document.getElementById("tab-" + name + "-link").classList.add("active");
  if (name === "backtests" && !btLoaded) loadBacktests();
  // Update URL without reload
  if (history.replaceState) history.replaceState(null, "", name === "trader" ? "/" : "/backtests");
}

// ───────── Trader pane ─────────
let chart;
async function refresh() {
  const r = await fetch("/api/state").then(r => r.json());
  document.getElementById("hb").textContent = "updated " + (r.now || "");
  document.getElementById("tv").textContent = dollar(r.portfolio.total_value);
  document.getElementById("cash").textContent = dollar(r.portfolio.cash);
  const pl = r.portfolio.total_value - 1000;
  const plEl = document.getElementById("pl");
  plEl.textContent = (pl >= 0 ? "+" : "") + dollar(pl);
  plEl.className = "v " + (pl >= 0 ? "pos" : "neg");
  document.getElementById("sp").textContent = r.sp500 ? fmt(r.sp500) : "—";

  const posBody = document.querySelector("#pos-tbl tbody");
  posBody.innerHTML = r.positions.map(p => {
    const cls = (p.unrealized_pl || 0) >= 0 ? "pos" : "neg";
    const label = p.type === "stock" ? p.type :
                  `${p.type.toUpperCase()} ${p.strike}/${p.expiry}`;
    return `<tr><td>${p.ticker}</td><td>${label}</td>
      <td class="num">${fmt(p.qty,4)}</td>
      <td class="num">${fmt(p.avg_cost)}</td>
      <td class="num">${fmt(p.current_price)}</td>
      <td class="num ${cls}">${fmt(p.unrealized_pl)}</td></tr>`;
  }).join("") || `<tr><td colspan="6" class="muted">no positions</td></tr>`;

  const trBody = document.querySelector("#trades-tbl tbody");
  trBody.innerHTML = r.trades.map(t => {
    const cls = t.action.startsWith("SELL") ? "sell" : "buy";
    return `<tr><td>${dt(t.timestamp)}</td>
      <td><span class="pill ${cls}">${t.action}</span></td>
      <td>${t.ticker}</td>
      <td class="num">${fmt(t.qty,4)}</td>
      <td class="num">${fmt(t.price)}</td>
      <td class="muted">${(t.reason||"").slice(0,80)}</td></tr>`;
  }).join("") || `<tr><td colspan="6" class="muted">no trades</td></tr>`;

  const dBody = document.querySelector("#dec-tbl tbody");
  dBody.innerHTML = r.decisions.map(d => {
    let reason = "";
    try {
      const j = JSON.parse(d.reasoning || "{}");
      reason = (j.decision && j.decision.reasoning) || j.detail || "";
    } catch (_) { reason = d.reasoning || ""; }
    return `<tr><td>${dt(d.timestamp)}</td>
      <td>${d.market_open ? "yes" : "no"}</td>
      <td class="num">${d.signal_count}</td>
      <td>${(d.action_taken||"").slice(0,40)}</td>
      <td class="num">${fmt(d.portfolio_value)}</td>
      <td class="muted">${reason.slice(0,140)}</td></tr>`;
  }).join("") || `<tr><td colspan="6" class="muted">no decisions yet</td></tr>`;

  const labels = r.equity.map(p => dt(p.timestamp));
  const values = r.equity.map(p => p.total_value);
  const sp     = r.equity.map(p => p.sp500_price);
  if (!chart) {
    chart = new Chart(document.getElementById("eq"), {
      type: "line",
      data: { labels, datasets: [
        { label: "Equity", data: values, borderColor: "#42a5f5",
          backgroundColor: "rgba(66,165,245,0.08)", fill: true, tension: 0.18, borderWidth: 2, pointRadius: 0 },
        { label: "S&P 500 (raw)", data: sp, borderColor: "#ffb74d",
          backgroundColor: "rgba(255,183,77,0)", borderDash: [4,4], borderWidth: 1, pointRadius: 0, yAxisID: "y2" },
      ]},
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { color: "#cfd8dc" }}},
        scales: {
          x: { ticks: { color: "#78909c", maxTicksLimit: 8 }, grid: { color: "#1b2229" }},
          y: { ticks: { color: "#cfd8dc" }, grid: { color: "#1b2229" }},
          y2:{ position: "right", ticks: { color: "#78909c" }, grid: { display: false }}
        }
      }
    });
  } else {
    chart.data.labels = labels;
    chart.data.datasets[0].data = values;
    chart.data.datasets[1].data = sp;
    chart.update("none");
  }
}

// ───────── Backtests pane ─────────
let btLoaded = false;
let btChart;
let btRuns = [];
let btPollTimer = null;
let btSelectedRunId = null;          // currently highlighted run
let btHiddenRuns = new Set();        // run_ids hidden from chart
let btLastUpdated = null;            // ms epoch
let btSortKey = "run_id", btSortDir = 1;
let btDetailSubtab = "trades";

function btRunColor(runId, idx) { return RUN_COLORS[idx % RUN_COLORS.length]; }
function hexToRgba(hex, a) {
  const h = hex.replace("#","");
  const r = parseInt(h.slice(0,2),16), g = parseInt(h.slice(2,4),16), b = parseInt(h.slice(4,6),16);
  return `rgba(${r},${g},${b},${a})`;
}

async function loadBacktests() {
  try {
    const r = await fetch("/api/backtests").then(r => r.json());
    btRuns = r.runs || [];
    btLastUpdated = Date.now();
    btLoaded = true;
    renderBacktests();
  } catch (e) {
    console.error(e);
  } finally {
    if (btPollTimer) clearTimeout(btPollTimer);
    btPollTimer = setTimeout(loadBacktests, 5000);
  }
}

function renderBacktests() {
  const total = btRuns.length;
  const completed = btRuns.filter(x => x.status === "complete");
  const running = btRuns.filter(x => x.status === "running");
  const failed = btRuns.filter(x => x.status === "failed");
  const pctDone = total ? (completed.length / total) * 100 : 0;
  document.getElementById("bt-progress-bar").style.width = pctDone + "%";
  let lbl = `${completed.length}/${total || 10} runs complete`;
  if (running.length)  lbl += ` · ${running.length} running`;
  if (failed.length)   lbl += ` · ${failed.length} failed`;
  document.getElementById("bt-progress-label").textContent = lbl;
  document.getElementById("bt-live-indicator").innerHTML =
    running.length ? `<span class="live-dot"></span>live` : `<span style="color:#66bb6a;">●</span> idle`;

  if (completed.length) {
    const avg = completed.reduce((a,b) => a + b.total_return_pct, 0) / completed.length;
    const avgF = completed.reduce((a,b) => a + b.final_value, 0) / completed.length;
    const best = completed.reduce((a,b) => a.final_value > b.final_value ? a : b);
    const worst = completed.reduce((a,b) => a.final_value < b.final_value ? a : b);
    const spy = completed[0].spy_return_pct;
    const beat = completed.filter(x => x.total_return_pct > spy).length;
    const avgEl = document.getElementById("bt-avg");
    avgEl.textContent = (avg >= 0 ? "+" : "") + fmt(avg) + "%";
    avgEl.className = "v " + (avg >= 0 ? "pos" : "neg");
    document.getElementById("bt-avg-final").textContent = dollar(avgF);
    document.getElementById("bt-best").innerHTML =
      `<span class="pos">${dollar(best.final_value)}</span> <span class="muted" style="font-size:12px;">#${best.run_id}</span>`;
    document.getElementById("bt-worst").innerHTML =
      `<span class="${worst.total_return_pct >= 0 ? 'pos' : 'neg'}">${dollar(worst.final_value)}</span> <span class="muted" style="font-size:12px;">#${worst.run_id}</span>`;
    const spyEl = document.getElementById("bt-spy");
    spyEl.textContent = (spy >= 0 ? "+" : "") + fmt(spy) + "%";
    spyEl.className = "v " + (spy >= 0 ? "pos" : "neg");
    document.getElementById("bt-beat").textContent = `${beat} / ${completed.length}`;
  } else {
    ["bt-avg","bt-avg-final","bt-best","bt-worst","bt-spy","bt-beat"].forEach(id =>
      document.getElementById(id).textContent = "—");
  }

  renderLegend();
  renderTable();
  drawBacktestChart();
  tickLastUpdated();
}

function renderLegend() {
  const wrap = document.getElementById("bt-legend");
  const ordered = [...btRuns].sort((a,b) => a.run_id - b.run_id);
  wrap.innerHTML = ordered.map((r, i) => {
    const idx = btRuns.findIndex(x => x.run_id === r.run_id);
    const color = btRunColor(r.run_id, idx);
    const hidden = btHiddenRuns.has(r.run_id);
    const selected = btSelectedRunId === r.run_id;
    const ret = r.total_return_pct;
    const retCls = (ret || 0) >= 0 ? "pos" : "neg";
    const retTxt = (ret == null) ? "—" : ((ret >= 0 ? "+" : "") + fmt(ret) + "%");
    return `<div class="bt-legend-row${hidden ? ' hidden-run' : ''}${selected ? ' selected' : ''}" onclick="selectRun(${r.run_id})">
      <input type="checkbox" ${hidden ? '' : 'checked'} onclick="event.stopPropagation();toggleRun(${r.run_id})">
      <span class="bt-swatch" style="background:${color};"></span>
      <span class="name">Run #${r.run_id}${r.status === 'running' ? ' <span class=\"spinner\" style=\"width:8px;height:8px;border-width:1px;margin:0 0 0 4px;\"></span>' : ''}</span>
      <span class="ret ${retCls}">${retTxt}</span>
    </div>`;
  }).join("") || `<div class="muted" style="font-size:12px;">no runs yet</div>`;
}

function renderTable() {
  const tbody = document.querySelector("#bt-tbl tbody");
  // attach sort handlers
  document.querySelectorAll("#bt-tbl thead th").forEach(th => {
    th.classList.add("sortable-h");
    th.classList.remove("sort-asc","sort-desc");
    if (th.dataset.k === btSortKey) th.classList.add(btSortDir > 0 ? "sort-asc" : "sort-desc");
    th.onclick = () => {
      const k = th.dataset.k;
      if (btSortKey === k) btSortDir = -btSortDir; else { btSortKey = k; btSortDir = 1; }
      renderTable();
    };
  });
  const sorted = [...btRuns].sort((a,b) => {
    const va = a[btSortKey], vb = b[btSortKey];
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    if (typeof va === "number") return (va - vb) * btSortDir;
    return String(va).localeCompare(String(vb)) * btSortDir;
  });
  tbody.innerHTML = sorted.map(r => {
    const isRunning = r.status === "running";
    const isComplete = r.status === "complete";
    const retCls = (r.total_return_pct || 0) >= 0 ? "pos" : "neg";
    const vsCls  = (r.vs_spy_pct || 0) >= 0 ? "pos" : "neg";
    const selected = btSelectedRunId === r.run_id;
    const idx = btRuns.findIndex(x => x.run_id === r.run_id);
    const color = btRunColor(r.run_id, idx);
    const equityCell = isRunning
      ? `<span class="spinner"></span>${dollar(r.final_value)}`
      : dollar(r.final_value);
    const retCell = r.total_return_pct == null
      ? `<span class="muted">—</span>`
      : `<span class="${retCls}">${(r.total_return_pct >= 0 ? "+" : "") + fmt(r.total_return_pct)}%</span>`;
    const vsCell = isComplete
      ? `<span class="${vsCls}">${(r.vs_spy_pct >= 0 ? "+" : "") + fmt(r.vs_spy_pct)}%</span>`
      : `<span class="muted">—</span>`;
    const startTxt = r.started_at ? r.started_at.replace("T"," ").slice(5,16) : "—";
    return `<tr class="bt-row${selected ? ' selected' : ''}" onclick="selectRun(${r.run_id})">
      <td><span class="pill" style="background:${hexToRgba(color,0.18)};color:${color};">#${r.run_id}</span></td>
      <td class="num">${r.seed}</td>
      <td><span class="pill status-${r.status || 'pending'}">${r.status || 'pending'}</span></td>
      <td class="num">${equityCell}</td>
      <td class="num">${retCell}</td>
      <td class="num">${vsCell}</td>
      <td class="num">${r.n_trades || 0}</td>
      <td class="num">${r.n_decisions || 0}</td>
      <td class="muted" style="font-size:12px;">${startTxt}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="9" class="muted">no backtest runs yet — run paper_trader.backtest</td></tr>`;
}

function drawBacktestChart() {
  const dateSet = new Set();
  btRuns.forEach(r => (r.equity_curve||[]).forEach(p => dateSet.add(p.date)));
  const labels = Array.from(dateSet).sort();

  const datasets = btRuns.map((r, i) => {
    const lookup = {};
    (r.equity_curve||[]).forEach(p => lookup[p.date] = p.value);
    let last = 1000;
    const data = labels.map(d => {
      if (lookup[d] != null) { last = lookup[d]; return lookup[d]; }
      return last;
    });
    const isRunning = r.status === "running";
    const color = btRunColor(r.run_id, i);
    const isHidden = btHiddenRuns.has(r.run_id);
    const hasSelection = btSelectedRunId != null;
    const isSelected = btSelectedRunId === r.run_id;
    const dim = hasSelection && !isSelected;
    return {
      label: `Run #${r.run_id}${isRunning ? ' (live)' : ''}`,
      data,
      runId: r.run_id,
      kind: "run",
      borderColor: dim ? hexToRgba(color, 0.2) : color,
      backgroundColor: hexToRgba(color, 0.05),
      borderWidth: isSelected ? 3.5 : (dim ? 1 : 2),
      borderDash: isRunning ? [5, 4] : [],
      pointRadius: 0, pointHoverRadius: 5,
      tension: 0.18, fill: false,
      hidden: isHidden,
    };
  });

  const completed = btRuns.filter(x => x.status === "complete");
  if (completed.length && labels.length > 1) {
    const spy = completed[0].spy_return_pct / 100;
    const spyData = labels.map((d, i) => 1000 * (1 + spy * i / (labels.length - 1)));
    const hasSelection = btSelectedRunId != null;
    datasets.push({
      label: `SPY (${(spy*100).toFixed(2)}%)`,
      data: spyData,
      kind: "spy",
      borderColor: hasSelection ? hexToRgba(SPY_COLOR, 0.25) : SPY_COLOR,
      borderDash: [6,4],
      borderWidth: 2, pointRadius: 0, tension: 0, fill: false,
    });
  }

  if (btChart) btChart.destroy();
  btChart = new Chart(document.getElementById("bt-chart"), {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      onClick: (evt, els, chart) => {
        // Click directly on a line point: highlight that run.
        if (els && els.length) {
          // prefer the topmost matched dataset that's a run
          for (const el of els) {
            const ds = chart.data.datasets[el.datasetIndex];
            if (ds && ds.kind === "run") { selectRun(ds.runId); return; }
          }
        }
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          mode: "index", intersect: false,
          backgroundColor: "rgba(15,20,28,0.95)",
          borderColor: "#2a3a4f", borderWidth: 1,
          titleColor: "#cfd8dc", bodyColor: "#cfd8dc",
          padding: 10, boxPadding: 4,
          itemSort: (a,b) => b.parsed.y - a.parsed.y,
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: ${dollar(ctx.parsed.y)}`,
          },
        },
      },
      scales: {
        x: { ticks: { color: "#78909c", maxTicksLimit: 10 }, grid: { color: "#1b2229" }},
        y: { ticks: { color: "#cfd8dc", callback: v => "$"+v }, grid: { color: "#1b2229" }},
      },
    },
  });
}

function selectRun(runId) {
  btSelectedRunId = (btSelectedRunId === runId) ? null : runId;
  renderLegend();
  renderTable();
  drawBacktestChart();
  if (btSelectedRunId != null) loadRunDetail(btSelectedRunId);
  else closeDetail();
}

function toggleRun(runId) {
  if (btHiddenRuns.has(runId)) btHiddenRuns.delete(runId);
  else btHiddenRuns.add(runId);
  renderLegend();
  drawBacktestChart();
}

function btToggleAll(show) {
  btHiddenRuns = show ? new Set() : new Set(btRuns.map(r => r.run_id));
  renderLegend();
  drawBacktestChart();
}

function closeDetail() {
  document.getElementById("bt-detail").style.display = "none";
  btSelectedRunId = null;
  renderLegend(); renderTable(); drawBacktestChart();
}

function showBtSubtab(name) {
  btDetailSubtab = name;
  document.querySelectorAll(".bt-subpane").forEach(el => el.classList.remove("active"));
  document.querySelectorAll(".bt-tabs a").forEach(el => el.classList.remove("active"));
  document.getElementById("bt-tab-" + name).classList.add("active");
  document.getElementById("bt-tab-" + name + "-link").classList.add("active");
}

async function loadRunDetail(runId) {
  const wrap = document.getElementById("bt-detail");
  document.getElementById("bt-detail-id").textContent = "#" + runId;
  wrap.style.display = "block";
  const r = await fetch(`/api/backtests/${runId}`).then(r => r.json());
  const meta = [];
  if (r.seed != null) meta.push(`seed ${r.seed}`);
  if (r.start_date) meta.push(`${r.start_date} → ${r.end_date || '…'}`);
  if (r.status) meta.push(r.status);
  if (r.n_trades != null) meta.push(`${r.n_trades} trades`);
  if (r.n_decisions != null) meta.push(`${r.n_decisions} decisions`);
  if (r.notes) meta.push(r.notes);
  document.getElementById("bt-detail-meta").textContent = meta.join(" · ");

  const tBody = document.querySelector("#bt-trades-tbl tbody");
  tBody.innerHTML = (r.trades || []).map(t => {
    const cls = (t.action||"").startsWith("SELL") ? "sell" : "buy";
    return `<tr><td>${t.sim_date || ''}</td>
      <td><span class="pill ${cls}">${t.action || ''}</span></td>
      <td>${t.ticker || ''}</td>
      <td class="num">${fmt(t.qty,4)}</td>
      <td class="num">${fmt(t.price)}</td>
      <td class="num">${fmt(t.value)}</td>
      <td class="muted">${(t.reason||"").slice(0,140)}</td></tr>`;
  }).join("") || `<tr><td colspan="7" class="muted">no trades</td></tr>`;

  const dBody = document.querySelector("#bt-decisions-tbl tbody");
  dBody.innerHTML = (r.decisions || []).map(d => {
    return `<tr><td>${d.sim_date || ''}</td>
      <td>${d.action || ''}</td>
      <td>${d.ticker || ''}</td>
      <td><span class="pill">${d.status || ''}</span></td>
      <td class="muted">${(d.detail||"").slice(0,140)}</td>
      <td class="num">${fmt(d.total_value)}</td></tr>`;
  }).join("") || `<tr><td colspan="6" class="muted">no decisions</td></tr>`;
}

function tickLastUpdated() {
  const el = document.getElementById("bt-last-updated");
  if (!el) return;
  if (btLastUpdated == null) { el.textContent = "last update: —"; return; }
  const s = Math.floor((Date.now() - btLastUpdated)/1000);
  el.textContent = `last updated ${s}s ago`;
}
setInterval(tickLastUpdated, 1000);

// ───────── Signal feed (from Digital Intern) ─────────
async function refreshSignals() {
  const ul = document.getElementById("signal-feed");
  try {
    const r = await fetch("http://10.19.203.44:8080/api/articles?limit=3");
    if (!r.ok) {
      ul.innerHTML = `<li class="muted">signal feed unavailable (HTTP ${r.status})</li>`;
      return;
    }
    const arts = await r.json();
    if (!Array.isArray(arts) || !arts.length) {
      ul.innerHTML = `<li class="muted">no signals yet</li>`;
      return;
    }
    ul.innerHTML = arts.map(a => {
      const score = (a.score != null ? a.score : 0).toFixed(1);
      const url = a.url || "#";
      const title = (a.title || "(no title)").replace(/</g,"&lt;");
      const src = (a.source || "").replace(/</g,"&lt;");
      return `<li style="padding:6px 0;border-bottom:1px solid #1b2229;">
        <span class="pill" style="background:#1f3a4d;color:#82b1ff;margin-right:8px;">${score}</span>
        <a href="${url}" target="_blank" rel="noopener" style="color:#cfd8dc;text-decoration:none">${title}</a>
        <span class="muted" style="margin-left:6px;">· ${src}</span>
      </li>`;
    }).join("");
  } catch (e) {
    ul.innerHTML = `<li class="muted">digital intern unreachable</li>`;
  }
}

// ───────── boot ─────────
refresh();
refreshSignals();
setInterval(refresh, 15_000);
setInterval(refreshSignals, 30_000);
showTab(INITIAL_TAB || "trader");
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(TEMPLATE, initial_tab="trader")


@app.route("/backtests")
def backtests_page():
    return render_template_string(TEMPLATE, initial_tab="backtests")


@app.route("/api/state")
def state():
    store = get_store()
    pf = store.get_portfolio()
    positions = store.open_positions()
    trades = store.recent_trades(40)
    decisions = store.recent_decisions(20)
    eq = store.equity_curve(500)
    sp = eq[-1]["sp500_price"] if eq else None
    from datetime import datetime, timezone
    return jsonify({
        "now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "portfolio": pf,
        "positions": positions,
        "trades": trades,
        "decisions": decisions,
        "equity": eq,
        "sp500": sp,
    })


@app.route("/api/portfolio")
def portfolio_api():
    """Compact public read of the portfolio — consumed by Digital Intern's dashboard."""
    store = get_store()
    pf = store.get_portfolio()
    return jsonify({
        "total_value": pf.get("total_value"),
        "cash": pf.get("cash"),
        "starting_value": 1000.0,
    })


@app.route("/api/backtests")
def backtests_api():
    from datetime import datetime, timezone
    try:
        from .backtest import BacktestStore
        store = BacktestStore()
        runs = store.all_runs()
        completed = [r for r in runs if r.get("status") == "complete"]
        spy_baseline = completed[0].get("spy_return_pct") if completed else None
        return jsonify({
            "runs": runs,
            "spy_baseline": spy_baseline,
            "last_updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
    except Exception as e:
        return jsonify({"runs": [], "error": str(e)})


@app.route("/api/backtests/<int:run_id>")
def backtest_detail(run_id: int):
    try:
        from .backtest import BacktestStore
        store = BacktestStore()
        detail = store.run_detail(run_id)
        if not detail:
            return jsonify({"error": "not found"}), 404
        return jsonify(detail)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/backtests/<int:run_id>/trades")
def backtest_trades(run_id: int):
    try:
        from .backtest import BacktestStore
        store = BacktestStore()
        detail = store.run_detail(run_id)
        if not detail:
            return jsonify({"error": "not found"}), 404
        return jsonify({"run_id": run_id, "trades": detail.get("trades", [])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/backtests/<int:run_id>/decisions")
def backtest_decisions(run_id: int):
    try:
        from .backtest import BacktestStore
        store = BacktestStore()
        detail = store.run_detail(run_id)
        if not detail:
            return jsonify({"error": "not found"}), 404
        return jsonify({"run_id": run_id, "decisions": detail.get("decisions", [])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def run(host: str = "0.0.0.0", port: int = 8090):
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    run()
