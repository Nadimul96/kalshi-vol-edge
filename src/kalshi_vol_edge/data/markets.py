"""Kalshi crypto bracket market records.

Ticker conventions
------------------
::

    KXBTC15M-26MAR222030-30   15-minute: BTC at close >= strike (strike = BRTI at previous close)
    KXBTCD-26MAR2322-T70599.99 hourly threshold: BTC above 70,599.99 at the top of the hour
    KXBTC-26MAR2122-B67650     hourly bracket: BTC between floor and cap

The hour in the ticker is US Eastern, not UTC. This module never parses times out of tickers;
it uses the ``open_time`` / ``close_time`` fields the API returns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

THRESHOLD_TYPES = ("greater", "greater_or_equal")


@dataclass(frozen=True, slots=True)
class BracketMarket:
    """A settled or open Kalshi bracket market."""

    ticker: str
    event_ticker: str
    series: str
    asset: str
    strike_type: str  # greater | greater_or_equal | between | less
    floor_strike: float | None
    cap_strike: float | None
    open_time: datetime
    close_time: datetime
    result: str | None  # yes | no | None while open
    expiration_value: float | None  # settlement value reported by Kalshi
    volume: float = 0.0

    @property
    def strike(self) -> float:
        """The single strike for threshold markets."""
        if self.strike_type in THRESHOLD_TYPES and self.floor_strike is not None:
            return self.floor_strike
        if self.strike_type == "less" and self.cap_strike is not None:
            return self.cap_strike
        raise ValueError(f"{self.ticker}: no single strike for type {self.strike_type}")

    @property
    def duration_minutes(self) -> float:
        """Open-to-close length."""
        return (self.close_time - self.open_time).total_seconds() / 60.0

    @property
    def open_ts(self) -> int:
        """Unix timestamp of open."""
        return int(self.open_time.timestamp())

    @property
    def close_ts(self) -> int:
        """Unix timestamp of close."""
        return int(self.close_time.timestamp())

    def yes_wins(self) -> bool | None:
        """True if YES settled, False if NO, None if unsettled."""
        if self.result == "yes":
            return True
        if self.result == "no":
            return False
        return None

    @classmethod
    def from_record(cls, rec: dict[str, Any]) -> BracketMarket:
        """Build from a fixture record or a Kalshi market dict."""
        open_time = _parse_time(rec.get("open_time"))
        close_time = _parse_time(rec.get("close_time") or rec.get("expiration_time"))
        if open_time is None or close_time is None:
            raise ValueError(f"{rec.get('ticker')}: open_time and close_time are required")
        exp = rec.get("expiration_value")
        return cls(
            ticker=str(rec["ticker"]),
            event_ticker=str(rec.get("event_ticker", "")),
            series=str(
                rec.get("series") or rec.get("series_ticker") or rec["ticker"].split("-")[0]
            ),
            asset=str(rec.get("asset") or _asset_from_ticker(rec["ticker"])),
            strike_type=str(rec.get("strike_type", "greater")),
            floor_strike=_opt_float(rec.get("floor_strike")),
            cap_strike=_opt_float(rec.get("cap_strike")),
            open_time=open_time,
            close_time=close_time,
            result=rec.get("result") or None,
            expiration_value=_opt_float(exp) if exp not in ("", None) else None,
            volume=float(rec.get("volume") or rec.get("volume_fp") or 0.0),
        )


def _asset_from_ticker(ticker: str) -> str:
    upper = ticker.upper()
    for asset in ("BTC", "ETH", "SOL"):
        if asset in upper:
            return asset
    return "UNKNOWN"


def _opt_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    return float(v)


def _parse_time(v: Any) -> datetime | None:
    if not v:
        return None
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def load_markets(path: Path) -> list[BracketMarket]:
    """Load a fixture file of market records. Fails loudly on a missing or empty file."""
    if not path.exists():
        raise FileNotFoundError(f"market fixture not found: {path}")
    with path.open() as fh:
        records = json.load(fh)
    if not isinstance(records, list) or not records:
        raise ValueError(f"market fixture {path} is empty or not a list")
    markets = [BracketMarket.from_record(r) for r in records]
    return sorted(markets, key=lambda m: (m.close_time, m.ticker))
