"""Pull scored news signals + ML predictions from the digital-intern pipeline."""
import gzip
import json
import os
import re
import sys
import sqlite3
import zlib
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

DIGITAL_INTERN = "/home/zeph/digital-intern"
if DIGITAL_INTERN not in sys.path:
    sys.path.insert(0, DIGITAL_INTERN)

# Discover the article DB the same way digital-intern does.
USB_DB = Path(os.environ.get("DIGITAL_INTERN_USB", "/media/zeph/projects/digital-intern/db")) / "articles.db"
LOCAL_DB = Path(DIGITAL_INTERN) / "data" / "articles.db"


def _db_path() -> Path:
    if USB_DB.exists():
        return USB_DB
    return LOCAL_DB


_TICKER_RE = re.compile(r"\b([A-Z]{1,5})\b")
# common english noise that's all-caps but not tickers
_NOT_TICKERS = {
    "A", "I", "AI", "ALL", "AN", "AND", "ANY", "API", "APR", "AS", "AT",
    "AUG", "BE", "BEA", "BLS", "BOE", "BOJ", "BUT", "BY", "CEO", "CFO",
    "CPI", "CTO", "DEC", "DOJ", "ECB", "EIA", "EPS", "ETF", "ETFS", "EU",
    "FBI", "FDA", "FEB", "FED", "FOMC", "FOR", "FX", "FY", "GDP", "GOP",
    "HOW", "IMF", "IN", "IPO", "IS", "ISM", "IT", "ITS", "JAN", "JULY",
    "JUNE", "MAR", "MAY", "MOM", "NATO", "NEW", "NO", "NOV", "OCT", "OF",
    "OK", "OLD", "ON", "ONE", "OPEC", "OR", "PB", "PBOC", "PCE", "PE", "PM",
    "PMI", "PPI", "Q1", "Q2", "Q3", "Q4", "QE", "QOQ", "QT", "RE", "SEC",
    "SEPT", "SO", "TBA", "THE", "TO", "TWO", "UN", "UP", "US", "USA",
    "USD", "USDA", "VS", "WE", "WHAT", "WHEN", "WHERE", "WHO", "WHY",
    "WTI", "WTO", "YES", "YOY", "ADP",
}


def _extract_tickers(text: str) -> set[str]:
    """Heuristic ticker extraction — pulls $TICKER or ALLCAPS 1-5 char tokens, filters noise."""
    out = set()
    for m in re.finditer(r"\$([A-Z]{1,5})\b", text or ""):
        out.add(m.group(1))
    for m in _TICKER_RE.finditer(text or ""):
        tok = m.group(1)
        if tok in _NOT_TICKERS or len(tok) < 2:
            continue
        out.add(tok)
    return out


def _decompress(blob: bytes | None) -> str:
    if not blob:
        return ""
    try:
        return zlib.decompress(blob).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _connect_ro() -> sqlite3.Connection | None:
    path = _db_path()
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        print(f"[signals] cannot open {path}: {e}")
        return None


