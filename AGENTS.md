# AGENTS.md — paper-trader

Companion to `CLAUDE.md` aimed at coding agents that touch this repo
during automated review / fix cycles. Where `CLAUDE.md` documents the
*system*, this file documents the *workflows*.

## Repository layout (quick reference)

- `paper_trader/runner.py` — live trader main loop
- `paper_trader/strategy.py` — live Opus decision engine + watchlist
- `paper_trader/backtest.py` — backtest engine, `_ml_decide`, indicators
- `paper_trader/ml/decision_scorer.py` — MLP that gates trade conviction
- `run_continuous_backtests.py` — long-running training loop
- `tests/` — pytest suite (all offline, all deterministic)

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

### Tests

```bash
# ML + backtest only
cd /home/zeph/paper-trader && python3 -m pytest tests/ -v -k "ml or backtest or scorer"

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
