"""The ``kve dashboard`` web app.

Server-rendered pages (Jinja) with a small amount of vanilla JavaScript: the live panel is
re-fetched as an HTML fragment every few seconds, and charts get a hover read-out. There is
no build step and no client-side framework, which keeps the dashboard and the static report
on one set of templates.

Every page renders without network access. Live data comes from public, key-free endpoints;
when they fail the page says so and keeps the last good numbers. Account data is read-only
and only appears when a Kalshi key is configured.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from kalshi_vol_edge.backtest.engine import BacktestReport, Fixture, FixtureError
from kalshi_vol_edge.config import Settings, load_settings
from kalshi_vol_edge.data.live import MarketDataSource, PublicMarketData
from kalshi_vol_edge.web.account import AccountReader, load_account
from kalshi_vol_edge.web.live import LiveService
from kalshi_vol_edge.web.render import DEFAULT_SERIES, Renderer
from kalshi_vol_edge.web.views import (
    backtest_context,
    basis_context,
    load_recorded_reconcile,
    reconcile_context,
    run_fixture_backtest,
    spot_chart,
)

LIVE_REFRESH_MS = 5000


@lru_cache(maxsize=8)
def _cached_backtest(fixtures: Path, series: str) -> tuple[Fixture, BacktestReport]:
    return run_fixture_backtest(fixtures, series)


def create_app(
    settings: Settings | None = None,
    market_data: MarketDataSource | None = None,
    account_client: AccountReader | None = None,
) -> FastAPI:
    """Build the app. Tests inject a recorded ``market_data`` and a fake ``account_client``."""
    settings = settings or load_settings()
    fixtures = Path(settings.kve_fixtures_dir)
    owned: list[Any] = []

    if market_data is None:
        public = PublicMarketData(settings.kalshi_market_data_url, settings.coinbase_base_url)
        owned.append(public)
        market_data = public
    if account_client is None and settings.has_credentials:
        from kalshi_vol_edge.kalshi.client import KalshiClient

        kalshi = KalshiClient(settings)
        owned.append(kalshi)
        account_client = kalshi

    live = LiveService(market_data, series=settings.kalshi_series)
    renderer = Renderer(
        "app",
        has_credentials=settings.has_credentials,
        environment=settings.kalshi_environment.value,
        refresh_ms=LIVE_REFRESH_MS,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        for resource in owned:
            close = getattr(resource, "aclose", None) or resource.close
            await close()

    app = FastAPI(title="kalshi-vol-edge", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(files("kalshi_vol_edge.web") / "static")))

    def page(template: str, active: str, title: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        return HTMLResponse(
            renderer.render(template, active=active, title=title, **ctx), status_code=status
        )

    def fixture_error(active: str, title: str, exc: FixtureError) -> HTMLResponse:
        return page("error.html", active, title, status=503, error=str(exc), fixtures=fixtures)

    @app.get("/", response_class=HTMLResponse)
    async def live_page() -> HTMLResponse:
        view = await live.view()
        return page("live.html", "live", "Live", view=view, chart=spot_chart(view))

    @app.get("/live/panel", response_class=HTMLResponse)
    async def live_panel() -> HTMLResponse:
        view = await live.view()
        html = renderer.env.get_template("_live_panel.html").render(
            view=view, chart=spot_chart(view)
        )
        return HTMLResponse(html)

    @app.get("/api/live")
    async def live_json() -> JSONResponse:
        view = await live.view()
        return JSONResponse(jsonable_encoder(view))

    @app.get("/backtest", response_class=HTMLResponse)
    async def backtest_page(series: str = Query(DEFAULT_SERIES)) -> HTMLResponse:
        try:
            fixture, report = _cached_backtest(fixtures, series)
        except FixtureError as exc:
            return fixture_error("backtest", "Backtest", exc)
        return page(
            "backtest.html",
            "backtest",
            "Backtest",
            series_options=_series(fixture),
            **backtest_context(fixture, report),
        )

    @app.get("/api/backtest")
    async def backtest_json(series: str = Query(DEFAULT_SERIES)) -> JSONResponse:
        try:
            _, report = _cached_backtest(fixtures, series)
        except FixtureError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        return JSONResponse(jsonable_encoder(report.to_dict()))

    @app.get("/basis", response_class=HTMLResponse)
    async def basis_page() -> HTMLResponse:
        try:
            fixture, report = _cached_backtest(fixtures, DEFAULT_SERIES)
        except FixtureError as exc:
            return fixture_error("basis", "Settlement basis", exc)
        return page("basis.html", "basis", "Settlement basis", **basis_context(fixture, report))

    @app.get("/reconcile", response_class=HTMLResponse)
    async def reconcile_page() -> HTMLResponse:
        try:
            result = load_recorded_reconcile(fixtures)
        except FixtureError as exc:
            return fixture_error("reconcile", "Reconciliation", exc)
        return page("reconcile.html", "reconcile", "Reconciliation", **reconcile_context(result))

    @app.get("/api/reconcile")
    async def reconcile_json() -> JSONResponse:
        try:
            result = load_recorded_reconcile(fixtures)
        except FixtureError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        rows = [d.as_dict() for d in result.discrepancies]
        return JSONResponse(jsonable_encoder({"summary": result.counts, "discrepancies": rows}))

    @app.get("/account", response_class=HTMLResponse)
    async def account_page() -> HTMLResponse:
        snapshot = await load_account(settings, account_client)
        ctx: dict[str, Any] = {"account": snapshot}
        if snapshot.reconcile is not None:
            ctx.update(reconcile_context(snapshot.reconcile))
        return page("account.html", "account", "Account", **ctx)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _series(fixture: Fixture) -> list[str]:
    return [k for k, v in fixture.manifest["markets"].items() if isinstance(v, dict)]
