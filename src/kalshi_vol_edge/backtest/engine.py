"""Replay the three-arm strategy minute by minute over recorded markets and candles.

Design rules
------------
* **Fail loudly.** A missing directory, manifest, candle file or market file raises
  :class:`FixtureError` with the path. Nothing is synthesized to fill a gap. The live system
  once printed a "+10,793,499 % return" from a silent fallback to generated data; that class of
  bug is excluded here by construction.
* **No look-ahead.** The spot at decision minute ``t`` is the close of the candle that ended at
  ``t``. Realized vol uses the sixty closes before ``t``. The settlement result is read only
  when the trade is scored.
* **Deterministic.** No randomness anywhere in the replay; the same fixture always gives the
  same report, which a test pins.
* **Quotes are declared, not assumed.** Entry prices come from a :class:`QuoteSource`. The
  sample fixture has no recorded order books, so its manifest declares a
  :class:`ModeledQuotes` source and the report prints that at the top. Win rate and
  settlement-basis statistics do not depend on quotes; P&L does.

Settlement basis
----------------
For every market the engine also records ``coinbase_close - brti_settlement`` using the last
completed Coinbase candle before close. That number is the irreducible noise floor for any
strategy that prices off Coinbase: when the strategy's lead over the strike is smaller than
the basis, the sign of the outcome is decided by the index composition, not by the model.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Protocol

from kalshi_vol_edge.data.markets import THRESHOLD_TYPES, BracketMarket, load_markets
from kalshi_vol_edge.data.prices import RecordedFeed
from kalshi_vol_edge.strategy.indicators import TechnicalEngine
from kalshi_vol_edge.strategy.vol_edge import (
    Decision,
    MarketSnapshot,
    StrategyConfig,
    VolEdgeStrategy,
    fair_value,
    realized_vol_annualized,
)


class FixtureError(RuntimeError):
    """A fixture is missing or malformed. The backtest refuses to run."""


# ── Quote sources ──────────────────────────────────────────────────────────────


class QuoteSource(Protocol):
    """Yields (yes_ask, no_ask) for a market at a decision minute."""

    @property
    def description(self) -> str:
        """Human-readable label for reports."""
        ...

    def quote(
        self, ticker: str, at_ts: int, spot: float, strike: float, minutes_left: float
    ) -> tuple[float, float] | None:
        """Return asks in dollars, or None if no quote is available for this minute."""
        ...


@dataclass
class ModeledQuotes:
    """Quotes modeled as fair value at a fixed implied vol plus a half-spread, in cents.

    This is the explicit stand-in for an order book that was not recorded. It is labeled as
    such in every report. The strategy's edge against these quotes is therefore exactly the
    realized-vs-implied vol gap, which is the thesis under test, but the dollar P&L should be
    read as "P&L under the declared quote model", not as a fill history.
    """

    implied_vol: float = 0.40
    half_spread: float = 0.02

    @property
    def description(self) -> str:
        """Human-readable label for reports."""
        return (
            f"MODELED (no recorded order books): fair value at implied vol "
            f"{self.implied_vol:.0%} + {self.half_spread * 100:.0f}c half-spread"
        )

    def quote(
        self, ticker: str, at_ts: int, spot: float, strike: float, minutes_left: float
    ) -> tuple[float, float] | None:
        """Model the asks from the fixed implied vol."""
        fv = fair_value(spot, strike, self.implied_vol, minutes_left)
        yes_ask = min(0.99, max(0.01, round(fv + self.half_spread, 2)))
        no_ask = min(0.99, max(0.01, round((1.0 - fv) + self.half_spread, 2)))
        return yes_ask, no_ask


@dataclass
class RecordedQuotes:
    """Quotes from a recorded ``quotes.jsonl`` (``{ticker, ts, yes_ask, no_ask}`` per line).

    A quote is used for a decision minute only if it was recorded within ``max_age_s`` before
    that minute. Missing quotes mean no trade, never a modeled fill.
    """

    quotes: dict[str, list[tuple[int, float, float]]]
    max_age_s: int = 60

    @property
    def description(self) -> str:
        """Human-readable label for reports."""
        n = sum(len(v) for v in self.quotes.values())
        return f"RECORDED order-book snapshots ({n} quotes, max age {self.max_age_s}s)"

    @classmethod
    def from_file(cls, path: Path, max_age_s: int = 60) -> RecordedQuotes:
        """Load a JSONL quote file."""
        if not path.exists():
            raise FixtureError(f"quotes file not found: {path}")
        quotes: dict[str, list[tuple[int, float, float]]] = {}
        with path.open() as fh:
            for line in fh:
                if not line.strip():
                    continue
                rec = json.loads(line)
                quotes.setdefault(rec["ticker"], []).append(
                    (int(rec["ts"]), float(rec["yes_ask"]), float(rec["no_ask"]))
                )
        for v in quotes.values():
            v.sort()
        return cls(quotes, max_age_s)

    def quote(
        self, ticker: str, at_ts: int, spot: float, strike: float, minutes_left: float
    ) -> tuple[float, float] | None:
        """Most recent recorded quote within ``max_age_s``."""
        best: tuple[int, float, float] | None = None
        for ts, ya, na in self.quotes.get(ticker, []):
            if ts <= at_ts and at_ts - ts <= self.max_age_s:
                best = (ts, ya, na)
            elif ts > at_ts:
                break
        return (best[1], best[2]) if best else None


# ── Fixture loading ────────────────────────────────────────────────────────────


@dataclass
class Fixture:
    """Everything a backtest run needs, loaded and validated."""

    root: Path
    manifest: dict[str, Any]
    asset: str
    feed: RecordedFeed
    markets: list[BracketMarket]
    series: str
    quotes: QuoteSource


def load_fixture(root: Path | str, series: str | None = None) -> Fixture:
    """Load and validate a fixture directory. Raises :class:`FixtureError` on any problem."""
    root = Path(root)
    if not root.is_dir():
        raise FixtureError(f"fixture directory not found: {root}")
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        raise FixtureError(f"fixture manifest not found: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        raise FixtureError(f"fixture manifest is not valid JSON: {manifest_path}: {exc}") from exc

    for key in ("asset", "candles", "markets", "quotes"):
        if key not in manifest:
            raise FixtureError(f"fixture manifest missing required key {key!r}: {manifest_path}")
    asset = str(manifest["asset"])

    candles_file = root / manifest["candles"].get("file", "")
    try:
        feed = RecordedFeed.from_file(candles_file, asset)
    except (FileNotFoundError, ValueError) as exc:
        raise FixtureError(str(exc)) from exc

    series_entries = {k: v for k, v in manifest["markets"].items() if isinstance(v, dict)}
    if not series_entries:
        raise FixtureError(f"fixture manifest declares no market series: {manifest_path}")
    if series is None:
        series = next(iter(series_entries))
    if series not in series_entries:
        raise FixtureError(f"series {series!r} not in fixture; available: {sorted(series_entries)}")
    markets_file = root / series_entries[series].get("file", "")
    try:
        markets = load_markets(markets_file)
    except (FileNotFoundError, ValueError) as exc:
        raise FixtureError(str(exc)) from exc
    markets = [m for m in markets if m.series == series]
    if not markets:
        raise FixtureError(f"no markets with series {series!r} in {markets_file}")

    unsettled = [m.ticker for m in markets if m.yes_wins() is None]
    if unsettled:
        raise FixtureError(
            f"{len(unsettled)} markets have no settlement result, e.g. {unsettled[0]}"
        )

    first_ts, last_ts = feed.coverage(asset)
    for m in markets:
        if m.open_ts - 3600 < first_ts or m.close_ts > last_ts + 60:
            raise FixtureError(
                f"candles do not cover market {m.ticker} (need {m.open_ts - 3600}..{m.close_ts}, "
                f"have {first_ts}..{last_ts})"
            )

    q = manifest["quotes"]
    source = q.get("source")
    quotes: QuoteSource
    if source == "modeled":
        quotes = ModeledQuotes(float(q.get("implied_vol", 0.40)), float(q.get("half_spread", 0.02)))
    elif source == "recorded":
        quotes = RecordedQuotes.from_file(
            root / q.get("file", "quotes.jsonl"), int(q.get("max_age_s", 60))
        )
    else:
        raise FixtureError(f"unknown quotes.source {source!r} in {manifest_path}")

    return Fixture(root, manifest, asset, feed, markets, series, quotes)


# ── Replay ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BacktestTrade:
    """One simulated entry, held to settlement."""

    ticker: str
    event_ticker: str
    arm: str
    side: str
    entry_ts: int
    minutes_left: float
    spot: float
    strike: float
    price: float
    contracts: int
    fee: float
    win_prob: float
    edge: float
    annual_vol: float
    won: bool
    pnl: float  # after fee

    @staticmethod
    def score(decision: Decision, won: bool) -> float:
        """Hold-to-expiry P&L: no exit fee on Kalshi."""
        if won:
            return decision.contracts * (1.0 - decision.price) - decision.fee
        return -(decision.contracts * decision.price) - decision.fee


@dataclass
class BacktestReport:
    """Aggregate statistics of one run."""

    fixture: str
    series: str
    asset: str
    quote_source: str
    markets: int
    decision_minutes: int
    trades: list[BacktestTrade] = field(default_factory=list)
    settlement_basis: list[dict[str, Any]] = field(default_factory=list)

    @property
    def trade_count(self) -> int:
        """Number of simulated entries."""
        return len(self.trades)

    @property
    def wins(self) -> int:
        """Entries that settled in our favour."""
        return sum(1 for t in self.trades if t.won)

    @property
    def win_rate(self) -> float | None:
        """wins / trades, or None with no trades."""
        return self.wins / self.trade_count if self.trades else None

    @property
    def gross_pnl(self) -> float:
        """P&L before fees."""
        return sum(t.pnl + t.fee for t in self.trades)

    @property
    def fees(self) -> float:
        """Total entry fees."""
        return sum(t.fee for t in self.trades)

    @property
    def net_pnl(self) -> float:
        """P&L after fees."""
        return sum(t.pnl for t in self.trades)

    @property
    def capital_deployed(self) -> float:
        """Sum of entry costs."""
        return sum(t.price * t.contracts for t in self.trades)

    @property
    def max_drawdown(self) -> float:
        """Largest peak-to-trough fall of cumulative net P&L in settlement order."""
        peak = equity = 0.0
        worst = 0.0
        for t in sorted(self.trades, key=lambda x: (x.entry_ts, x.ticker, x.arm)):
            equity += t.pnl
            peak = max(peak, equity)
            worst = max(worst, peak - equity)
        return worst

    def by_arm(self) -> dict[str, dict[str, Any]]:
        """Per-arm trade count, wins and net P&L."""
        out: dict[str, dict[str, Any]] = {}
        for t in self.trades:
            b = out.setdefault(t.arm, {"trades": 0, "wins": 0, "net_pnl": 0.0, "fees": 0.0})
            b["trades"] += 1
            b["wins"] += int(t.won)
            b["net_pnl"] += t.pnl
            b["fees"] += t.fee
        return out

    def basis_summary(self) -> dict[str, Any]:
        """Statistics of Coinbase-close minus BRTI-settlement across markets."""
        vals = [b["basis"] for b in self.settlement_basis if b["basis"] is not None]
        if not vals:
            return {"markets": 0}
        abs_vals = sorted(abs(v) for v in vals)
        flips = sum(1 for b in self.settlement_basis if b.get("direction_flip"))
        return {
            "markets": len(vals),
            "mean_abs_basis": sum(abs_vals) / len(abs_vals),
            "median_abs_basis": median(abs_vals),
            "max_abs_basis": abs_vals[-1],
            "direction_flips": flips,
        }

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable summary (trades included)."""
        return {
            "fixture": self.fixture,
            "series": self.series,
            "asset": self.asset,
            "quote_source": self.quote_source,
            "markets": self.markets,
            "decision_minutes": self.decision_minutes,
            "trade_count": self.trade_count,
            "wins": self.wins,
            "win_rate": self.win_rate,
            "gross_pnl": round(self.gross_pnl, 4),
            "fees": round(self.fees, 4),
            "net_pnl": round(self.net_pnl, 4),
            "capital_deployed": round(self.capital_deployed, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "by_arm": self.by_arm(),
            "settlement_basis": self.basis_summary(),
            "trades": [asdict(t) for t in self.trades],
        }


def run_backtest(
    fixture: Fixture, config: StrategyConfig | None = None, lookback_minutes: int = 60
) -> BacktestReport:
    """Replay every market in the fixture through the strategy."""
    strategy = VolEdgeStrategy(config)
    report = BacktestReport(
        fixture=str(fixture.root),
        series=fixture.series,
        asset=fixture.asset,
        quote_source=fixture.quotes.description,
        markets=len(fixture.markets),
        decision_minutes=0,
    )

    for market in fixture.markets:
        if market.strike_type not in THRESHOLD_TYPES:
            continue  # bracket ("between") markets are outside this strategy
        won_yes = market.yes_wins()
        assert won_yes is not None  # load_fixture rejects unsettled markets
        strike = market.strike
        traded: set[str] = set()

        # Settlement basis: last completed Coinbase candle before close vs BRTI settlement.
        cb_close = fixture.feed.spot(fixture.asset, market.close_ts)
        basis = (
            None
            if cb_close is None or market.expiration_value is None
            else (cb_close - market.expiration_value)
        )
        report.settlement_basis.append(
            {
                "ticker": market.ticker,
                "coinbase_close": cb_close,
                "brti_settlement": market.expiration_value,
                "basis": basis,
                "direction_flip": (cb_close is not None and ((cb_close >= strike) != won_yes)),
            }
        )

        for t in range(market.open_ts + 60, market.close_ts, 60):
            spot = fixture.feed.spot(fixture.asset, t)
            if spot is None:
                continue
            report.decision_minutes += 1
            minutes_left = (market.close_ts - t) / 60.0
            minutes_elapsed = (t - market.open_ts) / 60.0

            history = fixture.feed.candles(fixture.asset, t - lookback_minutes * 60, t)
            closes = [c.close for c in history]
            vol = strategy.clamp_vol(realized_vol_annualized(closes))

            q = fixture.quotes.quote(market.ticker, t, spot, strike, minutes_left)
            if q is None:
                continue
            yes_ask, no_ask = q
            rsi = TechnicalEngine.from_candles(history).rsi(14) if len(history) >= 15 else None

            snap = MarketSnapshot(
                event_ticker=market.event_ticker,
                ticker=market.ticker,
                asset=fixture.asset,
                strike=strike,
                spot=spot,
                minutes_left=minutes_left,
                minutes_elapsed=minutes_elapsed,
                annual_vol=vol,
                yes_ask=yes_ask,
                no_ask=no_ask,
                rsi=rsi,
            )
            for d in strategy.evaluate(snap, frozenset(traded)):
                traded.add(d.arm)
                won = won_yes if d.side == "yes" else not won_yes
                report.trades.append(
                    BacktestTrade(
                        ticker=market.ticker,
                        event_ticker=market.event_ticker,
                        arm=d.arm,
                        side=d.side,
                        entry_ts=t,
                        minutes_left=minutes_left,
                        spot=spot,
                        strike=strike,
                        price=d.price,
                        contracts=d.contracts,
                        fee=d.fee,
                        win_prob=d.win_prob,
                        edge=d.edge,
                        annual_vol=vol,
                        won=won,
                        pnl=BacktestTrade.score(d, won),
                    )
                )
    return report


def format_report(report: BacktestReport) -> str:
    """Plain-text report."""
    lines = [
        "kalshi-vol-edge backtest",
        f"  fixture          : {report.fixture}",
        f"  series / asset   : {report.series} / {report.asset}",
        f"  quotes           : {report.quote_source}",
        f"  markets replayed : {report.markets}  (decision minutes: {report.decision_minutes})",
        "",
        f"  trades           : {report.trade_count}",
    ]
    if report.trade_count:
        lines += [
            f"  wins / win rate  : {report.wins} / {report.win_rate:.1%}",
            f"  capital deployed : ${report.capital_deployed:,.2f}",
            f"  gross P&L        : ${report.gross_pnl:+,.2f}",
            f"  fees (taker)     : ${report.fees:,.2f}",
            f"  net P&L          : ${report.net_pnl:+,.2f}",
            f"  max drawdown     : ${report.max_drawdown:,.2f}",
            "",
            "  by arm:",
        ]
        for arm, b in sorted(report.by_arm().items()):
            wr = b["wins"] / b["trades"]
            lines.append(
                f"    {arm:<6} trades={b['trades']:<3} win_rate={wr:6.1%} "
                f"net=${b['net_pnl']:+.2f} fees=${b['fees']:.2f}"
            )
    else:
        lines.append("  (no arm fired on any decision minute)")
    bs = report.basis_summary()
    if bs.get("markets"):
        lines += [
            "",
            "  settlement basis (Coinbase last 1m close - BRTI settlement):",
            f"    markets={bs['markets']}  mean|basis|=${bs['mean_abs_basis']:.2f}  "
            f"median|basis|=${bs['median_abs_basis']:.2f}  max|basis|=${bs['max_abs_basis']:.2f}",
            "    direction flips (Coinbase side of strike != settled side): "
            f"{bs['direction_flips']}",
        ]
    return "\n".join(lines)
