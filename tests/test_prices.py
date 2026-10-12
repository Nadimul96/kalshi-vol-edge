"""Recorded feed semantics (no look-ahead) and market records."""

from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import pytest

from kalshi_vol_edge.data.markets import BracketMarket, load_markets
from kalshi_vol_edge.data.prices import Candle, RecordedFeed


def _feed() -> RecordedFeed:
    candles = [
        Candle(ts=60 * i, low=99, high=101, open=100, close=100 + i, volume=1.0) for i in range(10)
    ]
    return RecordedFeed({"BTC": candles})


def test_spot_uses_only_completed_candles():
    feed = _feed()
    # at t=300 the candle starting at 240 has completed (close=104); the one at 300 has not
    assert feed.spot("BTC", 300) == 104
    assert feed.spot("BTC", 330) == 104
    assert feed.spot("BTC", 60) == 100
    assert feed.spot("BTC", 0) is None
    assert feed.spot("BTC") == 109
    assert feed.spot("ETH", 300) is None


def test_candles_window_is_half_open():
    feed = _feed()
    assert [c.ts for c in feed.candles("BTC", 120, 300)] == [120, 180, 240]
    assert feed.coverage("BTC") == (0, 540)


def test_recorded_feed_fails_loudly_on_missing_or_empty_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        RecordedFeed.from_file(tmp_path / "nope.json", "BTC")
    empty = tmp_path / "empty.json"
    empty.write_text("[]")
    with pytest.raises(ValueError):
        RecordedFeed.from_file(empty, "BTC")


def test_bracket_market_from_record_and_strike_semantics():
    m = BracketMarket.from_record(
        {
            "ticker": "KXBTC15M-26MAR222030-30",
            "event_ticker": "KXBTC15M-26MAR222030",
            "series": "KXBTC15M",
            "strike_type": "greater_or_equal",
            "floor_strike": 67989.01,
            "cap_strike": None,
            "open_time": "2026-03-23T00:15:00+00:00",
            "close_time": "2026-03-23T00:30:00Z",
            "result": "yes",
            "expiration_value": 68000.61,
            "volume": 329959.0,
        }
    )
    assert m.asset == "BTC" and m.strike == 67989.01 and m.duration_minutes == 15
    assert m.yes_wins() is True
    assert m.close_time == datetime(2026, 3, 23, 0, 30, tzinfo=UTC)
    between = BracketMarket.from_record(
        {
            "ticker": "KXBTC-26MAR2122-B67650",
            "strike_type": "between",
            "floor_strike": 67600,
            "cap_strike": 67699.99,
            "open_time": "2026-03-22T01:00:00Z",
            "close_time": "2026-03-22T02:00:00Z",
        }
    )
    assert between.yes_wins() is None
    with pytest.raises(ValueError):
        _ = between.strike


def test_load_markets_sorted_and_loud(sample_dir: Path, tmp_path: Path):
    markets = load_markets(sample_dir / "markets_KXBTC15M.json")
    assert len(markets) == 30
    assert all(a.close_time <= b.close_time for a, b in pairwise(markets))
    with pytest.raises(FileNotFoundError):
        load_markets(tmp_path / "missing.json")
