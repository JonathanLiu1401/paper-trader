# AGENTS.md — paper-trader

Companion to `CLAUDE.md` aimed at coding agents that touch this repo
during automated review / fix cycles. Where `CLAUDE.md` documents the
*system*, this file documents the *workflows*.

## Repository layout (quick reference)

- `paper_trader/runner.py` — live trader main loop
- `paper_trader/strategy.py` — live Opus decision engine + watchlist
- `paper_trader/signals.py` — live news signal queries against digital-intern's articles.db
- `paper_trader/market.py` — yfinance wrapper + NYSE session calendar
- `paper_trader/store.py` — SQLite store (portfolio, trades, positions, decisions, equity_curve)
- `paper_trader/reporter.py` — Discord output via openclaw
- `paper_trader/dashboard.py` — Flask dashboard on :8090
- `paper_trader/backtest.py` — backtest engine, `_ml_decide`, indicators
- `paper_trader/ml/decision_scorer.py` — MLP that gates trade conviction
- `run_continuous_backtests.py` — long-running training loop
- `tests/` — pytest suite (all offline, all deterministic)

---

## Core (live trader) domain

### Architecture & data flow

One cycle of the live trader (`paper_trader/runner.py::_cycle`):

```
runner._cycle()
  └─▶ strategy.decide()
        ├─ market.is_market_open()                      (NYSE hours + 2026 holidays)
        ├─ _portfolio_snapshot(store)                   (mark-to-market every open position)
        ├─ signals.get_top_signals(20, hours=2, ≥4.0)   (live-only DB filter)
        ├─ signals.get_urgent_articles(minutes=30)
        ├─ signals.ticker_sentiments(WATCHLIST, hours=4)
        ├─ market.get_prices(WATCHLIST + futures + ^GSPC)
        ├─ get_quant_signals_live(...)                  (RSI / MACD / BB / momentum, 5-min cached)
        ├─ _build_payload(...) → SYSTEM_PROMPT          (single string)
        ├─ _claude_call(...) → JSON                     (subprocess: claude --print --permission-mode bypassPermissions)
        ├─ _parse_decision(...)                         (strip ```json fences, raw_decode first {…})
        ├─ _enforce_risk_pre_trade(...)                 (only blocks SELL beyond held qty)
        ├─ _execute(...)                                (BUY / SELL / BUY_CALL / BUY_PUT / SELL_CALL / SELL_PUT / HOLD / REBALANCE)
        ├─ store.record_decision(...) / store.record_equity_point(...)
        └─ return summary dict
  └─▶ if FILLED: reporter.send_trade_alert(...) + reporter.send_decision_log(...)
  └─▶ _maybe_hourly() + _maybe_daily_close()
  └─▶ sleep OPEN_INTERVAL_S (1800s) or CLOSED_INTERVAL_S (3600s)
```

`_portfolio_snapshot` is called twice in `decide()` — once before the trade
(input to the prompt) and once after (so the equity_point reflects post-trade
mark-to-market). The two calls keep the DB's `positions_json` and `total_value`
consistent through the cycle.

### How to run the paper trader

```bash
cd /home/zeph/paper-trader

# Foreground (logs to stdout)
python3 -m paper_trader.runner

# Under systemd
systemctl --user start paper-trader   # see paper-trader.service
journalctl --user -fu paper-trader

