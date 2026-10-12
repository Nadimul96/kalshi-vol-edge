"""The client_order_id convention: built by the executor, parsed by the truth layer."""

import pytest

from kalshi_vol_edge.kalshi.order_ids import (
    OrderKey,
    build_client_order_id,
    parse_client_order_id,
)


@pytest.mark.parametrize(
    "parts",
    [
        ("vol_edge_15m", "late", "BTC", "KXBTC15M-26MAR222030"),
        ("vol_edge_15m", "early", "ETH", "KXETH15M-26MAR230115"),
        ("base_rate_v1", "arm1", "JORD", "KXMLBMENTION-26MAR27NYYSF"),
        ("x", "mid", "BTC", "KXBTCD-26MAR2322"),
    ],
)
def test_build_then_parse_round_trips(parts):
    assert parse_client_order_id(build_client_order_id(*parts)) == OrderKey(*parts)


@pytest.mark.parametrize(
    "parts",
    [
        ("vol_edge_15m", "late_arm", "BTC", "KXBTC15M-26MAR222030"),  # underscore in arm
        ("vol_edge_15m", "late", "BTC", "KXBTC15M_26MAR222030"),  # underscore in event
        ("vol_edge_15m", "late", "btc", "KXBTC15M-26MAR222030"),  # lowercase asset
        ("", "late", "BTC", "KXBTC15M-26MAR222030"),  # no strategy
        ("vol_edge_15m", "late", "BTC", ""),  # no event
    ],
)
def test_build_refuses_parts_that_would_not_parse(parts):
    with pytest.raises(ValueError, match="parseable"):
        build_client_order_id(*parts)


def test_parse_rejects_ids_outside_the_convention():
    assert parse_client_order_id(None) is None
    assert parse_client_order_id("") is None
    assert parse_client_order_id("a_b_c") is None
    assert parse_client_order_id("6f1c2a9e-7d1b-4c55-9a8e-2b1f0c3d4e5f") is None
    assert parse_client_order_id("vol_edge_15m_late_BTC_kxbtc15m") is None
