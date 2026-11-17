"""Unconstrained place/route (issue #5672): a missing ``clock_period_ns`` is
an unconstrained run, not an error -- every timing-driven pass is skipped so
a timing-free (structural) Liberty can reach ``place``/``route``. Script-text
and validation tests only; no OpenROAD."""

from __future__ import annotations

import pytest

from klayout_tools import place_and_route
from klayout_tools.place_and_route import PlaceAndRouteError, run_place_and_route
from klayout_tools.place_and_route_request import _validate_constraints
from klayout_tools.place_and_route_script import _clock_lines
from klayout_tools.place_and_route_unconstrained import unconstrained_warnings
from test_place_and_route import (
    _REAL_SKY130_PNR_VARIANT,
    HAVE_OPENROAD,
    _base_request,
    _setup_success_env,
    _stub_merge_def_to_gds,
    _stub_openroad_success,
    _synth_netlist_path,
    _write_request,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _stage_lines(stage, *, clock_port=None, clock_period_ns=None):
    return place_and_route._stage_script_lines(
        stage=stage,
        checkpoint_in="/tmp/in.odb",
        checkpoint_out="/tmp/out.odb",
        tech_lef="t.lef",
        cell_lef="c.lef",
        liberty_path="c.lib",
        netlist_path="n.v",
        hdl_toplevel="top",
        floorplan={"method": "def", "def_path": "/tmp/f.def"},
        io_spec={"layer_h": "met4", "layer_v": "met3"},
        macros=[],
        power=None,
        clock_port=clock_port,
        clock_period_ns=clock_period_ns,
        max_transition_ns=None,
        max_capacitance_pf=None,
        max_fanout=None,
        input_delay_ns=None,
        output_delay_ns=None,
        cell_library="sky130_fd_sc_hd",
        seed=1,
        output_dir="/tmp",
        route_critical_nets_percentage=0,
        max_antenna_repair_iterations=1,
    )


def test_place_stage_unconstrained_skips_resizer_passes():
    text = "\n".join(_stage_lines("place"))
    assert "create_clock" not in text
    assert "repair_design" not in text
    assert "repair_timing" not in text
    assert "global_placement" in text
    assert "detailed_placement" in text
    assert "estimate_parasitics -placement" in text
    assert "set_wire_rc" in text


def test_place_stage_constrained_keeps_resizer_passes():
    lines = _stage_lines("place", clock_period_ns=10.0)
    assert lines.index("repair_design") < lines.index("repair_timing")
    assert lines.index("repair_timing") < lines.index("detailed_placement")


def test_cts_stage_unconstrained_is_a_noop_that_still_writes_def():
    text = "\n".join(_stage_lines("cts"))
    assert "clock_tree_synthesis -root_buf" not in text
    assert "repair_timing -hold" not in text.replace(
        "clock_tree_synthesis and repair_timing -hold", ""
    )
    assert "skipping clock_tree_synthesis" in text
    assert "write_def /tmp/top.cts.def" in text


@pytest.mark.parametrize("kwargs", [{"clock_period_ns": 10.0}, {"clock_port": "clk"}])
def test_cts_stage_with_a_clock_is_unchanged(kwargs):
    text = "\n".join(_stage_lines("cts", **kwargs))
    assert "clock_tree_synthesis -root_buf" in text
    assert "repair_timing -hold" in text
    assert "write_def /tmp/top.cts.def" in text


def test_route_stage_has_no_clock_dependent_difference():
    unconstrained = _stage_lines("route")
    assert "clock_tree_synthesis" not in "\n".join(unconstrained)
    assert "detailed_route" in "\n".join(unconstrained)
    # The constrained route stage differs only by its clock lines.
    constrained = _stage_lines("route", clock_period_ns=10.0)
    clock_lines = _clock_lines(None, 10.0)
    assert [ln for ln in constrained if ln not in clock_lines] == unconstrained


@pytest.mark.parametrize("stage", ["place", "cts", "route"])
def test_validation_no_longer_requires_a_clock(stage):
    place_and_route._validate_stage_prerequisites(
        stage_index=place_and_route.STAGE_ORDER.index(stage),
        target_stage=stage,
        cell_library="sky130_fd_sc_hd",
        clock_port=None,
        io_spec={"layer_h": "met4", "layer_v": "met3"},
        power=None,
        clock_period_ns=None,
    )


def test_validation_still_requires_io_at_place():
    with pytest.raises(PlaceAndRouteError, match="request.io"):
        place_and_route._validate_stage_prerequisites(
            stage_index=place_and_route.STAGE_ORDER.index("place"),
            target_stage="place",
            cell_library="sky130_fd_sc_hd",
            clock_port=None,
            io_spec=None,
            power=None,
        )


@pytest.mark.parametrize(
    "constraints, match",
    [
        ({"clock_port": "clk"}, "clock_period_ns"),
        ({"clock_period_ns": 0}, "clock_period_ns must be"),
        ({"clock_period_ns": -1.0}, "clock_period_ns must be"),
        ({"input_delay_ns": 0.5}, "require"),
        ({"output_delay_ns": 0.5}, "require"),
    ],
)
def test_clock_constraint_errors_stay_errors(constraints, match):
    with pytest.raises(PlaceAndRouteError, match=match):
        _validate_constraints(constraints)


def test_unconstrained_warning_names_every_skipped_pass():
    warn = unconstrained_warnings
    route = " ".join(warn("route", None, None))
    for token in (
        "repair_design",
        "repair_timing",
        "clock_tree_synthesis",
        "repair_timing -hold",
    ):
        assert token in route
    place = " ".join(warn("place", None, None))
    assert "repair_design" in place and "clock_tree_synthesis" not in place
    assert warn("floorplan", None, None) == []
    assert warn("route", None, 10.0) == []
    assert warn("route", "clk", None) == []


@pytest.mark.parametrize("constraints", [None, {}])
@pytest.mark.parametrize("target_stage", ["place", "cts", "route"])
def test_run_without_clock_reaches_target_with_warning(
    tmp_path, monkeypatch, target_stage, constraints
):
    request_path = _setup_success_env(
        tmp_path, monkeypatch, target_stage=target_stage, constraints=constraints
    )
    _stub_openroad_success(
        monkeypatch,
        stages=place_and_route.STAGE_ORDER[
            : place_and_route.STAGE_ORDER.index(target_stage) + 1
        ],
    )
    _stub_merge_def_to_gds(monkeypatch)
    report = run_place_and_route(request_path)
    assert report["status"] == "ok"
    assert report["stage_reached"] == target_stage
    assert any("unconstrained run" in w for w in report["warnings"]), report


def test_run_floorplan_without_clock_has_no_warning(tmp_path, monkeypatch):
    request_path = _setup_success_env(
        tmp_path, monkeypatch, target_stage="floorplan", constraints=None
    )
    _stub_openroad_success(monkeypatch, stages=("floorplan",))
    report = run_place_and_route(request_path)
    assert not any("unconstrained run" in w for w in report["warnings"])


def test_run_with_period_has_no_unconstrained_warning(tmp_path, monkeypatch):
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        target_stage="route",
        constraints={"clock_period_ns": 10.0},
    )
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)
    report = run_place_and_route(request_path)
    assert not any("unconstrained run" in w for w in report["warnings"])


