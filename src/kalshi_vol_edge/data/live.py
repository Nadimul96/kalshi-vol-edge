"""Public, key-free live market data: Kalshi markets and Coinbase spot and candles.

Neither source needs an account. Kalshi's ``GET /markets`` is public on the production
exchange, which is where the real BTC 15-minute markets trade, so this client always reads
market data from ``Settings.kalshi_market_data_url`` and never signs a request. Account
reads live in :mod:`kalshi_vol_edge.kalshi.client`.

:class:`MarketDataSource` is the seam the dashboard depends on; tests swap in a recorded
implementation so nothing in the test suite touches the network.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import httpx

from kalshi_vol_edge.data.prices import Candle

COINBASE_URL = "https://api.exchange.coinbase.com"


@dataclass(frozen=True, slots=True)
class LiveMarket:
    """One Kalshi market as the public API reports it, prices in dollars."""

    ticker: str
    event_ticker: str
    title: str
    status: str
    strike: float | None
    open_time: datetime
    close_time: datetime
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    last_price: float | None
    volume: float
    result: str | None = None
    expiration_value: float | None = None

    @property
    def open_ts(self) -> int:
        """Unix timestamp of open."""
        return int(self.open_time.timestamp())

    @property
    def close_ts(self) -> int:
        """Unix timestamp of close."""
        return int(self.close_time.timestamp())

    @classmethod
    def from_api(cls, rec: dict[str, Any]) -> LiveMarket:
        """Parse a ``GET /markets`` record (2026 schema: ``*_dollars`` strings)."""
        return cls(
            ticker=str(rec["ticker"]),
            event_ticker=str(rec.get("event_ticker", "")),
            title=str(rec.get("title", "")),
            status=str(rec.get("status", "")),
            strike=_opt_float(rec.get("floor_strike")),
            open_time=_parse_time(rec["open_time"]),
            close_time=_parse_time(rec["close_time"]),
            yes_bid=_price(rec.get("yes_bid_dollars")),
            yes_ask=_price(rec.get("yes_ask_dollars")),
            no_bid=_price(rec.get("no_bid_dollars")),
            no_ask=_price(rec.get("no_ask_dollars")),
            last_price=_price(rec.get("last_price_dollars")),
            volume=_opt_float(rec.get("volume_fp")) or 0.0,
            result=rec.get("result") or None,
            expiration_value=_opt_float(rec.get("expiration_value")),
        )


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _price(value: Any) -> float | None:
    """A dollar price, or None when the side of the book is empty (0 or missing)."""
    p = _opt_float(value)
    return p if p is not None and 0.0 < p < 1.0 else None


def _parse_time(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class MarketDataSource(Protocol):
    """What the live dashboard reads. Every method may raise; callers handle failure."""

    async def open_markets(self, series: str) -> list[LiveMarket]:
        """Markets currently open for trading in ``series``, soonest close first."""
        ...

    async def settled_markets(self, series: str, limit: int) -> list[LiveMarket]:
        """The most recently settled markets in ``series``, latest first."""
        ...

    async def spot(self, asset: str) -> float:
        """Latest Coinbase trade price for ``asset``-USD."""
        ...

    async def candles(self, asset: str, minutes: int) -> list[Candle]:
        """The last ``minutes`` one-minute Coinbase candles, ascending."""
        ...


class PublicMarketData:
    """:class:`MarketDataSource` over the public Kalshi and Coinbase REST APIs."""

    def __init__(
        self,
        kalshi_url: str,
        coinbase_url: str = COINBASE_URL,
        timeout_s: float = 6.0,
    ) -> None:
        self._kalshi = httpx.AsyncClient(base_url=kalshi_url, timeout=timeout_s)
        self._coinbase = httpx.AsyncClient(base_url=coinbase_url, timeout=timeout_s)

    async def aclose(self) -> None:
        """Close both connection pools."""
        await self._kalshi.aclose()
        await self._coinbase.aclose()

    async def _markets(self, params: dict[str, Any]) -> list[LiveMarket]:
        resp = await self._kalshi.get("/markets", params=params)
        resp.raise_for_status()
        return [LiveMarket.from_api(m) for m in resp.json().get("markets", [])]

    async def open_markets(self, series: str) -> list[LiveMarket]:
        """Open markets in ``series``, soonest close first."""
        markets = await self._markets({"series_ticker": series, "status": "open", "limit": 20})
        return sorted(markets, key=lambda m: m.close_time)

    async def settled_markets(self, series: str, limit: int) -> list[LiveMarket]:
        """Most recently settled markets in ``series`` (the API returns latest first)."""
        markets = await self._markets(
            {"series_ticker": series, "status": "settled", "limit": limit}
        )
        return sorted(markets, key=lambda m: m.close_time, reverse=True)

    async def spot(self, asset: str) -> float:
        """Latest trade price from the Coinbase ticker."""
        resp = await self._coinbase.get(f"/products/{asset}-USD/ticker")
        resp.raise_for_status()
        return float(resp.json()["price"])

    async def candles(self, asset: str, minutes: int) -> list[Candle]:
        """One-minute candles covering the last ``minutes`` (Coinbase caps a page at 300)."""
        end = int(time.time())
        resp = await self._coinbase.get(
            f"/products/{asset}-USD/candles",
            params={"granularity": 60, "start": end - minutes * 60, "end": end},
        )
        resp.raise_for_status()
        return sorted((Candle.from_coinbase_row(r) for r in resp.json()), key=lambda c: c.ts)
