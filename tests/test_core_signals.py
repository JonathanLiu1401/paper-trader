"""Tests for paper_trader.signals — articles.db queries and ticker extraction.

These tests use a temp SQLite DB that mirrors digital-intern's schema so we
can drive the queries deterministically without touching the real DB. The
backtest-filter clause is exercised directly: a backtest:// row must NOT be
returned, and a synthetic source row must NOT be returned.
"""
from __future__ import annotations

import sqlite3
import sys
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from paper_trader import signals


def _build_articles_db(path: Path, rows: list[dict]) -> None:
    """Create an articles.db with just the columns paper_trader/signals.py uses."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY,
            url TEXT,
            title TEXT,
            source TEXT,
            ai_score REAL,
            urgency REAL,
            first_seen TEXT,
            full_text BLOB
        )
        """
    )
    for r in rows:
        conn.execute(
            "INSERT INTO articles (id, url, title, source, ai_score, urgency, first_seen, full_text) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                r.get("id"),
                r.get("url"),
                r.get("title"),
                r.get("source"),
                r.get("ai_score"),
                r.get("urgency"),
                r.get("first_seen"),
                zlib.compress(r.get("body", "").encode("utf-8")) if r.get("body") else None,
            ),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def fake_articles_db(tmp_path, monkeypatch):
    db = tmp_path / "articles.db"
    # Override the path discovery so signals._db_path() returns our temp file.
    monkeypatch.setattr(signals, "USB_DB", Path("/nonexistent/articles.db"))
    monkeypatch.setattr(signals, "LOCAL_DB", db)
    return db


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hours_ago(h: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()


class TestExtractTickers:
    def test_dollar_prefixed_ticker_extracted(self):
        assert "NVDA" in signals._extract_tickers("Big move in $NVDA today")

    def test_plain_allcaps_extracted(self):
        assert "AMD" in signals._extract_tickers("AMD beats earnings")

    def test_common_acronyms_filtered_out(self):
        # The whole text is acronyms; the result should contain no tickers.
        out = signals._extract_tickers("FOMC PCE CPI Q1 GDP say AND THE FED")
        assert out == set()

    def test_single_letter_filtered(self):
        # Single letters are below the length floor (2 chars min).
        assert "A" not in signals._extract_tickers("A and I went to lunch")

    def test_mixed_tickers_and_noise(self):
        out = signals._extract_tickers("NVDA and AMD beat Q1 estimates, said the FED")
        assert "NVDA" in out
        assert "AMD" in out
        assert "Q1" not in out
        assert "FED" not in out

    def test_empty_string_returns_empty(self):
        assert signals._extract_tickers("") == set()
        assert signals._extract_tickers(None) == set()


class TestDecompress:
    def test_roundtrip(self):
        blob = zlib.compress(b"hello world")
        assert signals._decompress(blob) == "hello world"

    def test_empty_blob_returns_empty(self):
        assert signals._decompress(b"") == ""
        assert signals._decompress(None) == ""

    def test_corrupt_blob_returns_empty(self):
        assert signals._decompress(b"not-zlib-data") == ""


class TestGetTopSignals:
    def test_empty_db_returns_empty(self, fake_articles_db):
        _build_articles_db(fake_articles_db, [])
        assert signals.get_top_signals(n=10) == []

    def test_missing_db_returns_empty(self, monkeypatch, tmp_path):
        # Point both candidate paths at nonexistent files.
        monkeypatch.setattr(signals, "USB_DB", tmp_path / "nope.db")
        monkeypatch.setattr(signals, "LOCAL_DB", tmp_path / "nope2.db")
        assert signals.get_top_signals(n=10) == []

    def test_min_score_threshold_filters_below(self, fake_articles_db):
        _build_articles_db(fake_articles_db, [
            {"id": 1, "url": "http://a", "title": "low", "source": "x",
             "ai_score": 2.0, "urgency": 0, "first_seen": _now_iso(), "body": "low signal"},
            {"id": 2, "url": "http://b", "title": "high", "source": "x",
             "ai_score": 8.0, "urgency": 0, "first_seen": _now_iso(), "body": "high signal NVDA"},
        ])
        rows = signals.get_top_signals(n=10, hours=24, min_score=4.0)
        assert len(rows) == 1
        assert rows[0]["title"] == "high"

    def test_score_descending_order(self, fake_articles_db):
        _build_articles_db(fake_articles_db, [
            {"id": 1, "url": "http://a", "title": "mid", "source": "x",
             "ai_score": 5.0, "urgency": 0, "first_seen": _now_iso(), "body": ""},
            {"id": 2, "url": "http://b", "title": "high", "source": "x",
             "ai_score": 9.0, "urgency": 0, "first_seen": _now_iso(), "body": ""},
            {"id": 3, "url": "http://c", "title": "low_pass", "source": "x",
             "ai_score": 4.5, "urgency": 0, "first_seen": _now_iso(), "body": ""},
        ])
        rows = signals.get_top_signals(n=10, hours=24, min_score=4.0)
        scores = [r["ai_score"] for r in rows]
        assert scores == sorted(scores, reverse=True)

    def test_backtest_url_filtered(self, fake_articles_db):
        # Backtest synthetic rows must never reach the live trader.
        _build_articles_db(fake_articles_db, [
            {"id": 1, "url": "backtest://NVDA/2025-05-01", "title": "synthetic",
             "source": "backtest_opus", "ai_score": 9.0, "urgency": 1,
             "first_seen": _now_iso(), "body": "should not appear"},
            {"id": 2, "url": "http://real.com", "title": "real article",
             "source": "reuters", "ai_score": 9.0, "urgency": 1,
             "first_seen": _now_iso(), "body": "real"},
        ])
        rows = signals.get_top_signals(n=10, hours=24, min_score=4.0)
        assert len(rows) == 1
        assert rows[0]["url"] == "http://real.com"

    def test_opus_annotation_source_filtered(self, fake_articles_db):
        _build_articles_db(fake_articles_db, [
            {"id": 1, "url": "http://x", "title": "annot",
             "source": "opus_annotation_v1", "ai_score": 9.0, "urgency": 1,
             "first_seen": _now_iso(), "body": "should not appear"},
            {"id": 2, "url": "http://y", "title": "real",
             "source": "bloomberg", "ai_score": 5.0, "urgency": 0,
             "first_seen": _now_iso(), "body": "real"},
        ])
        rows = signals.get_top_signals(n=10, hours=24, min_score=4.0)
        assert len(rows) == 1
        assert rows[0]["source"] == "bloomberg"

    def test_old_articles_filtered_by_hours(self, fake_articles_db):
        _build_articles_db(fake_articles_db, [
            {"id": 1, "url": "http://old", "title": "stale", "source": "x",
             "ai_score": 9.0, "urgency": 0, "first_seen": _hours_ago(24), "body": ""},
            {"id": 2, "url": "http://new", "title": "fresh", "source": "x",
             "ai_score": 9.0, "urgency": 0, "first_seen": _now_iso(), "body": ""},
        ])
        rows = signals.get_top_signals(n=10, hours=2, min_score=4.0)
        assert len(rows) == 1
        assert rows[0]["url"] == "http://new"

    def test_tickers_extracted_into_output(self, fake_articles_db):
        _build_articles_db(fake_articles_db, [
            {"id": 1, "url": "http://x", "title": "NVDA crushes earnings",
             "source": "x", "ai_score": 9.0, "urgency": 0,
             "first_seen": _now_iso(), "body": "AMD and NVDA up 5%"},
        ])
        rows = signals.get_top_signals(n=10, hours=24, min_score=4.0)
        assert len(rows) == 1
        tickers = set(rows[0]["tickers"])
        assert "NVDA" in tickers
        assert "AMD" in tickers


class TestTickerSentiments:
    def test_unmentioned_ticker_returns_zero_defaults(self, fake_articles_db):
        _build_articles_db(fake_articles_db, [
            {"id": 1, "url": "http://x", "title": "AAPL beats", "source": "x",
             "ai_score": 9.0, "urgency": 0, "first_seen": _now_iso(), "body": ""},
        ])
        out = signals.ticker_sentiments(["NVDA"], hours=24)
        assert len(out) == 1
        assert out[0] == {"ticker": "NVDA", "avg_score": 0.0, "max_score": 0.0, "n": 0, "urgent": 0}

    def test_average_score_calculation(self, fake_articles_db):
        _build_articles_db(fake_articles_db, [
            {"id": 1, "url": "http://a", "title": "NVDA earnings",
             "source": "x", "ai_score": 4.0, "urgency": 0,
             "first_seen": _now_iso(), "body": ""},
            {"id": 2, "url": "http://b", "title": "NVDA downgrade",
             "source": "x", "ai_score": 8.0, "urgency": 0,
             "first_seen": _now_iso(), "body": ""},
        ])
        out = signals.ticker_sentiments(["NVDA"], hours=24)
        nvda = out[0]
        assert nvda["n"] == 2
        # avg = (4 + 8) / 2 = 6.0
        assert nvda["avg_score"] == pytest.approx(6.0)
        assert nvda["max_score"] == 8.0

    def test_urgent_counter_increments(self, fake_articles_db):
        _build_articles_db(fake_articles_db, [
            {"id": 1, "url": "http://a", "title": "NVDA flash crash",
             "source": "x", "ai_score": 9.0, "urgency": 1,
             "first_seen": _now_iso(), "body": ""},
            {"id": 2, "url": "http://b", "title": "NVDA boring news",
             "source": "x", "ai_score": 4.0, "urgency": 0,
             "first_seen": _now_iso(), "body": ""},
        ])
        out = signals.ticker_sentiments(["NVDA"], hours=24)
        assert out[0]["urgent"] == 1
        assert out[0]["n"] == 2

    def test_backtest_rows_filtered(self, fake_articles_db):
        _build_articles_db(fake_articles_db, [
            {"id": 1, "url": "backtest://x", "title": "NVDA synthetic",
             "source": "backtest_run1", "ai_score": 9.0, "urgency": 0,
             "first_seen": _now_iso(), "body": ""},
            {"id": 2, "url": "http://real", "title": "NVDA real",
             "source": "bloomberg", "ai_score": 5.0, "urgency": 0,
             "first_seen": _now_iso(), "body": ""},
        ])
        out = signals.ticker_sentiments(["NVDA"], hours=24)
        # Only the bloomberg row contributes.
        assert out[0]["n"] == 1
        assert out[0]["avg_score"] == pytest.approx(5.0)

    def test_dollar_prefixed_ticker_matched(self, fake_articles_db):
        # The pattern is `(?:\$|\b)NVDA\b` so $NVDA must also count.
        _build_articles_db(fake_articles_db, [
            {"id": 1, "url": "http://a", "title": "$NVDA pop",
             "source": "x", "ai_score": 7.0, "urgency": 0,
             "first_seen": _now_iso(), "body": ""},
        ])
        out = signals.ticker_sentiments(["NVDA"], hours=24)
        assert out[0]["n"] == 1
        assert out[0]["max_score"] == 7.0

    def test_word_boundary_prevents_substring_match(self, fake_articles_db):
        # "MUSE" should NOT count as a mention of "MU".
        _build_articles_db(fake_articles_db, [
            {"id": 1, "url": "http://a", "title": "MUSEUM opens",
             "source": "x", "ai_score": 5.0, "urgency": 0,
             "first_seen": _now_iso(), "body": "MUSEUMS everywhere"},
        ])
        out = signals.ticker_sentiments(["MU"], hours=24)
        assert out[0]["n"] == 0


class TestGetUrgentArticles:
    def test_only_urgency_ge_1_returned(self, fake_articles_db):
        _build_articles_db(fake_articles_db, [
            {"id": 1, "url": "http://a", "title": "flat", "source": "x",
             "ai_score": 5.0, "urgency": 0,
             "first_seen": _now_iso(), "body": ""},
            {"id": 2, "url": "http://b", "title": "BREAKING", "source": "x",
             "ai_score": 5.0, "urgency": 1,
             "first_seen": _now_iso(), "body": ""},
        ])
        out = signals.get_urgent_articles(minutes=60)
        assert len(out) == 1
        assert out[0]["title"] == "BREAKING"

    def test_null_ai_score_coerced_to_zero(self, fake_articles_db):
        # If a row has NULL ai_score, the get_urgent_articles output must not
        # crash downstream formatting that does f"{ai_score:.1f}".
        _build_articles_db(fake_articles_db, [
            {"id": 1, "url": "http://a", "title": "BREAKING", "source": "x",
             "ai_score": None, "urgency": 2,
             "first_seen": _now_iso(), "body": ""},
        ])
        out = signals.get_urgent_articles(minutes=60)
        assert len(out) == 1
        # Must be coerced to a float so downstream formatting works.
        assert out[0]["ai_score"] == 0.0
        # And the format string used downstream must not raise.
        f"{out[0]['ai_score']:.1f}"
