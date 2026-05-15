#!/usr/bin/env bash
# Hourly parallel Opus 4.7 pass — 4 agents in parallel.
# Agents 1-3: systematic code review + bug fixes + test suite + docs.
# Agent 4: feature development, brainstorming, user-perspective testing.
set -euo pipefail

export PATH="/home/zeph/.local/bin:/home/zeph/.nvm/versions/node/v24.15.0/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

DISCORD_TARGET="channel:1496099475838603324"
LOG_DIR="/tmp/review_logs"
mkdir -p "$LOG_DIR"
TS=$(date +%Y%m%d_%H%M%S)

notify() {
    openclaw message send --channel discord --target "$DISCORD_TARGET" --message "$1" 2>/dev/null || true
}

notify "🔄 Hourly review cycle started ($TS) — 4 Opus 4.7 agents launching in parallel"

# ── Agent 1: paper-trader core ────────────────────────────────────────────────
(
cd /home/zeph/paper-trader
claude --model claude-opus-4-7 --permission-mode bypassPermissions --print \
'BEFORE STARTING: Read AGENTS.md if it exists in /home/zeph/paper-trader. Read every file listed below in full before touching anything.

You are doing a systematic code review, bug-fix, test suite, and documentation pass on /home/zeph/paper-trader core.

## Files to read in full first:
- AGENTS.md (if exists)
- paper_trader/runner.py
- paper_trader/reporter.py
- paper_trader/signals.py
- paper_trader/strategy.py
- paper_trader/dashboard.py
- paper_trader/market.py
- paper_trader/store.py

## Step 1 — Bug fix pass
Find and fix ALL bugs, logic errors, race conditions, missing error handling, dead code, and quality issues. Be surgical.

## Step 2 — Build comprehensive test suite
Create or update tests/ directory with pytest tests covering:
- paper_trader/signals.py: test signal generation, edge cases (empty data, NaN prices, missing tickers)
- paper_trader/strategy.py: test decision logic, position sizing, risk limits
- paper_trader/store.py: test portfolio read/write, trade recording
- paper_trader/market.py: test is_market_open() with mocked datetime
- paper_trader/runner.py: test _maybe_daily_close() logic

Write tests that can run without external APIs (mock yfinance, mock Discord). Use pytest fixtures. Tests must actually run and pass.

Run tests after writing: cd /home/zeph/paper-trader && python3 -m pytest tests/ -v 2>&1 | tail -30

Fix any test failures before proceeding.

## Step 3 — Write/update AGENTS.md
Create or update /home/zeph/paper-trader/AGENTS.md with:
- Architecture overview (what each file does, data flow)
- How to run the paper trader
- How to run tests: "cd /home/zeph/paper-trader && python3 -m pytest tests/ -v"
- Key invariants and constraints (e.g. no env key in openclaw.json, live trader uses Opus 4.7)
- Common failure modes and how to debug them
- All API endpoints the dashboard exposes

## Step 4 — Verify
python3 -c "import sys; sys.path.insert(0,\".\"); from paper_trader import signals, reporter, strategy; print(\"imports OK\")"
python3 -m pytest tests/ -v 2>&1 | tail -20

## Step 5 — Commit
git add -A && git commit -m "review: bug fixes, test suite, AGENTS.md update" && git push

Completion: openclaw message send --channel discord --target channel:1496099475838603324 --message "[REVIEW] Agent 1 (paper-trader core) done — fixed: [issues], tests: [N passed], docs: updated"
Failure: openclaw message send --channel discord --target channel:1496099475838603324 --message "[REVIEW] Agent 1 (paper-trader core) FAILED: [reason]"' \
> "$LOG_DIR/agent1_$TS.log" 2>&1
) &
A1=$!

