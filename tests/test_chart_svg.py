"""Unit tests for services/chart_svg.py -- plain-SVG chart rendering
(no Chart.js/canvas, see the module docstring for why). Assertions
check structural correctness (valid SVG, right number of marks, no
known label-collision regressions) rather than pixel output."""
from __future__ import annotations

import re

from services.chart_svg import _label_stride, _nice_max, render_bar_chart, render_line_chart


# --- _nice_max ---

def test_nice_max_rounds_up_to_clean_number():
    assert _nice_max(143) == 200
    assert _nice_max(4371) == 5000
    assert _nice_max(10148) == 20000


def test_nice_max_zero_or_negative_returns_one():
    assert _nice_max(0) == 1.0
    assert _nice_max(-5) == 1.0


# --- _label_stride ---

def test_label_stride_no_thinning_needed():
    assert _label_stride(8) == 1


def test_label_stride_thins_long_series():
    assert _label_stride(31, max_labels=10) == 4  # ceil(31/10)


# --- render_line_chart ---

def test_render_line_chart_empty_data_returns_empty_string():
    assert render_line_chart([], [], title="Empty") == ""
    assert render_line_chart(["2026-08-01"], [0], title="All zero") == ""


def test_render_line_chart_produces_valid_svg_with_one_point_per_value():
    svg = render_line_chart(["2026-08-01", "2026-08-02", "2026-08-03"], [10, 20, 30], title="Sales")
    assert svg.startswith("<svg")
    assert svg.endswith("</svg>")
    # 3 data points -> the polyline has exactly 3 "x,y" pairs.
    points_match = re.search(r'<polyline points="([^"]+)"', svg)
    assert points_match is not None
    assert len(points_match.group(1).split()) == 3


def test_render_line_chart_single_point_does_not_collide_with_axis_label():
    """Regression: a lone point used to render at x=pad_left, directly
    under the y-axis tick labels -- see chart_svg.py's x_at()."""
    svg = render_line_chart(["2026-08-01"], [143], title="Single Point")
    circle_match = re.search(r'<circle cx="([\d.]+)"', svg)
    assert circle_match is not None
    cx = float(circle_match.group(1))
    assert cx > 100  # clear of the y-axis label gutter (pad_left=56)


def test_render_line_chart_many_points_thins_labels_without_collision():
    """Regression: the forced last-label used to be added ALONGSIDE a
    too-close stride label rather than replacing it, so two adjacent
    dense date labels rendered on top of each other."""
    labels = [f"2026-08-{d:02d}" for d in range(1, 13)]  # 12 days
    svg = render_line_chart(labels, list(range(100, 100 + 12)), title="Dense")
    text_labels = re.findall(r'<text x="[\d.]+" y="[\d.]+" text-anchor="(?:middle|end)"[^>]*>([^<]+)</text>', svg)
    x_axis_labels = [t for t in text_labels if t.startswith("2026-")]
    # The last two source labels must never BOTH appear (that's the
    # exact collision this regression guards against).
    assert not ({"2026-08-11", "2026-08-12"} <= set(x_axis_labels))
    assert "2026-08-12" in x_axis_labels  # the real endpoint is always shown


def test_render_line_chart_endpoint_label_is_end_anchored():
    """The endpoint sits at the plot's right edge -- centering it would
    overflow past the chart boundary, so it must anchor "end" (grows
    leftward) instead of "middle"."""
    svg = render_line_chart(["2026-08-01", "2026-08-02"], [10, 20], title="Two")
    match = re.search(r'text-anchor="(\w+)"[^>]*>2026-08-02<', svg)
    assert match is not None
    assert match.group(1) == "end"


# --- render_bar_chart ---

def test_render_bar_chart_empty_data_returns_empty_string():
    assert render_bar_chart([], [], title="Empty") == ""


def test_render_bar_chart_one_bar_per_venue():
    svg = render_bar_chart(["Venue A", "Venue B", "Venue C"], [100, 50, 10], title="Sales by Venue")
    assert svg.startswith("<svg")
    assert svg.count('fill="#2a78d6"') == 3  # one bar path per venue
    assert "Venue A" in svg and "Venue B" in svg and "Venue C" in svg


def test_render_bar_chart_labels_every_bar_value():
    svg = render_bar_chart(["Venue A", "Venue B"], [10148, 5423], title="Sales by Venue")
    assert "10,148" in svg
    assert "5,423" in svg


def test_render_bar_chart_all_zero_values_does_not_crash():
    svg = render_bar_chart(["Venue A", "Venue B"], [0, 0], title="Sales by Venue")
    assert svg.startswith("<svg")
