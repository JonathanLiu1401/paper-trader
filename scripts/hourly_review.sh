#!/usr/bin/env bash
# Hourly parallel Opus 4.7 pass — 4 agents in parallel.
# Agents 1-3: systematic code review + bug fixes.
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
'Systematic code review and bug-fix pass on /home/zeph/paper-trader core.

Read each file in full. Find and fix ALL bugs, logic errors, race conditions, missing error handling, dead code, and quality issues. Be surgical — fix only what is broken. No padding or over-engineering.

Files (read fully before touching):
- paper_trader/runner.py
- paper_trader/reporter.py
- paper_trader/signals.py
- paper_trader/strategy.py
- paper_trader/dashboard.py

Process:
1. Read entire file
2. List every issue found
3. Fix them directly in the file
4. Verify no import errors with: python3 -c "import sys; sys.path.insert(0,\".\"); from paper_trader import signals, reporter; print(\"OK\")"

Completion: openclaw message send --channel discord --target channel:1496099475838603324 --message "[REVIEW] Agent 1 (paper-trader core) done — fixed: [concise list of issues fixed, or \"no issues found\"]"
Failure: openclaw message send --channel discord --target channel:1496099475838603324 --message "[REVIEW] Agent 1 (paper-trader core) FAILED: [reason]"' \
> "$LOG_DIR/agent1_$TS.log" 2>&1
) &
A1=$!

# ── Agent 2: paper-trader ML + backtests ─────────────────────────────────────
(
cd /home/zeph/paper-trader
claude --model claude-opus-4-7 --permission-mode bypassPermissions --print \
'Systematic code review and bug-fix pass on /home/zeph/paper-trader ML and backtest files.

Read each file in full. Find and fix ALL bugs, logic errors, race conditions, missing error handling, dead code, and quality issues. Be surgical.

Files (read fully before touching):
- paper_trader/ml/decision_scorer.py
- paper_trader/backtest.py
- run_continuous_backtests.py

Process:
1. Read entire file
2. List every issue found
3. Fix them directly

Completion: openclaw message send --channel discord --target channel:1496099475838603324 --message "[REVIEW] Agent 2 (ML+backtests) done — fixed: [concise list of issues fixed, or \"no issues found\"]"
Failure: openclaw message send --channel discord --target channel:1496099475838603324 --message "[REVIEW] Agent 2 (ML+backtests) FAILED: [reason]"' \
> "$LOG_DIR/agent2_$TS.log" 2>&1
) &
A2=$!

# ── Agent 3: digital-intern full codebase ────────────────────────────────────
(
cd /home/zeph/digital-intern
claude --model claude-opus-4-7 --permission-mode bypassPermissions --print \
'Systematic code review and bug-fix pass on /home/zeph/digital-intern.

Read each file in full. Find and fix ALL bugs, logic errors, race conditions, missing error handling, dead code, and quality issues. Be surgical.

Files/areas to cover:
- daemon.py
- heartbeat.sh
- storage/article_store.py (verify get_unalerted_urgent filters backtest:// correctly)
- watchers/alert_agent.py
- watchers/urgency_scorer.py
- ml/trainer.py
- ml/model.py
- ml/features.py
- collectors/web_scraper.py
- analysis/claude_analyst.py

Process:
1. Read entire file
2. List every issue found
3. Fix them directly

Completion: openclaw message send --channel discord --target channel:1496099475838603324 --message "[REVIEW] Agent 3 (digital-intern) done — fixed: [concise list of issues fixed, or \"no issues found\"]"
Failure: openclaw message send --channel discord --target channel:1496099475838603324 --message "[REVIEW] Agent 3 (digital-intern) FAILED: [reason]"' \
> "$LOG_DIR/agent3_$TS.log" 2>&1
) &
A3=$!

# ── Agent 4: feature development + user-perspective brainstorming ────────────
(
cd /home/zeph
claude --model claude-opus-4-7 --permission-mode bypassPermissions --print \
'You are a senior product engineer and power user taking full ownership of this trading stack. Your job is creative feature development and user-perspective testing — NOT just bug fixing.

Repos:
- /home/zeph/paper-trader   (paper trading engine, ML scorer, backtests, Flask dashboard :8090)
- /home/zeph/digital-intern (news collector, AI scorer, Bloomberg alerts, chat API :8080)
- /home/zeph/unified_dashboard.py (reverse proxy :8888 — /intern/, /trader/, /chat)

Step 1 — EXPLORE as a user. Browse the codebase as if you just inherited it:
- Read the dashboards (web_server.py, dashboard.py, unified_dashboard.py)
- Read the strategy, signals, reporter, and scanner logic
- Read the ML scoring and backtest pipeline
- Look at the chat API, portfolio P&L display, alert system

Step 2 — BRAINSTORM. List at least 10 high-value features or UX improvements you would want as a user of this system. Think like a trader: What data am I missing? What decisions is this system not helping me make? What would save me time or give me an edge?

Step 3 — IMPLEMENT the 2-3 highest-impact improvements from your list. Be creative and ambitious. Ideas to consider (but not limited to):
- Better signal summarization on the dashboard
- Richer portfolio analytics (sector exposure, drawdown, Sharpe estimate)
- Improved chat context (more articles, portfolio history, P&L trend)
- Alert deduplication or urgency decay
- Backtest comparison view
- DRAM/semis sector heatmap on the dashboard
- Signal confidence intervals alongside scores
- Auto-suggest trades based on top signals + current positions

Step 4 — TEST your changes. Run python3 syntax checks. Verify imports. If you can, hit the API with curl to confirm new endpoints work.

Step 5 — Report what you built. Be specific.

Completion: openclaw message send --channel discord --target channel:1496099475838603324 --message "[FEATURE] Agent 4 (feature-dev) done — built: [specific list of features implemented]"
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