def get_top_signals(n: int = 20, hours: int = 2, min_score: float = 4.0) -> list[dict]:
    """Top scored articles from the last N hours with ai_score >= min_score."""
    conn = _connect_ro()
    if not conn:
        return []
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        rows = conn.execute(
            "SELECT id, url, title, source, ai_score, urgency, first_seen, full_text "
            "FROM articles WHERE first_seen >= ? AND ai_score >= ? "
            "AND url NOT LIKE 'backtest://%' AND source NOT LIKE 'backtest_%' "
            "AND source NOT LIKE 'opus_annotation%' "
            "ORDER BY ai_score DESC, first_seen DESC LIMIT ?",
            (since, min_score, n),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        summary = _decompress(r["full_text"])
        out.append({
            "id": r["id"],
            "url": r["url"],
            "title": r["title"],
            "source": r["source"],
            "ai_score": r["ai_score"],
            "urgency": r["urgency"],
            "first_seen": r["first_seen"],
            "summary": summary[:400],
            "tickers": sorted(_extract_tickers(f"{r['title']} {summary}")),
        })
    return out


def get_ticker_sentiment(ticker: str, hours: int = 4) -> dict:
    """Average score + counts of articles mentioning the ticker."""
    conn = _connect_ro()
    if not conn:
        return {"ticker": ticker, "avg_score": 0.0, "max_score": 0.0, "n": 0, "urgent": 0}
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        rows = conn.execute(
            "SELECT title, full_text, ai_score, urgency FROM articles "
            "WHERE first_seen >= ? AND ai_score > 0 "
            "AND url NOT LIKE 'backtest://%' AND source NOT LIKE 'backtest_%' "
            "AND source NOT LIKE 'opus_annotation%'",
            (since,),
        ).fetchall()
    finally:
        conn.close()
    scores = []
    urgent = 0
    needle = ticker.upper()
    pattern = re.compile(rf"(?:\$|\b){re.escape(needle)}\b")
    for r in rows:
        body = f"{r['title']} {_decompress(r['full_text'])}".upper()
        if pattern.search(body):
            scores.append(r["ai_score"])
            if (r["urgency"] or 0) >= 1:
                urgent += 1
    if not scores:
        return {"ticker": ticker, "avg_score": 0.0, "max_score": 0.0, "n": 0, "urgent": urgent}
    return {
        "ticker": ticker,
        "avg_score": round(sum(scores) / len(scores), 2),
        "max_score": max(scores),
        "n": len(scores),
        "urgent": urgent,
    }


def get_urgent_articles(minutes: int = 30) -> list[dict]:
    """Articles flagged urgent (>=1) in the last N minutes."""
    conn = _connect_ro()
    if not conn:
        return []
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    try:
        rows = conn.execute(
            "SELECT id, title, source, ai_score, urgency, first_seen, full_text "
            "FROM articles WHERE urgency >= 1 AND first_seen >= ? "
            "AND url NOT LIKE 'backtest://%' AND source NOT LIKE 'backtest_%' "
            "AND source NOT LIKE 'opus_annotation%' "
            "ORDER BY ai_score DESC LIMIT 20",
            (since,),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        summary = _decompress(r["full_text"])
        out.append({
            "id": r["id"],
            "title": r["title"],
            "source": r["source"],
            # urgent rows are not score-filtered, so ai_score may be NULL —
            # coerce to 0.0 so downstream `f"{ai_score:.1f}"` formatting is safe.
            "ai_score": r["ai_score"] or 0.0,
            "urgency": r["urgency"],
            "first_seen": r["first_seen"],
            "summary": summary[:300],
            "tickers": sorted(_extract_tickers(f"{r['title']} {summary}")),
        })
    return out


def get_ml_predictions(articles: list[dict] | None = None) -> list[dict]:
    """Run digital-intern ML scoring against a candidate list of articles.

    If `articles` is omitted, scores the most recent unscored-or-low-score batch.
    Safe to return [] on failure — caller continues with rule-based signals.
    """
    try:
        from ml.inference import score_articles  # type: ignore
    except Exception as e:
        print(f"[signals] ML unavailable: {e}")
        return []

    if articles is None:
        articles = get_top_signals(30, hours=6, min_score=0.0)
    if not articles:
        return []

    try:
        scores = score_articles(articles)
    except Exception as e:
        print(f"[signals] ML inference failed: {e}")
        return []

    out = []
    for a, s in zip(articles, scores):
        out.append({
            "id": a.get("id"),
            "title": a.get("title"),
            "tickers": a.get("tickers", []),
            "relevance": s.relevance,
            "urgency": s.urgency,
            "rel_std": s.rel_std,
            "urg_std": s.urg_std,
            "needs_llm": s.needs_llm,
        })
    return out


def ticker_sentiments(tickers: list[str], hours: int = 4) -> list[dict]:
    """Bulk wrapper — one scan, scores aggregated per ticker."""
    conn = _connect_ro()
    if not conn:
        return [{"ticker": t, "avg_score": 0.0, "max_score": 0.0, "n": 0, "urgent": 0} for t in tickers]
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        rows = conn.execute(
            "SELECT title, full_text, ai_score, urgency FROM articles "
            "WHERE first_seen >= ? AND ai_score > 0 "
            "AND url NOT LIKE 'backtest://%' AND source NOT LIKE 'backtest_%' "
            "AND source NOT LIKE 'opus_annotation%'",
            (since,),
        ).fetchall()
    finally:
        conn.close()
    agg = defaultdict(lambda: {"scores": [], "urgent": 0})
    upper_tickers = [t.upper() for t in tickers]
    patterns = {t: re.compile(rf"(?:\$|\b){re.escape(t)}\b") for t in upper_tickers}
    for r in rows:
        body = f"{r['title']} {_decompress(r['full_text'])}".upper()
        urg = (r["urgency"] or 0) >= 1
        for t, pat in patterns.items():
            if pat.search(body):
                agg[t]["scores"].append(r["ai_score"])
                if urg:
                    agg[t]["urgent"] += 1
    out = []
    for t in upper_tickers:
        sc = agg[t]["scores"]
        out.append({
            "ticker": t,
            "avg_score": round(sum(sc) / len(sc), 2) if sc else 0.0,
            "max_score": max(sc) if sc else 0.0,
            "n": len(sc),
            "urgent": agg[t]["urgent"],
        })
    return out


HISTORICAL_GZ = Path(
    os.environ.get(
        "DIGITAL_INTERN_HISTORICAL",
        "/media/zeph/projects/digital-intern/db/training_data.json.gz",
    )
)


def get_historical_signals(min_score: float = 4.0, limit: int | None = None) -> list[dict]:
    """Backtest-friendly fallback: read the gzip training-data export.

    Returns up to ``limit`` records with ``ai_score >= min_score`` (or all if
    ``limit`` is None). Returns [] and prints a short note if the file is missing.
    """
    if not HISTORICAL_GZ.exists():
        print(f"[signals] historical gzip missing at {HISTORICAL_GZ}")
        return []
    out: list[dict] = []
    try:
        with gzip.open(HISTORICAL_GZ, "rt", encoding="utf-8") as gz:
            for line in gz:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                try:
                    score = rec.get("score") or rec.get("ai_score")
                    if score is None or float(score) < min_score:
                        continue
                except (TypeError, ValueError):
                    # Non-numeric / corrupt score field — skip this record but
                    # keep reading the rest of the file.
                    continue
                out.append(rec)
                if limit is not None and len(out) >= limit:
                    break
    except Exception as e:
        print(f"[signals] historical read error: {e}")
        return []
    return out


if __name__ == "__main__":
    print("=== top signals ===")
    for s in get_top_signals(5):
        print(f"  [{s['ai_score']:.1f}] {s['title']!r:60} tickers={s['tickers']}")
    print("\n=== urgent ===")
    for s in get_urgent_articles():
        print(f"  [{s['urgency']}] {s['title']!r}")
    print("\n=== ticker sentiments ===")
    for r in ticker_sentiments(["NVDA", "MU", "AMD", "LITE"]):
        print(f"  {r}")
