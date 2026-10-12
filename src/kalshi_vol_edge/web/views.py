"""View models for the backtest, settlement-basis and reconciliation pages.

These turn engine output into what a template needs (rows, summary numbers, chart markup)
and are shared by the live dashboard and the static report, which is what keeps the two
visually and numerically identical.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

from markupsafe import Markup

from kalshi_vol_edge.backtest.engine import (
    BacktestReport,
    BacktestTrade,
    Fixture,
    FixtureError,
    load_fixture,
    run_backtest,
)
from kalshi_vol_edge.kalshi.truth import Discrepancy, reconcile_fills, summarize_discrepancies
from kalshi_vol_edge.web.charts import Band, RefLine, bin_values, histogram, line_chart
from kalshi_vol_edge.web.format import cents, num, usd, utc_time
from kalshi_vol_edge.web.live import LiveView

ARM_ORDER = ("early", "mid", "late")
ARM_BLURB = {
    "early": "Fades a drop of $40+ in minutes 3-6 by buying YES",
    "mid": "Trades the leading side with 7-10 min left; z rises 0.84 to 1.28",
    "late": "Trades the leading side with 3-5.5 min left at z = 1.28",
}


# ── Backtest ──────────────────────────────────────────────────────────────────


def run_fixture_backtest(fixtures: Path, series: str) -> tuple[Fixture, BacktestReport]:
    """Load a fixture and replay it. Raises :class:`FixtureError` like the CLI does."""
    fixture = load_fixture(fixtures, series=series)
    return fixture, run_backtest(fixture)


@dataclass(frozen=True)
class ArmRow:
    """Per-arm aggregate."""

    arm: str
    blurb: str
    trades: int
    wins: int
    win_rate: float | None
    net_pnl: float
    fees: float
    avg_price: float | None
    avg_model_edge: float | None


def arm_rows(trades: list[BacktestTrade]) -> list[ArmRow]:
    """One row per arm in the strategy's evaluation order, including arms that never fired."""
    rows = []
    for arm in ARM_ORDER:
        ts = [t for t in trades if t.arm == arm]
        n = len(ts)
        rows.append(
            ArmRow(
                arm=arm,
                blurb=ARM_BLURB[arm],
                trades=n,
                wins=sum(t.won for t in ts),
                win_rate=sum(t.won for t in ts) / n if n else None,
                net_pnl=sum(t.pnl for t in ts),
                fees=sum(t.fee for t in ts),
                avg_price=sum(t.price for t in ts) / n if n else None,
                avg_model_edge=sum(t.edge for t in ts) / n if n else None,
            )
        )
    return rows


def equity_chart(trades: list[BacktestTrade]) -> Markup:
    """Cumulative net P&L after each trade, in settlement order."""
    ordered = sorted(trades, key=lambda t: (t.entry_ts, t.ticker, t.arm))
    xs: list[float] = [0.0]
    ys: list[float] = [0.0]
    tips = ["Start: $0.00"]
    equity = 0.0
    for i, t in enumerate(ordered, start=1):
        equity += t.pnl
        xs.append(float(i))
        ys.append(equity)
        outcome = "won" if t.won else "lost"
        tips.append(
            f"#{i} {utc_time(t.entry_ts, with_date=True)} UTC · {t.arm} {t.side.upper()} @ "
            f"{cents(t.price)} · {outcome} {usd(t.pnl, signed=True)} · "
            f"cumulative {usd(equity, signed=True)}"
        )
    n = len(ordered)
    step = max(1, round(n / 6))
    x_ticks = [(float(i), f"#{i}") for i in range(step, n + 1, step)]
    return line_chart(
        xs,
        ys,
        tips=tips,
        x_ticks=x_ticks,
        y_format=lambda v: usd(v, 0, signed=True) if abs(v) >= 1 or v == 0 else usd(v, signed=True),
        label="Cumulative net P&L by trade",
        refs=[RefLine(0.0, "", "zero")],
        include_zero=True,
        height=240,
    )


def backtest_context(fixture: Fixture, report: BacktestReport) -> dict[str, Any]:
    """Template context for the backtest page."""
    trades = sorted(report.trades, key=lambda t: (t.entry_ts, t.ticker, t.arm))
    return {
        "report": report,
        "fixture_description": fixture.manifest.get("description", ""),
        "quote_note": fixture.manifest.get("quotes", {}).get("note", ""),
        "arms": arm_rows(trades),
        "trades": trades,
        "equity": equity_chart(trades) if trades else None,
        "span": _span(fixture),
    }


