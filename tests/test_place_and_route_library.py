"""``request.library`` (issue #4086): per-library inputs for a standard-cell
library the built-in tables do not cover. Script-text and validation tests
only; no OpenROAD."""

from __future__ import annotations

import pytest

from klayout_tools import place_and_route
from klayout_tools.place_and_route import PlaceAndRouteError
from klayout_tools.place_and_route_request import _validate_library
from test_place_and_route import (
    _base_request,
    _isolate_pdk,
    _make_pdk_install,
    _write,
    _write_request,
)

_PINS = [
    {"net_role": "power", "pattern": "VDD", "primary": True},
    {"net_role": "ground", "pattern": "VSS", "primary": True},
]
_RC = {"resistance_ohm_per_um": 0.0123, "capacitance_pf_per_um": 0.000045}


def _stage_lines(cell_library, stage, *, library=None, power=None):
    return place_and_route._stage_script_lines(
        stage=stage,
        checkpoint_in="/tmp/in.odb",
        checkpoint_out="/tmp/out.odb",
        tech_lef="t.lef",
        cell_lef="c.lef",
        liberty_path="c.lib",
        netlist_path="n.v",
        hdl_toplevel="top",
        floorplan={},
        io_spec={"layer_h": "M4", "layer_v": "M3"},
        macros=[],
        power=power,
        clock_port="clk",
        clock_period_ns=10.0,
        max_transition_ns=None,
        max_capacitance_pf=None,
        max_fanout=None,
        input_delay_ns=None,
        output_delay_ns=None,
        cell_library=cell_library,
        seed=1,
        output_dir="/tmp",
        route_critical_nets_percentage=0,
        max_antenna_repair_iterations=0,
        library=library,
    )


def test_cts_script_uses_supplied_buffer_and_rc():
    lib = _validate_library({"cts_buffer": "BUFx4_ASAP7", "wire_rc": _RC})
    text = "\n".join(_stage_lines("asap7_lib", "cts", library=lib))
    assert "BUFx4_ASAP7" in text
    assert "set_wire_rc -signal -resistance 0.0123 -capacitance 4.5e-05" in text
    assert "set_wire_rc -clock -resistance 0.0123 -capacitance 4.5e-05" in text
    assert "set_wire_rc -layer" not in text


def test_place_script_uses_supplied_rc_without_cts_buffer():
    lib = _validate_library({"wire_rc": _RC})
    text = "\n".join(_stage_lines("asap7_lib", "place", library=lib))
    assert "set_wire_rc -signal -resistance 0.0123" in text


def test_power_pins_emit_global_connections():
    lib = _validate_library({"power_pins": _PINS})
    power = place_and_route._validate_power(
        {
            "straps": [
                {
                    "layer": "met4",
                    "width_um": 1.6,
                    "pitch_um": 27.14,
                    "offset_um": 13.57,
                }
            ]
        },
        "sg13g2_stdcell",
    )
    text = "\n".join(
        place_and_route._power_delivery_lines(power, "sg13g2_stdcell", lib)
    )
    assert (
        "add_global_connection -net {VDD} -inst_pattern {.*} -pin_pattern {VDD} -power"
        in text
    )
    assert "-pin_pattern {VSS} -ground" in text


def test_no_library_keeps_built_in_scripts_identical():
    base = _stage_lines("sky130_fd_sc_hd", "cts")
    assert "set_wire_rc -layer M3" in base
    assert _stage_lines("sky130_fd_sc_hd", "cts", library=None) == base
    override = _validate_library({"cts_buffer": "my_buf"})
    assert "my_buf" in "\n".join(
        _stage_lines("sky130_fd_sc_hd", "cts", library=override)
    )


@pytest.mark.parametrize(
    "raw, match",
    [
        ("x", "must be a JSON object"),
        ({"bogus": 1}, "unknown key"),
        ({"cts_buffer": ""}, "cts_buffer"),
        ({"cts_buffer": 3}, "cts_buffer"),
        ({"power_pins": []}, "power_pins"),
        ({"power_pins": [{"net_role": "x", "pattern": "a"}]}, "net_role"),
        ({"power_pins": [{"net_role": "power", "pattern": 1}]}, "pattern"),
        (
            {"power_pins": [{"net_role": "power", "pattern": "a", "extra": 1}]},
            "unknown key",
        ),
        (
            {"power_pins": [_PINS[0], {**_PINS[0], "pattern": "VDD2"}, _PINS[1]]},
            "exactly one primary pin for net_role 'power'",
        ),
        ({"power_pins": [_PINS[0]]}, "net_role 'ground'"),
        (
            {"wire_rc": {"resistance_ohm_per_um": 1}},
            "capacitance_pf_per_um is required",
        ),
        ({"wire_rc": {**_RC, "resistance_ohm_per_um": 0}}, "resistance_ohm_per_um"),
        ({"wire_rc": {**_RC, "capacitance_pf_per_um": -1}}, "capacitance_pf_per_um"),
        ({"wire_rc": {**_RC, "capacitance_pf_per_um": True}}, "capacitance_pf_per_um"),
        ({"wire_rc": {**_RC, "x": 1}}, "unknown key"),
    ],
)
def test_malformed_library_rejected(raw, match):
    with pytest.raises(PlaceAndRouteError, match=match):
        _validate_library(raw)


def test_library_absent_is_none():
    assert _validate_library(None) is None


def _unknown_lib_request(tmp_path, monkeypatch, **overrides):
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "acmeA", cell_library="acme_fd_sc_hd")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    return _write_request(
        tmp_path / "request.json",
        _base_request(
            pdk={"cell_library": "acme_fd_sc_hd", "corner": "tt_025C_1v80"},
            **overrides,
        ),
    )


def test_preflight_reports_every_missing_input_with_field_names(tmp_path, monkeypatch):
    request_path = _unknown_lib_request(
        tmp_path,
        monkeypatch,
        power={
            "straps": [
                {
                    "layer": "met4",
                    "width_um": 1.6,
                    "pitch_um": 27.14,
                    "offset_um": 13.57,
                }
            ]
        },
    )
    with pytest.raises(PlaceAndRouteError) as excinfo:
        place_and_route.run_place_and_route(request_path)
    msg = str(excinfo.value)
    assert "missing" in msg
    for fragment in (
        "no clock-tree buffer cell known",
        "request.library.cts_buffer",
        "no routing-layer range known",
        "no antenna-diode cell known",
        "no power pin-pattern table known",
        "request.library.power_pins",
        "no tapcell master known",
        "no filler-cell masters known",
        "no request override yet",
    ):
        assert fragment in msg, fragment


def test_supplied_library_clears_cts_and_pin_checks(tmp_path, monkeypatch):
    request_path = _unknown_lib_request(
        tmp_path,
        monkeypatch,
        target_stage="cts",
        library={"cts_buffer": "BUF", "wire_rc": _RC, "power_pins": _PINS},
    )
    # Past the preflight, the run proceeds to the engine (absent here).
    try:
        place_and_route.run_place_and_route(request_path)
    except PlaceAndRouteError as exc:
        assert "clock-tree buffer" not in str(exc)
        assert "request.library" not in str(exc)
