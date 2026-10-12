"""The numbers quoted in post-mortem 02 come from the committed trade CSV via the script."""

import importlib.util
import math
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "postmortem_02_stats.py"
_spec = importlib.util.spec_from_file_location("postmortem_02_stats", SCRIPT)
assert _spec is not None and _spec.loader is not None
stats_script = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = stats_script  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(stats_script)


def test_poisson_binomial_matches_the_binomial_and_sums_to_one():
    pmf = stats_script.poisson_binomial_pmf([0.3] * 10)
    assert sum(pmf) == pytest.approx(1.0)
    assert pmf[2] == pytest.approx(math.comb(10, 2) * 0.3**2 * 0.7**8)


def test_post_mortem_numbers_reproduce_from_the_committed_csv():
    s = stats_script.compute(stats_script.load_trades(stats_script.DEFAULT_CSV))
    assert (s.trades, s.wins) == (35, 1)
    assert round(s.mean_price, 4) == 0.1443
    assert (s.min_price, s.max_price) == (0.09, 0.28)
    assert round(s.expected_wins, 2) == 5.05
    assert round(s.variance, 4) == 4.2735 and round(s.sd, 2) == 2.07
    assert round(s.z, 2) == -1.96
    assert round(s.p_exact, 3) == 0.029 and round(2 * s.p_exact, 2) == 0.06
    assert round(s.p_normal, 3) == 0.025
    assert round(s.coin_flip_z, 2) == -5.58
    assert s.exits == {
        "THESIS_BREAK": 27,
        "SETTLED": 4,
        "TRAILING_STOP": 2,
        "PROFIT_LOCK": 1,
        "ALREADY_CLOSED": 1,
    }
    assert "z                               : -1.96" in stats_script.format_stats(s)
