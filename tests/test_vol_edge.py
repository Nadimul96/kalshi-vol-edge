"""Vol-edge pricing functions and the three-arm strategy."""

import math
from dataclasses import replace
from statistics import NormalDist

import pytest

from kalshi_vol_edge.kalshi.fees import taker_fee
from kalshi_vol_edge.strategy.vol_edge import (
    Z_80,
    Z_90,
    MarketSnapshot,
    StrategyConfig,
    VolEdgeStrategy,
    dollar_sigma,
    fair_value,
    implied_vol,
    mid_entry_z_score,
    realized_vol_annualized,
    required_lead,
    win_probability,
)


def _snap(**kw) -> MarketSnapshot:
    base = dict(
        event_ticker="KXBTC15M-26MAR222030",
        ticker="KXBTC15M-26MAR222030-30",
        asset="BTC",
        strike=70000.0,
        spot=70100.0,
        minutes_left=4.0,
        minutes_elapsed=11.0,
        annual_vol=0.40,
        yes_ask=0.90,
        no_ask=0.12,
    )
    base.update(kw)
    return MarketSnapshot(**base)


def test_required_lead_scales_with_sqrt_time_and_vol():
    a = required_lead(70000, 0.40, 4.0, Z_90)
    b = required_lead(70000, 0.40, 16.0, Z_90)
    c = required_lead(70000, 0.80, 4.0, Z_90)
    assert b == pytest.approx(2 * a)
    assert c == pytest.approx(2 * a)
    assert a == pytest.approx(Z_90 * dollar_sigma(70000, 0.40, 4.0))


def test_win_probability_is_the_log_normal_fair_value():
    """Regression: win_probability used arithmetic Brownian motion while fair_value, which
    prices the quotes, was log-normal."""
    fv = fair_value(70100, 70000, 0.40, 4.0)
    assert win_probability("yes", 70100, 70000, 0.40, 4.0) == fv
    assert win_probability("no", 70100, 70000, 0.40, 4.0) == pytest.approx(1 - fv)
    assert win_probability("yes", 70050, 70000, 0.4, 4.0) > win_probability(
        "yes", 70050, 70000, 0.4, 8.0
    )
    assert win_probability("yes", 70010, 70000, 0.40, 0.0) == 1.0
    assert win_probability("no", 69990, 70000, 0.40, 0.0) == 1.0


@pytest.mark.parametrize("vol", [0.20, 0.40, 0.80])
@pytest.mark.parametrize("minutes", [1.0, 4.0, 15.0])
@pytest.mark.parametrize("z", [Z_80, Z_90])
def test_dollar_lead_gate_agrees_with_the_log_normal_probability(vol, minutes, z):
    spot = 70000.0
    lead = required_lead(spot, vol, minutes, z)
    target = NormalDist().cdf(z)
    assert win_probability("yes", spot, spot - lead, vol, minutes) == pytest.approx(
        target, abs=1e-3
    )
    assert win_probability("no", spot, spot + lead, vol, minutes) == pytest.approx(target, abs=1e-3)


@pytest.mark.parametrize("realized", [0.25, 0.40, 0.60])
@pytest.mark.parametrize("quote_vol", [0.30, 0.50])
def test_price_edge_is_the_realized_vs_implied_comparison(realized, quote_vol):
    strike, minutes = 70000.0, 6.0
    # the side that is ahead (late and mid arms): cheap exactly when realized < implied
    ahead = fair_value(70150.0, strike, quote_vol, minutes)
    iv = implied_vol(ahead, 70150.0, strike, minutes)
    assert iv is not None
    edge = win_probability("yes", 70150.0, strike, realized, minutes) - ahead
    assert (edge > 0) == (realized < iv)
    # the side that is behind (early arm): cheap exactly when realized > implied
    behind = fair_value(69950.0, strike, quote_vol, minutes)
    iv = implied_vol(behind, 69950.0, strike, minutes)
    assert iv is not None
    edge = win_probability("yes", 69950.0, strike, realized, minutes) - behind
    assert (edge > 0) == (realized > iv)


