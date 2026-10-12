"""``kve report``: the backtest, basis and reconciliation pages as a static site.

Same templates, same CSS and same charts as the dashboard, written to plain HTML files with
relative links, so the output can be opened from disk or served by GitHub Pages. The live
and account pages are left out: they need a running server.
"""

from __future__ import annotations

import shutil
from importlib.resources import as_file, files
from pathlib import Path

from kalshi_vol_edge.web.render import DEFAULT_SERIES, Renderer
from kalshi_vol_edge.web.views import (
    backtest_context,
    basis_context,
    load_recorded_reconcile,
    now_label,
    reconcile_context,
    run_fixture_backtest,
)

STATIC_FILES = ("app.css", "app.js", "favicon.svg")


def build_site(fixtures: Path, out: Path) -> list[Path]:
    """Render every static page into ``out``. Raises ``FixtureError`` on a bad fixture."""
    fixture, report = run_fixture_backtest(fixtures, DEFAULT_SERIES)
    series = [k for k, v in fixture.manifest["markets"].items() if isinstance(v, dict)]
    renderer = Renderer("site", generated=now_label())
    links = renderer.links
    pages: dict[str, str] = {}

    for s in series:
        fx, rep = (fixture, report) if s == DEFAULT_SERIES else run_fixture_backtest(fixtures, s)
        pages[links.backtest(s)] = renderer.render(
            "backtest.html",
            active="backtest",
            title="Backtest",
            series_options=series,
            **backtest_context(fx, rep),
        )
    pages[links.basis] = renderer.render(
        "basis.html", active="basis", title="Settlement basis", **basis_context(fixture, report)
    )
    pages[links.reconcile] = renderer.render(
        "reconcile.html",
        active="reconcile",
        title="Reconciliation",
        **reconcile_context(load_recorded_reconcile(fixtures)),
    )

    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, html in pages.items():
        path = out / name
        path.write_text(html, encoding="utf-8")
        written.append(path)
    static_out = out / "static"
    static_out.mkdir(exist_ok=True)
    for name in STATIC_FILES:
        with as_file(files("kalshi_vol_edge.web") / "static" / name) as src:
            shutil.copyfile(src, static_out / name)
    (out / ".nojekyll").write_text("")  # serve files as-is on GitHub Pages
    return written
