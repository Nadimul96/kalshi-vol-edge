"""``kve doctor``: say exactly what works, what is missing, and what is optional.

Every check returns a :class:`Check`. Only ``fail`` makes the command exit non-zero; a
missing Kalshi key is ``optional`` because everything except the account view and order
placement works without one.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import Literal

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from kalshi_vol_edge.backtest.engine import FixtureError, load_fixture
from kalshi_vol_edge.config import Settings

Status = Literal["ok", "optional", "warn", "fail"]


@dataclass(frozen=True)
class Check:
    """One line of the doctor report."""

    name: str
    status: Status
    detail: str


def check_python() -> Check:
    """Python 3.11 or newer."""
    v = sys.version_info
    ok = v >= (3, 11)
    return Check("python", "ok" if ok else "fail", f"{v.major}.{v.minor}.{v.micro}")


def check_fixtures(settings: Settings) -> Check:
    """The sample fixture loads (backtest, report and reconcile --dry-run need it)."""
    try:
        fx = load_fixture(settings.kve_fixtures_dir)
    except FixtureError as exc:
        return Check("fixtures", "fail", str(exc))
    return Check("fixtures", "ok", f"{settings.kve_fixtures_dir}: {len(fx.markets)} {fx.series}")


def check_credentials(settings: Settings) -> Check:
    """Key id and private key: both set, the file exists and holds an RSA key."""
    key_id, path = settings.kalshi_api_key_id, settings.kalshi_private_key_path
    if not key_id and not path:
        return Check(
            "kalshi key",
            "optional",
            "not set; needed only for the account view and kve order "
            "(Kalshi's demo exchange gives free paper-trading keys)",
        )
    if not key_id:
        return Check(
            "kalshi key", "fail", "KALSHI_PRIVATE_KEY_PATH is set but KALSHI_API_KEY_ID is not"
        )
    if not path:
        return Check(
            "kalshi key", "fail", "KALSHI_API_KEY_ID is set but KALSHI_PRIVATE_KEY_PATH is not"
        )
    if not path.exists():
        return Check("kalshi key", "fail", f"KALSHI_PRIVATE_KEY_PATH does not exist: {path}")
    try:
        key = load_pem_private_key(path.read_bytes(), password=None)
    except (ValueError, TypeError) as exc:
        return Check("kalshi key", "fail", f"private key could not be parsed: {type(exc).__name__}")
    if not isinstance(key, rsa.RSAPrivateKey):
        return Check("kalshi key", "fail", "private key is not an RSA key")
    env = settings.kalshi_environment.value
    return Check("kalshi key", "ok", f"RSA key loaded; account reads use the {env} exchange")


async def _get(client: httpx.AsyncClient, name: str, url: str, params: dict[str, str]) -> Check:
    try:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return Check(name, "warn", f"HTTP {exc.response.status_code} from {url}")
    except httpx.HTTPError as exc:
        return Check(name, "warn", f"unreachable ({type(exc).__name__}); live view will show this")
    return Check(name, "ok", f"reachable ({resp.elapsed.total_seconds() * 1000:.0f} ms)")


async def check_network(settings: Settings, timeout_s: float = 6.0) -> list[Check]:
    """Public endpoints the live dashboard uses. Failures are warnings, not errors."""
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        return list(
            await asyncio.gather(
                _get(
                    client,
                    "kalshi market data",
                    f"{settings.kalshi_market_data_url}/markets",
                    {"series_ticker": settings.kalshi_series, "status": "open", "limit": "1"},
                ),
                _get(
                    client, "coinbase", f"{settings.coinbase_base_url}/products/BTC-USD/ticker", {}
                ),
            )
        )


async def check_account(settings: Settings) -> Check:
    """An authenticated read on the configured environment, only when a key is set."""
    from kalshi_vol_edge.kalshi.client import KalshiClient

    try:
        async with KalshiClient(settings) as client:
            await client.get_balance()
    except httpx.HTTPStatusError as exc:
        return Check("kalshi account", "fail", f"HTTP {exc.response.status_code}: key rejected?")
    except httpx.HTTPError as exc:
        return Check("kalshi account", "warn", f"unreachable ({type(exc).__name__})")
    return Check("kalshi account", "ok", f"authenticated on {settings.kalshi_environment.value}")


def run_doctor(settings: Settings, offline: bool = False) -> list[Check]:
    """All checks, in display order."""
    checks = [check_python(), check_fixtures(settings), check_credentials(settings)]
    if not offline:
        checks += asyncio.run(check_network(settings))
        if checks[2].status == "ok":
            checks.append(asyncio.run(check_account(settings)))
    checks.append(
        Check(
            "order limits",
            "ok",
            f"max {settings.kve_max_position_contracts} contracts per ticker, "
            f"max ${settings.kve_max_order_usd:.2f} per order",
        )
    )
    return checks
