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
| `test_core_strategy.py` | JSON parse w/ fences + trailing prose, RSI/EMA/MACD math, SELL-exceeds-held blocking, BUY insufficient cash blocking, **ambiguous option close blocking**, **expired-option settlement** (`_option_expired` boundary incl. expiry-day-still-live; `_expired_intrinsic` ITM/OTM/no-underlying; `_portfolio_snapshot` marks expired contracts to intrinsic/0 not premium; live-option transient-None still → avg_cost; `SELL_CALL` on a dead contract settles at intrinsic; **`_portfolio_snapshot` total_value = cash + Σ position market_value across a mixed stock+option book**) |
| `test_core_runner.py` | `_maybe_daily_close` weekend/time gating + once-per-day flag + retry-on-failure, `_maybe_hourly` 3600s gating + retry-on-failure |
| `test_core_reporter.py` | openclaw missing → False, timeout/nonzero exit → False, trade alert + decision log + portfolio line formatting, **daily-close P/L baseline label tracks `_INITIAL_EQUITY` not a hardcoded `$1000`** |
| `test_round_trips.py` | `build_round_trips` arithmetic: simple/partial/re-entry round-trips, option ×100, distinct (ticker,type,strike,expiry) keys, open-lot exclusion, orphan SELL, zero-cost `pnl_pct=None`, negative/unparseable `hold_days`, sub-cent rounding |
| `test_core_analytics.py` | `/api/analytics` end-to-end via Flask test client: exact `win_rate_pct` / `profit_factor` / `avg_holding_days` / `realized_pl_usd` / `n_round_trips` for a fixed ledger; open positions excluded; empty ledger → null metrics |
| `test_core_dashboard_helpers.py` | Pure dashboard helpers with no prior coverage: `_scorer_verdict` 5-way boundary bucketing; `_position_ages_from_trades` open-lot state machine (partial-sell keeps entry, full-sell→re-buy resets, option trades ignored); `_next_market_open` open/close/weekend/holiday arithmetic; `_classify_action` co-pilot selection incl. the **EXIT-before-TRIM** ordering regression and "never BUY without a technical confirm" |
| `test_decision_drought.py` | `build_decision_drought` segmentation: `_classify` fill/block/hold/no-decision; two-drought scenario with exact portfolio/SPY/alpha %; PARALYSIS vs DELIBERATE_HOLD split; ongoing drought detection; `involuntary_alpha_bleed_pct` counts PARALYSIS-only negative alpha; min-reportable-cycles filter; NEVER_TRADED / NO_DATA verdicts; alpha=None when SPY missing |
| `test_news_edge.py` | `build_news_edge`: `_index_at_or_after` exact/gap/overflow; EDGE_CONFIRMED with exact raw means; **SPY-abnormal subtraction is applied** (raw 2.0, spy +1.0 → abnormal 1.0); NO_EDGE on a falling top-band ticker; INSUFFICIENT_DATA under `_MIN_BAND_N`; `$TK`/word-boundary resolution incl. "AMDOCS" must not match AMD; **adaptive reference horizon degrades to 1d when only a 1d forward window exists** (the live-data early-history case) |

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
   breaks. `_maybe_daily_close` also skips weekends **and** NYSE full-holiday
   closes (`market.NYSE_HOLIDAYS_2026`) — both guards `return` *before* touching
   `_daily_close_sent_for`, so the flag never advances on a non-trading day and
   the next real trading day still gets its close report. Locked by
   `tests/test_core_runner.py::TestMaybeDailyClose` (incl.
   `test_does_not_fire_on_nyse_holiday`).

7. **`paper_trader.db` uses WAL** — any external reader must use
   `PRAGMA journal_mode=WAL` or open the file as `file:...?mode=ro` to avoid
   lock contention with the live writer.
   *Known concern (not fixed here — too invasive for a surgical pass):* the
   in-process Flask dashboard runs in a daemon thread but shares the **same**
   `Store` singleton (and thus the same `sqlite3.Connection`,
   `check_same_thread=False`) as the runner. Writes are serialized by
   `Store._lock`; reads (`get_portfolio`, `recent_trades`, `open_positions`,
   `recent_decisions`, `equity_curve`) are **not**. Concurrent dashboard reads
   during a runner write are tolerated by WAL but are not strictly
   connection-safe. A proper fix would give the dashboard its own read-only
   connection rather than reworking locking on the live writer.

