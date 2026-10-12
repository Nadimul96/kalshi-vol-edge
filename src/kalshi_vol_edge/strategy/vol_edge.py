"""Realized-vs-implied volatility edge on short-dated binary brackets.

The contract pays $1 if the settlement price is at or above the strike. Under a driftless
log-normal model with annualised volatility sigma and ``m`` minutes to expiry::

    sigma_window      = spot * sigma * sqrt(m / MINUTES_PER_YEAR)        (dollar std-dev)
    P(YES settles)    = Phi(lead / sigma_window)   where lead = spot - strike
    required lead     = z * sigma_window           so that P(hold) >= Phi(z)

The market quote implies its own sigma (:func:`implied_vol`). The *vol edge* is the gap between
the sigma realized over the last hour of one-minute candles and the sigma the quote implies.
When realized vol is lower than implied, a lead that already exists is more likely to hold
than the market is paying for, and the YES (or NO) contract is cheap.

Three arms trade the same 15-minute window with different time-to-expiry and confidence:

* **late**  (3.0-5.5 min left): z = 1.282 (90%). Entry price up to 0.97. Taker fill assumed.
* **mid**   (7-10 min left):    z interpolates 0.842 -> 1.282 as minute 5 -> 8 elapses.
  Entry price 0.55-0.80.
* **early** (3-6 min elapsed):  fades a drop of >= $40 by buying YES in 0.15-0.75. Small size.

Every arm is independent and idempotent per event: at most one order per arm per event. All
of the above are pure functions of a :class:`MarketSnapshot`; :class:`VolEdgeStrategy` holds
only configuration, which is what makes the live loop and the backtest share this file.

Fees are included in the edge test using the taker schedule; see ``kalshi/fees.py`` for why.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import NormalDist

import numpy as np

from kalshi_vol_edge.kalshi.fees import entry_fee

MINUTES_PER_YEAR = 365.25 * 24 * 60
_NORM = NormalDist()

Z_90 = 1.282  # Phi^-1(0.90)
Z_80 = 0.842  # Phi^-1(0.80)


# ── Pure pricing functions ─────────────────────────────────────────────────────


def realized_vol_annualized(
    closes: list[float] | np.ndarray,
    floor: float = 0.0,
    cap: float = float("inf"),
    min_points: int = 10,
) -> float | None:
    """Annualised realised vol from one-minute closes (std of log returns, ddof=1).

    Returns None when fewer than ``min_points`` returns are available. The caller decides
    whether to fall back to a default; this function does not invent a number.
    """
    arr = np.asarray(closes, dtype=float)
    if arr.size < min_points + 1:
        return None
    log_returns = np.diff(np.log(arr))
    per_min = float(np.std(log_returns, ddof=1))
    annual = per_min * math.sqrt(MINUTES_PER_YEAR)
    return max(floor, min(cap, annual))


def dollar_sigma(spot: float, annual_vol: float, minutes_left: float) -> float:
    """Dollar standard deviation of the price change over ``minutes_left``."""
    if minutes_left <= 0 or annual_vol <= 0 or spot <= 0:
        return 0.0
    return spot * annual_vol * math.sqrt(minutes_left / MINUTES_PER_YEAR)


def required_lead(spot: float, annual_vol: float, minutes_left: float, z: float) -> float:
    """Dollar lead over the strike needed so that P(lead holds to expiry) >= Phi(z)."""
    return z * dollar_sigma(spot, annual_vol, minutes_left)


def win_probability(lead: float, spot: float, annual_vol: float, minutes_left: float) -> float:
    """P(lead holds) = Phi(lead / sigma_window). At expiry: 1 if lead > 0 else 0."""
    sigma = dollar_sigma(spot, annual_vol, minutes_left)
    if sigma <= 0:
        return 1.0 if lead > 0 else 0.0
    return _NORM.cdf(lead / sigma)


def fair_value(spot: float, strike: float, annual_vol: float, minutes_left: float) -> float:
    """Fair YES price: P(settlement >= strike) under the driftless log-normal model."""
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    if minutes_left <= 0 or annual_vol <= 0:
        return 1.0 if spot >= strike else 0.0
    t = minutes_left / MINUTES_PER_YEAR
    d2 = (math.log(spot / strike) - 0.5 * annual_vol**2 * t) / (annual_vol * math.sqrt(t))
    return _NORM.cdf(d2)


def implied_vol(
    yes_price: float,
    spot: float,
    strike: float,
    minutes_left: float,
    lo: float = 0.01,
    hi: float = 5.0,
    tol: float = 1e-6,
) -> float | None:
    """Annualised vol at which :func:`fair_value` equals ``yes_price`` (bisection).

    For an in-the-money contract (spot > strike) fair value *decreases* with vol; out of the
    money it *increases*. Bisection handles both by checking the direction at the bounds.
    Returns None when the price is outside what any vol in ``[lo, hi]`` can produce, which is
    the honest answer for 0.01/0.99 quotes near expiry.
    """
    if not 0.0 < yes_price < 1.0 or minutes_left <= 0:
        return None
    f_lo = fair_value(spot, strike, lo, minutes_left) - yes_price
    f_hi = fair_value(spot, strike, hi, minutes_left) - yes_price
    if f_lo == 0:
        return lo
    if f_hi == 0:
        return hi
    if (f_lo > 0) == (f_hi > 0):
        return None
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        f_mid = fair_value(spot, strike, mid, minutes_left) - yes_price
        if abs(f_mid) < tol or hi - lo < tol:
            return mid
        if (f_mid > 0) == (f_lo > 0):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def mid_entry_z_score(
    minutes_elapsed: float,
    start_minute: float = 5.0,
    end_minute: float = 8.0,
    z_start: float = Z_80,
    z_end: float = Z_90,
) -> float:
    """Linearly interpolate the mid-arm confidence from z_start at minute 5 to z_end at minute 8."""
    clamped = max(start_minute, min(end_minute, minutes_elapsed))
    t = (clamped - start_minute) / (end_minute - start_minute)
    return z_start + t * (z_end - z_start)


def size_contracts(max_position_usd: float, max_contracts: int, price: float) -> int:
    """Largest contract count within both the dollar and count limits (at least one)."""
    return min(max_contracts, max(1, int(max_position_usd / price)))


# ── Snapshot / decision types ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Everything the strategy may look at for one decision. No I/O behind these fields."""

    event_ticker: str
    ticker: str
    asset: str
    strike: float
    spot: float
    minutes_left: float
    minutes_elapsed: float
    annual_vol: float
    yes_ask: float
    no_ask: float
    rsi: float | None = None

    @property
    def lead(self) -> float:
        """spot - strike; positive means YES is currently winning."""
        return self.spot - self.strike


