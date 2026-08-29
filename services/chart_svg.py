"""
Minimal, dependency-free inline-SVG chart rendering for the Senior
Management Report (services/management_report.py + backend/api/
reports.py). Deliberately NOT Chart.js/canvas: the same rendered markup
has to work unmodified inside a static PDF export
(services/report_pdf.py, via Playwright's page.set_content() + page.pdf())
where there is no guarantee a client-side JS chart library finishes
drawing to a <canvas> before the PDF snapshot is taken, and no CDN
access should be required to produce a report at all. Plain SVG built
server-side sidesteps that risk entirely and renders identically in the
on-screen preview and the PDF.

Single-hue, single-series only (this report has exactly two: sales over
time, sales by venue) -- per the data-viz method, a 1-series chart needs
no legend and no categorical palette, just one sequential-safe accent
color. Deliberately fixed-light (a white card), not theme-aware: this
markup is shared verbatim with the PDF export, which has no dark-mode
concept at all, and a fixed light "card" floating on either a light or
dark on-screen page is a normal, acceptable pattern for an embedded
report visual (matching the pattern a photo or logo already follows).

Mark specs followed (see the dataviz skill's marks-and-anatomy
reference): 2px round-cap/join line, >=8px end marker, <=24px-thick
bars with a 4px rounded cap, hairline recessive gridlines, axis/label
text in muted ink (never the series color), y-axis ticks rounded to
clean numbers, direct value labels at the line's endpoint and on every
bar's cap (a small, fixed number of venues -- not the "never a number
on every point" density this rule guards against).
"""
from __future__ import annotations

import html
import math

_ACCENT = "#2a78d6"  # dataviz skill's validated default sequential hue (blue)
_GRIDLINE = "#e1e0d9"
_MUTED_TEXT = "#898781"
_AXIS = "#c3c2b7"
_SURFACE = "#ffffff"


def _nice_max(value: float) -> float:
    """Rounds up to a "clean" axis maximum (1/2/5 x a power of ten) --
    never a jagged number like 4,371 on a y-axis tick."""
    if value <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 5, 10):
        candidate = step * magnitude
        if candidate >= value:
            return candidate
    return 10 * magnitude


def _format_value(value: float) -> str:
    return f"{value:,.0f}"


def _label_stride(count: int, max_labels: int = 10) -> int:
    """Shows every Nth x-axis label when there are too many to fit
    without collision, rather than shrinking/rotating text past
    legibility -- "measure first," not clip."""
    if count <= max_labels:
        return 1
    return math.ceil(count / max_labels)


def render_line_chart(labels: list[str], values: list[float], *, title: str) -> str:
    """A single-series trend-over-time line. Returns "" (caller renders
    a plain "no data" message instead) when there's nothing to plot."""
    if not labels or not values or not any(values):
        return ""

    width, height = 680, 260
    pad_left, pad_right, pad_top, pad_bottom = 56, 24, 28, 40
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    y_max = _nice_max(max(values))
    n = len(values)
    step_x = plot_w / (n - 1) if n > 1 else 0

    def x_at(i: int) -> float:
        if n == 1:
            # A lone point at the left edge would sit directly under the
            # y-axis tick labels; center it in the plot instead.
            return pad_left + plot_w / 2
        return pad_left + i * step_x

    def y_at(v: float) -> float:
        return pad_top + plot_h - (v / y_max) * plot_h

    points = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, v in enumerate(values))

    gridlines = []
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        y = pad_top + plot_h * (1 - frac)
        tick_value = y_max * frac
        gridlines.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}" '
            f'stroke="{_GRIDLINE}" stroke-width="1"/>'
            f'<text x="{pad_left - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="11" fill="{_MUTED_TEXT}">'
            f'{_format_value(tick_value)}</text>'
        )

    # Always show the last label (the most recent data point) plus every
    # `stride`-th one before it -- replacing (never adding to) the final
    # stride pick when it would land within one stride of the real
    # endpoint, so the two can never render close enough to collide.
    stride = _label_stride(n)
    shown_indices = list(range(0, n, stride))
    if shown_indices[-1] != n - 1:
        if n - 1 - shown_indices[-1] < stride:
            shown_indices[-1] = n - 1
        else:
            shown_indices.append(n - 1)

    x_labels = []
    for i in shown_indices:
        # The endpoint label sits at the very right edge of the plot --
        # centering it would overflow the chart's right boundary
        # ("measure first," never clip), so anchor it to grow leftward
        # instead. Every other label has enough padding on both sides
        # to stay centered safely.
        anchor = "end" if i == n - 1 else "middle"
        x_labels.append(
            f'<text x="{x_at(i):.1f}" y="{height - pad_bottom + 16}" text-anchor="{anchor}" '
            f'font-size="11" fill="{_MUTED_TEXT}">{html.escape(labels[i])}</text>'
        )

    last_x, last_y = x_at(n - 1), y_at(values[-1])

    svg = f"""
<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="{html.escape(title)}"
     style="background:{_SURFACE};border:1px solid {_GRIDLINE};border-radius:8px;">
  <text x="{pad_left}" y="16" font-size="13" font-weight="600" fill="#1a1d23">{html.escape(title)}</text>
  {''.join(gridlines)}
  <line x1="{pad_left}" y1="{pad_top + plot_h}" x2="{width - pad_right}" y2="{pad_top + plot_h}" stroke="{_AXIS}" stroke-width="1"/>
  <polyline points="{points}" fill="none" stroke="{_ACCENT}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
  <circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="4" fill="{_ACCENT}" stroke="{_SURFACE}" stroke-width="2"/>
  <text x="{last_x:.1f}" y="{last_y - 10:.1f}" text-anchor="end" font-size="11" font-weight="600" fill="#1a1d23">{_format_value(values[-1])}</text>
  {''.join(x_labels)}
</svg>
""".strip()
    return svg


