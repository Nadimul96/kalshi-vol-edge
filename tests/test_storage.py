"""SQLite store: idempotent writes and connection hygiene."""

import sqlite3

import pytest

from kalshi_vol_edge.storage.sqlite import TradeStore


def test_upsert_order_dedupes_on_client_and_platform_ids():
    with TradeStore(":memory:") as store:
        assert store.upsert_order("k1", "T", "yes", "0.90", 20, "submitting") is True
        assert (
            store.upsert_order("k1", "T", "yes", "0.90", 20, "executed", platform_order_id="p1")
            is False
        )
        # a replay that only knows the exchange id must not create a second row
        assert (
            store.upsert_order(
                "k1-retry", "T", "yes", "0.90", 20, "executed", platform_order_id="p1"
            )
            is False
        )
        assert store.count_orders() == 1
        row = store.get_order("k1")
        assert row["status"] == "executed" and row["platform_order_id"] == "p1"
        assert store.has_order("k1") and not store.has_order("k1-retry")


def test_list_orders_returns_recorded_orders_for_reconciliation():
    with TradeStore(":memory:") as store:
        store.upsert_order("k1", "T", "yes", "0.90", 20, "executed", platform_order_id="p1")
        store.upsert_order("k2", "T", "no", "0.10", 5, "dry_run")
        orders = store.list_orders()
    assert [o["client_order_id"] for o in orders] == ["k1", "k2"]
    assert orders[0]["platform_order_id"] == "p1" and orders[0]["contracts"] == 20
    assert orders[0]["action"] == "buy" and orders[0]["created_at"]


def test_record_fill_ignores_duplicates_and_preserves_money_as_text():
    fill = {
        "fill_id": "f1",
        "ticker": "T",
        "side": "yes",
        "count_fp": "10.00",
        "yes_price_dollars": "0.6200",
        "fee_cost": "0.1650",
        "is_taker": True,
        "created_time": "2026-03-23T00:25:31Z",
    }
    with TradeStore(":memory:") as store:
        assert store.record_fill(fill) is True
        assert store.record_fill(fill) is False
        assert store.count_fills() == 1
        (stored,) = store.list_fills("T")
        assert stored["yes_price_dollars"] == "0.6200" and stored["is_taker"] is True


def test_settlement_upsert_reports_changes_only():
    s = {"ticker": "T", "market_result": "yes", "settled_time": "2026-03-23T00:30:05Z"}
    with TradeStore(":memory:") as store:
        assert store.upsert_settlement(s) is True
        assert store.upsert_settlement(s) is False
        assert store.upsert_settlement({**s, "market_result": "no"}) is True
        assert store.count_settlements() == 1


def test_store_closes_connection_and_persists_to_disk(tmp_path):
    path = tmp_path / "kve.sqlite3"
    store = TradeStore(path)
    store.upsert_order("k1", "T", "no", "0.40", 5, "dry_run")
    store.close()
    assert store.closed
    store.close()  # idempotent
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_read_only_store_reads_but_never_writes_or_creates(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(FileNotFoundError):
        TradeStore(missing, read_only=True)
    assert not missing.exists()

    path = tmp_path / "kve.sqlite3"
    with TradeStore(path) as store:
        store.record_fill(
            {"fill_id": "f1", "ticker": "T", "side": "yes", "count_fp": "1", "created_time": "x"}
        )
    with TradeStore(path, read_only=True) as ro:
        assert [f["fill_id"] for f in ro.list_fills()] == ["f1"]
        with pytest.raises(sqlite3.OperationalError):
            ro.record_fill({"fill_id": "f2", "ticker": "T", "side": "no", "count_fp": "1"})
