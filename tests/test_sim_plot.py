"""Unit tests for :mod:`klayout_tools.sim_plot` -- the dependency-free
waveform-SVG renderer backing ``klt sim --plot`` (issue #1723).

These are pure-function tests over :func:`render_waveform_svg` only; the
higher-level "which signal gets which window/target, what filenames get
written" wiring lives in ``tests/test_sim.py`` (the caller,
``klayout_tools.sim._write_corner_plots``).
"""

from __future__ import annotations

import re

import pytest

from klayout_tools.sim_plot import render_waveform_svg

_SVG_ROOT_RE = re.compile(r"^<svg\b")


def test_render_waveform_svg_produces_well_formed_root():
    svg = render_waveform_svg(
        signal_name="v(out)",
        corner_id="tt/1.800V/27C",
        x_values=[0.0, 1e-9, 2e-9],
        y_values=[0.0, 0.5, 1.0],
        x_axis_label="time",
    )
    assert _SVG_ROOT_RE.match(svg)
    assert svg.rstrip().endswith("</svg>")
    assert "v(out)" in svg
    assert "tt/1.800V/27C" in svg


def test_render_waveform_svg_rejects_empty_series():
    with pytest.raises(ValueError):
        render_waveform_svg(
            signal_name="v(out)",
            corner_id="tt",
            x_values=[],
            y_values=[],
            x_axis_label="time",
        )


def test_render_waveform_svg_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        render_waveform_svg(
            signal_name="v(out)",
            corner_id="tt",
            x_values=[0.0, 1.0],
            y_values=[0.0],
            x_axis_label="time",
        )


def test_render_waveform_svg_flat_signal_still_renders_a_visible_line():
    """A non-starting oscillator's output is a perfectly flat line -- the
    exact case #1723's acceptance criterion names. `y_lo == y_hi` must not
    collapse the plot to a degenerate zero-height range."""
    svg = render_waveform_svg(
        signal_name="v(out)",
        corner_id="tt",
        x_values=[0.0, 1e-6, 2e-6],
        y_values=[1.0, 1.0, 1.0],
        x_axis_label="time",
    )
    # The y-axis min/max labels must differ even though every sample is 1.0 --
    # otherwise the padding logic did nothing and the plot has zero height.
    # (10% padding around a flat value of 1.0 -> axis bounds [0, 2].)
    assert ">2<" in svg
    assert ">0<" in svg


def test_render_waveform_svg_log_x_axis_labelled_for_ac():
    svg = render_waveform_svg(
        signal_name="vdb(out)",
        corner_id="tt",
        x_values=[1.0, 10.0, 100.0, 1000.0],
        y_values=[0.0, -3.0, -20.0, -40.0],
        x_axis_label="frequency",
        log_x=True,
    )
    assert "(log)" in svg


def test_render_waveform_svg_log_x_falls_back_to_linear_on_nonpositive_x():
    """A `dc`/`tran` sweep can legitimately start at 0 or go negative; log_x
    must degrade gracefully rather than raising `ValueError: math domain
    error` from `math.log10`."""
    svg = render_waveform_svg(
        signal_name="v(out)",
        corner_id="tt",
        x_values=[0.0, 1.0, 2.0],
        y_values=[0.0, 1.0, 2.0],
        x_axis_label="time",
        log_x=True,
    )
    assert "(log)" not in svg


def test_render_waveform_svg_window_shades_a_rect():
    svg = render_waveform_svg(
        signal_name="v(out)",
        corner_id="tt",
        x_values=[0.0, 1e-6, 2e-6, 3e-6],
        y_values=[0.0, 1.0, 1.0, 1.0],
        x_axis_label="time",
        window=(1e-6, 2e-6),
    )
    assert 'fill="#fde68a"' in svg


def test_render_waveform_svg_no_window_omits_shading():
    svg = render_waveform_svg(
        signal_name="v(out)",
        corner_id="tt",
        x_values=[0.0, 1e-6, 2e-6],
        y_values=[0.0, 1.0, 1.0],
        x_axis_label="time",
    )
    assert "#fde68a" not in svg


def test_render_waveform_svg_target_draws_reference_line_and_label():
    svg = render_waveform_svg(
        signal_name="v(out)",
        corner_id="tt",
        x_values=[0.0, 1e-6, 2e-6],
        y_values=[0.0, 0.5, 1.0],
        x_axis_label="time",
        target=1.5,
    )
    assert "target=1.5" in svg
    assert "stroke-dasharray" in svg


def test_render_waveform_svg_no_target_omits_reference_line():
    svg = render_waveform_svg(
        signal_name="v(out)",
        corner_id="tt",
        x_values=[0.0, 1e-6, 2e-6],
        y_values=[0.0, 0.5, 1.0],
        x_axis_label="time",
    )
    assert "stroke-dasharray" not in svg


def test_render_waveform_svg_escapes_signal_and_corner_id():
    svg = render_waveform_svg(
        signal_name="v(<out>)",
        corner_id='tt & "1.8V"',
        x_values=[0.0, 1.0],
        y_values=[0.0, 1.0],
        x_axis_label="time",
    )
    assert "<out>" not in svg
    assert "&lt;out&gt;" in svg
    assert "&amp;" in svg