# ── Agent 2: paper-trader ML + backtests ─────────────────────────────────────
(
cd /home/zeph/paper-trader
claude --model claude-opus-4-7 --permission-mode bypassPermissions --print \
'BEFORE STARTING: Read AGENTS.md if it exists in /home/zeph/paper-trader. Read every file listed below in full before touching anything.

You are doing a systematic code review, bug-fix, test suite, and documentation pass on /home/zeph/paper-trader ML and backtest files.

## Files to read in full first:
- AGENTS.md (if exists)
- paper_trader/ml/decision_scorer.py
- paper_trader/backtest.py
- run_continuous_backtests.py

## Step 1 — Bug fix pass
Find and fix ALL bugs, logic errors, race conditions, missing error handling, dead code, and quality issues. Be surgical.

## Step 2 — Build comprehensive test suite
Create or update tests/ with pytest tests covering:
- paper_trader/ml/decision_scorer.py: test scoring with mock article data, test edge cases (empty features, zero scores, all-same scores), test that output is in expected range
- paper_trader/backtest.py: test backtest logic with synthetic price series, test that returns are calculated correctly, test position tracking
- run_continuous_backtests.py: test the scheduling logic, test that backtest results are persisted

Mock external dependencies (yfinance, DB reads). Tests must run without network access.

Run tests: cd /home/zeph/paper-trader && python3 -m pytest tests/ -v -k "ml or backtest or scorer" 2>&1 | tail -30

Fix any failures before proceeding.

## Step 3 — Update AGENTS.md
Add or update ML/backtest section in /home/zeph/paper-trader/AGENTS.md:
- How the ML decision scorer works
- How to run backtests manually
- How to interpret backtest results
- Test commands for ML/backtest domain

## Step 4 — Verify
python3 -m pytest tests/ -v 2>&1 | tail -20

## Step 5 — Commit
git add -A && git commit -m "review: ML+backtest bug fixes, test suite, docs update" && git push

Completion: openclaw message send --channel discord --target channel:1496099475838603324 --message "[REVIEW] Agent 2 (ML+backtests) done — fixed: [issues], tests: [N passed], docs: updated"
Failure: openclaw message send --channel discord --target channel:1496099475838603324 --message "[REVIEW] Agent 2 (ML+backtests) FAILED: [reason]"' \
> "$LOG_DIR/agent2_$TS.log" 2>&1
) &
A2=$!

# ── Agent 3: digital-intern full codebase ────────────────────────────────────
(
cd /home/zeph/digital-intern
claude --model claude-opus-4-7 --permission-mode bypassPermissions --print \
'BEFORE STARTING: Read AGENTS.md if it exists in /home/zeph/digital-intern. Read every file listed below in full before touching anything.

You are doing a systematic code review, bug-fix, test suite, and documentation pass on /home/zeph/digital-intern.

## Files to read in full first:
- AGENTS.md (if exists)
- daemon.py
- storage/article_store.py
- watchers/alert_agent.py
- watchers/urgency_scorer.py
- ml/trainer.py
- ml/model.py
- ml/features.py
- collectors/web_scraper.py
- analysis/claude_analyst.py

## Step 1 — Bug fix pass
Find and fix ALL bugs, logic errors, race conditions, missing error handling, dead code, and quality issues. Be surgical.

IMPORTANT constraints:
- backtest:// URLs and backtest_ sources must NEVER reach live signals or Bloomberg alert formatter (they stay in DB for training only)
- ml_score column is for model predictions; ai_score is for LLM labels only — do not let model predictions pollute ai_score
- score_source column must be set correctly: "llm"/"briefing_boost" for LLM labels, "ml" for model predictions

## Step 2 — Build comprehensive test suite
Create or update tests/ directory with pytest tests covering:
- storage/article_store.py: test get_unalerted_urgent filters backtest:// URLs correctly, test score_source isolation, test CRUD operations with in-memory SQLite
- watchers/urgency_scorer.py: test scoring with mock articles, test threshold logic
- ml/features.py: test feature extraction with mock articles, test all 15 feature dimensions are correct
- ml/model.py: test model forward pass with dummy tensors, test checkpoint save/load
- ml/trainer.py: test that training only uses llm/briefing_boost score_source, test sample weighting

Mock the SQLite DB with in-memory SQLite (:memory:). Mock external API calls. Tests must run without network access or the real DB file.

Run tests: cd /home/zeph/digital-intern && python3 -m pytest tests/ -v 2>&1 | tail -30

Fix any failures before proceeding.

## Step 3 — Write/update AGENTS.md
Create or update /home/zeph/digital-intern/AGENTS.md with:
- Architecture overview (workers, data flow from collection to alert)
- Critical invariants (backtest isolation, ml_score vs ai_score separation)
- How to run the daemon
- How to run tests: "cd /home/zeph/digital-intern && python3 -m pytest tests/ -v"
- Worker descriptions and their roles
- How the ML training pipeline works (label flow, weighting)
- Common failure modes and debugging

## Step 4 — Verify
python3 -c "import sys; sys.path.insert(0,\".\"); from storage import article_store; from ml import features, model; print(\"imports OK\")"
python3 -m pytest tests/ -v 2>&1 | tail -20

## Step 5 — Commit
git add -A && git commit -m "review: bug fixes, comprehensive test suite, AGENTS.md" && git push

Completion: openclaw message send --channel discord --target channel:1496099475838603324 --message "[REVIEW] Agent 3 (digital-intern) done — fixed: [issues], tests: [N passed], docs: updated"
Failure: openclaw message send --channel discord --target channel:1496099475838603324 --message "[REVIEW] Agent 3 (digital-intern) FAILED: [reason]"' \
> "$LOG_DIR/agent3_$TS.log" 2>&1
) &
A3=$!