# Dashboard only (no decision loop)
python3 -c "from paper_trader.dashboard import run; run(host='0.0.0.0', port=8090)"
```

The runner starts a daemon thread for the Flask dashboard on `:8090` and
posts a `**PAPER TRADER ONLINE**` ping to Discord on first boot.

### How to run tests

```bash
cd /home/zeph/paper-trader && python3 -m pytest tests/ -v
```

All tests are offline — yfinance, Discord, and the digital-intern DB are
mocked. The `tests/conftest.py` autouse fixture redirects backtest paths to
a tmp directory; core tests use their own `fresh_store` fixture that points
`store.DB_PATH` at `tmp_path`.

Core tests live in `tests/test_core_*.py` — one file per module under
review:

| File | What it asserts |
|------|-----------------|
| `test_core_store.py` | cash bookkeeping, position upsert/blend/close, trade & equity ordering |
| `test_core_market.py` | weekend / pre-open / after-close / holiday gating, price-cache TTL, option chain lookup |
| `test_core_signals.py` | top-signal score threshold + sort order, backtest-row filter, urgent ai_score=NULL coercion, ticker regex word-boundary |
| `test_core_strategy.py` | JSON parse w/ fences + trailing prose, RSI/EMA/MACD math, SELL-exceeds-held blocking, BUY insufficient cash blocking, **ambiguous option close blocking** |
| `test_core_runner.py` | `_maybe_daily_close` weekend/time gating + once-per-day flag + retry-on-failure, `_maybe_hourly` 3600s gating + retry-on-failure |
| `test_core_reporter.py` | openclaw missing → False, timeout/nonzero exit → False, trade alert + decision log + portfolio line formatting |

### Key invariants and constraints

1. **Live trader uses Claude Opus 4.7** — `MODEL = "claude-opus-4-7"` in
   `strategy.py`. The whole prompt is tuned around Opus's reasoning. Do not
   downgrade to Sonnet without an explicit decision.

2. **No hard risk limits** — `_enforce_risk_pre_trade` only checks that a
   SELL doesn't exceed held quantity. There are no position-size, leverage,
   or daily-loss caps. The system prompt grants Opus full autonomy. If a
   reviewer "fixes" this by adding caps, it changes the system's identity —
   discuss before merging.

3. **Live-only DB filter** — every read in `signals.py` against digital-intern's
   `articles.db` includes:
   ```sql
   AND url NOT LIKE 'backtest://%'
   AND source NOT LIKE 'backtest_%'
   AND source NOT LIKE 'opus_annotation%'
   ```
   Mirror this in any new query. The dashboard's `_ticker_news_pulse` already
   does. Forgetting the filter contaminates live signals with the engine's
   own backtest annotations.

4. **Ambiguous option closes are rejected** — when `SELL_CALL` / `SELL_PUT`
   matches more than one open contract and `strike`/`expiry` are unspecified,
   `_execute` returns `BLOCKED` with the open legs in the detail string.
   Picking the "first match" silently could exit the wrong leg.

5. **openclaw env key invariant** — the Discord channel ID lives directly in
   `reporter.DISCORD_CHANNEL`. Do NOT add an env-key dependency or move the
   channel ID into `openclaw.json` — the current setup intentionally hard-codes
   the channel so a missing config doesn't silently route messages elsewhere.

6. **Hourly/daily close idempotence** — `_maybe_hourly` and `_maybe_daily_close`
   only advance their "last sent" markers on actual send success. A transient
   openclaw failure retries on the next cycle rather than silently skipping
   the hour or day. If a reviewer adds a "fire-and-forget" path, this property
   breaks.

7. **`paper_trader.db` uses WAL** — any external reader must use
   `PRAGMA journal_mode=WAL` or open the file as `file:...?mode=ro` to avoid
   lock contention with the live writer.

8. **Position uniqueness** — the `positions` table has a UNIQUE constraint on
   `(ticker, type, expiry, strike)` with `closed_at IS NULL`. A second BUY on
   an existing open lot blends the avg_cost; a SELL that zeros out qty marks
   the row closed. A re-BUY after close creates a new row.

### Dashboard API endpoints (port 8090)

All endpoints serve `application/json`. CORS is wide open (`*`) so the
Digital Intern dashboard on `:8080` can cross-fetch.

| Endpoint | Purpose |
|----------|---------|
| `GET /` | HTML — live trader page (portfolio + trades + chart) |
| `GET /backtests` | HTML — backtest grid + equity overlay |
| `GET /api/state` | Portfolio + positions + last 40 trades + last 20 decisions + equity curve |
| `GET /api/portfolio` | Compact portfolio read (consumed by Digital Intern at :8080) |
| `GET /api/backtests` | Full backtest run list with SPY/QQQ baselines |
| `GET /api/backtests/<run_id>` | Single backtest detail (trades, decisions, equity) |
| `GET /api/backtests/compare?ids=1,2,3` | Normalized overlay of 2–4 runs |
| `GET /api/backtests/<run_id>/trades` | Trades for a single backtest run |
| `GET /api/backtests/<run_id>/decisions` | Decisions for a single backtest run |
| `GET /api/model-progress` | Per-cycle aggregated returns for the Model Progress chart |
| `GET /api/analytics` | Sector exposure, Sharpe, Sortino, Calmar, win rate, profit factor, beta, drawdown |
| `GET /api/sector-pulse` | Semis-focused card: price, RSI, vol_ratio, top headline per ticker |
| `GET /api/risk` | Concentration, leveraged exposure, position ages, SPY-shock estimate |
| `GET /api/briefing` | Pre-market / live briefing: futures, next-open countdown, urgent news |
| `GET /api/suggestions` | Trade-idea cards: BUY / ADD / TRIM / EXIT / WATCH per ticker |
| `GET /api/greeks` | Per-leg and portfolio-wide Black-Scholes Greeks |
| `GET /api/scorer-predictions` | DecisionScorer 5d-return predictions per held stock |
| `GET /api/sector-heatmap` | DRAM/semis sector heatmap with momentum + news pulse |
| `GET /api/news-deduped` | Top signals after dedup + exponential urgency decay |
| `GET /api/position-thesis` | Per-position cards combining scorer + technicals + news + last decision |
| `GET /api/calibration` | Confidence-bucket win rate + signal-source attribution |
| `GET /api/drawdown` | Drawdown anatomy: peak/trough, time-in-DD, per-position contribution |
| `GET /api/earnings-risk` | Upcoming earnings ⨯ held positions / watchlist, tiered |
| `GET /api/scorer-confidence` | Empirical residual bands + directional hit-rate for DecisionScorer |
| `GET /api/decision-health` | Action mix, NO_DECISION parse-failure rate, confidence trend |

### Common failure modes (live trader)

| Symptom | Likely cause | Where to look |
|---------|--------------|---------------|
| Loop posts `NO_DECISION` every cycle | Claude returned malformed JSON or timed out (`DECISION_TIMEOUT_S=120`) | `strategy.py::_parse_decision`; tail runner stdout for `[strategy] claude err:` |
| Live trader stuck on `BLOCKED` for a SELL | `_enforce_risk_pre_trade` rejected — qty > held, or option `strike+expiry` unspecified with multiple open legs | `strategy.py::_enforce_risk_pre_trade`, `_execute` (option ambiguity check) |
| Hourly summary never posts | `_maybe_hourly` only advances on send success; openclaw missing → permanent retry-loop with stdout log | Search runner stdout for `[reporter] openclaw not installed` |
| `signals.get_top_signals` returns `[]` | `articles.db` not at `USB_DB` (USB unmounted) or `LOCAL_DB`; live-only filter is correct so backtest contamination is *not* the cause | `signals._db_path()`; run `python3 -m paper_trader.signals` |
| `paper_trader.db is locked` | Another writer attached without `?mode=ro`; or a long-running query inside `_lock` | Check for ad-hoc scripts; only the runner should write |
| Dashboard `/api/scorer-predictions` shows `is_trained: false` | `data/decision_outcomes.jsonl` has < 500 rows — scorer hasn't trained enough yet | `wc -l data/decision_outcomes.jsonl` |
| Discord posts stop entirely | `openclaw` binary missing / auth expired | `which openclaw`; `openclaw message send --channel discord ...` manually |
| Live cross-dashboard (`:8080` → `:8090`) shows blanks | CORS or paper-trader process down | `curl http://localhost:8090/api/portfolio` |
| Strategy returns `HOLD` constantly even with strong signals | Opus is being conservative — by design, no threshold gating to override | Inspect the prompt context in `strategy.py::_build_payload`; if the watchlist has stale prices yfinance is rate-limited |

