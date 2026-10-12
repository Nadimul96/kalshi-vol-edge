"""Command-line entry point: ``kve``.

    kve backtest  --fixtures data/sample [--series KXBTC15M] [--json]
    kve reconcile --dry-run [--fixtures data/sample] [--db data/kve.sqlite3]
    kve fees      --price 0.62 --contracts 10

Exit codes: 0 success, 1 unexpected error, 2 bad input (missing fixture, bad arguments).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kalshi_vol_edge import __version__
from kalshi_vol_edge.backtest.engine import (
    FixtureError,
    ModeledQuotes,
    format_report,
    load_fixture,
    run_backtest,
)
from kalshi_vol_edge.config import CredentialsError, load_settings
from kalshi_vol_edge.kalshi.fees import breakeven_win_rate, maker_fee, taker_fee
from kalshi_vol_edge.kalshi.truth import Discrepancy, reconcile_fills, summarize_discrepancies
from kalshi_vol_edge.logging import configure_logging

EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2


def _print_err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def cmd_backtest(args: argparse.Namespace) -> int:
    """Replay the strategy over a fixture directory."""
    try:
        fixture = load_fixture(args.fixtures, series=args.series)
    except FixtureError as exc:
        _print_err(f"cannot run backtest: {exc}")
        _print_err(
            "the backtest never falls back to synthetic data; provide a fixture directory "
            "with manifest.json, candles and settled markets"
        )
        return EXIT_USAGE
    if args.implied_vol is not None or args.half_spread is not None:
        if not isinstance(fixture.quotes, ModeledQuotes):
            _print_err("--implied-vol/--half-spread apply only to fixtures with modeled quotes")
            return EXIT_USAGE
        fixture.quotes = ModeledQuotes(
            implied_vol=args.implied_vol
            if args.implied_vol is not None
            else fixture.quotes.implied_vol,
            half_spread=args.half_spread
            if args.half_spread is not None
            else fixture.quotes.half_spread,
        )
    report = run_backtest(fixture)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        print(format_report(report))
    return EXIT_OK


def _load_json(path: Path) -> Any:
    if not path.exists():
        raise FixtureError(f"file not found: {path}")
    return json.loads(path.read_text())


def _print_discrepancies(discrepancies: list[Discrepancy], as_json: bool) -> None:
    counts = summarize_discrepancies(discrepancies)
    if as_json:
        print(
            json.dumps(
                {"summary": counts, "discrepancies": [d.as_dict() for d in discrepancies]},
                indent=2,
                default=str,
            )
        )
        return
    print(
        f"reconciliation: {len(discrepancies)} discrepancies "
        f"(critical={counts['critical']} warning={counts['warning']} info={counts['info']})"
    )
    for d in discrepancies:
        print(
            f"  [{d.severity:<8}] {d.ticker:<32} {d.field:<18} local={d.local_value!r} "
            f"api={d.api_value!r}"
        )
        print(f"             {d.context}")


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Reconcile local records against exchange fills (recorded fixture in --dry-run)."""
    if args.dry_run:
        root = Path(args.fixtures) / "reconcile"
        try:
            local = _load_json(root / "local_orders.json")
            api = _load_json(root / "api_fills.json")
        except FixtureError as exc:
            _print_err(f"cannot run dry-run reconcile: {exc}")
            return EXIT_USAGE
        print(
            f"dry run: {len(local)} local records vs {len(api)} recorded exchange fills from {root}"
        )
        _print_discrepancies(reconcile_fills(local, api), args.json)
        return EXIT_OK

    settings = load_settings()
    if not settings.has_credentials:
        _print_err(
            "live reconcile needs KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH; "
            "use --dry-run to run against the recorded fixture"
        )
        return EXIT_USAGE

    from kalshi_vol_edge.kalshi.client import KalshiClient
    from kalshi_vol_edge.kalshi.truth import KalshiTruth
    from kalshi_vol_edge.storage.sqlite import TradeStore

    async def _run() -> int:
        async with KalshiClient(settings) as client:
            with TradeStore(args.db) as store:
                truth = KalshiTruth(client)
                synced = await truth.sync_to_store(store)
                print(f"synced: {synced}")
                local = store.list_fills()
                fills = await truth.get_all_fills()
                _print_discrepancies(reconcile_fills(local, fills), args.json)
        return EXIT_OK

    try:
        return asyncio.run(_run())
    except CredentialsError as exc:
        _print_err(str(exc))
        return EXIT_USAGE


def cmd_fees(args: argparse.Namespace) -> int:
    """Print the fee schedule for one order."""
    p, c = args.price, args.contracts
    print(f"price={p:.2f} contracts={c}")
    print(f"  taker fee        : ${taker_fee(c, p):.4f}")
    print(f"  maker fee        : ${maker_fee(c, p):.4f}")
    print(f"  breakeven (taker): {breakeven_win_rate(p, True):.2%}")
    print(f"  breakeven (maker): {breakeven_win_rate(p, False):.2%}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """Argument parser for ``kve``."""
    parser = argparse.ArgumentParser(
        prog="kve", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--version", action="version", version=f"kve {__version__}")
    parser.add_argument("--log-level", default="WARNING")
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("backtest", help="replay the strategy over recorded fixtures")
    b.add_argument("--fixtures", required=True, help="fixture directory containing manifest.json")
    b.add_argument(
        "--series", default=None, help="market series to replay (default: first in manifest)"
    )
    b.add_argument("--json", action="store_true", help="emit JSON instead of text")
    b.add_argument(
        "--implied-vol",
        type=float,
        default=None,
        help="override the modeled quote's implied vol (sensitivity check)",
    )
    b.add_argument(
        "--half-spread",
        type=float,
        default=None,
        help="override the modeled quote's half-spread in dollars",
    )
    b.set_defaults(func=cmd_backtest)

    r = sub.add_parser("reconcile", help="compare local state with exchange fills")
    r.add_argument("--dry-run", action="store_true", help="use the recorded fixture, no network")
    r.add_argument("--fixtures", default="data/sample", help="fixture directory (dry-run)")
    r.add_argument("--db", default=None, help="SQLite path (live mode)")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_reconcile)

    f = sub.add_parser("fees", help="fee schedule for one order")
    f.add_argument("--price", type=float, required=True)
    f.add_argument("--contracts", type=int, default=1)
    f.set_defaults(func=cmd_fees)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)
    if getattr(args, "db", None) is None and args.command == "reconcile":
        args.db = load_settings().kve_db_path
    try:
        command: Callable[[argparse.Namespace], int] = args.func
        return command(args)
    except FixtureError as exc:
        _print_err(str(exc))
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
