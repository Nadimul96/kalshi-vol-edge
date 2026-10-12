"""Technical indicators over one-minute candles. Pure numpy; no network.

The live system used these only as a veto and for logging (RSI on the early arm). They are kept
because the realized-vol estimate and the candle bookkeeping live here, and because the live
loop and the backtest must compute them identically.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from kalshi_vol_edge.data.prices import Candle

MINUTES_PER_YEAR = 365.25 * 24 * 60


@dataclass
class TechnicalSignal:
    """Indicator values plus a weighted composite in [-1, +1]."""

    rsi_signal: float = 0.0
    bollinger_signal: float = 0.0
    momentum_signal: float = 0.0
    vwap_signal: float = 0.0
    ma_cross_signal: float = 0.0
    composite: float = 0.0
    confidence: float = 0.0
    rsi: float = 50.0
    bollinger_pct: float = 0.5
    vwap: float = 0.0
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    current_price: float = 0.0
    price_1m_change: float = 0.0
    price_5m_change: float = 0.0
    volume_ratio: float = 1.0
    realized_vol_1h: float = 0.0
    atr: float = 0.0
    direction: str = "neutral"
    strength: str = "weak"


class TechnicalEngine:
    """Indicator calculator over arrays of closes/highs/lows/opens/volumes."""

    def __init__(self) -> None:
        self.closes: np.ndarray = np.array([], dtype=float)
        self.highs: np.ndarray = np.array([], dtype=float)
        self.lows: np.ndarray = np.array([], dtype=float)
        self.opens: np.ndarray = np.array([], dtype=float)
        self.volumes: np.ndarray = np.array([], dtype=float)

    @classmethod
    def from_candles(cls, candles: list[Candle]) -> TechnicalEngine:
        """Build from ascending candles."""
        eng = cls()
        eng.load(candles)
        return eng

    def load(self, candles: list[Candle]) -> None:
        """Replace the candle arrays."""
        self.closes = np.array([c.close for c in candles], dtype=float)
        self.highs = np.array([c.high for c in candles], dtype=float)
        self.lows = np.array([c.low for c in candles], dtype=float)
        self.opens = np.array([c.open for c in candles], dtype=float)
        self.volumes = np.array([c.volume for c in candles], dtype=float)

    def rsi(self, period: int = 14) -> float:
        """Relative Strength Index (0-100) over the last ``period`` changes."""
        if len(self.closes) < period + 1:
            return 50.0
        deltas = np.diff(self.closes[-(period + 1) :])
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        avg_gain = float(np.mean(gains))
        avg_loss = float(np.mean(losses))
        if avg_loss <= 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return float(100 - 100 / (1 + rs))

    def bollinger(
        self, period: int = 20, num_std: float = 2.0
    ) -> tuple[float, float, float, float]:
        """(middle, upper, lower, %b) with %b clipped to [0, 1]."""
        if len(self.closes) < period:
            price = float(self.closes[-1]) if len(self.closes) else 0.0
            return price, price, price, 0.5
        window = self.closes[-period:]
        middle = float(np.mean(window))
        std = float(np.std(window, ddof=1))
        upper, lower = middle + num_std * std, middle - num_std * std
        price = float(self.closes[-1])
        width = upper - lower
        pct_b = (price - lower) / width if width > 0 else 0.5
        return middle, upper, lower, float(np.clip(pct_b, 0, 1))

    def ema(self, period: int) -> float:
        """Exponential moving average over all loaded closes."""
        if len(self.closes) < period:
            return float(self.closes[-1]) if len(self.closes) else 0.0
        k = 2 / (period + 1)
        val = float(self.closes[0])
        for p in self.closes[1:]:
            val = (float(p) - val) * k + val
        return val

    def vwap(self, periods: int = 60) -> float:
        """Volume-weighted average of typical price over the last ``periods`` candles."""
        n = min(periods, len(self.closes))
        if n == 0:
            return 0.0
        typical = (self.highs[-n:] + self.lows[-n:] + self.closes[-n:]) / 3
        vol = float(np.sum(self.volumes[-n:]))
        if vol <= 0:
            return float(np.mean(typical))
        return float(np.sum(typical * self.volumes[-n:]) / vol)

    def atr(self, period: int = 14) -> float:
        """Average true range in dollars."""
        n = min(period, len(self.closes) - 1)
        if n < 1:
            return 0.0
        prev_close = self.closes[-(n + 1) : -1]
        tr = np.maximum(
            self.highs[-n:] - self.lows[-n:],
            np.maximum(np.abs(self.highs[-n:] - prev_close), np.abs(self.lows[-n:] - prev_close)),
        )
        return float(np.mean(tr))

    def volume_ratio(self, short: int = 5, long: int = 30) -> float:
        """Recent volume relative to the longer average (>1 = above average)."""
        if len(self.volumes) < long:
            return 1.0
        recent = float(np.mean(self.volumes[-short:]))
        avg = float(np.mean(self.volumes[-long:]))
        return recent / avg if avg > 0 else 1.0

    def realized_vol(self, periods: int = 60) -> float | None:
        """Annualised realised vol over the last ``periods`` closes; None if too few."""
        n = min(periods, len(self.closes))
        if n < 5:
            return None
        log_returns = np.diff(np.log(self.closes[-n:]))
        return float(np.std(log_returns, ddof=1)) * math.sqrt(MINUTES_PER_YEAR)

    def momentum(self, minutes: int = 5) -> float:
        """Percent change over the last ``minutes`` candles."""
        n = min(minutes, len(self.closes) - 1)
        if n < 1:
            return 0.0
        return float((self.closes[-1] - self.closes[-(n + 1)]) / self.closes[-(n + 1)] * 100)

    def compute_signal(self) -> TechnicalSignal:
        """Weighted composite of RSI, Bollinger, VWAP, momentum and EMA cross."""
        if len(self.closes) < 20:
            return TechnicalSignal()
        price = float(self.closes[-1])
        sig = TechnicalSignal(current_price=price)

        sig.rsi = self.rsi(14)
        if sig.rsi < 30:
            sig.rsi_signal = 0.5 + (30 - sig.rsi) / 60
        elif sig.rsi > 70:
            sig.rsi_signal = -0.5 - (sig.rsi - 70) / 60
        else:
            sig.rsi_signal = (sig.rsi - 50) / 40 * 0.2

        _, _, _, pct_b = self.bollinger(20, 2.0)
        sig.bollinger_pct = pct_b
        if pct_b < 0.1:
            sig.bollinger_signal = 0.6
        elif pct_b > 0.9:
            sig.bollinger_signal = -0.6
        else:
            sig.bollinger_signal = (0.5 - pct_b) * 0.4

        sig.price_1m_change = self.momentum(1)
        sig.price_5m_change = self.momentum(5)
        sig.momentum_signal = float(np.clip(sig.price_5m_change / 0.15, -1, 1)) * 0.5

        sig.vwap = self.vwap(60)
        if sig.vwap > 0:
            vwap_diff = (price - sig.vwap) / sig.vwap * 100
            sig.vwap_signal = float(np.clip(-vwap_diff / 0.2, -1, 1)) * 0.3

        sig.ema_fast, sig.ema_slow = self.ema(9), self.ema(21)
        if sig.ema_slow > 0:
            cross = (sig.ema_fast - sig.ema_slow) / sig.ema_slow * 100
            sig.ma_cross_signal = float(np.clip(cross / 0.1, -1, 1)) * 0.4

        sig.volume_ratio = self.volume_ratio(5, 30)
        vol_boost = min(sig.volume_ratio - 1.0, 1.0) * 0.3 if sig.volume_ratio > 1.2 else 0.0

        sig.realized_vol_1h = self.realized_vol(60) or 0.0
        sig.atr = self.atr(14)

        raw = (
            0.25 * sig.rsi_signal
            + 0.20 * sig.bollinger_signal
            + 0.20 * sig.vwap_signal
            + 0.15 * sig.momentum_signal
            + 0.20 * sig.ma_cross_signal
        )
        sign = 1 if raw > 0 else -1
        sig.composite = float(np.clip(raw + vol_boost * sign, -1, 1))

        signals = [
            sig.rsi_signal,
            sig.bollinger_signal,
            sig.momentum_signal,
            sig.vwap_signal,
            sig.ma_cross_signal,
        ]
        agree = max(sum(1 for s in signals if s > 0.1), sum(1 for s in signals if s < -0.1))
        sig.confidence = agree / len(signals)

        sig.direction = (
            "bullish" if sig.composite > 0.15 else "bearish" if sig.composite < -0.15 else "neutral"
        )
        sig.strength = (
            "strong"
            if abs(sig.composite) > 0.4
            else "moderate"
            if abs(sig.composite) > 0.2
            else "weak"
        )
        return sig
