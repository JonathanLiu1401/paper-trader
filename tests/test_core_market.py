"""Tests for paper_trader.market — NYSE session calendar and price helpers.

Session-calendar tests use injected fake "now" timestamps so they are fast
and independent of the actual wall clock. Price helpers are tested with
yfinance mocked so the suite never hits the network.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from paper_trader import market

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def _ny(year, month, day, hour, minute):
    """Build a UTC datetime corresponding to a given NY wall-clock time."""
    return datetime(year, month, day, hour, minute, tzinfo=NY).astimezone(UTC)


class TestIsMarketOpen:
    def test_weekend_saturday_returns_false(self):
        # 2026-05-16 is a Saturday.
        assert market.is_market_open(_ny(2026, 5, 16, 10, 0)) is False

    def test_weekend_sunday_returns_false(self):
        assert market.is_market_open(_ny(2026, 5, 17, 10, 0)) is False

    def test_pre_open_929_returns_false(self):
        # 2026-05-14 Thursday, 9:29 AM ET is still pre-open.
        assert market.is_market_open(_ny(2026, 5, 14, 9, 29)) is False

    def test_after_close_4pm_returns_false(self):
        # The window is half-open [9:30, 16:00), so 16:00 is the close.
        assert market.is_market_open(_ny(2026, 5, 14, 16, 0)) is False

    def test_after_close_401pm_returns_false(self):
        assert market.is_market_open(_ny(2026, 5, 14, 16, 1)) is False

    def test_weekday_10am_returns_true(self):
        assert market.is_market_open(_ny(2026, 5, 14, 10, 0)) is True

    def test_weekday_exactly_930_returns_true(self):
        # Lower bound is inclusive.
        assert market.is_market_open(_ny(2026, 5, 14, 9, 30)) is True

    def test_weekday_1559_returns_true(self):
        # Upper bound is exclusive — one minute before close is still open.
        assert market.is_market_open(_ny(2026, 5, 14, 15, 59)) is True

    def test_thanksgiving_returns_false(self):
        # 2026-11-26 is Thanksgiving; even mid-day the market is closed.
        assert market.is_market_open(_ny(2026, 11, 26, 10, 0)) is False

    def test_new_years_day_returns_false(self):
        assert market.is_market_open(_ny(2026, 1, 1, 10, 0)) is False

    def test_good_friday_returns_false(self):
        # 2026-04-03 is Good Friday.
        assert market.is_market_open(_ny(2026, 4, 3, 10, 0)) is False


class TestPriceCache:
    def setup_method(self):
        # The module-level cache leaks between tests; clear before each.
        market._PRICE_CACHE.clear()

    def test_cached_price_returns_cached_value(self):
        market._store_price("NVDA", 500.0)
        assert market._cached_price("NVDA") == 500.0

    def test_cached_price_missing_returns_none(self):
        assert market._cached_price("ABSENT") is None

    def test_cache_expires_after_ttl(self, monkeypatch):
        market._store_price("NVDA", 500.0)
        # Move the module's view of time forward beyond TTL.
        import time as _t
        real = _t.time()
        monkeypatch.setattr(market.time, "time", lambda: real + market._PRICE_TTL + 1)
        assert market._cached_price("NVDA") is None


class TestGetPriceMocked:
    def setup_method(self):
        market._PRICE_CACHE.clear()

    def test_fast_info_path_returns_price_and_caches(self, monkeypatch):
        fake_ticker = MagicMock()
        fake_ticker.fast_info = {"last_price": 123.45, "regular_market_price": 0}
        monkeypatch.setattr(market.yf, "Ticker", lambda t: fake_ticker)
        assert market.get_price("FAKE") == 123.45
        assert market._cached_price("FAKE") == 123.45

    def test_zero_fast_info_falls_back_to_history(self, monkeypatch):
        import pandas as pd
        fake_ticker = MagicMock()
        fake_ticker.fast_info = {"last_price": 0, "regular_market_price": 0}
        fake_ticker.history.return_value = pd.DataFrame({"Close": [99.5]})
        monkeypatch.setattr(market.yf, "Ticker", lambda t: fake_ticker)
        assert market.get_price("FAKE") == pytest.approx(99.5)

    def test_yfinance_exception_returns_none(self, monkeypatch):
        def raise_(_t):
            raise RuntimeError("network down")
        monkeypatch.setattr(market.yf, "Ticker", raise_)
        assert market.get_price("FAKE") is None

    def test_get_prices_empty_returns_empty(self):
        assert market.get_prices([]) == {}

    def test_get_prices_uses_cache(self):
        market._store_price("AAA", 10.0)
        market._store_price("BBB", 20.0)
        out = market.get_prices(["AAA", "BBB"])
        assert out == {"AAA": 10.0, "BBB": 20.0}


class TestGetOptionPrice:
    def test_strike_not_in_chain_returns_none(self, monkeypatch):
        import pandas as pd
        # Build a fake chain that does NOT contain strike=999.
        chain = MagicMock()
        chain.calls = pd.DataFrame([{"strike": 100.0, "lastPrice": 5.0, "bid": 4.5, "ask": 5.5}])
        chain.puts = pd.DataFrame([{"strike": 100.0, "lastPrice": 1.0, "bid": 0.5, "ask": 1.5}])
        fake_ticker = MagicMock()
        fake_ticker.option_chain.return_value = chain
        monkeypatch.setattr(market.yf, "Ticker", lambda t: fake_ticker)
        assert market.get_option_price("FAKE", "2026-12-19", 999.0, "call") is None

    def test_mid_of_bid_ask_when_both_positive(self, monkeypatch):
        import pandas as pd
        chain = MagicMock()
        chain.calls = pd.DataFrame([{"strike": 100.0, "lastPrice": 5.0, "bid": 4.0, "ask": 6.0}])
        chain.puts = pd.DataFrame([{"strike": 100.0, "lastPrice": 1.0, "bid": 0.0, "ask": 0.0}])
        fake_ticker = MagicMock()
        fake_ticker.option_chain.return_value = chain
        monkeypatch.setattr(market.yf, "Ticker", lambda t: fake_ticker)
        # Mid = (4+6)/2 = 5.0
        assert market.get_option_price("FAKE", "2026-12-19", 100.0, "call") == 5.0

    def test_falls_back_to_last_when_bid_ask_zero(self, monkeypatch):
        import pandas as pd
        chain = MagicMock()
        chain.calls = pd.DataFrame([{"strike": 100.0, "lastPrice": 7.5, "bid": 0.0, "ask": 0.0}])
        chain.puts = pd.DataFrame([{"strike": 100.0, "lastPrice": 1.0, "bid": 0.0, "ask": 0.0}])
        fake_ticker = MagicMock()
        fake_ticker.option_chain.return_value = chain
        monkeypatch.setattr(market.yf, "Ticker", lambda t: fake_ticker)
        assert market.get_option_price("FAKE", "2026-12-19", 100.0, "call") == 7.5
