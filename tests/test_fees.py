"""Fee schedule and fee-aware edge math."""

import math

import pytest

from kalshi_vol_edge.kalshi.fees import (
    breakeven_win_rate,
    expected_value,
    maker_fee,
    multi_leg_fee,
    net_edge,
    round_trip_fee,
    taker_fee,
)


def test_taker_fee_matches_schedule_at_50c():
    # ceil(0.07 * 1 * 0.5 * 0.5 * 10000) / 10000 = 0.0175
    assert taker_fee(1, 0.50) == 0.0175


def test_maker_fee_is_quarter_of_taker_before_rounding():
    assert maker_fee(1, 0.50) == 0.0044  # ceil(43.75)/10000
    assert maker_fee(100, 0.50) == pytest.approx(taker_fee(100, 0.50) / 4, rel=0.01)


def test_fee_is_symmetric_in_price():
    assert taker_fee(10, 0.30) == taker_fee(10, 0.70)


def test_fee_rounds_up_to_hundredth_of_cent():
    raw = 0.07 * 7 * 0.68 * 0.32
    assert taker_fee(7, 0.68) == math.ceil(raw * 10000) / 10000
    assert taker_fee(7, 0.68) >= raw


def test_fee_scales_with_contracts():
    assert abs(taker_fee(10, 0.30) - 10 * taker_fee(1, 0.30)) <= 0.001


def test_index_markets_pay_half():
    assert taker_fee(10, 0.50, index=True) == pytest.approx(taker_fee(10, 0.50) / 2, abs=1e-4)


def test_zero_contracts_is_free_and_bad_price_rejected():
    assert taker_fee(0, 0.5) == 0.0
    with pytest.raises(ValueError):
        taker_fee(1, 1.0)


def test_round_trip_doubles_taker_cost_vs_hold_to_expiry():
    hold = taker_fee(10, 0.60)
    rt = round_trip_fee(10, 0.60, 0.70)
    assert rt > hold
    assert rt == taker_fee(10, 0.60) + taker_fee(10, 0.70)


def test_multi_leg_fee_is_sum_of_legs():
    # a condor at 0.75/0.75 pays two fees for a max payout of one dollar per pair
    two_leg = multi_leg_fee([0.75, 0.75], 10)
    assert two_leg == 2 * taker_fee(10, 0.75)
    assert two_leg > taker_fee(10, 0.75)


def test_breakeven_win_rate_above_price_for_taker():
    # at 0.50 the live analysis found 51.75% breakeven with taker fees
    assert breakeven_win_rate(0.50, is_taker=True) == pytest.approx(0.5175, abs=1e-4)
    assert breakeven_win_rate(0.50, is_taker=False) == pytest.approx(0.5044, abs=1e-4)
    assert breakeven_win_rate(0.90, is_taker=True) > 0.90


def test_net_edge_and_ev_subtract_fee():
    assert net_edge(0.70, 0.60, 10) == pytest.approx(0.10 - taker_fee(10, 0.60) / 10)
    ev = expected_value(0.70, 0.60, 10)
    assert ev == pytest.approx(0.7 * 10 * 0.4 - 0.3 * 10 * 0.6 - taker_fee(10, 0.60))
