"""Reconciliation of local claims against exchange fills."""

from kalshi_vol_edge.kalshi.reconcile import reconcile_orders, summarize_discrepancies

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


def test_reconcile_clean_match_has_no_discrepancies():
    local = [
        {
            "ticker": "T-A",
            "side": "yes",
            "count": "10",
            "yes_price": "0.62",
            "fee": "0.165",
            "is_taker": True,
            "created_time": "2026-03-23T00:25:30Z",
        },
        {
            "ticker": "T-B",
            "side": "no",
            "count": "5",
            "yes_price": "0.30",
            "fee": "0.0735",
            "created_time": "2026-03-23T00:40:03Z",
        },
    ]
    assert reconcile_orders(local, API_FILLS) == []


def test_reconcile_flags_phantom_unlogged_and_fee_mismatch():
    local = [
        # fee estimated as maker, charged as taker
        {
            "ticker": "T-A",
            "side": "yes",
            "count": "10",
            "yes_price": "0.62",
            "fee": "0.0413",
            "is_taker": False,
            "created_time": "2026-03-23T00:25:31Z",
        },
        # phantom: never filled on the exchange
        {"ticker": "T-C", "side": "yes", "count": "3", "created_time": "2026-03-23T01:00:00Z"},
    ]
    out = reconcile_orders(local, API_FILLS)
    fields = {(d.ticker, d.field, d.severity) for d in out}
    assert ("T-A", "fee_cost", "critical") in fields
    assert ("T-A", "is_taker", "critical") in fields
    assert ("T-C", "fill_id", "critical") in fields  # phantom
    assert ("T-B", "fill_id", "info") in fields  # unlogged exchange fill
    assert summarize_discrepancies(out) == {"critical": 3, "warning": 0, "info": 1}


def test_reconcile_count_mismatch_and_timestamp_tolerance():
    local = [
        {"ticker": "T-A", "side": "yes", "count": "12", "created_time": "2026-03-23T00:25:31Z"}
    ]
    out = reconcile_orders(local, API_FILLS[:1])
    assert [d.field for d in out] == ["count"]
    # 30 seconds away: no match, so phantom + unlogged
    late = [{"ticker": "T-A", "side": "yes", "count": "10", "created_time": "2026-03-23T00:26:01Z"}]
    assert {d.severity for d in reconcile_orders(late, API_FILLS[:1])} == {"critical", "info"}


def _fill(fill_id, order_id, count, fee, created, **kw):
    return {
        "fill_id": fill_id,
        "order_id": order_id,
        "ticker": "T-A",
        "side": "yes",
        "action": "buy",
        "count_fp": count,
        "yes_price_dollars": "0.6200",
        "no_price_dollars": "0.3800",
        "fee_cost": fee,
        "is_taker": True,
        "created_time": created,
        **kw,
    }


def test_partial_fills_of_one_order_are_aggregated():
    """Regression: two partials gave a false count/fee critical plus a false 'unlogged'."""
    fills = [
        _fill("f1", "o1", "6.00", "0.0990", "2026-03-23T00:25:31Z", client_order_id="K"),
        _fill("f2", "o1", "4.00", "0.0660", "2026-03-23T00:25:40Z", client_order_id="K"),
    ]
    local = [
        {
            "client_order_id": "K",
            "ticker": "T-A",
            "side": "yes",
            "count": "10",
            "yes_price": "0.62",
            "fee": "0.165",
            "is_taker": True,
            "created_time": "2026-03-23T00:25:30Z",
            "status": "executed",
        }
    ]
    assert reconcile_orders(local, fills) == []


def test_order_id_match_ignores_clock_skew():
    fills = [_fill("f1", "o-9", "10.00", "0.1650", "2026-03-23T00:25:31Z")]
    # the local write happened a minute after the fill: matched by the exchange order id
    local = [
        {
            "platform_order_id": "o-9",
            "ticker": "T-A",
            "side": "yes",
            "count": "10",
            "created_time": "2026-03-23T00:26:31Z",
        }
    ]
    assert reconcile_orders(local, fills) == []


