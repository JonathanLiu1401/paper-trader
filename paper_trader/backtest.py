"""Backtesting engine — runs N independent year-long simulations.

Each run starts with $1000, samples every 5th NYSE trading day,
fetches historical news from GDELT, scores with a keyword heuristic,
and asks Opus 4.7 for trading decisions. Stocks-only (no options —
yfinance has no historical option prices). Stop-loss / take-profit
are checked daily between sampled decisions using cached closes.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf

from .strategy import MODEL

ROOT = Path(__file__).resolve().parent.parent
BACKTEST_DB = ROOT / "backtest.db"
CACHE_DIR = ROOT / "data" / "backtest_cache"
GDELT_CACHE = CACHE_DIR / "gdelt"
PRICE_CACHE_PATH = CACHE_DIR / "prices.json"

START_DATE = date(2025, 5, 13)
END_DATE = date(2026, 5, 13)
INITIAL_CASH = 1000.0
SAMPLE_EVERY_N_DAYS = 5
GDELT_RATE_LIMIT_S = 5.5       # GDELT actual limit is ~1 req/5s; use 5.5 for safety
GDELT_MAX_RECORDS = 100
GDELT_RETRY_BACKOFF_S = 20.0  # reduced; 30s was too conservative
GDELT_WARM_WORKERS = 3        # parallel workers for cache pre-warming
OPUS_TIMEOUT_S = 150

WATCHLIST = [
    # Core US large-cap + semis (kept from v1 watchlist)
    "SPY", "QQQ", "NVDA", "AMD", "MU", "LITE", "AMAT", "LRCX",
    "SMH", "TSM", "INTC", "QCOM", "AAPL", "MSFT", "META", "GOOGL",
    "AMZN", "BTC-USD", "GC=F",
    # Global / ADR
    "BABA", "ASML", "SAP", "NVO", "TM", "SONY", "HSBC", "BP", "RIO", "BHP",
    # US financials
    "GS", "JPM", "BAC",
    # Energy / healthcare / payments
    "XOM", "CVX", "LLY", "UNH", "V", "MA",
    # Fintech / crypto-adjacent / speculative
    "SHOP", "SQ", "COIN", "MSTR", "PLTR", "RIVN", "NIO", "ARKK",
    # Macro / commodity ETFs
    "TLT", "GLD", "SLV", "USO", "UNG",
    # Leveraged ETFs — 3x Bull
    "TQQQ", "UPRO", "SPXL", "UDOW", "URTY",        # index 3x
    "SOXL", "TECL", "FNGU", "CURE", "LABU",         # sector 3x (semis/tech/health/bio)
    "NAIL", "WANT", "DFEN", "MIDU", "TNA",           # housing/China/defense/mid/small 3x
    "DPST", "FAS", "HIBL", "UTSL",                   # banks/financials/high-beta/utilities 3x
    # Leveraged ETFs — 2x Bull
    "QLD", "SSO", "MVV", "SAA", "UWM",               # index 2x
    "NVDU", "MSFU", "AMZU", "GOOGU", "METAU",        # single-stock 2x (NVDA/MSFT/AMZN/GOOG/META)
    "TSLT", "AAPLU", "CONL", "TSLL",                 # Tesla/Apple/Coinbase 2x
    "LNOK", "SMCI2X", "PLTU",                        # Nokia/SMCI/Palantir 2x
    "USD", "ROM", "UXI", "UYG",                      # tech/industrial/financial 2x
    # Leveraged ETFs — Bear / Inverse (for hedging)
    "SQQQ", "SPXS", "SDOW", "SRTY",                  # index 3x inverse
    "SOXS", "TECS", "FNGD",                          # sector 3x inverse
    "TZA", "FAZ", "HIBS",                            # small/financial/high-beta inverse
    # Crypto/commodity leveraged
    "BITX", "BITU", "ETHU",                          # crypto 2x
    "BOIL", "UNG", "UCO", "AGQ",                     # nat gas/oil/silver 2x
    # Market structure / sector rotation gauges
    "^VIX", "XLK", "XLE", "XLF", "XLV", "XLI",
]

# Subset of the watchlist for which we compute heavier technical indicators
# (RSI/MACD/MA crossover/volume/52w proximity). Top 10 most-traded large caps
# plus the index proxies.
QUANT_SIGNAL_TICKERS = [
    "SPY", "QQQ", "NVDA", "AMD", "MU", "TSM", "AAPL", "MSFT", "META", "TQQQ",
]
# LNOK is a thin OTC name and yfinance often returns nothing → omitted from default fetch.

# ─────────────────────────── trading personas ───────────────────────────
# Each parallel run gets a distinct style so the 10 runs do not converge on
# identical trades when fed the same news. Keyed by run_id; callers should map
# arbitrary run_ids onto 1..10 with ((run_id - 1) % 10) + 1 so continuous
# cycling stays inside the dict.
PERSONAS: dict[int, dict[str, str]] = {
    1: {
        "name": "Value Investor",
        "style": (
            "You are a deep-value investor in the Buffett / Graham tradition. "
            "Hunt for undervalued cash-flow machines: low P/E, low P/B, high free cash "
            "flow yield, durable competitive moats, healthy balance sheets. Be skeptical "
            "of hype; prefer boring mature businesses trading below intrinsic value. "
            "Avoid momentum chases and unprofitable speculative names entirely. Hold for "
            "the thesis to play out — patience is the edge."
        ),
    },
    2: {
        "name": "Momentum Trader",
        "style": (
            "You are a price-momentum trader. Buy what is already going up. Earnings "
            "beats with raised guidance are gold — pile in. Chase breakouts, ride trends, "
            "respect the tape. Strength begets strength; weakness begets weakness. Cut "
            "losers fast, let winners run. Avoid contrarian bottom-fishing; never catch "
            "falling knives. Stops are tight; size scales with conviction in the trend."
        ),
    },
    3: {
        "name": "Contrarian",
        "style": (
            "You are a contrarian investor. Buy fear, sell greed. When headlines scream "
            "panic and quality names get dumped in indiscriminate selloffs, you step in. "
            "When everyone is euphoric and price targets are being raised in unison, you "
            "trim. Look for oversold quality — strong businesses under temporary clouds. "
            "Ignore momentum; trust mean reversion. Comfortable being early and lonely."
        ),
    },
    4: {
        "name": "Global Macro",
        "style": (
            "You are a global macro trader. You think in regimes: rates, inflation, FX, "
            "commodities, central bank policy, geopolitics. Translate macro views into "
            "trades — long TLT when you expect rates to fall, long GLD/SLV on real-rate "
            "compression, long USO/UNG on supply shocks, long/short FX-sensitive ADRs on "
            "currency moves. Stocks are a vehicle for a macro thesis, not the thesis."
        ),
    },
    5: {
        "name": "Growth at a Reasonable Price (GARP)",
        "style": (
            "You are a GARP investor — Peter Lynch / Terry Smith style. Find high-quality "
            "compounders growing revenue >15% with sane multiples and improving margins. "
            "Avoid pure value traps AND avoid bubble-multiple growth. The sweet spot is "
            "underappreciated quality growth: AI infrastructure, healthcare innovators, "
            "high-ROIC consumer brands. Size moderately and hold for the compounding."
        ),
    },
    6: {
        "name": "Quant / Event-Driven",
        "style": (
            "You are a pure-signal quant. Trade the news, not the story. Earnings beats, "
            "guidance revisions, FDA approvals, M&A leaks, regulatory catalysts — react "
            "fast and unemotionally. Treat each decision as a signal-weighted bet. "
            "No narratives, no loyalty to tickers, just probabilistic edge on catalysts. "
            "Set tight stop-losses; close positions when the catalyst is priced in."
        ),
    },
    7: {
        "name": "Sector Rotator",
        "style": (
            "You are a sector rotator. Capital flows between sectors as the macro cycle "
            "turns — energy in inflation, tech in disinflation, financials when curves "
            "steepen, defensives in slowdowns. Use ETFs (XLE, XLK, XLF, SMH, ARKK) and "
            "sector leaders to express rotation views. Always be long *something*; cash "
            "is the absence of a thesis. Rotate aggressively when the regime changes."
        ),
    },
    8: {
        "name": "Small / Mid Cap Hunter",
        "style": (
            "You are a small/mid cap specialist. Mega-caps are crowded and efficiently "
            "priced; your edge is in names below ~$50B market cap that institutions "
            "overlook. Hunt for hidden compounders, niche category leaders, post-IPO "
            "orphans. Avoid SPY/QQQ/NVDA/AAPL/MSFT/GOOGL/AMZN/META unless they are part "
            "of a hedge. Prefer LITE, MU (mid-cap when undervalued), RIVN, NIO, COIN, "
            "PLTR, MSTR, SHOP, SQ. Concentrate; small caps reward conviction."
        ),
    },
    9: {
        "name": "ESG / Thematic",
        "style": (
            "You are a thematic investor riding mega-trends. Clean energy transition, AI "
            "infrastructure (compute, power, cooling), semiconductor sovereignty, GLP-1 "
            "healthcare revolution, electrification. Buy the picks-and-shovels: NVDA/AMD/"
            "TSM/ASML for AI compute, LLY for GLP-1, RIVN/NIO/TSLA for EVs, ARKK for "
            "innovation beta. Ignore quarter-to-quarter noise; the trend is 5+ years."
        ),
    },
    10: {
        "name": "Pure Speculator",
        "style": (
            "You are a high-conviction speculator. Asymmetric payoffs only — small "
            "downside, massive upside. Concentrated bets, no diversification cult. "
            "When you see asymmetric setup (BTC-USD on macro shifts, MSTR/COIN as "
            "crypto leverage, MU on memory super-cycles, biotech catalysts, deep OTM "
            "macro plays via TLT/USO) — go big. 100% position sizing is fine. Cash "
            "between trades, full send when the setup is right. No half-measures."
        ),
    },
}


def persona_for(run_id: int) -> dict[str, str]:
    """Map any run_id to one of the 10 personas (cycles after 10)."""
    key = ((int(run_id) - 1) % len(PERSONAS)) + 1
    return PERSONAS[key]

KEYWORD_GROUPS = [
    "stock market earnings semiconductor",
    "NVDA AMD Micron earnings revenue",
    "Federal Reserve interest rates inflation",
    "SP500 market rally selloff",
    "Micron DRAM memory chip supply",
    "Lumentum photonics optical",
    # Global / multi-asset coverage for broader ML training signal
    "global markets central bank interest rates",
    "earnings revenue profit loss guidance",
    "commodity oil gold copper energy",
    "cryptocurrency bitcoin ethereum blockchain",
    "European stocks DAX FTSE earnings",
    "Asian markets Nikkei Hang Seng",
    "emerging markets China India Brazil",
    "currency forex dollar euro yen",
]

# Heuristic scorer lexicon
BUY_PHRASES = [
    "beat earnings", "earnings beat", "revenue beat", "guidance raised",
    "raised guidance", "record revenue", "strong demand", "supply shortage",
    "upgrade", "outperform", "all-time high", "rally", "surge", "soar",
    "buy rating", "price target raised", "expansion", "breakthrough",
]
SELL_PHRASES = [
    "miss earnings", "earnings miss", "guidance cut", "cut guidance",
    "layoffs", "inventory correction", "downgrade", "underperform",
    "recession", "selloff", "plunge", "tumble", "sell rating",
    "price target cut", "bankruptcy", "fraud", "investigation", "crash",
]
SEMIS_TICKERS = {"NVDA", "AMD", "MU", "LRCX", "AMAT", "TSM", "INTC", "ASML", "KLAC", "MRVL", "SMH", "SOXX"}


# ─────────────────────────── store ───────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id              INTEGER PRIMARY KEY,
    seed                INTEGER NOT NULL,
    start_date          TEXT NOT NULL,
    end_date            TEXT NOT NULL,
    start_value         REAL NOT NULL,
    final_value         REAL NOT NULL DEFAULT 0,
    total_return_pct    REAL NOT NULL DEFAULT 0,
    spy_return_pct      REAL NOT NULL DEFAULT 0,
    vs_spy_pct          REAL NOT NULL DEFAULT 0,
    n_trades            INTEGER NOT NULL DEFAULT 0,
    n_decisions         INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    completed_at        TEXT,
    equity_curve_json   TEXT NOT NULL DEFAULT '[]',
    notes               TEXT
);

CREATE TABLE IF NOT EXISTS backtest_trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL,
    sim_date    TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    action      TEXT NOT NULL,
    qty         REAL NOT NULL,
    price       REAL NOT NULL,
    value       REAL NOT NULL,
    reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_bt_trades_run ON backtest_trades(run_id);

CREATE TABLE IF NOT EXISTS backtest_decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL,
    sim_date        TEXT NOT NULL,
    action          TEXT,
    ticker          TEXT,
    qty             REAL,
    confidence      REAL,
    reasoning       TEXT,
    status          TEXT,
    detail          TEXT,
    cash            REAL,
    total_value     REAL,
    signal_count    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_bt_dec_run ON backtest_decisions(run_id);
"""


