"""Backtest engine: deterministic replay and loud failure."""

import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

from kalshi_vol_edge.backtest.engine import (
    BacktestTrade,
    FixtureError,
    ModeledQuotes,
    RecordedQuotes,
    format_report,
    load_fixture,
    run_backtest,
)
from kalshi_vol_edge.kalshi.order_ids import build_client_order_id
from kalshi_vol_edge.strategy.vol_edge import Decision, fair_value


def test_sample_fixture_replay_is_deterministic(sample_dir: Path):
    a = run_backtest(load_fixture(sample_dir))
    b = run_backtest(load_fixture(sample_dir))
    assert a.to_dict() == b.to_dict()
    # pinned result of the committed fixture under its declared quote model
    assert a.markets == 30 and a.decision_minutes == 420
    assert a.trade_count == 9 and a.wins == 4
    assert a.win_rate == pytest.approx(4 / 9)
    assert round(a.net_pnl, 2) == 8.94
    assert round(a.fees, 2) == 0.72
    assert round(a.max_drawdown, 2) == 4.55
    assert all(t.edge >= 0 for t in a.trades)  # every arm applies its min_edge
    assert set(a.by_arm()) == {"early"}
    basis = a.basis_summary()
    assert basis["markets"] == 30 and basis["direction_flips"] == 2
    assert 10 < basis["mean_abs_basis"] < 25
    assert round(basis["median_abs_basis"], 2) == 12.96  # true median of 30 values
    text = format_report(a)
    assert "MODELED" in text and "trades           : 9" in text
    assert "direction flips" in text


def test_hourly_series_replays_all_three_arms(sample_dir: Path):
    report = run_backtest(load_fixture(sample_dir, series="KXBTCD"))
    assert report.markets == 672 and report.decision_minutes == 672 * 59
    assert report.trade_count == 21 and report.wins == 15
    arms = report.by_arm()
    assert set(arms) == {"early", "mid", "late"}
    assert arms["late"]["trades"] == 10 and arms["late"]["wins"] == 10
    assert report.max_drawdown >= 0
    assert report.net_pnl == pytest.approx(report.gross_pnl - report.fees)


def test_each_arm_trades_once_per_event_like_the_executor(sample_dir: Path):
    """Regression: the backtest reset idempotency per market, so an hourly event with 36
    strikes could trade one arm many times while the live key allows one order per event."""
    report = run_backtest(load_fixture(sample_dir, series="KXBTCD"))
    per_event_arm = Counter((t.event_ticker, t.arm) for t in report.trades)
    assert per_event_arm and max(per_event_arm.values()) == 1
    keys = {
        build_client_order_id("vol_edge_15m", t.arm, "BTC", t.event_ticker) for t in report.trades
    }
    assert len(keys) == report.trade_count  # every backtest trade maps to a distinct live key


def test_missing_fixture_directory_fails_loudly(tmp_path: Path):
    with pytest.raises(FixtureError, match="fixture directory not found"):
        load_fixture(tmp_path / "nonexistent")


def test_missing_manifest_and_files_fail_loudly(sample_dir: Path, tmp_path: Path):
    root = tmp_path / "fx"
    root.mkdir()
    with pytest.raises(FixtureError, match="manifest not found"):
        load_fixture(root)
    manifest = json.loads((sample_dir / "manifest.json").read_text())
    (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(FixtureError, match="candle fixture not found"):
        load_fixture(root)
    shutil.copy(sample_dir / "candles_BTC_1m.json", root)
    with pytest.raises(FixtureError, match="market fixture not found"):
        load_fixture(root)
    shutil.copy(sample_dir / "markets_KXBTC15M.json", root)
    with pytest.raises(FixtureError, match="not in fixture"):
        load_fixture(root, series="KXETH15M")
    assert load_fixture(root).series == "KXBTC15M"


def test_unsettled_market_and_uncovered_candles_are_rejected(sample_dir: Path, tmp_path: Path):
    root = tmp_path / "fx"
    shutil.copytree(sample_dir, root)
    markets = json.loads((root / "markets_KXBTC15M.json").read_text())
    markets[0]["result"] = None
    (root / "markets_KXBTC15M.json").write_text(json.dumps(markets))
    with pytest.raises(FixtureError, match="no settlement result"):
        load_fixture(root)
    markets[0]["result"] = "yes"
    markets[0]["open_time"] = "2026-03-20T00:00:00+00:00"
    markets[0]["close_time"] = "2026-03-20T00:15:00+00:00"
    (root / "markets_KXBTC15M.json").write_text(json.dumps(markets))
    with pytest.raises(FixtureError, match="candles do not cover"):
        load_fixture(root)


def test_unknown_quote_source_is_rejected(sample_dir: Path, tmp_path: Path):
    root = tmp_path / "fx"
    shutil.copytree(sample_dir, root)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["quotes"] = {"source": "guess"}
    (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(FixtureError, match=r"unknown quotes\.source"):
        load_fixture(root)


def test_modeled_quotes_are_fair_value_plus_spread_in_cents():
    q = ModeledQuotes(implied_vol=0.40, half_spread=0.02)
    yes_ask, no_ask = q.quote("T", 0, 70100.0, 70000.0, 4.0)
    fv = fair_value(70100.0, 70000.0, 0.40, 4.0)
    assert yes_ask == round(fv + 0.02, 2) and no_ask == round(1 - fv + 0.02, 2)
    assert q.quote("T", 0, 80000.0, 70000.0, 4.0) == (0.99, 0.02)
    assert "MODELED" in q.description


def test_recorded_quotes_respect_max_age(tmp_path: Path):
    path = tmp_path / "quotes.jsonl"
    path.write_text('{"ticker":"T","ts":100,"yes_ask":0.6,"no_ask":0.42}\n')
    q = RecordedQuotes.from_file(path, max_age_s=60)
    assert q.quote("T", 120, 0, 0, 0) == (0.6, 0.42)
    assert q.quote("T", 200, 0, 0, 0) is None  # too old: no fill is invented
    assert q.quote("T", 90, 0, 0, 0) is None  # from the future: not used
    with pytest.raises(FixtureError):
        RecordedQuotes.from_file(tmp_path / "missing.jsonl")


def test_trade_scoring_holds_to_expiry_without_exit_fee():
    d = Decision(
        arm="late",
        side="yes",
        price=0.90,
        contracts=10,
        win_prob=0.95,
        edge=0.02,
        z_score=1.282,
        required_lead=10,
        fee=0.07,
        reason="",
    )
    assert BacktestTrade.score(d, True) == pytest.approx(10 * 0.10 - 0.07)
    assert BacktestTrade.score(d, False) == pytest.approx(-9.0 - 0.07)
