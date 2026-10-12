"""Exchange as the single source of truth.

Every number that touches money comes from the Kalshi API, is parsed to :class:`~decimal.Decimal`
at exactly one place (:func:`to_decimal`), and is reconciled against local state rather than
the other way round. The local database and any JSONL trade log are *claims*; the exchange's
fills, positions and settlements are *facts*.

Why this module exists
----------------------
In the live system the local trade log drifted from reality in three ways that this layer
catches:

* phantom orders: logged as filled but never filled (``post_only`` rejection, network error
  after the write);
* unlogged fills: filled on the exchange after the process had crashed;
* wrong fee type: estimated as maker, charged as taker.

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
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

from kalshi_vol_edge.kalshi.order_ids import parse_client_order_id
from kalshi_vol_edge.logging import get_logger

if TYPE_CHECKING:
    from kalshi_vol_edge.kalshi.client import KalshiClient
    from kalshi_vol_edge.storage.sqlite import TradeStore

logger = get_logger(__name__)

TIMESTAMP_MATCH_TOLERANCE_S = 5


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


@dataclass(frozen=True)
class Discrepancy:
    """One difference between local state and the exchange."""

    ticker: str
    field: str
    local_value: Any
    api_value: Any
    severity: str  # "critical" | "warning" | "info"
    fill_id: str | None
    context: str

    def as_dict(self) -> dict[str, Any]:
        """Plain dict for JSON output."""
        return asdict(self)


def _match_key(record: dict[str, Any]) -> tuple[str, str]:
    side = record.get("side") or record.get("taker_side") or ""
    return (str(record.get("ticker", "")), str(side))


def reconcile_fills(
    local_records: list[dict[str, Any]],
    api_fills: list[dict[str, Any]],
    tolerance_s: int = TIMESTAMP_MATCH_TOLERANCE_S,
) -> list[Discrepancy]:
    """Compare local fill/order records with exchange fills. Pure function.

    Matching key: ``ticker + side + created_time within tolerance``. A local record with no
    exchange match is a phantom (critical). An exchange fill with no local match is unlogged
    (info: money moved, the log did not notice). Matched pairs are compared on count, price and
    fee; a fee mismatch usually means a maker estimate for a taker fill.

    Local records may use either the API field names or the short forms ``count``,
    ``yes_price``, ``no_price``, ``fee``.
    """
    api_index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for fill in api_fills:
        api_index.setdefault(_match_key(fill), []).append(fill)

    matched: set[str] = set()
    out: list[Discrepancy] = []

    for rec in local_records:
        ticker, side = _match_key(rec)
        rec_time = parse_timestamp(rec.get("created_time") or rec.get("timestamp"))
        best: dict[str, Any] | None = None
        for fill in api_index.get((ticker, side), []):
            fill_id = str(fill.get("fill_id", ""))
            if fill_id in matched:
                continue
            fill_time = parse_timestamp(fill.get("created_time"))
            if rec_time is None or fill_time is None:
                best = fill
                break
            if abs((rec_time - fill_time).total_seconds()) <= tolerance_s:
                best = fill
                break

        if best is None:
            out.append(
                Discrepancy(
                    ticker=ticker,
                    field="fill_id",
                    local_value=rec.get("client_order_id")
                    or rec.get("order_id")
                    or rec.get("fill_id"),
                    api_value=None,
                    severity="critical",
                    fill_id=None,
                    context=f"Local record ({side} @ {rec_time}) has no matching exchange fill: "
                    "phantom trade in local state.",
                )
            )
            continue

        fill_id = str(best.get("fill_id", ""))
        matched.add(fill_id)

        checks = (
            (
                "count",
                rec.get("count", rec.get("count_fp")),
                best.get("count_fp"),
                Decimal("0.01"),
                "Contract count mismatch",
            ),
            (
                "yes_price_dollars",
                rec.get("yes_price", rec.get("yes_price_dollars")),
                best.get("yes_price_dollars"),
                Decimal("0.01"),
                "YES price mismatch",
            ),
            (
                "fee_cost",
                rec.get("fee", rec.get("fee_cost")),
                best.get("fee_cost"),
                Decimal("0.001"),
                "Fee mismatch (maker estimate for a taker fill?)",
            ),
        )
        for field, local_v, api_v, tol, label in checks:
            if local_v in (None, "") or api_v in (None, ""):
                continue
            try:
                if abs(to_decimal(api_v) - to_decimal(local_v)) > tol:
                    out.append(
                        Discrepancy(
                            ticker,
                            field,
                            local_v,
                            api_v,
                            "critical",
                            fill_id,
                            f"{label}: local={local_v} api={api_v}",
                        )
                    )
            except ValueError:
                out.append(
                    Discrepancy(
                        ticker,
                        field,
                        local_v,
                        api_v,
                        "warning",
                        fill_id,
                        f"{label}: unparseable value",
                    )
                )

        if (
            rec.get("is_taker") is not None
            and best.get("is_taker") is not None
            and bool(rec["is_taker"]) != bool(best["is_taker"])
        ):
            out.append(
                Discrepancy(
                    ticker,
                    "is_taker",
                    rec["is_taker"],
                    best["is_taker"],
                    "critical",
                    fill_id,
                    "Taker flag mismatch: fee and P&L estimates are wrong.",
                )
            )

    for fill in api_fills:
        fill_id = str(fill.get("fill_id", ""))
        if fill_id not in matched:
            out.append(
                Discrepancy(
                    ticker=str(fill.get("ticker", "")),
                    field="fill_id",
                    local_value=None,
                    api_value=fill_id,
                    severity="info",
                    fill_id=fill_id,
                    context=f"Exchange fill ({fill.get('side')} @ {fill.get('created_time')}) "
                    "is not in local state.",
                )
            )
    return out


def summarize_discrepancies(discrepancies: list[Discrepancy]) -> dict[str, int]:
    """Count discrepancies by severity."""
    counts = {"critical": 0, "warning": 0, "info": 0}
    for d in discrepancies:
        counts[d.severity] = counts.get(d.severity, 0) + 1
    return counts


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

    async def reconcile(self, local_records: list[dict[str, Any]]) -> list[Discrepancy]:
        """Reconcile local records against every exchange fill."""
        fills = await self.get_all_fills()
        result = reconcile_fills(local_records, fills)
        logger.info(
            "truth_reconcile",
            local=len(local_records),
            api=len(fills),
            **summarize_discrepancies(result),
        )
        return result

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