# ── Agent 4: feature development + user-perspective brainstorming ────────────
(
cd /home/zeph
claude --model claude-opus-4-7 --permission-mode bypassPermissions --print \
'BEFORE STARTING: Read AGENTS.md in both /home/zeph/paper-trader and /home/zeph/digital-intern if they exist. Then read the dashboards, strategy, and ML files to build a complete mental model of the system BEFORE implementing anything.

You are a senior product engineer taking full ownership of this trading stack. Your job is creative feature development and user-perspective testing.

Repos:
- /home/zeph/paper-trader   (paper trading engine, ML scorer, backtests, Flask dashboard :8090)
- /home/zeph/digital-intern (news collector, AI scorer, Bloomberg alerts, chat API :8080)
- /home/zeph/unified_dashboard.py (reverse proxy :8888 — /intern/, /trader/, /ops/)

## Step 1 — READ ALL DOCUMENTATION FIRST
Before writing a single line of code:
- Read /home/zeph/paper-trader/AGENTS.md (if exists)
- Read /home/zeph/digital-intern/AGENTS.md (if exists)
- Read unified_dashboard.py
- Read paper_trader/dashboard.py and paper_trader/strategy.py
- Read digital-intern/dashboard/web_server.py
- Read digital-intern/ml/trainer.py

## Step 2 — EXPLORE as a user
Browse the live system: curl the APIs, look at what data is available.

## Step 3 — BRAINSTORM
List at least 10 high-value features or UX improvements. Think like a trader.

## Step 4 — IMPLEMENT the 2-3 highest-impact improvements
Ideas to consider:
- Better signal summarization on the dashboard
- Richer portfolio analytics (sector exposure, drawdown, Sharpe estimate)
- Improved chat context (more articles, portfolio history, P&L trend)
- Alert deduplication or urgency decay
- Backtest comparison view
- DRAM/semis sector heatmap
- Signal confidence intervals
- Auto-suggest trades based on top signals + current positions

## Step 5 — TEST your changes (REQUIRED before committing)
For every change made:
1. Run python3 syntax check on modified files
2. Run import verification
3. If tests/ exists, run the full test suite and ensure ALL tests pass
4. If you added new functionality, write tests for it in tests/
5. DO NOT commit if any tests fail — fix them first

Test commands:
- paper-trader: cd /home/zeph/paper-trader && python3 -m pytest tests/ -v
- digital-intern: cd /home/zeph/digital-intern && python3 -m pytest tests/ -v

## Step 6 — Update docs
Update AGENTS.md with any new features, endpoints, or architecture changes.

## Step 7 — Commit
git add -A && git commit -m "feature: [description]" && git push

Completion: openclaw message send --channel discord --target channel:1496099475838603324 --message "[FEATURE] Agent 4 (feature-dev) done — built: [specific list], tests: [N passed]"
Failure: openclaw message send --channel discord --target channel:1496099475838603324 --message "[FEATURE] Agent 4 (feature-dev) FAILED: [reason]"' \
> "$LOG_DIR/agent4_$TS.log" 2>&1
) &
A4=$!

wait $A1 $A2 $A3 $A4
notify "✅ Hourly review cycle $TS complete — all 4 agents finished"

# Append run log entry
RUN_LOG="/home/zeph/paper-trader/data/run_log.md"
echo "" >> "$RUN_LOG"
echo "## $TS" >> "$RUN_LOG"
echo "- Agents: core, ML+backtests, digital-intern, feature-dev" >> "$RUN_LOG"
echo "- Logs: $LOG_DIR/agent{1,2,3,4}_$TS.log" >> "$RUN_LOG"
