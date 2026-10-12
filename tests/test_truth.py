"""Exchange-as-truth reconciliation layer."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from kalshi_vol_edge.kalshi.reconcile import reconcile_orders
from kalshi_vol_edge.kalshi.truth import (
    KalshiTruth,
    cents_to_decimal,
    parse_strategy_from_client_order_id,
    to_decimal,
)
from kalshi_vol_edge.storage.sqlite import TradeStore

API_FILLS = [
    {
        "fill_id": "f1",
        "ticker": "T-A",
        "side": "yes",
        "count_fp": "10.00",
        "yes_price_dollars": "0.6200",
        "fee_cost": "0.1650",
        "is_taker": True,
        "created_time": "2026-03-23T00:25:31Z",
        "client_order_id": "vol_edge_15m_late_BTC_1711234567000",
    },
    {
        "fill_id": "f2",
        "ticker": "T-B",
        "side": "no",
        "count_fp": "5.00",
        "yes_price_dollars": "0.3000",
        "fee_cost": "0.0735",
        "is_taker": True,
        "created_time": "2026-03-23T00:40:00Z",
    },
]


def test_to_decimal_never_uses_float_and_rejects_garbage():
    assert to_decimal("0.6200") == Decimal("0.6200")
    assert to_decimal(None) == Decimal("0")
    assert to_decimal("", default="1") == Decimal("1")
    assert cents_to_decimal(45536) == Decimal("455.36")
    with pytest.raises(ValueError):
        to_decimal("not-a-number")


def test_strategy_parsed_from_client_order_id():
    assert (
        parse_strategy_from_client_order_id("vol_edge_15m_late_BTC_KXBTC15M-26MAR222030")
        == "vol_edge_15m"
    )
    # ids written by the earlier timestamp scheme still parse
    assert (
        parse_strategy_from_client_order_id("vol_edge_15m_late_BTC_1711234567000") == "vol_edge_15m"
    )
    assert (
        parse_strategy_from_client_order_id("base_rate_v1_arm1_JORD_1711234567000")
        == "base_rate_v1"
    )
    assert parse_strategy_from_client_order_id("abc123") is None
    assert parse_strategy_from_client_order_id("3f2b9c1e-uuid-like") is None
    assert parse_strategy_from_client_order_id(None) is None


class _FakeClient:
    def __init__(self):
        self.get_balance = AsyncMock(return_value={"balance": 45536, "portfolio_value": 1200})
        self.get_fills = AsyncMock(return_value=API_FILLS)
        self.get_positions = AsyncMock(
            side_effect=lambda status: {
                "unsettled": [
                    {
                        "ticker": "T-B",
                        "position_fp": "-5.00",
                        "fees_paid_dollars": "0.0735",
                        "realized_pnl_dollars": "0",
                        "market_exposure_dollars": "1.50",
                    }
                ],
                "settled": [
                    {
                        "ticker": "T-A",
                        "position_fp": "0",
                        "fees_paid_dollars": "0.1650",
                        "realized_pnl_dollars": "3.6350",
                        "market_exposure_dollars": "0",
                    }
                ],
            }[status]
        )
        self.get_settlements = AsyncMock(
            return_value=[
                {
                    "ticker": "T-A",
                    "market_result": "yes",
                    "yes_count_fp": "10.00",
                    "revenue": 1000,
                    "fee_cost": "0.1650",
                    "settled_time": "2026-03-23T00:30:05Z",
                },
            ]
        )


async def test_balance_and_summary_are_decimal():
    truth = KalshiTruth(_FakeClient())  # type: ignore[arg-type]
    bal = await truth.get_balance()
    assert bal["total_dollars"] == Decimal("467.36")
    summary = await truth.get_portfolio_summary()
    assert summary["total_fees_paid"] == Decimal("0.2385")
    assert summary["total_realized_pnl"] == Decimal("3.6350")
    assert summary["taker_fills"] == 2 and summary["open_positions"] == 1
    by_ticker = await truth.get_pnl_by_ticker()
    assert by_ticker["T-A"]["is_settled"] is True and by_ticker["T-B"]["is_settled"] is False


async def test_balance_missing_field_is_an_error_not_zero():
    client = _FakeClient()
    client.get_balance = AsyncMock(return_value={"balance": 100})
    with pytest.raises(KeyError):
        await KalshiTruth(client).get_balance()  # type: ignore[arg-type]


async def test_sync_to_store_is_idempotent():
    truth = KalshiTruth(_FakeClient())  # type: ignore[arg-type]
    with TradeStore(":memory:") as store:
        first = await truth.sync_to_store(store)
        second = await truth.sync_to_store(store)
        assert first["fills_inserted"] == 2 and first["settlements_upserted"] == 1
        assert second["fills_inserted"] == 0 and second["settlements_upserted"] == 0
        assert store.count_fills() == 2 and store.count_settlements() == 1
        # the stored fills reconcile cleanly against the API that produced them
        assert reconcile_orders(store.list_fills(), API_FILLS) == []
