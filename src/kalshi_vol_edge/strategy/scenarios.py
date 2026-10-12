"""Monte Carlo terminal-price scenarios for a bracket chain (pure numpy, seedable).

Used to sanity-check the closed-form :func:`~kalshi_vol_edge.strategy.vol_edge.fair_value`
and to price multi-strike questions ("P(settles between A and B)") without new formulas.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from kalshi_vol_edge.kalshi.fees import entry_fee

MINUTES_PER_YEAR = 365.25 * 24 * 60


@dataclass
class SimulationResult:
    """Terminal distribution summary and per-strike probabilities."""

    mean_price: float = 0.0
    median_price: float = 0.0
    std_price: float = 0.0
    p5_price: float = 0.0
    p25_price: float = 0.0
    p75_price: float = 0.0
    p95_price: float = 0.0
    bracket_probs: dict[float, float] = field(default_factory=dict)
    best_directional: dict[str, Any] | None = None
    num_paths: int = 0
    spot_at_sim: float = 0.0
    vol_used: float = 0.0
    drift_used: float = 0.0
    minutes_simulated: float = 0.0
    compute_time_ms: float = 0.0
    simulation_error: float = 0.0


def _per_minute_vol(annual_vol: float) -> float:
    return annual_vol / math.sqrt(MINUTES_PER_YEAR)


def simulate_price_paths(
    spot: float,
    annual_vol: float,
    minutes_remaining: float,
    num_paths: int = 10000,
    drift_per_min: float = 0.0,
    seed: int | None = None,
) -> np.ndarray:
    """Terminal prices of ``num_paths`` driftless-GBM paths (one log-normal draw each)."""
    rng = np.random.default_rng(seed)
    if minutes_remaining <= 0 or annual_vol <= 0:
        return np.full(num_paths, spot)
    total_vol = _per_minute_vol(annual_vol) * math.sqrt(minutes_remaining)
    total_drift = drift_per_min * minutes_remaining
    z = rng.standard_normal(num_paths)
    return spot * np.exp((total_drift - 0.5 * total_vol**2) + total_vol * z)


def simulate_price_paths_full(
    spot: float,
    annual_vol: float,
    minutes_remaining: float,
    num_paths: int = 1000,
    step_minutes: float = 1.0,
    drift_per_min: float = 0.0,
    seed: int | None = None,
) -> np.ndarray:
    """Full paths of shape ``(num_paths, steps + 1)``; column 0 is ``spot``."""
    rng = np.random.default_rng(seed)
    steps = max(1, int(minutes_remaining / step_minutes))
    if minutes_remaining <= 0 or annual_vol <= 0:
        return np.full((num_paths, steps + 1), spot)
    step_vol = _per_minute_vol(annual_vol) * math.sqrt(step_minutes)
    z = rng.standard_normal((num_paths, steps))
    increments = (drift_per_min * step_minutes - 0.5 * step_vol**2) + step_vol * z
    log_paths = np.empty((num_paths, steps + 1))
    log_paths[:, 0] = math.log(spot)
    log_paths[:, 1:] = math.log(spot) + np.cumsum(increments, axis=1)
    return np.exp(log_paths)


def compute_bracket_probabilities(
    terminal_prices: np.ndarray, strikes: list[float]
) -> dict[float, float]:
    """Fraction of paths above each strike."""
    return {s: float(np.mean(terminal_prices > s)) for s in sorted(strikes)}


def find_mispriced_brackets(
    sim_probs: dict[float, float],
    market_prices: dict[float, dict[str, Any]],
    contracts: int = 1,
    is_taker: bool = True,
    min_edge: float = 0.05,
) -> list[dict[str, Any]]:
    """Strikes where simulated probability beats the ask by more than fees plus ``min_edge``.

    The fee is Kalshi's schedule for ``contracts`` at that ask (:func:`entry_fee`), per
    contract: 1.75c at 50c for a taker, not a flat number. ``ev_per_dollar`` is the edge per
    dollar paid (ask plus fee).
    """
    if contracts < 1:
        raise ValueError(f"contracts must be at least 1, got {contracts}")
    out = []
    for strike, p_above in sim_probs.items():
        mp = market_prices.get(strike, {})
        for side, p, ask in (
            ("yes", p_above, mp.get("yes_ask", 0)),
            ("no", 1.0 - p_above, mp.get("no_ask", 0)),
        ):
            if not 0 < ask < 1:
                continue
            fee = entry_fee(contracts, ask, is_taker) / contracts
            edge = p - ask - fee
            if edge > min_edge:
                out.append(
                    {
                        "strike": strike,
                        "side": side,
                        "sim_prob": round(p, 4),
                        "market_price": ask,
                        "fee_per_contract": round(fee, 4),
                        "edge": round(edge, 4),
                        "ev_per_dollar": round(edge / (ask + fee), 4),
                    }
                )
    out.sort(key=lambda x: x["edge"], reverse=True)
    return out


def run_full_simulation(
    spot: float,
    strikes: list[float],
    minutes_remaining: float,
    annual_vol: float,
    market_prices: dict[float, dict[str, Any]] | None = None,
    drift_per_min: float = 0.0,
    num_paths: int = 10000,
    seed: int | None = None,
) -> SimulationResult:
    """Paths -> percentiles -> per-strike probabilities -> best mispriced bracket."""
    start = time.perf_counter()
    terminal = simulate_price_paths(
        spot, annual_vol, minutes_remaining, num_paths, drift_per_min, seed
    )
    result = SimulationResult(
        mean_price=round(float(np.mean(terminal)), 2),
        median_price=round(float(np.median(terminal)), 2),
        std_price=round(float(np.std(terminal)), 2),
        p5_price=round(float(np.percentile(terminal, 5)), 2),
        p25_price=round(float(np.percentile(terminal, 25)), 2),
        p75_price=round(float(np.percentile(terminal, 75)), 2),
        p95_price=round(float(np.percentile(terminal, 95)), 2),
        num_paths=num_paths,
        spot_at_sim=spot,
        vol_used=annual_vol,
        drift_used=drift_per_min,
        minutes_simulated=minutes_remaining,
        simulation_error=float(np.std(terminal) / math.sqrt(num_paths)),
    )
    result.bracket_probs = compute_bracket_probabilities(terminal, strikes)
    mispriced = find_mispriced_brackets(result.bracket_probs, market_prices or {})
    result.best_directional = mispriced[0] if mispriced else None
    result.compute_time_ms = max((time.perf_counter() - start) * 1000, 1e-3)
    return result