def render_bar_chart(labels: list[str], values: list[float], *, title: str) -> str:
    """A single-series magnitude comparison across categories (venues).
    Vertical columns, capped at 24px thick per the mark spec, value
    labeled on every cap (a small fixed number of venues, not the dense
    case the "never a number on every point" rule guards against)."""
    if not labels or not values:
        return ""

    width = max(680, 90 * len(labels) + 80)
    height = 260
    pad_left, pad_right, pad_top, pad_bottom = 56, 24, 28, 40
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    y_max = _nice_max(max(values)) if any(values) else 1.0
    n = len(labels)
    band_w = plot_w / n
    bar_w = min(24, band_w * 0.6)

    gridlines = []
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        y = pad_top + plot_h * (1 - frac)
        tick_value = y_max * frac
        gridlines.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}" '
            f'stroke="{_GRIDLINE}" stroke-width="1"/>'
            f'<text x="{pad_left - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="11" fill="{_MUTED_TEXT}">'
            f'{_format_value(tick_value)}</text>'
        )

    bars = []
    for i, (label, value) in enumerate(zip(labels, values)):
        band_x = pad_left + i * band_w
        bar_x = band_x + (band_w - bar_w) / 2
        bar_h = (value / y_max) * plot_h if y_max else 0
        bar_y = pad_top + plot_h - bar_h
        radius = min(4, bar_h)
        bars.append(
            f'<path d="M{bar_x:.1f},{bar_y + radius:.1f} '
            f'a{radius:.1f},{radius:.1f} 0 0 1 {radius:.1f},-{radius:.1f} '
            f'h{max(bar_w - 2 * radius, 0):.1f} '
            f'a{radius:.1f},{radius:.1f} 0 0 1 {radius:.1f},{radius:.1f} '
            f'v{max(bar_h - radius, 0):.1f} h-{bar_w:.1f} z" fill="{_ACCENT}"/>'
            f'<text x="{band_x + band_w / 2:.1f}" y="{bar_y - 6:.1f}" text-anchor="middle" '
            f'font-size="11" font-weight="600" fill="#1a1d23">{_format_value(value)}</text>'
            f'<text x="{band_x + band_w / 2:.1f}" y="{height - pad_bottom + 16}" text-anchor="middle" '
            f'font-size="11" fill="{_MUTED_TEXT}">{html.escape(label)}</text>'
        )

    svg = f"""
<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="{html.escape(title)}"
     style="background:{_SURFACE};border:1px solid {_GRIDLINE};border-radius:8px;">
  <text x="{pad_left}" y="16" font-size="13" font-weight="600" fill="#1a1d23">{html.escape(title)}</text>
  {''.join(gridlines)}
  <line x1="{pad_left}" y1="{pad_top + plot_h}" x2="{width - pad_right}" y2="{pad_top + plot_h}" stroke="{_AXIS}" stroke-width="1"/>
  {''.join(bars)}
</svg>
""".strip()
    return svg
