"""Reconciliation of local claims against exchange fills."""

from kalshi_vol_edge.kalshi.reconcile import reconcile_fills, summarize_discrepancies

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
    assert reconcile_fills(local, API_FILLS) == []


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
    out = reconcile_fills(local, API_FILLS)
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
    out = reconcile_fills(local, API_FILLS[:1])
    assert [d.field for d in out] == ["count"]
    # 30 seconds away: no match, so phantom + unlogged
    late = [{"ticker": "T-A", "side": "yes", "count": "10", "created_time": "2026-03-23T00:26:01Z"}]
    assert {d.severity for d in reconcile_fills(late, API_FILLS[:1])} == {"critical", "info"}
