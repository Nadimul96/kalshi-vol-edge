"""Jinja environment and URL scheme shared by the dashboard and the static report.

The two differ only in where links point: the dashboard has routes (``/backtest``), the
static report has files (``backtest-kxbtcd.html``) and no live or account pages. Templates
call ``links.*`` and never hard-code a URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from kalshi_vol_edge import __version__
from kalshi_vol_edge.web.format import FILTERS

Mode = Literal["app", "site"]
DEFAULT_SERIES = "KXBTC15M"
REPO_URL = "https://github.com/Nadimul96/kalshi-vol-edge"


@dataclass(frozen=True)
class NavItem:
    """One tab in the top bar."""

    key: str
    label: str
    href: str


@dataclass(frozen=True)
class Links:
    """URL scheme for one rendering mode."""

    mode: Mode

    @property
    def static(self) -> str:
        """Prefix for static assets."""
        return "/static/" if self.mode == "app" else "static/"

    def backtest(self, series: str = DEFAULT_SERIES) -> str:
        """Backtest page for ``series``."""
        if self.mode == "app":
            return "/backtest" if series == DEFAULT_SERIES else f"/backtest?series={series}"
        return "index.html" if series == DEFAULT_SERIES else f"backtest-{series.lower()}.html"

    @property
    def basis(self) -> str:
        """Settlement-basis page."""
        return "/basis" if self.mode == "app" else "basis.html"

    @property
    def reconcile(self) -> str:
        """Reconciliation page."""
        return "/reconcile" if self.mode == "app" else "reconcile.html"

    def nav(self) -> list[NavItem]:
        """Tabs, in order. The static report has no live or account tabs."""
        items = [
            NavItem("backtest", "Backtest", self.backtest()),
            NavItem("basis", "Settlement basis", self.basis),
            NavItem("reconcile", "Reconciliation", self.reconcile),
        ]
        if self.mode == "app":
            items.insert(0, NavItem("live", "Live", "/"))
            items.append(NavItem("account", "Account", "/account"))
        return items


def make_environment() -> Environment:
    """The Jinja environment with the package's templates, filters and globals."""
    env = Environment(
        loader=PackageLoader("kalshi_vol_edge.web", "templates"),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(FILTERS)
    env.globals.update(version=__version__, repo_url=REPO_URL)
    return env


class Renderer:
    """Renders a page template with the context every page needs."""

    def __init__(self, mode: Mode, **globals_: Any) -> None:
        self.links = Links(mode)
        self.env = make_environment()
        self.env.globals.update(links=self.links, mode=mode, **globals_)

    def render(self, template: str, *, active: str, title: str, **context: Any) -> str:
        """Render ``template`` as the ``active`` tab."""
        return self.env.get_template(template).render(
            active=active, title=title, nav=self.links.nav(), **context
        )
