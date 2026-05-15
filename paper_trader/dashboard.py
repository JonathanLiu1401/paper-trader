"""Flask dashboard at :8090 — portfolio chart, trade log, positions, decisions, backtests."""
from __future__ import annotations

import re
import sqlite3
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

from .store import get_store

app = Flask(__name__)


# Static sector classification for analytics + sector-pulse cards.
# Keyed by the symbols we actually use in the watchlist + portfolio.
SECTOR_MAP = {
    # Semis (cash)
    "NVDA": "semis", "AMD": "semis", "MU": "semis", "AMAT": "semis",
    "LRCX": "semis", "KLAC": "semis", "TSM": "semis", "ASML": "semis",
    "MRVL": "semis", "SMH": "semis", "SOXX": "semis",
    "DRAM": "semis", "SNDU": "semis",
    # Semis leveraged
    "SOXL": "semis_lev", "SOXS": "semis_lev", "NVDU": "semis_lev",
    "MUU": "semis_lev",
    # Optical / networking
    "LITE": "optical", "LNOK": "optical",
    # Broad market
    "SPY": "broad", "QQQ": "broad", "VOO": "broad", "VTI": "broad",
    # Broad leveraged
    "TQQQ": "broad_lev", "UPRO": "broad_lev", "SPXL": "broad_lev",
    "QLD": "broad_lev", "SSO": "broad_lev", "UDOW": "broad_lev",
    "URTY": "broad_lev", "TNA": "broad_lev",
    "SPXS": "broad_lev", "SQQQ": "broad_lev",
    # Tech / FAANG
    "AAPL": "tech", "MSFT": "tech", "META": "tech", "GOOG": "tech",
    "GOOGL": "tech", "AMZN": "tech", "TSLA": "tech", "NFLX": "tech",
    "TECL": "tech_lev", "TECS": "tech_lev", "FNGU": "tech_lev",
    "FNGD": "tech_lev", "MSFU": "tech_lev", "AMZU": "tech_lev",
    "GOOGU": "tech_lev", "METAU": "tech_lev", "TSLL": "tech_lev",
    "CONL": "crypto_lev", "BITU": "crypto_lev", "ETHU": "crypto_lev",
    # Sector leveraged
    "LABU": "bio_lev", "CURE": "health_lev",
    "FAS": "fin_lev", "DPST": "fin_lev",
    "NAIL": "housing_lev", "UTSL": "util_lev",
    "DFEN": "defense_lev",
}

# Sector-pulse card focuses on the user's actual interest areas.
SECTOR_PULSE_TICKERS = [
    "MU", "NVDA", "AMD", "TSM", "AMAT", "LRCX", "KLAC", "MRVL", "ASML",
    "SMH", "SOXX", "SOXL",
    "LITE", "LNOK", "DRAM", "SNDU", "MUU",
]


def _classify(ticker: str) -> str:
    return SECTOR_MAP.get(ticker.upper(), "other")


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
    @import url('https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=Outfit:wght@400;500;600;700&family=DM+Mono:ital,wght@0,400;0,500;1,400&display=swap');
    :root {
      color-scheme: dark;
      --bg: #0c0d0f;
      --bg-panel: #111316;
      --bg-elevated: #17191d;
      --bg-hover: #1c1f24;
      --bg-input: #0e1012;
      --border: rgba(255,255,255,0.07);
      --border-strong: rgba(255,255,255,0.13);
      --text: #dde1e7;
      --text-secondary: #8b929d;
      --text-muted: #50565f;
      --amber: #f0b429;
      --amber-dim: rgba(240,180,41,0.12);
      --cyan: #0acdff;
      --cyan-dim: rgba(10,205,255,0.12);
      --green: #00c896;
      --green-dim: rgba(0,200,150,0.12);
      --red: #ff4455;
      --red-dim: rgba(255,68,85,0.12);
      --blue: #4d9eff;
      --blue-dim: rgba(77,158,255,0.12);
      --yellow: #fbbf24;
      --yellow-dim: rgba(251,191,36,0.12);
      --pink: #f472b6;
      --font-sans: 'Outfit', system-ui, sans-serif;
      --font-mono: 'DM Mono', 'JetBrains Mono', monospace;
      --font-display: 'Syne', system-ui, sans-serif;
      --radius: 8px;
      --radius-sm: 5px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; padding: 0;
      font-family: var(--font-sans);
      background: var(--bg); color: var(--text);
      font-size: 15px; line-height: 1.5;
    }
    .brand, h1, h2, h3 { font-family: var(--font-display); }
    .page-content { padding: 24px; max-width: 1600px; }
    .topbar {
      background: var(--bg-panel);
      border-bottom: 1px solid var(--border);
      padding: 0 20px; height: 48px;
      display: flex; align-items: center; gap: 2px;
      position: sticky; top: 0; z-index: 100; margin: 0;
    }
    .brand {
      font-weight: 700; color: var(--amber);
      font-size: 13px; letter-spacing: 0.08em;
      text-transform: uppercase;
      margin-right: 16px; flex-shrink: 0;
    }
    .topbar a {
      color: var(--text-secondary); text-decoration: none;
      font-size: 13px; font-weight: 500;
      padding: 5px 12px; border-radius: var(--radius-sm);
      transition: color 0.15s, background 0.15s;
      white-space: nowrap;
    }
    .topbar a:hover { color: var(--text); background: var(--bg-hover); }
    .topbar a.active { color: var(--amber); background: var(--amber-dim); }
    h1 { margin: 0 0 4px; font-size: 22px; font-weight: 600; color: var(--text); }
    .sub { color: var(--text-secondary); font-size: 13px; margin-bottom: 20px; }
    nav.tabs {
      display: flex; gap: 2px; margin-bottom: 18px;
      border-bottom: 1px solid var(--border);
    }
    nav.tabs a {
      padding: 8px 16px; color: var(--text-secondary); text-decoration: none;
      border-bottom: 2px solid transparent; font-size: 13px; font-weight: 500;
      cursor: pointer; transition: color 0.15s; margin-bottom: -1px;
    }
    nav.tabs a.active { color: var(--amber); border-bottom-color: var(--amber); }
    nav.tabs a:hover { color: var(--text); }
    .tab-pane { display: none; }
    .tab-pane.active { display: block; }
    .grid {
      display: grid; gap: 18px;
      grid-template-columns: 1fr 1fr;
    }
    @media (max-width: 980px) { .grid { grid-template-columns: 1fr; } }
    .card {
      background: var(--bg-panel); border: 1px solid var(--border);
      border-radius: var(--radius); padding: 18px 20px;
    }
    .card h2 {
      margin: 0 0 14px; font-size: 11px; font-weight: 600;
      color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.1em;
    }
    .stat-row { display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 12px; }
    .stat { flex: 1 1 120px; }
    .stat .v {
      font-family: var(--font-mono);
      font-size: 24px; color: var(--text); font-weight: 500;
      font-variant-numeric: tabular-nums;
    }
    .stat .l { color: var(--text-muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; }
    .pos, .pl { color: var(--green); }
    .neg { color: var(--red); }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th {
      text-align: left; padding: 0 10px 10px;
      font-size: 11px; font-weight: 600; color: var(--text-muted);
      text-transform: uppercase; letter-spacing: 0.08em;
      border-bottom: 1px solid var(--border-strong);
    }
    td {
      padding: 8px 10px; border-bottom: 1px solid var(--border);
      font-size: 13px;
    }
    td.num {
      text-align: right;
      font-family: var(--font-mono);
      font-variant-numeric: tabular-nums;
    }
    tr:hover td { background: var(--bg-hover); }
    .muted { color: var(--text-secondary); }
    canvas { max-height: 280px; }
    .pill {
      display: inline-flex; align-items: center;
      padding: 2px 8px; border-radius: 4px;
      background: var(--bg-elevated); color: var(--text-secondary);
      font-size: 11px; font-weight: 500; letter-spacing: 0.04em;
      font-family: var(--font-sans);
    }
    .pill.buy { background: var(--green-dim); color: var(--green); }
    .pill.sell { background: var(--red-dim); color: var(--red); }
    .pill.hold { background: var(--bg-elevated); color: var(--text-secondary); }
    .pill.run { background: var(--blue-dim); color: var(--blue); }
    .pill.status-running  { background: var(--blue-dim); color: var(--blue); }
    .pill.status-complete { background: var(--green-dim); color: var(--green); }
    .pill.status-failed   { background: var(--red-dim); color: var(--red); }
    .pill.status-pending  { background: var(--bg-elevated); color: var(--text-secondary); }
    .spinner {
      display: inline-block; width: 10px; height: 10px;
      border: 2px solid var(--border-strong); border-top-color: var(--cyan);
      border-radius: 50%; animation: spin 0.8s linear infinite;
      vertical-align: middle; margin-right: 6px;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    .progress-wrap {
      margin: 8px 0; height: 4px; background: var(--bg-elevated);
      border-radius: 4px; overflow: hidden;
    }
    .progress-bar {
      height: 100%; background: linear-gradient(90deg, var(--amber), var(--cyan));
      transition: width 0.4s ease;
    }
    .progress-label { font-size: 11px; color: var(--text-muted); margin-bottom: 4px; }
    tr.bt-row { cursor: pointer; }
    tr.bt-row:hover td { background: var(--bg-hover); }
    tr.bt-row.best td { background: var(--green-dim); }
    tr.bt-row.beat td:first-child { border-left: 2px solid var(--green); }
    tr.bt-row.miss td:first-child { border-left: 2px solid var(--red); }
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
    .bt-sidebar { position: sticky; top: 62px; max-height: calc(100vh - 78px); overflow-y: auto; }
    .bt-sidebar h2 { margin: 0; }
    .bt-legend-row {
      display: flex; align-items: center; gap: 8px; padding: 6px 4px;
      border-bottom: 1px solid var(--border); cursor: pointer; user-select: none;
      transition: background 0.15s;
    }
    .bt-legend-row:hover { background: var(--bg-hover); }
    .bt-legend-row.selected { background: var(--bg-elevated); }
    .bt-legend-row.hidden-run { opacity: 0.35; }
    .bt-legend-row input[type=checkbox] { accent-color: var(--cyan); margin: 0; }
    .bt-swatch {
      width: 12px; height: 12px; border-radius: 3px; flex: 0 0 12px;
    }
    .bt-legend-row .name { flex: 1; font-size: 13px; color: var(--text); }
    .bt-legend-row .ret { font-size: 11px; font-variant-numeric: tabular-nums; font-family: var(--font-mono); }
    .bt-btn {
      background: var(--bg-elevated); color: var(--text);
      border: 1px solid var(--border-strong); border-radius: var(--radius-sm);
      padding: 3px 8px; font-size: 11px; cursor: pointer;
      text-transform: uppercase; letter-spacing: 0.5px;
      font-family: var(--font-sans);
    }
    .bt-btn:hover { background: var(--bg-hover); }
    .bt-tabs {
      display: flex; gap: 2px; margin-bottom: 12px;
      border-bottom: 1px solid var(--border);
    }
    .bt-tabs a {
      padding: 8px 14px; color: var(--text-secondary); cursor: pointer; font-size: 13px;
      border-bottom: 2px solid transparent; font-weight: 500;
    }
    .bt-tabs a.active { color: var(--amber); border-bottom-color: var(--amber); }
    .bt-subpane { display: none; }
    .bt-subpane.active { display: block; }
    tr.bt-row.selected td { background: var(--bg-elevated) !important; }
    .pill.status-running { animation: pulse 1.5s ease-in-out infinite; }
    @keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:0.55;} }
    .live-dot {
      display: inline-block; width: 7px; height: 7px; border-radius: 50%;
      background: var(--green); margin-right: 6px; animation: pulse 1.5s infinite;
    }
    th.sortable-h { cursor: pointer; user-select: none; }
    th.sortable-h:hover { color: var(--text); }
    th.sortable-h.sort-asc::after  { content: " ▲"; font-size: 9px; }
    th.sortable-h.sort-desc::after { content: " ▼"; font-size: 9px; }
    select, input[type="text"], input[type="number"] {
      background: var(--bg-input); color: var(--text);
      border: 1px solid var(--border-strong); border-radius: var(--radius-sm);
      padding: 6px 10px; font-size: 13px; font-family: var(--font-sans);
    }
    button, .btn {
      background: var(--bg-elevated); color: var(--text);
      border: 1px solid var(--border-strong); border-radius: var(--radius-sm);
      padding: 6px 14px; font-size: 13px; font-family: var(--font-sans);
      cursor: pointer; transition: background 0.15s;
    }
    button:hover, .btn:hover { background: var(--bg-hover); }
    button.primary, .btn-primary {
      background: var(--amber-dim);
      border-color: rgba(240,180,41,0.3);
      color: var(--amber);
    }
    /* === Mobile-first responsive additions ============================== */
    .nav-hamburger {
      display: none; flex-direction: column; justify-content: space-between;
      width: 32px; height: 22px; background: none; border: none; cursor: pointer;
      padding: 0; margin-left: auto;
    }
    .nav-hamburger span {
      display: block; height: 2px; background: var(--text); border-radius: 2px;
      transition: all 0.2s;
    }
    .nav-drawer {
      position: fixed; top: 0; left: -280px; width: 280px; height: 100vh;
      background: var(--bg-panel); border-right: 1px solid #1e2028;
      z-index: 1000; transition: left 0.25s ease; overflow-y: auto; padding: 20px 0;
    }
    .nav-drawer.open { left: 0; }
    .nav-drawer-header {
      font-family: var(--font-display); font-weight: 700; color: var(--amber);
      font-size: 13px; letter-spacing: 0.1em; padding: 0 20px 20px;
      border-bottom: 1px solid #1e2028; margin-bottom: 8px;
    }
    .nav-drawer a {
      display: block; padding: 12px 20px; color: var(--text-secondary);
      text-decoration: none; font-size: 14px; transition: all 0.15s;
    }
    .nav-drawer a:hover, .nav-drawer a.active {
      color: var(--text); background: var(--bg-elevated);
    }
    .nav-overlay {
      display: none; position: fixed; inset: 0;
      background: rgba(0,0,0,0.6); z-index: 999;
    }
    .nav-overlay.open { display: block; }
    .bottom-nav {
      display: none; position: fixed; bottom: 0; left: 0; right: 0; height: 64px;
      background: var(--bg-panel); border-top: 1px solid #1e2028;
      grid-template-columns: repeat(5, 1fr); z-index: 200; align-items: stretch;
    }
    .bottom-tab {
      display: flex; flex-direction: column; align-items: center;
      justify-content: center; gap: 4px; color: var(--text-secondary);
      text-decoration: none; font-size: 10px; min-height: 44px; transition: color 0.15s;
    }
    .bottom-tab svg { width: 20px; height: 20px; }
    .bottom-tab.active, .bottom-tab:hover { color: var(--amber); }
    .table-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
    .table-scroll table { min-width: 500px; }
    @media (max-width: 768px) {
      .topbar-nav { display: none; }
      .nav-hamburger { display: flex; }
      body { font-size: 14px; }
      button, .btn, a.btn, [role="button"] { min-height: 44px; min-width: 44px; }
    }
    @media (max-width: 480px) {
      body { padding-bottom: 72px; }
      .bottom-nav { display: grid; }
      .topbar { padding: 0 16px; }
      .card { min-height: auto !important; padding: 14px 16px; }
      .grid, .grid-2, .grid2 { grid-template-columns: 1fr !important; }
      [style*="max-height: 520px"],
      [style*="max-height:520px"] { max-height: 60vh !important; }
      table { font-size: 12px; }
      th, td { padding: 8px 10px; }
    }
  </style>
