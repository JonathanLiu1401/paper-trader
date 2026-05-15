"""Tests for variable backtest window selection + engine date plumbing."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import pytest

from run_continuous_backtests import _pick_window


# ─────────────────────────── _pick_window ───────────────────────────

class TestPickWindow:
    def test_same_seed_returns_identical_window(self):
        a_start, a_end = _pick_window(42)
        b_start, b_end = _pick_window(42)
        assert a_start == b_start
        assert a_end == b_end

    def test_different_seeds_give_different_windows(self):
        # Across a small sample, at least two seeds must yield different windows.
        # Probability of collision across 10 random seeds is vanishingly low.
        windows = {_pick_window(s) for s in range(10)}
        assert len(windows) > 1, "Different seeds should diverge"

    def test_duration_is_1_to_5_years(self):
        for seed in range(50):
            start, end = _pick_window(seed)
            days = (end - start).days
            # 1 yr = 365, 5 yr = 1825. Allow exact range.
            assert 365 <= days <= 5 * 365, (
                f"seed {seed}: duration {days}d not in [365, 1825]"
            )

    def test_window_ends_at_least_6_months_before_today(self):
        cutoff = date.today() - timedelta(days=180)
        for seed in range(50):
            _start, end = _pick_window(seed)
            assert end <= cutoff, (
                f"seed {seed}: end {end} not at least 6mo before today {date.today()}"
            )

    def test_window_starts_no_earlier_than_1996(self):
        for seed in range(50):
            start, _end = _pick_window(seed)
            assert start >= date(1996, 1, 1), (
                f"seed {seed}: start {start} before 1996-01-01"
            )


# ─────────────────────── engine date plumbing ───────────────────────

# yfinance is real-network. Patch it inside backtest's namespace so engine
# init returns synthetic OHLCV instead of calling out.
def _make_fake_hist(start: date, end: date):
    """Build a pandas DataFrame with a Close column for trading-day-ish dates."""
    import pandas as pd
    days = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:  # business day
            days.append(cur)
        cur += timedelta(days=1)
    df = pd.DataFrame(
        {"Close": [100.0 + i * 0.1 for i in range(len(days))],
         "Volume": [1_000_000 + i * 1000 for i in range(len(days))]},
        index=pd.DatetimeIndex(days),
    )
    return df


@pytest.fixture
def isolated_caches(tmp_path, monkeypatch):
    """Redirect all backtest disk caches to a temp dir so tests don't pollute real cache."""
    import paper_trader.backtest as bt

    cache_dir = tmp_path / "backtest_cache"
    cache_dir.mkdir()
    gdelt_dir = cache_dir / "gdelt"
    gdelt_dir.mkdir()
    av_dir = cache_dir / "alphavantage"
    av_dir.mkdir()

    monkeypatch.setattr(bt, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(bt, "GDELT_CACHE", gdelt_dir)
    monkeypatch.setattr(bt, "AV_CACHE_DIR", av_dir)
    monkeypatch.setattr(bt, "AV_QUOTA_PATH", cache_dir / "av_quota.json")
    # Reset volume cache state so a previous test's data doesn't bleed in.
    monkeypatch.setattr(bt, "_VOLUME_CACHE", {})
    monkeypatch.setattr(bt, "_VOLUME_CACHE_DISK_LOADED", set())
    # Redirect backtest.db to a temp path too.
    monkeypatch.setattr(bt, "BACKTEST_DB", tmp_path / "backtest.db")
    # Tiny watchlist so test fakes stay cheap.
    monkeypatch.setattr(bt, "WATCHLIST", ["SPY", "AAPL"])

    yield cache_dir


class TestEngineWithCustomDates:
    def test_engine_initializes_with_custom_window(self, isolated_caches, monkeypatch):
        import paper_trader.backtest as bt

        # Fake the yfinance Ticker so PriceCache._load gets synthetic data.
        class _FakeTicker:
            def __init__(self, sym):
                self.sym = sym
            def history(self, start, end, auto_adjust):
                from datetime import date as _d
                s = _d.fromisoformat(start)
                e = _d.fromisoformat(end)
                return _make_fake_hist(s, e)

        monkeypatch.setattr(bt.yf, "Ticker", _FakeTicker)

        custom_start = date(2020, 1, 1)
        custom_end = date(2022, 1, 1)
        engine = bt.BacktestEngine(start=custom_start, end=custom_end)

        assert engine.start == custom_start
        assert engine.end == custom_end
        assert len(engine.prices.trading_days) > 0

    def test_backtest_run_stores_engine_dates_not_module_defaults(
        self, isolated_caches, monkeypatch
    ):
        """Sanity-check that the persisted DB row carries the engine's dates."""
        import paper_trader.backtest as bt

        class _FakeTicker:
            def __init__(self, sym):
                self.sym = sym
            def history(self, start, end, auto_adjust):
                from datetime import date as _d
                s = _d.fromisoformat(start)
                e = _d.fromisoformat(end)
                return _make_fake_hist(s, e)

        monkeypatch.setattr(bt.yf, "Ticker", _FakeTicker)

        custom_start = date(2020, 3, 1)
        custom_end = date(2020, 12, 31)
        engine = bt.BacktestEngine(start=custom_start, end=custom_end)
        engine.store.upsert_run(
            run_id=999, seed=1, status="running",
            start=custom_start, end=custom_end,
        )
        row = engine.store.conn.execute(
            "SELECT start_date, end_date FROM backtest_runs WHERE run_id=999"
        ).fetchone()
        assert row["start_date"] == custom_start.isoformat()
        assert row["end_date"] == custom_end.isoformat()
