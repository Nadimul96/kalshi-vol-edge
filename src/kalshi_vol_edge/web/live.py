"""The live view: open markets priced by the strategy's own model, with source health.

:func:`build_live_view` is a pure function from fetched data to a view model, so it is
tested without a network. :class:`LiveService` does the fetching: every source is fetched
independently, a failure keeps the last good value and is reported with its age, and a
short cache stops several open tabs from hammering the public APIs.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from kalshi_vol_edge.data.live import LiveMarket, MarketDataSource
from kalshi_vol_edge.data.prices import Candle, RecordedFeed
from kalshi_vol_edge.kalshi.fees import net_edge
from kalshi_vol_edge.logging import get_logger
from kalshi_vol_edge.strategy.vol_edge import (
    ArmCheck,
    ArmConfig,
    MarketSnapshot,
    VolEdgeStrategy,
    fair_value,
    implied_vol,
    realized_vol_annualized,
)

logger = get_logger(__name__)

T = TypeVar("T")

VOL_LOOKBACK_MINUTES = 60
CANDLE_MINUTES = 90  # covers the vol lookback plus the last few settlements
SETTLED_SHOWN = 4


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """Health of one upstream: did the last fetch work, and how old is the data shown."""

    name: str
    ok: bool
    age_s: float | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MarketView:
    """One open market priced by the model."""

    market: LiveMarket
    minutes_left: float
    minutes_elapsed: float
    lead: float | None
    yes_mid: float | None
    fair_yes: float | None
    implied_vol: float | None
    edge_yes: float | None
    edge_no: float | None
    arms: list[ArmCheck]

    @property
    def window_minutes(self) -> float:
        """Open-to-close length of the market."""
        return (self.market.close_ts - self.market.open_ts) / 60.0

    @property
    def elapsed_fraction(self) -> float:
        """How far through its window the market is, 0..1."""
        return min(1.0, max(0.0, self.minutes_elapsed / max(self.window_minutes, 1e-9)))


@dataclass(frozen=True, slots=True)
class SettledView:
    """A recently settled market with the measured Coinbase-vs-BRTI basis."""

    market: LiveMarket
    coinbase_close: float | None
    basis: float | None
    direction_flip: bool | None


@dataclass(frozen=True)
class LiveView:
    """Everything the live page renders."""

    generated_at: float
    series: str
    asset: str
    spot: float | None
    realized_vol: float | None
    model_vol: float
    model_vol_is_default: bool
    candles: list[Candle]
    markets: list[MarketView]
    settled: list[SettledView]
    sources: list[SourceStatus] = field(default_factory=list)
    arm_windows: dict[str, str] = field(default_factory=dict)

    @property
    def degraded(self) -> bool:
        """True when any upstream failed on the last refresh."""
        return any(not s.ok for s in self.sources)

    @property
    def current(self) -> MarketView | None:
        """The market closing soonest."""
        return self.markets[0] if self.markets else None


def arm_window_label(arm: ArmConfig) -> str:
    """Human-readable entry window, e.g. ``3-5.5 min left``."""
    if arm.min_minutes_left is not None and arm.max_minutes_left is not None:
        return f"{arm.min_minutes_left:g}-{arm.max_minutes_left:g} min left"
    if arm.min_minutes_elapsed is not None and arm.max_minutes_elapsed is not None:
        return f"minutes {arm.min_minutes_elapsed:g}-{arm.max_minutes_elapsed:g}"
    return "never"


def _mid(bid: float | None, ask: float | None) -> float | None:
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return ask if ask is not None else bid


def price_market(
    market: LiveMarket,
    spot: float | None,
    model_vol: float,
    now_ts: float,
    strategy: VolEdgeStrategy,
    asset: str,
) -> MarketView:
    """Price one open market with the model and run every arm's diagnostics on it."""
    minutes_left = max(0.0, (market.close_ts - now_ts) / 60.0)
    minutes_elapsed = max(0.0, (now_ts - market.open_ts) / 60.0)
    yes_mid = _mid(market.yes_bid, market.yes_ask)
    if spot is None or market.strike is None:
        return MarketView(
            market, minutes_left, minutes_elapsed, None, yes_mid, None, None, None, None, []
        )

    fair = fair_value(spot, market.strike, model_vol, minutes_left)
    iv = (
        implied_vol(yes_mid, spot, market.strike, minutes_left)
        if yes_mid is not None and minutes_left > 0
        else None
    )
    edge_yes = net_edge(fair, market.yes_ask) if market.yes_ask is not None else None
    edge_no = net_edge(1.0 - fair, market.no_ask) if market.no_ask is not None else None
    snap = MarketSnapshot(
        event_ticker=market.event_ticker,
        ticker=market.ticker,
        asset=asset,
        strike=market.strike,
        spot=spot,
        minutes_left=minutes_left,
        minutes_elapsed=minutes_elapsed,
        annual_vol=model_vol,
        yes_ask=market.yes_ask or 0.0,
        no_ask=market.no_ask or 0.0,
    )
    return MarketView(
        market=market,
        minutes_left=minutes_left,
        minutes_elapsed=minutes_elapsed,
        lead=spot - market.strike,
        yes_mid=yes_mid,
        fair_yes=fair,
        implied_vol=iv,
        edge_yes=edge_yes,
        edge_no=edge_no,
        arms=strategy.diagnose(snap),
    )


