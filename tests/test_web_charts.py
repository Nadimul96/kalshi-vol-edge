"""Server-rendered charts and number formatting."""

from __future__ import annotations

import json
import re
from itertools import pairwise

import pytest

from kalshi_vol_edge.web.charts import Band, RefLine, bin_values, histogram, line_chart, nice_ticks
from kalshi_vol_edge.web.format import age, cents, clock, num, pct, tone, usd


def test_nice_ticks_cover_range_with_round_steps():
    assert nice_ticks(0, 10) == [0, 5, 10]
    ticks = nice_ticks(83412, 83603)
    assert ticks[0] <= 83412 and ticks[-1] >= 83603
    steps = {round(b - a, 6) for a, b in pairwise(ticks)}
    assert len(steps) == 1 and steps.pop() in (50, 100)
    assert nice_ticks(5, 5)  # flat series still gets an axis


def test_bin_values_aligns_to_width_and_counts_everything():
    bins = bin_values([-25.9, -24.5, -1.0, 0.0, 11.9, 52.4], 10.0)
    assert bins[0].lo == -30 and bins[-1].hi == 60
    assert sum(b.count for b in bins) == 6
    assert next(b for b in bins if b.lo == -30).count == 2


def test_line_chart_emits_points_refs_and_band_in_percent():
    html = str(
        line_chart(
            [0, 1, 2],
            [10.0, 20.0, 15.0],
            tips=["a", "b", "c"],
            x_ticks=[(1, "mid")],
            y_format=str,
            label="test chart",
            refs=[RefLine(12.0, "Strike")],
            band=Band(1, 3, "This market"),
        )
    )
    points = json.loads(re.search(r'data-points="([^"]+)"', html).group(1).replace("&#34;", '"'))
    assert [p[2] for p in points] == ["a", "b", "c"]
    assert all(0 <= p[0] <= 100 and 0 <= p[1] <= 100 for p in points)
    assert 'aria-label="test chart"' in html and "Strike" in html and "This market" in html
    assert 'class="band"' in html and 'class="line"' in html


def test_charts_escape_labels_and_handle_empty_input():
    assert "No data" in str(line_chart([], [], tips=[], x_ticks=[], y_format=str, label="x"))
    assert "No data" in str(histogram([], label="x", x_format=str, unit="market"))
    html = str(
        histogram(bin_values([1.0], 10), label="<b>", x_format=lambda v: f"{v:g}", unit="market")
    )
    assert "<b>" not in html and "1 market" in html


@pytest.mark.parametrize(
    ("fn", "value", "expected"),
    [
        (lambda v: usd(v), 1234.5, "$1,234.50"),
        (lambda v: usd(v, signed=True), -2.01, "\N{MINUS SIGN}$2.01"),
        (lambda v: usd(v, signed=True), 6.5, "+$6.50"),
        (lambda v: pct(v), 0.385, "38.5%"),
        (lambda v: cents(v), 0.75, "75\N{CENT SIGN}"),
        (lambda v: cents(v, True, 1), -0.052, "\N{MINUS SIGN}5.2\N{CENT SIGN}"),
        (lambda v: num(v, 2), 67890.019, "67,890.02"),
        (lambda v: clock(v), 10.17, "10:10"),
        (lambda v: usd(v), None, "\N{EM DASH}"),
        (lambda v: pct(v), float("nan"), "\N{EM DASH}"),
    ],
)
def test_formatters(fn, value, expected):
    assert fn(value) == expected


def test_age_and_tone():
    assert (age(None), age(4.2), age(130), age(7300)) == ("never", "4s", "2m", "2h")
    assert (tone(1.0), tone(-1.0), tone(0.0), tone(None)) == ("pos", "neg", "", "")
