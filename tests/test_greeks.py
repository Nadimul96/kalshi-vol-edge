"""Binary option Greeks (ported from the source system's passing suite)."""

from itertools import pairwise

from kalshi_vol_edge.strategy.greeks import (
    compute_binary_greeks,
    compute_bracket_chain,
    theta_decay_schedule,
)


def test_fair_value_itm_otm_atm():
    assert compute_binary_greeks(71000, 70000, 30, 0.60).fair_value > 0.85
    assert compute_binary_greeks(70000, 72000, 30, 0.60).fair_value < 0.15
    assert 0.40 < compute_binary_greeks(70000, 70000, 30, 0.60).fair_value < 0.60


def test_delta_positive_and_gamma_highest_atm():
    atm = compute_binary_greeks(70000, 70000, 10, 0.60)
    otm = compute_binary_greeks(70000, 71000, 10, 0.60)
    assert atm.delta > 0
    assert abs(atm.gamma) > abs(otm.gamma)


def test_gamma_explodes_near_expiry_and_gamma_zone_flag():
    g60 = compute_binary_greeks(70000, 70000, 60, 0.60)
    g5 = compute_binary_greeks(70000, 70000, 5, 0.60)
    assert abs(g5.gamma) > abs(g60.gamma)
    assert compute_binary_greeks(70050, 70000, 5, 0.60).gamma_zone is True
    assert compute_binary_greeks(70050, 70000, 30, 0.60).gamma_zone is False


def test_expiry_is_binary():
    assert compute_binary_greeks(70100, 70000, 0, 0.60).fair_value == 1.0
    assert compute_binary_greeks(69900, 70000, 0, 0.60).fair_value == 0.0


def test_higher_vol_raises_otm_value_and_edge_is_fair_minus_market():
    low = compute_binary_greeks(70000, 71000, 30, 0.40)
    high = compute_binary_greeks(70000, 71000, 30, 0.80)
    assert high.fair_value > low.fair_value
    g = compute_binary_greeks(70500, 70000, 30, 0.60, market_yes_price=0.60)
    assert abs(g.edge - (g.fair_value - 0.60)) < 1e-9


def test_bracket_chain_monotonic_and_theta_schedule_length():
    chain = compute_bracket_chain(70500, [69500, 70000, 70500, 71000, 71500], 30, 0.60)
    assert len(chain) == 5
    for a, b in pairwise(chain):
        assert a.fair_value >= b.fair_value
    sched = theta_decay_schedule(70000, 70000, 0.60, total_minutes=60)
    assert len(sched) == 60
    assert 0.40 < sched[0]["fair_value"] < 0.60
