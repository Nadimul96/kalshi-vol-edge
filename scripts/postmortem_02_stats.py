"""Reproduce every number in docs/postmortems/02-anti-predictive-signal.md.

    uv run python scripts/postmortem_02_stats.py [path/to/trades.csv]

Input: one row per live trade with ``entry_price`` (dollars paid for the side bought),
``settled_in_money`` (1 if that side settled in the money, recorded at settlement even when the
position had already been exited) and ``exit_rule``. The committed file is
docs/postmortems/data/02-v6-hourly-trades.csv, cut down from the live system's trade log to
those three columns.

Null hypothesis: the market's price is a fair probability, so a contract bought at ``p_i``
settles in the money with probability ``p_i``, independently across trades. Wins are then
Poisson-binomial with mean sum(p_i) and variance sum(p_i * (1 - p_i)). The exact lower tail is
computed by convolving the Bernoulli distributions; no simulation, no third-party packages.
"""

from __future__ import annotations

import argparse
import csv
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from statistics import NormalDist

DEFAULT_CSV = (
    Path(__file__).resolve().parent.parent / "docs/postmortems/data/02-v6-hourly-trades.csv"
)


@dataclass(frozen=True)
class Trade:
    """One live trade: what was paid, whether the side bought won, how the position closed."""

    entry_price: float
    settled_in_money: bool
    exit_rule: str


@dataclass(frozen=True)
class Stats:
    """Everything the post-mortem quotes."""

    trades: int
    wins: int
    mean_price: float
    min_price: float
    max_price: float
    expected_wins: float
    variance: float
    z: float
    p_exact: float  # P(wins <= observed) under the market-implied null
    p_normal: float
    coin_flip_z: float
    coin_flip_p: float  # the wrong null: every trade a fair coin
    exits: dict[str, int]

    @property
    def sd(self) -> float:
        """Standard deviation of the win count under the market-implied null."""
        return math.sqrt(self.variance)


def load_trades(path: Path) -> list[Trade]:
    """Read the per-trade CSV."""
    with path.open(newline="") as fh:
        return [
            Trade(float(r["entry_price"]), r["settled_in_money"] == "1", r["exit_rule"])
            for r in csv.DictReader(fh)
        ]


def poisson_binomial_pmf(probs: list[float]) -> list[float]:
    """P(exactly k successes) for independent Bernoulli(p_i), k = 0..len(probs)."""
    pmf = [1.0]
    for p in probs:
        nxt = [0.0] * (len(pmf) + 1)
        for k, mass in enumerate(pmf):
            nxt[k] += mass * (1.0 - p)
            nxt[k + 1] += mass * p
        pmf = nxt
    return pmf


def compute(trades: list[Trade]) -> Stats:
    """Win count against the prices paid, plus the exit-rule breakdown."""
    probs = [t.entry_price for t in trades]
    n, wins = len(trades), sum(t.settled_in_money for t in trades)
    expected = sum(probs)
    variance = sum(p * (1 - p) for p in probs)
    z = (wins - expected) / math.sqrt(variance)
    coin = poisson_binomial_pmf([0.5] * n)
    return Stats(
        trades=n,
        wins=wins,
        mean_price=expected / n,
        min_price=min(probs),
        max_price=max(probs),
        expected_wins=expected,
        variance=variance,
        z=z,
        p_exact=sum(poisson_binomial_pmf(probs)[: wins + 1]),
        p_normal=NormalDist().cdf(z),
        coin_flip_z=(wins - n / 2) / math.sqrt(n / 4),
        coin_flip_p=sum(coin[: wins + 1]),
        exits=dict(Counter(t.exit_rule for t in trades).most_common()),
    )


def format_stats(s: Stats) -> str:
    """The report the post-mortem quotes."""
    exits = ", ".join(f"{rule} {count}" for rule, count in s.exits.items())
    return "\n".join(
        [
            f"trades                          : {s.trades}",
            f"bought side settled in the money: {s.wins} ({s.wins / s.trades:.1%})",
            f"entry price                     : mean ${s.mean_price:.4f}, "
            f"range ${s.min_price:.2f}-${s.max_price:.2f}",
            f"expected wins, sum(p)           : {s.expected_wins:.2f}",
            f"variance, sum(p(1-p))           : {s.variance:.4f} (sd {s.sd:.3f})",
            f"z                               : {s.z:.2f}",
            f"exact one-sided P(wins <= {s.wins})    : {s.p_exact:.4f} (Poisson-binomial)",
            f"normal approximation            : {s.p_normal:.4f}",
            f"two-sided, doubled              : {2 * s.p_exact:.3f}",
            f"coin-flip null (wrong baseline) : expected {s.trades / 2:.1f}, "
            f"z {s.coin_flip_z:.2f}, P(wins <= {s.wins}) {s.coin_flip_p:.1e}",
            f"exits                           : {exits}",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    """Print the statistics for a trade CSV."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv", nargs="?", type=Path, default=DEFAULT_CSV)
    args = parser.parse_args(argv)
    print(format_stats(compute(load_trades(args.csv))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
