"""Tests for paper_trader.store — portfolio bookkeeping, trades, positions.

These tests use a real sqlite store backed by a temp DB. Each test gets a
fresh Store via the ``tmp_store`` fixture (see conftest.py), so writes from
one test do not leak into another.

The goal is to catch logic bugs in the store's invariants:
- cash bookkeeping after BUY / SELL
- position upserts (open, add to lot, partial close, full close)
- trade ordering (recent_trades returns most-recent first)
- equity curve ordering (ascending after the inversion in store.equity_curve)
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from paper_trader import store as store_mod
from paper_trader.store import INITIAL_CASH, Store


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    """Return a brand-new Store with its DB in tmp_path."""
    db = tmp_path / "paper_trader.db"
    monkeypatch.setattr(store_mod, "DB_PATH", db)
    monkeypatch.setattr(store_mod, "_singleton", None)
    s = Store()
    try:
        yield s
    finally:
        s.close()


class TestPortfolioInitialization:
    def test_initial_cash_is_default(self, fresh_store):
        pf = fresh_store.get_portfolio()
        assert pf["cash"] == INITIAL_CASH
        assert pf["total_value"] == INITIAL_CASH
        assert pf["positions"] == []

    def test_no_open_positions_initially(self, fresh_store):
        assert fresh_store.open_positions() == []

    def test_no_trades_initially(self, fresh_store):
        assert fresh_store.recent_trades() == []


class TestCashBookkeeping:
    def test_update_portfolio_persists_cash(self, fresh_store):
        fresh_store.update_portfolio(cash=750.0, total_value=900.0, positions=[])
        pf = fresh_store.get_portfolio()
        assert pf["cash"] == 750.0
        assert pf["total_value"] == 900.0

    def test_buy_then_update_portfolio_decreases_cash(self, fresh_store):
        # Simulate the cash flow of buying 10 shares at $50 (total $500):
        fresh_store.record_trade("NVDA", "BUY", qty=10, price=50.0, reason="t1")
        fresh_store.upsert_position("NVDA", "stock", qty=10, avg_cost=50.0)
        fresh_store.update_portfolio(
            cash=INITIAL_CASH - 500.0, total_value=INITIAL_CASH, positions=[])
        pf = fresh_store.get_portfolio()
        assert pf["cash"] == INITIAL_CASH - 500.0
        assert pf["total_value"] == INITIAL_CASH  # mark-to-market not yet applied


class TestUpsertPosition:
    def test_first_buy_creates_position(self, fresh_store):
        fresh_store.upsert_position("AMD", "stock", qty=5, avg_cost=100.0)
        positions = fresh_store.open_positions()
        assert len(positions) == 1
        assert positions[0]["ticker"] == "AMD"
        assert positions[0]["qty"] == 5
        assert positions[0]["avg_cost"] == 100.0

    def test_second_buy_blends_avg_cost(self, fresh_store):
        # First lot: 10 @ 100.  Second lot: 10 @ 120.  Blended = 110.
        fresh_store.upsert_position("AMD", "stock", qty=10, avg_cost=100.0)
        fresh_store.upsert_position("AMD", "stock", qty=10, avg_cost=120.0)
        pos = fresh_store.open_positions()
        assert len(pos) == 1
        assert pos[0]["qty"] == 20
        assert pos[0]["avg_cost"] == pytest.approx(110.0)

    def test_partial_sell_keeps_avg_cost(self, fresh_store):
        # Open 10 @ 100.  Sell 4 @ 130 → 6 remain at avg_cost 100 (cost basis unchanged).
        fresh_store.upsert_position("AMD", "stock", qty=10, avg_cost=100.0)
        fresh_store.upsert_position("AMD", "stock", qty=-4, avg_cost=130.0)
        pos = fresh_store.open_positions()
        assert len(pos) == 1
        assert pos[0]["qty"] == 6
        # avg_cost should NOT change on a sell — the blended formula bypasses
        # for qty <= 0 specifically to preserve cost basis.
        assert pos[0]["avg_cost"] == pytest.approx(100.0)

    def test_full_sell_closes_position(self, fresh_store):
        fresh_store.upsert_position("AMD", "stock", qty=10, avg_cost=100.0)
        fresh_store.upsert_position("AMD", "stock", qty=-10, avg_cost=110.0)
        # open_positions() filters closed_at IS NULL AND qty > 0; this should be empty.
        assert fresh_store.open_positions() == []

    def test_overselling_closes_position(self, fresh_store):
        # Defensive: even if we slip past the pre-trade check, an oversell
        # should NOT leave a negative quantity dangling.
        fresh_store.upsert_position("AMD", "stock", qty=5, avg_cost=100.0)
        fresh_store.upsert_position("AMD", "stock", qty=-10, avg_cost=110.0)
        assert fresh_store.open_positions() == []

    def test_options_and_stock_are_separate_positions(self, fresh_store):
        # Same ticker, different type → distinct rows.
        fresh_store.upsert_position("NVDA", "stock", qty=10, avg_cost=500.0)
        fresh_store.upsert_position("NVDA", "call", qty=2, avg_cost=15.0,
                                    expiry="2026-12-19", strike=600.0)
        positions = fresh_store.open_positions()
        types = {p["type"] for p in positions}
        assert types == {"stock", "call"}

    def test_different_strikes_are_separate_positions(self, fresh_store):
        fresh_store.upsert_position("NVDA", "call", qty=1, avg_cost=10.0,
                                    expiry="2026-12-19", strike=600.0)
        fresh_store.upsert_position("NVDA", "call", qty=1, avg_cost=20.0,
                                    expiry="2026-12-19", strike=700.0)
        positions = fresh_store.open_positions()
        assert len(positions) == 2
        strikes = sorted(p["strike"] for p in positions)
        assert strikes == [600.0, 700.0]

    def test_reopen_after_close_creates_new_row(self, fresh_store):
        fresh_store.upsert_position("AMD", "stock", qty=5, avg_cost=100.0)
        fresh_store.upsert_position("AMD", "stock", qty=-5, avg_cost=110.0)
        # Now buy again — should create a new open row, not reopen the closed one.
        fresh_store.upsert_position("AMD", "stock", qty=3, avg_cost=120.0)
        positions = fresh_store.open_positions()
        assert len(positions) == 1
        assert positions[0]["qty"] == 3
        assert positions[0]["avg_cost"] == 120.0


class TestTradesOrdering:
    def test_recent_trades_most_recent_first(self, fresh_store):
        # Insert in known order; recent_trades should reverse.
        fresh_store.record_trade("AAA", "BUY", 1, 1.0, "first")
        fresh_store.record_trade("BBB", "BUY", 1, 2.0, "second")
        fresh_store.record_trade("CCC", "BUY", 1, 3.0, "third")
        trades = fresh_store.recent_trades(limit=10)
        assert [t["ticker"] for t in trades] == ["CCC", "BBB", "AAA"]

    def test_recent_trades_respects_limit(self, fresh_store):
        for i in range(5):
            fresh_store.record_trade(f"T{i}", "BUY", 1, 10.0, "")
        assert len(fresh_store.recent_trades(limit=2)) == 2

    def test_record_trade_value_for_stock(self, fresh_store):
        fresh_store.record_trade("AMD", "BUY", qty=4, price=25.0, reason="")
        t = fresh_store.recent_trades(1)[0]
        # qty * price for stocks (no 100x multiplier).
        assert t["value"] == 100.0

    def test_record_trade_value_for_option(self, fresh_store):
        # qty * price * 100 for options.
        fresh_store.record_trade("NVDA", "BUY_CALL", qty=2, price=5.0, reason="",
                                 option_type="call", strike=600.0, expiry="2026-12-19")
        t = fresh_store.recent_trades(1)[0]
        assert t["value"] == 2 * 5.0 * 100  # = 1000.0


class TestEquityCurve:
    def test_equity_curve_returns_ascending(self, fresh_store):
        # Recording 3 points in chronological order; equity_curve should
        # return them oldest-first (DESC then reversed).
        fresh_store.record_equity_point(1000.0, 1000.0, None)
        fresh_store.record_equity_point(1010.0, 990.0, None)
        fresh_store.record_equity_point(1020.0, 980.0, None)
        eq = fresh_store.equity_curve(limit=10)
        assert len(eq) == 3
        # Total values should be monotonically increasing (matches insert order).
        values = [p["total_value"] for p in eq]
        assert values == sorted(values)

    def test_equity_curve_limit(self, fresh_store):
        for i in range(7):
            fresh_store.record_equity_point(1000.0 + i, 1000.0, None)
        eq = fresh_store.equity_curve(limit=3)
        assert len(eq) == 3
        # The 3 most recent are the highest values 1004, 1005, 1006 ascending.
        assert [p["total_value"] for p in eq] == [1004.0, 1005.0, 1006.0]


class TestDecisions:
    def test_record_decision_returns_id(self, fresh_store):
        rid = fresh_store.record_decision(True, 5, "BUY AMD → FILLED", "{}", 1000.0, 500.0)
        assert rid == 1
        rid2 = fresh_store.record_decision(True, 5, "HOLD → HOLD", "{}", 1000.0, 500.0)
        assert rid2 == 2

    def test_recent_decisions_ordering(self, fresh_store):
        fresh_store.record_decision(True, 1, "first", "", 0, 0)
        fresh_store.record_decision(True, 2, "second", "", 0, 0)
        recs = fresh_store.recent_decisions(limit=5)
        actions = [r["action_taken"] for r in recs]
        assert actions == ["second", "first"]


class TestUpdatePositionMarks:
    def test_marks_persist(self, fresh_store):
        fresh_store.upsert_position("NVDA", "stock", qty=10, avg_cost=100.0)
        pid = fresh_store.open_positions()[0]["id"]
        fresh_store.update_position_marks({pid: (120.0, 200.0)})
        pos = fresh_store.open_positions()[0]
        assert pos["current_price"] == 120.0
        assert pos["unrealized_pl"] == 200.0
