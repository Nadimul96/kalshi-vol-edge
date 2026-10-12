"""The ``client_order_id`` convention, built and parsed in one place.

Format: ``{strategy}_{arm}_{asset}_{event_ticker}``, for example
``vol_edge_15m_late_BTC_KXBTC15M-26MAR222030``.

The id doubles as the idempotency key (one order per strategy, arm and event) and as the
attribution tag the truth layer reads back out of exchange fills. Only the strategy name may
contain underscores; the other three parts never do (Kalshi tickers use hyphens), so the id
parses unambiguously from the right. Ids written by the earlier ``..._{timestamp_ms}`` scheme
still parse, with the timestamp in the ``event_ticker`` slot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_STRATEGY = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")
_ARM = re.compile(r"[a-z0-9]+")
_ASSET = re.compile(r"[A-Z0-9]+")
_EVENT = re.compile(r"[A-Z0-9][A-Z0-9.-]*")


@dataclass(frozen=True, slots=True)
class OrderKey:
    """The parts of a ``client_order_id``."""

    strategy: str
    arm: str
    asset: str
    event_ticker: str


def _valid(key: OrderKey) -> bool:
    return bool(
        _STRATEGY.fullmatch(key.strategy)
        and _ARM.fullmatch(key.arm)
        and _ASSET.fullmatch(key.asset)
        and _EVENT.fullmatch(key.event_ticker)
    )


def build_client_order_id(strategy: str, arm: str, asset: str, event_ticker: str) -> str:
    """Deterministic ``client_order_id`` for one arm of one strategy on one event.

    Raises:
        ValueError: when a part would make the id ambiguous or unparseable.
    """
    key = OrderKey(strategy, arm, asset, event_ticker)
    if not _valid(key):
        raise ValueError(
            f"cannot build a parseable client_order_id from {key}: strategy is lower_snake_case, "
            "arm is lowercase, asset is uppercase, event_ticker has no underscores"
        )
    return f"{strategy}_{arm}_{asset}_{event_ticker}"


def parse_client_order_id(client_order_id: str | None) -> OrderKey | None:
    """Inverse of :func:`build_client_order_id`; None when the id does not follow it."""
    if not client_order_id:
        return None
    parts = client_order_id.split("_")
    if len(parts) < 4:
        return None
    key = OrderKey("_".join(parts[:-3]), parts[-3], parts[-2], parts[-1])
    return key if _valid(key) else None
