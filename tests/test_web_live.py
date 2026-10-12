"""Live view model and the failure-isolating live service (no network)."""

from __future__ import annotations

import pytest

from kalshi_vol_edge.data.live import LiveMarket
from kalshi_vol_edge.strategy.vol_edge import VolEdgeStrategy, fair_value, implied_vol
from kalshi_vol_edge.web.live import LiveService, build_live_view
from web_fakes import DownMarketData, FakeMarketData, candles_until, market_at, recorded_market

NOW = 1_800_000_000


def _view(markets: list[LiveMarket], spot: float | None = 70120.0, **kw):
    return build_live_view(
        now_ts=NOW,
        series="KXBTC15M",
        asset="BTC",
        markets=markets,
        settled=kw.pop("settled", []),
        spot=spot,
        candles=kw.pop("candles", candles_until(NOW)),
        strategy=VolEdgeStrategy(),
        **kw,
    )


def test_recorded_api_record_parses_dollar_strings():
    m = recorded_market()
    assert m.strike == pytest.approx(83499.07)
    assert (m.yes_bid, m.yes_ask, m.no_bid, m.no_ask) == (0.74, 0.75, 0.25, 0.26)
    assert m.close_ts - m.open_ts == 900
    assert m.result is None and m.expiration_value is None


def test_view_prices_the_open_market_with_the_strategy_model():
    m = market_at(NOW - 300)
    view = _view([m])
    mv = view.current
    assert mv is not None
    assert mv.minutes_left == pytest.approx(10.0) and mv.minutes_elapsed == pytest.approx(5.0)
    assert mv.lead == pytest.approx(120.0)
    assert mv.fair_yes == pytest.approx(fair_value(70120.0, 70000.0, view.model_vol, 10.0))
    assert mv.implied_vol == pytest.approx(implied_vol(0.615, 70120.0, 70000.0, 10.0))
    assert {a.arm for a in mv.arms} == {"early", "mid", "late"}
    assert view.arm_windows["late"] == "3-5.5 min left"
    assert 0.0 < view.model_vol <= 0.80
    assert view.realized_vol is not None


def test_view_drops_markets_that_already_closed_and_handles_missing_inputs():
    closed = market_at(NOW - 1200)
    assert _view([closed]).current is None
    no_spot = _view([market_at(NOW - 300)], spot=None).current
    assert no_spot is not None and no_spot.fair_yes is None and no_spot.arms == []
    no_candles = _view([], candles=[])
    assert no_candles.realized_vol is None and no_candles.model_vol_is_default
    assert no_candles.model_vol == VolEdgeStrategy().config.default_vol


def test_settled_market_basis_uses_last_completed_candle_before_close():
    candles = candles_until(NOW)
    close_ts = NOW - 600
    settled = market_at(
        close_ts - 900, strike=70100.0, result="no", expiration_value=70050.0, status="finalized"
    )
    s = _view([], candles=candles, settled=[settled]).settled[0]
    expected = next(c.close for c in candles if c.ts == close_ts - 60)
    assert s.coinbase_close == pytest.approx(expected)
    assert s.basis == pytest.approx(expected - 70050.0)
    assert s.direction_flip == (expected >= 70100.0)


def test_settled_market_outside_candle_window_has_no_basis():
    old = market_at(NOW - 86400, result="yes", expiration_value=70000.0)
    s = _view([], settled=[old]).settled[0]
    assert s.coinbase_close is None and s.basis is None


async def test_service_reports_every_source_down_without_raising():
    view = await LiveService(DownMarketData()).view()
    assert view.current is None and view.spot is None
    assert view.degraded and all(not s.ok for s in view.sources)
    assert {s.error for s in view.sources} == {"ConnectError"}


async def test_service_keeps_last_good_data_when_a_source_fails():
    clock = {"t": 0.0}
    source = FakeMarketData()
    service = LiveService(source, clock=lambda: source.now + clock["t"])
    first = await service.view()
    assert first.current is not None and not first.degraded

    async def boom(series: str):
        raise RuntimeError("upstream down")

    source.open_markets = boom  # type: ignore[method-assign]
    clock["t"] = 10.0
    second = await service.view()
    status = next(s for s in second.sources if s.name == "Kalshi markets")
    assert not status.ok and status.error == "RuntimeError"
    assert status.age_s == pytest.approx(10.0)
    assert second.current is not None  # last good market still shown


async def test_service_caches_within_ttl():
    source = FakeMarketData()
    service = LiveService(source, clock=lambda: float(source.now))
    await service.view()
    await service.view()
    assert source.calls == 1
