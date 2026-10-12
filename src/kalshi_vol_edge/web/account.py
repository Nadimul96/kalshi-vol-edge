"""Read-only account view for the dashboard, available when a Kalshi key is configured.

Nothing here writes: no orders, no store sync. Reconciliation compares the orders recorded in
the local database, opened with SQLite's read-only mode, against the exchange's fills. Order
placement is deliberately CLI-only (``kve order``).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from kalshi_vol_edge.config import Settings
from kalshi_vol_edge.kalshi.reconcile import reconcile_orders
from kalshi_vol_edge.kalshi.truth import cents_to_decimal, to_decimal
from kalshi_vol_edge.storage.sqlite import TradeStore
from kalshi_vol_edge.web.live import describe_error
from kalshi_vol_edge.web.views import ReconcileResult

RECENT_FILLS = 25


class AccountReader(Protocol):
    """The read-only subset of :class:`~kalshi_vol_edge.kalshi.client.KalshiClient` used here."""

    async def get_balance(self) -> dict[str, Any]: ...

    async def get_positions(self, settlement_status: str = "unsettled") -> list[dict[str, Any]]: ...

    async def get_fills(self, **params: Any) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class Position:
    """An open position, from the exchange."""

    ticker: str
    contracts: Decimal
    exposure: Decimal
    realized_pnl: Decimal
    fees_paid: Decimal


@dataclass
class AccountSnapshot:
    """What the account page shows. ``error`` is set instead of raising."""

    environment: str
    configured: bool
    balance: Decimal | None = None
    portfolio_value: Decimal | None = None
    positions: list[Position] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    reconcile: ReconcileResult | None = None
    local_db_note: str = ""
    error: str | None = None


def _position(p: dict[str, Any]) -> Position:
    return Position(
        ticker=str(p.get("ticker", "")),
        contracts=to_decimal(p.get("position_fp")),
        exposure=to_decimal(p.get("market_exposure_dollars")),
        realized_pnl=to_decimal(p.get("realized_pnl_dollars")),
        fees_paid=to_decimal(p.get("fees_paid_dollars")),
    )


def _local_orders(settings: Settings) -> tuple[list[dict[str, Any]], str]:
    path = settings.kve_db_path
    try:
        with TradeStore(path, read_only=True) as store:
            orders = store.list_orders()
    except FileNotFoundError:
        return [], f"No local database at {path}; every exchange fill shows as unlogged."
    return orders, f"Local database {path} opened read-only ({len(orders)} orders)."


async def load_account(settings: Settings, client: AccountReader | None) -> AccountSnapshot:
    """Fetch balance, open positions and fills, and reconcile them against local state."""
    env = settings.kalshi_environment.value
    if client is None or not settings.has_credentials:
        return AccountSnapshot(environment=env, configured=False)
    try:
        balance, positions, fills = await asyncio.gather(
            client.get_balance(), client.get_positions("unsettled"), client.get_fills()
        )
    except Exception as exc:  # shown on the page; credentials are never part of the message
        return AccountSnapshot(environment=env, configured=True, error=describe_error(exc))
    local, note = _local_orders(settings)
    recent = sorted(fills, key=lambda f: str(f.get("created_time", "")), reverse=True)
    open_positions = [
        _position(p) for p in positions if to_decimal(p.get("position_fp")) != Decimal("0")
    ]
    return AccountSnapshot(
        environment=env,
        configured=True,
        balance=cents_to_decimal(balance.get("balance")),
        portfolio_value=cents_to_decimal(balance.get("portfolio_value")),
        positions=open_positions,
        fills=recent[:RECENT_FILLS],
        reconcile=ReconcileResult(
            source=f"Live account ({env}, read-only)",
            local=local,
            api=fills,
            discrepancies=reconcile_orders(local, fills),
        ),
        local_db_note=note,
    )
