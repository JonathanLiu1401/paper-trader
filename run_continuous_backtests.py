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
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone

from paper_trader.backtest import (
    BacktestEngine,
    BacktestRun,
    ROOT,
    _get_quant_signals,
    _market_regime,
)

RUNS_PER_CYCLE = 5  # reduced from 10 — 5 runs × 3 max-concurrent claude = safe on 14 GB RAM
TOP_RUNS_TO_TRAIN = 3  # aggregate top-N runs per cycle into training data
KEEP_LAST_RUNS = 500
MAX_OUTCOMES_FOR_TRAINING = 5000  # cap decision_outcomes.jsonl tail used per retrain
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


def _compute_decision_outcomes(engine: "BacktestEngine",
                               top_runs: list["BacktestRun"]) -> list[dict]:
    """Compute actual 5-day forward returns for BUY/SELL decisions in top_runs.

    Re-uses PriceCache for returns and _get_quant_signals for features so no
    network calls are needed. Returns a list of outcome records ready to pass
    to train_scorer().
    """
    outcomes: list[dict] = []
    _quant_cache: dict[tuple, dict] = {}

    # Decisions whose 5-day forward window extends past the last price-cache day
    # have no real outcome — price_on falls back to the latest available close,
    # which is the same close used at sim_date, giving a spurious 0% return.
    # Skip those to avoid polluting training data with fake neutrals.
    last_data_day = engine.prices.trading_days[-1] if engine.prices.trading_days else None

    for run in top_runs:
        try:
            rows = engine.store.conn.execute(
                "SELECT action, ticker, sim_date, reasoning "
                "FROM backtest_decisions "
                "WHERE run_id=? AND action IN ('BUY','SELL') "
                "AND ticker IS NOT NULL AND ticker != ''",
                (run.run_id,),
            ).fetchall()
        except Exception as exc:
            print(f"[outcomes] run {run.run_id} read failed: {exc}")
            continue

        for r in rows:
            ticker = r["ticker"] or ""
            sim_date_str = r["sim_date"] or ""
            if not ticker or not sim_date_str:
                continue
            try:
                sim_d = date.fromisoformat(sim_date_str)
            except ValueError:
                continue

            end_d = sim_d + timedelta(days=7)  # ~5 trading days
            if last_data_day is not None and end_d > last_data_day:
                continue
            # returns_pct silently returns 0.0 when either price lookup misses,
            # which would inject a fake neutral outcome into training data for
            # tickers without coverage in this window. Skip those records.
            if (engine.prices.price_on(ticker, sim_d) is None
                    or engine.prices.price_on(ticker, end_d) is None):
                continue
            fwd_ret = engine.prices.returns_pct(ticker, sim_d, end_d)

            cache_key = (sim_date_str, ticker)
            if cache_key not in _quant_cache:
                sigs = _get_quant_signals(sim_d, [ticker], engine.prices)
                _quant_cache[cache_key] = sigs.get(ticker, {})
            q = _quant_cache[cache_key]

            regime = _market_regime(sim_d, engine.prices)
            # Match _ml_decide: "unknown" is treated as neutral 1.0, not bear.
            if regime == "bull":
                regime_mult = 1.0
            elif regime == "sideways":
                regime_mult = 0.6
            elif regime == "bear":
                regime_mult = 0.3
            else:
                regime_mult = 1.0

            ml_score = 0.0
            m = re.search(r"score=([0-9.+-]+)", r["reasoning"] or "")
            if m:
                try:
                    ml_score = float(m.group(1))
                except ValueError:
                    pass

            outcomes.append({
                "run_id": run.run_id,
                "sim_date": sim_date_str,
                "ticker": ticker,
                "action": r["action"],
                "ml_score": ml_score,
                # Use only numeric quant fields; the legacy uppercase "MACD"
                # is a string label and would corrupt scorer features if it
                # leaked through via `or`-fallback when macd_signal==0.0.
                "rsi": q.get("rsi"),
                "macd": q.get("macd_signal"),
                "mom5": q.get("mom_5d"),
                "mom20": q.get("mom_20d"),
                "regime_mult": regime_mult,
                "vol_ratio": q.get("vol_ratio"),
                "bb_position": q.get("bb_position"),
                "forward_return_5d": round(fwd_ret, 4),
                "return_pct": run.total_return_pct,
            })

    return outcomes


