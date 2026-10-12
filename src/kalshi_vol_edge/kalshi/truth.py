"""Exchange as the single source of truth.

Every number that touches money comes from the Kalshi API, is parsed to :class:`~decimal.Decimal`
at exactly one place (:func:`to_decimal`), and is reconciled against local state rather than
the other way round (:mod:`kalshi_vol_edge.kalshi.reconcile`). The local database and any JSONL
trade log are *claims*; the exchange's fills, positions and settlements are *facts*.

API field reference (2026)
--------------------------
::

    balance:     balance (int cents), portfolio_value (int cents)
    fills:       fill_id, order_id, client_order_id, ticker, side, action, count_fp (str),
                 yes_price_dollars (str), no_price_dollars (str), is_taker (bool),
                 fee_cost (str dollars), created_time (ISO 8601)
    positions:   ticker, position_fp (str, signed), total_cost_dollars, realized_pnl_dollars,
                 fees_paid_dollars, market_exposure_dollars
    settlements: ticker, market_result (yes/no/scalar/void), yes_count_fp, no_count_fp,
                 revenue (int cents), fee_cost (str dollars), settled_time
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

from kalshi_vol_edge.kalshi.order_ids import parse_client_order_id
from kalshi_vol_edge.logging import get_logger

if TYPE_CHECKING:
    from kalshi_vol_edge.kalshi.client import KalshiClient
    from kalshi_vol_edge.storage.sqlite import TradeStore

logger = get_logger(__name__)


def to_decimal(value: Any, default: str = "0") -> Decimal:
    """Parse an API value to Decimal. The only money conversion point in the package.

    Raises:
        ValueError: when ``value`` is not a decimal number.
    """
    if value is None or value == "":
        return Decimal(default)
    try:
        return Decimal(str(value))
    except Exception as exc:  # InvalidOperation is not a ValueError subclass
        raise ValueError(f"Cannot parse API value {value!r} as Decimal") from exc


def cents_to_decimal(cents: Any) -> Decimal:
    """Convert a legacy integer-cents field (balance, portfolio_value, revenue) to dollars."""
    if cents is None:
        return Decimal("0")
    return Decimal(str(int(cents))) / Decimal("100")


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO 8601 string (``Z`` or offset) to an aware datetime; None on failure."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_strategy_from_client_order_id(client_order_id: str | None) -> str | None:
    """Strategy attribution from a fill's ``client_order_id``.

    ``vol_edge_15m_late_BTC_KXBTC15M-26MAR222030`` -> ``vol_edge_15m``. The format is defined
    once, in :mod:`kalshi_vol_edge.kalshi.order_ids`, which the executor uses to build it.
    Returns None when the id does not follow the convention.
    """
    key = parse_client_order_id(client_order_id)
    return key.strategy if key else None


class KalshiTruth:
    """Money-touching reads from the exchange, in Decimal, fully paginated."""

    def __init__(self, client: KalshiClient) -> None:
        self._client = client

    async def get_balance(self) -> dict[str, Any]:
        """Cash and portfolio value as Decimal dollars (API returns integer cents)."""
        resp = await self._client.get_balance()
        for key in ("balance", "portfolio_value"):
            if key not in resp:
                raise KeyError(f"balance response missing {key!r}; got {sorted(resp)}")
        balance = cents_to_decimal(resp["balance"])
        portfolio = cents_to_decimal(resp["portfolio_value"])
        return {
            "balance_dollars": balance,
            "portfolio_dollars": portfolio,
            "total_dollars": balance + portfolio,
        }

    async def get_all_fills(self) -> list[dict[str, Any]]:
        """Every fill on the account."""
        fills = await self._client.get_fills()
        logger.info("truth_fills", total=len(fills))
        return fills

    async def get_all_positions(self) -> list[dict[str, Any]]:
        """Settled and unsettled positions (two paginated calls; ``all`` is not an API value)."""
        unsettled, settled = await asyncio.gather(
            self._client.get_positions("unsettled"), self._client.get_positions("settled")
        )
        return unsettled + settled

    async def get_portfolio_summary(self) -> dict[str, Any]:
        """Aggregate fees, realized P&L and fill mix from raw API data."""
        balance, positions, fills = await asyncio.gather(
            self.get_balance(), self.get_all_positions(), self.get_all_fills()
        )
        fees = sum((to_decimal(p.get("fees_paid_dollars")) for p in positions), Decimal("0"))
        realized = sum((to_decimal(p.get("realized_pnl_dollars")) for p in positions), Decimal("0"))
        open_positions = sum(
            1
            for p in positions
            if to_decimal(p.get("position_fp")) != 0
            or to_decimal(p.get("market_exposure_dollars")) != 0
        )
        taker = sum(1 for f in fills if f.get("is_taker") is True)
        return {
            **balance,
            "total_fees_paid": fees,
            "total_realized_pnl": realized,
            "total_fills": len(fills),
            "taker_fills": taker,
            "maker_fills": len(fills) - taker,
            "taker_share": (Decimal(taker) / Decimal(len(fills))).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            if fills
            else Decimal("0"),
            "positions": len(positions),
            "open_positions": open_positions,
        }

    async def get_pnl_by_ticker(self) -> dict[str, dict[str, Any]]:
        """Per-ticker realized P&L, fees and fill count from positions and fills."""
        fills, positions = await asyncio.gather(self.get_all_fills(), self.get_all_positions())
        by_ticker: dict[str, dict[str, Any]] = {}
        for fill in fills:
            t = str(fill.get("ticker", ""))
            if t:
                by_ticker.setdefault(t, {"fills": []})["fills"].append(fill)
        for pos in positions:
            t = str(pos.get("ticker", ""))
            if t:
                by_ticker.setdefault(t, {"fills": []})["position"] = pos
        out: dict[str, dict[str, Any]] = {}
        for t, data in by_ticker.items():
            pos = data.get("position", {})
            net = to_decimal(pos.get("position_fp"))
            exposure = to_decimal(pos.get("market_exposure_dollars"))
            out[t] = {
                "fill_count": len(data["fills"]),
                "net_position": net,
                "realized_pnl": to_decimal(pos.get("realized_pnl_dollars")),
                "fees_paid": to_decimal(pos.get("fees_paid_dollars")),
                "is_settled": bool(pos) and net == 0 and exposure == 0,
            }
        return out

    async def sync_to_store(self, store: TradeStore) -> dict[str, int]:
        """Write exchange fills and settlements into the local store idempotently.

        Re-running this is safe: fills dedupe on ``fill_id``, settlements on ``ticker``.
        Returns counts of rows actually inserted.
        """
        fills, settlements = await asyncio.gather(
            self.get_all_fills(), self._client.get_settlements()
        )
        inserted_fills = sum(1 for f in fills if store.record_fill(f))
        upserted_settlements = sum(1 for s in settlements if store.upsert_settlement(s))
        store.commit()
        logger.info(
            "truth_sync",
            fills_seen=len(fills),
            fills_inserted=inserted_fills,
            settlements_seen=len(settlements),
            settlements_upserted=upserted_settlements,
        )
        return {
            "fills_seen": len(fills),
            "fills_inserted": inserted_fills,
            "settlements_seen": len(settlements),
            "settlements_upserted": upserted_settlements,
        }
