"""Dashboard routes through FastAPI's TestClient with recorded and failing feeds."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kalshi_vol_edge.config import Settings
from kalshi_vol_edge.web.app import create_app
from web_fakes import DownMarketData, FakeAccount, FakeMarketData


def _settings(sample_dir: Path, **kw) -> Settings:
    base = dict(
        _env_file=None,
        kalshi_api_key_id="",
        kalshi_private_key_path=None,
        kve_fixtures_dir=sample_dir,
    )
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def client(sample_dir: Path):
    app = create_app(_settings(sample_dir), market_data=FakeMarketData())
    with TestClient(app) as c:
        yield c


def test_live_page_renders_model_view_and_disclaimer(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "KXBTC15M-TEST-" in html and "$70,120.00" in html
    assert "Model output, not advice" in html and "Arms on this market" in html
    assert 'data-poll="/live/panel"' in html and "No key" in html


def test_live_panel_is_a_fragment(client):
    r = client.get("/live/panel")
    assert r.status_code == 200
    assert "<html" not in r.text and "Arms on this market" in r.text


def test_live_json_exposes_model_numbers(client):
    data = client.get("/api/live").json()
    market = data["markets"][0]
    assert 0 < market["fair_yes"] < 1
    assert {a["arm"] for a in market["arms"]} == {"early", "mid", "late"}
    assert all(s["ok"] for s in data["sources"])


def test_live_page_degrades_gracefully_when_upstreams_are_down(sample_dir):
    app = create_app(_settings(sample_dir), market_data=DownMarketData())
    with TestClient(app) as c:
        r = c.get("/")
    assert r.status_code == 200
    assert "Some sources are unreachable" in r.text
    assert "No open KXBTC15M market right now" in r.text


def test_backtest_page_shows_modeled_quote_caveat_and_pinned_numbers(client):
    html = client.get("/backtest").text
    assert "Quotes are modeled." in html and "implied vol 40%" in html
    assert "+$6.50" in html and "38.5%" in html and "Equity curve" in html
    assert html.count('class="mono">KXBTC15M-') == 13


def test_backtest_hourly_series_and_json(client):
    html = client.get("/backtest", params={"series": "KXBTCD"}).text
    assert "672 settled markets" in html
    data = client.get("/api/backtest").json()
    assert data["trade_count"] == 13 and data["quote_source"].startswith("MODELED")


def test_unknown_series_is_a_clear_error_not_a_crash(client):
    r = client.get("/backtest", params={"series": "NOPE"})
    assert r.status_code == 503 and "not in fixture" in r.text


def test_basis_page_shows_flips_and_histogram(client):
    html = client.get("/basis").text
    assert "Direction flips" in html and 'class="chart hist"' in html
    assert html.count("row-flag") == 2


def test_reconcile_page_and_json(client):
    html = client.get("/reconcile").text
    assert "Recorded fixture" in html and "phantom trade" in html and "Fee mismatch" in html
    data = client.get("/api/reconcile").json()
    assert data["summary"] == {"critical": 3, "warning": 0, "info": 1}


def test_missing_fixture_directory_is_reported(tmp_path):
    app = create_app(_settings(tmp_path / "nope"), market_data=FakeMarketData())
    with TestClient(app) as c:
        r = c.get("/backtest")
        assert r.status_code == 503 and "fixture directory not found" in r.text
        assert c.get("/api/reconcile").status_code == 503


def test_account_without_key_explains_the_free_path(client):
    html = client.get("/account").text
    assert "No Kalshi key configured" in html and "demo" in html


def test_account_with_key_is_read_only(sample_dir, tmp_path):
    key = tmp_path / "kalshi.pem"
    key.write_text("not used: the account client is faked")
    db = tmp_path / "kve.sqlite3"
    settings = _settings(
        sample_dir, kalshi_api_key_id="test-key", kalshi_private_key_path=key, kve_db_path=db
    )
    account = FakeAccount()
    app = create_app(settings, market_data=FakeMarketData(), account_client=account)
    with TestClient(app) as c:
        html = c.get("/account").text
    assert "$123.45" in html and "KXBTC15M-26OCT120000-00" in html
    assert "Live account (demo, read-only)" in html
    assert "No local database" in html and not db.exists()  # never created
    assert "test-key" not in html
    assert set(account.calls) == {"balance", "positions:unsettled", "fills"}


def test_static_assets_and_health(client):
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/healthz").json() == {"status": "ok"}