def _span(fixture: Fixture) -> str:
    first = min(m.open_time for m in fixture.markets)
    last = max(m.close_time for m in fixture.markets)
    return f"{first:%b %d %H:%M} to {last:%b %d %H:%M} UTC"


# ── Settlement basis ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BasisRow:
    """One settled market: Coinbase's last close against the BRTI settlement."""

    ticker: str
    close_time: datetime
    strike: float
    coinbase_close: float | None
    settlement: float | None
    basis: float | None
    result: str | None
    direction_flip: bool


def basis_context(fixture: Fixture, report: BacktestReport) -> dict[str, Any]:
    """Template context for the settlement-basis page."""
    by_ticker = {m.ticker: m for m in fixture.markets}
    rows = []
    for b in report.settlement_basis:
        m = by_ticker[b["ticker"]]
        rows.append(
            BasisRow(
                ticker=m.ticker,
                close_time=m.close_time,
                strike=m.strike,
                coinbase_close=b["coinbase_close"],
                settlement=b["brti_settlement"],
                basis=b["basis"],
                result=m.result,
                direction_flip=bool(b["direction_flip"]),
            )
        )
    values = [r.basis for r in rows if r.basis is not None]
    abs_values = [abs(v) for v in values]
    summary = report.basis_summary()
    return {
        "report": report,
        "rows": rows,
        "summary": summary,
        "signed_mean": sum(values) / len(values) if values else None,
        "median_abs": median(abs_values) if abs_values else None,
        "within_10": sum(v <= 10 for v in abs_values),
        "histogram": histogram(
            bin_values(values, 10.0),
            label="Distribution of Coinbase close minus BRTI settlement",
            x_format=lambda v: usd(v, 0, signed=True),
            unit="market",
        )
        if values
        else None,
    }


# ── Reconciliation ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReconcileResult:
    """Inputs and output of one reconciliation run."""

    source: str
    local: list[dict[str, Any]]
    api: list[dict[str, Any]]
    discrepancies: list[Discrepancy]

    @property
    def counts(self) -> dict[str, int]:
        """Discrepancies by severity."""
        return summarize_discrepancies(self.discrepancies)


def load_recorded_reconcile(fixtures: Path) -> ReconcileResult:
    """Reconcile the recorded local orders against the recorded exchange fills."""
    root = fixtures / "reconcile"
    paths = (root / "local_orders.json", root / "api_fills.json")
    for path in paths:
        if not path.exists():
            raise FixtureError(f"file not found: {path}")
    local, api = (json.loads(p.read_text()) for p in paths)
    return ReconcileResult(
        source=f"Recorded fixture ({root.as_posix()})",
        local=local,
        api=api,
        discrepancies=reconcile_fills(local, api),
    )


SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def reconcile_context(result: ReconcileResult) -> dict[str, Any]:
    """Template context for the reconciliation page."""
    ordered = sorted(result.discrepancies, key=lambda d: SEVERITY_ORDER.get(d.severity, 9))
    return {"recon": result, "discrepancies": ordered, "counts": result.counts}


# ── Live chart ────────────────────────────────────────────────────────────────


def spot_chart(view: LiveView) -> Markup | None:
    """Last hour of Coinbase closes, the current strike and the current market window."""
    if not view.candles:
        return None
    xs = [float(c.ts + 60) for c in view.candles]  # a candle's close is known at its end
    ys = [c.close for c in view.candles]
    tips = [f"{utc_time(x)} UTC · {usd(y)}" for x, y in zip(xs, ys, strict=True)]
    refs: list[RefLine] = []
    band = None
    current = view.current
    if current is not None:
        if current.market.strike is not None:
            refs.append(RefLine(current.market.strike, f"Strike {usd(current.market.strike)}"))
        band = Band(float(current.market.open_ts), float(current.market.close_ts), "This market")
    lo = xs[0]
    hi = max(xs[-1], band.x1 if band else xs[-1])
    first_tick = (int(lo) // 900 + 1) * 900
    x_ticks = [(float(t), utc_time(t)) for t in range(first_tick, int(hi) + 1, 900)]
    return line_chart(
        xs,
        ys,
        tips=tips,
        x_ticks=x_ticks,
        y_format=lambda v: num(v),
        label=f"{view.asset} spot, last hour, Coinbase one-minute closes",
        refs=refs,
        band=band,
        area=False,
        height=270,
    )


def now_label() -> str:
    """Generation time for footers."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