@pytest.mark.skipif(
    not HAVE_OPENROAD, reason="openroad is not installed on this machine"
)
@pytest.mark.skipif(
    _REAL_SKY130_PNR_VARIANT is None,
    reason="no real sky130_fd_sc_hd LEF/liberty/GDS set resolves via list_pdks()",
)
def test_integration_real_openroad_no_clock_reaches_route(tmp_path, monkeypatch):
    """Issue #5672: with no `constraints` at all a combinational adder reaches
    `route` under real OpenROAD, reports `timing_status: "unconstrained"` and
    warns. Uses sky130's *timed* Liberty -- it does not exercise a timing-free
    Liberty (no CTS-0111 repro); that is tracked as live verification."""
    root, variant = _REAL_SKY130_PNR_VARIANT
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", variant)

    from klayout_tools.synthesize import run_synthesize

    (tmp_path / "add4.v").write_text(
        "module add4(input [3:0] a, input [3:0] b, output [4:0] s);\n"
        "  assign s = a + b;\nendmodule\n",
        encoding="utf-8",
    )
    synth_request = _write_request(
        tmp_path / "synth_request.json",
        {
            "engine": "yosys",
            "sources": ["add4.v"],
            "hdl_toplevel": "add4",
            "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
        },
    )
    synth_report = run_synthesize(synth_request)
    request_path = _write_request(
        tmp_path / "pnr_request.json",
        _base_request(
            netlist=_synth_netlist_path(synth_request, "add4", synth_report["run_id"]),
            hdl_toplevel="add4",
            constraints=None,
        ),
    )

    report = run_place_and_route(request_path)

    assert report["status"] == "ok"
    assert report["stage_reached"] == "route"
    route = next(s for s in report["stages"] if s["name"] == "route")
    assert route["timing_status"] == "unconstrained", route
    assert any("unconstrained run" in w for w in report["warnings"]), report
