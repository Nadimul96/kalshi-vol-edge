"""Reconcile local orders against exchange fills. Pure functions, no I/O.

The local database and any JSONL trade log are *claims*; the exchange's fills are *facts*
(see :mod:`kalshi_vol_edge.kalshi.truth`). In the live system the local log drifted from the
exchange in three ways that this module catches:

* phantom orders: logged as filled but never filled (``post_only`` rejection, network error
  after the write);
* unlogged fills: filled on the exchange after the process had crashed;
* wrong fee type: estimated as maker, charged as taker.

Matching
--------
Exchange fills are first grouped by the exchange ``order_id``, so an order that filled in
several partials is compared as one order: summed count, summed fee, volume-weighted price.
Each local record is then matched to an exchange order by, in order:

1. ``client_order_id`` (our idempotency key, echoed on every fill),
2. the exchange order id (``platform_order_id`` or ``order_id`` on the local record),
3. ticker, side and action with the first fill within ``tolerance_s`` of the record's time,
   closest first, never across two different ids. A record with no id and no timestamp is
   reported as unmatchable instead of being paired with an arbitrary fill.

Local records may use API field names (``count_fp``, ``yes_price_dollars``, ``fee_cost``), the
short forms (``count``, ``yes_price``, ``fee``) or the ``orders`` table columns (``contracts``,
``price`` in the order's own side, ``status``, ``created_at``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, NamedTuple

from kalshi_vol_edge.kalshi.truth import parse_timestamp, to_decimal

TIMESTAMP_MATCH_TOLERANCE_S = 5
COUNT_TOLERANCE = Decimal("0.01")
PRICE_TOLERANCE = Decimal("0.01")
FEE_TOLERANCE = Decimal("0.001")

# A local record without a status is a claimed fill, like these.
FILLED_STATUSES = frozenset({"executed", "filled"})
# Orders that never reached the exchange are not reconciled.
NOT_SENT_STATUSES = frozenset({"dry_run"})


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


class _Finding(NamedTuple):
    field: str
    local_value: Any
    api_value: Any
    severity: str
    context: str


def _first(record: dict[str, Any], *keys: str) -> Any:
    """The first of ``keys`` present with a non-empty value, else None."""
    return next((record[k] for k in keys if record.get(k) not in (None, "")), None)


def _count(record: dict[str, Any]) -> Any:
    return _first(record, "count", "count_fp", "contracts")


def _price(record: dict[str, Any]) -> Any:
    return _first(record, "price", "yes_price", "yes_price_dollars", "no_price", "no_price_dollars")


def _fee(record: dict[str, Any]) -> Any:
    return _first(record, "fee", "fee_cost")


def _side_price(record: dict[str, Any], side: str) -> Decimal | None:
    """Price in ``side``: the ``price`` column if present, else from the YES or NO price."""
    if (own := record.get("price")) not in (None, ""):
        return to_decimal(own)
    yes = _first(record, "yes_price", "yes_price_dollars")
    no = _first(record, "no_price", "no_price_dollars")
    direct, other = (yes, no) if side == "yes" else (no, yes)
    if direct is not None:
        return to_decimal(direct)
    return 1 - to_decimal(other) if other is not None else None


@dataclass(frozen=True)
class _ExchangeOrder:
    """One exchange order, summarised from its fills (one or more partials)."""

    key: str
    order_id: str
    client_order_id: str
    ticker: str
    side: str
    action: str
    first_time: datetime | None
    first_fill_id: str
    fills: int
    count: Decimal
    fee: Decimal | None
    price: Decimal | None  # volume-weighted, in ``side``
    is_taker: bool | None  # None when the partials disagree or the flag is missing

    @classmethod
    def from_fills(cls, key: str, fills: list[dict[str, Any]]) -> _ExchangeOrder:
        def field(name: str) -> str:
            return str(next((f[name] for f in fills if f.get(name)), ""))

        side = field("side")
        counts = [to_decimal(_count(f)) for f in fills]
        prices = [_side_price({**f, "price": None}, side) for f in fills]
        fees = [to_decimal(f["fee_cost"]) for f in fills if f.get("fee_cost") not in (None, "")]
        times = [t for t in (parse_timestamp(f.get("created_time")) for f in fills) if t]
        flags = {f.get("is_taker") for f in fills}
        total = sum(counts, Decimal(0))
        vwap = None
        if total and None not in prices:
            vwap = sum((p * c for p, c in zip(prices, counts, strict=True) if p), Decimal(0))
            vwap = (vwap / total).quantize(Decimal("0.0001"))
        return cls(
            key=key,
            order_id=field("order_id"),
            client_order_id=field("client_order_id"),
            ticker=field("ticker"),
            side=side,
            action=field("action") or "buy",
            first_time=min(times) if times else None,
            first_fill_id=str(fills[0].get("fill_id", "")),
            fills=len(fills),
            count=total,
            fee=sum(fees, Decimal(0)) if fees else None,
            price=vwap,
            is_taker=bool(next(iter(flags))) if len(flags) == 1 and None not in flags else None,
        )


def _n_fills(order: _ExchangeOrder) -> str:
    return f"{order.fills} fill" + ("" if order.fills == 1 else "s")


def _group_fills(api_fills: list[dict[str, Any]]) -> list[_ExchangeOrder]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for fill in api_fills:
        if fill.get("order_id"):
            key = f"order:{fill['order_id']}"
        elif fill.get("client_order_id"):
            key = f"coid:{fill['client_order_id']}"
        else:
            key = f"fill:{fill.get('fill_id', len(groups))}"
        groups.setdefault(key, []).append(fill)
    return [_ExchangeOrder.from_fills(k, v) for k, v in groups.items()]


def _local_time(rec: dict[str, Any]) -> datetime | None:
    return parse_timestamp(_first(rec, "created_time", "timestamp", "created_at"))


def _ids(rec: dict[str, Any]) -> tuple[str, str]:
    """(client_order_id, exchange order id) of a local record, empty when absent."""
    return str(rec.get("client_order_id") or ""), str(
        _first(rec, "platform_order_id", "order_id") or ""
    )


def _match(
    local: list[dict[str, Any]], orders: list[_ExchangeOrder], tolerance_s: int
) -> dict[int, _ExchangeOrder]:
    """Index of each local record -> its exchange order: by id first, then by time."""
    by_coid = {o.client_order_id: o for o in reversed(orders) if o.client_order_id}
    by_oid = {o.order_id: o for o in reversed(orders) if o.order_id}
    matches: dict[int, _ExchangeOrder] = {}
    claimed: set[str] = set()

    for i, rec in enumerate(local):
        coid, oid = _ids(rec)
        order = (by_coid.get(coid) if coid else None) or (by_oid.get(oid) if oid else None)
        if order is not None and order.key not in claimed:
            matches[i] = order
            claimed.add(order.key)

    for i, rec in enumerate(local):
        rec_time = _local_time(rec)
        if i in matches or rec_time is None:
            continue
        coid, oid = _ids(rec)
        shape = (str(rec.get("ticker", "")), str(rec.get("side", "")), rec.get("action") or "buy")
        candidates = [
            (abs((o.first_time - rec_time).total_seconds()), o)
            for o in orders
            if o.key not in claimed
            and o.first_time is not None
            and (o.ticker, o.side, o.action) == shape
            and not (coid and o.client_order_id and coid != o.client_order_id)
            and not (oid and o.order_id and oid != o.order_id)
        ]
        candidates = [c for c in candidates if c[0] <= tolerance_s]
        if candidates:
            best = min(candidates, key=lambda c: c[0])[1]
            matches[i] = best
            claimed.add(best.key)
    return matches


def _check_count(rec: dict[str, Any], order: _ExchangeOrder) -> _Finding | None:
    local, ordered, filled = _count(rec), to_decimal(_count(rec)), order.count
    status = rec.get("status")
    if (status is None or status in FILLED_STATUSES) and abs(filled - ordered) > COUNT_TOLERANCE:
        context = f"Contract count mismatch: local={local} exchange={filled} ({_n_fills(order)})"
        return _Finding("count", local, str(filled), "critical", context)
    if filled > ordered + COUNT_TOLERANCE:
        context = f"Overfilled: the exchange filled {filled} on an order for {local}"
        return _Finding("count", local, str(filled), "critical", context)
    if filled < ordered - COUNT_TOLERANCE:
        context = f"Partially filled: {filled} of {local} so far (order is {status})"
        return _Finding("count", local, str(filled), "info", context)
    return None


def _check_price(rec: dict[str, Any], order: _ExchangeOrder) -> _Finding | None:
    side = str(rec.get("side", ""))
    local, api = _side_price(rec, side), order.price
    if local is None or api is None:
        return None
    worse = api - local if order.action == "buy" else local - api
    label = f"{side.upper()} price: local={local} exchange={api} (volume-weighted)"
    if worse > PRICE_TOLERANCE:
        return _Finding("price", str(local), str(api), "critical", f"Worse {label}")
    if worse < -PRICE_TOLERANCE:
        context = f"Better {label}: price improvement, or the local price is the limit"
        return _Finding("price", str(local), str(api), "info", context)
    return None


def _check_fee(rec: dict[str, Any], order: _ExchangeOrder) -> _Finding | None:
    local = _fee(rec)
    if order.fee is None or abs(order.fee - to_decimal(local)) <= FEE_TOLERANCE:
        return None
    context = f"Fee mismatch (maker estimate for a taker fill?): local={local} api={order.fee}"
    return _Finding("fee_cost", local, str(order.fee), "critical", context)


_Check = Callable[[dict[str, Any], "_ExchangeOrder"], _Finding | None]
_CHECKS: tuple[tuple[str, Callable[[dict[str, Any]], Any], _Check], ...] = (
    ("count", _count, _check_count),
    ("price", _price, _check_price),
    ("fee_cost", _fee, _check_fee),
)


def _compare(rec: dict[str, Any], order: _ExchangeOrder) -> list[Discrepancy]:
    """Differences between one local order and its exchange fills."""
    findings: list[_Finding] = []
    if rec.get("status") == "rejected":
        context = "Exchange filled an order recorded as rejected."
        findings.append(_Finding("status", "rejected", "filled", "critical", context))
    for name, local_value, check in _CHECKS:
        if (raw := local_value(rec)) is None:
            continue
        try:
            finding = check(rec, order)
        except ValueError:
            finding = _Finding(name, raw, None, "warning", f"{name}: unparseable value")
        if finding is not None:
            findings.append(finding)
    taker = rec.get("is_taker")
    if taker is not None and order.is_taker is not None and bool(taker) != order.is_taker:
        context = "Taker flag mismatch: fee and P&L estimates are wrong."
        findings.append(_Finding("is_taker", taker, order.is_taker, "critical", context))
    ticker = str(rec.get("ticker", ""))
    return [
        Discrepancy(
            ticker, f.field, f.local_value, f.api_value, f.severity, order.first_fill_id, f.context
        )
        for f in findings
    ]


def _unmatched(rec: dict[str, Any]) -> Discrepancy | None:
    """What a local order with no exchange fills means, given its status."""
    ticker, status, rec_time = str(rec.get("ticker", "")), rec.get("status"), _local_time(rec)
    ident = next((i for i in _ids(rec) if i), None)
    if ident is None and rec_time is None:
        context = "No client_order_id, order id or timestamp: cannot be matched to a fill."
        return Discrepancy(ticker, "match", None, None, "warning", None, context)
    if status == "submitting":
        context = "Submission outcome was never recorded and no fill carries this order."
        return Discrepancy(ticker, "status", status, None, "warning", None, context)
    if status is None or status in FILLED_STATUSES:
        context = (
            f"Local record ({rec.get('side')} @ {rec_time}) has no matching exchange fill: "
            "phantom trade in local state."
        )
        return Discrepancy(ticker, "fill_id", ident, None, "critical", None, context)
    return None  # resting, canceled or rejected with no fills: nothing to reconcile


def reconcile_orders(
    local_records: list[dict[str, Any]],
    api_fills: list[dict[str, Any]],
    tolerance_s: int = TIMESTAMP_MATCH_TOLERANCE_S,
) -> list[Discrepancy]:
    """Compare local orders with exchange fills. Pure function.

    A local record that claims a fill but has none on the exchange is a phantom (critical). An
    exchange order with no local record is unlogged (info: money moved, the log did not
    notice). Matched orders are compared on summed count, volume-weighted price, summed fee
    and taker flag; a fee mismatch usually means a maker estimate for a taker fill.
    """
    local = [r for r in local_records if r.get("status") not in NOT_SENT_STATUSES]
    orders = _group_fills(api_fills)
    matches = _match(local, orders, tolerance_s)

    out: list[Discrepancy] = []
    for i, rec in enumerate(local):
        if i in matches:
            out.extend(_compare(rec, matches[i]))
        elif (d := _unmatched(rec)) is not None:
            out.append(d)

    matched = {o.key for o in matches.values()}
    for o in orders:
        if o.key not in matched:
            what = f"order {o.order_id} ({_n_fills(o)}, " if o.order_id else "fill ("
            context = f"Exchange {what}{o.count} {o.side} @ {o.first_time}) is not in local state."
            api_value = o.order_id or o.first_fill_id
            out.append(
                Discrepancy(o.ticker, "fill_id", None, api_value, "info", o.first_fill_id, context)
            )
    return out


def summarize_discrepancies(discrepancies: list[Discrepancy]) -> dict[str, int]:
    """Count discrepancies by severity."""
    counts = {"critical": 0, "warning": 0, "info": 0}
    for d in discrepancies:
        counts[d.severity] = counts.get(d.severity, 0) + 1
    return counts
