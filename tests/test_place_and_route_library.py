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
        floorplan={"method": "def", "def_path": "/tmp/f.def"},
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
        max_antenna_repair_iterations=2,
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
        ({"route_layers": {"low": "M2"}}, "route_layers.high is required"),
        ({"route_layers": {"low": "M2", "high": ""}}, "route_layers.high"),
        ({"route_layers": {"low": "M2", "high": "M7", "x": 1}}, "unknown key"),
        ({"route_layers": "M2-M7"}, "route_layers must be a JSON object"),
        ({"antenna_diode": {"cell": "D"}}, "antenna_diode.pin is required"),
        ({"antenna_diode": {"cell": 1, "pin": "I"}}, "antenna_diode.cell"),
        ({"tapcell": {"endcap": "E", "distance_um": 5}}, "tapcell.master is required"),
        ({"tapcell": {"master": "T"}}, "tapcell.distance_um is required"),
        ({"tapcell": {"master": "T", "distance_um": 0}}, "tapcell.distance_um"),
        (
            {"tapcell": {"master": "T", "distance_um": 5, "endcap": ""}},
            "tapcell.endcap",
        ),
        ({"tapcell": {"master": "T", "distance_um": 5, "x": 1}}, "unknown key"),
        ({"tapcell": []}, "tapcell must be a JSON object"),
        ({"filler": []}, "filler must be a non-empty list"),
        ({"filler": ["F", ""]}, r"filler\[1\]"),
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
        "request.library.route_layers",
        "request.library.antenna_diode",
        "request.library.tapcell",
        "request.library.filler",
    ):
        assert fragment in msg, fragment
    assert "no request override yet" not in msg


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


_ASAP7 = {
    "cts_buffer": "BUFx4",
    "wire_rc": _RC,
    "power_pins": _PINS,
    "route_layers": {"low": "M2", "high": "M7"},
    "antenna_diode": {"cell": "ANTENNA", "pin": "I"},
    "tapcell": {"master": "TAPCELL", "endcap": "ENDCAP", "distance_um": 25},
    "filler": ["FILL1", "FILL2"],
}
_STRAP = {"layer": "M5", "width_um": 1.0, "pitch_um": 20.0, "offset_um": 5.0}


def test_validated_new_keys():
    lib = _validate_library(_ASAP7)
    assert lib["route_layers"] == "M2-M7"
    assert lib["antenna_diode"] == ("ANTENNA", "I")
    assert lib["tapcell"] == ("TAPCELL", "ENDCAP", 25)
    assert lib["filler"] == ("FILL1", "FILL2")
    assert _validate_library({"tapcell": None})["tapcell"] == (None, None, None)


def test_route_script_uses_supplied_layers_diode_tap_filler():
    lib = _validate_library(_ASAP7)
    power = place_and_route._validate_power(
        {"straps": [_STRAP]}, "asap7sc7p5t_28_RVT", lib
    )
    text = "\n".join(
        _stage_lines("asap7sc7p5t_28_RVT", "floorplan", library=lib, power=power)
        + _stage_lines("asap7sc7p5t_28_RVT", "route", library=lib, power=power)
    )
    assert "tapcell -distance 25 -tapcell_master TAPCELL -endcap_master ENDCAP" in text
    assert "M2-M7" in text
    assert "ANTENNA" in text
    assert "filler_placement {FILL1 FILL2}" in text
    assert "-remove_cells {TAPCELL ENDCAP FILL1 FILL2}" in text


def test_supplied_key_overrides_built_in_entry():
    base = "\n".join(_stage_lines("sky130_fd_sc_hd", "route"))
    lib = _validate_library(
        {
            "route_layers": {"low": "met2", "high": "met4"},
            "antenna_diode": {"cell": "my_diode", "pin": "DIODE"},
        }
    )
    text = "\n".join(_stage_lines("sky130_fd_sc_hd", "route", library=lib))
    assert "met2-met4" in text and "my_diode" in text
    assert "my_diode" not in base
    assert place_and_route._tapcell_masters(
        "sky130_fd_sc_hd", _validate_library({"tapcell": None})
    ) == (None, None, None)
    assert place_and_route._filler_masters(
        "sky130_fd_sc_hd", _validate_library({"filler": ["X"]})
    ) == ("X",)


def test_explicit_no_tapcell_is_distinct_from_unverified():
    lib = _validate_library({"tapcell": None})
    assert place_and_route._tapcell_lines("acme", None, lib) == []
    with pytest.raises(KeyError):
        place_and_route._tapcell_lines("acme", None, None)
    with pytest.raises(PlaceAndRouteError, match="cannot be honored"):
        place_and_route._validate_power(
            {"straps": [_STRAP], "tapcell": {"distance_um": 10}}, "acme", lib
        )


def test_missing_tables_skipped_by_supplied_keys():
    lib = _validate_library(_ASAP7)
    power = place_and_route._validate_power({"straps": [_STRAP]}, "acme", lib)
    place_and_route._validate_stage_prerequisites(
        stage_index=place_and_route.STAGE_ORDER.index("route"),
        target_stage="route",
        cell_library="acme",
        clock_port="clk",
        io_spec={"layer_h": "M4", "layer_v": "M3"},
        power=power,
        library=lib,
    )
    partial = _validate_library({k: v for k, v in _ASAP7.items() if k != "filler"})
    with pytest.raises(PlaceAndRouteError, match=r"request\.library\.filler"):
        place_and_route._validate_stage_prerequisites(
            stage_index=place_and_route.STAGE_ORDER.index("route"),
            target_stage="route",
            cell_library="acme",
            clock_port="clk",
            io_spec={"layer_h": "M4", "layer_v": "M3"},
            power=power,
            library=partial,
        )
