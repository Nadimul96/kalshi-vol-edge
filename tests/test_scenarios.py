"""Monte Carlo scenario engine (ported from the source system's passing suite)."""

import numpy as np

from kalshi_vol_edge.strategy.scenarios import (
    compute_bracket_probabilities,
    find_mispriced_brackets,
    run_full_simulation,
    simulate_price_paths,
    simulate_price_paths_full,
)
from kalshi_vol_edge.strategy.vol_edge import fair_value


def test_terminal_prices_centered_and_seeded_deterministic():
    a = simulate_price_paths(70000, 0.60, 60, num_paths=50000, seed=42)
    b = simulate_price_paths(70000, 0.60, 60, num_paths=50000, seed=42)
    assert np.array_equal(a, b)
    assert abs(float(np.mean(a)) - 70000) / 70000 < 0.01


def test_vol_and_time_widen_distribution_and_drift_shifts_it():
    base = simulate_price_paths(70000, 0.60, 60, 10000, seed=1)
    assert np.std(simulate_price_paths(70000, 0.90, 60, 10000, seed=1)) > np.std(base)
    assert np.std(base) > np.std(simulate_price_paths(70000, 0.60, 10, 10000, seed=1))
    assert np.mean(simulate_price_paths(70000, 0.60, 60, 50000, 0.0001, seed=1)) > np.mean(
        simulate_price_paths(70000, 0.60, 60, 50000, 0.0, seed=1)
    )


def test_bracket_probabilities_monotonic_and_agree_with_closed_form():
    terminal = simulate_price_paths(70000, 0.60, 30, 200000, seed=42)
    probs = compute_bracket_probabilities(terminal, [69000, 69500, 70000, 70500, 71000])
    prev = 1.0
    for s, p in probs.items():
        assert p <= prev + 0.01
        prev = p
        assert abs(p - fair_value(70000, s, 0.60, 30)) < 0.01


def test_full_paths_shape_and_simulation_result():
    paths = simulate_price_paths_full(70000, 0.60, 60, 100, 1.0, seed=42)
    assert paths.shape == (100, 61) and np.allclose(paths[:, 0], 70000)
    result = run_full_simulation(70000, [69500, 70000, 70500], 30, 0.60, num_paths=5000, seed=3)
    assert result.num_paths == 5000 and len(result.bracket_probs) == 3
    assert result.compute_time_ms > 0 and result.simulation_error > 0


def test_mispriced_brackets_require_edge_over_fee():
    probs = {70000.0: 0.80}
    assert find_mispriced_brackets(probs, {70000.0: {"yes_ask": 0.60}})[0]["side"] == "yes"
    assert find_mispriced_brackets(probs, {70000.0: {"yes_ask": 0.78}}) == []