class BacktestStore:
    def __init__(self, path: Path = BACKTEST_DB):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._lock = threading.Lock()

    def upsert_run(self, run_id: int, seed: int, status: str) -> None:
        with self._lock:
            existing = self.conn.execute(
                "SELECT run_id FROM backtest_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            now = datetime.now(timezone.utc).isoformat()
            if existing:
                self.conn.execute(
                    "UPDATE backtest_runs SET status=? WHERE run_id=?", (status, run_id)
                )
            else:
                self.conn.execute(
                    "INSERT INTO backtest_runs (run_id, seed, start_date, end_date, "
                    "start_value, status, started_at) VALUES (?,?,?,?,?,?,?)",
                    (run_id, seed, START_DATE.isoformat(), END_DATE.isoformat(),
                     INITIAL_CASH, status, now),
                )
            self.conn.commit()

    def finalize_run(self, run_id: int, final_value: float, spy_return_pct: float,
                     n_trades: int, n_decisions: int, equity_curve: list,
                     status: str = "complete", notes: str = "") -> None:
        total_return_pct = (final_value - INITIAL_CASH) / INITIAL_CASH * 100
        vs_spy = total_return_pct - spy_return_pct
        with self._lock:
            self.conn.execute(
                "UPDATE backtest_runs SET final_value=?, total_return_pct=?, "
                "spy_return_pct=?, vs_spy_pct=?, n_trades=?, n_decisions=?, "
                "equity_curve_json=?, status=?, completed_at=?, notes=? WHERE run_id=?",
                (final_value, total_return_pct, spy_return_pct, vs_spy,
                 n_trades, n_decisions, json.dumps(equity_curve), status,
                 datetime.now(timezone.utc).isoformat(), notes, run_id),
            )
            self.conn.commit()

    def update_partial_progress(self, run_id: int, current_value: float,
                                n_trades: int, n_decisions: int,
                                equity_curve: list) -> None:
        """Push in-progress equity curve + counters so the dashboard can render
        partial state while a run is still executing."""
        pct = (current_value - INITIAL_CASH) / INITIAL_CASH * 100
        with self._lock:
            self.conn.execute(
                "UPDATE backtest_runs SET final_value=?, total_return_pct=?, "
                "n_trades=?, n_decisions=?, equity_curve_json=? WHERE run_id=?",
                (current_value, pct, n_trades, n_decisions,
                 json.dumps(equity_curve), run_id),
            )
            self.conn.commit()

    def record_trade(self, run_id: int, sim_date: str, ticker: str, action: str,
                     qty: float, price: float, reason: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO backtest_trades (run_id, sim_date, ticker, action, qty, "
                "price, value, reason) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, sim_date, ticker, action, qty, price, qty * price, reason),
            )
            self.conn.commit()

    def record_decision(self, run_id: int, sim_date: str, decision: dict | None,
                        status: str, detail: str, cash: float, total_value: float,
                        signal_count: int) -> None:
        d = decision or {}
        with self._lock:
            self.conn.execute(
                "INSERT INTO backtest_decisions (run_id, sim_date, action, ticker, qty, "
                "confidence, reasoning, status, detail, cash, total_value, signal_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, sim_date, d.get("action"), d.get("ticker"), d.get("qty"),
                 d.get("confidence"), d.get("reasoning"), status, detail, cash,
                 total_value, signal_count),
            )
            self.conn.commit()

    def all_runs(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM backtest_runs ORDER BY run_id ASC"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["equity_curve"] = json.loads(d.pop("equity_curve_json") or "[]")
            except Exception:
                d["equity_curve"] = []
            out.append(d)
        return out

    def run_detail(self, run_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM backtest_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["equity_curve"] = json.loads(d.pop("equity_curve_json") or "[]")
        except Exception:
            d["equity_curve"] = []
        trades = self.conn.execute(
            "SELECT * FROM backtest_trades WHERE run_id=? ORDER BY sim_date ASC, id ASC",
            (run_id,),
        ).fetchall()
        decisions = self.conn.execute(
            "SELECT * FROM backtest_decisions WHERE run_id=? ORDER BY sim_date ASC, id ASC",
            (run_id,),
        ).fetchall()
        d["trades"] = [dict(t) for t in trades]
        d["decisions"] = [dict(x) for x in decisions]
        return d


# ─────────────────────────── price cache ───────────────────────────

class PriceCache:
    """Loads OHLCV history for all watchlist tickers once. Lookups by date."""

    def __init__(self, tickers: list[str], start: date, end: date):
        self.tickers = tickers
        self.start = start
        self.end = end
        # ticker -> {iso_date: close}
        self.prices: dict[str, dict[str, float]] = {}
        self.trading_days: list[date] = []
        self._load()

    def _load(self) -> None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if PRICE_CACHE_PATH.exists():
            try:
                cached = json.loads(PRICE_CACHE_PATH.read_text())
                meta = cached.get("_meta", {})
                if (meta.get("start") == self.start.isoformat()
                        and meta.get("end") == self.end.isoformat()
                        and set(meta.get("tickers", [])) >= set(self.tickers)):
                    self.prices = {k: v for k, v in cached.items() if k != "_meta"}
                    self._build_trading_days()
                    print(f"[price_cache] loaded {len(self.prices)} tickers from cache "
                          f"({len(self.trading_days)} trading days)")
                    return
            except Exception as e:
                print(f"[price_cache] cache read failed: {e}")

        print(f"[price_cache] downloading {len(self.tickers)} tickers "
              f"{self.start} → {self.end} from yfinance…")
        # Pad end by +1 day because yfinance end is exclusive.
        end_pad = (self.end + timedelta(days=2)).isoformat()
        for t in self.tickers:
            try:
                hist = yf.Ticker(t).history(start=self.start.isoformat(),
                                            end=end_pad, auto_adjust=False)
                if hist.empty:
                    print(f"[price_cache]   {t}: no data")
                    self.prices[t] = {}
                    continue
                series: dict[str, float] = {}
                for ts, row in hist.iterrows():
                    iso = ts.date().isoformat()
                    close = row.get("Close")
                    if close is None or close != close:  # NaN check
                        continue
                    series[iso] = float(close)
                self.prices[t] = series
                print(f"[price_cache]   {t}: {len(series)} rows")
            except Exception as e:
                print(f"[price_cache]   {t} failed: {e}")
                self.prices[t] = {}

        payload = {"_meta": {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "tickers": list(self.prices.keys()),
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }}
        payload.update(self.prices)
        PRICE_CACHE_PATH.write_text(json.dumps(payload))
        self._build_trading_days()
        print(f"[price_cache] saved → {PRICE_CACHE_PATH} "
              f"({len(self.trading_days)} trading days)")

    def _build_trading_days(self) -> None:
        spy = self.prices.get("SPY") or {}
        if not spy:
            # fallback: any ticker
            for t in self.prices:
                if self.prices[t]:
                    spy = self.prices[t]
                    break
        days = sorted(date.fromisoformat(d) for d in spy.keys()
                      if self.start <= date.fromisoformat(d) <= self.end)
        self.trading_days = days

    def price_on(self, ticker: str, d: date) -> float | None:
        """Close on `d` if available; else most recent prior close."""
        series = self.prices.get(ticker)
        if not series:
            return None
        iso = d.isoformat()
        if iso in series:
            return series[iso]
        # walk back up to 7 days
        for delta in range(1, 8):
            prior = (d - timedelta(days=delta)).isoformat()
            if prior in series:
                return series[prior]
        return None

    def returns_pct(self, ticker: str, start_d: date, end_d: date) -> float:
        s = self.price_on(ticker, start_d)
        e = self.price_on(ticker, end_d)
        if not s or not e:
            return 0.0
        return (e - s) / s * 100


# ─────────────────────────── technical indicators ───────────────────────────

# Volume series cache: ticker -> {iso_date: volume}. Filled lazily on first
# call to _get_quant_signals so existing close-only price cache stays compatible.
_VOLUME_CACHE: dict[str, dict[str, float]] = {}
_VOLUME_CACHE_PATH = CACHE_DIR / "volumes.json"
_VOLUME_CACHE_LOCK = threading.Lock()
_VOLUME_CACHE_LOADED = False


def _load_volume_cache_from_disk() -> None:
    global _VOLUME_CACHE, _VOLUME_CACHE_LOADED
    with _VOLUME_CACHE_LOCK:
        if _VOLUME_CACHE_LOADED:
            return
        if _VOLUME_CACHE_PATH.exists():
            try:
                _VOLUME_CACHE = json.loads(_VOLUME_CACHE_PATH.read_text())
            except Exception:
                _VOLUME_CACHE = {}
        _VOLUME_CACHE_LOADED = True


def _persist_volume_cache() -> None:
    try:
        _VOLUME_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _VOLUME_CACHE_PATH.write_text(json.dumps(_VOLUME_CACHE))
    except Exception as e:
        print(f"[volume_cache] persist failed: {e}")


def _ensure_volume_for(ticker: str, start: date, end: date) -> dict[str, float]:
    """Lazily fetch a volume series for `ticker` covering [start, end]. Cached on disk."""
    _load_volume_cache_from_disk()
    existing = _VOLUME_CACHE.get(ticker)
    if existing:
        return existing
    try:
        end_pad = (end + timedelta(days=2)).isoformat()
        hist = yf.Ticker(ticker).history(start=start.isoformat(),
                                         end=end_pad, auto_adjust=False)
        series: dict[str, float] = {}
        if hist is not None and not hist.empty:
            for ts, row in hist.iterrows():
                vol = row.get("Volume")
                if vol is None or vol != vol:
                    continue
                series[ts.date().isoformat()] = float(vol)
        with _VOLUME_CACHE_LOCK:
            _VOLUME_CACHE[ticker] = series
            _persist_volume_cache()
        return series
    except Exception as e:
        print(f"[volume_cache] {ticker} fetch failed: {e}")
        with _VOLUME_CACHE_LOCK:
            _VOLUME_CACHE[ticker] = {}
        return {}


def _series_up_to(prices: "PriceCache", ticker: str, sim_date: date,
                  max_points: int = 260) -> list[tuple[date, float]]:
    """Return (date, close) tuples for `ticker` <= sim_date, oldest first, capped at max_points."""
    series = prices.prices.get(ticker) or {}
    if not series:
        return []
    iso = sim_date.isoformat()
    pairs = [(date.fromisoformat(d), v) for d, v in series.items() if d <= iso]
    pairs.sort(key=lambda x: x[0])
    return pairs[-max_points:]


def _ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    out: list[float] = []
    seed = sum(values[:period]) / period
    out.append(seed)
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    gains, losses = 0.0, 0.0
    # initial averages over first `period` deltas
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_g = gains / period
    avg_l = losses / period
    # Wilder smoothing for the rest
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        g = diff if diff > 0 else 0.0
        l = -diff if diff < 0 else 0.0
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def _macd(closes: list[float]) -> tuple[str, float, float] | None:
    """Return (label, macd, signal). label is 'bullish'/'bearish'/'flat'."""
    if len(closes) < 35:
        return None
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    if not ema12 or not ema26:
        return None
    # align: ema26 starts 14 points later than ema12 (offset of 26-12=14)
    offset = len(ema12) - len(ema26)
    macd_line = [ema12[i + offset] - ema26[i] for i in range(len(ema26))]
    if len(macd_line) < 9:
        return None
    signal_line = _ema(macd_line, 9)
    if not signal_line:
        return None
    m = macd_line[-1]
    s = signal_line[-1]
    label = "bullish" if m > s else "bearish" if m < s else "flat"
    return (label, m, s)


def _compute_technical_indicators(ticker: str, sim_date: date,
                                  prices: "PriceCache") -> dict | None:
    """RSI/MACD/MA crossover/volume ratio/52w proximity computed from cached closes.

    Returns None if there isn't enough history for the ticker at sim_date."""
    pairs = _series_up_to(prices, ticker, sim_date, max_points=260)
    if len(pairs) < 60:
        return None
    closes = [p[1] for p in pairs]
    last = closes[-1]

    rsi = _rsi(closes, 14)
    macd_res = _macd(closes)
    macd_label = macd_res[0] if macd_res else None

    ma_cross = None
    if len(closes) >= 200:
        ma50 = sum(closes[-50:]) / 50
        ma200 = sum(closes[-200:]) / 200
        ma_cross = "golden" if ma50 > ma200 else "death"
    elif len(closes) >= 50:
        ma50 = sum(closes[-50:]) / 50
        ma_cross = "above50" if last > ma50 else "below50"

    hi_52 = max(closes[-252:]) if len(closes) >= 252 else max(closes)
    lo_52 = min(closes[-252:]) if len(closes) >= 252 else min(closes)
    pct_from_52h = (last - hi_52) / hi_52 * 100 if hi_52 else 0.0
    pct_from_52l = (last - lo_52) / lo_52 * 100 if lo_52 else 0.0

    vol_ratio: float | None = None
    try:
        # volumes cover [start, end] for the ticker
        vols = _ensure_volume_for(ticker, START_DATE - timedelta(days=400), END_DATE)
        if vols:
            iso = sim_date.isoformat()
            # find sim_date volume + last 20 trading-day window
            vdates = sorted(d for d in vols.keys() if d <= iso)
            if len(vdates) >= 21:
                today_v = vols[vdates[-1]]
                prior20 = [vols[d] for d in vdates[-21:-1]]
                avg20 = sum(prior20) / len(prior20)
                if avg20 > 0:
                    vol_ratio = today_v / avg20
    except Exception:
        vol_ratio = None

    return {
        "RSI": round(rsi, 1) if rsi is not None else None,
        "MACD": macd_label,
        "MA_cross": ma_cross,
        "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
        "pct_from_52h": round(pct_from_52h, 1),
        "pct_from_52l": round(pct_from_52l, 1),
    }


def _get_quant_signals(sim_date: date, tickers: list[str],
                       prices: "PriceCache") -> dict[str, dict]:
    """Compute technical indicators for each ticker at sim_date.

    Returns a dict {ticker: {RSI, MACD, MA_cross, vol_ratio, pct_from_52h, pct_from_52l}}.
    Tickers with insufficient history are omitted."""
    out: dict[str, dict] = {}
    for t in tickers:
        try:
            ind = _compute_technical_indicators(t, sim_date, prices)
            if ind is not None:
                out[t] = ind
        except Exception as e:
            print(f"[quant] {t} indicator compute failed: {e}")
    return out


def _market_regime(sim_date: date, prices: "PriceCache") -> str:
    """Bull/bear/sideways via SPY 50/200 MA + slope."""
    pairs = _series_up_to(prices, "SPY", sim_date, max_points=260)
    if len(pairs) < 200:
        return "unknown"
    closes = [p[1] for p in pairs]
    last = closes[-1]
    ma50 = sum(closes[-50:]) / 50
    ma200 = sum(closes[-200:]) / 200
    if last > ma50 > ma200:
        return "bull"
    if last < ma50 < ma200:
        return "bear"
    return "sideways"


def _sector_rotation(sim_date: date, prices: "PriceCache",
                     lookback_days: int = 21) -> list[tuple[str, float]]:
    """Trailing ~1 month total return for sector ETFs, sorted descending."""
    sectors = ["XLK", "XLE", "XLF", "XLV", "XLI"]
    results: list[tuple[str, float]] = []
    for s in sectors:
        pairs = _series_up_to(prices, s, sim_date, max_points=lookback_days + 5)
        if len(pairs) < 2:
            continue
        start = pairs[0][1]
        end = pairs[-1][1]
        if start <= 0:
            continue
        results.append((s, (end - start) / start * 100))
    results.sort(key=lambda x: x[1], reverse=True)
    return results


def _vix_level(sim_date: date, prices: "PriceCache") -> float | None:
    return prices.price_on("^VIX", sim_date)


# ─────────────────────────── GDELT fetcher ───────────────────────────

class GDELTFetcher:
    """Cached GDELT fetcher using the gdeltdoc library (alex9smith/gdelt-doc-api).

    Thread-safe: a class-level lock serializes outbound GDELT requests so 10
    parallel run threads don't all hit the 5s rate limit simultaneously."""

    def __init__(self):
        GDELT_CACHE.mkdir(parents=True, exist_ok=True)
        from gdeltdoc import GdeltDoc
        self._client = GdeltDoc()
        self._request_lock = threading.Lock()
        self._last_request_ts = 0.0

    def _cache_key(self, d: date, keywords: str) -> Path:
        slug = hashlib.md5(keywords.encode()).hexdigest()[:8]
        return GDELT_CACHE / f"{d.isoformat()}_{slug}.json"

    def fetch(self, d: date, keywords: str) -> list[dict]:
        path = self._cache_key(d, keywords)
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                pass

        from gdeltdoc import Filters
        from gdeltdoc.errors import RateLimitError
        start_str = d.strftime("%Y-%m-%d")
        end_str = (d + timedelta(days=1)).strftime("%Y-%m-%d")

        articles: list[dict] = []
        success = False
        for attempt in range(3):
            err: str | None = None
            with self._request_lock:
                elapsed = time.time() - self._last_request_ts
                if elapsed < GDELT_RATE_LIMIT_S:
                    time.sleep(GDELT_RATE_LIMIT_S - elapsed)
                try:
                    f = Filters(keyword=keywords, start_date=start_str, end_date=end_str)
                    df = self._client.article_search(f)
                    self._last_request_ts = time.time()
                    if df is not None and not df.empty:
                        keep = [c for c in ["title", "url", "domain", "seendate"]
                                if c in df.columns]
                        articles = df[keep].rename(columns={"domain": "source"}).to_dict("records")
                    success = True
                except RateLimitError:
                    self._last_request_ts = time.time()
                    err = "rate-limited"
                except Exception as e:
                    self._last_request_ts = time.time()
                    err = f"{type(e).__name__}: {e}"
            if success:
                break
            backoff = GDELT_RETRY_BACKOFF_S * (attempt + 1)
            print(f"[gdelt] {err} {d} {keywords[:30]!r} "
                  f"attempt {attempt+1}/3 — sleeping {backoff:.0f}s")
            time.sleep(backoff)

        if success:
            try:
                path.write_text(json.dumps(articles))
            except Exception:
                pass
        return articles


# ─────────────────────────── Alpha Vantage news fetcher ───────────────────────────

AV_CACHE_DIR = CACHE_DIR / "alphavantage"
AV_QUOTA_PATH = CACHE_DIR / "av_quota.json"
AV_MAX_DAILY = 22  # stay under 25/day limit with margin


class AlphaVantageNewsFetcher:
    """Disk-cached Alpha Vantage NEWS_SENTIMENT fetcher.

    Extremely conservative: max 22 calls/day tracked across restarts, skip
    when quota is exhausted. All results persisted to disk so backtest reruns
    are free. Gracefully disabled when ALPHA_VANTAGE_KEY is unset.
    """
    _lock = threading.Lock()

    def __init__(self):
        AV_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._key = os.environ.get("ALPHA_VANTAGE_KEY", "").strip()

    def _quota(self) -> dict:
        try:
            if AV_QUOTA_PATH.exists():
                q = json.loads(AV_QUOTA_PATH.read_text())
                if q.get("date") == date.today().isoformat():
                    return q
        except Exception:
            pass
        return {"date": date.today().isoformat(), "calls": 0}

    def _inc_quota(self):
        with self._lock:
            q = self._quota()
            q["calls"] += 1
            AV_QUOTA_PATH.write_text(json.dumps(q))

    def _cache_path(self, ticker: str, d: date) -> Path:
        return AV_CACHE_DIR / f"{d.isoformat()}_{ticker}.json"

    def fetch(self, tickers: list[str], d: date) -> list[dict]:
        if not self._key:
            return []
        articles: list[dict] = []
        for tk in tickers:
            path = self._cache_path(tk, d)
            if path.exists():
                try:
                    articles.extend(json.loads(path.read_text()))
                    continue
                except Exception:
                    pass
            with self._lock:
                q = self._quota()
                if q["calls"] >= AV_MAX_DAILY:
                    continue
            try:
                resp = requests.get(
                    "https://www.alphavantage.co/query",
                    params={"function": "NEWS_SENTIMENT", "tickers": tk,
                            "limit": 50, "apikey": self._key},
                    timeout=12,
                )
                data = resp.json()
                feed = data.get("feed", [])
                items = [{"title": a.get("title", ""), "url": a.get("url", ""),
                          "source": a.get("source", "")}
                         for a in feed if a.get("title")]
                path.write_text(json.dumps(items))
                articles.extend(items)
                self._inc_quota()
                time.sleep(1.2)  # AV rate-limit buffer
            except Exception as e:
                print(f"[av_news] {tk} {d}: {e}")
        return articles


# ─────────────────────────── heuristic scorer ───────────────────────────

_TICKER_RE = re.compile(r"\b([A-Z]{2,5})\b")
_NOT_TICKERS = {
    "AI", "AND", "FOR", "THE", "WITH", "FROM", "AFTER", "INTO", "HAVE", "WILL",
    "MAY", "JUNE", "JULY", "AUG", "SEPT", "OCT", "NOV", "DEC", "CEO", "ETF",
    "USA", "USD", "GDP", "CPI", "OPEC", "FED", "FOMC", "PMI", "ISM", "WHO",
    "NEW", "OLD", "ALL", "YES", "USA", "ITS", "OUR", "ONE", "TWO", "AND",
}


def _extract_tickers(text: str) -> set[str]:
    out = set()
    for m in re.finditer(r"\$([A-Z]{1,5})\b", text or ""):
        out.add(m.group(1))
    for m in _TICKER_RE.finditer(text or ""):
        tok = m.group(1)
        if tok in _NOT_TICKERS:
            continue
        out.add(tok)
    return out


def score_article(article: dict) -> tuple[float, list[str]]:
    """Return (score 0..5, tickers). Pure keyword heuristic."""
    title = (article.get("title") or "")
    body = title.lower()
    pos = sum(1 for p in BUY_PHRASES if p in body)
    neg = sum(1 for p in SELL_PHRASES if p in body)
    tickers = _extract_tickers(title)
    semis_boost = 0.5 if tickers & SEMIS_TICKERS else 0.0
    score = 2.5 + pos * 0.5 - neg * 0.5 + semis_boost
    return max(0.0, min(5.0, score)), sorted(tickers)


# ─────────────────────────── portfolio sim ───────────────────────────

@dataclass
class SimPortfolio:
    cash: float = INITIAL_CASH
    # ticker -> {qty, avg_cost, stop_loss, take_profit, peak_pct}
    positions: dict[str, dict] = field(default_factory=dict)

    def total_value(self, prices: PriceCache, d: date) -> float:
        v = self.cash
        for ticker, p in self.positions.items():
            px = prices.price_on(ticker, d) or p["avg_cost"]
            v += px * p["qty"]
        return v

    def open_value(self, prices: PriceCache, d: date) -> float:
        v = 0.0
        for ticker, p in self.positions.items():
            px = prices.price_on(ticker, d) or p["avg_cost"]
            v += px * p["qty"]
        return v


def _buy(portfolio: SimPortfolio, ticker: str, qty: float, price: float,
         stop_loss: float | None, take_profit: float | None) -> None:
    notional = qty * price
    portfolio.cash -= notional
    existing = portfolio.positions.get(ticker)
    if existing:
        new_qty = existing["qty"] + qty
        blended = (existing["qty"] * existing["avg_cost"] + qty * price) / new_qty
        existing["qty"] = new_qty
        existing["avg_cost"] = blended
        if stop_loss:
            existing["stop_loss"] = stop_loss
        if take_profit:
            existing["take_profit"] = take_profit
    else:
        portfolio.positions[ticker] = {
            "qty": qty, "avg_cost": price,
            "stop_loss": stop_loss, "take_profit": take_profit,
        }


def _sell(portfolio: SimPortfolio, ticker: str, qty: float, price: float) -> float:
    pos = portfolio.positions.get(ticker)
    if not pos:
        return 0.0
    qty = min(qty, pos["qty"])
    proceeds = qty * price
    portfolio.cash += proceeds
    pos["qty"] -= qty
    if pos["qty"] <= 1e-6:
        del portfolio.positions[ticker]
    return proceeds


def _enforce_risk_exits(portfolio: SimPortfolio, prices: PriceCache,
                        from_day: date, to_day: date, run_id: int,
                        store: BacktestStore) -> int:
    """Honor only explicit stop_loss / take_profit from Opus. No default risk exits."""
    n = 0
    if not portfolio.positions:
        return 0
    cur = from_day + timedelta(days=1)
    while cur <= to_day:
        if not portfolio.positions:
            break
        if cur not in prices.trading_days:
            cur += timedelta(days=1)
            continue
        for ticker in list(portfolio.positions.keys()):
            pos = portfolio.positions[ticker]
            px = prices.price_on(ticker, cur)
            if px is None:
                continue
            sl = pos.get("stop_loss")
            tp = pos.get("take_profit")
            if sl and px <= sl:
                qty = pos["qty"]
                _sell(portfolio, ticker, qty, px)
                store.record_trade(run_id, cur.isoformat(), ticker, "SELL", qty, px,
                                   f"stop-loss @ {sl} (close {px:.2f})")
                n += 1
            elif tp and px >= tp:
                qty = pos["qty"]
                _sell(portfolio, ticker, qty, px)
                store.record_trade(run_id, cur.isoformat(), ticker, "SELL", qty, px,
                                   f"take-profit @ {tp} (close {px:.2f})")
                n += 1
        cur += timedelta(days=1)
    return n


# ─────────────────────────── Opus call ───────────────────────────

SYSTEM_PROMPT = """You are managing a paper trading portfolio with $1000 starting capital.
This is a HISTORICAL backtest — you are deciding trades for a specific past date based on news available at that date.
Your ONLY goal is maximum profit over a 1-year horizon. You have complete freedom over position
sizing, risk, and timing. There are NO enforced limits. You can:
- Put 100% of portfolio into one trade if you have high conviction
- Go all-in on a single ticker
- Let losers run if you expect reversal

THINK LIKE A HEDGE FUND MANAGER WHO WANTS ASYMMETRIC RETURNS.
Small, safe trades will not outperform. Take calculated risks.
High conviction = large size. Low conviction = stay cash.

Trade US stocks ONLY (no options or futures in this backtest).

LEVERAGE INSTRUMENTS AVAILABLE:
- Leveraged ETFs 3x Bull: TQQQ (QQQ), UPRO/SPXL (SPY), UDOW (Dow), URTY (Russell), SOXL (semis), TECL (tech), FNGU (tech FANGs), CURE (healthcare), LABU (biotech), NAIL (homebuilders), DPST (banks), FAS (financials), DFEN (defense), TNA (small-cap), UTSL (utilities)
- Leveraged ETFs 2x Bull: QLD (QQQ 2x), SSO (SPY 2x), NVDU (NVDA), MSFU (MSFT), AMZU (AMZN), GOOGU (GOOG), METAU (META), TSLL (TSLA), CONL (COIN), LNOK (Nokia), BITU (BTC), ETHU (ETH)
- Leveraged ETFs Bear/Hedge: SQQQ/SPXS (3x short index), SOXS (3x short semis), TECS (3x short tech), FNGD (3x short FANGs)
- Crypto leveraged: BITX (2x BTC), BITU (2x BTC), ETHU (2x ETH)
- For high-conviction directional trades, consider 2-3x leveraged ETFs instead of the underlying
- For options-equivalent exposure: buy deep ITM LEAPS calls (delta >0.80) to simulate leveraged long
- Risk: leveraged ETFs decay in sideways markets; best for strong trending moves only

POSITION SIZING GUIDANCE (committee should consider):
- High conviction (RSI+MACD+MA all aligned): up to 40% portfolio
- Medium conviction (2/3 signals aligned): 15-25%
- Low conviction / leveraged ETF: max 10%
- Never go 100% into one leveraged ETF (decay risk)

Respond with a SINGLE JSON object — no prose, no markdown fences. Schema:

{
  "action": "BUY" | "SELL" | "HOLD",
  "ticker": "NVDA",
  "qty": 0.5,
  "confidence": 0.85,
  "reasoning": "1-3 sentences why",
  "stop_loss": 850.0,       // optional — only honored if set
  "take_profit": 950.0      // optional — only honored if set
}

- For SELL, ticker must match an open position.
- Fractional shares are allowed (qty can be e.g. 0.5).
- If you set stop_loss / take_profit, they will fire on daily closes.

Return JSON ONLY.
"""


def _claude_call(prompt: str, retries: int = 1) -> str | None:
    if not shutil.which("claude"):
        print("[backtest] claude CLI not found")
        return None
    for attempt in range(retries + 1):
        try:
            r = subprocess.run(
                ["claude", "--model", MODEL, "--print",
                 "--permission-mode", "bypassPermissions"],
                input=prompt, capture_output=True, text=True,
                timeout=OPUS_TIMEOUT_S,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
            print(f"[backtest] claude attempt {attempt+1} returncode={r.returncode} "
                  f"err={r.stderr.strip()[:200]!r}")
        except subprocess.TimeoutExpired:
            print(f"[backtest] claude timeout attempt {attempt+1}")
        except Exception as e:
            print(f"[backtest] claude exception attempt {attempt+1}: {e}")
        if attempt < retries:
            time.sleep(2)
    return None


def _parse_decision(raw: str | None) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception as e:
        print(f"[backtest] JSON parse failed: {e} raw={text[:200]!r}")
        return None


COMMITTEE_BRIEF = """You are a trading committee of 10 traders managing a single $1000 paper trading portfolio.
Each committee member has a distinct style. For this trading day, each member silently proposes
a trade. The committee then VOTES and executes the single highest-conviction consensus trade.

THE 10 COMMITTEE MEMBERS:
1. VALUE      — P/E, fundamentals, undervalued cash-flow machines, durable moats
2. MOMENTUM   — Buys what is going up; earnings beats + raised guidance; ride trends
3. CONTRARIAN — Buys fear, sells greed; oversold quality; mean reversion
4. MACRO      — Rates, FX, commodities, geopolitics; TLT/GLD/USO/UNG as macro vehicles
5. GARP       — Growth at reasonable price; quality compounders with sane multiples
6. QUANT      — Pure signal/catalyst reaction; news-driven, unemotional
7. ROTATOR    — Sector rotation by macro cycle; XLE/XLK/XLF/SMH/ARKK
8. SMALLCAP   — Gems outside mega-caps; LITE, MU, RIVN, NIO, COIN, PLTR, MSTR, SHOP, SQ
9. ESG/THEME  — AI infra, clean energy, semis, GLP-1, EVs; picks-and-shovels
10. SPECULATOR — Concentrated asymmetric bets; full-size when setup is right

PROCESS:
  (a) Each member proposes one trade (BUY/SELL/HOLD).
  (b) Members vote — weighted by conviction and by how well the proposal fits today's signals.
  (c) Output the SINGLE consensus trade as JSON.

The reasoning field MUST briefly list each member's proposal then state the consensus, e.g.:
"VALUE: BUY HSBC. MOMENTUM: BUY NVDA. CONTRARIAN: HOLD. MACRO: BUY TLT. GARP: BUY LLY.
QUANT: BUY NVDA. ROTATOR: BUY SMH. SMALLCAP: BUY LITE. ESG: BUY NVDA. SPECULATOR: BUY MSTR.
Consensus: BUY NVDA (4 votes + highest conviction on AI compute catalyst)."
"""


def _build_prompt(run_id: int, seed: int, sim_date: date, portfolio: SimPortfolio,
                  top_articles: list[dict], prices: PriceCache) -> str:
    pos_lines = []
    for ticker, p in portfolio.positions.items():
        px = prices.price_on(ticker, sim_date) or p["avg_cost"]
        pl_pct = (px - p["avg_cost"]) / p["avg_cost"] * 100
        pos_lines.append(f"  {ticker}: qty={p['qty']} avg=${p['avg_cost']:.2f} "
                         f"now=${px:.2f} P/L={pl_pct:+.1f}%")

    art_lines = []
    for a in top_articles:
        tickers = a.get("tickers", [])
        t_str = f" tickers={','.join(tickers[:5])}" if tickers else ""
        art_lines.append(f"  [{a['score']:.1f}] {a['title'][:140]}{t_str}")

    px_lines = []
    for t in WATCHLIST:
        if t.startswith("^"):
            continue  # index gauges shown elsewhere
        p = prices.price_on(t, sim_date)
        px_lines.append(f"  {t}: ${p:.2f}" if p else f"  {t}: N/A")

    # Technical signals for held positions + top watchlist names.
    quant_tickers = sorted(set(QUANT_SIGNAL_TICKERS) | set(portfolio.positions.keys()))
    quant_sigs = _get_quant_signals(sim_date, quant_tickers, prices)
    quant_lines = []
    for tk in sorted(quant_sigs.keys()):
        q = quant_sigs[tk]
        quant_lines.append(
            f"  {tk}: RSI={q.get('RSI')}  MACD={q.get('MACD')}  "
            f"MA={q.get('MA_cross')}  vol_ratio={q.get('vol_ratio')}  "
            f"52h={q.get('pct_from_52h')}%  52l={q.get('pct_from_52l')}%"
        )

    vix = _vix_level(sim_date, prices)
    regime = _market_regime(sim_date, prices)
    rotation = _sector_rotation(sim_date, prices)
    rot_str = ", ".join(f"{t} {p:+.1f}%" for t, p in rotation) if rotation else "n/a"
    vix_str = f"{vix:.2f}" if vix is not None else "N/A"

    total = portfolio.total_value(prices, sim_date)

    return f"""{SYSTEM_PROMPT}

---
{COMMITTEE_BRIEF}

---
SIMULATION CONTEXT:
You are committee instance #{run_id} with random seed {seed}. Other parallel committees
see different slices of news and may reach different consensus trades — that's expected.

SIMULATED DATE: {sim_date.isoformat()}

PORTFOLIO:
  cash: ${portfolio.cash:.2f}
  total value: ${total:.2f}
  positions:
{chr(10).join(pos_lines) if pos_lines else '  (none)'}

WATCHLIST CLOSES on {sim_date.isoformat()}:
{chr(10).join(px_lines)}

MARKET STRUCTURE on {sim_date.isoformat()}:
  VIX: {vix_str}
  Market regime: {regime} (SPY vs 50/200 MA)
  Sector rotation (~21d return): {rot_str}

TECHNICAL SIGNALS (positions + top watchlist):
{chr(10).join(quant_lines) if quant_lines else '  (no quant signals — insufficient history)'}

TOP NEWS SIGNALS for {sim_date.isoformat()} (score 0..5 from keyword heuristic):
{chr(10).join(art_lines) if art_lines else '  (no signals)'}

Return JSON only — a SINGLE consensus decision, no per-member objects.
"""


# ─────────────────────────── engine ───────────────────────────

@dataclass
class BacktestRun:
    run_id: int
    seed: int
    start_date: str = START_DATE.isoformat()
    end_date: str = END_DATE.isoformat()
    start_value: float = INITIAL_CASH
    final_value: float = 0.0
    total_return_pct: float = 0.0
    spy_return_pct: float = 0.0
    vs_spy_pct: float = 0.0
    n_trades: int = 0
    n_decisions: int = 0
    status: str = "pending"
    trades: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)


class BacktestEngine:
    def __init__(self):
        self.store = BacktestStore()
        self.prices = PriceCache(WATCHLIST, START_DATE, END_DATE)
        self.gdelt = GDELTFetcher()
        self.av_news = AlphaVantageNewsFetcher()
        if not self.prices.trading_days:
            raise RuntimeError("PriceCache has no trading days — yfinance fetch failed")

    def _sampled_days(self) -> list[date]:
        days = self.prices.trading_days
        return days[::SAMPLE_EVERY_N_DAYS]

    def _fetch_signals(self, d: date, seed: int, rng: random.Random) -> list[dict]:
        # rotate 2 keyword groups based on seed/day so different runs see different slices
        # After _warm_gdelt_cache(), these should all be disk-cache hits (no outbound calls).
        idxs = rng.sample(range(len(KEYWORD_GROUPS)), 2)
        articles: list[dict] = []
        seen_urls = set()
        for i in idxs:
            kw = KEYWORD_GROUPS[i]
            for a in self.gdelt.fetch(d, kw):
                url = a.get("url", "")
                if url in seen_urls or not a.get("title"):
                    continue
                seen_urls.add(url)
                score, tickers = score_article(a)
                articles.append({
                    "title": a["title"],
                    "url": url,
                    "score": score,
                    "tickers": tickers,
                })
        # Alpha Vantage NEWS_SENTIMENT — disk-cached, quota-guarded (22 req/day max).
        # Fetches for the 3 most relevant tickers based on current portfolio + top watchlist.
        av_tickers = list(portfolio.positions.keys())[:2] + ["NVDA", "SPY"]
        for a in self.av_news.fetch(list(dict.fromkeys(av_tickers))[:4], d):
            url = a.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                score, tickers = score_article(a)
                articles.append({"title": a["title"], "url": url,
                                 "score": score, "tickers": tickers})

        # Supplement with yfinance ticker news (no rate limits, no API key needed).
        for a in self._fetch_yf_news(list(QUANT_SIGNAL_TICKERS), d):
            if a["url"] not in seen_urls:
                seen_urls.add(a["url"])
                articles.append(a)

        # sort by score, take top 10 then sample 5 with rng
        articles.sort(key=lambda x: x["score"], reverse=True)
        top10 = articles[:10]
        if len(top10) <= 5:
            return top10
        return sorted(rng.sample(top10, 5), key=lambda x: x["score"], reverse=True)

    def _execute_decision(self, run_id: int, sim_date: date, decision: dict,
                          portfolio: SimPortfolio) -> tuple[str, str]:
        action = (decision.get("action") or "HOLD").upper()
        if action == "HOLD":
            return "HOLD", decision.get("reasoning", "")
        ticker = (decision.get("ticker") or "").upper()
        try:
            qty = float(decision.get("qty") or 0)
        except (TypeError, ValueError):
            return "BLOCKED", "bad qty"
        if not ticker:
            return "BLOCKED", "no ticker"
        if qty <= 0:
            return "BLOCKED", "qty must be > 0"

        price = self.prices.price_on(ticker, sim_date)
        if price is None or price <= 0:
            return "BLOCKED", f"no price for {ticker} on {sim_date}"

        if action == "BUY":
            notional = qty * price
            if portfolio.cash - notional < 0:
                return "BLOCKED", f"insufficient cash (have ${portfolio.cash:.2f}, need ${notional:.2f})"
            sl = decision.get("stop_loss")
            tp = decision.get("take_profit")
            _buy(portfolio, ticker, qty, price,
                 float(sl) if isinstance(sl, (int, float)) else None,
                 float(tp) if isinstance(tp, (int, float)) else None)
            self.store.record_trade(run_id, sim_date.isoformat(), ticker, "BUY", qty,
                                    price, decision.get("reasoning", "")[:200])
            return "FILLED", f"BUY {qty} {ticker} @ {price:.2f}"

        if action == "SELL":
            pos = portfolio.positions.get(ticker)
            if not pos:
                return "BLOCKED", f"no open position in {ticker}"
            sell_qty = min(qty, pos["qty"])
            _sell(portfolio, ticker, sell_qty, price)
            self.store.record_trade(run_id, sim_date.isoformat(), ticker, "SELL",
                                    sell_qty, price,
                                    decision.get("reasoning", "")[:200])
            return "FILLED", f"SELL {sell_qty} {ticker} @ {price:.2f}"

        return "BLOCKED", f"unsupported action {action}"

    def run_one(self, run_id: int, seed: int | None = None) -> BacktestRun:
        if seed is None:
            seed = int.from_bytes(os.urandom(4), "big") ^ (run_id * 1337)
        rng = random.Random(seed)
        self.store.upsert_run(run_id, seed, "running")
        print(f"\n══════ RUN {run_id}/10  seed={seed} ══════")

        portfolio = SimPortfolio()
        equity_curve: list[dict] = []
        n_trades = 0
        n_decisions = 0
        prev_sample = self.prices.trading_days[0] - timedelta(days=1)
        last_curve_day: date | None = None

        sampled = self._sampled_days()
        if sampled and sampled[-1] != self.prices.trading_days[-1]:
            sampled.append(self.prices.trading_days[-1])
        print(f"[run {run_id}] {len(sampled)} sample days")

        for idx, sim_date in enumerate(sampled):
            # daily SL/TP scan since previous sample
            exits = _enforce_risk_exits(portfolio, self.prices, prev_sample,
                                        sim_date, run_id, self.store)
            n_trades += exits
            prev_sample = sim_date

            # fetch & score
            signals = self._fetch_signals(sim_date, seed, rng)

            # build prompt + call Opus
            prompt = _build_prompt(run_id, seed, sim_date, portfolio, signals, self.prices)
            raw = _claude_call(prompt)
            decision = _parse_decision(raw)
            n_decisions += 1

            if decision:
                status, detail = self._execute_decision(run_id, sim_date, decision, portfolio)
                if status == "FILLED":
                    n_trades += 1
            else:
                status, detail = "NO_DECISION", "claude returned no parseable JSON"

            total = portfolio.total_value(self.prices, sim_date)
            self.store.record_decision(run_id, sim_date.isoformat(), decision,
                                       status, detail, portfolio.cash, total,
                                       len(signals))

            # Per-sample equity snapshot + every-5-samples DB persist so the
            # dashboard can render partial equity curves while the run executes.
            equity_curve.append({
                "date": sim_date.isoformat(),
                "value": round(total, 2),
                "cash": round(portfolio.cash, 2),
            })
            last_curve_day = sim_date
            if idx % 5 == 0 or idx == len(sampled) - 1:
                try:
                    self.store.update_partial_progress(
                        run_id, total, n_trades, n_decisions, equity_curve,
                    )
                except Exception as pe:
                    print(f"[run {run_id}] partial persist failed: {pe}")

            if idx % 10 == 0 or idx == len(sampled) - 1:
                print(f"  [run {run_id} {idx+1}/{len(sampled)}] {sim_date} "
                      f"action={status} cash=${portfolio.cash:.2f} total=${total:.2f}")

        # final mark
        final_day = self.prices.trading_days[-1]
        # one more SL/TP sweep after last sample
        _enforce_risk_exits(portfolio, self.prices, prev_sample, final_day,
                            run_id, self.store)
        final_value = portfolio.total_value(self.prices, final_day)
        if not equity_curve or equity_curve[-1]["date"] != final_day.isoformat():
            equity_curve.append({
                "date": final_day.isoformat(),
                "value": round(final_value, 2),
                "cash": round(portfolio.cash, 2),
            })

        spy_return = self.prices.returns_pct("SPY", self.prices.trading_days[0], final_day)
        self.store.finalize_run(run_id, final_value, spy_return, n_trades,
                                n_decisions, equity_curve, status="complete")

        ret_pct = (final_value - INITIAL_CASH) / INITIAL_CASH * 100
        print(f"[run {run_id}] DONE  final=${final_value:.2f}  return={ret_pct:+.2f}%  "
              f"vs SPY {spy_return:+.2f}%  trades={n_trades}")

        return BacktestRun(
            run_id=run_id, seed=seed,
            final_value=round(final_value, 2),
            total_return_pct=round(ret_pct, 2),
            spy_return_pct=round(spy_return, 2),
            vs_spy_pct=round(ret_pct - spy_return, 2),
            n_trades=n_trades, n_decisions=n_decisions,
            equity_curve=equity_curve, status="complete",
        )

    def _warm_gdelt_cache(self) -> None:
        """Pre-fetch all date×keyword combos into disk cache before parallel runs start.

        Uses GDELT_WARM_WORKERS parallel workers so we fill the cache fast without
        hammering GDELT (each worker obeys the rate-limit via the shared lock).
        When run_all() calls this first, every subsequent thread cache-lookup is a
        disk hit — zero outbound GDELT requests during the parallel phase.
        """
        days = self._sampled_days()
        combos = [(d, kw) for d in days for kw in KEYWORD_GROUPS]
        uncached = [(d, kw) for d, kw in combos
                    if not self.gdelt._cache_key(d, kw).exists()]
        if not uncached:
            print(f"[cache_warm] all {len(combos)} combos already cached — skipping")
            return
        print(f"[cache_warm] warming {len(uncached)}/{len(combos)} date×keyword combos "
              f"with {GDELT_WARM_WORKERS} workers …")
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import random as _rnd

        def _warm_one(args):
            d, kw = args
            try:
                time.sleep(_rnd.uniform(0, 1.0))  # jitter to stagger workers
                self.gdelt.fetch(d, kw)
            except Exception:
                pass

        done = 0
        with ThreadPoolExecutor(max_workers=GDELT_WARM_WORKERS) as pool:
            futs = {pool.submit(_warm_one, c): c for c in uncached}
            for fut in as_completed(futs):
                done += 1
                if done % 20 == 0:
                    print(f"[cache_warm] {done}/{len(uncached)} warmed")
        print(f"[cache_warm] done — {len(uncached)} new entries cached")

    def _fetch_yf_news(self, tickers: list[str], sim_date: date) -> list[dict]:
        """Supplement GDELT with yfinance ticker news. No rate limits, no API key.

        Returns recent headlines scored by the same keyword heuristic. Only useful
        for dates close to today (yfinance only keeps ~30 news items per ticker).
        """
        cutoff = date.today() - timedelta(days=30)
        if sim_date < cutoff:
            return []
        articles: list[dict] = []
        seen = set()
        sample_tickers = tickers[:8]  # limit to avoid slow fetches
        for tk in sample_tickers:
            try:
                import yfinance as yf
                news = yf.Ticker(tk).news or []
                for n in news[:5]:
                    title = n.get("title", "")
                    url = n.get("link", "")
                    if not title or url in seen:
                        continue
                    seen.add(url)
                    score, found_tickers = score_article({"title": title, "url": url})
                    articles.append({"title": title, "url": url,
                                     "score": score, "tickers": found_tickers})
            except Exception:
                pass
        return articles

    def run_all(self, n: int = 10, start_run_id: int = 1) -> list[BacktestRun]:
        import traceback
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # Pre-warm GDELT disk cache so parallel threads never block on outbound requests.
        self._warm_gdelt_cache()

        spy_return = self.prices.returns_pct("SPY", self.prices.trading_days[0],
                                             self.prices.trading_days[-1])
        print(f"[engine] SPY baseline {self.prices.trading_days[0]} → "
              f"{self.prices.trading_days[-1]}: {spy_return:+.2f}%")
        # Print persona map so the run log is self-describing.
        print(f"[engine] Launching {n} runs starting at run_id={start_run_id}")
        for i in range(start_run_id, start_run_id + n):
            p = persona_for(i)
            print(f"[engine]   run_id={i} persona={p['name']}")

        results: list[BacktestRun] = []
        completed = 0

        def _run(i: int):
            try:
                return self.run_one(i)
            except Exception as e:
                print(f"[engine] RUN {i} CRASHED: {e}")
                traceback.print_exc()
                self.store.upsert_run(i, 0, "failed")
                return None

        with ThreadPoolExecutor(max_workers=n) as pool:
            futures = {pool.submit(_run, i): i
                       for i in range(start_run_id, start_run_id + n)}
            for fut in as_completed(futures):
                run_id = futures[fut]
                result = fut.result()
                completed += 1
                if result is not None:
                    results.append(result)
                print(f"[engine] {completed}/{n} runs finished (run_id={run_id})")
                if completed % 2 == 0:
                    self._send_progress(completed, n, results, spy_return)

        # Feed the ML model with the winners' decisions before announcing completion.
        try:
            self._train_ml_from_winners(results)
        except Exception as e:
            print(f"[engine] _train_ml_from_winners failed: {e}")
            traceback.print_exc()

        self._send_final(results, spy_return)
        return results

    def _train_ml_from_winners(self, results: list[BacktestRun]) -> None:
        """Write a JSONL training feed from the top 3 runs' decisions.

        Each non-HOLD decision from a winning run becomes a training record. BUY
        decisions get ai_score=5.0 (positive label); SELL decisions get 0.0
        (negative label). The file is then offered to the digital-intern
        trainer; that module does not currently support --from-file (it pulls
        from its own SQLite store) so we just persist the JSONL for later use.
        """
        if not results:
            print("[ml-feed] no results — skipping winner training feed")
            return
        # Top 3 by return; if fewer than 3 results, take whatever we have.
        winners = sorted(results, key=lambda r: r.total_return_pct, reverse=True)[:3]
        out_path = ROOT / "data" / "winner_training.jsonl"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        records: list[dict] = []
        for run in winners:
            try:
                rows = self.store.conn.execute(
                    "SELECT action, ticker, sim_date, reasoning FROM backtest_decisions "
                    "WHERE run_id = ? AND action IS NOT NULL AND action != 'HOLD'",
                    (run.run_id,),
                ).fetchall()
            except Exception as e:
                print(f"[ml-feed] read decisions failed for run {run.run_id}: {e}")
                continue
            for row in rows:
                action = (row["action"] or "").upper()
                if action not in ("BUY", "SELL"):
                    continue
                ticker = row["ticker"] or ""
                sim_date = row["sim_date"] or ""
                records.append({
                    "title": f"{action} {ticker} on {sim_date}",
                    "source": f"backtest_run_{run.run_id}_winner",
                    "ai_score": 5.0 if action == "BUY" else 0.0,
                    "urgency": 1,
                    "label": action,
                    "reasoning": row["reasoning"] or "",
                    "return_pct": run.total_return_pct,
                })

        try:
            with out_path.open("w") as fh:
                for rec in records:
                    fh.write(json.dumps(rec) + "\n")
            print(f"[ml-feed] wrote {len(records)} records "
                  f"from {len(winners)} winners → {out_path}")
        except Exception as e:
            print(f"[ml-feed] write failed: {e}")
            return

        # The digital-intern trainer reads from its own SQLite store and does
        # not (yet) accept --from-file. We invoke it anyway and tolerate
        # failure — the JSONL is the durable artifact a future trainer mode
        # can consume. To wire this up properly, extend ml/trainer.py with a
        # CLI that ingests this JSONL into the articles store.
        try:
            r = subprocess.run(
                ["python3", "-m", "ml.trainer",
                 "--from-file", str(out_path)],
                cwd="/home/zeph/digital-intern",
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                print(f"[ml-feed] trainer ingest ok: {r.stdout.strip()[:200]}")
            else:
                # Expected when ml.trainer has no CLI — JSONL is still on disk.
                print("[ml-feed] ml.trainer --from-file not implemented yet — "
                      f"JSONL persisted at {out_path}")
        except Exception as e:
            print(f"[ml-feed] trainer invoke skipped: {e}")

    def _send_progress(self, done: int, total: int, results: list[BacktestRun],
                       spy: float) -> None:
        if not results:
            return
        last = results[-1]
        msg = (f"[Backtest] Run {done}/{total} complete. "
               f"Final: ${last.final_value:.2f} "
               f"({last.total_return_pct:+.2f}% vs SPY {spy:+.2f}%)")
        self._discord(msg)

    def _send_final(self, results: list[BacktestRun], spy: float) -> None:
        if not results:
            self._discord("[Backtest Complete] all runs failed")
            return
        avg_return = sum(r.total_return_pct for r in results) / len(results)
        avg_final = sum(r.final_value for r in results) / len(results)
        best = max(results, key=lambda r: r.final_value)
        worst = min(results, key=lambda r: r.final_value)
        msg = (f"[Backtest Complete] {len(results)}/10 runs done. "
               f"avg ${avg_final:.2f} ({avg_return:+.2f}%), "
               f"best ${best.final_value:.2f} ({best.total_return_pct:+.2f}%), "
               f"worst ${worst.final_value:.2f} ({worst.total_return_pct:+.2f}%). "
               f"SPY baseline {spy:+.2f}%. "
               f"Dashboard: http://10.19.203.44:8090/backtests")
        self._discord(msg)

    def _discord(self, message: str) -> bool:
        if not shutil.which("openclaw"):
            print(f"[discord] (no openclaw) {message}")
            return False
        try:
            r = subprocess.run(
                ["openclaw", "message", "send", "--channel", "discord",
                 "--target", "channel:1496099475838603324", "--message", message],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode != 0:
                print(f"[discord] failed: {r.stderr.strip()[:200]}")
                return False
            print(f"[discord] sent: {message[:100]}")
            return True
        except Exception as e:
            print(f"[discord] exception: {e}")
            return False


if __name__ == "__main__":
    BacktestEngine().run_all(10)
