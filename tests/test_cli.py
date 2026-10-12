"""CLI exit codes and output."""

import json
from pathlib import Path

from kalshi_vol_edge.cli import EXIT_OK, EXIT_USAGE, main


def test_backtest_on_sample_prints_report(sample_dir: Path, capsys):
    assert main(["backtest", "--fixtures", str(sample_dir)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "trades           : 13" in out and "net P&L" in out and "max drawdown" in out


def test_backtest_json_output(sample_dir: Path, capsys):
    assert main(["backtest", "--fixtures", str(sample_dir), "--json"]) == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["trade_count"] == 13 and len(data["trades"]) == 13
    assert data["quote_source"].startswith("MODELED")


def test_backtest_without_fixtures_exits_nonzero_with_clear_error(capsys):
    assert main(["backtest", "--fixtures", "/nonexistent"]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "fixture directory not found: /nonexistent" in err
    assert "never falls back to synthetic data" in err


def test_backtest_quote_override_changes_result(sample_dir: Path, capsys):
    assert (
        main(["backtest", "--fixtures", str(sample_dir), "--implied-vol", "0.80", "--json"])
        == EXIT_OK
    )
    data = json.loads(capsys.readouterr().out)
    assert "80%" in data["quote_source"]
    assert data["trade_count"] != 13


def test_reconcile_dry_run_uses_fixture(sample_dir: Path, capsys):
    assert main(["reconcile", "--dry-run", "--fixtures", str(sample_dir)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "4 discrepancies" in out and "phantom" in out and "Fee mismatch" in out


def test_reconcile_live_without_credentials_is_refused(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)  # no .env here
    for var in ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(var, raising=False)
    assert main(["reconcile"]) == EXIT_USAGE
    assert "needs KALSHI_API_KEY_ID" in capsys.readouterr().err


def test_fees_command(capsys):
    assert main(["fees", "--price", "0.5", "--contracts", "10"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "taker fee        : $0.1750" in out and "breakeven (taker): 51.75%" in out


def test_reconcile_live_runs_with_sync_store(monkeypatch, tmp_path, capsys):
    """Regression: the live path opened the sync TradeStore with ``async with``."""
    from kalshi_vol_edge.kalshi.truth import KalshiTruth

    key = tmp_path / "k.pem"
    key.write_text("unused: KalshiTruth is stubbed")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(key))

    async def fake_sync(self, store, fills=None):
        return {"fills_seen": 0, "fills_inserted": 0}

    async def fake_fills(self):
        return []

    monkeypatch.setattr(KalshiTruth, "sync_to_store", fake_sync)
    monkeypatch.setattr(KalshiTruth, "get_all_fills", fake_fills)
    assert main(["reconcile", "--db", str(tmp_path / "kve.sqlite3")]) == EXIT_OK
    assert "0 discrepancies" in capsys.readouterr().out


def test_reconcile_live_checks_recorded_orders_not_synced_fills(monkeypatch, tmp_path, capsys):
    """Regression: live reconcile synced exchange fills into the store, then compared the store
    with those same fills, so it could never report a discrepancy."""
    from kalshi_vol_edge.kalshi.truth import KalshiTruth
    from kalshi_vol_edge.storage.sqlite import TradeStore

    key = tmp_path / "k.pem"
    key.write_text("unused: KalshiTruth is stubbed")
    db = tmp_path / "kve.sqlite3"
    with TradeStore(db) as store:
        store.upsert_order(
            "vol_edge_15m_late_BTC_KXBTC15M-A", "KXBTC15M-A-00", "yes", "0.90", 10, "executed"
        )
        store.upsert_order(
            "vol_edge_15m_late_BTC_KXBTC15M-B", "KXBTC15M-B-00", "yes", "0.90", 10, "executed"
        )  # never filled: a phantom
    fills = [
        {
            "fill_id": "f1",
            "order_id": "o1",
            "client_order_id": "vol_edge_15m_late_BTC_KXBTC15M-A",
            "ticker": "KXBTC15M-A-00",
            "side": "yes",
            "action": "buy",
            "count_fp": "10.00",
            "yes_price_dollars": "0.9000",
            "created_time": "2026-03-23T00:25:31Z",
        },
        {
            "fill_id": "f2",
            "order_id": "o2",
            "ticker": "KXBTC15M-C-00",
            "side": "no",
            "action": "buy",
            "count_fp": "3.00",
            "created_time": "2026-03-23T00:40:00Z",
        },
    ]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(key))

    async def fake_fills(self):
        return fills

    async def fake_sync(self, store, fills=None):
        return {"fills_seen": len(fills or []), "fills_inserted": 0}

    monkeypatch.setattr(KalshiTruth, "get_all_fills", fake_fills)
    monkeypatch.setattr(KalshiTruth, "sync_to_store", fake_sync)
    assert main(["reconcile", "--db", str(db), "--json"]) == EXIT_OK
    report = json.loads(capsys.readouterr().out)  # progress lines go to stderr with --json
    assert report["summary"] == {"critical": 1, "warning": 0, "info": 1}
    phantom, unlogged = report["discrepancies"]
    assert phantom["local_value"] == "vol_edge_15m_late_BTC_KXBTC15M-B"
    assert unlogged["api_value"] == "o2"


def test_report_writes_a_static_site_with_relative_links(sample_dir: Path, tmp_path, capsys):
    out = tmp_path / "site"
    assert main(["report", "--fixtures", str(sample_dir), "--out", str(out)]) == EXIT_OK
    names = sorted(p.name for p in out.iterdir())
    assert names == [
        ".nojekyll",
        "backtest-kxbtcd.html",
        "basis.html",
        "index.html",
        "reconcile.html",
        "static",
    ]
    index = (out / "index.html").read_text()
    assert "Quotes are modeled." in index and 'href="static/app.css"' in index
    assert 'href="basis.html"' in index and 'href="/' not in index  # works from disk and Pages
    assert "data-poll" not in index and "Live view runs locally" in index
    assert (out / "static" / "app.js").exists()
    assert "Direction flips" in (out / "basis.html").read_text()


def test_report_refuses_a_missing_fixture(tmp_path, capsys):
    assert main(["report", "--fixtures", str(tmp_path / "nope"), "--out", str(tmp_path)]) == 2
    assert "fixture directory not found" in capsys.readouterr().err