8. **Position uniqueness** — the `positions` table has a *table-wide* UNIQUE
   constraint on `(ticker, type, expiry, strike)` (it is **not** scoped to
   `closed_at IS NULL` — there is no partial index). A second BUY on an
   existing open lot blends the avg_cost; a SELL that zeros out qty marks the
   row closed. A re-BUY after a full close **reactivates the same row** (fresh
   qty/avg_cost/opened_at, marks reset, `closed_at` cleared) — it does *not*
   insert a new row. This is load-bearing: because SQLite treats NULLs as
   distinct in UNIQUE, the old "insert a new row" path only worked for stock
   (NULL strike/expiry); re-entering a previously-closed *option* raised an
   uncaught `IntegrityError` mid-`_execute`, leaving a recorded trade with no
   position and skipping the cash debit + decision/equity write. Locked by
   `tests/test_core_store.py::TestUpsertPosition::test_reopen_option_after_close_does_not_crash`.

9. **Deterministic ordering** — `store.recent_trades`, `recent_decisions`, and
   `equity_curve` order by `(timestamp DESC, id DESC)`. The `id` tiebreaker is
   load-bearing: two writes inside the same microsecond collide on `timestamp`
   alone, and `runner._cycle` reads `recent_trades(1)` immediately after
   `_execute` records a trade — without the tiebreaker `send_trade_alert` could
   post a stale same-microsecond row. `equity_curve` still returns ascending
   `{timestamp,total_value,cash,sp500_price}` (no `id` leaked to callers).
   Locked by `tests/test_core_invariants.py::TestSameTimestampOrdering`.

10. **Round-trip aggregation has one home** — `paper_trader/analytics/round_trips.py::build_round_trips`
   is the single source of truth for closed-round-trip P&L (a round-trip is the
   slice of same-`(ticker,type,strike,expiry)` trades from qty-leaves-zero to
   qty-returns-zero; a re-BUY after a full close starts a new one). `analytics_api`
   (`/api/analytics`) consumes it for `win_rate_pct` / `profit_factor` /
   `avg_holding_days`; do **not** reintroduce an inline copy here or in a future
   trade-attribution endpoint — they drift. `pnl_usd` is rounded to 4dp and the
   win/loss split is strict `> 0`, so a sub-cent artefact reads as a non-win
   (pinned by `tests/test_round_trips.py::TestEdgeCases::test_subcent_pnl_rounds_to_zero`).
   The `/api/backtests/compare` win-rate is a **different** metric (per-fill FIFO
   lot win/loss, stocks only) and intentionally does *not* use this helper.

11. **Scorer honesty is end-to-end** — every panel that surfaces a
   DecisionScorer prediction calls `predict_with_meta()` (never the bare
   scalar `predict()`) and propagates `off_distribution` +
   `raw_pred_5d_return_pct`: `/api/scorer-predictions`, `/api/position-thesis`
   (→ thesis card → unified conviction board), `/api/disagreement`,
   `/api/scorer-confidence`. A clamped ±50 floor must never reach a UI/board
   without its low-trust flag, or a phantom "confident EXIT" pins downstream
   conviction. Locked by `tests/test_scorer_honesty.py`. **The on-disk clamp
   is necessary but not sufficient: a long-running `:8090` process that
   booted before the clamp commit keeps extrapolating to ±700% in memory.**
   `/api/build-info` (`stale: true`) is the canonical signal that a restart
   is required to apply committed scorer/code fixes; locked by
   `tests/test_build_info.py`.

