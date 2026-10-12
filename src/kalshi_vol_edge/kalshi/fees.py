"""Kalshi fee schedule and the edge arithmetic that depends on it.

Formulas, per fill::

    taker fee = ceil(0.07   * C * P * (1 - P) * 10000) / 10000
    maker fee = ceil(0.0175 * C * P * (1 - P) * 10000) / 10000

where ``C`` is the contract count and ``P`` the price in dollars. Holding a contract to expiry
incurs no exit fee.

What is verified, and what is assumed
-------------------------------------
Kalshi's "Fee Rounding" API page (docs.kalshi.com/getting_started/fee_rounding, read October
2026) says the model fee is rounded up to $0.000001, the balance change is then floored onto the
member's grid ($0.0001 for direct members, $0.01 for members clearing through an FCM), and the
rounding overpayment goes into a per-order accumulator that is rebated on later fills of the
same order. Its worked example, a model fee of $0.00363825 on one contract at 5.5c, is exactly
``0.07 * 0.055 * 0.945``, which confirms the taker multiplier; the test suite reproduces it.

So the ceiling to $0.0001 above is exact for a direct member's single-fill order. It overstates
a multi-fill order by less than $0.0001 per fill (the accumulator rebates that), and it
understates an FCM-cleared account, whose fills round to whole cents before rebates. The maker
multiplier (a quarter of taker) and the half rate for index markets (S&P, Nasdaq) come from
Kalshi's fee schedule as the live system used it; the schedule PDF was not reachable when this
was re-checked, so treat those two as assumptions.

Two lessons from live trading are encoded here rather than left in a README:

* Most limit orders on thin bracket books end up crossing the spread and filling as taker.
  ``post_only`` rejections fall back to taker fills at four times the fee. Edge estimates must
  therefore be computed with :func:`taker_fee` unless a maker fill is proven after the fact.
* Fees are charged per leg. A two-leg structure (condor, ladder, statistical-arbitrage pair)
  pays two entry fees and, if it is closed early, two exit fees. :func:`multi_leg_fee`
  makes that multiplication explicit so a thin edge cannot hide it.
"""

from __future__ import annotations

import math

TAKER_MULTIPLIER = 0.07
MAKER_MULTIPLIER = 0.0175


def _fee(multiplier: float, contracts: int, price: float) -> float:
    if contracts <= 0:
        return 0.0
    if not 0.0 < price < 1.0:
        raise ValueError(f"price must be in (0, 1), got {price}")
    # Round away binary floating-point noise before the ceiling: 0.07 * 0.5 * 0.5 * 10000 is
    # 175.00000000000003 in IEEE-754 and would otherwise round *up* to 176 (a 0.57% overcharge).
    raw = round(multiplier * contracts * price * (1.0 - price) * 10000, 6)
    return math.ceil(raw) / 10000


def taker_fee(contracts: int, price: float, index: bool = False) -> float:
    """Taker fee in dollars for ``contracts`` at ``price``."""
    return _fee(TAKER_MULTIPLIER * (0.5 if index else 1.0), contracts, price)


def maker_fee(contracts: int, price: float, index: bool = False) -> float:
    """Maker fee in dollars for ``contracts`` at ``price``."""
    return _fee(MAKER_MULTIPLIER * (0.5 if index else 1.0), contracts, price)


def entry_fee(contracts: int, price: float, is_taker: bool = True, index: bool = False) -> float:
    """Entry fee for a fill; defaults to taker because that is what live fills mostly were."""
    return taker_fee(contracts, price, index) if is_taker else maker_fee(contracts, price, index)


def round_trip_fee(
    contracts: int,
    entry_price: float,
    exit_price: float,
    entry_maker: bool = False,
    exit_maker: bool = False,
    index: bool = False,
) -> float:
    """Entry plus exit fee for a position that is closed before expiry."""
    return entry_fee(contracts, entry_price, not entry_maker, index) + entry_fee(
        contracts, exit_price, not exit_maker, index
    )


def multi_leg_fee(leg_prices: list[float], contracts: int, is_taker: bool = True) -> float:
    """Total entry fee across the legs of a multi-leg structure.

    Each leg is a separate contract and is charged separately. For a condor with legs at 0.75
    and 0.75 this is twice the fee of a single directional trade at 0.75, while the maximum
    payout is still one dollar per contract pair.
    """
    return sum(entry_fee(contracts, p, is_taker) for p in leg_prices)


def breakeven_win_rate(price: float, is_taker: bool = True) -> float:
    """Win rate needed for zero expected value when buying one contract at ``price`` and
    holding to expiry.

    win  -> profit = (1 - price) - fee
    loss -> cost   = price + fee
    breakeven = cost / (profit + cost)
    """
    fee = entry_fee(1, price, is_taker)
    profit = (1.0 - price) - fee
    cost = price + fee
    return cost / (profit + cost)


def net_edge(model_p: float, price: float, contracts: int = 1, is_taker: bool = True) -> float:
    """Expected profit per contract after the entry fee: ``model_p - price - fee/contract``."""
    fee = entry_fee(contracts, price, is_taker) / max(contracts, 1)
    return model_p - price - fee


def expected_value(model_p: float, price: float, contracts: int, is_taker: bool = True) -> float:
    """Dollar EV of buying ``contracts`` at ``price`` with win probability ``model_p``."""
    fee = entry_fee(contracts, price, is_taker)
    return model_p * contracts * (1.0 - price) - (1.0 - model_p) * contracts * price - fee