</head>
<body>
  <nav class="topbar">
    <span class="brand">◈ TRADING STACK</span>
    <span class="topbar-nav" style="display:flex;align-items:center;gap:2px;">
      <a href="/">Command Center</a>
      <a href="/intern/">Digital Intern</a>
      <a href="/trader/" class="{% if initial_tab != 'backtests' %}active{% endif %}">Paper Trader</a>
      <a href="/trader/backtests" class="{% if initial_tab == 'backtests' %}active{% endif %}">Backtests</a>
      <a href="/backtests/compare">Compare</a>
      <a href="/journal">Journal</a>
      <a href="/ops/">Ops View</a>
      <a href="/intern/chat">Chat</a>
      <a href="/system/">System</a>
    </span>
    <button class="nav-hamburger" id="navToggle" aria-label="Menu">
      <span></span><span></span><span></span>
    </button>
  </nav>
  <div class="nav-drawer" id="navDrawer">
    <div class="nav-drawer-header">◈ TRADING STACK</div>
    <a href="/">Command Center</a>
    <a href="/intern/">Digital Intern</a>
    <a href="/trader/" class="{% if initial_tab != 'backtests' %}active{% endif %}">Paper Trader</a>
    <a href="/trader/backtests" class="{% if initial_tab == 'backtests' %}active{% endif %}">Backtests</a>
    <a href="/backtests/compare">Compare</a>
    <a href="/journal">Journal</a>
    <a href="/ops/">Ops View</a>
    <a href="/intern/chat">Chat</a>
    <a href="/system/">System</a>
  </div>
  <div class="nav-overlay" id="navOverlay"></div>

  <div class="page-content">
  <h1>Paper Trader</h1>
  <div class="sub" id="hb">loading…</div>

  <div class="card" style="margin-bottom:18px;">
    <h2 style="display:flex;justify-content:space-between;align-items:center;">
      <span>Signal Feed — Digital Intern</span>
      <a href="/intern/" style="font-size:11px;color:#0acdff;text-decoration:none;text-transform:none;letter-spacing:normal">View All Signals →</a>
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

    <!-- ─── Equity Curve (pinned top) ─── -->
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

    <!-- ─── Daily Briefing (futures + market countdown + urgent news) ─── -->
    <div class="card" id="briefing-card" style="margin-bottom:18px;">
      <h2 style="display:flex;justify-content:space-between;align-items:center;">
        <span><span id="briefing-dot" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#8b929d;margin-right:8px;"></span>Daily briefing</span>
        <span class="muted" id="briefing-asof" style="font-size:11px;text-transform:none;letter-spacing:normal;">—</span>
      </h2>
      <div id="briefing-status" style="font-size:14px;color:#dde1e7;margin-bottom:12px;">loading…</div>
      <div id="briefing-futures" style="display:flex;flex-wrap:wrap;gap:14px;margin-bottom:14px;font-size:13px;"></div>
      <div style="font-size:11px;color:#8b929d;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;">Urgent overnight news</div>
      <ul id="briefing-urgent" style="margin:0;padding:0;list-style:none;font-size:13px;"></ul>
    </div>

    <!-- ─── Trade Suggestions (co-pilot) ─── -->
    <div class="card" style="margin-bottom:18px;">
      <h2 style="display:flex;justify-content:space-between;align-items:center;">
        <span>Trade suggestions <span class="muted" style="font-size:11px;text-transform:none;letter-spacing:normal;font-weight:normal;">— co-pilot, not auto-executed</span></span>
        <span class="muted" id="sug-meta" style="font-size:11px;text-transform:none;letter-spacing:normal;">—</span>
      </h2>
      <div id="sug-summary" style="font-size:12px;color:#8b929d;margin-bottom:10px;">loading…</div>
      <table id="sug-tbl" style="font-size:13px;">
        <thead><tr>
          <th>action</th><th>ticker</th><th class="num">conv.</th>
          <th class="num">price</th><th class="num">qty</th>
          <th class="num">news</th><th class="num">RSI</th>
          <th>reasons</th><th>headline</th>
        </tr></thead><tbody><tr><td colspan="9" class="muted">loading…</td></tr></tbody>
      </table>
    </div>

    <!-- ─── Risk panel (concentration / leverage / age / shock) ─── -->
    <div class="card" style="margin-bottom:18px;">
      <h2>Risk panel</h2>
      <div class="stat-row" style="margin-bottom:14px;">
        <div class="stat"><div class="l">concentration top1</div><div class="v" id="risk-top1">—</div></div>
        <div class="stat"><div class="l">top3 weight</div><div class="v" id="risk-top3">—</div></div>
        <div class="stat"><div class="l">leveraged %</div><div class="v" id="risk-lev">—</div></div>
        <div class="stat"><div class="l">SPY -3% shock</div><div class="v" id="risk-shock">—</div></div>
        <div class="stat"><div class="l">median age (d)</div><div class="v" id="risk-age">—</div></div>
        <div class="stat"><div class="l">stale positions</div><div class="v" id="risk-stale-n">—</div></div>
      </div>
      <div id="risk-stale-list" style="font-size:12px;color:#dde1e7;"></div>
    </div>

    <!-- ─── Earnings Risk ─── -->
    <div class="card" style="margin-bottom:18px;">
      <h2 style="display:flex;justify-content:space-between;align-items:center;">
        <span>Earnings radar <span class="muted" style="font-size:11px;text-transform:none;letter-spacing:normal;font-weight:normal;">— scheduled gap risk on holdings &amp; watchlist</span></span>
        <span class="muted" id="er-asof" style="font-size:11px;text-transform:none;letter-spacing:normal;">—</span>
      </h2>
      <div class="muted" id="er-meta" style="font-size:11px;margin-bottom:8px;">—</div>
      <ul id="er-list" style="margin:0;padding:0;list-style:none;font-size:13px;">
        <li class="muted">loading…</li>
      </ul>
    </div>

    <!-- ─── Portfolio Greeks (options exposure) ─── -->
    <div class="card" id="greeks-card" style="margin-bottom:18px;display:none;">
      <h2 style="display:flex;justify-content:space-between;align-items:center;">
        <span>Portfolio Greeks <span class="muted" style="font-size:11px;text-transform:none;letter-spacing:normal;font-weight:normal;">— Black-Scholes, live IV from yfinance</span></span>
        <span class="muted" id="gk-asof" style="font-size:11px;text-transform:none;letter-spacing:normal;">—</span>
      </h2>
      <div class="stat-row" style="margin-bottom:14px;">
        <div class="stat"><div class="l">net delta</div><div class="v" id="gk-delta">—</div></div>
        <div class="stat"><div class="l">net gamma</div><div class="v" id="gk-gamma">—</div></div>
        <div class="stat"><div class="l">theta / day</div><div class="v" id="gk-theta">—</div></div>
        <div class="stat"><div class="l">vega / 1% IV</div><div class="v" id="gk-vega">—</div></div>
        <div class="stat"><div class="l">gross $ notional</div><div class="v" id="gk-notional">—</div></div>
        <div class="stat"><div class="l">delta % of port</div><div class="v" id="gk-deltapct">—</div></div>
      </div>
      <table id="gk-tbl" style="font-size:13px;">
        <thead><tr>
          <th>ticker</th><th>type</th><th class="num">qty</th>
          <th class="num">expiry / strike</th><th class="num">IV</th>
          <th class="num">Δ delta</th><th class="num">Γ</th>
          <th class="num">Θ / day</th><th class="num">ν / 1%</th>
        </tr></thead><tbody><tr><td colspan="9" class="muted">no option positions</td></tr></tbody>
      </table>
    </div>

    <!-- ─── DecisionScorer per-position predictions ─── -->
    <div class="card" id="scorer-card" style="margin-bottom:18px;">
      <h2 style="display:flex;justify-content:space-between;align-items:center;">
        <span>ML scorer · per-position outlook <span class="muted" style="font-size:11px;text-transform:none;letter-spacing:normal;font-weight:normal;">— predicted 5-day forward return from DecisionScorer MLP</span></span>
        <span class="muted" id="sc-asof" style="font-size:11px;text-transform:none;letter-spacing:normal;">—</span>
      </h2>
      <div class="muted" id="sc-meta" style="font-size:11px;margin-bottom:8px;">loading…</div>
      <table id="sc-tbl" style="font-size:13px;">
        <thead><tr>
          <th>ticker</th>
          <th class="num">pred 5d</th>
          <th>verdict</th>
          <th class="num">RSI</th>
          <th class="num">MACD</th>
          <th class="num">mom 5d</th>
          <th class="num">mom 20d</th>
          <th class="num">news</th>
        </tr></thead>
        <tbody><tr><td colspan="8" class="muted">no open stock positions</td></tr></tbody>
      </table>
    </div>

    <!-- ─── Position Thesis Cards (new 2026-05-15) ─── -->
    <div class="card" id="thesis-card" style="margin-bottom:18px;">
      <h2 style="display:flex;justify-content:space-between;align-items:center;">
        <span>Position thesis <span class="muted" style="font-size:11px;text-transform:none;letter-spacing:normal;font-weight:normal;">— per-holding integrated view: news, scorer, technicals, last decision, verdict</span></span>
        <span class="muted" id="th-asof" style="font-size:11px;text-transform:none;letter-spacing:normal;">—</span>
      </h2>
      <div class="muted" id="th-meta" style="font-size:11px;margin-bottom:10px;">loading…</div>
      <div id="th-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:12px;">
        <div class="muted">loading…</div>
      </div>
    </div>

    <!-- ─── Drawdown Anatomy (new 2026-05-15) ─── -->
    <div class="card" id="dd-card" style="margin-bottom:18px;">
      <h2 style="display:flex;justify-content:space-between;align-items:center;">
        <span>Drawdown anatomy <span class="muted" style="font-size:11px;text-transform:none;letter-spacing:normal;font-weight:normal;">— current DD from peak with per-position contribution</span></span>
        <span class="muted" id="dd-asof" style="font-size:11px;text-transform:none;letter-spacing:normal;">—</span>
      </h2>
      <div class="stat-row" style="margin-bottom:14px;">
        <div class="stat"><div class="l">current equity</div><div class="v" id="dd-current">—</div></div>
        <div class="stat"><div class="l">peak equity</div><div class="v" id="dd-peak">—</div></div>
        <div class="stat"><div class="l">drawdown</div><div class="v" id="dd-pct">—</div></div>
        <div class="stat"><div class="l">trough</div><div class="v" id="dd-trough">—</div></div>
        <div class="stat"><div class="l">time in DD</div><div class="v" id="dd-hours">—</div></div>
        <div class="stat"><div class="l">recovered</div><div class="v" id="dd-rec">—</div></div>
      </div>
      <div style="font-size:13px;color:#dde1e7;margin-bottom:8px;text-transform:uppercase;letter-spacing:0.5px;">Per-position contribution</div>
      <table id="dd-tbl" style="font-size:13px;">
        <thead><tr>
          <th>ticker</th><th class="num">qty</th><th class="num">cost</th>
          <th class="num">px</th><th class="num">P/L $</th><th class="num">P/L %</th>
          <th>drag</th>
        </tr></thead>
        <tbody><tr><td colspan="7" class="muted">loading…</td></tr></tbody>
      </table>
    </div>

    <!-- ─── Confidence Calibration + Signal Attribution (new 2026-05-15) ─── -->
    <div class="card" id="cal-card" style="margin-bottom:18px;">
      <h2 style="display:flex;justify-content:space-between;align-items:center;">
        <span>Confidence calibration &amp; signal attribution <span class="muted" style="font-size:11px;text-transform:none;letter-spacing:normal;font-weight:normal;">— do high-confidence trades actually win? which signal types pay?</span></span>
        <span class="muted" id="cal-asof" style="font-size:11px;text-transform:none;letter-spacing:normal;">—</span>
      </h2>
      <div class="muted" id="cal-meta" style="font-size:11px;margin-bottom:10px;">loading…</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:18px;">
        <div>
          <div style="font-size:12px;color:#dde1e7;margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px;">By Opus confidence</div>
          <table id="cal-conf-tbl" style="font-size:13px;">
            <thead><tr>
              <th>bucket</th><th class="num">n</th><th class="num">win %</th>
              <th class="num">avg ret</th><th class="num">avg conf</th>
            </tr></thead>
            <tbody><tr><td colspan="5" class="muted">loading…</td></tr></tbody>
          </table>
        </div>
        <div>
          <div style="font-size:12px;color:#dde1e7;margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px;">By signal source</div>
          <table id="cal-src-tbl" style="font-size:13px;">
            <thead><tr>
              <th>source</th><th class="num">n</th><th class="num">win %</th>
              <th class="num">avg ret</th><th class="num">best / worst</th>
            </tr></thead>
            <tbody><tr><td colspan="5" class="muted">loading…</td></tr></tbody>
          </table>
        </div>
      </div>
      <div style="font-size:12px;color:#dde1e7;margin-top:14px;margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px;">Recent realized trades</div>
      <table id="cal-recent-tbl" style="font-size:12px;">
        <thead><tr>
          <th>buy → sell</th><th>ticker</th><th class="num">return</th>
          <th class="num">conf</th><th>source</th><th>reasoning</th>
        </tr></thead>
        <tbody><tr><td colspan="6" class="muted">loading…</td></tr></tbody>
      </table>
    </div>

    <!-- ─── Decision Pipeline Health (new 2026-05-15, agent 4) ─── -->
    <div class="card" id="dh-card" style="margin-bottom:18px;">
      <h2 style="display:flex;justify-content:space-between;align-items:center;">
        <span>Decision pipeline health <span class="muted" style="font-size:11px;text-transform:none;letter-spacing:normal;font-weight:normal;">— is the live Opus trader actually deciding? NO_DECISION = parse failure</span></span>
        <span id="dh-verdict" style="font-size:12px;padding:3px 10px;border-radius:4px;background:#1f2126;color:#8b929d;">—</span>
      </h2>
      <div class="muted" id="dh-reason" style="font-size:12px;margin-bottom:12px;">loading…</div>
      <div class="stat-row" style="margin-bottom:14px;">
        <div class="stat"><div class="l">cycles (24h)</div><div class="v" id="dh-total">—</div></div>
        <div class="stat"><div class="l">parse-fail (24h)</div><div class="v" id="dh-fail">—</div></div>
        <div class="stat"><div class="l">fills (24h)</div><div class="v" id="dh-fills">—</div></div>
        <div class="stat"><div class="l">avg confidence</div><div class="v" id="dh-conf">—</div></div>
        <div class="stat"><div class="l">since last fill</div><div class="v" id="dh-lastfill">—</div></div>
        <div class="stat"><div class="l">signals / cycle</div><div class="v" id="dh-sigs">—</div></div>
      </div>
      <div style="font-size:12px;color:#dde1e7;margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px;">Action mix (all-time)</div>
      <div id="dh-mix" style="margin-bottom:14px;"><div class="muted">loading…</div></div>
      <div style="font-size:12px;color:#dde1e7;margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px;">Recent cycles</div>
      <table id="dh-tape" style="font-size:12px;">
        <thead><tr>
          <th>time</th><th>outcome</th><th>action</th>
          <th class="num">conf</th><th class="num">signals</th>
        </tr></thead>
        <tbody><tr><td colspan="5" class="muted">loading…</td></tr></tbody>
      </table>
    </div>

    <!-- ─── Scorer Reliability + Confidence Intervals (new 2026-05-15, agent 4) ─── -->
    <div class="card" id="scrl-card" style="margin-bottom:18px;">
      <h2 style="display:flex;justify-content:space-between;align-items:center;">
        <span>Scorer reliability <span class="muted" style="font-size:11px;text-transform:none;letter-spacing:normal;font-weight:normal;">— how far DecisionScorer predictions actually land from reality</span></span>
        <span class="muted" id="scrl-asof" style="font-size:11px;text-transform:none;letter-spacing:normal;">—</span>
      </h2>
      <div class="muted" id="scrl-meta" style="font-size:11px;margin-bottom:10px;">loading…</div>
      <div class="stat-row" style="margin-bottom:14px;">
        <div class="stat"><div class="l">directional accuracy</div><div class="v" id="scrl-dir">—</div></div>
        <div class="stat"><div class="l">mean abs error</div><div class="v" id="scrl-mae">—</div></div>
        <div class="stat"><div class="l">90% residual band</div><div class="v" id="scrl-band">—</div></div>
        <div class="stat"><div class="l">replay samples</div><div class="v" id="scrl-n">—</div></div>
      </div>
      <div style="font-size:12px;color:#dde1e7;margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px;">Held positions — prediction with empirical band</div>
      <table id="scrl-pos" style="font-size:13px;margin-bottom:16px;">
        <thead><tr>
          <th>ticker</th><th class="num">pred 5d</th><th class="num">likely range</th>
          <th>verdict</th><th class="num">band hit %</th><th>trust</th>
        </tr></thead>
        <tbody><tr><td colspan="6" class="muted">loading…</td></tr></tbody>
      </table>
      <div style="font-size:12px;color:#dde1e7;margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px;">Calibration by prediction band</div>
      <table id="scrl-cal" style="font-size:12px;">
        <thead><tr>
          <th>predicted band</th><th class="num">n</th><th class="num">mean actual</th>
          <th class="num">residual P10/P90</th><th class="num">MAE</th><th class="num">dir. acc.</th>
        </tr></thead>
        <tbody><tr><td colspan="6" class="muted">loading…</td></tr></tbody>
      </table>
    </div>

    <!-- ─── Portfolio Analytics ─── -->
    <div class="card" style="margin-bottom:18px;">
      <h2>Portfolio analytics</h2>
      <div class="stat-row" style="margin-bottom:18px;">
        <div class="stat"><div class="l">today's P/L</div><div class="v" id="an-daily">—</div></div>
        <div class="stat"><div class="l">max drawdown</div><div class="v" id="an-dd">—</div></div>
        <div class="stat"><div class="l">sharpe (ann.)</div><div class="v" id="an-sharpe">—</div></div>
        <div class="stat"><div class="l">win rate</div><div class="v" id="an-winrate">—</div></div>
        <div class="stat"><div class="l">avg winner</div><div class="v" id="an-avgw">—</div></div>
        <div class="stat"><div class="l">avg loser</div><div class="v" id="an-avgl">—</div></div>
        <div class="stat"><div class="l">realized P/L</div><div class="v" id="an-realized">—</div></div>
      </div>
      <div class="stat-row" style="margin-bottom:18px;">
        <div class="stat"><div class="l">profit factor</div><div class="v" id="an-pf">—</div></div>
        <div class="stat"><div class="l">sortino (ann.)</div><div class="v" id="an-sortino">—</div></div>
        <div class="stat"><div class="l">calmar</div><div class="v" id="an-calmar">—</div></div>
        <div class="stat"><div class="l">S&amp;P β</div><div class="v" id="an-beta">—</div></div>
        <div class="stat"><div class="l">S&amp;P corr</div><div class="v" id="an-corr">—</div></div>
        <div class="stat"><div class="l">avg hold</div><div class="v" id="an-hold">—</div></div>
      </div>
      <div style="font-size:13px;color:#dde1e7;margin-bottom:8px;text-transform:uppercase;letter-spacing:0.5px;">Sector exposure</div>
      <div id="an-sector-bar" style="display:flex;height:22px;border-radius:6px;overflow:hidden;background:#0d1117;border:1px solid #1f2126;margin-bottom:6px;"></div>
      <div id="an-sector-legend" style="display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:#dde1e7;"></div>
    </div>

    <!-- ─── Sector Pulse ─── -->
    <div class="card" style="margin-bottom:18px;">
      <h2 style="display:flex;justify-content:space-between;align-items:center;">
        <span>Sector pulse — semis &amp; optical</span>
        <span class="muted" id="sp-asof" style="font-size:11px;text-transform:none;letter-spacing:normal;">—</span>
      </h2>
      <div id="sp-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px;">
        <div class="muted">loading…</div>
      </div>
    </div>

    <!-- ─── DRAM / Semis Sector Heatmap ─── -->
    <div class="card" style="margin-bottom:18px;">
      <h2 style="display:flex;justify-content:space-between;align-items:center;">
        <span>DRAM / semis heatmap <span class="muted" style="font-size:11px;text-transform:none;letter-spacing:normal;font-weight:normal;">— 5d momentum &amp; news pulse</span></span>
        <span class="muted" id="hm-asof" style="font-size:11px;text-transform:none;letter-spacing:normal;">—</span>
      </h2>
      <div class="muted" id="hm-bench" style="font-size:11px;margin-bottom:10px;">SOXX baseline: —</div>
      <div id="hm-grid"><div class="muted">loading…</div></div>
    </div>

    <!-- ─── Deduped News Feed ─── -->
    <div class="card" style="margin-bottom:18px;">
      <h2 style="display:flex;justify-content:space-between;align-items:center;">
        <span>Deduped signals <span class="muted" style="font-size:11px;text-transform:none;letter-spacing:normal;font-weight:normal;">— syndication collapsed, urgency decayed (halflife 4h)</span></span>
        <span class="muted" id="nd-asof" style="font-size:11px;text-transform:none;letter-spacing:normal;">—</span>
      </h2>
      <div class="muted" id="nd-meta" style="font-size:11px;margin-bottom:8px;">—</div>
      <ul id="nd-list" style="margin:0;padding:0;list-style:none;font-size:13px;">
        <li class="muted">loading…</li>
      </ul>
    </div>

    <div class="grid">
      <div class="card">
        <h2>Open positions</h2>
        <div class="table-scroll">
        <table id="pos-tbl">
          <thead><tr>
            <th>ticker</th><th>type</th><th class="num">qty</th>
            <th class="num">avg</th><th class="num">now</th>
            <th class="num">total $</th><th class="num">% port</th>
            <th class="num">P/L</th>
          </tr></thead><tbody></tbody>
        </table>
        </div>
      </div>
      <div class="card">
        <h2>Recent trades</h2>
        <div class="table-scroll">
        <table id="trades-tbl">
          <thead><tr>
            <th>time</th><th>action</th><th>ticker</th>
            <th class="num">qty</th><th class="num">price</th><th>reason</th>
          </tr></thead><tbody></tbody>
        </table>
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:18px;">
      <h2>Decision log</h2>
      <div class="table-scroll">
      <table id="dec-tbl">
        <thead><tr>
          <th>time</th><th>open?</th><th class="num">signals</th>
          <th>action</th><th class="num">equity</th><th>reasoning</th>
        </tr></thead><tbody></tbody>
      </table>
      </div>
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
            <div style="text-align:right;font-size:12px;color:#8b929d;">
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
            <div class="stat"><div class="l">QQQ</div><div class="v" id="bt-qqq">—</div></div>
            <div class="stat"><div class="l">vs SPY</div><div class="v" id="bt-beat">—</div></div>
          </div>
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;font-size:12px;color:#8b929d;">
            <label for="bt-chart-limit">Show last</label>
            <input id="bt-chart-limit" type="range" min="10" max="200" step="10" value="50"
              style="width:120px;cursor:pointer;accent-color:#0acdff;"
              oninput="document.getElementById('bt-chart-limit-val').textContent=this.value; drawBacktestChart()">
            <span id="bt-chart-limit-val">50</span> runs
          </div>
          <div style="position:relative;height:420px;"><canvas id="bt-chart"></canvas></div>
        </div>

        <div class="card" style="margin-bottom:14px;">
          <h2 style="margin:0 0 4px;">Model progress — return by cycle</h2>
          <div style="color:#8b929d;font-size:12px;margin-bottom:10px;">Best / avg / worst return per cycle of 5 runs. Upward trend = model improving.</div>
          <div style="position:relative;height:220px;"><canvas id="mp-chart"></canvas></div>
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
const API_PREFIX = "{{ api_prefix }}";
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
  const r = await fetch(API_PREFIX + "/api/state").then(r => r.json());
  document.getElementById("hb").textContent = "updated " + (r.now || "");
  document.getElementById("tv").textContent = dollar(r.portfolio.total_value);
  document.getElementById("cash").textContent = dollar(r.portfolio.cash);
  const pl = r.portfolio.total_value - 1000;
  const plEl = document.getElementById("pl");
  plEl.textContent = (pl >= 0 ? "+" : "") + dollar(pl);
  plEl.className = "v " + (pl >= 0 ? "pos" : "neg");
  document.getElementById("sp").textContent = r.sp500 ? fmt(r.sp500) : "—";

  const posBody = document.querySelector("#pos-tbl tbody");
  const portTotal = r.portfolio.total_value || 0;
  posBody.innerHTML = r.positions.map(p => {
    const cls = (p.unrealized_pl || 0) >= 0 ? "pos" : "neg";
    const label = p.type === "stock" ? p.type :
                  `${p.type.toUpperCase()} ${p.strike}/${p.expiry}`;
    const mult = (p.type === "call" || p.type === "put") ? 100 : 1;
    const totalVal = (p.current_price || 0) * (p.qty || 0) * mult;
    const pctPort = portTotal > 0 ? (totalVal / portTotal * 100) : 0;
    return `<tr><td>${p.ticker}</td><td>${label}</td>
      <td class="num">${fmt(p.qty,4)}</td>
      <td class="num">${fmt(p.avg_cost)}</td>
      <td class="num">${fmt(p.current_price)}</td>
      <td class="num">${dollar(totalVal)}</td>
      <td class="num">${fmt(pctPort,1)}%</td>
      <td class="num ${cls}">${fmt(p.unrealized_pl)}</td></tr>`;
  }).join("") || `<tr><td colspan="8" class="muted">no positions</td></tr>`;

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
        { label: "Equity", data: values, borderColor: "#0acdff",
          backgroundColor: "rgba(66,165,245,0.08)", fill: true, tension: 0.18, borderWidth: 2, pointRadius: 0 },
        { label: "S&P 500 (raw)", data: sp, borderColor: "#ffb74d",
          backgroundColor: "rgba(255,183,77,0)", borderDash: [4,4], borderWidth: 1, pointRadius: 0, yAxisID: "y2" },
      ]},
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { color: "#dde1e7" }}},
        scales: {
          x: { ticks: { color: "#8b929d", maxTicksLimit: 8 }, grid: { color: "#1f2126" }},
          y: { ticks: { color: "#dde1e7" }, grid: { color: "#1f2126" }},
          y2:{ position: "right", ticks: { color: "#8b929d" }, grid: { display: false }}
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
let btSpyBaseline = null;            // SPY % return over sim period (from API)
let btQqqBaseline = null;            // QQQ % return over sim period (from API)

function btRunColor(runId, idx) { return RUN_COLORS[idx % RUN_COLORS.length]; }
function hexToRgba(hex, a) {
  const h = hex.replace("#","");
  const r = parseInt(h.slice(0,2),16), g = parseInt(h.slice(2,4),16), b = parseInt(h.slice(4,6),16);
  return `rgba(${r},${g},${b},${a})`;
}

let mpChart;
async function loadModelProgress() {
  try {
    const d = await fetch(API_PREFIX + "/api/model-progress").then(r => r.json());
    const cycles = d.cycles || [];
    if (!cycles.length) return;
    // cycle label is now a run_id range string e.g. "#1491-#1495"
    const labels = cycles.map(c => c.cycle);
    const best  = cycles.map(c => c.best);
    const avg   = cycles.map(c => c.avg);
    const worst = cycles.map(c => c.worst);
    const totalRuns = d.total_runs || cycles.length * 5;
    const ctx = document.getElementById("mp-chart");
    if (!ctx) return;
    // Update subtitle with total run count
    const sub = ctx.closest(".card")?.querySelector("div.sub,div[style*='78909c']");
    if (sub) sub.textContent = `Best / avg / worst return per cycle of 5 runs (${totalRuns} total). Upward trend = model improving.`;
    if (mpChart) mpChart.destroy();
    mpChart = new Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [
          { label: "Best %",  data: best,  borderColor: "#00c896", backgroundColor: "rgba(76,175,80,0.08)",  tension: 0.3, pointRadius: 3, fill: false },
          { label: "Avg %",   data: avg,   borderColor: "#0acdff", backgroundColor: "rgba(66,165,245,0.08)", tension: 0.3, pointRadius: 3, fill: false },
          { label: "Worst %", data: worst, borderColor: "#ff4455", backgroundColor: "rgba(239,83,80,0.08)",  tension: 0.3, pointRadius: 3, fill: false },
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: "#dde1e7", font: { size: 11 } } },
          tooltip: { callbacks: { label: c => c.dataset.label + ": " + c.raw.toFixed(1) + "%" } }
        },
        scales: {
          x: {
            ticks: { color: "#8b929d", maxTicksLimit: 20, maxRotation: 45 },
            grid: { color: "rgba(255,255,255,0.05)" }
          },
          y: { ticks: { color: "#8b929d", callback: v => v.toFixed(0) + "%" }, grid: { color: "rgba(255,255,255,0.05)" } }
        }
      }
    });
  } catch(e) { console.error("model-progress:", e); }
}