def test_fair_value_atm_near_half_and_expiry_binary():
    assert 0.45 < fair_value(70000, 70000, 0.40, 10) < 0.55
    assert fair_value(70010, 70000, 0.40, 0) == 1.0
    assert fair_value(69990, 70000, 0.40, 0) == 0.0
    with pytest.raises(ValueError):
        fair_value(0, 70000, 0.4, 5)


@pytest.mark.parametrize("sigma", [0.25, 0.40, 0.90])
@pytest.mark.parametrize("spot", [69900.0, 70000.0, 70150.0])
def test_implied_vol_inverts_fair_value(sigma, spot):
    price = fair_value(spot, 70000.0, sigma, 6.0)
    iv = implied_vol(price, spot, 70000.0, 6.0)
    if not 0.01 < price < 0.99:
        # a 0.99+ quote is consistent with any low vol; the solver must say so, not guess
        assert iv is None or fair_value(spot, 70000.0, iv, 6.0) == pytest.approx(price, abs=1e-5)
        return
    assert iv is not None
    # the inversion must reproduce the quote; at the money vega is ~0 so sigma itself is not
    # identifiable from a price, which is the honest limit of the method
    assert fair_value(spot, 70000.0, iv, 6.0) == pytest.approx(price, abs=1e-5)
    if spot != 70000.0:
        assert iv == pytest.approx(sigma, rel=1e-3)


def test_implied_vol_returns_none_for_unreachable_quotes():
    assert implied_vol(0.999, 69000, 70000, 1.0) is None  # deep OTM cannot be 0.999
    assert implied_vol(0.5, 70000, 70000, 0.0) is None  # no time left
    assert implied_vol(1.0, 70000, 70000, 5.0) is None


def test_mid_entry_z_interpolates_and_clamps():
    assert mid_entry_z_score(5.0) == pytest.approx(Z_80)
    assert mid_entry_z_score(8.0) == pytest.approx(Z_90)
    assert mid_entry_z_score(6.5) == pytest.approx((Z_80 + Z_90) / 2)
    assert mid_entry_z_score(0.0) == pytest.approx(Z_80)
    assert mid_entry_z_score(14.0) == pytest.approx(Z_90)


def test_realized_vol_recovers_known_sigma_and_refuses_short_series():
    import numpy as np

    rng = np.random.default_rng(7)
    per_min = 0.60 / math.sqrt(365.25 * 24 * 60)
    closes = 70000 * np.exp(np.cumsum(rng.standard_normal(5000) * per_min))
    assert realized_vol_annualized(closes) == pytest.approx(0.60, rel=0.05)
    assert realized_vol_annualized([1.0, 2.0, 3.0]) is None
    assert realized_vol_annualized(closes, floor=0.7) == 0.7


def test_late_arm_fires_only_when_lead_clears_threshold_and_edge_positive():
    strat = VolEdgeStrategy()
    sigma = dollar_sigma(70100, 0.40, 4.0)
    # lead well above z*sigma, cheap quote -> fires
    strong = _snap(spot=70000 + 3 * sigma, yes_ask=0.90, no_ask=0.12)
    decisions = strat.evaluate(strong)
    assert [d.arm for d in decisions] == ["late"]
    d = decisions[0]
    assert d.side == "yes"
    assert d.price == 0.90
    assert d.contracts == 22  # min(40, int(20 / 0.90))
    assert d.fee == taker_fee(22, 0.90)
    assert d.edge == pytest.approx(d.win_prob - 0.90 - d.fee / 22)
    # lead too small -> nothing
    assert strat.evaluate(_snap(spot=70000 + 0.5 * sigma)) == []


def test_late_arm_respects_price_cap_and_min_edge():
    strat = VolEdgeStrategy()
    sigma = dollar_sigma(70100, 0.40, 4.0)
    assert strat.evaluate(_snap(spot=70000 + 3 * sigma, yes_ask=0.98)) == []  # above 0.97 cap
    # a quote at 0.99 edge would be negative even if allowed; a fair quote leaves no edge
    fair = _snap(spot=70000 + 1.5 * sigma, yes_ask=0.97)
    assert strat.evaluate(fair) == []


