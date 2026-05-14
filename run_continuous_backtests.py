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

RUNS_PER_CYCLE = 10
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


def _append_winner_decisions(engine: BacktestEngine, winner: BacktestRun,
                             cycle: int) -> int:
    """Append the winner's BUY/SELL decisions to WINNER_JSONL tagged with cycle."""
    WINNER_JSONL.parent.mkdir(parents=True, exist_ok=True)
    try:
        rows = engine.store.conn.execute(
            "SELECT action, ticker, sim_date, reasoning, qty, confidence "
            "FROM backtest_decisions "
            "WHERE run_id = ? AND action IS NOT NULL AND action != 'HOLD'",
            (winner.run_id,),
        ).fetchall()
    except Exception as e:
        print(f"[continuous] winner read failed: {e}")
        return 0

    written = 0
    with WINNER_JSONL.open("a") as fh:
        for row in rows:
            action = (row["action"] or "").upper()
            if action not in ("BUY", "SELL"):
                continue
            rec = {
                "cycle": cycle,
                "run_id": winner.run_id,
                "title": f"{action} {row['ticker']} on {row['sim_date']}",
                "source": f"backtest_cycle_{cycle}_run_{winner.run_id}_winner",
                "ai_score": 5.0 if action == "BUY" else 0.0,
                "urgency": 1,
                "label": action,
                "ticker": row["ticker"] or "",
                "sim_date": row["sim_date"] or "",
                "qty": row["qty"],
                "confidence": row["confidence"],
                "reasoning": row["reasoning"] or "",
                "return_pct": winner.total_return_pct,
            }
            fh.write(json.dumps(rec) + "\n")
            written += 1
    print(f"[continuous] appended {written} winner records → {WINNER_JSONL}")
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
        if results:
            winner = max(results, key=lambda r: r.total_return_pct)
            spy_pct = winner.spy_return_pct
            try:
                _append_winner_decisions(engine, winner, cycle)
            except Exception as e:
                print(f"[continuous] winner append failed: {e}")

        ml_status = _try_train_ml() if winner else "no winner"
        print(f"[continuous] ml: {ml_status}")

        if winner:
            msg = (f"Cycle {cycle} done. Best: Run {winner.run_id} "
                   f"{winner.total_return_pct:+.1f}% vs SPY {spy_pct:+.1f}%. "
                   f"ML training triggered ({ml_status}). Next cycle starting...")
        else:
            msg = f"Cycle {cycle} done but produced no results. Next cycle starting..."
        _discord(msg)

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
