#!/usr/bin/env python3
"""Continuous backtesting loop — v3 ensemble-committee edition.

Each cycle:
  1. Runs 10 fresh parallel backtests (BacktestEngine.run_all). Each run is
     now an ENSEMBLE COMMITTEE of all 10 personas voting per decision;
     diversity across the 10 runs comes from different random seeds (which
     drive GDELT article sampling + selection).
  2. Picks the top winner by total_return_pct.
  3. Appends the winner's decisions to data/winner_training.jsonl tagged
     with the cycle number. (Does NOT overwrite — accumulates forever.)
  4. Attempts ML training from the winner JSONL (best-effort).
  5. Sends a Discord status message.
  6. Trims backtest_runs to the most recent 100 entries.
  7. Sleeps 60 seconds and loops.

SIGTERM/SIGINT exits cleanly between cycles.
"""
from __future__ import annotations

import json
import shutil
import signal
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone

from paper_trader.backtest import (
    BacktestEngine,
    BacktestRun,
    ROOT,
)

RUNS_PER_CYCLE = 5  # reduced from 10 — 5 runs × 3 max-concurrent claude = safe on 14 GB RAM
TOP_RUNS_TO_TRAIN = 3  # aggregate top-N runs per cycle into training data
KEEP_LAST_RUNS = 100
COOLDOWN_SECONDS = 60
DISCORD_CHANNEL = "channel:1496099475838603324"
WINNER_JSONL = ROOT / "data" / "winner_training.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _next_run_id(engine: BacktestEngine) -> int:
    row = engine.store.conn.execute(
        "SELECT COALESCE(MAX(run_id), 0) FROM backtest_runs"
    ).fetchone()
    return int(row[0]) + 1


def _trim_history(engine: BacktestEngine, keep: int = KEEP_LAST_RUNS) -> int:
    conn = engine.store.conn
    with engine.store._lock:
        row = conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()
        total = int(row[0])
        if total <= keep:
            return 0
        cutoff = conn.execute(
            "SELECT run_id FROM backtest_runs "
            "ORDER BY run_id DESC LIMIT 1 OFFSET ?",
            (keep,),
        ).fetchone()
        if cutoff is None:
            return 0
        cutoff_id = int(cutoff[0])
        conn.execute("DELETE FROM backtest_trades WHERE run_id <= ?", (cutoff_id,))
        conn.execute("DELETE FROM backtest_decisions WHERE run_id <= ?", (cutoff_id,))
        cur = conn.execute("DELETE FROM backtest_runs WHERE run_id <= ?", (cutoff_id,))
        conn.commit()
        return cur.rowcount or 0


def _append_top_decisions(engine: BacktestEngine, top_runs: list[BacktestRun],
                          cycle: int) -> int:
    """Aggregate BUY/SELL decisions from top N runs into WINNER_JSONL.

    Records are weighted by each run's return — higher-return runs contribute
    decisions with higher ai_score so the ML trainer up-weights them.
    """
    WINNER_JSONL.parent.mkdir(parents=True, exist_ok=True)
    # Normalise returns to [0.5, 1.0] weight range so even 2nd/3rd place matter
    returns = [r.total_return_pct for r in top_runs]
    max_ret = max(returns) if returns else 1.0
    min_ret = min(returns) if returns else 0.0
    span = max_ret - min_ret or 1.0

    written = 0
    with WINNER_JSONL.open("a") as fh:
        for run in top_runs:
            weight = 0.5 + 0.5 * (run.total_return_pct - min_ret) / span
            try:
                rows = engine.store.conn.execute(
                    "SELECT action, ticker, sim_date, reasoning, qty, confidence "
                    "FROM backtest_decisions "
                    "WHERE run_id = ? AND action IS NOT NULL AND action != 'HOLD'",
                    (run.run_id,),
                ).fetchall()
            except Exception as e:
                print(f"[continuous] run {run.run_id} read failed: {e}")
                continue
            rank = top_runs.index(run) + 1
            for row in rows:
                action = (row["action"] or "").upper()
                if action not in ("BUY", "SELL"):
                    continue
                rec = {
                    "cycle": cycle,
                    "run_id": run.run_id,
                    "rank": rank,
                    "title": f"{action} {row['ticker']} on {row['sim_date']}",
                    "source": f"backtest_cycle_{cycle}_rank{rank}",
                    "ai_score": round(weight * (5.0 if action == "BUY" else 0.5), 2),
                    "urgency": 1 if rank == 1 else 0,
                    "label": action,
                    "ticker": row["ticker"] or "",
                    "sim_date": row["sim_date"] or "",
                    "qty": row["qty"],
                    "confidence": row["confidence"],
                    "reasoning": row["reasoning"] or "",
                    "return_pct": run.total_return_pct,
                    "weight": round(weight, 3),
                }
                fh.write(json.dumps(rec) + "\n")
                written += 1
    print(f"[continuous] appended {written} records from top {len(top_runs)} runs → {WINNER_JSONL}")
    return written


