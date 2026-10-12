"""Technical indicators (ported from the source system's passing suite)."""

import numpy as np

from kalshi_vol_edge.data.prices import Candle
from kalshi_vol_edge.strategy.indicators import TechnicalEngine


def _engine(prices, volumes=None) -> TechnicalEngine:
    volumes = volumes or [100.0] * len(prices)
    candles = [
        Candle(ts=60 * i, low=p * 0.999, high=p * 1.001, open=p, close=p, volume=v)
        for i, (p, v) in enumerate(zip(prices, volumes, strict=True))
    ]
    return TechnicalEngine.from_candles(candles)


def test_rsi_regimes():
    assert _engine([70000 + i * 100 for i in range(30)]).rsi(14) > 65
    assert _engine([72000 - i * 100 for i in range(30)]).rsi(14) < 35
    assert 35 < _engine([70000 + (i % 2) * 10 - 5 for i in range(30)]).rsi(14) < 65
    assert _engine([1.0, 2.0]).rsi(14) == 50.0  # too short


def test_bollinger_and_ema():
    rng = np.random.default_rng(42)
    prices = (70000 + np.cumsum(rng.standard_normal(100) * 50)).tolist()
    middle, upper, lower, pct_b = _engine(prices).bollinger(20, 2.0)
    assert upper > middle > lower and 0 <= pct_b <= 1
    up = _engine([70000 + i * 50 for i in range(30)])
    assert 70000 < up.ema(9) < up.closes[-1]


def test_vwap_momentum_volume_ratio():
    assert _engine([100, 200, 300], [1000, 100, 100]).vwap(3) < 200
    assert _engine([70000 + i * 50 for i in range(30)]).momentum(5) > 0
    assert _engine([70000] * 30, [100] * 25 + [500] * 5).volume_ratio(5, 25) > 2.0


def test_composite_signal_in_range_and_realized_vol():
    rng = np.random.default_rng(42)
    prices = (70000 + np.cumsum(rng.standard_normal(100) * 50)).tolist()
    sig = _engine(prices).compute_signal()
    assert -1.0 <= sig.composite <= 1.0 and 0.0 <= sig.confidence <= 1.0
    assert sig.direction in ("bullish", "bearish", "neutral")
    returns = rng.standard_normal(60) * 0.0008
    vol = _engine((70000 * np.exp(np.cumsum(returns))).tolist()).realized_vol(60)
    assert vol is not None and 0.20 < vol < 2.0
    assert _engine([1.0, 2.0, 3.0]).realized_vol(60) is None