12. **One source of truth for the $1000 baseline** — every starting-equity /
   P&L-% denominator must read `store.INITIAL_CASH`, never a hardcoded
   `1000.0`. `reporter._INITIAL_EQUITY`, `dashboard.portfolio_api`
   (`starting_value`), and `dashboard.analytics_api` (Calmar's
   `total_return_pct`) all reference the constant. A literal silently
   desyncs the moment `INITIAL_CASH` moves (fixed in `reporter.py`,
   commit `2a154df`; the analytics Calmar leak fixed in this pass). The
   backtest-side `1000.0` in `backtest_compare`'s empty-curve fallback is a
   *separate* baseline (`backtest.py`'s own `INITIAL_CASH`) and is out of
   scope of this rule. Locked by
   `tests/test_core_analytics.py::TestCalmarBaseline`.

13. **Expired options settle at intrinsic, never at premium** — yfinance has
   no option chain past expiry, so `market.get_option_price` returns `None`
   for a held-to-expiry contract. The old `cur = cur or p["avg_cost"]` in
   `strategy._portfolio_snapshot` then marked a (usually worthless) expired
   contract at its full purchase premium **forever**, never closing it —
   silently inflating `total_value` and every reported P/L. The system
   prompt explicitly tells Opus it "can hold options through expiry", so
   this is reachable *by design*, not an accident. Fixed at two sites:
   `_portfolio_snapshot` (the mark) and `_execute`'s `SELL_CALL`/`SELL_PUT`
   close path. Both now route an expired contract through
   `strategy._expired_intrinsic(ticker, otype, strike)` =
   `max(0, underlying−strike)` (call) / `max(0, strike−underlying)` (put),
   falling back to **0.0** (never avg_cost) when the underlying price is
   unavailable. The `or`→`is not None` change on the mark fallback is
   load-bearing: a legitimate `0.0` intrinsic must survive, and `0.0 or
   avg_cost` would clobber it straight back to premium. `_option_expired`
   uses `<` (an option is live *on* its expiry date).
   **This is a *valuation* fix, not a risk limit.** It does not violate the
   "no hard risk limits / Opus has full autonomy" invariant (#2) — that
   invariant governs *gating decisions*, not *valuing instruments*. Do not
   read this as an autonomy violation and revert it. Full auto-settlement
   (recording a synthetic SELL + closing the row at expiry) was
   *deliberately deferred*: it would make `_portfolio_snapshot` state-
   mutating for every caller (it is currently a pure mark), which is too
   invasive for a surgical pass and risks the parse-retry tests that
   monkeypatch it. The conservative fix removes the phantom-equity harm; an
   expired contract simply marks to its true value and stays an open row
   until Opus closes it (and closing it now also settles correctly). The
   live `paper_trader.db` has had **zero** option positions to date, so
   this is latent, not active — but the bug is real code-path and the test
   suite locks the desired behaviour. Locked by
   `tests/test_core_strategy.py::TestOptionExpired` /
   `::TestExpiredIntrinsic` / `::TestPortfolioSnapshotExpiredOptions` /
   `::TestExecuteCloseExpiredOption`.

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
| `GET /api/scorer-predictions` | DecisionScorer 5d-return predictions per held stock (clamped; `off_distribution` + `raw_pred_5d_return_pct` flag extrapolation) |
| `GET /api/sector-heatmap` | DRAM/semis sector heatmap with momentum + news pulse |
| `GET /api/news-deduped` | Top signals after dedup + exponential urgency decay |
| `GET /api/position-thesis` | Per-position cards combining scorer + technicals + news + last decision. Each card carries `off_distribution` + `raw_pred_5d_return_pct` so the unified conviction board can decay its ML axis off the explicit flag (not a re-derived magnitude heuristic) |
| `GET /api/calibration` | Confidence-bucket win rate + signal-source attribution |
| `GET /api/drawdown` | Drawdown anatomy: peak/trough, time-in-DD, per-position contribution |
| `GET /api/earnings-risk` | Upcoming earnings ⨯ held positions / watchlist, tiered |
| `GET /api/scorer-confidence` | Empirical residual bands + directional hit-rate for DecisionScorer |
| `GET /api/decision-health` | Action mix, NO_DECISION parse-failure rate, confidence trend |
| `GET /api/decision-forensics` | *Why* NO_DECISION: failure-mode taxonomy (timeout/truncated/no-json/fenced/prose/malformed/legacy), open-vs-closed split, hourly trend, retry-exhausted count, actionable hint + raw Opus excerpts |
| `GET /api/liquidity` | Capital deployment & liquidity: cash vs deployed %, position weights, unrealized P/L, days-since-last-entry, status (NO_DRY_POWDER/DRY_POWDER_LOW/BALANCED/CASH_HEAVY) + flags |
| `GET /api/build-info` | Code-freshness probe: `{boot_sha, head_sha, behind, stale}`. `stale: true` ⇒ this `:8090` process booted before the on-disk HEAD — committed fixes (e.g. the DecisionScorer ±50 clamp) are NOT applied until restart. The unified dashboard's landing banner reads this + its own to flag stale processes |
| `GET /api/decision-drought` | What the trader's *inaction* cost. Segments cycles into droughts between FILLED trades; per drought: duration, NO_DECISION/HOLD/BLOCKED mix, portfolio Δ% vs S&P Δ% over the idle window, alpha. Splits involuntary `PARALYSIS` (NO_DECISION-dominated) from `DELIBERATE_HOLD`; `involuntary_alpha_bleed_pct` sums the **negative alpha of PARALYSIS droughts only** (DELIBERATE_HOLD drift is a strategy choice, excluded). Complements decision-forensics (*why*) with the *cost*. DB-only, no network. Pure core: `analytics/decision_drought.py::build_decision_drought` |
| `GET /api/news-edge` | Does a high-`ai_score` headline actually predict the move? Per live (non-backtest) scored article naming a watchlist ticker, 1/3/5-trading-day forward return — raw **and SPY-abnormal** — banded by ai_score; verdict judged on abnormal return only. `?days=` (lookback, default 30) / `?min_score=` (default 2.0). Reference horizon is **adaptive**: the longest horizon whose top band is well-sampled, falling back to 1d early on — so the verdict *matures with article history* (digital-intern's `articles.db` only retains a few days of live news, so 3d/5d populate as history deepens; early state is honestly `INSUFFICIENT_DATA` with partial 1d data, never all-dashes). Live-only SQL filter inlined. Pure core: `analytics/news_edge.py::build_news_edge`; daily-bar yfinance history cached 30 min (`_NEWS_EDGE_PX_CACHE`) |

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
| Equity / P/L looks too high and won't come down; an option position never closes | Pre-fix `_portfolio_snapshot` marked an expired contract at avg_cost forever (no live chain past expiry). Fixed — see invariant #13. If you see this on an old `:8090` process, check `/api/build-info` `stale` and restart | `strategy._option_expired` / `_expired_intrinsic`; `SELECT * FROM positions WHERE type IN ('call','put') AND closed_at IS NULL AND expiry < date('now')` |

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
after each cycle. Until trained AND `_n_train >= 500`, `predict()` returns
`0.0` and `_ml_decide` ignores it entirely.

Once `_scorer.is_trained and _n_train >= 500`, the scorer **modulates BUY
conviction only — it never cancels a trade** (an earlier HOLD-block
version oscillated leveraged-ETF strategies; see the comment in
`_ml_decide`). Given the predicted 5-day return `p`:

| Condition | Effect on conviction |
|-----------|----------------------|
| `p < -10` | `× 0.6` (strong headwind, still buys) |
| `-10 ≤ p < 0` | `× 0.85` (mild headwind) |
| `0 ≤ p ≤ 5` | unchanged |
| `5 < p ≤ 10` | `× 1.15`, capped at 0.95 |
| `p > 10` | `× 1.3`, capped at 0.95 |

> Note: `CLAUDE.md` §6 still documents the older HOLD-blocking gate
> (`p < -5 → HOLD`, `p < 0 → ×0.7`). The code in `_ml_decide` above is
> authoritative; CLAUDE.md §6 is stale on this point.

**Prediction is clamped to the empirical label support.** `MLPRegressor`
has no output bound, so for off-distribution feature vectors it extrapolates
to nonsense (observed: −89% then +32% for the *same* LITE vector across two
retrain cycles — the unbounded head is volatile). `predict()` clamps its
output to `±PRED_CLAMP_PCT` (50%). The bound is load-bearing-safe: across the
9k+ rows in `decision_outcomes.jsonl` only ~0.4% of real 5d outcomes exceed
|50%| (p1=−25%, p99=+32%), and every gate boundary above (±10/±5/0) sits well
inside ±50, so a clamped −89→−50 stays in the same `p < -10 → ×0.6` bucket —
**gating behaviour is unchanged**. Clamping is output-only: it does not touch
`build_features`/`SECTORS`/`N_FEATURES`, so the pickle stays compatible, and
`train_scorer` never calls `predict()`, so there is no label-feedback loop.
The untrained short-circuit (`return 0.0`) still runs *before* the clamp.
`predict_with_meta()` is the sibling that exposes
`{pred, raw, clamped, off_distribution}` for panels that want to flag
extrapolation honestly (`/api/scorer-predictions` adds `off_distribution`
+ `raw_pred_5d_return_pct`; the unified dashboard's `_conviction_axes` decays
the ML axis toward a 0.3 trust floor once `|pred| > 20%` instead of letting a
clamped floor read as full ±1.0 conviction). Locked by
`tests/test_decision_scorer.py::TestPredictionClamp`.

**Concurrency invariant (`backtest.py`):** the module-global
`_VOLUME_CACHE` is shared across the parallel run threads. Every read
*and* every iteration of it must hold `_VOLUME_CACHE_LOCK` — iterating it
unlocked while another run thread inserts raises
`RuntimeError: dictionary changed size during iteration`, which the
persist helper's `try/except` swallows (silently dropping the disk
cache so every run re-fetches volumes from yfinance). It is also
window-keyed and never evicted, so a long-lived continuous loop's RSS
grows slowly across cycles — restart the loop periodically; do not add
an ad-hoc eviction policy without measuring.

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

### Position sizing invariant (`_ml_decide`)

A backtest BUY's notional is `min(total_val * conviction, cash * 0.95)`.
`conviction` has a hard ceiling: `min(0.25, best_score/20)` for normal
tickers, `min(0.40, best_score/15)` for a `_LEVERAGED_ETFS` name in a
bull/sideways regime. The DecisionScorer (once `_n_train >= 500`) only
*modulates* this conviction — it never lifts the cap (the ×1.3/×1.15
tailwind arms are themselves capped at 0.95, and the notional is still
clipped by the two `min`s). Both arms are now test-locked:
`tests/test_backtest.py::TestMlDecide::test_oversize_buy_clipped_by_cash`
pins the cash arm; `::test_conviction_caps_position_size_when_cash_is_abundant`
pins the conviction arm with exact expected values (a regression that drops
`min(0.25, …)` doubles the notional and fails the assertion). If you change
the conviction formula, update both tests deliberately — they assert exact
numbers, not ranges, by design.

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
  subclass (`np.float64` is). `_to_float` falls back to an **`np.number`**
  check (NOT `np.generic`: `np.generic` also matches `np.str_`/`np.bool_`,
  and `np.isfinite(np.str_("x"))` raises an *unhandled* `TypeError` that
  would propagate out of `build_features` and crash `train_scorer`; numpy
  strings/bools must take the safe default like Python `str`/`bool` do).
  If you add new numpy inputs, verify they pass through. It rejects
  every non-finite value (NaN **and** ±inf) on both the Python and numpy
  branches via `math.isfinite` / `np.isfinite` — this is load-bearing: a
  single `decision_outcomes.jsonl` row with a non-finite `forward_return_5d`
  poisons `train_scorer`'s `y` vector, `MLPRegressor.fit` raises, and
  `_train_decision_scorer` swallows it — silently wedging scorer retraining
  for that cycle and every cycle after (the row persists in the 5000-record
  tail). Pinned by `tests/test_decision_scorer.py::TestToFloat` +
  `::TestTrainScorer::test_handles_non_finite_forward_return`.
- **Forward leakage** — anything that reads news must filter on
  `url NOT LIKE 'backtest://%'` and `source NOT LIKE 'backtest_%'` /
  `'opus_annotation%'`. The live `signals.py` and the backtest
  `_load_local_articles` / `_query_news_context` already do this; new
  readers must mirror it.
- **Single sqlite3 connection across threads** — `BacktestStore.conn` is
  shared across run threads and the background `_opus_annotate` thread.
  Every read / write must hold `store._lock`. If you add a new query path,
  copy the locking pattern from `_trim_history` / `_append_top_decisions`.
- **Resolve module-global paths at call time, not as default args** —
  `def __init__(self, path=BACKTEST_DB)` binds the global's *value* at
  import, so conftest's `monkeypatch.setattr(bt, "BACKTEST_DB", tmp)` is a
  no-op for that call and the no-arg `BacktestStore()` silently hits the
  real persistent DB (this caused an order-dependent flaky test). Use
  `path=None` then `path = path or BACKTEST_DB` inside the body. Same rule
  for any new constructor that touches `SCORER_PATH` / `CACHE_DIR` / a
  cache path the conftest redirects. Locked by
  `tests/test_backtest.py::TestBacktestStoreIsolation`.
- **`SAMPLE_EVERY_N_DAYS = 1`** — backtests sample every trading day.
  Don't change this casually; the continuous loop's timing budget
  assumes a year-long sim completes in ~minutes per run.
- **Scorer-train status must stay truthful** — in
  `run_continuous_backtests.py::_train_decision_scorer`, `train_scorer()`
  pickles the model to `SCORER_PATH` and returns `status="ok"` *before* the
  temporal-OOS diagnostic runs. The OOS block (`DecisionScorer()` reload +
  `evaluate_scorer_oos`) has its **own** `try/except` that degrades to
  `oos_rmse=n/a (...)`. Do not collapse it back into the outer
  train `try/except`: a post-train diagnostic crash would then surface as
  `scorer err` on the operator-facing log/Discord even though the scorer is
  trained and the next cycle's singleton reset deploys it — a false
  "scorer broken / gate never engages" signal. Locked by
  `tests/test_continuous.py::TestTrainDecisionScorer::test_oos_eval_failure_does_not_mask_successful_train`.
- **Run-return weight is applied twice into the ArticleNet feed (by
  design, not a bug)** — `_append_top_decisions` folds the per-run weight
  `w = 0.5 + 0.5·(ret−min)/span` into the JSONL `ai_score`
  (`w·5.0` for BUY, `w·0.5` for SELL) *and* stores the bare `w` as
  `weight`. `_inject_and_train` then writes `eff = min(10, ai_score·weight)`
  into digital-intern's `articles.db`, so a top-run BUY lands at `≈5·w²`
  (`w∈[0.5,1.0] → eff∈[1.25,5.0]`) — the run-quality term is **squared**,
  intentionally compressing lower-ranked runs harder than a linear weight
  would. This only affects ArticleNet's training emphasis (a *separate*
  model in digital-intern), never the DecisionScorer or any trade. Opus
  annotation rows side-step it (`weight=1.0`, so `eff=ai_score`). Do not
  "linearise" this in a surgical pass — it perturbs ArticleNet training
  dynamics and is out of scope for the ML/backtest review.

### When to bump model versions

The scorer model has no explicit version field. Treat a change to
`N_FEATURES`, `SECTORS`, or `build_features` parameter signature as a
breaking change: delete `data/ml/decision_scorer.pkl` and let the next
continuous cycle retrain from `data/decision_outcomes.jsonl`. The pickle
auto-recreates atomically (`.pkl.tmp` → `replace`) so a fresh-start
deletion is safe even if a backtest thread is mid-read.