async function loadBacktests() {
  try {
    const r = await fetch(API_PREFIX + "/api/backtests").then(r => r.json());
    btRuns = r.runs || [];
    btSpyBaseline = r.spy_baseline != null ? r.spy_baseline : null;
    btQqqBaseline = r.qqq_baseline != null ? r.qqq_baseline : null;
    btLastUpdated = Date.now();
    btLoaded = true;
    renderBacktests();
    loadModelProgress();
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
    running.length ? `<span class="live-dot"></span>live` : `<span style="color:#00c896;">●</span> idle`;

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
    const spyVal = btSpyBaseline ?? spy;
    const spyEl = document.getElementById("bt-spy");
    spyEl.textContent = (spyVal >= 0 ? "+" : "") + fmt(spyVal) + "%";
    spyEl.className = "v " + (spyVal >= 0 ? "pos" : "neg");
    const qqqEl = document.getElementById("bt-qqq");
    if (btQqqBaseline != null) {
      qqqEl.textContent = (btQqqBaseline >= 0 ? "+" : "") + fmt(btQqqBaseline) + "%";
      qqqEl.className = "v " + (btQqqBaseline >= 0 ? "pos" : "neg");
    } else {
      qqqEl.textContent = "—";
    }
    document.getElementById("bt-beat").textContent = `${beat} / ${completed.length}`;
  } else {
    ["bt-avg","bt-avg-final","bt-best","bt-worst","bt-spy","bt-qqq","bt-beat"].forEach(id =>
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
  const limitEl = document.getElementById("bt-chart-limit");
  const limit = limitEl ? parseInt(limitEl.value, 10) : 50;
  // Show the most recent `limit` runs (sorted by run_id descending, then reversed for chart order)
  const visibleRuns = [...btRuns]
    .sort((a, b) => b.run_id - a.run_id)
    .slice(0, limit)
    .reverse();

  const dateSet = new Set();
  visibleRuns.forEach(r => (r.equity_curve||[]).forEach(p => dateSet.add(p.date)));
  const labels = Array.from(dateSet).sort();

  const datasets = visibleRuns.map((r, i) => {
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

  // Benchmark overlays — SPY and QQQ, bold so they stand out against run lines.
  // Use btSpyBaseline / btQqqBaseline (total % over the sim period) for linear interpolation.
  const hasSelection = btSelectedRunId != null;
  const _benchmarkLine = (retPct, label, color) => {
    if (retPct == null || labels.length < 2) return null;
    const r = retPct / 100;
    const data = labels.map((d, i) => 1000 * (1 + r * i / (labels.length - 1)));
    return {
      label,
      data,
      kind: "benchmark",
      borderColor: hasSelection ? hexToRgba(color, 0.2) : color,
      borderWidth: 3.5,
      borderDash: [],
      pointRadius: 0,
      tension: 0,
      fill: false,
      order: -1,  // draw on top of run lines
    };
  };
  // Fall back to per-run spy_return_pct if global baseline not yet in API
  const spyPct = btSpyBaseline ?? (btRuns.find(x => x.status === "complete")?.spy_return_pct ?? null);
  const spyLine = _benchmarkLine(spyPct, `SPY ${spyPct != null ? (spyPct >= 0 ? "+" : "") + spyPct.toFixed(1) + "%" : ""}`, "#e0e0e0");
  const qqqLine = _benchmarkLine(btQqqBaseline, `QQQ ${btQqqBaseline != null ? (btQqqBaseline >= 0 ? "+" : "") + btQqqBaseline.toFixed(1) + "%" : ""}`, "#0acdff");
  if (spyLine) datasets.push(spyLine);
  if (qqqLine) datasets.push(qqqLine);

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
          titleColor: "#dde1e7", bodyColor: "#dde1e7",
          padding: 10, boxPadding: 4,
          itemSort: (a,b) => b.parsed.y - a.parsed.y,
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: ${dollar(ctx.parsed.y)}`,
          },
        },
      },
      scales: {
        x: { ticks: { color: "#8b929d", maxTicksLimit: 10 }, grid: { color: "#1f2126" }},
        y: { ticks: { color: "#dde1e7", callback: v => "$"+v }, grid: { color: "#1f2126" }},
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
  const r = await fetch(API_PREFIX + `/api/backtests/${runId}`).then(r => r.json());
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
    const r = await fetch("/intern/api/articles?limit=3");
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
      return `<li style="padding:6px 0;border-bottom:1px solid #1f2126;">
        <span class="pill" style="background:#1f3a4d;color:#4d9eff;margin-right:8px;">${score}</span>
        <a href="${url}" target="_blank" rel="noopener" style="color:#dde1e7;text-decoration:none">${title}</a>
        <span class="muted" style="margin-left:6px;">· ${src}</span>
      </li>`;
    }).join("");
  } catch (e) {
    ul.innerHTML = `<li class="muted">digital intern unreachable</li>`;
  }
}

// ───────── Portfolio Analytics ─────────
const SECTOR_COLORS = {
  semis: "#0acdff", semis_lev: "#1e88e5",
  optical: "#ab47bc",
  broad: "#00c896", broad_lev: "#43a047",
  tech: "#ffb74d", tech_lev: "#fb8c00",
  crypto_lev: "#ffd54f",
  bio_lev: "#ec407a", health_lev: "#e91e63",
  fin_lev: "#26a69a", defense_lev: "#7e57c2",
  housing_lev: "#8d6e63", util_lev: "#90a4ae",
  cash: "#455a64", other: "#8b929d",
};

function _sectorColor(name) { return SECTOR_COLORS[name] || "#8b929d"; }

async function refreshAnalytics() {
  let a;
  try { a = await fetch(API_PREFIX + "/api/analytics").then(r => r.json()); }
  catch (e) { return; }
  if (!a || a.error) return;

  const setStat = (id, txt, cls) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = txt;
    el.className = "v" + (cls ? " " + cls : "");
  };
  const sign = v => v == null ? "" : (v >= 0 ? "+" : "");
  const fmtPct = (v, d=2) => v == null ? "—" : sign(v) + fmt(v, d) + "%";
  const fmtUsd = (v, d=2) => v == null ? "—" : sign(v) + "$" + fmt(Math.abs(v), d);

  setStat("an-daily", a.daily_pl_usd == null ? "—" :
          `${fmtUsd(a.daily_pl_usd)} (${fmtPct(a.daily_pl_pct, 2)})`,
          a.daily_pl_usd == null ? null : (a.daily_pl_usd >= 0 ? "pos" : "neg"));
  setStat("an-dd", a.max_drawdown_usd == null ? "—" :
          `-${fmt(a.max_drawdown_usd)} (${fmt(a.max_drawdown_pct)}%)`,
          a.max_drawdown_usd > 0 ? "neg" : null);
  setStat("an-sharpe", a.sharpe_annualized == null ? "—" : fmt(a.sharpe_annualized, 2),
          a.sharpe_annualized != null ? (a.sharpe_annualized >= 0 ? "pos" : "neg") : null);
  if (a.win_rate_pct == null) setStat("an-winrate", `— (0 trips)`);
  else setStat("an-winrate", `${fmt(a.win_rate_pct, 1)}% (${a.n_round_trips})`,
                a.win_rate_pct >= 50 ? "pos" : "neg");
  setStat("an-avgw", a.avg_winner_usd == null ? "—" : "$" + fmt(a.avg_winner_usd), a.avg_winner_usd != null ? "pos" : null);
  setStat("an-avgl", a.avg_loser_usd == null ? "—" : fmtUsd(a.avg_loser_usd), a.avg_loser_usd != null ? "neg" : null);
  setStat("an-realized", fmtUsd(a.realized_pl_usd, 2), a.realized_pl_usd >= 0 ? "pos" : "neg");

  setStat("an-pf", a.profit_factor == null ? "—" : fmt(a.profit_factor, 2),
          a.profit_factor != null ? (a.profit_factor >= 1 ? "pos" : "neg") : null);
  setStat("an-sortino", a.sortino_annualized == null ? "—" : fmt(a.sortino_annualized, 2),
          a.sortino_annualized != null ? (a.sortino_annualized >= 0 ? "pos" : "neg") : null);
  setStat("an-calmar", a.calmar_ratio == null ? "—" : fmt(a.calmar_ratio, 2),
          a.calmar_ratio != null ? (a.calmar_ratio >= 0 ? "pos" : "neg") : null);
  setStat("an-beta", a.sp500_beta == null ? "—" : fmt(a.sp500_beta, 2));
  setStat("an-corr", a.sp500_correlation == null ? "—" : fmt(a.sp500_correlation, 2));
  setStat("an-hold", a.avg_holding_days == null ? "—" :
          fmt(a.avg_holding_days, 1) + "d");

  // Sector stacked bar
  const sectors = a.sector_exposure_pct || {};
  const cashPct = a.cash_pct || 0;
  const segs = [];
  for (const [name, pct] of Object.entries(sectors)) {
    if (pct > 0) segs.push({ name, pct, color: _sectorColor(name) });
  }
  if (cashPct > 0) segs.push({ name: "cash", pct: cashPct, color: _sectorColor("cash") });
  segs.sort((a, b) => b.pct - a.pct);

  const barEl = document.getElementById("an-sector-bar");
  if (barEl) {
    barEl.innerHTML = segs.map(s =>
      `<div title="${s.name} ${fmt(s.pct,1)}%" style="flex:${s.pct};background:${s.color};border-right:1px solid #0d1117;"></div>`
    ).join("") || `<div class="muted" style="padding:3px 8px;font-size:12px;">no allocations</div>`;
  }
  const legEl = document.getElementById("an-sector-legend");
  if (legEl) {
    legEl.innerHTML = segs.map(s =>
      `<span><span style="display:inline-block;width:10px;height:10px;background:${s.color};border-radius:2px;margin-right:5px;vertical-align:middle;"></span>${s.name}: ${fmt(s.pct,1)}%</span>`
    ).join("") || `<span class="muted">no allocations</span>`;
  }
}

// ───────── Sector Pulse ─────────
async function refreshSectorPulse() {
  let r;
  try { r = await fetch(API_PREFIX + "/api/sector-pulse").then(r => r.json()); }
  catch (e) { return; }
  if (!r || !r.tickers) return;
  const grid = document.getElementById("sp-grid");
  if (!grid) return;
  document.getElementById("sp-asof").textContent = r.as_of ? "as of " + r.as_of.replace("T"," ").slice(0,16) + " UTC" : "";
  grid.innerHTML = r.tickers.map(t => {
    const rsi = t.rsi;
    const rsiCls = rsi == null ? "muted" :
                   rsi >= 70 ? "neg" :
                   rsi <= 30 ? "pos" : "";
    const mom5 = t.mom_5d;
    const mom5Cls = mom5 == null ? "muted" : (mom5 >= 0 ? "pos" : "neg");
    const px = t.price;
    const news = t.news_count_24h || 0;
    const urgent = t.news_urgent_24h || 0;
    const newsBadge = urgent > 0
      ? `<span style="background:#3a1b1b;color:#ff4455;padding:1px 6px;border-radius:8px;font-size:10px;font-weight:600;">${urgent}!</span>`
      : news > 0
        ? `<span style="background:#1f3a4d;color:#4d9eff;padding:1px 6px;border-radius:8px;font-size:10px;">${news}</span>`
        : `<span class="muted" style="font-size:10px;">0</span>`;
    const headline = t.top_headline
      ? `<div style="margin-top:6px;font-size:11px;line-height:1.4;color:#dde1e7;">
           ${t.top_url ? `<a href="${t.top_url}" target="_blank" rel="noopener" style="color:#dde1e7;text-decoration:none;">${(t.top_headline||'').slice(0,100)}</a>` : (t.top_headline||'').slice(0,100)}
         </div>`
      : `<div class="muted" style="margin-top:6px;font-size:11px;">no news</div>`;
    return `<div style="background:#0d1117;border:1px solid #1f2126;border-radius:6px;padding:10px;">
      <div style="display:flex;justify-content:space-between;align-items:baseline;">
        <span style="font-weight:600;color:#eceff1;font-size:14px;">${t.ticker}</span>
        <span style="font-size:13px;color:#dde1e7;font-variant-numeric:tabular-nums;">${px == null ? '—' : '$'+fmt(px)}</span>
      </div>
      <div style="display:flex;gap:8px;font-size:11px;margin-top:5px;color:#8b929d;">
        <span>RSI <span class="${rsiCls}">${rsi == null ? '—' : fmt(rsi,1)}</span></span>
        <span>5d <span class="${mom5Cls}">${mom5 == null ? '—' : (mom5>=0?'+':'')+fmt(mom5,1)+'%'}</span></span>
        <span style="margin-left:auto;">${newsBadge}</span>
      </div>
      ${headline}
    </div>`;
  }).join("");
}

// ───────── Daily briefing card ─────────
async function refreshBriefing() {
  try {
    const r = await fetch(API_PREFIX + "/api/briefing").then(r => r.json());
    if (r.error) return;
    const dot = document.getElementById("briefing-dot");
    if (dot) dot.style.background = r.market_open ? "#00c896" : "#ff4455";
    document.getElementById("briefing-status").textContent = r.status_line || "";
    document.getElementById("briefing-asof").textContent = (r.as_of || "").replace("T"," ").slice(0,19);
    // Futures row
    const futWrap = document.getElementById("briefing-futures");
    const futNames = {"ES=F":"S&P fut","NQ=F":"NQ fut","CL=F":"WTI","GC=F":"Gold"};
    futWrap.innerHTML = Object.entries(r.futures || {}).map(([sym,px]) => {
      const label = futNames[sym] || sym;
      const value = (px == null) ? "—" : Number(px).toLocaleString(undefined,{maximumFractionDigits:2});
      return `<div><span class="muted" style="font-size:11px;">${label}</span><div style="font-variant-numeric:tabular-nums;font-size:15px;color:#dde1e7;">${value}</div></div>`;
    }).join("");
    // Urgent news (top 5)
    const urgEl = document.getElementById("briefing-urgent");
    const urgent = r.urgent_news || [];
    if (!urgent.length) {
      urgEl.innerHTML = `<li class="muted" style="padding:4px 0;">no urgent news in the last 8h</li>`;
    } else {
      urgEl.innerHTML = urgent.map(u => {
        const sc = (u.ai_score != null) ? Number(u.ai_score).toFixed(1) : "—";
        const tk = (u.tickers || []).slice(0,3).join(" ");
        return `<li style="padding:4px 0;border-bottom:1px solid #1f2126;">
          <span style="display:inline-block;min-width:34px;color:#ff4455;font-variant-numeric:tabular-nums;font-weight:600;">${sc}</span>
          <span style="color:#dde1e7;">${(u.title || "").replace(/[<>]/g, '')}</span>
          ${tk ? `<span class="muted" style="font-size:11px;margin-left:6px;">[${tk}]</span>` : ""}
        </li>`;
      }).join("");
    }
  } catch (e) { console.error("briefing:", e); }
}

// ───────── Trade suggestions card ─────────
async function refreshSuggestions() {
  try {
    const r = await fetch(API_PREFIX + "/api/suggestions").then(r => r.json());
    if (r.error) {
      document.getElementById("sug-summary").textContent = "error: " + r.error;
      return;
    }
    const counts = r.action_counts || {};
    const summary = Object.entries(counts).map(([a,n]) => `${n} ${a}`).join(" · ") || "no actionable candidates";
    document.getElementById("sug-summary").textContent = `${r.n_candidates} candidates from ${r.n_signals_used} signals — ${summary}`;
    document.getElementById("sug-meta").textContent = (r.as_of || "").replace("T"," ").slice(0,19);
    const tbody = document.querySelector("#sug-tbl tbody");
    const items = r.suggestions || [];
    if (!items.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="muted">no suggestions — no actionable news in the last 6h</td></tr>`;
      return;
    }
    const actionStyle = {
      "BUY":   "background:#1b3a2a;color:#00c896;",
      "ADD":   "background:#1b3a2a;color:#00c896;",
      "TRIM":  "background:#3a2f1b;color:#ffb74d;",
      "EXIT":  "background:#3a1b1b;color:#ff4455;",
      "WATCH": "background:#1f3a4d;color:#4d9eff;",
      "HOLD":  "background:#1f2933;color:#dde1e7;",
    };
    tbody.innerHTML = items.map(s => {
      const styleA = actionStyle[s.action] || actionStyle["HOLD"];
      const px = (s.price == null) ? "—" : "$" + Number(s.price).toFixed(2);
      const qty = s.held_qty ? Number(s.held_qty).toFixed(2) : "—";
      const rsi = (s.rsi == null) ? "—" : Number(s.rsi).toFixed(0);
      const rsiCls = (s.rsi != null && s.rsi >= 70) ? "neg" : (s.rsi != null && s.rsi <= 35) ? "pos" : "";
      const urgent = s.news_urgent ? `<span style="color:#ff4455;font-weight:600;">!</span>` : "";
      const newsCell = s.news_count > 0
        ? `<span style="color:#4d9eff;">${s.news_count}</span> <span class="muted">@</span> ${Number(s.news_max_score).toFixed(1)} ${urgent}`
        : `<span class="muted">0</span>`;
      const reasons = (s.reasons || []).slice(0,3).join(" · ");
      const head = s.top_headline ? (s.top_url
        ? `<a href="${s.top_url}" target="_blank" rel="noopener" style="color:#dde1e7;">${s.top_headline.replace(/[<>]/g,'')}</a>`
        : `<span class="muted">${s.top_headline.replace(/[<>]/g,'')}</span>`) : `<span class="muted">—</span>`;
      return `<tr>
        <td><span class="pill" style="${styleA}padding:3px 8px;font-size:11px;font-weight:600;">${s.action}</span></td>
        <td style="font-weight:600;">${s.ticker}</td>
        <td class="num">${Number(s.conviction).toFixed(2)}</td>
        <td class="num">${px}</td>
        <td class="num muted">${qty}</td>
        <td class="num">${newsCell}</td>
        <td class="num ${rsiCls}">${rsi}</td>
        <td class="muted" style="font-size:11px;">${reasons}</td>
        <td style="font-size:12px;">${head}</td>
      </tr>`;
    }).join("");
  } catch (e) { console.error("suggestions:", e); }
}

// ───────── Risk panel card ─────────
async function refreshRisk() {
  try {
    const r = await fetch(API_PREFIX + "/api/risk").then(r => r.json());
    if (r.error) return;
    const top1Txt = r.concentration_top1_ticker
      ? `${Number(r.concentration_top1_pct).toFixed(1)}% <span class="muted" style="font-size:13px;">${r.concentration_top1_ticker}</span>`
      : "—";
    const top1El = document.getElementById("risk-top1");
    top1El.innerHTML = top1Txt;
    top1El.className = "v " + (r.concentration_top1_pct >= 40 ? "neg" : "");
    document.getElementById("risk-top3").textContent = (r.concentration_top3_pct != null) ? Number(r.concentration_top3_pct).toFixed(1) + "%" : "—";
    const levEl = document.getElementById("risk-lev");
    levEl.textContent = (r.leveraged_pct != null) ? Number(r.leveraged_pct).toFixed(1) + "%" : "—";
    levEl.className = "v " + (r.leveraged_pct >= 30 ? "neg" : "");
    const shockEl = document.getElementById("risk-shock");
    if (r.spy_shock_3pct_usd != null) {
      const v = Number(r.spy_shock_3pct_usd);
      const pct = Number(r.spy_shock_3pct_pct || 0);
      shockEl.innerHTML = `${v >= 0 ? "+" : ""}$${v.toFixed(2)} <span class="muted" style="font-size:12px;">(${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%)</span>`;
      shockEl.className = "v " + (v < 0 ? "neg" : "pos");
    }
    document.getElementById("risk-age").textContent = (r.median_age_days != null) ? r.median_age_days : "—";
    const staleEl = document.getElementById("risk-stale-n");
    const stale = r.stale_positions || [];
    staleEl.textContent = stale.length;
    staleEl.className = "v " + (stale.length > 0 ? "neg" : "");
    const staleList = document.getElementById("risk-stale-list");
    if (!stale.length) {
      staleList.innerHTML = `<span class="muted">no stale positions — all holds are either fresh or moving</span>`;
    } else {
      staleList.innerHTML = "Stale: " + stale.map(s =>
        `<span style="display:inline-block;background:#1f2126;border:1px solid #3a2f1b;border-radius:4px;padding:3px 8px;margin-right:6px;margin-bottom:4px;">${s.ticker} ${s.age_days}d ${s.pl_pct >= 0 ? "+" : ""}${s.pl_pct}%</span>`
      ).join("");
    }
  } catch (e) { console.error("risk:", e); }
}

// ───────── Earnings radar ─────────
async function refreshEarningsRisk() {
  let r;
  try { r = await fetch(API_PREFIX + "/api/earnings-risk").then(r => r.json()); }
  catch (e) { return; }
  const list = document.getElementById("er-list");
  const meta = document.getElementById("er-meta");
  const asof = document.getElementById("er-asof");
  if (!list) return;
  if (!r || r.error) { list.innerHTML = `<li class="muted">unavailable</li>`; return; }
  if (asof && r.as_of) asof.textContent = r.as_of.slice(11, 16) + " UTC";
  const evs = r.events || [];
  if (!r.source_ok) {
    meta.textContent = "earnings calendar (:8080) unreachable";
  } else {
    meta.innerHTML = `${r.n_held_reporting} holding(s) reporting · ` +
      `<span class="${r.n_imminent > 0 ? 'neg' : 'muted'}">${r.n_imminent} imminent (≤3d)</span> · ` +
      `$${Number(r.held_exposure_at_risk_usd || 0).toFixed(0)} exposure at risk`;
  }
  if (!evs.length) {
    list.innerHTML = `<li class="muted">no earnings within horizon for holdings or watchlist</li>`;
    return;
  }
  const tierStyle = {
    HELD_IMMINENT: "background:#3a1b1b;border:1px solid #7a2f2f;",
    HELD_SOON:     "background:#3a2f1b;border:1px solid #7a5f2f;",
    WATCH:         "background:#1f2126;border:1px solid #2f3540;",
  };
  const tierLabel = { HELD_IMMINENT: "⚠ HELD", HELD_SOON: "HELD", WATCH: "watch" };
  list.innerHTML = evs.slice(0, 14).map(e => {
    const d = e.days_away == null ? "?" : Number(e.days_away).toFixed(1) + "d";
    const exp = e.held ? ` · $${Number(e.exposure_usd).toFixed(0)}` : "";
    return `<li style="padding:6px 8px;margin-bottom:4px;border-radius:5px;${tierStyle[e.tier] || ''}">` +
      `<b>${e.ticker}</b> <span class="muted" style="font-size:11px;">${tierLabel[e.tier] || ''}</span>` +
      `<span style="float:right;">in ${d}${exp}</span></li>`;
  }).join("");
}

// ───────── Greeks card (options exposure) ─────────
async function refreshGreeks() {
  try {
    const r = await fetch(API_PREFIX + "/api/greeks").then(r => r.json());
    if (r.error) { return; }
    const positions = (r.positions || []).filter(p => p.type === "call" || p.type === "put");
    const card = document.getElementById("greeks-card");
    if (!card) return;
    // Hide card entirely when there are no option positions — keeps dashboard clean.
    if (positions.length === 0) { card.style.display = "none"; return; }
    card.style.display = "block";
    const t = r.totals || {};
    document.getElementById("gk-asof").textContent = r.as_of ? r.as_of.replace("T"," ").slice(0,16) : "—";
    const dElem = document.getElementById("gk-delta");
    dElem.textContent = fmt(t.delta, 2);
    dElem.className = "v " + ((t.delta || 0) >= 0 ? "pos" : "neg");
    document.getElementById("gk-gamma").textContent = fmt(t.gamma, 5);
    const thElem = document.getElementById("gk-theta");
    thElem.textContent = "$" + fmt(t.theta, 2);
    thElem.className = "v " + ((t.theta || 0) >= 0 ? "pos" : "neg");
    document.getElementById("gk-vega").textContent = "$" + fmt(t.vega, 2);
    document.getElementById("gk-notional").textContent = dollar(t.gross_notional);
    document.getElementById("gk-deltapct").textContent = (t.delta_pct_port != null) ? (fmt(t.delta_pct_port,1) + "%") : "—";
    const tbody = document.querySelector("#gk-tbl tbody");
    tbody.innerHTML = positions.map(p => {
      const cls = (p.delta || 0) >= 0 ? "pos" : "neg";
      const ivStr = p.iv != null ? (fmt(p.iv * 100, 1) + "%") : "—";
      const dteStr = p.days_to_expiry != null ? (p.days_to_expiry + "d") : "";
      return `<tr>
        <td>${p.ticker}</td>
        <td>${p.type.toUpperCase()}</td>
        <td class="num">${fmt(p.qty, 0)}</td>
        <td class="num">${p.strike || "—"} / ${p.expiry || "—"} ${dteStr ? `<span class="muted">(${dteStr})</span>` : ""}</td>
        <td class="num">${ivStr}</td>
        <td class="num ${cls}">${fmt(p.delta, 2)}</td>
        <td class="num">${fmt(p.gamma, 5)}</td>
        <td class="num">${fmt(p.theta, 2)}</td>
        <td class="num">${fmt(p.vega, 2)}</td>
      </tr>`;
    }).join("");
  } catch (e) { console.error("greeks:", e); }
}

// ───────── DRAM/Semis heatmap ─────────
function hmColorFor(pct) {
  if (pct == null) return "#1f2126";
  // Map [-5%..+5%] to red..green via HSL.
  const clamped = Math.max(-5, Math.min(5, pct));
  // -5 → hue 0 (red), +5 → hue 130 (green)
  const hue = 65 + clamped * 13;
  const sat = 55;
  const lit = 24 + Math.abs(clamped) * 1.5;
  return `hsl(${hue}, ${sat}%, ${lit}%)`;
}
async function refreshHeatmap() {
  try {
    const r = await fetch(API_PREFIX + "/api/sector-heatmap").then(r => r.json());
    if (r.error) {
      document.getElementById("hm-grid").innerHTML =
        `<div class="muted">heatmap error: ${r.error}</div>`;
      return;
    }
    document.getElementById("hm-asof").textContent = r.as_of ? r.as_of.replace("T"," ").slice(0,16) : "—";
    const bench = r.reference_mom_5d;
    const benchStr = bench != null ? `${r.reference} 5d ${bench >= 0 ? "+" : ""}${fmt(bench, 2)}%` : `${r.reference} —`;
    document.getElementById("hm-bench").textContent = "Benchmark: " + benchStr;

    const grid = document.getElementById("hm-grid");
    const buckets = r.buckets || [];
    grid.innerHTML = buckets.map(b => {
      const cells = (b.tickers || []).map(t => {
        const m5 = t.mom_5d;
        const rs = t.vs_sox_5d;
        const news = t.n || 0;
        const urg = t.urgent || 0;
        const bg = hmColorFor(m5);
        const rsStr = rs == null ? "" : `<span style="color:${rs >= 0 ? '#7fff00' : '#ff7b7b'};font-size:10px;margin-left:4px;">vs SOX ${rs >= 0 ? '+' : ''}${fmt(rs,1)}</span>`;
        const newsStr = news > 0
          ? `<span style="color:#dde1e7;font-size:10px;margin-left:6px;">📰 ${news}${urg ? `<span style="color:#ff4455">!</span>` : ""}</span>`
          : "";
        const rsi = t.rsi;
        const rsiStr = rsi == null ? "" : `<span style="color:${rsi > 70 ? '#ff7b7b' : (rsi < 30 ? '#80deea' : '#8b929d')};font-size:10px;margin-left:6px;">RSI ${fmt(rsi,0)}</span>`;
        const px = t.price == null ? "—" : "$" + fmt(t.price, 2);
        return `<div style="background:${bg};border:1px solid #1f2126;border-radius:4px;padding:6px 8px;min-width:130px;">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;">
            <span style="font-weight:bold;color:#fff;">${t.ticker}</span>
            <span style="font-size:11px;color:#dde1e7;">${px}</span>
          </div>
          <div style="font-size:13px;color:${(m5 || 0) >= 0 ? '#7fff00' : '#ff7b7b'};font-weight:bold;">${m5 == null ? "—" : (m5 >= 0 ? "+" : "") + fmt(m5, 2) + "%"}</div>
          <div style="margin-top:2px;">${rsStr}${rsiStr}${newsStr}</div>
        </div>`;
      }).join("");
      const bm = b.avg_mom_5d;
      const bmStr = bm == null ? "—" : (bm >= 0 ? "+" : "") + fmt(bm, 2) + "%";
      const bmCls = (bm || 0) >= 0 ? "pos" : "neg";
      return `<div style="margin-bottom:14px;">
        <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;">
          <span style="text-transform:uppercase;font-size:11px;letter-spacing:0.5px;color:#8b929d;">${b.name.replace(/_/g, " ")}</span>
          <span class="${bmCls}" style="font-size:11px;">avg 5d ${bmStr}</span>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:6px;">${cells}</div>
      </div>`;
    }).join("");
  } catch (e) { console.error("heatmap:", e); }
}

// ───────── DecisionScorer per-position predictions ─────────
function scorerColor(v) {
  if (v == null) return "#dde1e7";
  if (v >= 2) return "#7fff00";
  if (v >= 0.5) return "#a5d6a7";
  if (v >= -0.5) return "#dde1e7";
  if (v >= -2) return "#ff9100";
  return "#ff4455";
}
function verdictBadge(v) {
  const colors = {
    STRONG_HOLD: ["#1b5e20", "#a5d6a7"],
    HOLD:        ["#2e7d32", "#c5e1a5"],
    NEUTRAL:     ["#37474f", "#dde1e7"],
    TRIM:        ["#ef6c00", "#ffe0b2"],
    EXIT:        ["#b71c1c", "#ffcdd2"],
  };
  const [bg, fg] = colors[v] || ["#1f2126", "#8b929d"];
  return `<span style="background:${bg};color:${fg};padding:1px 6px;border-radius:3px;font-size:11px;letter-spacing:0.5px;">${v || "—"}</span>`;
}
async function refreshScorer() {
  try {
    const r = await fetch(API_PREFIX + "/api/scorer-predictions").then(r => r.json());
    if (r.error) {
      document.getElementById("sc-meta").textContent = "scorer error: " + r.error;
      return;
    }
    document.getElementById("sc-asof").textContent = r.as_of ? r.as_of.replace("T"," ").slice(0,16) : "—";
    const meta = r.is_trained
      ? `trained (n=${r.n_train}) · regime mult ${fmt(r.regime_mult, 2)} · gate ≥ ${r.gate_threshold}`
      : `not trained yet (n=${r.n_train}/${r.gate_threshold}) — predictions will be 0.00 until threshold reached`;
    document.getElementById("sc-meta").textContent = meta;
    const tbody = document.querySelector("#sc-tbl tbody");
    const rows = r.predictions || [];
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="muted">no open stock positions</td></tr>`;
      return;
    }
    tbody.innerHTML = rows.map(p => {
      const v = p.pred_5d_return_pct;
      const sign = v >= 0 ? "+" : "";
      const newsCell = (p.news_count || 0) > 0
        ? `${p.news_count}${(p.news_urgent || 0) > 0 ? ` <span style="color:#ff4455">!</span>` : ""}`
        : "—";
      return `<tr>
        <td><strong>${p.ticker}</strong></td>
        <td class="num" style="color:${scorerColor(v)};font-weight:bold;">${v == null ? "—" : sign + fmt(v, 2) + "%"}</td>
        <td>${verdictBadge(p.verdict)}</td>
        <td class="num">${p.rsi == null ? "—" : fmt(p.rsi, 0)}</td>
        <td class="num">${p.macd == null ? "—" : fmt(p.macd, 3)}</td>
        <td class="num">${p.mom_5d == null ? "—" : (p.mom_5d >= 0 ? "+" : "") + fmt(p.mom_5d, 2) + "%"}</td>
        <td class="num">${p.mom_20d == null ? "—" : (p.mom_20d >= 0 ? "+" : "") + fmt(p.mom_20d, 2) + "%"}</td>
        <td class="num">${newsCell}</td>
      </tr>`;
    }).join("");
  } catch (e) { console.error("scorer:", e); }
}

// ───────── Deduped signals feed ─────────
async function refreshDedupedNews() {
  try {
    const r = await fetch(API_PREFIX + "/api/news-deduped?hours=6&min_score=4").then(r => r.json());
    if (r.error) {
      document.getElementById("nd-list").innerHTML = `<li class="muted">${r.error}</li>`;
      return;
    }
    document.getElementById("nd-asof").textContent = r.as_of ? r.as_of.replace("T"," ").slice(0,16) : "—";
    const meta = `${r.n_after_dedup} unique signals from ${r.n_raw} raw articles (compression ${fmt(r.compression_ratio, 1)}x) · halflife ${r.halflife_hours}h`;
    document.getElementById("nd-meta").textContent = meta;
    const items = (r.articles || []).slice(0, 15);
    const list = document.getElementById("nd-list");
    if (!items.length) {
      list.innerHTML = `<li class="muted">no signals in window</li>`;
      return;
    }
    list.innerHTML = items.map(a => {
      const score = a.ai_score != null ? fmt(a.ai_score, 1) : "—";
      const urgD = a.urgency_decayed != null ? fmt(a.urgency_decayed, 2) : "—";
      const dups = a.dup_count && a.dup_count > 1
        ? `<span class="muted" style="font-size:11px;margin-left:6px;">×${a.dup_count}</span>` : "";
      const urgBadge = (a.urgency_decayed || 0) >= 0.7
        ? `<span style="background:#ff1744;color:#fff;border-radius:3px;padding:1px 5px;font-size:10px;margin-right:6px;">URG ${urgD}</span>`
        : ((a.urgency_decayed || 0) > 0
            ? `<span style="background:#ff9100;color:#000;border-radius:3px;padding:1px 5px;font-size:10px;margin-right:6px;">u ${urgD}</span>`
            : "");
      const tickers = (a.tickers || []).slice(0, 4).map(t =>
        `<span style="background:#1f2126;color:#0acdff;font-size:10px;padding:1px 5px;border-radius:3px;margin-left:4px;">${t}</span>`
      ).join("");
      const title = (a.title || "").replace(/</g, "&lt;");
      const ts = a.first_seen ? a.first_seen.replace("T", " ").slice(5, 16) : "";
      return `<li style="padding:6px 0;border-bottom:1px solid #1f2126;">
        ${urgBadge}<span style="color:#dde1e7;">${title}</span>${dups}
        <div class="muted" style="font-size:11px;margin-top:3px;">
          [${score}] ${a.source || "?"} · ${ts}${tickers}
        </div>
      </li>`;
    }).join("");
  } catch (e) { console.error("deduped:", e); }
}

// ───────── Position thesis (new 2026-05-15) ─────────
function verdictPill(v) {
  const colors = {
    STRONG_HOLD: ["#1b5e20", "#a5d6a7"],
    HOLD:        ["#33691e", "#c5e1a5"],
    WATCH:       ["#37474f", "#dde1e7"],
    TRIM:        ["#bf360c", "#ffccbc"],
    EXIT:        ["#b71c1c", "#ffcdd2"],
  };
  const [bg, fg] = colors[v] || ["#37474f", "#dde1e7"];
  return `<span style="background:${bg};color:${fg};border-radius:3px;padding:2px 8px;font-size:11px;font-weight:bold;letter-spacing:0.5px;">${v}</span>`;
}

async function refreshThesis() {
  try {
    const r = await fetch(API_PREFIX + "/api/position-thesis").then(r => r.json());
    document.getElementById("th-asof").textContent =
      r.as_of ? r.as_of.replace("T"," ").slice(0,16) : "—";
    if (r.error) {
      document.getElementById("th-meta").textContent = "error: " + r.error;
      document.getElementById("th-grid").innerHTML = "";
      return;
    }
    const cards = r.cards || [];
    const meta = `${r.n_positions || 0} open positions · scorer ` +
      (r.scorer_trained ? `trained (n=${r.scorer_n_train})` : `untrained (n=${r.scorer_n_train})`);
    document.getElementById("th-meta").textContent = meta;
    const grid = document.getElementById("th-grid");
    if (!cards.length) {
      grid.innerHTML = `<div class="muted">no open positions</div>`;
      return;
    }
    grid.innerHTML = cards.map(c => {
      const pl = c.unrealized_pl || 0;
      const plPct = c.pl_pct || 0;
      const plColor = pl >= 0 ? "#00c896" : "#ff4455";
      const plSign = pl >= 0 ? "+" : "";
      const news = c.news || {};
      const head = (news.headlines || [])[0];
      const headHtml = head
        ? `<div class="muted" style="font-size:11px;margin-top:4px;">📰 [${fmt(head.score,1)}] ${(head.title||"").replace(/</g,"&lt;").slice(0,120)}</div>`
        : `<div class="muted" style="font-size:11px;margin-top:4px;">no recent news</div>`;
      const ld = c.last_decision;
      const ldHtml = ld
        ? `<div style="font-size:11px;color:#90a4ae;margin-top:4px;">last: <strong>${ld.action.replace(/→.*/,'').trim()}</strong> conf=${ld.confidence!=null?fmt(ld.confidence,2):"?"} · ${(ld.reasoning||"").replace(/</g,"&lt;").slice(0,140)}</div>`
        : "";
      const rsi = c.rsi != null ? fmt(c.rsi, 0) : "—";
      const m5 = c.mom_5d != null ? (c.mom_5d >= 0 ? "+" : "") + fmt(c.mom_5d, 1) + "%" : "—";
      const m20 = c.mom_20d != null ? (c.mom_20d >= 0 ? "+" : "") + fmt(c.mom_20d, 1) + "%" : "—";
      const pred = c.scorer_pred_5d;
      const predHtml = pred != null
        ? `<span style="color:${scorerColor(pred)};">${pred>=0?"+":""}${fmt(pred,2)}%</span>`
        : "—";
      const newsPulse = news.n
        ? `${news.n}·<span style="color:#00c896">${news.bull||0}↑</span>/<span style="color:#ff4455">${news.bear||0}↓</span> avg ${fmt(news.avg_score,1)}`
        : "<span class='muted'>—</span>";
      return `<div style="background:#0d1117;border:1px solid #1f2126;border-radius:6px;padding:12px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
          <div><strong style="font-size:15px;color:#e0e0e0;">${c.ticker}</strong>
            <span class="muted" style="font-size:11px;margin-left:6px;">qty ${c.qty} @ $${fmt(c.avg_cost,2)} · ${fmt(c.days_held,1)}d</span>
          </div>
          <div>${verdictPill(c.verdict)}</div>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:12px;color:#dde1e7;margin-bottom:6px;">
          <span>P/L <span style="color:${plColor};font-weight:bold;">${plSign}$${fmt(pl,2)} (${plSign}${fmt(plPct,2)}%)</span></span>
          <span>scorer ${predHtml}</span>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:10px;font-size:11px;color:#90a4ae;margin-bottom:4px;">
          <span>RSI ${rsi}</span><span>mom5 ${m5}</span><span>mom20 ${m20}</span>
          <span>news ${newsPulse}</span>
        </div>
        <div style="font-size:11px;color:#dde1e7;font-style:italic;margin-top:6px;">→ ${c.thesis||"—"}</div>
        ${headHtml}
        ${ldHtml}
      </div>`;
    }).join("");
  } catch (e) { console.error("thesis:", e); }
}

// ───────── Drawdown anatomy (new 2026-05-15) ─────────
async function refreshDrawdown() {
  try {
    const r = await fetch(API_PREFIX + "/api/drawdown").then(r => r.json());
    document.getElementById("dd-asof").textContent =
      r.as_of ? r.as_of.replace("T"," ").slice(0,16) : "—";
    if (r.error) {
      document.getElementById("dd-pct").textContent = "err";
      return;
    }
    document.getElementById("dd-current").textContent = "$" + fmt(r.current_value, 2);
    document.getElementById("dd-peak").textContent = "$" + fmt(r.peak_value, 2);
    const ddPct = r.drawdown_pct || 0;
    const ddEl = document.getElementById("dd-pct");
    if (r.at_high_water) {
      ddEl.innerHTML = `<span style="color:#00c896;font-weight:bold;">◆ at high-water</span>`;
    } else {
      const col = ddPct <= -5 ? "#ff4455" : (ddPct <= -2 ? "#ff9100" : "#ffd54f");
      ddEl.innerHTML = `<span style="color:${col};">${fmt(ddPct,2)}% ($${fmt(r.drawdown_abs,2)})</span>`;
    }
    document.getElementById("dd-trough").textContent =
      r.trough_value != null ? `$${fmt(r.trough_value,2)} (${fmt(r.trough_pct,2)}%)` : "—";
    document.getElementById("dd-hours").textContent =
      r.hours_in_dd != null ? fmt(r.hours_in_dd, 1) + "h" : "—";
    document.getElementById("dd-rec").textContent =
      (r.at_high_water ? "100" : fmt(r.recovery_pct, 0)) + "%";
    const tbody = document.querySelector("#dd-tbl tbody");
    const rows = r.contributors || [];
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="muted">no open positions</td></tr>`;
      return;
    }
    tbody.innerHTML = rows.map(p => {
      const pl = p.unrealized_pl || 0;
      const plPct = p.pl_pct || 0;
      const color = pl >= 0 ? "#00c896" : "#ff4455";
      const dragBadge = p.drag
        ? `<span style="background:#b71c1c;color:#ffcdd2;border-radius:3px;padding:1px 5px;font-size:10px;">DRAG</span>`
        : `<span class="muted">—</span>`;
      return `<tr>
        <td><strong>${p.ticker}</strong> <span class="muted" style="font-size:10px;">${p.type||""}</span></td>
        <td class="num">${p.qty}</td>
        <td class="num">$${fmt(p.avg_cost,2)}</td>
        <td class="num">$${fmt(p.current_price,2)}</td>
        <td class="num" style="color:${color};font-weight:bold;">${pl>=0?"+":""}$${fmt(pl,2)}</td>
        <td class="num" style="color:${color};">${plPct>=0?"+":""}${fmt(plPct,2)}%</td>
        <td>${dragBadge}</td>
      </tr>`;
    }).join("");
  } catch (e) { console.error("drawdown:", e); }
}

// ───────── Confidence calibration + signal attribution (new 2026-05-15) ─────────
async function refreshCalibration() {
  try {
    const r = await fetch(API_PREFIX + "/api/calibration").then(r => r.json());
    document.getElementById("cal-asof").textContent =
      r.as_of ? r.as_of.replace("T"," ").slice(0,16) : "—";
    if (r.error) {
      document.getElementById("cal-meta").textContent = "error: " + r.error;
      return;
    }
    document.getElementById("cal-meta").textContent =
      `${r.n_decisions_parsed||0} decisions parsed · ${r.n_realized_trades||0} realized round-trips matched`;
    const confTbody = document.querySelector("#cal-conf-tbl tbody");
    const confRows = r.confidence_buckets || [];
    if (!confRows.some(b => b.n)) {
      confTbody.innerHTML = `<tr><td colspan="5" class="muted">no closed trades yet — calibration builds over time</td></tr>`;
    } else {
      confTbody.innerHTML = confRows.map(b => {
        const wrColor = b.win_rate >= 60 ? "#00c896" : (b.win_rate >= 40 ? "#ffd54f" : "#ff4455");
        const retColor = b.avg_return > 0 ? "#00c896" : "#ff4455";
        return `<tr>
          <td>${b.bucket}</td>
          <td class="num">${b.n}</td>
          <td class="num" style="color:${b.n?wrColor:'#8b929d'};">${b.n?fmt(b.win_rate,1)+"%":"—"}</td>
          <td class="num" style="color:${b.n?retColor:'#8b929d'};">${b.n?(b.avg_return>=0?"+":"")+fmt(b.avg_return,2)+"%":"—"}</td>
          <td class="num">${b.n?fmt(b.avg_conf,2):"—"}</td>
        </tr>`;
      }).join("");
    }
    const srcTbody = document.querySelector("#cal-src-tbl tbody");
    const srcRows = (r.signal_sources || []).sort((a,b) => (b.n||0)-(a.n||0));
    if (!srcRows.some(s => s.n)) {
      srcTbody.innerHTML = `<tr><td colspan="5" class="muted">no realized trades yet</td></tr>`;
    } else {
      srcTbody.innerHTML = srcRows.map(s => {
        const wrColor = s.win_rate >= 60 ? "#00c896" : (s.win_rate >= 40 ? "#ffd54f" : "#ff4455");
        const retColor = s.avg_return > 0 ? "#00c896" : "#ff4455";
        const bw = s.n
          ? `<span style="color:#00c896;">+${fmt(s.best,1)}%</span> / <span style="color:#ff4455;">${fmt(s.worst,1)}%</span>`
          : "—";
        return `<tr>
          <td><strong>${s.source}</strong></td>
          <td class="num">${s.n}</td>
          <td class="num" style="color:${s.n?wrColor:'#8b929d'};">${s.n?fmt(s.win_rate,1)+"%":"—"}</td>
          <td class="num" style="color:${s.n?retColor:'#8b929d'};">${s.n?(s.avg_return>=0?"+":"")+fmt(s.avg_return,2)+"%":"—"}</td>
          <td class="num" style="font-size:11px;">${bw}</td>
        </tr>`;
      }).join("");
    }
    const rTbody = document.querySelector("#cal-recent-tbl tbody");
    const recent = (r.recent_realized || []).slice().reverse();  // most recent first
    if (!recent.length) {
      rTbody.innerHTML = `<tr><td colspan="6" class="muted">no realized round-trips yet</td></tr>`;
    } else {
      rTbody.innerHTML = recent.slice(0, 12).map(t => {
        const ret = t.return_pct;
        const color = ret >= 0 ? "#00c896" : "#ff4455";
        const sign = ret >= 0 ? "+" : "";
        const buyTs = (t.buy_ts || "").replace("T", " ").slice(5, 16);
        const sellTs = (t.sell_ts || "").replace("T", " ").slice(5, 16);
        const conf = t.confidence != null ? fmt(t.confidence, 2) : "—";
        const reason = (t.reasoning_excerpt || "").replace(/</g, "&lt;");
        return `<tr>
          <td class="muted" style="font-size:11px;">${buyTs} → ${sellTs}</td>
          <td><strong>${t.ticker}</strong></td>
          <td class="num" style="color:${color};font-weight:bold;">${sign}${fmt(ret,2)}%</td>
          <td class="num">${conf}</td>
          <td>${t.source||"—"}</td>
          <td style="font-size:11px;color:#dde1e7;">${reason}</td>
        </tr>`;
      }).join("");
    }
  } catch (e) { console.error("calibration:", e); }
}

// ───────── Decision pipeline health (new 2026-05-15, agent 4) ─────────
async function refreshDecisionHealth() {
  try {
    const r = await fetch(API_PREFIX + "/api/decision-health").then(r => r.json());
    if (r.error) {
      document.getElementById("dh-reason").textContent = "error: " + r.error;
      return;
    }
    const vmap = {
      HEALTHY:  ["#1b5e20", "#a5d6a7"],
      DEGRADED: ["#b8860b", "#000000"],
      CRITICAL: ["#b71c1c", "#ffffff"],
      NO_DATA:  ["#1f2126", "#8b929d"],
    };
    const [bg, fg] = vmap[r.verdict] || vmap.NO_DATA;
    const vEl = document.getElementById("dh-verdict");
    vEl.textContent = r.verdict + (r.verdict_window ? ` (${r.verdict_window})` : "");
    vEl.style.background = bg;
    vEl.style.color = fg;
    document.getElementById("dh-reason").textContent = r.verdict_reason || "";

    const w = (r.windows && r.windows["24h"]) || {};
    document.getElementById("dh-total").textContent = w.total != null ? w.total : "—";
    const failEl = document.getElementById("dh-fail");
    failEl.textContent = w.parse_fail_pct != null ? fmt(w.parse_fail_pct, 0) + "%" : "—";
    failEl.style.color = (w.parse_fail_pct || 0) >= 50 ? "#ff4455"
                       : (w.parse_fail_pct || 0) >= 25 ? "#ffa726" : "#4caf50";
    document.getElementById("dh-fills").textContent =
      (w.filled != null ? w.filled : "—") + (w.fill_pct != null ? ` (${fmt(w.fill_pct,1)}%)` : "");
    const c = r.confidence || {};
    const trendArrow = {rising:" ↑", falling:" ↓", flat:""}[c.trend] || "";
    document.getElementById("dh-conf").textContent =
      c.avg != null ? fmt(c.avg, 2) + trendArrow : "—";
    const cad = r.cadence || {};
    document.getElementById("dh-lastfill").textContent =
      cad.hours_since_fill != null ? fmt(cad.hours_since_fill, 1) + "h" : "never";
    const sc = r.signal_count || {};
    document.getElementById("dh-sigs").textContent =
      sc.avg != null ? fmt(sc.avg, 1) : "—";

    // action mix bars
    const mixColors = {FILLED:"#4caf50", HOLD:"#5c6bc0", BLOCKED:"#ffa726",
                       NO_DECISION:"#ff4455", OTHER:"#8b929d"};
    const mix = r.action_mix || [];
    const mixEl = document.getElementById("dh-mix");
    if (!mix.length) {
      mixEl.innerHTML = '<div class="muted">no decisions yet</div>';
    } else {
      mixEl.innerHTML = mix.map(m => `
        <div style="display:flex;align-items:center;gap:8px;margin:3px 0;font-size:12px;">
          <span style="width:96px;color:#dde1e7;">${m.category}</span>
          <div style="flex:1;background:#1f2126;border-radius:3px;height:14px;overflow:hidden;">
            <div style="width:${m.pct}%;height:100%;background:${mixColors[m.category]||"#8b929d"};"></div>
          </div>
          <span class="muted" style="width:96px;text-align:right;">${m.n} · ${fmt(m.pct,1)}%</span>
        </div>`).join("");
    }

    // recent decision tape
    const tape = r.recent || [];
    const tb = document.querySelector("#dh-tape tbody");
    if (!tape.length) {
      tb.innerHTML = `<tr><td colspan="5" class="muted">no cycles</td></tr>`;
    } else {
      tb.innerHTML = tape.map(d => {
        const col = mixColors[d.category] || "#8b929d";
        const t = d.timestamp ? d.timestamp.replace("T", " ").slice(5, 16) : "—";
        return `<tr>
          <td class="muted">${t}</td>
          <td><span style="color:${col};font-weight:bold;">${d.category}</span></td>
          <td>${(d.action || "—").replace(/</g,"&lt;")}</td>
          <td class="num">${d.confidence != null ? fmt(d.confidence,2) : "—"}</td>
          <td class="num">${d.signal_count != null ? d.signal_count : "—"}</td>
        </tr>`;
      }).join("");
    }
  } catch (e) { console.error("decision-health:", e); }
}

// ───────── Scorer reliability + confidence intervals (new 2026-05-15, agent 4) ─────────
async function refreshScorerConfidence() {
  try {
    const r = await fetch(API_PREFIX + "/api/scorer-confidence").then(r => r.json());
    if (r.error) {
      document.getElementById("scrl-meta").textContent = "error: " + r.error;
      return;
    }
    document.getElementById("scrl-asof").textContent =
      r.as_of ? r.as_of.replace("T", " ").slice(0, 16) : "—";
    const o = r.overall;
    if (!o) {
      document.getElementById("scrl-meta").textContent =
        `scorer not ready — ${r.n_samples || 0} replay samples (need more outcomes)`;
      return;
    }
    document.getElementById("scrl-meta").textContent =
      `trained on n=${r.n_train} · replayed over ${r.n_samples} historical outcomes · ` +
      `residual = predicted − realized return`;
    const dirEl = document.getElementById("scrl-dir");
    dirEl.textContent = fmt(o.directional_accuracy_pct, 1) + "%";
    dirEl.style.color = o.directional_accuracy_pct >= 65 ? "#4caf50"
                      : o.directional_accuracy_pct >= 55 ? "#ffa726" : "#ff4455";
    document.getElementById("scrl-mae").textContent = "±" + fmt(o.mae, 2) + "%";
    document.getElementById("scrl-band").textContent =
      fmt(o.resid_p10, 1) + " … +" + fmt(o.resid_p90, 1);
    document.getElementById("scrl-n").textContent = r.n_samples;

    // held positions with empirical band
    const pos = r.positions || [];
    const pb = document.querySelector("#scrl-pos tbody");
    if (!pos.length) {
      pb.innerHTML = `<tr><td colspan="6" class="muted">no open stock positions</td></tr>`;
    } else {
      const trustColor = {high:"#4caf50", medium:"#ffa726", low:"#ff4455", none:"#8b929d"};
      pb.innerHTML = pos.map(p => {
        const v = p.pred_5d_return_pct;
        const iv = p.interval || {};
        const range = (iv.low != null && iv.high != null)
          ? `${iv.low >= 0 ? "+" : ""}${fmt(iv.low,1)}% … ${iv.high >= 0 ? "+" : ""}${fmt(iv.high,1)}%`
          : "—";
        return `<tr>
          <td><strong>${p.ticker}</strong></td>
          <td class="num" style="color:${scorerColor(v)};font-weight:bold;">${v == null ? "—" : (v>=0?"+":"") + fmt(v,2) + "%"}</td>
          <td class="num" style="color:#dde1e7;">${range}</td>
          <td>${verdictBadge(p.verdict)}</td>
          <td class="num">${iv.directional_accuracy_pct != null ? fmt(iv.directional_accuracy_pct,0) + "%" : "—"}</td>
          <td><span style="color:${trustColor[iv.reliability]||"#8b929d"};">${iv.reliability || "—"}</span></td>
        </tr>`;
      }).join("");
    }

    // calibration table
    const cb = document.querySelector("#scrl-cal tbody");
    const buckets = r.buckets || [];
    if (!buckets.length) {
      cb.innerHTML = `<tr><td colspan="6" class="muted">not enough samples</td></tr>`;
    } else {
      cb.innerHTML = buckets.map(b => `<tr>
        <td>${(b.pred_lo>=0?"+":"") + fmt(b.pred_lo,1)}% … ${(b.pred_hi>=0?"+":"") + fmt(b.pred_hi,1)}%</td>
        <td class="num">${b.n}</td>
        <td class="num" style="color:${scorerColor(b.mean_actual)};">${(b.mean_actual>=0?"+":"") + fmt(b.mean_actual,2)}%</td>
        <td class="num muted">${fmt(b.resid_p10,1)} / +${fmt(b.resid_p90,1)}</td>
        <td class="num">±${fmt(b.mae,1)}</td>
        <td class="num" style="color:${b.directional_accuracy_pct>=65?"#4caf50":b.directional_accuracy_pct>=55?"#ffa726":"#ff4455"};">${fmt(b.directional_accuracy_pct,0)}%</td>
      </tr>`).join("");
    }
  } catch (e) { console.error("scorer-confidence:", e); }
}

// ───────── boot ─────────
refresh();
refreshSignals();
refreshAnalytics();
refreshSectorPulse();
refreshBriefing();
refreshSuggestions();
refreshRisk();
refreshEarningsRisk();
refreshGreeks();
refreshHeatmap();
refreshDedupedNews();
refreshScorer();
refreshThesis();
refreshDrawdown();
refreshCalibration();
refreshDecisionHealth();
refreshScorerConfidence();
setInterval(refresh, 15_000);
setInterval(refreshSignals, 30_000);
setInterval(refreshAnalytics, 30_000);
setInterval(refreshSectorPulse, 60_000);
setInterval(refreshBriefing, 60_000);
setInterval(refreshSuggestions, 45_000);
setInterval(refreshRisk, 30_000);
setInterval(refreshEarningsRisk, 300_000);
setInterval(refreshGreeks, 60_000);
setInterval(refreshHeatmap, 60_000);
setInterval(refreshDedupedNews, 45_000);
setInterval(refreshScorer, 60_000);
setInterval(refreshThesis, 60_000);
setInterval(refreshDrawdown, 30_000);
setInterval(refreshCalibration, 120_000);
setInterval(refreshDecisionHealth, 60_000);
setInterval(refreshScorerConfidence, 120_000);
showTab(INITIAL_TAB || "trader");
</script>
</div><!-- /.page-content -->

<nav class="bottom-nav" id="bottomNav">
  <a href="/" class="bottom-tab">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9.5L12 3l9 6.5V20a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1V9.5z"/></svg>
    <span>Home</span>
  </a>
  <a href="/intern/" class="bottom-tab">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M7 14l4-4 3 3 5-6"/></svg>
    <span>Intern</span>
  </a>
  <a href="/trader/" class="bottom-tab">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l6-6 4 4 8-8"/><path d="M17 7h4v4"/></svg>
    <span>Trader</span>
  </a>
  <a href="/intern/chat" class="bottom-tab">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-8.5 8.5 8.5 8.5 0 0 1-3.8-.9L3 21l1.9-5.7A8.38 8.38 0 0 1 4 11.5 8.5 8.5 0 0 1 12.5 3 8.38 8.38 0 0 1 21 11.5z"/></svg>
    <span>Chat</span>
  </a>
  <a href="/trader/backtests" class="bottom-tab">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v5h5"/><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/><path d="M12 7v5l4 2"/></svg>
    <span>Backtests</span>
  </a>
</nav>
<script>
(function(){
  const navToggle = document.getElementById('navToggle');
  const navDrawer = document.getElementById('navDrawer');
  const navOverlay = document.getElementById('navOverlay');
  if (navToggle) {
    navToggle.addEventListener('click', () => {
      navDrawer.classList.toggle('open');
      navOverlay.classList.toggle('open');
    });
    navOverlay.addEventListener('click', () => {
      navDrawer.classList.remove('open');
      navOverlay.classList.remove('open');
    });
  }
  document.querySelectorAll('.bottom-tab').forEach(tab => {
    if (tab.getAttribute('href') === window.location.pathname) {
      tab.classList.add('active');
    }
  });
})();
</script>
</body>
</html>
"""


def _api_prefix() -> str:
    return request.headers.get("X-Forwarded-Prefix", "").rstrip("/")


@app.route("/")
def index():
    return render_template_string(TEMPLATE, initial_tab="trader", api_prefix=_api_prefix())


@app.route("/backtests")
def backtests_page():
    return render_template_string(TEMPLATE, initial_tab="backtests", api_prefix=_api_prefix())


@app.route("/api/state")
def state():
    store = get_store()
    pf = store.get_portfolio()
    positions = store.open_positions()
    trades = store.recent_trades(40)
    decisions = store.recent_decisions(20)
    eq = store.equity_curve(500)
    sp = eq[-1]["sp500_price"] if eq else None
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
        from .backtest import BacktestStore, PRICE_CACHE_PATH, START_DATE, END_DATE
        import json as _json
        store = BacktestStore()
        runs = store.all_runs()
        completed = [r for r in runs if r.get("status") == "complete"]
        spy_baseline = completed[0].get("spy_return_pct") if completed else None

        # Compute QQQ return from cached prices (no network call)
        qqq_baseline = None
        try:
            if PRICE_CACHE_PATH.exists():
                px = _json.loads(PRICE_CACHE_PATH.read_text())
                qqq_prices = px.get("QQQ", {})
                start_str = START_DATE.isoformat()
                end_str = END_DATE.isoformat()
                # Find nearest cached prices to start and end dates
                dates = sorted(qqq_prices.keys())
                starts = [d for d in dates if d >= start_str]
                ends = [d for d in dates if d <= end_str]
                if starts and ends:
                    p0 = qqq_prices[starts[0]]
                    p1 = qqq_prices[ends[-1]]
                    if p0:
                        qqq_baseline = round((p1 - p0) / p0 * 100, 2)
        except Exception:
            pass

        return jsonify({
            "runs": runs,
            "spy_baseline": spy_baseline,
            "qqq_baseline": qqq_baseline,
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


@app.route("/api/backtests/compare")
def backtest_compare():
    """Side-by-side comparison of 2-4 backtest runs.

    Query: ``/api/backtests/compare?ids=1,2,3`` (comma-separated run_ids).

    Returns equity_curve points re-shaped for overlay rendering:
      - ``day_index`` = days since run's start_date, so runs with different
        windows can be drawn on the same x-axis.
      - ``value_pct`` = (value / start_value - 1) * 100, so returns compare
        on a normalized y-axis regardless of initial cash differences.

    Per-run summary fields (return %, vs_spy %, max drawdown, trade count,
    decision count, win rate) are computed from the same equity_curve + trades
    that the existing /api/backtests/<id> route already returns, so this is a
    pure aggregation — no new state.
    """
    raw_ids = request.args.get("ids", "").strip()
    if not raw_ids:
        return jsonify({"error": "missing ids — e.g. ?ids=1,2,3"}), 400
    try:
        ids = []
        for tok in raw_ids.split(","):
            tok = tok.strip()
            if not tok:
                continue
            ids.append(int(tok))
        if not ids:
            return jsonify({"error": "no valid ids"}), 400
        if len(ids) > 4:
            return jsonify({"error": "max 4 runs per comparison"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "ids must be comma-separated integers"}), 400

    try:
        from .backtest import BacktestStore
        from datetime import date
        store = BacktestStore()
        out_runs = []
        for rid in ids:
            detail = store.run_detail(rid)
            if not detail:
                out_runs.append({"run_id": rid, "error": "not found"})
                continue
            eq = detail.get("equity_curve") or []
            trades = detail.get("trades") or []
            # Normalize the equity curve for overlay.
            start_val = float(eq[0]["value"]) if eq else 1000.0
            start_date_str = detail.get("start_date") or (eq[0]["date"] if eq else None)
            try:
                start_d = date.fromisoformat(start_date_str) if start_date_str else None
            except (TypeError, ValueError):
                start_d = None

            curve = []
            peak = start_val
            max_dd = 0.0
            for p in eq:
                v = float(p.get("value") or 0.0)
                if v > peak:
                    peak = v
                if peak > 0:
                    dd = (peak - v) / peak * 100.0
                    if dd > max_dd:
                        max_dd = dd
                d_str = p.get("date")
                day_idx = None
                if start_d and d_str:
                    try:
                        day_idx = (date.fromisoformat(d_str) - start_d).days
                    except (TypeError, ValueError):
                        day_idx = None
                curve.append({
                    "date": d_str,
                    "day_index": day_idx,
                    "value": v,
                    "value_pct": round((v / start_val - 1.0) * 100.0, 3) if start_val else 0.0,
                })

            # Win rate from trades that we can pair: BUYs followed by a SELL on the
            # same ticker close at a higher price. Best-effort — backtest trades use
            # ``action`` ∈ {BUY, SELL, BUY_CALL, SELL_CALL, ...}; we score stocks only
            # so the metric stays interpretable.
            wins = 0
            losses = 0
            held: dict[str, list[tuple[float, float]]] = {}  # ticker -> [(qty, price)]
            for t in trades:
                act = (t.get("action") or "").upper()
                tk = t.get("ticker") or ""
                qty = float(t.get("qty") or 0)
                px = float(t.get("price") or 0)
                if not tk or qty <= 0 or px <= 0:
                    continue
                if act == "BUY":
                    held.setdefault(tk, []).append((qty, px))
                elif act == "SELL":
                    lots = held.get(tk) or []
                    remaining = qty
                    while remaining > 0 and lots:
                        lot_qty, lot_px = lots[0]
                        use = min(lot_qty, remaining)
                        if px > lot_px:
                            wins += 1
                        elif px < lot_px:
                            losses += 1
                        if use >= lot_qty:
                            lots.pop(0)
                        else:
                            lots[0] = (lot_qty - use, lot_px)
                        remaining -= use
                    held[tk] = lots
            total_rt = wins + losses
            win_rate = (wins / total_rt) if total_rt else None

            out_runs.append({
                "run_id": rid,
                "start_date": detail.get("start_date"),
                "end_date": detail.get("end_date"),
                "status": detail.get("status"),
                "total_return_pct": detail.get("total_return_pct"),
                "spy_return_pct": detail.get("spy_return_pct"),
                "vs_spy_pct": detail.get("vs_spy_pct"),
                "max_drawdown_pct": round(max_dd, 2),
                "n_trades": detail.get("n_trades"),
                "n_decisions": detail.get("n_decisions"),
                "n_round_trips": total_rt,
                "win_rate": round(win_rate, 4) if win_rate is not None else None,
                "final_value": detail.get("final_value"),
                "start_value": start_val,
                "n_points": len(curve),
                "equity_curve": curve,
            })
        return jsonify({
            "ids": ids,
            "n_runs": len(out_runs),
            "runs": out_runs,
        })
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


@app.route("/api/model-progress")
def model_progress():
    """Per-cycle aggregated returns for the Model Progress chart.

    Groups completed runs into cycles of RUNS_PER_CYCLE=5 by run_id order.
    Labels use actual run_id ranges so trimming old runs does not renumber cycles.
    """
    try:
        from .backtest import BacktestStore
        store = BacktestStore()
        rows = store.conn.execute(
            "SELECT run_id, total_return_pct, completed_at FROM backtest_runs "
            "WHERE status='complete' ORDER BY run_id"
        ).fetchall()
        if not rows:
            return jsonify({"cycles": []})

        cycle_size = 5  # RUNS_PER_CYCLE
        cycles = []
        for i in range(0, len(rows), cycle_size):
            chunk = rows[i:i + cycle_size]
            returns = [r["total_return_pct"] for r in chunk]
            run_ids = [r["run_id"] for r in chunk]
            # Use actual run_id range as label so chart is stable across trims
            label = f"#{run_ids[0]}" if len(run_ids) == 1 else f"#{run_ids[0]}-{run_ids[-1]}"
            cycles.append({
                "cycle": label,
                "run_start": run_ids[0],
                "best": round(max(returns), 2),
                "avg": round(sum(returns) / len(returns), 2),
                "worst": round(min(returns), 2),
                "n": len(returns),
                "completed_at": chunk[-1]["completed_at"],
            })
        return jsonify({"cycles": cycles, "total_runs": len(rows)})
    except Exception as e:
        return jsonify({"cycles": [], "error": str(e)})


def _holding_days(buy_ts: str | None, sell_ts: str | None) -> float | None:
    """Calendar days between a round-trip's first BUY and its closing SELL."""
    if not buy_ts or not sell_ts:
        return None
    try:
        b = datetime.fromisoformat(buy_ts.replace("Z", "+00:00"))
        s = datetime.fromisoformat(sell_ts.replace("Z", "+00:00"))
    except Exception:
        return None
    dd = (s - b).total_seconds() / 86400.0
    return round(dd, 2) if dd >= 0 else None


@app.route("/api/analytics")
def analytics_api():
    """Derived portfolio analytics — sector exposure, drawdown, Sharpe, win rate, daily P/L."""
    try:
        store = get_store()
        pf = store.get_portfolio()
        positions = store.open_positions()
        # Pull a generous trades sample for round-trip accounting.
        trades = list(reversed(store.recent_trades(2000)))  # oldest → newest
        eq = store.equity_curve(5000)  # most recent 5000, ascending after the bugfix

        total_value = pf.get("total_value") or 0.0

        # ─── 1. Sector exposure ───
        sector_usd: dict[str, float] = {}
        for p in positions:
            mult = 100 if p["type"] in ("call", "put") else 1
            price = p.get("current_price") or p["avg_cost"]
            val = price * p["qty"] * mult
            sec = _classify(p["ticker"])
            sector_usd[sec] = sector_usd.get(sec, 0.0) + val

        sector_pct = {
            s: round((v / total_value * 100) if total_value else 0.0, 2)
            for s, v in sector_usd.items()
        }
        cash_pct = round((pf.get("cash", 0) / total_value * 100) if total_value else 0.0, 2)

        # ─── 2. Max drawdown (peak-to-trough on equity curve) ───
        # Return None (not 0.0) when there's no equity history so the frontend's
        # `== null` branch fires and renders "—" instead of "-0.00 (0.00%)".
        max_dd_usd: float | None = None
        max_dd_pct: float | None = None
        if eq:
            max_dd_usd = 0.0
            max_dd_pct = 0.0
            peak = eq[0]["total_value"]
            for p in eq:
                v = p["total_value"]
                if v > peak:
                    peak = v
                dd_usd = peak - v
                dd_pct = (dd_usd / peak * 100) if peak else 0.0
                if dd_usd > max_dd_usd:
                    max_dd_usd = dd_usd
                if dd_pct > max_dd_pct:
                    max_dd_pct = dd_pct

        # ─── 3. Sharpe estimate from daily-bucketed returns ───
        # Bucket equity_curve by date, take last value per date, compute log returns,
        # annualize as mean/std * sqrt(252).
        sharpe = None
        daily_returns: list[float] = []
        by_day: dict[str, float] = {}
        for p in eq:
            day = (p["timestamp"] or "")[:10]
            if day:
                by_day[day] = p["total_value"]  # last write wins, leaves us with EOD close
        day_keys = sorted(by_day.keys())
        for i in range(1, len(day_keys)):
            prev = by_day[day_keys[i - 1]]
            cur = by_day[day_keys[i]]
            if prev and prev > 0:
                daily_returns.append((cur / prev) - 1.0)
        if len(daily_returns) >= 5:
            mean = sum(daily_returns) / len(daily_returns)
            var = sum((r - mean) ** 2 for r in daily_returns) / len(daily_returns)
            std = var ** 0.5
            sharpe = round((mean / std) * (252 ** 0.5), 2) if std > 0 else None

        # ─── 4. Win rate (round-trips per distinct position) ───
        # A round-trip closes when held qty returns to ≈ 0. P/L = proceeds - cost.
        # Key by (ticker, type, strike, expiry) so stock and option legs of the
        # same ticker don't conflate into a single round-trip.
        per_position: dict[tuple, dict] = {}
        round_trips: list[float] = []
        holding_days: list[float] = []  # one entry per closed round-trip
        for t in trades:
            typ = t.get("option_type") or "stock"
            key = (t["ticker"], typ, t.get("strike"), t.get("expiry"))
            rec = per_position.setdefault(
                key, {"cost": 0.0, "proceeds": 0.0, "held": 0.0, "first_buy_ts": None})
            if (t["action"] or "").startswith("BUY"):
                # Stamp the open time on the first BUY of a fresh round-trip.
                if abs(rec["held"]) < 1e-4:
                    rec["first_buy_ts"] = t.get("timestamp")
                rec["cost"] += t["value"]
                rec["held"] += t["qty"]
            elif (t["action"] or "").startswith("SELL"):
                rec["proceeds"] += t["value"]
                rec["held"] -= t["qty"]
                if abs(rec["held"]) < 1e-4:
                    round_trips.append(rec["proceeds"] - rec["cost"])
                    hd = _holding_days(rec["first_buy_ts"], t.get("timestamp"))
                    if hd is not None:
                        holding_days.append(hd)
                    rec["cost"] = rec["proceeds"] = rec["held"] = 0.0
                    rec["first_buy_ts"] = None

        wins = [p for p in round_trips if p > 0]
        losses = [p for p in round_trips if p <= 0]
        win_rate = round(len(wins) / len(round_trips) * 100, 2) if round_trips else None
        avg_winner = round(sum(wins) / len(wins), 2) if wins else None
        avg_loser = round(sum(losses) / len(losses), 2) if losses else None
        total_realized = round(sum(round_trips), 2) if round_trips else 0.0

        # ─── 4b. Profit factor + avg holding period ───
        # Profit factor = gross wins / gross losses. >1 means the edge survives
        # losers; a 50% win rate with PF 2.0 is a real edge, PF 0.8 is bleeding.
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 1e-9 else None
        avg_hold_days = (round(sum(holding_days) / len(holding_days), 2)
                         if holding_days else None)

        # ─── 4c. Sortino — like Sharpe but only downside vol is "risk" ───
        sortino = None
        if len(daily_returns) >= 5:
            dmean = sum(daily_returns) / len(daily_returns)
            downside = [r for r in daily_returns if r < 0]
            if downside:
                dvar = sum(r * r for r in downside) / len(daily_returns)
                dstd = dvar ** 0.5
                if dstd > 0:
                    sortino = round((dmean / dstd) * (252 ** 0.5), 2)

        # ─── 4d. S&P 500 beta + correlation (paired daily returns) ───
        sp_by_day: dict[str, float] = {}
        for p in eq:
            day = (p["timestamp"] or "")[:10]
            spx = p.get("sp500_price")
            if day and spx:
                sp_by_day[day] = spx
        port_ret: list[float] = []
        spx_ret: list[float] = []
        for i in range(1, len(day_keys)):
            d0, d1 = day_keys[i - 1], day_keys[i]
            if d0 in sp_by_day and d1 in sp_by_day:
                pv0, sv0 = by_day[d0], sp_by_day[d0]
                pv1, sv1 = by_day[d1], sp_by_day[d1]
                if pv0 > 0 and sv0 > 0:
                    port_ret.append(pv1 / pv0 - 1.0)
                    spx_ret.append(sv1 / sv0 - 1.0)
        sp500_beta = None
        sp500_corr = None
        if len(port_ret) >= 5:
            n = len(port_ret)
            mp = sum(port_ret) / n
            ms = sum(spx_ret) / n
            cov = sum((port_ret[i] - mp) * (spx_ret[i] - ms) for i in range(n)) / n
            var_s = sum((s - ms) ** 2 for s in spx_ret) / n
            var_p = sum((p - mp) ** 2 for p in port_ret) / n
            if var_s > 0:
                sp500_beta = round(cov / var_s, 2)
                if var_p > 0:
                    sp500_corr = round(cov / ((var_s ** 0.5) * (var_p ** 0.5)), 3)

        # ─── 4e. Calmar — annualized return ÷ max drawdown ───
        # Meaningless on <20 trading days of history, so gate it hard.
        calmar = None
        if len(daily_returns) >= 20 and max_dd_pct and max_dd_pct > 0:
            total_return_pct = (total_value / 1000.0 - 1.0) * 100.0
            years = len(day_keys) / 252.0
            if years > 0:
                calmar = round((total_return_pct / years) / max_dd_pct, 2)

        # ─── 5. Daily P/L (today only, UTC bucket) ───
        today = datetime.now(timezone.utc).date().isoformat()
        today_eq = [p for p in eq if (p["timestamp"] or "").startswith(today)]
        daily_pl = None
        daily_pl_pct = None
        if today_eq:
            open_val = today_eq[0]["total_value"]
            cur_val = total_value
            if open_val:
                daily_pl = round(cur_val - open_val, 2)
                daily_pl_pct = round(daily_pl / open_val * 100, 2)

        return jsonify({
            "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "total_value": round(total_value, 2),
            "cash_pct": cash_pct,
            "sector_exposure_pct": sector_pct,
            "sector_exposure_usd": {s: round(v, 2) for s, v in sector_usd.items()},
            "max_drawdown_usd": round(max_dd_usd, 2) if max_dd_usd is not None else None,
            "max_drawdown_pct": round(max_dd_pct, 2) if max_dd_pct is not None else None,
            "sharpe_annualized": sharpe,
            "n_trading_days": len(daily_returns),
            "n_round_trips": len(round_trips),
            "win_rate_pct": win_rate,
            "avg_winner_usd": avg_winner,
            "avg_loser_usd": avg_loser,
            "realized_pl_usd": total_realized,
            "profit_factor": profit_factor,
            "avg_holding_days": avg_hold_days,
            "sortino_annualized": sortino,
            "calmar_ratio": calmar,
            "sp500_beta": sp500_beta,
            "sp500_correlation": sp500_corr,
            "daily_pl_usd": daily_pl,
            "daily_pl_pct": daily_pl_pct,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _articles_db_path() -> Path | None:
    """Match how paper_trader.signals discovers the digital-intern articles.db."""
    import os
    usb = Path(os.environ.get("DIGITAL_INTERN_USB",
                              "/media/zeph/projects/digital-intern/db")) / "articles.db"
    if usb.exists():
        return usb
    local = Path("/home/zeph/digital-intern/data/articles.db")
    if local.exists():
        return local
    return None


def _ticker_news_pulse(tickers: list[str], hours: int = 24) -> dict[str, dict]:
    """For each ticker, count + top headline of articles mentioning it.

    Reads the articles DB in read-only mode. Live-only filter is applied so
    backtest/opus_annotation synthetic rows are excluded.
    """
    out: dict[str, dict] = {t.upper(): {
        "n": 0, "urgent": 0, "top_title": None, "top_url": None, "top_score": 0.0,
    } for t in tickers}
    path = _articles_db_path()
    if path is None:
        return out
    conn = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = conn.execute(
            "SELECT title, url, full_text, ai_score, urgency FROM articles "
            "WHERE first_seen >= ? AND ai_score > 0 "
            "AND url NOT LIKE 'backtest://%' "
            "AND source NOT LIKE 'backtest_%' "
            "AND source NOT LIKE 'opus_annotation%' "
            "ORDER BY ai_score DESC LIMIT 2000",
            (since,),
        ).fetchall()
    except Exception:
        return out
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    patterns = {t.upper(): re.compile(rf"(?:\$|\b){re.escape(t.upper())}\b") for t in tickers}
    for r in rows:
        body = r["title"] or ""
        if r["full_text"]:
            try:
                body = body + " " + zlib.decompress(r["full_text"]).decode("utf-8", "replace")
            except Exception:
                pass
        body_up = body.upper()
        for t, pat in patterns.items():
            if pat.search(body_up):
                rec = out[t]
                rec["n"] += 1
                if (r["urgency"] or 0) >= 1:
                    rec["urgent"] += 1
                if (r["ai_score"] or 0) > rec["top_score"]:
                    rec["top_score"] = r["ai_score"]
                    rec["top_title"] = r["title"]
                    rec["top_url"] = r["url"]
    return out


@app.route("/api/sector-pulse")
def sector_pulse_api():
    """Compact semis-sector card: price, day %, RSI, news count, top headline per ticker."""
    try:
        from . import market
        from .strategy import _QUANT_CACHE, get_quant_signals_live

        tickers = SECTOR_PULSE_TICKERS
        # Warm the quant cache only for tickers we don't already have fresh data for.
        # get_quant_signals_live respects its own 5-min TTL.
        try:
            get_quant_signals_live(tickers)
        except Exception:
            pass

        prices = market.get_prices(tickers)
        news = _ticker_news_pulse(tickers, hours=24)

        out = []
        for t in tickers:
            cached = _QUANT_CACHE.get(t)
            quant = cached[0] if cached else {}
            # Compute today's % change from quant signals' 1y history if we cached it.
            rsi = quant.get("RSI")
            mom_5d = quant.get("mom_5d")
            mom_20d = quant.get("mom_20d")
            macd = quant.get("MACD")
            vol_ratio = quant.get("vol_ratio")
            pct_from_52h = quant.get("pct_from_52h")
            nrec = news.get(t.upper(), {})
            out.append({
                "ticker": t,
                "price": prices.get(t),
                "rsi": rsi,
                "macd": macd,
                "mom_5d": mom_5d,
                "mom_20d": mom_20d,
                "vol_ratio": vol_ratio,
                "pct_from_52h": pct_from_52h,
                "news_count_24h": nrec.get("n", 0),
                "news_urgent_24h": nrec.get("urgent", 0),
                "top_headline": nrec.get("top_title"),
                "top_url": nrec.get("top_url"),
                "top_score": nrec.get("top_score") or 0.0,
            })
        return jsonify({
            "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tickers": out,
        })
    except Exception as e:
        return jsonify({"tickers": [], "error": str(e)}), 500


# ───────────────────────── Feature-dev additions (2026-05-14) ─────────────────────────
# Three additive endpoints + supporting helpers:
#   /api/suggestions  — co-pilot trade ideas from news × positions × quant signals
#   /api/risk         — concentration / leveraged-exposure / position-age / shock estimate
#   /api/briefing     — futures + market-open countdown + top urgent news
# All routes degrade gracefully — yfinance / signals / strategy imports are lazy and
# wrapped so a missing dependency returns a structured error instead of 500.

# Leverage factors for the SPY-shock dollar-at-risk estimate. Conservative single
# beta numbers chosen to be obviously approximate — this is decision support, not VaR.
_LEVERAGE_BETA = {
    "broad": 1.0,
    "broad_lev": 3.0,       # Most broad-leveraged are 3x; QLD/SSO are 2x but in the same bucket here
    "tech": 1.2,
    "tech_lev": 3.0,
    "crypto_lev": 2.5,
    "semis": 1.5,
    "semis_lev": 3.0,
    "optical": 1.4,
    "bio_lev": 3.0,
    "health_lev": 3.0,
    "fin_lev": 3.0,
    "housing_lev": 3.0,
    "util_lev": 3.0,
    "defense_lev": 3.0,
    "other": 1.0,
}

_LEVERAGED_SECTORS = {s for s in _LEVERAGE_BETA if s.endswith("_lev")}


def _position_ages_from_trades(open_positions: list[dict], trades_oldest_first: list[dict]) -> dict[str, int]:
    """For each currently-open ticker, return days since the earliest BUY in the
    most recent open lot. Walks trades chronologically and resets the open-lot
    timestamp every time the running quantity returns to ≈0."""
    open_tickers = {p["ticker"] for p in open_positions if p.get("type") == "stock"}
    earliest: dict[str, str] = {}
    held: dict[str, float] = {}
    for t in trades_oldest_first:
        tk = t.get("ticker")
        if tk not in open_tickers:
            continue
        act = (t.get("action") or "").upper()
        # Only stock trades affect stock-position age. BUY_CALL / SELL_PUT etc.
        # would otherwise corrupt the running stock quantity for this ticker.
        if act not in ("BUY", "SELL"):
            continue
        qty = float(t.get("qty") or 0)
        ts = t.get("timestamp") or ""
        if act == "BUY":
            if held.get(tk, 0.0) < 1e-6 or tk not in earliest:
                earliest[tk] = ts
            held[tk] = held.get(tk, 0.0) + qty
        else:  # SELL
            held[tk] = held.get(tk, 0.0) - qty
            if abs(held.get(tk, 0.0)) < 1e-6:
                earliest.pop(tk, None)
    now = datetime.now(timezone.utc)
    ages: dict[str, int] = {}
    for tk, ts in earliest.items():
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ages[tk] = max(0, (now - dt).days)
        except Exception:
            continue
    return ages


@app.route("/api/risk")
def risk_api():
    """Risk-focused portfolio panel. Fields are intentionally disjoint from
    /api/analytics: concentration, leveraged exposure, position age, stale flags,
    SPY-shock dollar-at-risk estimate. Pair with /api/analytics for full picture."""
    try:
        store = get_store()
        pf = store.get_portfolio()
        positions = store.open_positions()
        total_value = float(pf.get("total_value") or 0.0)
        cash = float(pf.get("cash") or 0.0)

        # ── Per-position market values + sector classification ──
        rows = []
        leveraged_usd = 0.0
        shock_usd = 0.0  # estimated $ change if SPY drops 3%
        for p in positions:
            mult = 100 if p["type"] in ("call", "put") else 1
            price = p.get("current_price") or p.get("avg_cost") or 0.0
            qty = float(p.get("qty") or 0)
            val = price * qty * mult
            sec = _classify(p["ticker"])
            beta = _LEVERAGE_BETA.get(sec, 1.0)
            # Options inherit underlying sector beta but with a rough 3x payoff
            # multiplier for at-the-money ITM exposure; cap at 4.
            if p["type"] in ("call", "put"):
                beta = min(beta * 3.0, 4.0)
                if p["type"] == "put":
                    beta = -beta  # puts profit on a drop
            shock_usd += -0.03 * beta * val  # negative = loss on -3% SPY
            if sec in _LEVERAGED_SECTORS:
                leveraged_usd += val
            rows.append({
                "ticker": p["ticker"],
                "type": p["type"],
                "sector": sec,
                "market_value": round(val, 2),
                "pct_port": round((val / total_value * 100) if total_value else 0.0, 2),
                "beta_est": round(beta, 2),
            })

        rows.sort(key=lambda r: -r["market_value"])
        largest = rows[0] if rows else None
        top3_pct = round(sum(r["pct_port"] for r in rows[:3]), 2)

        # ── Position ages from trade history ──
        trades_oldest_first = list(reversed(store.recent_trades(2000)))
        ages = _position_ages_from_trades(positions, trades_oldest_first)

        # ── Stale flag: held > 7d, |P/L| < 2% — likely sitting on dead money ──
        # store.open_positions() rows have current_price/avg_cost but no pl_pct,
        # so derive it here rather than reading a key that's always missing.
        stale = []
        for p in positions:
            tk = p["ticker"]
            avg = float(p.get("avg_cost") or 0.0)
            cur = float(p.get("current_price") or 0.0) or avg
            pl_pct_signed = ((cur - avg) / avg * 100) if avg else 0.0
            age = ages.get(tk)
            if age is not None and age >= 7 and abs(pl_pct_signed) < 2.0:
                stale.append({
                    "ticker": tk,
                    "age_days": age,
                    "pl_pct": round(pl_pct_signed, 2),
                    "market_value": round(
                        cur * float(p.get("qty") or 0)
                        * (100 if p["type"] in ("call", "put") else 1),
                        2,
                    ),
                })

        ages_list = sorted(ages.values()) if ages else []
        if ages_list:
            mid = len(ages_list) // 2
            if len(ages_list) % 2:
                median_age = ages_list[mid]
            else:
                median_age = round((ages_list[mid - 1] + ages_list[mid]) / 2)
        else:
            median_age = None

        return jsonify({
            "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "total_value": round(total_value, 2),
            "cash_usd": round(cash, 2),
            "cash_pct": round((cash / total_value * 100) if total_value else 0.0, 2),
            "n_positions": len(positions),
            "concentration_top1_pct": round(largest["pct_port"], 2) if largest else 0.0,
            "concentration_top1_ticker": largest["ticker"] if largest else None,
            "concentration_top3_pct": top3_pct,
            "leveraged_usd": round(leveraged_usd, 2),
            "leveraged_pct": round((leveraged_usd / total_value * 100) if total_value else 0.0, 2),
            "spy_shock_3pct_usd": round(shock_usd, 2),  # negative = loss
            "spy_shock_3pct_pct": round((shock_usd / total_value * 100) if total_value else 0.0, 2),
            "median_age_days": median_age,
            "max_age_days": max(ages.values()) if ages else None,
            "position_ages": ages,
            "stale_positions": stale,
            "positions_by_value": rows,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _next_market_open() -> tuple[datetime | None, int | None]:
    """Return (next_open_dt_utc, seconds_until). If market is open right now,
    returns the next close instead with a sign convention noted by the caller.
    Uses paper_trader.market constants — keeps the NYSE holiday calendar in one place."""
    try:
        from . import market as _mkt
    except Exception:
        return None, None
    now_utc = datetime.now(timezone.utc)
    now_ny = now_utc.astimezone(_mkt.NY)
    open_min = 9 * 60 + 30
    cur_min = now_ny.hour * 60 + now_ny.minute
    # If currently open, return next close.
    if _mkt.is_market_open(now_utc):
        close_dt = now_ny.replace(hour=16, minute=0, second=0, microsecond=0)
        return close_dt.astimezone(timezone.utc), int((close_dt - now_ny).total_seconds())
    # Walk forward day-by-day to find the next open day. The outer guard
    # `(not is_today or cur_min < open_min)` already excludes "today, past
    # market open" — by the time we'd consider returning today, we must be
    # before 9:30 AM NY, so no past-close edge case to handle.
    from datetime import timedelta as _td
    candidate = now_ny
    for _ in range(10):
        is_weekday = candidate.weekday() < 5
        is_holiday = candidate.date() in _mkt.NYSE_HOLIDAYS_2026
        is_today = candidate.date() == now_ny.date()
        if is_weekday and not is_holiday and (not is_today or cur_min < open_min):
            open_dt = candidate.replace(hour=9, minute=30, second=0, microsecond=0)
            return open_dt.astimezone(timezone.utc), int((open_dt - now_ny).total_seconds())
        candidate = candidate + _td(days=1)
        candidate = candidate.replace(hour=0, minute=0, second=0, microsecond=0)
    return None, None


@app.route("/api/briefing")
def briefing_api():
    """Pre-market / live briefing card. Combines market-open status, futures,
    top urgent overnight news, and a one-line summary string. Designed to be the
    first thing the user sees on the trader pane each morning."""
    try:
        from . import market as _mkt
        from . import signals as _sig

        now_utc = datetime.now(timezone.utc)
        is_open = _mkt.is_market_open(now_utc)
        next_dt, secs = _next_market_open()

        # ── Futures (cached 30s in market.get_futures_price) ──
        futures: dict[str, float | None] = {}
        for sym in ("ES=F", "NQ=F", "CL=F", "GC=F"):
            try:
                futures[sym] = _mkt.get_futures_price(sym)
            except Exception:
                futures[sym] = None

        # ── Urgent news from the last 8h (Reddit/Bloomberg-style overnight) ──
        urgent: list[dict] = []
        try:
            urgent = _sig.get_urgent_articles(minutes=8 * 60)[:5]
        except Exception:
            urgent = []
        urgent_compact = [{
            "title": (u.get("title") or "")[:140],
            "source": u.get("source"),
            "ai_score": u.get("ai_score"),
            "urgency": u.get("urgency"),
            "first_seen": u.get("first_seen"),
            "tickers": u.get("tickers", [])[:5],
        } for u in urgent]

        # ── High-score overnight signals as a secondary list ──
        top: list[dict] = []
        try:
            top = _sig.get_top_signals(n=5, hours=8, min_score=5.0)
        except Exception:
            top = []
        top_compact = [{
            "title": (s.get("title") or "")[:140],
            "source": s.get("source"),
            "ai_score": s.get("ai_score"),
            "tickers": s.get("tickers", [])[:5],
            "first_seen": s.get("first_seen"),
        } for s in top]

        # ── One-line summary ──
        if is_open:
            if secs is not None:
                hrs = secs // 3600
                mins = (secs % 3600) // 60
                status_line = f"Market OPEN — closes in {hrs}h{mins:02d}m"
            else:
                status_line = "Market OPEN"
        else:
            if secs is not None and next_dt is not None:
                hrs = secs // 3600
                mins = (secs % 3600) // 60
                status_line = f"Market CLOSED — opens in {hrs}h{mins:02d}m ({next_dt.astimezone(_mkt.NY).strftime('%a %H:%M %Z')})"
            else:
                status_line = "Market CLOSED"

        return jsonify({
            "as_of": now_utc.isoformat(timespec="seconds"),
            "market_open": is_open,
            "next_event_utc": next_dt.isoformat(timespec="seconds") if next_dt else None,
            "next_event_seconds": secs,
            "status_line": status_line,
            "futures": futures,
            "urgent_news": urgent_compact,
            "top_signals": top_compact,
            "urgent_count": len(urgent_compact),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _classify_action(ticker: str, held_qty: float, quant: dict, news_score: float, news_urgent: bool) -> tuple[str, float, list[str]]:
    """Co-pilot rules. Returns (action, conviction 0..1, reason_bullets).
    Conservative — never says BUY without at least one technical confirm."""
    notes: list[str] = []
    rsi = quant.get("RSI") if quant else None
    macd = quant.get("MACD") if quant else None
    mom5 = quant.get("mom_5d") if quant else None
    mom20 = quant.get("mom_20d") if quant else None

    # ── Technical scoring (-1..+1 bullish bias) ──
    bias = 0.0
    if rsi is not None:
        if rsi < 30:
            bias += 0.4; notes.append(f"RSI {rsi:.0f} oversold")
        elif rsi < 45:
            bias += 0.1; notes.append(f"RSI {rsi:.0f} cool")
        elif rsi > 70:
            bias -= 0.4; notes.append(f"RSI {rsi:.0f} overbought")
        elif rsi > 60:
            bias -= 0.1; notes.append(f"RSI {rsi:.0f} hot")
    if macd:
        if macd == "bullish":
            bias += 0.25; notes.append("MACD bullish")
        elif macd == "bearish":
            bias -= 0.25; notes.append("MACD bearish")
    if mom5 is not None:
        if mom5 > 3:
            bias += 0.15; notes.append(f"5d +{mom5:.1f}%")
        elif mom5 < -3:
            bias -= 0.15; notes.append(f"5d {mom5:.1f}%")
    if mom20 is not None:
        if mom20 > 8:
            bias += 0.1; notes.append(f"20d +{mom20:.1f}%")
        elif mom20 < -8:
            bias -= 0.1; notes.append(f"20d {mom20:.1f}%")

    bias = max(-1.0, min(1.0, bias))

    # ── News weight ──
    news_weight = min(news_score / 10.0, 1.0)
    if news_urgent:
        news_weight = min(news_weight + 0.2, 1.0)
        notes.insert(0, "URGENT news")

    # ── Action selection ──
    if held_qty > 0:
        # EXIT must be checked before TRIM: a strong bearish bias (< -0.5) also
        # satisfies the TRIM guard (bias < -0.3) when news is quiet, so testing
        # TRIM first swallowed the EXIT case and downgraded severity exactly
        # when the technical breakdown was strongest.
        if bias < -0.5:
            return "EXIT", min(0.65 + abs(bias) * 0.3, 0.95), notes
        if bias < -0.3 and news_weight < 0.4:
            return "TRIM", min(0.6 + abs(bias) * 0.3, 0.95), notes
        if bias > 0.25 and news_weight > 0.5:
            return "ADD", min(0.5 + bias * 0.3 + news_weight * 0.2, 0.95), notes
        return "HOLD", 0.4 + max(0.0, bias) * 0.2, notes
    else:
        # not held
        if news_weight > 0.65 and bias > 0.1:
            return "BUY", min(0.5 + news_weight * 0.3 + max(0.0, bias) * 0.2, 0.95), notes
        if news_weight > 0.5 or abs(bias) > 0.35:
            return "WATCH", min(0.3 + news_weight * 0.3 + abs(bias) * 0.2, 0.8), notes
        return "WATCH", 0.2 + news_weight * 0.2, notes


@app.route("/api/suggestions")
def suggestions_api():
    """Trade-idea co-pilot. Ranked list of BUY / ADD / TRIM / EXIT / WATCH cards.

    Inputs: top-scored articles from last 6h (digital-intern), live quant signals,
    current open positions. Output is *decision support*, not auto-execution —
    the live trader is still Opus 4.7 in strategy.py."""
    try:
        from . import signals as _sig

        # Pull top signals (broader window than the trader uses, for visibility).
        try:
            top_signals = _sig.get_top_signals(n=30, hours=6, min_score=5.0)
        except Exception as e:
            return jsonify({"error": f"signals unavailable: {e}", "suggestions": []})

        store = get_store()
        positions = store.open_positions()
        held: dict[str, float] = {}
        position_pl: dict[str, float] = {}
        for p in positions:
            if p.get("type") == "stock":
                held[p["ticker"]] = held.get(p["ticker"], 0.0) + float(p.get("qty") or 0)
                # store.open_positions() doesn't include pl_pct — derive from avg/current.
                avg = float(p.get("avg_cost") or 0.0)
                cur = float(p.get("current_price") or 0.0) or avg
                position_pl[p["ticker"]] = ((cur - avg) / avg * 100) if avg else 0.0

        # Build the candidate ticker set: (news-mentioned ∩ watchlist) ∪ currently held.
        # Constraining to the watchlist filters out the ticker-extractor's noise
        # (acronyms like GSPC / IXIC / DJI that yfinance can't price anyway).
        try:
            from .strategy import WATCHLIST as _WATCHLIST
            universe = {t.upper() for t in _WATCHLIST}
        except Exception:
            universe = set()
        universe |= {t.upper() for t in held}

        candidates: dict[str, dict] = {}
        for art in top_signals:
            for tk in art.get("tickers") or []:
                if not tk or len(tk) > 6:
                    continue
                if tk.upper() not in universe:
                    continue
                rec = candidates.setdefault(tk, {
                    "ticker": tk,
                    "news_count": 0,
                    "news_max_score": 0.0,
                    "news_urgent": False,
                    "top_headline": None,
                    "top_url": None,
                })
                rec["news_count"] += 1
                if (art.get("ai_score") or 0) > rec["news_max_score"]:
                    rec["news_max_score"] = float(art.get("ai_score") or 0)
                    rec["top_headline"] = (art.get("title") or "")[:140]
                    rec["top_url"] = art.get("url")
                if (art.get("urgency") or 0) >= 1:
                    rec["news_urgent"] = True
        for tk in held:
            candidates.setdefault(tk, {
                "ticker": tk,
                "news_count": 0,
                "news_max_score": 0.0,
                "news_urgent": False,
                "top_headline": None,
                "top_url": None,
            })

        # Pull quant signals in bulk (cached 5min).
        from . import market as _mkt
        try:
            from .strategy import get_quant_signals_live
            tickers = list(candidates.keys())
            quant = get_quant_signals_live(tickers) if tickers else {}
        except Exception:
            quant = {}

        # Live prices (bulk fetch from market.get_prices, cached 30s).
        try:
            prices = _mkt.get_prices(list(candidates.keys())) if candidates else {}
        except Exception:
            prices = {}

        out = []
        for tk, c in candidates.items():
            q = quant.get(tk, {})
            action, conviction, notes = _classify_action(
                tk,
                held.get(tk, 0.0),
                q,
                c["news_max_score"],
                c["news_urgent"],
            )
            out.append({
                "ticker": tk,
                "action": action,
                "conviction": round(conviction, 2),
                "price": prices.get(tk),
                "held_qty": held.get(tk, 0.0),
                "position_pl_pct": position_pl.get(tk),
                "news_count": c["news_count"],
                "news_max_score": round(c["news_max_score"], 1),
                "news_urgent": c["news_urgent"],
                "top_headline": c["top_headline"],
                "top_url": c["top_url"],
                "rsi": q.get("RSI"),
                "macd": q.get("MACD"),
                "mom_5d": q.get("mom_5d"),
                "mom_20d": q.get("mom_20d"),
                "reasons": notes,
            })

        # Rank: action priority then conviction.
        priority = {"EXIT": 0, "TRIM": 1, "BUY": 2, "ADD": 3, "WATCH": 4, "HOLD": 5}
        out.sort(key=lambda r: (priority.get(r["action"], 9), -r["conviction"]))
        out = out[:20]

        action_counts: dict[str, int] = {}
        for r in out:
            action_counts[r["action"]] = action_counts.get(r["action"], 0) + 1

        return jsonify({
            "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "n_candidates": len(candidates),
            "n_signals_used": len(top_signals),
            "action_counts": action_counts,
            "suggestions": out,
        })
    except Exception as e:
        return jsonify({"error": str(e), "suggestions": []}), 500


# ───────── Feature-dev additions (2026-05-14 part 2) ─────────
# /api/greeks         — portfolio-wide option Greeks (delta/gamma/theta/vega)
# /api/sector-heatmap — DRAM/semis bucket momentum + relative strength + news
# /api/news-deduped   — top signals after dedup + urgency decay (kills syndication noise)


@app.route("/api/greeks")
def greeks_api():
    """Per-leg and portfolio-wide Black-Scholes Greeks for open option positions.

    Stocks contribute pure delta. Options use implied vol from the live yfinance
    chain (DEFAULT_IV fallback when the chain has nothing useful)."""
    try:
        from .analytics.greeks import compute_position_greeks
        store = get_store()
        positions = store.open_positions()
        result = compute_position_greeks(positions)
        # Quick portfolio-level summary so callers don't have to recompute.
        total_value = float(store.get_portfolio().get("total_value") or 0.0)
        totals = result.get("totals", {})
        if total_value > 0:
            result["totals"]["delta_pct_port"] = round(
                totals.get("gross_notional", 0) / total_value * 100, 2
            )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _scorer_verdict(pred: float) -> str:
    """Bucket a predicted 5-day return into a coarse verdict label."""
    if pred >= 3.0:
        return "STRONG_HOLD"
    if pred >= 1.0:
        return "HOLD"
    if pred >= -1.0:
        return "NEUTRAL"
    if pred >= -3.0:
        return "TRIM"
    return "EXIT"


@app.route("/api/scorer-predictions")
def scorer_predictions_api():
    """DecisionScorer prediction per currently-held stock position.

    Builds a feature vector from live RSI/MACD/momentum + news sentiment for
    each held ticker, runs the trained scorer, and returns predicted 5-day
    forward return %. When the scorer isn't trained yet (<500 outcomes), the
    response still lists positions but ``is_trained`` is False so the UI can
    grey them out."""
    try:
        from .ml.decision_scorer import DecisionScorer
        from .strategy import get_quant_signals_live
        from . import signals as _sig
        from . import market as _mkt

        scorer = DecisionScorer()

        store = get_store()
        positions = store.open_positions()
        held_tickers = sorted({
            p["ticker"] for p in positions
            if p.get("type") == "stock" and (p.get("qty") or 0) > 0
        })

        result = {
            "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "is_trained": scorer.is_trained,
            "n_train": scorer.n_train,
            "gate_threshold": 500,
            "predictions": [],
        }
        if not held_tickers:
            return jsonify(result)

        # Live RSI / MACD / momentum — same source the live trader uses.
        quant = get_quant_signals_live(held_tickers) or {}
        # News-based "ml_score" proxy — average ai_score across mentions in the
        # last 4 hours. Matches the feature the model was trained on, since
        # backtest decisions used ml_score from articles in the same window.
        sent_list = _sig.ticker_sentiments(held_tickers, hours=4) or []
        sent_by_tk = {s["ticker"]: s for s in sent_list}

        # Crude regime proxy — SPY 5d momentum as the multiplier seed. Falls
        # back to 1.0 when unavailable so prediction still returns sensible.
        regime_mult = 1.0
        try:
            spy_q = get_quant_signals_live(["SPY"]).get("SPY") or {}
            spy_mom = spy_q.get("mom_5d")
            if isinstance(spy_mom, (int, float)):
                # Map roughly: +2% = bull (1.15), -2% = bear (0.85)
                regime_mult = max(0.7, min(1.3, 1.0 + spy_mom * 0.075))
        except Exception:
            pass

        preds = []
        for tk in held_tickers:
            q = quant.get(tk) or {}
            sent = sent_by_tk.get(tk) or {}
            # Use max_score for ml_score proxy — captures the strongest signal
            # in the window rather than diluting by averaging across mentions.
            ml_score = float(sent.get("max_score") or 0.0)
            pred = scorer.predict(
                ml_score=ml_score,
                rsi=q.get("rsi"),
                macd=q.get("macd_signal"),
                mom5=q.get("mom_5d"),
                mom20=q.get("mom_20d"),
                regime_mult=regime_mult,
                ticker=tk,
                vol_ratio=q.get("vol_ratio"),
                bb_pos=q.get("bb_position"),
            )
            preds.append({
                "ticker": tk,
                "pred_5d_return_pct": round(float(pred), 3),
                "verdict": _scorer_verdict(float(pred)),
                "rsi": q.get("RSI"),
                "macd": q.get("MACD"),
                "mom_5d": q.get("mom_5d"),
                "mom_20d": q.get("mom_20d"),
                "ml_news_score": round(ml_score, 2),
                "news_count": sent.get("n", 0),
                "news_urgent": sent.get("urgent", 0),
            })
        # Highest predicted return first so the trader sees winners at the top.
        preds.sort(key=lambda r: -(r["pred_5d_return_pct"] or 0))
        result["n_positions"] = len(preds)
        result["regime_mult"] = round(regime_mult, 3)
        result["predictions"] = preds
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "predictions": []}), 500


@app.route("/api/sector-heatmap")
def sector_heatmap_api():
    """DRAM / semis sector heatmap. Buckets: memory_core, semis_equipment, foundry,
    design, memory_leveraged, optical, etf. Each ticker carries mom_5d, mom_20d,
    RSI, vs_sox_5d, and the 24h news pulse from digital-intern."""
    try:
        from .analytics.sector_heatmap import compute_heatmap
        return jsonify(compute_heatmap())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/news-deduped")
def news_deduped_api():
    """Top signals after dedup + exponential urgency decay.

    Default window: last 6 hours, min_score 4.0. Halflife 4h means urgency=1 at
    t=0 becomes 0.5 at t=4h, 0.25 at t=8h, and falls out at 0.125 (5h+) when the
    default cutoff is 0.5. ?hours= and ?min_score= and ?halflife= are tunable."""
    try:
        from . import signals as _sig
        from .analytics.news_dedup import dedupe_and_decay
        hours = int(request.args.get("hours", 6))
        min_score = float(request.args.get("min_score", 4.0))
        halflife = float(request.args.get("halflife", 4.0))
        # Pull a fat candidate list — dedup will compress it heavily.
        raw = _sig.get_top_signals(n=80, hours=hours, min_score=min_score)
        cleaned = dedupe_and_decay(raw, halflife_hours=halflife, min_effective=0.0)
        # Compute the "compression ratio" for the UI so the user can see how
        # much noise was suppressed.
        return jsonify({
            "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "n_raw": len(raw),
            "n_after_dedup": len(cleaned),
            "compression_ratio": round(len(raw) / max(len(cleaned), 1), 2),
            "halflife_hours": halflife,
            "articles": cleaned[:30],
        })
    except Exception as e:
        return jsonify({"error": str(e), "articles": []}), 500


# ───────── Feature-dev additions (2026-05-15) ─────────
# /api/position-thesis  — per-position integrated card (news, scorer, technicals, last decision, verdict)
# /api/calibration       — confidence calibration + signal-source attribution from realized trades
# /api/drawdown          — current DD anatomy: peak/trough, time-in-DD, per-position contribution


@app.route("/api/position-thesis")
def position_thesis_api():
    """Per-open-position thesis cards.

    Combines DecisionScorer prediction, live quant signals, news pulse from
    digital-intern, and the most recent Opus decision that touched the ticker.
    Each card carries a coarse verdict and a one-line thesis."""
    try:
        from .analytics.position_thesis import build_thesis_cards
        from .ml.decision_scorer import DecisionScorer
        from .strategy import get_quant_signals_live
        from . import signals as _sig

        store = get_store()
        positions = store.open_positions()
        held = sorted({p["ticker"] for p in positions
                       if p.get("type") == "stock" and (p.get("qty") or 0) > 0})

        # Reuse the same scorer prediction shape as /api/scorer-predictions
        # without duplicating its logic — call into the live trader helpers.
        quant = get_quant_signals_live(held) if held else {}
        sent_list = _sig.ticker_sentiments(held, hours=4) if held else []
        sent_by_tk = {s["ticker"]: s for s in sent_list}

        regime_mult = 1.0
        try:
            spy_q = (get_quant_signals_live(["SPY"]) or {}).get("SPY") or {}
            mm = spy_q.get("mom_5d")
            if isinstance(mm, (int, float)):
                regime_mult = max(0.7, min(1.3, 1.0 + mm * 0.075))
        except Exception:
            pass

        scorer = DecisionScorer()
        scorer_preds = []
        for tk in held:
            q = quant.get(tk) or {}
            sent = sent_by_tk.get(tk) or {}
            # Mirror /api/scorer-predictions exactly so both endpoints agree:
            # the scorer wants numeric macd_signal, not the "bullish"/"bearish"
            # MACD label (which _to_float silently zeroes).
            pred = scorer.predict(
                ml_score=float(sent.get("max_score") or 0.0),
                rsi=q.get("rsi"), macd=q.get("macd_signal"),
                mom5=q.get("mom_5d"), mom20=q.get("mom_20d"),
                regime_mult=regime_mult, ticker=tk,
                vol_ratio=q.get("vol_ratio"), bb_pos=q.get("bb_position"),
            )
            scorer_preds.append({
                "ticker": tk,
                "pred_5d_return_pct": round(float(pred), 3),
            })

        decisions = store.recent_decisions(limit=80)
        out = build_thesis_cards(positions, decisions, scorer_preds, quant)
        out["scorer_trained"] = scorer.is_trained
        out["scorer_n_train"] = scorer.n_train
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e), "cards": []}), 500


@app.route("/api/calibration")
def calibration_api():
    """Confidence calibration + signal-source attribution.

    Buckets matched-and-closed BUY decisions by Opus's stated confidence
    (0.0-0.5, 0.5-0.65, 0.65-0.8, 0.8-1.0) and computes win rate + avg return
    per bucket. Also classifies decisions by reasoning keywords into
    news/technical/mixed/other and computes the same stats per source."""
    try:
        from .analytics.calibration import build_calibration
        store = get_store()
        decisions = store.recent_decisions(limit=500)
        trades = store.recent_trades(limit=500)
        return jsonify(build_calibration(decisions, trades))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/drawdown")
def drawdown_api():
    """Drawdown anatomy: peak/trough, time-in-DD, per-position contribution.

    Returns a structured 0% when the portfolio is at a fresh high so the UI
    can show a green high-water badge. ``recovery_pct`` measures how much of
    the trough has been clawed back."""
    try:
        from .analytics.drawdown import compute_drawdown
        store = get_store()
        eq = store.equity_curve(limit=2000)
        positions = store.open_positions()
        return jsonify(compute_drawdown(eq, positions))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/earnings-risk")
def earnings_risk_api():
    """Upcoming earnings cross-referenced against held positions + watchlist.

    Earnings are the #1 scheduled risk event — a position into a print can gap
    10%+ overnight. This pulls digital-intern's earnings calendar (:8080) and
    flags which holdings and watchlist names report soon, with a risk tier:
      HELD_IMMINENT  — you hold it and it reports within 3 days
      HELD_SOON      — you hold it and it reports within the horizon
      WATCH          — on the watchlist, not held
    """
    import json as _json
    import urllib.request as _urllib

    try:
        store = get_store()
        positions = store.open_positions()
        held: dict[str, float] = {}
        for p in positions:
            t = (p.get("ticker") or "").upper()
            if not t:
                continue
            mult = 100 if p.get("type") in ("call", "put") else 1
            price = p.get("current_price") or p.get("avg_cost") or 0.0
            held[t] = held.get(t, 0.0) + price * (p.get("qty") or 0.0) * mult

        try:
            from .strategy import WATCHLIST as _WATCHLIST
            watch = {t.upper() for t in _WATCHLIST}
        except Exception:
            watch = set()

        events = []
        source_ok = True
        try:
            with _urllib.urlopen(
                "http://127.0.0.1:8080/api/earnings", timeout=4) as resp:
                snap = _json.loads(resp.read().decode("utf-8"))
            events = snap.get("events") or []
        except Exception:
            source_ok = False

        out = []
        for ev in events:
            tk = (ev.get("ticker") or "").upper()
            if not tk:
                continue
            days = ev.get("days_away")
            in_port = tk in held
            on_watch = tk in watch
            if not in_port and not on_watch:
                continue
            if in_port and days is not None and days <= 3:
                tier = "HELD_IMMINENT"
            elif in_port:
                tier = "HELD_SOON"
            else:
                tier = "WATCH"
            out.append({
                "ticker": tk,
                "earnings_date": ev.get("earnings_date"),
                "days_away": days,
                "tier": tier,
                "held": in_port,
                "exposure_usd": round(held.get(tk, 0.0), 2) if in_port else 0.0,
            })
        # Held + soonest first; tier rank keeps imminent risk at the top.
        tier_rank = {"HELD_IMMINENT": 0, "HELD_SOON": 1, "WATCH": 2}
        out.sort(key=lambda e: (tier_rank.get(e["tier"], 9),
                                e["days_away"] if e["days_away"] is not None else 1e9))
        held_at_risk = round(sum(e["exposure_usd"] for e in out if e["held"]), 2)
        return jsonify({
            "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_ok": source_ok,
            "events": out,
            "n_held_reporting": sum(1 for e in out if e["held"]),
            "n_imminent": sum(1 for e in out if e["tier"] == "HELD_IMMINENT"),
            "held_exposure_at_risk_usd": held_at_risk,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ───────── Feature-dev additions (2026-05-15, agent 4) ─────────
# /api/scorer-confidence — empirical ± bands + directional hit-rate for the
#                          DecisionScorer, so its point predictions can be
#                          trusted (or distrusted) with a real error bar.
# /api/decision-health   — is the live Opus trader actually deciding? Surfaces
#                          the NO_DECISION (parse-failure) rate the dashboard
#                          otherwise hides entirely.


def _live_scorer_predictions(scorer) -> list[dict]:
    """Predicted 5d return for each held stock position (live feature vector).

    Same feature construction as ``/api/scorer-predictions`` — kept as a shared
    helper so the confidence endpoint stays in lockstep with the original."""
    from .strategy import get_quant_signals_live
    from . import signals as _sig

    store = get_store()
    held = sorted({
        p["ticker"] for p in store.open_positions()
        if p.get("type") == "stock" and (p.get("qty") or 0) > 0
    })
    if not held:
        return []
    quant = get_quant_signals_live(held) or {}
    sent_by_tk = {s["ticker"]: s for s in (_sig.ticker_sentiments(held, hours=4) or [])}
    regime_mult = 1.0
    try:
        spy_mom = (get_quant_signals_live(["SPY"]).get("SPY") or {}).get("mom_5d")
        if isinstance(spy_mom, (int, float)):
            regime_mult = max(0.7, min(1.3, 1.0 + spy_mom * 0.075))
    except Exception:
        pass
    preds = []
    for tk in held:
        q = quant.get(tk) or {}
        sent = sent_by_tk.get(tk) or {}
        ml_score = float(sent.get("max_score") or 0.0)
        pred = scorer.predict(
            ml_score=ml_score, rsi=q.get("rsi"), macd=q.get("macd_signal"),
            mom5=q.get("mom_5d"), mom20=q.get("mom_20d"), regime_mult=regime_mult,
            ticker=tk, vol_ratio=q.get("vol_ratio"), bb_pos=q.get("bb_position"),
        )
        preds.append({
            "ticker": tk,
            "pred_5d_return_pct": round(float(pred), 3),
            "verdict": _scorer_verdict(float(pred)),
            "rsi": q.get("RSI"), "mom_5d": q.get("mom_5d"), "mom_20d": q.get("mom_20d"),
        })
    return preds


def _load_decision_outcomes(max_rows: int = 4000) -> list[dict]:
    """Tail of data/decision_outcomes.jsonl — the scorer's own training history."""
    import json as _json
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "data" / "decision_outcomes.jsonl"
    if not path.exists():
        return []
    rows: list[dict] = []
    for ln in path.read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(_json.loads(ln))
        except Exception:
            continue
    return rows[-max_rows:]


@app.route("/api/scorer-confidence")
def scorer_confidence_api():
    """Empirical prediction intervals + reliability for the DecisionScorer.

    Replays the trained scorer over its own outcome history to measure how far
    its predictions actually land from realized returns. Returns a calibration
    table (residual P10/P50/P90 + directional hit-rate per prediction band) and,
    for each held stock position, the live prediction wrapped in an empirical
    [low, high] band drawn from the matching band's residual quantiles."""
    try:
        from .ml.decision_scorer import DecisionScorer
        from .analytics.scorer_confidence import build_scorer_confidence, interval_for

        scorer = DecisionScorer()
        outcomes = _load_decision_outcomes()
        conf = build_scorer_confidence(outcomes, scorer)

        positions = []
        if conf.get("overall"):
            for p in _live_scorer_predictions(scorer):
                iv = interval_for(p["pred_5d_return_pct"], conf)
                positions.append({**p, "interval": iv})
            positions.sort(key=lambda r: -(r["pred_5d_return_pct"] or 0))
        conf["positions"] = positions
        return jsonify(conf)
    except Exception as e:
        return jsonify({"error": str(e), "buckets": [], "positions": []}), 500


@app.route("/api/decision-health")
def decision_health_api():
    """Health of the live decision pipeline — action mix, parse-failure rate,
    confidence trend, cadence. Surfaces NO_DECISION ('claude returned no
    parseable JSON') cycles that no other dashboard panel exposes."""
    try:
        from .analytics.decision_health import build_decision_health
        decisions = get_store().recent_decisions(limit=2000)
        return jsonify(build_decision_health(decisions))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def run(host: str = "0.0.0.0", port: int = 8090):
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    run()
