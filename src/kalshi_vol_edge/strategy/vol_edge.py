"""Realized-vs-implied volatility edge on short-dated binary brackets.

The contract pays $1 if the settlement price is at or above the strike. Every probability in
this module comes from one driftless log-normal model, with annualised volatility sigma and
``t = m / MINUTES_PER_YEAR`` for ``m`` minutes to expiry::

    P(YES settles)    = Phi(d2),  d2 = (ln(spot / strike) - sigma^2 t / 2) / (sigma sqrt(t))
                        (:func:`fair_value`; :func:`win_probability` is it, or 1 minus it)
    sigma_window      = spot * sigma * sqrt(t)                          (dollar std-dev)
    required lead     = z * sigma_window                                (entry gate)

The entry gate is stated in dollars of lead, which is the normal approximation of the same
model: over a 15-minute window ``sigma sqrt(t)`` is about 0.2%, and at the required lead the
log-normal win probability is within 0.001 of ``Phi(z)`` for sigma between 20% and 80%
(``tests/test_vol_edge.py`` pins this). The edge itself always uses the log-normal probability.

The market quote implies its own sigma (:func:`implied_vol`). The strategy tests the vol edge in
price space, ``edge = win_probability(realized sigma) - ask - fee``, which before fees is the
same test as comparing realized with implied sigma: for the side that is ahead (late and mid
arms) fair value falls as sigma rises, so a positive edge means realized < implied, and a lead
that already exists is more likely to hold than the market is paying for. The early arm buys
the side that is behind, so its edge needs realized > implied. :func:`implied_vol` backs the
implied number out of the quote for the live page.

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
from dataclasses import dataclass, field, replace
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


def fair_value(spot: float, strike: float, annual_vol: float, minutes_left: float) -> float:
    """Fair YES price: P(settlement >= strike) under the driftless log-normal model."""
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    if minutes_left <= 0 or annual_vol <= 0:
        return 1.0 if spot >= strike else 0.0
    t = minutes_left / MINUTES_PER_YEAR
    d2 = (math.log(spot / strike) - 0.5 * annual_vol**2 * t) / (annual_vol * math.sqrt(t))
    return _NORM.cdf(d2)


def win_probability(
    side: str, spot: float, strike: float, annual_vol: float, minutes_left: float
) -> float:
    """P(``side`` settles in the money): :func:`fair_value` for YES, its complement for NO."""
    p_yes = fair_value(spot, strike, annual_vol, minutes_left)
    return p_yes if side == "yes" else 1.0 - p_yes


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


@dataclass(frozen=True, slots=True)
class ArmCheck:
    """One arm's evaluation of a snapshot, including the reason it did or did not fire.

    ``required_lead`` is the z-sigma lead for the late and mid arms and the minimum drop for
    the early arm. ``edge`` is None until the price and lead checks have passed.
    """

    arm: str
    window_open: bool
    side: str
    price: float
    z_score: float
    required_lead: float
    win_prob: float
    edge: float | None = None
    decision: Decision | None = None
    reason: str = ""


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
            decision = self._check_arm(arm, snap).decision
            if decision is not None:
                out.append(decision)
        return out

    def diagnose(self, snap: MarketSnapshot) -> list[ArmCheck]:
        """Every arm's view of this snapshot: what it requires, and why it fires or not.

        This is the same code path :meth:`evaluate` uses, so the dashboard and the backtest
        can never disagree about whether an arm would have traded.
        """
        checks = []
        for arm in self.config.arms:
            check = self._check_arm(arm, snap)
            if not arm.in_window(snap.minutes_left, snap.minutes_elapsed):
                check = replace(
                    check, window_open=False, decision=None, reason="outside entry window"
                )
            checks.append(check)
        return checks

    def _check_arm(self, arm: ArmConfig, snap: MarketSnapshot) -> ArmCheck:
        if arm.name == "early":
            check = self._early(arm, snap)
        else:
            z = (
                self.config.z_late
                if arm.name == "late"
                else mid_entry_z_score(snap.minutes_elapsed)
            )
            check = self._directional(arm, snap, z)
        return check

    def _directional(self, arm: ArmConfig, snap: MarketSnapshot, z: float) -> ArmCheck:
        """Late and mid arms: trade the side currently winning if the lead clears z sigma."""
        lead = snap.lead
        need = required_lead(snap.spot, snap.annual_vol, snap.minutes_left, z)
        side = "yes" if lead >= 0 else "no"
        price = snap.yes_ask if side == "yes" else snap.no_ask
        p_win = win_probability(side, snap.spot, snap.strike, snap.annual_vol, snap.minutes_left)

        def result(reason: str, edge: float | None = None, d: Decision | None = None) -> ArmCheck:
            return ArmCheck(arm.name, True, side, price, z, need, p_win, edge, d, reason)

        if lead == 0:
            return result("no lead")
        if abs(lead) < need:
            return result(f"lead ${abs(lead):,.0f} below required ${need:,.0f}")
        if not _price_in_band(arm, price):
            return result(_band_reason(arm, price))
        contracts = size_contracts(arm.max_position_usd, arm.max_contracts, price)
        fee = entry_fee(contracts, price, arm.is_taker)
        edge = p_win - price - fee / contracts
        if edge < arm.min_edge:
            return result(_edge_reason(arm, edge), edge)
        decision = Decision(
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
        return result("fires", edge, decision)

    def _early(self, arm: ArmConfig, snap: MarketSnapshot) -> ArmCheck:
        """Early arm: fade an initial drop by buying YES (recovery to the strike)."""
        move = snap.lead
        need = self.config.early_min_move_usd
        price = snap.yes_ask
        p_win = win_probability("yes", snap.spot, snap.strike, snap.annual_vol, snap.minutes_left)

        def result(reason: str, edge: float | None = None, d: Decision | None = None) -> ArmCheck:
            return ArmCheck(arm.name, True, "yes", price, 0.0, need, p_win, edge, d, reason)

        if move > -need:
            return result(f"no drop of ${need:,.0f} to fade")
        if not _price_in_band(arm, price):
            return result(_band_reason(arm, price))
        contracts = size_contracts(arm.max_position_usd, arm.max_contracts, price)
        fee = entry_fee(contracts, price, arm.is_taker)
        edge = p_win - price - fee / contracts
        if edge < arm.min_edge:
            return result(_edge_reason(arm, edge), edge)
        decision = Decision(
            arm=arm.name,
            side="yes",
            price=price,
            contracts=contracts,
            win_prob=p_win,
            edge=edge,
            z_score=0.0,
            required_lead=need,
            fee=fee,
            reason=f"move={move:+.0f} rsi={snap.rsi if snap.rsi is not None else float('nan'):.0f}",
        )
        return result("fires", edge, decision)


def _price_in_band(arm: ArmConfig, price: float) -> bool:
    return 0.0 < price < 1.0 and arm.min_price <= price <= arm.max_price


def _band_reason(arm: ArmConfig, price: float) -> str:
    return f"price {price:.2f} outside {arm.min_price:.2f}-{arm.max_price:.2f}"


def _edge_reason(arm: ArmConfig, edge: float) -> str:
    return f"edge {edge * 100:+.1f}c below {arm.min_edge * 100:.0f}c"
