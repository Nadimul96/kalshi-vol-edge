"""Spot price feeds.

Settlement source
-----------------
Kalshi's BTC markets settle on the **CF Benchmarks Bitcoin Real-Time Index (BRTI)**, a
volume-weighted aggregate of Coinbase, Bitstamp, Kraken, Gemini and LMAX Digital. Binance is not
a constituent. For 15-minute and hourly contracts the settlement value is the simple average of
the 60 one-second BRTI readings in the final minute, not a single tick.

The practical consequences, learned the expensive way:

* A model fed Binance prices can be $50-200 away from BRTI during fast markets. One live NO
  position at a $68,400 strike was priced correctly against Binance and lost by $32 against
  BRTI. This package therefore uses Coinbase, a BRTI constituent, as its reference feed.
* Even Coinbase is only a proxy. In the recorded sample, the last Coinbase one-minute close
  differs from the reported BRTI settlement by a few tens of dollars (see the backtest's
  "settlement basis" section). A strategy whose edge is smaller than that basis has no edge.

Feeds implement :class:`FeedAdapter`; the live :class:`CoinbaseFeed` and the fixture-backed
:class:`RecordedFeed` are interchangeable, which is what lets the backtest replay the same
strategy code that runs live.
"""

from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

SETTLEMENT_SOURCE = "CF Benchmarks BRTI (Coinbase, Bitstamp, Kraken, Gemini, LMAX Digital)"


@dataclass(frozen=True, slots=True)
class Candle:
    """One-minute OHLCV candle. ``ts`` is the unix timestamp of the minute's start (UTC)."""

    ts: int
    low: float
    high: float
    open: float
    close: float
    volume: float

    @classmethod
    def from_coinbase_row(cls, row: list[Any]) -> Candle:
        """Coinbase Exchange returns ``[time, low, high, open, close, volume]``."""
        return cls(
            int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])
        )


class FeedAdapter(Protocol):
    """Minimal price feed interface used by the strategy and the backtest."""

    def candles(self, asset: str, start_ts: int, end_ts: int) -> list[Candle]:
        """Candles with ``start_ts <= ts < end_ts``, ascending."""
        ...

    def spot(self, asset: str, at_ts: int | None = None) -> float | None:
        """Latest price known at ``at_ts`` (default: now). None if unavailable."""
        ...


class RecordedFeed:
    """Candles loaded from a fixture file. Deterministic; the backtest's feed.

    ``spot(asset, at_ts)`` returns the close of the last candle that *completed* at or before
    ``at_ts``, i.e. the candle with ``ts <= at_ts - 60``. Using the in-progress candle's close
    would leak the future into the replay.
    """

    def __init__(self, candles_by_asset: dict[str, list[Candle]]) -> None:
        self._candles = {a: sorted(c, key=lambda x: x.ts) for a, c in candles_by_asset.items()}
        self._ts = {a: [c.ts for c in cs] for a, cs in self._candles.items()}

    @classmethod
    def from_file(cls, path: Path, asset: str) -> RecordedFeed:
        """Load a Coinbase-format candle array (``[[ts, low, high, open, close, vol], ...]``)."""
        if not path.exists():
            raise FileNotFoundError(f"candle fixture not found: {path}")
        with path.open() as fh:
            rows = json.load(fh)
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"candle fixture {path} is empty or not a list")
        return cls({asset: [Candle.from_coinbase_row(r) for r in rows]})

    def assets(self) -> list[str]:
        """Assets present in the feed."""
        return sorted(self._candles)

    def candles(self, asset: str, start_ts: int, end_ts: int) -> list[Candle]:
        """Candles in ``[start_ts, end_ts)``."""
        ts = self._ts.get(asset, [])
        lo, hi = bisect_left(ts, start_ts), bisect_left(ts, end_ts)
        return self._candles[asset][lo:hi] if asset in self._candles else []

    def spot(self, asset: str, at_ts: int | None = None) -> float | None:
        """Close of the last completed candle at ``at_ts``."""
        ts = self._ts.get(asset, [])
        if not ts:
            return None
        if at_ts is None:
            return self._candles[asset][-1].close
        idx = bisect_right(ts, at_ts - 60) - 1
        if idx < 0:
            return None
        return self._candles[asset][idx].close

    def coverage(self, asset: str) -> tuple[int, int]:
        """(first_ts, last_ts) for the asset."""
        ts = self._ts[asset]
        return ts[0], ts[-1]


class CoinbaseFeed:
    """Live Coinbase Exchange public API (no key required)."""

    def __init__(self, base_url: str = "https://api.exchange.coinbase.com", timeout: float = 10.0):
        self._http = httpx.Client(base_url=base_url, timeout=timeout)

    def close(self) -> None:
        """Close the HTTP client."""
        self._http.close()

    def candles(self, asset: str, start_ts: int, end_ts: int) -> list[Candle]:
        """Fetch 1-minute candles (max 300 per request on Coinbase)."""
        resp = self._http.get(
            f"/products/{asset}-USD/candles",
            params={"granularity": 60, "start": start_ts, "end": end_ts},
        )
        resp.raise_for_status()
        rows = sorted(resp.json(), key=lambda r: r[0])
        return [Candle.from_coinbase_row(r) for r in rows if start_ts <= r[0] < end_ts]

    def spot(self, asset: str, at_ts: int | None = None) -> float | None:
        """Latest ticker price. ``at_ts`` is ignored for a live feed."""
        resp = self._http.get(f"/products/{asset}-USD/ticker")
        resp.raise_for_status()
        price = resp.json().get("price")
        return float(price) if price else None
