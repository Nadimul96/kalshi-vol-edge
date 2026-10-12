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
