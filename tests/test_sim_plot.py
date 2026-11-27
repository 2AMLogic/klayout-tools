"""Waveform SVG rendering and per-simulation annotation tests."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import Mock

import pytest

from klayout_tools import sim
from klayout_tools.sim_plot import (
    measurement_plot_path,
    prepare_annotations,
    render_waveform_svg,
    write_corner_plots,
)
from test_sim import _stub_subprocess_run_with_waveform, _write_body, _write_request

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


@pytest.mark.parametrize(
    "window", [None, (3e-6, 4e-6)], ids=["absent", "outside-sweep"]
)
def test_render_waveform_svg_omits_shading_without_window_overlap(window):
    svg = render_waveform_svg(
        signal_name="v(out)",
        corner_id="tt",
        x_values=[0.0, 1e-6, 2e-6],
        y_values=[0.0, 1.0, 1.0],
        x_axis_label="time",
        window=window,
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


def test_prepare_annotations_skips_unrecognized_signals_and_invalid_windows():
    signals, hints = prepare_annotations(
        [
            {"name": "ratio", "spice": ".meas tran ratio PARAM='a/b'"},
            {
                "name": "avgout",
                "spice": ".meas tran avgout AVG v(out) FROM=start TO=2n",
            },
        ]
    )

    assert signals == {"ratio": None, "avgout": "v(out)"}
    assert hints == {"v(out)": {"window": None, "target": None}}


@pytest.mark.parametrize(
    "waveform",
    [
        pytest.param("{", id="malformed-json"),
        pytest.param('{"variables":[{"name":"time"}],"points":[[0]]}', id="sweep-only"),
        pytest.param(
            '{"variables":[{"name":"time"},{"name":"v(out)","index":1}],"points":[]}',
            id="no-samples",
        ),
    ],
)
def test_write_corner_plots_skips_unplottable_waveform(tmp_path, waveform):
    artifact = tmp_path / "waveform.json"
    artifact.write_text(waveform)
    plots = write_corner_plots(
        corner={"corner_id": "tt", "artifacts": {"waveform": str(artifact)}},
        plot_dir=str(tmp_path),
        analysis_kind="tran",
        hints_by_signal={},
    )

    assert plots == []
    assert list(tmp_path.glob("*.svg")) == []


@pytest.mark.parametrize(
    ("worst_case", "signal"),
    [
        pytest.param(None, "v(out)", id="no-worst-case"),
        pytest.param({"corner_id": "tt"}, None, id="no-signal"),
        pytest.param({"corner_id": "ss"}, "v(out)", id="signal-only-at-other-corner"),
    ],
)
def test_measurement_plot_path_requires_signal_at_worst_corner(worst_case, signal):
    plots = {
        "tt": [{"signal": "v(out)", "path": "tt_out.svg"}],
        "ss": [{"signal": "v(in)", "path": "ss_in.svg"}],
    }

    assert measurement_plot_path({"worst_case": worst_case}, signal, plots) is None


@pytest.mark.usefixtures("bare_name_ngspice_resolution")
def test_plot_annotations_are_parsed_once_for_all_corners(tmp_path, monkeypatch):
    cards = [
        {"name": "avgout", "spice": ".meas tran avgout AVG v(out) FROM=1n TO=2n"},
        {"name": "cross", "spice": ".meas tran cross WHEN v(out)=0.5 RISE=1"},
    ]
    derived = {
        "name": "spread",
        "spice": None,
        "derive": {
            "from": "avgout",
            "axis": "params.vdd",
            "reduce": "range",
            "group_by": [],
        },
    }
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "corners": {"params": {"vdd": [0.8, 1.0, 1.2]}},
            "measurements": [*cards, derived],
        },
    )
    _stub_subprocess_run_with_waveform(
        monkeypatch,
        points=[(0.0, 0.0), (3e-9, 1.0)],
        log_text="avgout = 0.5\ncross = 1.5e-9\n",
    )
    parse = Mock(wraps=sim._measurement_diagnostic_hints)
    monkeypatch.setattr(
        f"{sim._measurement_diagnostic_hints.__module__}._measurement_diagnostic_hints",
        parse,
    )

    sim.run_sim(str(request))
    parse.assert_not_called()
    report = sim.run_sim(str(request), plot_dir=str(tmp_path / "plots"))

    assert [call.args[0] for call in parse.call_args_list] == [
        card["spice"] for card in cards
    ]
    assert report["status"] == "pass"
    spread = next(
        entry for entry in report["measurements"] if entry["name"] == "spread"
    )
    assert spread["plot"] is None
    assert len(report["plots"]) == 3
    for plot in report["plots"]:
        svg = Path(plot["path"]).read_text()
        assert 'fill="#fde68a"' in svg and "target=0.5" in svg
