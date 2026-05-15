"""Tests for paper_trader.strategy — JSON parsing, indicators, pre-trade enforcement,
and the BUY/SELL/SELL_CALL/BUY_CALL execution path against a real Store.

The live trader has NO hard limits by design — the system prompt grants Opus
full autonomy. So tests around "max position size" and "stop loss" instead
verify the limits that DO exist: cash must not go negative, sells must not
exceed held qty, and option closes must disambiguate when multiple contracts
match.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from paper_trader import strategy
from paper_trader import market
from paper_trader import store as store_mod
from paper_trader.store import Store


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    db = tmp_path / "paper_trader.db"
    monkeypatch.setattr(store_mod, "DB_PATH", db)
    monkeypatch.setattr(store_mod, "_singleton", None)
    s = Store()
    try:
        yield s
    finally:
        s.close()


# ─────────────────────────── _parse_decision ───────────────────────────

class TestParseDecision:
    def test_plain_json_object(self):
        d = strategy._parse_decision('{"action": "BUY", "ticker": "NVDA", "qty": 1}')
        assert d == {"action": "BUY", "ticker": "NVDA", "qty": 1}

    def test_strips_json_fence(self):
        d = strategy._parse_decision('```json\n{"action": "HOLD"}\n```')
        assert d == {"action": "HOLD"}

    def test_strips_bare_fence(self):
        d = strategy._parse_decision('```\n{"action": "HOLD"}\n```')
        assert d == {"action": "HOLD"}

    def test_extracts_first_object_with_trailing_text(self):
        # The model may emit a JSON object followed by prose.
        raw = '{"action": "BUY", "ticker": "AMD", "qty": 1.0}\n\nNotes: this is fine'
        d = strategy._parse_decision(raw)
        assert d["action"] == "BUY"
        assert d["ticker"] == "AMD"

    def test_returns_none_for_garbage(self):
        assert strategy._parse_decision("definitely not json at all") is None

    def test_returns_none_for_empty(self):
        assert strategy._parse_decision("") is None
        assert strategy._parse_decision(None) is None

    def test_skips_prose_before_json(self):
        raw = 'Here is my decision: {"action":"SELL", "ticker":"NVDA", "qty":2}'
        d = strategy._parse_decision(raw)
        assert d["action"] == "SELL"
        assert d["ticker"] == "NVDA"


# ─────────────────────────── indicator helpers ───────────────────────────

class TestRSILive:
    def test_returns_none_for_short_input(self):
        # Need > period closes; period=14 means need ≥ 15.
        assert strategy._rsi_live([1.0] * 14) is None

    def test_returns_100_when_no_losses(self):
        closes = [float(i) for i in range(1, 30)]  # strictly increasing
        rsi = strategy._rsi_live(closes, period=14)
        assert rsi == 100.0

    def test_rsi_range(self):
        # Alternating up/down should give RSI somewhere in (0, 100).
        closes = [100.0 + ((-1) ** i) * 0.5 for i in range(30)]
        rsi = strategy._rsi_live(closes, period=14)
        assert rsi is not None
        assert 0.0 <= rsi <= 100.0


class TestEMALive:
    def test_returns_empty_for_short(self):
        assert strategy._ema_live([1.0, 2.0, 3.0], period=5) == []

    def test_length_is_n_minus_period_plus_1(self):
        out = strategy._ema_live([float(i) for i in range(20)], period=5)
        assert len(out) == 20 - 5 + 1

    def test_first_value_is_sma(self):
        vals = [10.0, 20.0, 30.0, 40.0, 50.0]
        out = strategy._ema_live(vals, period=5)
        # First EMA value is the SMA of the first 5 elements.
        assert out[0] == pytest.approx(30.0)


class TestMACDLive:
    def test_returns_none_for_too_few_closes(self):
        # MACD needs at least 35 closes (26 EMA + 9 signal smoothing).
        assert strategy._macd_live([float(i) for i in range(34)]) is None

    def test_accelerating_uptrend_is_bullish(self):
        # A *strictly linear* uptrend hits a MACD steady-state where the
        # signal line equals the MACD line; floating-point noise then decides
        # the comparison. An accelerating uptrend keeps MACD above signal.
        closes = [100.0 + i + 0.02 * i * i for i in range(60)]
        assert strategy._macd_live(closes) == "bullish"

    def test_accelerating_downtrend_is_bearish(self):
        closes = [100.0 - i - 0.02 * i * i for i in range(60)]
        assert strategy._macd_live(closes) == "bearish"


# ─────────────────────────── _enforce_risk_pre_trade ───────────────────────────

class TestEnforceRiskPreTrade:
    def test_hold_always_allowed(self):
        snap = {"positions": []}
        ok, why = strategy._enforce_risk_pre_trade({"action": "HOLD"}, snap)
        assert ok is True
        assert why == ""

    def test_buy_with_zero_qty_blocked(self):
        snap = {"positions": []}
        ok, why = strategy._enforce_risk_pre_trade(
            {"action": "BUY", "ticker": "NVDA", "qty": 0}, snap)
        assert ok is False
        assert "qty" in why.lower()

    def test_buy_allowed_when_no_holdings(self):
        snap = {"positions": []}
        ok, _ = strategy._enforce_risk_pre_trade(
            {"action": "BUY", "ticker": "NVDA", "qty": 5}, snap)
        assert ok is True

    def test_sell_without_position_blocked(self):
        snap = {"positions": []}
        ok, why = strategy._enforce_risk_pre_trade(
            {"action": "SELL", "ticker": "NVDA", "qty": 1}, snap)
        assert ok is False
        assert "no open" in why.lower()

    def test_sell_exceeding_held_qty_blocked(self):
        snap = {"positions": [{"ticker": "NVDA", "type": "stock", "qty": 5}]}
        ok, why = strategy._enforce_risk_pre_trade(
            {"action": "SELL", "ticker": "NVDA", "qty": 10}, snap)
        assert ok is False
        assert "exceeds held" in why.lower()

    def test_sell_within_held_qty_allowed(self):
        snap = {"positions": [{"ticker": "NVDA", "type": "stock", "qty": 5}]}
        ok, _ = strategy._enforce_risk_pre_trade(
            {"action": "SELL", "ticker": "NVDA", "qty": 5}, snap)
        assert ok is True


# ─────────────────────────── _execute (BUY / SELL) ───────────────────────────

class TestExecuteBuy:
    def test_buy_decreases_cash_and_creates_position(self, fresh_store, monkeypatch):
        monkeypatch.setattr(market, "get_price", lambda t: 100.0)
        snap = {"cash": 1000.0, "total_value": 1000.0, "positions": []}
        decision = {"action": "BUY", "ticker": "AMD", "qty": 5, "reasoning": "test"}
        status, detail = strategy._execute(decision, snap, fresh_store)
        assert status == "FILLED"
        assert "BUY 5" in detail
        pf = fresh_store.get_portfolio()
        # 1000 - 5 * 100 = 500
        assert pf["cash"] == 500.0
        positions = fresh_store.open_positions()
        assert len(positions) == 1
        assert positions[0]["ticker"] == "AMD"
        assert positions[0]["qty"] == 5

    def test_buy_blocked_when_cash_insufficient(self, fresh_store, monkeypatch):
        monkeypatch.setattr(market, "get_price", lambda t: 100.0)
        snap = {"cash": 50.0, "total_value": 50.0, "positions": []}
        decision = {"action": "BUY", "ticker": "AMD", "qty": 5, "reasoning": ""}
        status, detail = strategy._execute(decision, snap, fresh_store)
        assert status == "BLOCKED"
        assert "insufficient cash" in detail

    def test_buy_blocked_when_no_price(self, fresh_store, monkeypatch):
        monkeypatch.setattr(market, "get_price", lambda t: None)
        snap = {"cash": 1000.0, "total_value": 1000.0, "positions": []}
        decision = {"action": "BUY", "ticker": "AMD", "qty": 1, "reasoning": ""}
        status, detail = strategy._execute(decision, snap, fresh_store)
        assert status == "BLOCKED"
        assert "no price" in detail

    def test_buy_blocked_on_non_numeric_qty(self, fresh_store):
        snap = {"cash": 1000.0, "total_value": 1000.0, "positions": []}
        decision = {"action": "BUY", "ticker": "AMD", "qty": "lots", "reasoning": ""}
        status, detail = strategy._execute(decision, snap, fresh_store)
        assert status == "BLOCKED"
        assert "qty" in detail.lower()


class TestExecuteSell:
    def test_sell_increases_cash_and_closes_position(self, fresh_store, monkeypatch):
        monkeypatch.setattr(market, "get_price", lambda t: 120.0)
        # Seed position: 5 @ 100. Snapshot reflects the open position.
        fresh_store.upsert_position("AMD", "stock", qty=5, avg_cost=100.0)
        snap = {
            "cash": 500.0, "total_value": 1000.0,
            "positions": [{"ticker": "AMD", "type": "stock", "qty": 5, "avg_cost": 100.0}],
        }
        decision = {"action": "SELL", "ticker": "AMD", "qty": 5, "reasoning": ""}
        status, _ = strategy._execute(decision, snap, fresh_store)
        assert status == "FILLED"
        pf = fresh_store.get_portfolio()
        # 500 cash + 5*120 = 1100
        assert pf["cash"] == 1100.0
        # Position fully closed.
        assert fresh_store.open_positions() == []


class TestExecuteBuyCall:
    def test_buy_call_records_position_with_strike_and_expiry(self, fresh_store, monkeypatch):
        monkeypatch.setattr(market, "get_option_price", lambda t, e, s, ot: 5.0)
        snap = {"cash": 1000.0, "total_value": 1000.0, "positions": []}
        decision = {"action": "BUY_CALL", "ticker": "NVDA", "qty": 1,
                    "strike": 600, "expiry": "2026-12-19", "reasoning": ""}
        status, _ = strategy._execute(decision, snap, fresh_store)
        assert status == "FILLED"
        positions = fresh_store.open_positions()
        assert len(positions) == 1
        assert positions[0]["type"] == "call"
        assert positions[0]["strike"] == 600.0
        # Cash: 1000 - 5 * 1 * 100 = 500
        assert fresh_store.get_portfolio()["cash"] == 500.0

    def test_buy_call_blocked_without_strike(self, fresh_store):
        snap = {"cash": 1000.0, "total_value": 1000.0, "positions": []}
        decision = {"action": "BUY_CALL", "ticker": "NVDA", "qty": 1,
                    "expiry": "2026-12-19", "reasoning": ""}
        status, detail = strategy._execute(decision, snap, fresh_store)
        assert status == "BLOCKED"
        assert "strike" in detail.lower()

    def test_buy_call_blocked_when_insufficient_cash(self, fresh_store, monkeypatch):
        monkeypatch.setattr(market, "get_option_price", lambda t, e, s, ot: 100.0)
        snap = {"cash": 50.0, "total_value": 50.0, "positions": []}
        decision = {"action": "BUY_CALL", "ticker": "NVDA", "qty": 1,
                    "strike": 600, "expiry": "2026-12-19", "reasoning": ""}
        status, detail = strategy._execute(decision, snap, fresh_store)
        assert status == "BLOCKED"
        assert "insufficient cash" in detail


class TestExecuteSellCallDisambiguation:
    """Regression: silently picking the first match when multiple option
    contracts share the same ticker+type is dangerous. The execute path now
    BLOCKS unless strike+expiry are specified."""

    def test_ambiguous_close_blocked(self, fresh_store, monkeypatch):
        monkeypatch.setattr(market, "get_option_price", lambda t, e, s, ot: 6.0)
        positions = [
            {"ticker": "NVDA", "type": "call", "qty": 1, "avg_cost": 5.0,
             "strike": 600.0, "expiry": "2026-12-19"},
            {"ticker": "NVDA", "type": "call", "qty": 1, "avg_cost": 5.0,
             "strike": 700.0, "expiry": "2026-12-19"},
        ]
        snap = {"cash": 1000.0, "total_value": 2000.0, "positions": positions}
        # No strike → ambiguous → must be BLOCKED.
        decision = {"action": "SELL_CALL", "ticker": "NVDA", "qty": 1, "reasoning": ""}
        status, detail = strategy._execute(decision, snap, fresh_store)
        assert status == "BLOCKED"
        assert "ambiguous" in detail.lower()

    def test_unambiguous_close_works(self, fresh_store, monkeypatch):
        # Only ONE open contract → strike not strictly required to disambiguate.
        monkeypatch.setattr(market, "get_option_price", lambda t, e, s, ot: 6.0)
        fresh_store.upsert_position("NVDA", "call", qty=1, avg_cost=5.0,
                                    expiry="2026-12-19", strike=600.0)
        positions = [{"ticker": "NVDA", "type": "call", "qty": 1, "avg_cost": 5.0,
                      "strike": 600.0, "expiry": "2026-12-19"}]
        snap = {"cash": 500.0, "total_value": 600.0, "positions": positions}
        decision = {"action": "SELL_CALL", "ticker": "NVDA", "qty": 1, "reasoning": ""}
        status, _ = strategy._execute(decision, snap, fresh_store)
        assert status == "FILLED"

    def test_disambiguated_close_works(self, fresh_store, monkeypatch):
        # Two contracts but caller specifies strike + expiry → match resolves.
        monkeypatch.setattr(market, "get_option_price", lambda t, e, s, ot: 6.0)
        fresh_store.upsert_position("NVDA", "call", qty=1, avg_cost=5.0,
                                    expiry="2026-12-19", strike=600.0)
        fresh_store.upsert_position("NVDA", "call", qty=1, avg_cost=5.0,
                                    expiry="2026-12-19", strike=700.0)
        positions = [
            {"ticker": "NVDA", "type": "call", "qty": 1, "avg_cost": 5.0,
             "strike": 600.0, "expiry": "2026-12-19"},
            {"ticker": "NVDA", "type": "call", "qty": 1, "avg_cost": 5.0,
             "strike": 700.0, "expiry": "2026-12-19"},
        ]
        snap = {"cash": 1000.0, "total_value": 2000.0, "positions": positions}
        decision = {"action": "SELL_CALL", "ticker": "NVDA", "qty": 1,
                    "strike": 700, "expiry": "2026-12-19", "reasoning": ""}
        status, _ = strategy._execute(decision, snap, fresh_store)
        assert status == "FILLED"


# ─────────────────────────── HOLD / REBALANCE / unknown ───────────────────────────

class TestExecuteOtherActions:
    def test_hold_returns_hold_status(self, fresh_store):
        snap = {"cash": 1000.0, "total_value": 1000.0, "positions": []}
        status, _ = strategy._execute(
            {"action": "HOLD", "reasoning": "waiting"}, snap, fresh_store)
        assert status == "HOLD"

    def test_rebalance_returns_hold_for_now(self, fresh_store):
        snap = {"cash": 1000.0, "total_value": 1000.0, "positions": []}
        status, detail = strategy._execute(
            {"action": "REBALANCE"}, snap, fresh_store)
        assert status == "HOLD"
        assert "not yet implemented" in detail.lower()

    def test_unknown_action_blocked(self, fresh_store):
        snap = {"cash": 1000.0, "total_value": 1000.0, "positions": []}
        status, detail = strategy._execute(
            {"action": "TELEPORT", "ticker": "NVDA", "qty": 1, "reasoning": ""}, snap, fresh_store)
        assert status == "BLOCKED"
        assert "unknown action" in detail.lower()