def _opus_annotate(engine: "BacktestEngine", top_runs: list[BacktestRun],
                   cycle: int) -> int:
    """Ask Opus 4.7 to annotate the top run's decisions for training.

    Sends a compact summary of the winning run's trades + returns to Opus and
    asks it to label each decision with a quality score and rationale. The
    annotations are appended to WINNER_JSONL as high-score training records.
    Returns number of annotation records written.
    """
    if not shutil.which("claude"):
        print("[opus_annotate] claude CLI not found — skipping annotation")
        return 0
    if not top_runs:
        return 0

    winner = top_runs[0]
    try:
        rows = engine.store.conn.execute(
            "SELECT action, ticker, sim_date, reasoning, qty, total_value "
            "FROM backtest_decisions WHERE run_id=? ORDER BY sim_date",
            (winner.run_id,),
        ).fetchall()
    except Exception as e:
        print(f"[opus_annotate] DB read failed: {e}")
        return 0

    # Build compact trade log for the prompt
    trade_lines = []
    for r in rows:
        if r["action"] in ("BUY", "SELL"):
            trade_lines.append(
                f"  {r['sim_date']} {r['action']} {r['ticker']} qty={r['qty']} "
                f"portfolio=${r['total_value']:.0f} reasoning={str(r['reasoning'])[:80]}"
            )
    if not trade_lines:
        return 0

    other_returns = " / ".join(f"run{r.run_id}={r.total_return_pct:+.1f}%" for r in top_runs[1:])
    prompt = f"""You are a quantitative trading analyst reviewing a backtest run for ML training purposes.

Backtest run #{winner.run_id} achieved {winner.total_return_pct:+.2f}% return over a 3-month simulation
using ML article sentiment + RSI/MACD/momentum signals (no live Claude calls).
Other top runs this cycle: {other_returns or "none"}

TRADE LOG:
{chr(10).join(trade_lines[:40])}

Your task: For each BUY and SELL trade, provide a quality label (GOOD/NEUTRAL/BAD) and a brief
rationale explaining what made it a good or bad decision based on timing, momentum, and risk.

Then provide an OVERALL LESSON — one concise paragraph summarising what signal patterns drove
outperformance in this run, phrased as a training rule (e.g., "BUY when RSI < 35 AND positive
earnings sentiment; SELL when RSI > 68 AND momentum reversal").

Respond as JSON with this schema:
{{
  "trade_labels": [
    {{"sim_date": "...", "action": "BUY/SELL", "ticker": "...", "quality": "GOOD/NEUTRAL/BAD", "rationale": "..."}}
  ],
  "overall_lesson": "...",
  "key_patterns": ["pattern1", "pattern2"]
}}

JSON only, no markdown fences."""

    try:
        r = subprocess.run(
            ["claude", "--model", "claude-opus-4-7", "--print",
             "--permission-mode", "bypassPermissions"],
            input=prompt, capture_output=True, text=True, timeout=180,
            env={**__import__("os").environ, "HOME": "/home/zeph"},
        )
    except subprocess.TimeoutExpired:
        print("[opus_annotate] timeout")
        return 0
    except Exception as e:
        print(f"[opus_annotate] subprocess error: {e}")
        return 0

    if r.returncode != 0 or not r.stdout.strip():
        print(f"[opus_annotate] claude rc={r.returncode}")
        return 0

    # Parse JSON response
    import re as _re
    raw = r.stdout.strip()
    m = _re.search(r"\{[\s\S]*\}", raw)
    if not m:
        print("[opus_annotate] no JSON in response")
        return 0
    try:
        annotation = json.loads(m.group(0))
    except Exception as e:
        print(f"[opus_annotate] JSON parse error: {e}")
        return 0

    # Write annotation records to training JSONL
    written = 0
    WINNER_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with WINNER_JSONL.open("a") as fh:
        # Overall lesson as a high-value training record
        lesson = annotation.get("overall_lesson", "")
        patterns = annotation.get("key_patterns", [])
        if lesson:
            fh.write(json.dumps({
                "cycle": cycle,
                "run_id": winner.run_id,
                "type": "opus_lesson",
                "title": f"Lesson from run {winner.run_id} (+{winner.total_return_pct:.1f}%): {lesson[:120]}",
                "source": f"opus_annotation_cycle_{cycle}",
                "ai_score": 5.0,
                "urgency": 1,
                "label": "LESSON",
                "return_pct": winner.total_return_pct,
                "reasoning": lesson,
                "key_patterns": patterns,
                "weight": 1.0,
            }) + "\n")
            written += 1

        # Per-trade labels
        quality_score = {"GOOD": 5.0, "NEUTRAL": 2.5, "BAD": 0.5}
        for tl in annotation.get("trade_labels", []):
            q = tl.get("quality", "NEUTRAL")
            fh.write(json.dumps({
                "cycle": cycle,
                "run_id": winner.run_id,
                "type": "opus_trade_label",
                "title": f"{tl.get('action','')} {tl.get('ticker','')} {tl.get('sim_date','')} [{q}]",
                "source": f"opus_annotation_cycle_{cycle}",
                "ai_score": quality_score.get(q, 2.5),
                "urgency": 1 if q == "GOOD" else 0,
                "label": tl.get("action", "HOLD"),
                "ticker": tl.get("ticker", ""),
                "sim_date": tl.get("sim_date", ""),
                "reasoning": tl.get("rationale", ""),
                "return_pct": winner.total_return_pct,
                "quality": q,
                "weight": 1.0 if q == "GOOD" else (0.5 if q == "NEUTRAL" else 0.1),
            }) + "\n")
            written += 1

    print(f"[opus_annotate] wrote {written} annotation records for run {winner.run_id}")
    return written