@dataclass(frozen=True, slots=True)
class Decision:
    """An actionable entry. One per arm per event."""

    arm: str
    side: str  # yes | no
    price: float
    contracts: int
    win_prob: float
    edge: float  # win_prob - price - fee per contract
    z_score: float
    required_lead: float
    fee: float
    reason: str


@dataclass(frozen=True)
class ArmConfig:
    """Entry window and sizing for one arm."""

    name: str
    max_position_usd: float
    max_contracts: int
    min_price: float
    max_price: float
    min_edge: float
    # window expressed either as minutes-left (late/mid) or minutes-elapsed (early)
    min_minutes_left: float | None = None
    max_minutes_left: float | None = None
    min_minutes_elapsed: float | None = None
    max_minutes_elapsed: float | None = None
    is_taker: bool = True

    def in_window(self, minutes_left: float, minutes_elapsed: float) -> bool:
        """True when the snapshot falls inside this arm's entry window."""
        if self.min_minutes_left is not None and self.max_minutes_left is not None:
            return self.min_minutes_left <= minutes_left <= self.max_minutes_left
        if self.min_minutes_elapsed is not None and self.max_minutes_elapsed is not None:
            return self.min_minutes_elapsed <= minutes_elapsed <= self.max_minutes_elapsed
        return False


@dataclass(frozen=True)
class StrategyConfig:
    """Parameters of the live three-arm BTC/ETH configuration (March 2026)."""

    late: ArmConfig = field(
        default_factory=lambda: ArmConfig(
            name="late",
            max_position_usd=20.0,
            max_contracts=40,
            min_price=0.01,
            max_price=0.97,
            min_edge=0.01,
            min_minutes_left=3.0,
            max_minutes_left=5.5,
        )
    )
    mid: ArmConfig = field(
        default_factory=lambda: ArmConfig(
            name="mid",
            max_position_usd=5.0,
            max_contracts=10,
            min_price=0.55,
            max_price=0.80,
            min_edge=0.01,
            min_minutes_left=7.0,
            max_minutes_left=10.0,
        )
    )
    early: ArmConfig = field(
        default_factory=lambda: ArmConfig(
            name="early",
            max_position_usd=2.0,
            max_contracts=6,
            min_price=0.15,
            max_price=0.75,
            min_edge=0.0,
            min_minutes_elapsed=3.0,
            max_minutes_elapsed=6.0,
        )
    )
    z_late: float = Z_90
    early_min_move_usd: float = 40.0
    default_vol: float = 0.40
    vol_floor: float = 0.20
    vol_cap: float = 0.80

    @property
    def arms(self) -> tuple[ArmConfig, ...]:
        """All arms in evaluation order."""
        return (self.early, self.mid, self.late)


