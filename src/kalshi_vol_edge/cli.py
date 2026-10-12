"""Command-line entry point: ``kve``.

    kve backtest  --fixtures data/sample [--series KXBTC15M] [--json]
    kve reconcile --dry-run [--fixtures data/sample] [--db data/kve.sqlite3]
    kve fees      --price 0.62 --contracts 10
    kve dashboard [--host 127.0.0.1] [--port 8000]
    kve report    --fixtures data/sample --out site
    kve doctor    [--offline]
    kve order     --ticker T --side yes --price 0.55 --contracts 2 [--live]

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
from kalshi_vol_edge.kalshi.reconcile import Discrepancy, reconcile_orders, summarize_discrepancies
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


def _print_status(msg: str, as_json: bool) -> None:
    """Progress lines go to stderr in --json mode so stdout stays parseable."""
    print(msg, file=sys.stderr if as_json else sys.stdout)


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
    """Reconcile local orders against exchange fills (recorded fixture in --dry-run)."""
    if args.dry_run:
        root = Path(args.fixtures) / "reconcile"
        try:
            local = _load_json(root / "local_orders.json")
            api = _load_json(root / "api_fills.json")
        except FixtureError as exc:
            _print_err(f"cannot run dry-run reconcile: {exc}")
            return EXIT_USAGE
        _print_status(
            f"dry run: {len(local)} local records vs {len(api)} recorded exchange fills "
            f"from {root}",
            args.json,
        )
        _print_discrepancies(reconcile_orders(local, api), args.json)
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
                fills = await truth.get_all_fills()
                # Reconcile what we *recorded* (orders) against what the exchange says, before
                # the sync copies the exchange's fills into the store for history.
                local = store.list_orders()
                _print_status(
                    f"live: {len(local)} local orders vs {len(fills)} exchange fills", args.json
                )
                _print_discrepancies(reconcile_orders(local, fills), args.json)
                synced = await truth.sync_to_store(store, fills)
                _print_status(f"synced: {synced}", args.json)
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


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Serve the web dashboard (live markets, backtest, basis, reconciliation, account)."""
    import uvicorn

    from kalshi_vol_edge.web.app import create_app

    print(f"kve dashboard on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    """Render the backtest, basis and reconciliation pages as a static site."""
    from kalshi_vol_edge.web.site import build_site

    try:
        written = build_site(Path(args.fixtures), Path(args.out))
    except FixtureError as exc:
        _print_err(f"cannot build report: {exc}")
        return EXIT_USAGE
    for path in written:
        print(f"wrote {path}")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check the environment and say what is missing."""
    from kalshi_vol_edge.doctor import run_doctor

    checks = run_doctor(load_settings(), offline=args.offline)
    width = max(len(c.name) for c in checks)
    for c in checks:
        print(f"  {c.status:<8} {c.name:<{width}}  {c.detail}")
    failed = [c for c in checks if c.status == "fail"]
    print(f"\n{len(failed)} problem(s)" if failed else "\nready")
    return EXIT_USAGE if failed else EXIT_OK


def cmd_order(args: argparse.Namespace) -> int:
    """Place one guarded limit order: demo by default, --live plus typed confirmation for prod."""
    import httpx

    from kalshi_vol_edge.config import Environment
    from kalshi_vol_edge.kalshi.client import KalshiClient
    from kalshi_vol_edge.trading import OrderRefusedError, place_order, validate

    try:
        order = validate(args.ticker, args.side, args.price, args.contracts, args.live)
    except OrderRefusedError as exc:
        _print_err(str(exc))
        return EXIT_USAGE
    if args.live and args.yes:
        _print_err(
            "--yes cannot be used with --live; a live order is confirmed by typing its ticker"
        )
        return EXIT_USAGE
    if args.live and not sys.stdin.isatty():
        _print_err("a live order must be confirmed at an interactive terminal")
        return EXIT_USAGE
    settings = load_settings().for_environment(order.environment)
    if not settings.has_credentials:
        _print_err(
            f"placing an order needs KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH for the "
            f"{order.environment.value} exchange. Kalshi's demo exchange is free for paper "
            "trading: create an account there, generate an API key, and see .env.example"
        )
        return EXIT_USAGE

    def confirm(text: str) -> bool:
        print(text)
        if order.environment == Environment.PROD:
            typed = input(f"Type {order.ticker} to place this LIVE order: ")
            return typed.strip() == order.ticker
        if args.yes:
            return True
        return input("Place this order on the demo exchange? [y/N] ").strip().lower() in (
            "y",
            "yes",
        )

    async def _run() -> dict[str, Any]:
        async with KalshiClient(settings) as client:
            return await place_order(order, settings, client, confirm)

    try:
        result = asyncio.run(_run())
    except OrderRefusedError as exc:
        _print_err(f"order not sent: {exc}")
        return EXIT_USAGE
    except CredentialsError as exc:
        _print_err(str(exc))
        return EXIT_USAGE
    except httpx.HTTPStatusError as exc:
        _print_err(f"the exchange rejected the order: HTTP {exc.response.status_code}")
        return EXIT_ERROR
    print(
        f"sent to {order.environment.value}: order {result.get('order_id')} "
        f"status={result.get('status')}"
    )
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

    d = sub.add_parser("dashboard", help="serve the web dashboard")
    d.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost only)")
    d.add_argument("--port", type=int, default=8000)
    d.set_defaults(func=cmd_dashboard)

    rp = sub.add_parser("report", help="render the static report (GitHub Pages)")
    rp.add_argument("--fixtures", default="data/sample", help="fixture directory")
    rp.add_argument("--out", default="site", help="output directory")
    rp.set_defaults(func=cmd_report)

    dr = sub.add_parser("doctor", help="check setup: fixtures, network, optional Kalshi key")
    dr.add_argument("--offline", action="store_true", help="skip network checks")
    dr.set_defaults(func=cmd_doctor)

    o = sub.add_parser(
        "order", help="place one limit order (demo exchange unless --live, always confirmed)"
    )
    o.add_argument("--ticker", required=True)
    o.add_argument("--side", required=True, choices=["yes", "no"])
    o.add_argument("--price", type=float, required=True, help="limit price in dollars")
    o.add_argument("--contracts", type=int, default=1)
    o.add_argument("--live", action="store_true", help="send to the production exchange")
    o.add_argument("--yes", action="store_true", help="skip the demo confirmation prompt")
    o.set_defaults(func=cmd_order)
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