For ML / backtest-side failures, see the ML section below and `CLAUDE.md` §11.

## ML / backtest domain

### How the DecisionScorer works

`paper_trader/ml/decision_scorer.py` defines an MLP (`sklearn.MLPRegressor`,
with a numpy lstsq fallback) that predicts **5-trading-day forward return %**
from a 17-dim feature vector:

| Slot | Feature | Source | Default |
|------|---------|--------|---------|
| 0 | `ml_score` | parsed from `_ml_decide` reasoning | 0.0 |
| 1 | `rsi` (14-period) | `_compute_technical_indicators` | 50.0 |
| 2 | `macd_signal` (numeric) | same | 0.0 |
| 3 | `mom5` (5-day %) | same | 0.0 |
| 4 | `mom20` (20-day %) | same | 0.0 |
| 5 | `regime_mult` | `_market_regime` (bull=1.0, sideways=0.6, bear=0.3, unknown=1.0) | 1.0 |
| 6 | `vol_ratio` | clamped to [0, 5] | 1.0 |
| 7 | `bb_pos` | clamped to [-2, 2] | 0.0 |
| 8 | `news_urgency` | clamped to [0, 100] | 50.0 |
| 9 | `news_article_count` | clamped to [0, 20] | 1.0 |
| 10..16 | sector one-hot | `SECTOR_MAP` lookup | "other" |

Training happens in `run_continuous_backtests.py::_train_decision_scorer`
after each cycle. The model is **only used to gate live trades when
`_n_train >= 500`** — below that threshold it returns 0.0 and `_ml_decide`
treats it as a no-op.

### How to run backtests manually

```bash
cd /home/zeph/paper-trader

# One-shot — 10 parallel year-long runs, default window 2025-05-01..2026-05-13
python3 run_backtests.py

# Continuous loop — 5 runs per cycle, retrains scorer between cycles
python3 run_continuous_backtests.py

# View results
sqlite3 backtest.db "SELECT run_id, total_return_pct, vs_spy_pct, status FROM backtest_runs ORDER BY run_id DESC LIMIT 20"

# Live dashboard
# http://localhost:8090/backtests
```

### How to interpret backtest results

- `total_return_pct` — full-window % change vs. $1000 starting capital.
  Positive means the persona made money; the "winner" of a cycle is the
  highest-positive run.