class VolEdgeStrategy:
    """Stateless decision function over :class:`MarketSnapshot`.

    ``evaluate`` returns at most one :class:`Decision` per arm whose window is open and whose
    thresholds pass. Idempotency (one order per arm per event) is the caller's responsibility:
    pass the set of arms already traded on this event in ``traded_arms``.
    """

    def __init__(self, config: StrategyConfig | None = None) -> None:
        self.config = config or StrategyConfig()

    def clamp_vol(self, annual_vol: float | None) -> float:
        """Apply the configured floor/cap; use the default when the estimate is unavailable."""
        if annual_vol is None:
            return self.config.default_vol
        return max(self.config.vol_floor, min(self.config.vol_cap, annual_vol))

    def evaluate(
        self, snap: MarketSnapshot, traded_arms: frozenset[str] = frozenset()
    ) -> list[Decision]:
        """Return the decisions for every arm that fires on this snapshot."""
        out: list[Decision] = []
        for arm in self.config.arms:
            if arm.name in traded_arms or not arm.in_window(
                snap.minutes_left, snap.minutes_elapsed
            ):
                continue
            decision = self._evaluate_arm(arm, snap)
            if decision is not None:
                out.append(decision)
        return out

    def _evaluate_arm(self, arm: ArmConfig, snap: MarketSnapshot) -> Decision | None:
        if arm.name == "early":
            return self._early(arm, snap)
        z = self.config.z_late if arm.name == "late" else mid_entry_z_score(snap.minutes_elapsed)
        return self._directional(arm, snap, z)

    def _directional(self, arm: ArmConfig, snap: MarketSnapshot, z: float) -> Decision | None:
        """Late and mid arms: trade the side currently winning if the lead clears z sigma."""
        lead = snap.lead
        if lead == 0:
            return None
        need = required_lead(snap.spot, snap.annual_vol, snap.minutes_left, z)
        if abs(lead) < need:
            return None
        side = "yes" if lead > 0 else "no"
        price = snap.yes_ask if side == "yes" else snap.no_ask
        if not 0.0 < price < 1.0 or price < arm.min_price or price > arm.max_price:
            return None
        p_win = win_probability(abs(lead), snap.spot, snap.annual_vol, snap.minutes_left)
        contracts = size_contracts(arm.max_position_usd, arm.max_contracts, price)
        fee = entry_fee(contracts, price, arm.is_taker)
        edge = p_win - price - fee / contracts
        if edge < arm.min_edge:
            return None
        return Decision(
            arm=arm.name,
            side=side,
            price=price,
            contracts=contracts,
            win_prob=p_win,
            edge=edge,
            z_score=z,
            required_lead=need,
            fee=fee,
            reason=f"lead={lead:+.0f} need={need:.0f} P={p_win:.3f} vol={snap.annual_vol:.2f}",
        )

    def _early(self, arm: ArmConfig, snap: MarketSnapshot) -> Decision | None:
        """Early arm: fade an initial drop by buying YES (recovery to the strike)."""
        move = snap.lead
        if move > -self.config.early_min_move_usd:
            return None
        price = snap.yes_ask
        if not 0.0 < price < 1.0 or price < arm.min_price or price > arm.max_price:
            return None
        p_win = win_probability(move, snap.spot, snap.annual_vol, snap.minutes_left)
        contracts = size_contracts(arm.max_position_usd, arm.max_contracts, price)
        fee = entry_fee(contracts, price, arm.is_taker)
        edge = p_win - price - fee / contracts
        return Decision(
            arm=arm.name,
            side="yes",
            price=price,
            contracts=contracts,
            win_prob=p_win,
            edge=edge,
            z_score=0.0,
            required_lead=self.config.early_min_move_usd,
            fee=fee,
            reason=f"move={move:+.0f} rsi={snap.rsi if snap.rsi is not None else float('nan'):.0f}",
        )