def measure_settled(market: LiveMarket, feed: RecordedFeed, asset: str) -> SettledView:
    """Coinbase's last completed one-minute close before expiry, against the BRTI settlement."""
    first, _ = feed.coverage(asset) if feed.assets() else (None, None)
    cb = None
    if first is not None and market.close_ts - 60 >= first:
        cb = feed.spot(asset, market.close_ts)
    if cb is None or market.expiration_value is None or market.strike is None:
        return SettledView(market, cb, None, None)
    yes_won = market.result == "yes"
    return SettledView(market, cb, cb - market.expiration_value, (cb >= market.strike) != yes_won)


def build_live_view(
    *,
    now_ts: float,
    series: str,
    asset: str,
    markets: list[LiveMarket],
    settled: list[LiveMarket],
    spot: float | None,
    candles: list[Candle],
    strategy: VolEdgeStrategy,
    sources: list[SourceStatus] | None = None,
) -> LiveView:
    """Assemble the live view. Pure: no clock, no network."""
    completed = [c for c in candles if c.ts <= now_ts - 60]
    closes = [c.close for c in completed[-(VOL_LOOKBACK_MINUTES + 1) :]]
    realized = realized_vol_annualized(closes)
    model_vol = strategy.clamp_vol(realized)
    feed = RecordedFeed({asset: completed}) if completed else RecordedFeed({})
    return LiveView(
        generated_at=now_ts,
        series=series,
        asset=asset,
        spot=spot,
        realized_vol=realized,
        model_vol=model_vol,
        model_vol_is_default=realized is None,
        candles=completed[-VOL_LOOKBACK_MINUTES:],
        markets=[
            price_market(m, spot, model_vol, now_ts, strategy, asset)
            for m in markets
            if m.close_ts > now_ts
        ],
        settled=[measure_settled(m, feed, asset) for m in settled[:SETTLED_SHOWN]],
        sources=sources or [],
        arm_windows={a.name: arm_window_label(a) for a in strategy.config.arms},
    )


@dataclass
class _Cached(Generic[T]):
    """Last good value of one upstream plus the outcome of the latest attempt."""

    name: str
    ttl_s: float
    value: T | None = None
    fetched_at: float | None = None
    attempted_at: float | None = None
    error: str | None = None

    def due(self, now: float) -> bool:
        return self.attempted_at is None or now - self.attempted_at >= self.ttl_s

    async def refresh(self, fetch: Callable[[], Awaitable[T]], now: float) -> None:
        self.attempted_at = now
        try:
            self.value = await fetch()
        except Exception as exc:  # any upstream failure is shown, never raised to the page
            self.error = describe_error(exc)
            logger.warning("live_source_failed", source=self.name, error=self.error)
        else:
            self.fetched_at = now
            self.error = None

    def status(self, now: float) -> SourceStatus:
        age = None if self.fetched_at is None else now - self.fetched_at
        return SourceStatus(self.name, self.error is None, age, self.error)


def describe_error(exc: Exception) -> str:
    """A short, secret-free description of a fetch failure."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is not None:
        return f"HTTP {status}"
    return type(exc).__name__


class LiveService:
    """Fetches live data with per-source caching and failure isolation."""

    def __init__(
        self,
        source: MarketDataSource,
        series: str = "KXBTC15M",
        asset: str = "BTC",
        strategy: VolEdgeStrategy | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.source = source
        self.series = series
        self.asset = asset
        self.strategy = strategy or VolEdgeStrategy()
        self._clock = clock
        self._lock = asyncio.Lock()
        self._markets: _Cached[list[LiveMarket]] = _Cached("Kalshi markets", ttl_s=4)
        self._settled: _Cached[list[LiveMarket]] = _Cached("Kalshi settlements", ttl_s=30)
        self._spot: _Cached[float] = _Cached("Coinbase spot", ttl_s=2)
        self._candles: _Cached[list[Candle]] = _Cached("Coinbase candles", ttl_s=20)

    async def view(self) -> LiveView:
        """Refresh whatever is due, then build the view from the last good data."""
        async with self._lock:
            now = self._clock()
            jobs: list[Awaitable[Any]] = []
            if self._markets.due(now):
                jobs.append(
                    self._markets.refresh(lambda: self.source.open_markets(self.series), now)
                )
            if self._settled.due(now):
                jobs.append(
                    self._settled.refresh(
                        lambda: self.source.settled_markets(self.series, SETTLED_SHOWN), now
                    )
                )
            if self._spot.due(now):
                jobs.append(self._spot.refresh(lambda: self.source.spot(self.asset), now))
            if self._candles.due(now):
                jobs.append(
                    self._candles.refresh(
                        lambda: self.source.candles(self.asset, CANDLE_MINUTES), now
                    )
                )
            await asyncio.gather(*jobs)
            now = self._clock()
            return build_live_view(
                now_ts=now,
                series=self.series,
                asset=self.asset,
                markets=self._markets.value or [],
                settled=self._settled.value or [],
                spot=self._spot.value,
                candles=self._candles.value or [],
                strategy=self.strategy,
                sources=[
                    c.status(now) for c in (self._markets, self._settled, self._spot, self._candles)
                ],
            )