def test_missing_timestamp_never_grabs_an_arbitrary_fill():
    """Regression: a record without a timestamp matched the first fill on its ticker."""
    fills = [
        _fill("f1", "o1", "10.00", "0.1650", "2026-03-23T00:25:31Z", client_order_id="A"),
        _fill("f2", "o2", "10.00", "0.1650", "2026-03-23T00:40:00Z", client_order_id="B"),
    ]
    by_id = [{"client_order_id": "B", "ticker": "T-A", "side": "yes", "count": "10"}]
    out = reconcile_orders(by_id, fills)
    assert [(d.field, d.api_value) for d in out] == [("fill_id", "o1")]  # only A is unlogged

    anonymous = [{"ticker": "T-A", "side": "yes", "count": "10"}]
    out = reconcile_orders(anonymous, fills)
    assert [(d.field, d.severity) for d in out] == [
        ("match", "warning"),
        ("fill_id", "info"),
        ("fill_id", "info"),
    ]


def test_action_must_match_and_closest_fill_wins():
    fills = [
        _fill("f-sell", "o1", "10.00", "0.1650", "2026-03-23T00:25:30Z", action="sell"),
        _fill("f-far", "o2", "10.00", "0.1650", "2026-03-23T00:25:26Z"),
        _fill("f-near", "o3", "10.00", "0.1650", "2026-03-23T00:25:32Z"),
    ]
    local = [
        {"ticker": "T-A", "side": "yes", "count": "10", "created_time": "2026-03-23T00:25:30Z"}
    ]
    unlogged = {d.fill_id for d in reconcile_orders(local, fills)}
    assert unlogged == {"f-sell", "f-far"}  # the buy record took the nearest buy fill


def test_status_decides_what_a_missing_or_partial_fill_means():
    fills = [_fill("f1", "o1", "4.00", "0.0660", "2026-03-23T00:25:31Z", client_order_id="R")]
    base = {"ticker": "T-A", "side": "yes", "price": "0.62", "created_at": "2026-03-23T00:25:30Z"}
    local = [
        {**base, "client_order_id": "R", "contracts": 10, "status": "resting"},
        {**base, "client_order_id": "S", "contracts": 5, "status": "submitting"},
        {**base, "client_order_id": "D", "contracts": 5, "status": "dry_run"},
        {**base, "client_order_id": "C", "contracts": 5, "status": "canceled"},
    ]
    out = reconcile_orders(local, fills)
    assert [(d.field, d.severity) for d in out] == [("count", "info"), ("status", "warning")]
    assert "Partially filled: 4.00 of 10" in out[0].context

    rejected = [{**base, "client_order_id": "R", "contracts": 4, "status": "rejected"}]
    assert [(d.field, d.severity) for d in reconcile_orders(rejected, fills)] == [
        ("status", "critical")
    ]
    over = [{**base, "client_order_id": "R", "contracts": 3, "status": "resting"}]
    assert [d.context.split(":")[0] for d in reconcile_orders(over, fills)] == ["Overfilled"]


def test_price_is_compared_in_the_order_side_against_the_vwap():
    fills = [
        _fill(
            "f1",
            "o1",
            "5.00",
            "0.0800",
            "2026-03-23T00:25:31Z",
            client_order_id="K",
            yes_price_dollars="0.6000",
            no_price_dollars="0.4000",
        ),
        _fill(
            "f2",
            "o1",
            "5.00",
            "0.0800",
            "2026-03-23T00:25:32Z",
            client_order_id="K",
            yes_price_dollars="0.6400",
            no_price_dollars="0.3600",
        ),
    ]  # VWAP 0.62
    rec = {"client_order_id": "K", "ticker": "T-A", "side": "yes", "count": "10"}
    assert reconcile_orders([{**rec, "price": "0.62"}], fills) == []
    worse = reconcile_orders([{**rec, "price": "0.60"}], fills)
    assert [(d.field, d.severity, d.api_value) for d in worse] == [("price", "critical", "0.6200")]
    better = reconcile_orders([{**rec, "price": "0.65"}], fills)
    assert [(d.field, d.severity) for d in better] == [("price", "info")]