def _train_decision_scorer(outcome_records: list[dict]) -> str:
    """Train DecisionScorer on outcome records. Returns a short status string."""
    if not outcome_records:
        return "no outcome records"
    try:
        from paper_trader.ml.decision_scorer import train_scorer
        result = train_scorer(outcome_records)
        rmse = result.get("val_rmse", float("nan"))
        rmse_s = f"{rmse:.2f}" if rmse == rmse else "n/a"
        return f"scorer {result['status']} n={result['n']} rmse={rmse_s}"
    except Exception as exc:
        return f"scorer err: {exc}"


def _query_news_context(ticker: str, sim_date_str: str, n: int = 4) -> list[str]:
    """Fetch recent article titles from digital-intern DB near sim_date for ticker."""
    DB = ROOT.parent / "digital-intern" / "data" / "articles.db"
    if not DB.exists():
        return []
    try:
        d = date.fromisoformat(sim_date_str)
    except ValueError:
        return []
    lo = (d - timedelta(days=3)).isoformat()
    hi = (d + timedelta(days=1)).isoformat()
    conn = None
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        rows = conn.execute(
            "SELECT title FROM articles "
            "WHERE (title LIKE ? OR title LIKE ?) "
            "AND published BETWEEN ? AND ? "
            "AND (url IS NULL OR url NOT LIKE 'backtest://%') "
            "AND (source IS NULL OR (source NOT LIKE 'backtest_%' "
            "AND source NOT LIKE 'opus_annotation%')) "
            "ORDER BY ai_score DESC LIMIT ?",
            (f"%{ticker}%", f"%{ticker.lower()}%", lo, hi, n),
        ).fetchall()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _opus_annotate(engine: "BacktestEngine", top_runs: list[BacktestRun],
                   cycle: int, outcome_records: list[dict] | None = None) -> int:
    """Ask Opus 4.7 to annotate ALL decisions (BUY, SELL, HOLD) in the winner run.

    Enhanced over previous version:
    - Covers every decision, not just trades, so HOLDs can also be critiqued
    - Attaches actual 5-day forward returns so Opus sees what happened after each call
    - Pulls relevant scraped news articles from articles DB near each decision date
    - Outcome records (from _compute_decision_outcomes) included as context when available

    Annotations are appended to WINNER_JSONL. Returns number of records written.
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

    # Build outcome lookup: (sim_date, ticker) -> forward_return_5d
    outcome_lookup: dict[tuple, float] = {}
    for o in (outcome_records or []):
        if o.get("run_id") == winner.run_id:
            outcome_lookup[(o["sim_date"], o["ticker"])] = o["forward_return_5d"]

    # Build enriched decision log — all actions, not just BUY/SELL
    decision_lines = []
    for r in rows:
        action = r["action"] or "HOLD"
        ticker = r["ticker"] or ""
        sim_date_str = r["sim_date"] or ""
        fwd_str = ""
        if ticker and sim_date_str:
            fwd = outcome_lookup.get((sim_date_str, ticker))
            if fwd is not None:
                fwd_str = f" →5d={fwd:+.1f}%"
            # Fetch scraped news snippets for this ticker/date
            news = _query_news_context(ticker, sim_date_str, n=2)
            news_str = " | NEWS: " + "; ".join(news[:2]) if news else ""
        else:
            news_str = ""
        qty_str = f" qty={r['qty']}" if r["qty"] else ""
        val_str = f" portfolio=${r['total_value']:.0f}" if r["total_value"] else ""
        reasoning_short = str(r["reasoning"] or "")[:100]
        decision_lines.append(
            f"  {sim_date_str} {action} {ticker}{qty_str}{val_str}{fwd_str}"
            f" | {reasoning_short}{news_str}"
        )

    if not decision_lines:
        return 0

    other_returns = " / ".join(f"run{r.run_id}={r.total_return_pct:+.1f}%" for r in top_runs[1:])
    prompt = f"""You are a quantitative trading analyst reviewing a backtest run for ML training purposes.

