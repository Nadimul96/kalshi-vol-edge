"""Binary (cash-or-nothing) option Greeks for Kalshi bracket strikes.

Fair value is ``Phi(d2)``; delta, gamma and vega follow from differentiating it. Unlike vanilla
options the gamma of a binary explodes at the money as expiry approaches, which is why the live
system refused to hold at-the-money contracts inside the last fifteen minutes (``gamma_zone``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from scipy.stats import norm

MINUTES_PER_YEAR = 365.25 * 24 * 60


@dataclass
class BinaryGreeks:
    """Greeks plus context for one binary option (one Kalshi strike)."""

    fair_value: float = 0.5
    market_price: float = 0.5
    edge: float = 0.0
    delta: float = 0.0  # dP/dS per $1
    gamma: float = 0.0  # d2P/dS2
    theta: float = 0.0  # dP per minute of time passing
    vega: float = 0.0  # dP per +1 vol point (0.01)
    spot: float = 0.0
    strike: float = 0.0
    minutes_left: float = 0.0
    annual_vol: float = 0.60
    side: str = "yes"
    moneyness: float = 0.0  # ln(S/K)
    z_score: float = 0.0  # d2
    gamma_zone: bool = False

    @property
    def is_itm(self) -> bool:
        """YES is in the money when spot is above the strike."""
        return self.spot > self.strike

    @property
    def distance_dollars(self) -> float:
        """spot - strike."""
        return self.spot - self.strike


def _d2(spot: float, strike: float, sigma: float, t: float) -> float:
    return (math.log(spot / strike) - 0.5 * sigma**2 * t) / (sigma * math.sqrt(t))


def compute_binary_greeks(
    spot: float,
    strike: float,
    minutes_left: float,
    annual_vol: float,
    market_yes_price: float = 0.0,
    market_no_price: float = 0.0,
) -> BinaryGreeks:
    """Fair value and Greeks of ``pays $1 if S_T >= strike``.

    Theta is computed numerically with a one-minute bump; vega with a one-point vol bump.
    """
    g = BinaryGreeks(spot=spot, strike=strike, minutes_left=minutes_left, annual_vol=annual_vol)
    if spot <= 0 or strike <= 0 or minutes_left <= 0 or annual_vol <= 0:
        g.fair_value = 1.0 if spot >= strike else 0.0
        return g

    t = minutes_left / MINUTES_PER_YEAR
    sqrt_t = math.sqrt(t)
    sigma = annual_vol
    d2 = _d2(spot, strike, sigma, t)
    p_above = float(norm.cdf(d2))

    g.fair_value = p_above
    g.moneyness = math.log(spot / strike)
    g.z_score = d2

    if market_yes_price > 0:
        g.market_price, g.edge, g.side = market_yes_price, p_above - market_yes_price, "yes"
    elif market_no_price > 0:
        g.market_price, g.edge, g.side = market_no_price, (1 - p_above) - market_no_price, "no"

    phi_d2 = float(norm.pdf(d2))
    g.delta = phi_d2 / (spot * sigma * sqrt_t)
    g.gamma = -phi_d2 * d2 / (spot**2 * sigma**2 * t)

    t_minus = max(t - 1 / MINUTES_PER_YEAR, 1e-12)
    g.theta = float(norm.cdf(_d2(spot, strike, sigma, t_minus))) - p_above

    g.vega = float(norm.cdf(_d2(spot, strike, sigma + 0.01, t))) - p_above

    vol_window = sigma * sqrt_t * spot
    g.gamma_zone = abs(spot - strike) < 0.5 * vol_window and minutes_left < 15
    return g


def compute_bracket_chain(
    spot: float,
    strikes: list[float],
    minutes_left: float,
    annual_vol: float,
    market_prices: dict[float, dict] | None = None,
) -> list[BinaryGreeks]:
    """Greeks for every strike of one event, ascending by strike."""
    market_prices = market_prices or {}
    chain = []
    for strike in sorted(strikes):
        mp = market_prices.get(strike, {})
        ya = mp.get("yes_ask", 0)
        na = mp.get("no_ask", 0)
        chain.append(
            compute_binary_greeks(
                spot,
                strike,
                minutes_left,
                annual_vol,
                market_yes_price=ya,
                market_no_price=na if ya <= 0 else 0,
            )
        )
    return chain


def theta_decay_schedule(
    spot: float, strike: float, annual_vol: float, total_minutes: int = 60
) -> list[dict]:
    """Minute-by-minute fair value and Greeks from ``total_minutes`` down to 1."""
    return [
        {
            "minutes_left": m,
            "fair_value": round(g.fair_value, 4),
            "delta": round(g.delta, 6),
            "gamma": round(g.gamma, 8),
            "theta_per_min": round(g.theta, 6),
            "gamma_zone": g.gamma_zone,
        }
        for m in range(total_minutes, 0, -1)
        for g in (compute_binary_greeks(spot, strike, float(m), annual_vol),)
    ]
