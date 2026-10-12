"""Offline stand-ins for the live market-data source and the Kalshi account client."""

from __future__ import annotations

import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from kalshi_vol_edge.data.live import LiveMarket
from kalshi_vol_edge.data.prices import Candle

FIXTURES = Path(__file__).parent / "fixtures"


def recorded_market() -> LiveMarket:
    """The recorded open market from ``fixtures/kalshi_markets_open.json``."""
    payload = json.loads((FIXTURES / "kalshi_markets_open.json").read_text())
    return LiveMarket.from_api(payload["markets"][0])


def market_at(
    open_ts: int, strike: float = 70000.0, yes_ask: float = 0.62, **kw: Any
) -> LiveMarket:
    """A 15-minute market opening at ``open_ts``."""
    base: dict[str, Any] = dict(
        ticker=f"KXBTC15M-TEST-{open_ts}",
        event_ticker=f"KXBTC15M-TEST{open_ts}",
        title="BTC price up in next 15 mins?",
        status="active",
        strike=strike,
        open_time=datetime.fromtimestamp(open_ts, UTC),
        close_time=datetime.fromtimestamp(open_ts + 900, UTC),
        yes_bid=yes_ask - 0.01,
        yes_ask=yes_ask,
        no_bid=1 - yes_ask - 0.01,
        no_ask=1 - yes_ask + 0.01,
        last_price=yes_ask,
        volume=1000.0,
    )
    base.update(kw)
    return LiveMarket(**base)


def candles_until(end_ts: int, n: int = 90, base: float = 70000.0) -> list[Candle]:
    """``n`` deterministic one-minute candles ending at ``end_ts`` (a gentle oscillation)."""
    start = end_ts - n * 60
    out = []
    for i in range(n):
        close = base + 40 * math.sin(i / 3.0) + 2 * i
        out.append(Candle(start + i * 60, close - 5, close + 5, close - 1, close, 1.0))
    return out


class FakeMarketData:
    """A working source anchored at construction time."""

    def __init__(self, spot: float = 70120.0) -> None:
        now = int(time.time())
        self.now = now
        self._spot = spot
        open_ts = now - 6 * 60  # six minutes into the market
        self.market = market_at(open_ts)
        self.settled = [
            market_at(
                open_ts - 900,
                strike=69990.0,
                status="finalized",
                result="yes",
                expiration_value=70000.0,
            )
        ]
        self.calls = 0

    async def open_markets(self, series: str) -> list[LiveMarket]:
        self.calls += 1
        return [self.market]

    async def settled_markets(self, series: str, limit: int) -> list[LiveMarket]:
        return self.settled[:limit]

    async def spot(self, asset: str) -> float:
        return self._spot

    async def candles(self, asset: str, minutes: int) -> list[Candle]:
        return candles_until(self.now, minutes)


class DownMarketData:
    """Every upstream refuses the connection."""

    async def open_markets(self, series: str) -> list[LiveMarket]:
        raise httpx.ConnectError("connection refused")

    async def settled_markets(self, series: str, limit: int) -> list[LiveMarket]:
        raise httpx.ConnectError("connection refused")

    async def spot(self, asset: str) -> float:
        raise httpx.ConnectError("connection refused")

    async def candles(self, asset: str, minutes: int) -> list[Candle]:
        raise httpx.ConnectError("connection refused")


class FakeAccount:
    """A read-only account with one open position and two fills."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_balance(self) -> dict[str, Any]:
        self.calls.append("balance")
        return {"balance": 12345, "portfolio_value": 2000}

    async def get_positions(self, settlement_status: str = "unsettled") -> list[dict[str, Any]]:
        self.calls.append(f"positions:{settlement_status}")
        return [
            {
                "ticker": "KXBTC15M-26OCT120000-00",
                "position_fp": "5",
                "market_exposure_dollars": "3.10",
                "realized_pnl_dollars": "0",
                "fees_paid_dollars": "0.08",
            },
            {"ticker": "CLOSED", "position_fp": "0"},
        ]

    async def get_fills(self, **params: Any) -> list[dict[str, Any]]:
        self.calls.append("fills")
        return [
            {
                "fill_id": "f-1",
                "ticker": "KXBTC15M-26OCT120000-00",
                "side": "yes",
                "count_fp": "5.00",
                "yes_price_dollars": "0.6200",
                "is_taker": True,
                "fee_cost": "0.0800",
                "created_time": "2026-10-12T03:50:00Z",
            }
        ]
