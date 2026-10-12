"""Turn a strategy decision into an exchange order, idempotently.

The idempotency key is the deterministic ``client_order_id`` from
:func:`~kalshi_vol_edge.kalshi.order_ids.build_client_order_id`:
``{strategy}_{arm}_{asset}_{event_ticker}``. The same decision on the same event always produces
the same key, so

* the local store rejects a second submission before it reaches the network, and
* if the process dies after the exchange accepted the order but before the local write, the
  retry is rejected by the exchange (duplicate ``client_order_id``) instead of double-filling.

The key also encodes the strategy attribution that the truth layer parses back out of fills with
:func:`~kalshi_vol_edge.kalshi.order_ids.parse_client_order_id`. Because the key names the event,
not the market, an arm trades at most once per event even when the event has many strikes; the
backtest (``backtest/engine.py``) applies the same scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from kalshi_vol_edge.kalshi.client import KalshiClient
from kalshi_vol_edge.kalshi.order_ids import build_client_order_id
from kalshi_vol_edge.logging import get_logger
from kalshi_vol_edge.storage.sqlite import TradeStore
from kalshi_vol_edge.strategy.vol_edge import Decision

logger = get_logger(__name__)


@dataclass(frozen=True)
class SubmitResult:
    """Outcome of one submission attempt."""

    client_order_id: str
    status: str  # submitted | duplicate | dry_run | rejected
    platform_order_id: str | None = None
    detail: str = ""


class Executor:
    """Submits decisions through the client and records them in the store."""

    def __init__(
        self,
        client: KalshiClient | None,
        store: TradeStore,
        strategy: str = "vol_edge_15m",
        dry_run: bool = True,
    ) -> None:
        self.client = client
        self.store = store
        self.strategy = strategy
        self.dry_run = dry_run or client is None

    async def submit(
        self, decision: Decision, ticker: str, event_ticker: str, asset: str
    ) -> SubmitResult:
        """Place the order unless the same arm already traded this event."""
        key = build_client_order_id(self.strategy, decision.arm, asset, event_ticker)
        if self.store.has_order(key):
            logger.info("executor_duplicate", client_order_id=key)
            return SubmitResult(key, "duplicate", detail="already submitted for this event/arm")

        if self.dry_run:
            self.store.upsert_order(
                key,
                ticker,
                decision.side,
                decision.price,
                decision.contracts,
                status="dry_run",
                strategy=self.strategy,
                arm=decision.arm,
            )
            self.store.commit()
            logger.info(
                "executor_dry_run",
                client_order_id=key,
                side=decision.side,
                price=decision.price,
                contracts=decision.contracts,
            )
            return SubmitResult(key, "dry_run")

        # Record intent first so a crash after the network call leaves a trace to reconcile.
        self.store.upsert_order(
            key,
            ticker,
            decision.side,
            decision.price,
            decision.contracts,
            status="submitting",
            strategy=self.strategy,
            arm=decision.arm,
        )
        self.store.commit()
        try:
            order: dict[str, Any] = await self.client.place_limit_order(  # type: ignore[union-attr]
                ticker,
                decision.side,
                decision.price,
                decision.contracts,
                client_order_id=key,
            )
        except httpx.HTTPStatusError as exc:
            self.store.upsert_order(
                key,
                ticker,
                decision.side,
                decision.price,
                decision.contracts,
                status="rejected",
                strategy=self.strategy,
                arm=decision.arm,
            )
            self.store.commit()
            logger.error("executor_rejected", client_order_id=key, status=exc.response.status_code)
            return SubmitResult(key, "rejected", detail=str(exc.response.status_code))

        platform_id = order.get("order_id")
        self.store.upsert_order(
            key,
            ticker,
            decision.side,
            decision.price,
            decision.contracts,
            status=str(order.get("status", "unknown")),
            platform_order_id=platform_id,
            strategy=self.strategy,
            arm=decision.arm,
        )
        self.store.commit()
        return SubmitResult(key, "submitted", platform_order_id=platform_id)
