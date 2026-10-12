"""Executor idempotency."""

from unittest.mock import AsyncMock

from kalshi_vol_edge.execution import Executor
from kalshi_vol_edge.kalshi.order_ids import build_client_order_id
from kalshi_vol_edge.kalshi.truth import parse_strategy_from_client_order_id
from kalshi_vol_edge.storage.sqlite import TradeStore
from kalshi_vol_edge.strategy.vol_edge import Decision

DECISION = Decision(
    arm="late",
    side="yes",
    price=0.90,
    contracts=22,
    win_prob=0.95,
    edge=0.03,
    z_score=1.282,
    required_lead=30.0,
    fee=0.14,
    reason="test",
)


def test_idempotency_key_is_deterministic():
    k = build_client_order_id("vol_edge_15m", "late", "BTC", "KXBTC15M-26MAR222030")
    assert k == "vol_edge_15m_late_BTC_KXBTC15M-26MAR222030"
    assert k == build_client_order_id("vol_edge_15m", "late", "BTC", "KXBTC15M-26MAR222030")


async def test_executor_key_round_trips_through_attribution():
    """Regression: the executor's keys used to parse to None in the truth layer."""
    with TradeStore(":memory:") as store:
        ex = Executor(client=None, store=store, strategy="vol_edge_15m", dry_run=True)
        res = await ex.submit(DECISION, "KXBTC15M-26MAR222030-30", "KXBTC15M-26MAR222030", "BTC")
    assert parse_strategy_from_client_order_id(res.client_order_id) == "vol_edge_15m"


async def test_dry_run_records_once_and_rejects_duplicates():
    with TradeStore(":memory:") as store:
        ex = Executor(client=None, store=store, dry_run=True)
        first = await ex.submit(DECISION, "KXBTC15M-26MAR222030-30", "KXBTC15M-26MAR222030", "BTC")
        second = await ex.submit(DECISION, "KXBTC15M-26MAR222030-30", "KXBTC15M-26MAR222030", "BTC")
        assert first.status == "dry_run" and second.status == "duplicate"
        assert store.count_orders() == 1


async def test_live_submit_records_intent_then_exchange_id():
    client = AsyncMock()
    client.place_limit_order = AsyncMock(return_value={"order_id": "p-123", "status": "resting"})
    with TradeStore(":memory:") as store:
        ex = Executor(client=client, store=store, dry_run=False)
        res = await ex.submit(DECISION, "T", "E", "BTC")
        assert res.status == "submitted" and res.platform_order_id == "p-123"
        client.place_limit_order.assert_awaited_once()
        assert client.place_limit_order.await_args.kwargs["client_order_id"] == res.client_order_id
        row = store.get_order(res.client_order_id)
        assert row["platform_order_id"] == "p-123" and row["status"] == "resting"
        # retry after a crash: local key already present -> no second network call
        again = await ex.submit(DECISION, "T", "E", "BTC")
        assert again.status == "duplicate"
        client.place_limit_order.assert_awaited_once()
