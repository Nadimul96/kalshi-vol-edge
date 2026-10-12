"""``kve order``: a single, guarded limit order. The only code path that places orders.

Safety rules, in the order they are applied:

1. The demo exchange is the default. Production requires ``--live``; ``KALSHI_ENVIRONMENT``
   alone never sends an order to production.
2. Inputs are validated and the order must fit ``KVE_MAX_ORDER_USD`` and, together with the
   existing position on that ticker (read from the exchange), ``KVE_MAX_POSITION_CONTRACTS``.
3. The order is summarised and confirmed. On demo, ``y`` (or ``--yes``). On production, the
   ticker must be typed at an interactive terminal; ``--yes`` is refused with ``--live``.

The dashboard never imports this module.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from kalshi_vol_edge.config import Environment, Settings
from kalshi_vol_edge.kalshi.fees import taker_fee
from kalshi_vol_edge.kalshi.truth import to_decimal


class OrderClient(Protocol):
    """What placing an order needs from :class:`~kalshi_vol_edge.kalshi.client.KalshiClient`."""

    async def get_positions(
        self, settlement_status: str = "unsettled", ticker: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def place_limit_order(
        self,
        ticker: str,
        side: str,
        price: float,
        contracts: int,
        client_order_id: str,
        post_only: bool = False,
    ) -> dict[str, Any]: ...


class OrderRefusedError(RuntimeError):
    """The order was not sent. The message says why."""


@dataclass(frozen=True)
class OrderRequest:
    """A validated order request."""

    ticker: str
    side: str
    price: float
    contracts: int
    environment: Environment

    @property
    def cost(self) -> float:
        """Premium paid if fully filled."""
        return self.price * self.contracts


def validate(ticker: str, side: str, price: float, contracts: int, live: bool) -> OrderRequest:
    """Check the inputs and pick the environment (demo unless ``live``)."""
    if not ticker:
        raise OrderRefusedError("ticker is required")
    if side not in ("yes", "no"):
        raise OrderRefusedError(f"side must be yes or no, got {side!r}")
    if not 0.01 <= price <= 0.99:
        raise OrderRefusedError(f"price must be between 0.01 and 0.99 dollars, got {price}")
    if contracts < 1:
        raise OrderRefusedError(f"contracts must be at least 1, got {contracts}")
    env = Environment.PROD if live else Environment.DEMO
    return OrderRequest(ticker, side, price, contracts, env)


def limit_violations(
    order: OrderRequest, current_contracts: Decimal, max_contracts: int, max_usd: float
) -> list[str]:
    """Reasons the order breaks the configured limits; empty when it fits."""
    problems = []
    if order.cost > max_usd + 1e-9:
        problems.append(f"order cost ${order.cost:.2f} exceeds KVE_MAX_ORDER_USD=${max_usd:.2f}")
    after = abs(current_contracts) + order.contracts
    if after > max_contracts:
        problems.append(
            f"position would be {after} contracts on {order.ticker} "
            f"(holding {abs(current_contracts)}), above KVE_MAX_POSITION_CONTRACTS={max_contracts}"
        )
    return problems


def summary(order: OrderRequest, current: Decimal, settings: Settings) -> str:
    """Human-readable confirmation text."""
    fee = taker_fee(order.contracts, order.price)
    return "\n".join(
        [
            f"environment : {order.environment.value.upper()}"
            + ("  (real money)" if order.environment == Environment.PROD else "  (paper)"),
            f"order       : buy {order.contracts} {order.side.upper()} {order.ticker} "
            f"@ ${order.price:.2f} limit",
            f"cost        : ${order.cost:.2f} + up to ${fee:.4f} taker fee",
            f"max payout  : ${order.contracts:.2f} if {order.side.upper()} settles",
            f"position    : {current} now, limit {settings.kve_max_position_contracts} contracts",
        ]
    )


async def place_order(
    order: OrderRequest,
    settings: Settings,
    client: OrderClient,
    confirm: Callable[[str], bool],
) -> dict[str, Any]:
    """Apply the position limit, ask for confirmation and send the order."""
    positions = await client.get_positions("unsettled", ticker=order.ticker)
    current = sum(
        (to_decimal(p.get("position_fp")) for p in positions if p.get("ticker") == order.ticker),
        Decimal("0"),
    )
    problems = limit_violations(
        order, current, settings.kve_max_position_contracts, settings.kve_max_order_usd
    )
    if problems:
        raise OrderRefusedError("; ".join(problems))
    if not confirm(summary(order, current, settings)):
        raise OrderRefusedError("not confirmed")
    return await client.place_limit_order(
        order.ticker,
        order.side,
        order.price,
        order.contracts,
        client_order_id=f"kve-manual-{uuid.uuid4().hex[:16]}",
    )
