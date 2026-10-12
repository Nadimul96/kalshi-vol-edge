"""Hand-made, dependency-free charts rendered on the server.

Each chart is an SVG that stretches to its container (``preserveAspectRatio="none"`` with
non-scaling strokes) plus HTML tick labels positioned in percent, so text stays crisp at any
width and the same markup works in the live dashboard and the static report. Hover is a
progressive enhancement: ``data-points`` (line charts) and ``data-tip`` (bars) are read by
``static/app.js``; without JavaScript the chart still renders completely.

All coordinates are computed here, so a chart is a pure function of its data and is covered
by unit tests.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from markupsafe import Markup, escape

VIEW = 1000.0  # SVG user units on both axes


def nice_ticks(lo: float, hi: float, target: int = 4) -> list[float]:
    """Round tick values covering ``[lo, hi]`` (1/2/5 steps)."""
    if not math.isfinite(lo) or not math.isfinite(hi):
        return []
    if hi == lo:
        hi, lo = hi + 1.0, lo - 1.0
    raw = (hi - lo) / max(target, 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks: list[float] = []
    v = start
    while v <= hi + step * 1e-9:
        ticks.append(round(v, 10))
        v += step
    if ticks and ticks[-1] < hi:
        ticks.append(round(ticks[-1] + step, 10))
    return ticks


@dataclass(frozen=True)
class RefLine:
    """A labelled horizontal reference (a strike, zero)."""

    value: float
    label: str
    tone: str = "ref"


@dataclass(frozen=True)
class Band:
    """A shaded vertical span on the x axis (the current market window)."""

    x0: float
    x1: float
    label: str


def _pct(v: float, lo: float, hi: float) -> float:
    return 0.0 if hi == lo else (v - lo) / (hi - lo) * 100.0


def line_chart(
    xs: Sequence[float],
    ys: Sequence[float],
    *,
    tips: Sequence[str],
    x_ticks: Sequence[tuple[float, str]],
    y_format: Callable[[float], str],
    label: str,
    refs: Sequence[RefLine] = (),
    band: Band | None = None,
    area: bool = True,
    include_zero: bool = False,
    height: int = 220,
) -> Markup:
    """A single-series line chart with optional reference lines and a shaded band."""
    if not xs or len(xs) != len(ys):
        return Markup('<div class="chart-empty">No data to plot</div>')
    x_lo, x_hi = min(xs), max(xs)
    if band is not None:
        x_hi = max(x_hi, band.x1)
    values = [*ys, *(r.value for r in refs)] + ([0.0] if include_zero else [])
    ticks = nice_ticks(min(values), max(values))
    y_lo, y_hi = ticks[0], ticks[-1]

    def px(x: float) -> float:
        return _pct(x, x_lo, x_hi) * VIEW / 100.0

    def py(y: float) -> float:
        return VIEW - _pct(y, y_lo, y_hi) * VIEW / 100.0

    pts = [(px(x), py(y)) for x, y in zip(xs, ys, strict=True)]
    line_d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    parts = [
        f'<svg viewBox="0 0 {VIEW:.0f} {VIEW:.0f}" preserveAspectRatio="none" aria-hidden="true">'
    ]
    parts += [
        f'<line class="grid" x1="0" x2="{VIEW:.0f}" y1="{py(t):.1f}" y2="{py(t):.1f}"/>'
        for t in ticks
    ]
    if band is not None:
        bx0, bx1 = px(max(band.x0, x_lo)), px(band.x1)
        parts.append(
            f'<rect class="band" x="{bx0:.1f}" y="0" width="{max(bx1 - bx0, 0):.1f}" '
            f'height="{VIEW:.0f}"/>'
        )
    if area:
        base = py(max(y_lo, min(0.0, y_hi))) if include_zero else VIEW
        area_d = f"{line_d} L{pts[-1][0]:.1f},{base:.1f} L{pts[0][0]:.1f},{base:.1f} Z"
        parts.append(f'<path class="area" d="{area_d}"/>')
    parts += [
        f'<line class="ref ref-{escape(r.tone)}" x1="0" x2="{VIEW:.0f}" '
        f'y1="{py(r.value):.1f}" y2="{py(r.value):.1f}"/>'
        for r in refs
    ]
    parts.append(f'<path class="line" d="{line_d}"/>')
    parts.append("</svg>")

    overlay = [
        f'<span class="tick tick-y" style="top:{100 - _pct(t, y_lo, y_hi):.2f}%">'
        f"{escape(y_format(t))}</span>"
        for t in ticks
    ]
    overlay += [
        f'<span class="tick tick-x" style="left:{_pct(x, x_lo, x_hi):.2f}%">{escape(text)}</span>'
        for x, text in x_ticks
        if x_lo <= x <= x_hi
    ]
    overlay += [
        f'<span class="ref-label ref-{escape(r.tone)}" '
        f'style="top:{100 - _pct(r.value, y_lo, y_hi):.2f}%">{escape(r.label)}</span>'
        for r in refs
    ]
    if band is not None:
        overlay.append(
            f'<span class="band-label" style="left:{_pct(max(band.x0, x_lo), x_lo, x_hi):.2f}%">'
            f"{escape(band.label)}</span>"
        )
    last_x, last_y = _pct(xs[-1], x_lo, x_hi), 100 - _pct(ys[-1], y_lo, y_hi)
    overlay.append(f'<span class="end-dot" style="left:{last_x:.2f}%;top:{last_y:.2f}%"></span>')

    points = [
        [round(_pct(x, x_lo, x_hi), 3), round(100 - _pct(y, y_lo, y_hi), 3), tip]
        for x, y, tip in zip(xs, ys, tips, strict=True)
    ]
    return Markup(
        f'<figure class="chart" style="--chart-h:{height}px" role="img" '
        f'aria-label="{escape(label)}" data-points="{escape(json.dumps(points))}">'
        f'<div class="chart-plot">{"".join(parts)}{"".join(overlay)}</div></figure>'
    )


@dataclass(frozen=True)
class Bin:
    """One histogram bin ``[lo, hi)``."""

    lo: float
    hi: float
    count: int


def bin_values(values: Sequence[float], width: float) -> list[Bin]:
    """Fixed-width bins aligned to multiples of ``width``, covering every value."""
    if not values:
        return []
    lo = math.floor(min(values) / width) * width
    hi = math.floor(max(values) / width) * width + width
    n = round((hi - lo) / width)
    counts = [0] * n
    for v in values:
        counts[min(n - 1, int((v - lo) // width))] += 1
    return [Bin(lo + i * width, lo + (i + 1) * width, c) for i, c in enumerate(counts)]


def histogram(
    bins: Sequence[Bin],
    *,
    label: str,
    x_format: Callable[[float], str],
    unit: str,
    highlight_zero: bool = True,
    height: int = 200,
) -> Markup:
    """Columns with 4px rounded caps, a 2px gap, a hover tip per bin and labelled edges."""
    if not bins:
        return Markup('<div class="chart-empty">No data to plot</div>')
    ticks = nice_ticks(0, max(b.count for b in bins), target=3)
    top = ticks[-1]
    cols = []
    for b in bins:
        plural = unit if b.count == 1 else unit + "s"
        tip = f"{x_format(b.lo)} to {x_format(b.hi)}: {b.count} {plural}"
        cols.append(
            f'<div class="hist-col" data-tip="{escape(tip)}">'
            f'<div class="hist-bar" style="height:{_pct(b.count, 0, top):.2f}%"></div></div>'
        )
    grid = "".join(
        f'<span class="hist-grid" style="top:{100 - _pct(t, 0, top):.2f}%"></span>'
        f'<span class="tick tick-y" style="top:{100 - _pct(t, 0, top):.2f}%">{t:g}</span>'
        for t in ticks
    )
    lo, hi = bins[0].lo, bins[-1].hi
    step = max(1, math.ceil(len(bins) / 6))
    edges = [b.lo for b in bins[::step]]
    if len(bins) % step == 0:
        edges.append(hi)
    xt = "".join(
        f'<span class="tick tick-x" style="left:{_pct(e, lo, hi):.2f}%">'
        f"{escape(x_format(e))}</span>"
        for e in edges
    )
    zero = (
        f'<span class="hist-zero" style="left:{_pct(0.0, lo, hi):.2f}%"></span>'
        if highlight_zero and lo < 0 < hi
        else ""
    )
    return Markup(
        f'<figure class="chart hist" style="--chart-h:{height}px" role="img" '
        f'aria-label="{escape(label)}"><div class="chart-plot">{grid}'
        f'<div class="hist-cols">{"".join(cols)}</div>{zero}{xt}</div></figure>'
    )