Backtest run #{winner.run_id} achieved {winner.total_return_pct:+.2f}% return over a 1-year simulation
using ML article sentiment + RSI/MACD/momentum signals. No live Claude calls were used — decisions
are pure quantitative signals. Other top runs this cycle: {other_returns or "none"}

FULL DECISION LOG (including HOLDs):
Format: date ACTION TICKER qty portfolio →5d_actual_return | reasoning | NEWS_CONTEXT
{chr(10).join(decision_lines[:60])}

Your task:
1. For EVERY decision (BUY, SELL, and HOLD), assign quality: GOOD / NEUTRAL / BAD
   - GOOD: the decision led to profit or correctly avoided loss (5d return confirms it)
   - BAD: the decision lost money or missed a clear profitable opportunity
   - NEUTRAL: outcome was mixed or the 5d return was near zero
   - For HOLDs: was holding the right call? Did a missed trade (5d return > +2%) mean BAD HOLD?
2. For BAD decisions: specify what signal should have triggered differently
3. For GOOD decisions: identify the specific signal that made it right
4. Provide an OVERALL LESSON as a concise trading rule derived from this run's outcomes

Respond as JSON with this schema (no markdown fences):
{{
  "trade_labels": [
    {{
      "sim_date": "YYYY-MM-DD",
      "action": "BUY/SELL/HOLD",
      "ticker": "...",
      "quality": "GOOD/NEUTRAL/BAD",
      "rationale": "...",
      "forward_return_5d": <number or null>,
      "signal_fix": "what signal should have changed this decision (if BAD or missed opportunity)"
    }}
  ],
  "overall_lesson": "...",
  "key_patterns": ["pattern1", "pattern2"],
  "improvement_suggestions": ["specific change to ML scoring or thresholds"]
}}"""

    try:
        r = subprocess.run(
            ["claude", "--model", "claude-opus-4-7", "--print",
             "--permission-mode", "bypassPermissions"],
            input=prompt, capture_output=True, text=True, timeout=240,
            env={**os.environ, "HOME": "/home/zeph"},
        )
    except subprocess.TimeoutExpired:
        print("[opus_annotate] timeout")
        return 0
    except Exception as e:
        print(f"[opus_annotate] subprocess error: {e}")
        return 0

    if r.returncode != 0 or not r.stdout.strip():
        print(f"[opus_annotate] claude rc={r.returncode} stderr={r.stderr.strip()[:200]!r}")
        return 0

    raw = r.stdout.strip()
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        print("[opus_annotate] no JSON in response")
        return 0
    try:
        annotation = json.loads(m.group(0))
    except Exception as e:
        print(f"[opus_annotate] JSON parse error: {e}")
        return 0

    written = 0
    WINNER_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with WINNER_JSONL.open("a") as fh:
        # Opus occasionally emits JSON null for list/string fields — dict.get
        # returns None in that case, so use `or` to fall back to safe defaults.
        lesson = annotation.get("overall_lesson") or ""
        patterns = annotation.get("key_patterns") or []
        suggestions = annotation.get("improvement_suggestions") or []
        if lesson:
            fh.write(json.dumps({
                "cycle": cycle,
                "run_id": winner.run_id,
                "type": "opus_lesson",
                "title": f"Lesson run {winner.run_id} ({winner.total_return_pct:+.1f}%): {lesson[:120]}",
                "source": f"opus_annotation_cycle_{cycle}",
                "ai_score": 5.0,
                "urgency": 1,
                "label": "LESSON",
                "return_pct": winner.total_return_pct,
                "reasoning": lesson,
                "key_patterns": patterns,
                "improvement_suggestions": suggestions,
                "weight": 1.0,
            }) + "\n")
            written += 1

        quality_score = {"GOOD": 5.0, "NEUTRAL": 2.5, "BAD": 0.5}
        for tl in (annotation.get("trade_labels") or []):
            q = tl.get("quality", "NEUTRAL")
            action = tl.get("action", "HOLD")
            fh.write(json.dumps({
                "cycle": cycle,
                "run_id": winner.run_id,
                "type": "opus_trade_label",
                "title": f"{action} {tl.get('ticker','')} {tl.get('sim_date','')} [{q}]",
                "source": f"opus_annotation_cycle_{cycle}",
                "ai_score": quality_score.get(q, 2.5),
                "urgency": 1 if q == "GOOD" else 0,
                "label": action,
                "ticker": tl.get("ticker", ""),
                "sim_date": tl.get("sim_date", ""),
                "reasoning": tl.get("rationale", ""),
                "signal_fix": tl.get("signal_fix", ""),
                "forward_return_5d": tl.get("forward_return_5d"),
                "return_pct": winner.total_return_pct,
                "quality": q,
                "weight": 1.0 if q == "GOOD" else (0.5 if q == "NEUTRAL" else 0.1),
            }) + "\n")
            written += 1

    print(f"[opus_annotate] wrote {written} annotation records for run {winner.run_id} "
          f"({len(decision_lines)} decisions reviewed)")
    return written


def _inject_and_train() -> str:
    """Inject winner JSONL into article store then retrain. Returns short status string."""
    import hashlib
    import zlib

    DB_PATH = "/home/zeph/digital-intern/data/articles.db"

    def _compress(text: str) -> bytes:
        return zlib.compress(text.encode("utf-8", errors="replace"), level=6)

    def _aid(url: str, title: str) -> str:
        return hashlib.sha256(f"{url}||{title}".encode()).hexdigest()[:20]

    if not WINNER_JSONL.exists():
        return "no jsonl"

    # Cap the JSONL read to the most recent records — older ones are already
    # in articles.db (INSERT OR IGNORE de-dups by id), so re-reading them every
    # cycle wastes memory and IO as the file grows without bound.
    _MAX_INJECT_RECORDS = 10000
    try:
        lines = WINNER_JSONL.read_text().splitlines()
        recent = [l for l in lines if l.strip()][-_MAX_INJECT_RECORDS:]
    except Exception as e:
        return f"jsonl read err: {e}"
    # Per-line parse so a single corrupt line doesn't drop the whole batch
    records: list[dict] = []
    for l in recent:
        try:
            records.append(json.loads(l))
        except Exception:
            pass

    now = datetime.now(timezone.utc).isoformat()
    aconn = None
    try:
        aconn = sqlite3.connect(DB_PATH, timeout=15)
        inserted = 0
        for rec in records:
            ai = float(rec.get("ai_score", 0))
            w = float(rec.get("weight", 1.0))
            eff = min(10.0, ai * w)
            title = rec.get("title", "")
            ticker = rec.get("ticker", "")
            reasoning = rec.get("reasoning", "")
            sim_date = rec.get("sim_date", "")
            label = rec.get("label", "")
            run_id = rec.get("run_id", 0)
            if not title:
                continue
            url = f"backtest://run_{run_id}/{sim_date}/{label}/{ticker}"
            aid = _aid(url, title)
            full_text = f"[{ticker}] {title}. {reasoning}"
            aconn.execute(
                "INSERT OR IGNORE INTO articles "
                "(id,url,title,source,published,kw_score,ai_score,urgency,first_seen,cycle,full_text) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (aid, url, title, f"backtest_run_{run_id}", sim_date or now[:10],
                 eff, eff, 0, now, rec.get("cycle", 0),
                 _compress(full_text)),
            )
            if aconn.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
        aconn.commit()
    except Exception as e:
        return f"inject err: {e}"
    finally:
        if aconn is not None:
            try:
                aconn.close()
            except Exception:
                pass

    # Now trigger actual training
    try:
        r = subprocess.run(
            ["python3", "-c",
             "import sys; sys.path.insert(0,'.'); "
             "from storage.article_store import ArticleStore; "
             "from ml.trainer import train; "
             "s=ArticleStore(); res=train(s,force=True); "
             "print(f\"trainer n={res.get('n',0)} loss={res.get('final_loss',0):.4f} "
             "val={res.get('val_loss',0):.4f}\")"],
            cwd="/home/zeph/digital-intern",
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            out = (r.stdout or "").strip().splitlines()
            return f"injected {inserted} new | {out[-1] if out else 'ok'}"
        return f"trainer rc={r.returncode} injected={inserted}"
    except subprocess.TimeoutExpired:
        return f"trainer timeout (injected {inserted})"
    except Exception as e:
        return f"trainer exc: {type(e).__name__}"


def _try_train_ml() -> str:
    return _inject_and_train()


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

            # Compute 5d forward return outcomes for every BUY/SELL decision
            # across ALL runs (winners and losers) so the scorer learns from
            # losing decisions too — training only on top runs caused survivorship
            # bias and an overly optimistic model.
            outcome_records: list[dict] = []
            try:
                outcome_records = _compute_decision_outcomes(engine, sorted_results)
                print(f"[continuous] computed {len(outcome_records)} decision outcomes "
                      f"from {len(sorted_results)} runs")
            except Exception as e:
                print(f"[continuous] outcome compute failed: {e}")

            # Train DecisionScorer on accumulated outcomes (accumulate across cycles)
            _all_outcomes_path = ROOT / "data" / "decision_outcomes.jsonl"
            if outcome_records:
                try:
                    _all_outcomes_path.parent.mkdir(parents=True, exist_ok=True)
                    with _all_outcomes_path.open("a") as _of:
                        for _o in outcome_records:
                            _of.write(json.dumps(_o) + "\n")
                except Exception as e:
                    print(f"[continuous] outcome append failed: {e}")

            # Load most recent outcomes and retrain scorer.
            # Capped at MAX_OUTCOMES_FOR_TRAINING — older outcomes describe a stale
            # signal regime and the file would otherwise grow unbounded.
            try:
                all_lines: list[str] = []
                if _all_outcomes_path.exists():
                    all_lines = [l for l in _all_outcomes_path.read_text().splitlines() if l.strip()]
                # Trim the file on disk when it grows past 2× the training cap so
                # it doesn't accumulate indefinitely across cycles. The model only
                # ever sees the tail anyway.
                if len(all_lines) > MAX_OUTCOMES_FOR_TRAINING * 2:
                    kept = all_lines[-MAX_OUTCOMES_FOR_TRAINING:]
                    _all_outcomes_path.write_text("\n".join(kept) + "\n")
                    print(f"[continuous] trimmed outcomes file "
                          f"{len(all_lines)} → {len(kept)} lines")
                    all_lines = kept
                all_outcomes: list[dict] = []
                for _line in all_lines:
                    try:
                        all_outcomes.append(json.loads(_line))
                    except Exception:
                        pass
                all_outcomes = all_outcomes[-MAX_OUTCOMES_FOR_TRAINING:]
                scorer_status = _train_decision_scorer(all_outcomes)
                print(f"[continuous] {scorer_status}")
                # Reset the singleton under its lock so next cycle reloads the
                # freshly-trained scorer. Bare assignment races with any backtest
                # thread mid-call to _get_decision_scorer().
                import paper_trader.backtest as _bt
                with _bt._DECISION_SCORER_LOCK:
                    _bt._DECISION_SCORER = None
            except Exception as e:
                print(f"[continuous] scorer train failed: {e}")

            # Opus 4.7 annotation in background thread — don't block next cycle
            import threading as _threading
            _threading.Thread(
                target=_opus_annotate, args=(engine, top_runs, cycle, outcome_records),
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
            chunk = min(2, COOLDOWN_SECONDS - slept)
            time.sleep(chunk)
            slept += chunk

    print(f"[continuous] {_now()} loop stopped after {cycle} cycle(s)")
    sys.exit(0)


if __name__ == "__main__":
    main()