def _try_train_ml() -> str:
    """Best-effort ML training from winner JSONL. Returns short status string."""
    try:
        r = subprocess.run(
            ["python3", "-m", "ml.trainer", "--from-file", str(WINNER_JSONL)],
            cwd="/home/zeph/digital-intern",
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0:
            return f"trainer ok: {r.stdout.strip()[:120]}"
        return f"trainer rc={r.returncode}"
    except FileNotFoundError:
        return "trainer dir missing"
    except subprocess.TimeoutExpired:
        return "trainer timeout"
    except Exception as e:
        return f"trainer exc: {type(e).__name__}"


def _discord(message: str) -> None:
    if not shutil.which("openclaw"):
        print(f"[discord] (no openclaw) {message}")
        return
    try:
        subprocess.run(
            ["openclaw", "message", "send", "--channel", "discord",
             "--target", DISCORD_CHANNEL, "--message", message],
            capture_output=True, text=True, timeout=60,
        )
        print(f"[discord] sent: {message[:120]}")
    except Exception as e:
        print(f"[discord] failed: {e}")


_STOP = False


def _handle_sig(_signum, _frame) -> None:
    global _STOP
    _STOP = True
    print(f"\n[continuous] {_now()} signal received — stopping after current cycle")


def main() -> None:
    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    print(f"[continuous] {_now()} starting ENSEMBLE-COMMITTEE loop "
          f"({RUNS_PER_CYCLE} runs/cycle, keep last {KEEP_LAST_RUNS}, "
          f"cooldown {COOLDOWN_SECONDS}s)")

    engine = BacktestEngine()
    cycle = 0
    while not _STOP:
        cycle += 1
        start_id = _next_run_id(engine)
        t0 = time.time()
        print(f"\n[continuous] {_now()} ─── cycle {cycle} start "
              f"(run_ids {start_id}..{start_id + RUNS_PER_CYCLE - 1}) ───")

        results: list[BacktestRun] = []
        try:
            results = engine.run_all(RUNS_PER_CYCLE, start_run_id=start_id) or []
        except Exception as e:
            print(f"[continuous] {_now()} cycle {cycle} crashed: {e}")
            traceback.print_exc()

        winner = None
        spy_pct = 0.0
        top_runs: list[BacktestRun] = []
        if results:
            sorted_results = sorted(results, key=lambda r: r.total_return_pct, reverse=True)
            # Only include runs that beat a flat 0% return (filter out pure losers)
            top_runs = [r for r in sorted_results[:TOP_RUNS_TO_TRAIN]
                        if r.total_return_pct > 0]
            if not top_runs:
                top_runs = sorted_results[:1]  # always train on best even if negative
            winner = top_runs[0]
            spy_pct = winner.spy_return_pct
            try:
                _append_top_decisions(engine, top_runs, cycle)
            except Exception as e:
                print(f"[continuous] top-runs append failed: {e}")
            # Opus 4.7 annotation in background thread — don't block next cycle
            import threading as _threading
            _threading.Thread(
                target=_opus_annotate, args=(engine, top_runs, cycle),
                daemon=True, name=f"opus-annotate-{cycle}"
            ).start()

        ml_status = _try_train_ml() if winner else "no winner"
        print(f"[continuous] ml: {ml_status}")

        # Backtest results are silent — check the dashboard at :8090

        try:
            deleted = _trim_history(engine, keep=KEEP_LAST_RUNS)
            if deleted:
                print(f"[continuous] trimmed {deleted} old runs "
                      f"(keeping last {KEEP_LAST_RUNS})")
        except Exception as e:
            print(f"[continuous] trim failed: {e}")

        elapsed = time.time() - t0
        if winner:
            print(f"[continuous] {_now()} cycle {cycle} done in {elapsed/60:.1f}min. "
                  f"Best run {winner.run_id} {winner.total_return_pct:+.2f}%")
        else:
            print(f"[continuous] {_now()} cycle {cycle} done in {elapsed/60:.1f}min")

        if _STOP:
            break

        print(f"[continuous] sleeping {COOLDOWN_SECONDS}s before cycle {cycle + 1}")
        slept = 0
        while slept < COOLDOWN_SECONDS and not _STOP:
            time.sleep(min(2, COOLDOWN_SECONDS - slept))
            slept += 2

    print(f"[continuous] {_now()} loop stopped after {cycle} cycle(s)")
    sys.exit(0)


if __name__ == "__main__":
    main()
