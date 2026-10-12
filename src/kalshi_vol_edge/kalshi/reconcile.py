"""Reconcile local claims against exchange fills. Pure functions, no I/O.

The local database and any JSONL trade log are *claims*; the exchange's fills are *facts*
(see :mod:`kalshi_vol_edge.kalshi.truth`). In the live system the local log drifted from the
exchange in three ways that this module catches:

* phantom orders: logged as filled but never filled (``post_only`` rejection, network error
  after the write);
* unlogged fills: filled on the exchange after the process had crashed;
* wrong fee type: estimated as maker, charged as taker.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from kalshi_vol_edge.kalshi.truth import parse_timestamp, to_decimal

TIMESTAMP_MATCH_TOLERANCE_S = 5


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