- `vs_spy_pct` — alpha vs. SPY buy-and-hold over the same window. The
  meaningful metric for skill evaluation.
- `status` — `running` / `complete` / `failed`. `failed` rows often mean
  yfinance returned nothing for the persona's preferred tickers; check
  `continuous.log` for the matching `[engine] RUN N CRASHED:` line.
- `equity_curve_json` — JSON list of `{date, value, cash}` snapshots; the
  dashboard renders these. Sparse during a run (every 5 samples) and full
  at finalize.

A healthy cycle log looks like:

```
[engine] SPY baseline 2025-05-01 → 2026-05-13: +X.X%
[engine] Launching 5 runs starting at run_id=N
[run K] DONE  final=$..  return=+Y.Y%  vs SPY +Z.Z%  trades=NN
[continuous] computed N decision outcomes from M runs
[continuous] scorer ok n=N rmse=...
[continuous] ml: injected I new | trainer n=N loss=...
```

If `scorer insufficient_after_dedup n=...` keeps appearing, the
`data/decision_outcomes.jsonl` tail is too small or too duplicated — more
cycles need to accumulate before the scorer can train.

### Tests (ML + backtest section)

```bash
# ML + backtest only
cd /home/zeph/paper-trader && python3 -m pytest tests/ -v -k "ml or backtest or scorer"

# Core (live trader) only
cd /home/zeph/paper-trader && python3 -m pytest tests/test_core_*.py -v

# Full suite
cd /home/zeph/paper-trader && python3 -m pytest tests/ -v

# A single class
cd /home/zeph/paper-trader && python3 -m pytest tests/test_decision_scorer.py::TestTrainScorer -v
```

All tests are offline — `tests/conftest.py` redirects `SCORER_PATH`,
`PRICE_CACHE_PATH`, `BACKTEST_DB`, and the various cache paths to
`tmp_path` so a test run never clobbers real data. Synthetic deterministic
prices come from the `synthetic_prices` fixture. No test should reach the
network; if you add one that does, mock `yfinance.Ticker` (see
`test_variable_windows.py::_make_fake_hist`).

### Bug-fix workflow

For automated review agents that touch ML / backtest code:

1. **Read first**: `CLAUDE.md` §6 (the two-model section), this file's
   feature table, then the function you're about to edit. The invariants
   in `CLAUDE.md` §8 (especially #1 backtest live-only filter, #5 scorer
   gate threshold, #6 claude subprocess cap) are load-bearing.
2. **Be surgical**: prefer a 3-line edit over a refactor. The continuous
   loop runs unattended; cosmetic churn risks breaking pickle
   compatibility for `data/ml/decision_scorer.pkl` or schema
   compatibility for `data/decision_outcomes.jsonl`.
3. **Run tests before committing**:
   `python3 -m pytest tests/ -v 2>&1 | tail -20`. Failures block
   the commit.
4. **Append an entry to `data/run_log.md`** with the
   `## YYYY-MM-DDTHH:MM:SSZ` header described at the top of that file.

### Common pitfalls

- **Pickle compatibility** — adding a feature to `build_features`
  invalidates `data/ml/decision_scorer.pkl`. The `predict()` exception
  handler now logs once per instance (was silent — masked exactly this
  case during a feature rollout). After a feature change, force a retrain
  by deleting the pickle before the next continuous-loop cycle.
- **`_to_float` and numpy types** — `np.float32` is *not* a Python `float`
  subclass (`np.float64` is). `_to_float` falls back to a `np.generic`
  check; if you add new numpy inputs, verify they pass through.
- **Forward leakage** — anything that reads news must filter on
  `url NOT LIKE 'backtest://%'` and `source NOT LIKE 'backtest_%'` /
  `'opus_annotation%'`. The live `signals.py` and the backtest
  `_load_local_articles` / `_query_news_context` already do this; new
  readers must mirror it.
- **Single sqlite3 connection across threads** — `BacktestStore.conn` is
  shared across run threads and the background `_opus_annotate` thread.
  Every read / write must hold `store._lock`. If you add a new query path,
  copy the locking pattern from `_trim_history` / `_append_top_decisions`.
- **`SAMPLE_EVERY_N_DAYS = 1`** — backtests sample every trading day.
  Don't change this casually; the continuous loop's timing budget
  assumes a year-long sim completes in ~minutes per run.

### When to bump model versions

The scorer model has no explicit version field. Treat a change to
`N_FEATURES`, `SECTORS`, or `build_features` parameter signature as a
breaking change: delete `data/ml/decision_scorer.pkl` and let the next
continuous cycle retrain from `data/decision_outcomes.jsonl`. The pickle
auto-recreates atomically (`.pkl.tmp` → `replace`) so a fresh-start
deletion is safe even if a backtest thread is mid-read.