def test_late_arm_trades_no_side_when_spot_below_strike():
    strat = VolEdgeStrategy()
    sigma = dollar_sigma(69900, 0.40, 4.0)
    snap = _snap(spot=70000 - 3 * sigma, yes_ask=0.10, no_ask=0.88)
    (d,) = strat.evaluate(snap)
    assert d.side == "no" and d.price == 0.88


def test_mid_arm_window_and_price_band():
    strat = VolEdgeStrategy()
    sigma = dollar_sigma(70100, 0.40, 8.0)
    in_window = _snap(
        minutes_left=8.0, minutes_elapsed=7.0, spot=70000 + 3 * sigma, yes_ask=0.70, no_ask=0.32
    )
    (d,) = strat.evaluate(in_window)
    assert d.arm == "mid"
    assert d.z_score == pytest.approx(mid_entry_z_score(7.0))
    assert d.contracts == 7  # min(10, int(5 / 0.70))
    # same setup but the quote is in late-arm territory -> skipped
    assert strat.evaluate(replace(in_window, yes_ask=0.85)) == []


def test_early_arm_fades_a_drop_with_yes_only():
    strat = VolEdgeStrategy()
    drop = _snap(minutes_left=11.0, minutes_elapsed=4.0, spot=69950.0, yes_ask=0.30, no_ask=0.72)
    (d,) = strat.evaluate(drop)
    assert d.arm == "early" and d.side == "yes" and d.contracts == 6
    assert d.edge >= StrategyConfig().early.min_edge
    # a rise is not faded (YES-only filter), nor is a drop smaller than $40
    rise = _snap(minutes_left=11.0, minutes_elapsed=4.0, spot=70050.0, yes_ask=0.60, no_ask=0.42)
    assert strat.evaluate(rise) == []
    small = _snap(minutes_left=11.0, minutes_elapsed=4.0, spot=69970.0, yes_ask=0.45, no_ask=0.57)
    assert strat.evaluate(small) == []


def test_early_arm_refuses_negative_edge():
    """Regression: the early arm skipped the min_edge check and bought overpriced YES."""
    strat = VolEdgeStrategy()
    # P(recover a $50 drop with 11 minutes left at 40% vol) is about 0.35: 0.40 is too dear
    dear = _snap(minutes_left=11.0, minutes_elapsed=4.0, spot=69950.0, yes_ask=0.40, no_ask=0.62)
    assert strat.evaluate(dear) == []
    early = next(c for c in strat.diagnose(dear) if c.arm == "early")
    assert early.edge is not None and early.edge < 0
    assert early.decision is None and early.reason.startswith("edge -")


def test_arms_are_idempotent_per_event():
    strat = VolEdgeStrategy()
    sigma = dollar_sigma(70100, 0.40, 4.0)
    snap = _snap(spot=70000 + 3 * sigma)
    assert strat.evaluate(snap, traded_arms=frozenset({"late"})) == []


def test_clamp_vol_uses_default_when_unknown():
    strat = VolEdgeStrategy(StrategyConfig())
    assert strat.clamp_vol(None) == 0.40
    assert strat.clamp_vol(0.05) == 0.20
    assert strat.clamp_vol(5.0) == 0.80
    assert strat.clamp_vol(0.5) == 0.5


def test_diagnose_reports_every_arm_with_a_reason():
    strat = VolEdgeStrategy()
    checks = {c.arm: c for c in strat.diagnose(_snap(spot=70300.0, yes_ask=0.95))}
    assert set(checks) == {"early", "mid", "late"}
    assert checks["late"].window_open and checks["late"].decision is not None
    assert checks["late"].reason == "fires"
    assert not checks["mid"].window_open and checks["mid"].reason == "outside entry window"
    assert checks["mid"].decision is None and checks["mid"].required_lead > 0


def test_diagnose_explains_a_lead_below_threshold_and_matches_evaluate():
    strat = VolEdgeStrategy()
    snap = _snap(spot=70005.0)
    late = next(c for c in strat.diagnose(snap) if c.arm == "late")
    assert late.decision is None and late.reason.startswith("lead $5 below required")
    assert late.required_lead == pytest.approx(required_lead(70005.0, 0.40, 4.0, Z_90))
    assert strat.evaluate(snap) == []
