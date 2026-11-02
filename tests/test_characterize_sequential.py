"""Clock-to-output arcs for sequential cells (issue #4755)."""

import json
import shutil
from pathlib import Path

import pytest

from klayout_tools import characterize

_SKIP_NO_NGSPICE = pytest.mark.skipif(
    shutil.which("ngspice") is None, reason="ngspice is not installed"
)


def _expect_error(tmp_path: Path, request: dict, match: str) -> None:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    with pytest.raises(characterize.CharacterizeError, match=match):
        characterize.run_characterize(str(path))


def _inverter_request(tmp_path: Path) -> dict:
    cells = tmp_path / "cells.spice"
    cells.write_text(
        ".subckt inv_demo Y A VDD VSS\n"
        "MN Y A VSS VSS nfet_demo w=1u l=0.15u\n"
        ".ends inv_demo\n"
    )
    return {
        "cell": {
            "name": "inv_demo",
            "netlist": str(cells),
            "pins": [
                {"name": "A", "direction": "input", "capacitance_pf": 0.002},
                {"name": "Y", "direction": "output", "function": "!A"},
            ],
            "power_pins": {"vdd": "VDD", "gnd": "VSS"},
        },
        "corner": {"name": "tt", "supply_v": 1.8, "temperature_c": 25},
        "grid": {"input_transition_ns": [0.02], "output_load_pf": [0.005]},
    }


def _dff_request(tmp_path: Path) -> dict:
    cells = tmp_path / "dff.spice"
    cells.write_text(
        ".subckt dff_demo Q QN CLK D VDD VSS\n"
        "MN Q D VSS VSS nfet_demo w=1u l=0.15u\n"
        ".ends dff_demo\n"
    )
    return {
        "cell": {
            "name": "dff_demo",
            "netlist": str(cells),
            "clock_pin": "CLK",
            "pins": [
                {"name": "CLK", "direction": "input", "capacitance_pf": 0.002},
                {"name": "D", "direction": "input", "capacitance_pf": 0.002},
                {"name": "Q", "direction": "output", "function": "D"},
                {"name": "QN", "direction": "output", "function": "!D"},
            ],
            "power_pins": {"vdd": "VDD", "gnd": "VSS"},
        },
        "corner": {"name": "tt", "supply_v": 1.8, "temperature_c": 25},
        "grid": {"input_transition_ns": [0.02, 0.08], "output_load_pf": [0.005, 0.03]},
    }


def test_clock_pin_derives_one_clock_to_output_arc_per_output(tmp_path):
    from klayout_tools import characterize as ch

    request = _dff_request(tmp_path)
    cell = ch._resolve_cell_spec(request["cell"], str(tmp_path), "request.cell")
    arcs = ch._resolve_arcs({}, cell)
    assert [(a.output_pin, a.related_pin, a.timing_type) for a in arcs] == [
        ("Q", "CLK", "rising_edge"),
        ("QN", "CLK", "rising_edge"),
    ]
    assert dict(arcs[0].side_inputs) == {"D": True}
    assert dict(arcs[0].fall_side_inputs) == {"D": False}
    assert dict(arcs[1].side_inputs) == {"D": False}  # QN is high when D is low


def test_clock_pin_falling_edge_timing_type(tmp_path):
    from klayout_tools import characterize as ch

    request = _dff_request(tmp_path)
    request["cell"]["clock_edge"] = "falling"
    cell = ch._resolve_cell_spec(request["cell"], str(tmp_path), "request.cell")
    assert {a.timing_type for a in ch._resolve_arcs({}, cell)} == {"falling_edge"}


def test_clock_pin_in_next_state_function_is_refused(tmp_path):
    request = _dff_request(tmp_path)
    request["cell"]["pins"][2]["function"] = "D*CLK"
    _expect_error(tmp_path, request, "not a declared input pin")


def test_clock_pin_must_be_a_declared_input(tmp_path):
    request = _dff_request(tmp_path)
    request["cell"]["clock_pin"] = "Q"
    _expect_error(tmp_path, request, "clock_pin must name a declared input pin")


def test_clock_edge_requires_clock_pin(tmp_path):
    request = _inverter_request(tmp_path)
    request["cell"]["clock_edge"] = "rising"
    _expect_error(tmp_path, request, "requires")


def test_sequential_testbench_has_priming_pulse_and_scoped_measures(tmp_path):
    from klayout_tools import characterize as ch
    from klayout_tools import liberty_writer as lw

    request = _dff_request(tmp_path)
    cell = ch._resolve_cell_spec(request["cell"], str(tmp_path), "request.cell")
    arcs = ch._resolve_arcs({}, cell)
    plan = ch._build_stimulus_plan(
        cell=cell,
        corner={"name": "tt", "supply_v": 1.8, "temperature_c": 25},
        grid={"input_transition_ns": (0.02,), "output_load_pf": (0.01,)},
        thresholds=lw.Thresholds(),
        options={"ramps_ns": (0.03,), "settle_ns": 2.0, "tran_step_ns": 0.01},
        arcs=arcs,
    )
    text = plan["netlist_text"]
    # data pin switches (own PWL source); clock has 3 pulses
    assert "Va0s0l0d_D" in text
    clk = next(ln for ln in text.splitlines() if ln.startswith("Va0s0l0 i_"))
    assert clk.count("1.8") == 6
    dr = next(m for m in plan["measurements"] if m["name"] == "a0s0l0_dr")
    assert "TD=" in dr["spice"] and "RISE=1" in dr["spice"]


@_SKIP_NO_NGSPICE
def test_dff_example_yields_clock_to_q_arcs_end_to_end(tmp_path):
    """The DFF worked example, through a real single-corner ngspice run."""
    example = Path(__file__).parent.parent / "examples" / "characterize"
    report = characterize.run_characterize(
        str(example / "request-dff.json"), outdir=str(tmp_path), backend="local"
    )
    cell = report["cells"][0] if "cells" in report else report
    (arc,) = cell["arcs"]
    assert (arc["output_pin"], arc["related_pin"]) == ("Q", "CLK")
    assert arc["timing_type"] == "rising_edge"
    for name in ("cell_rise", "cell_fall", "rise_transition", "fall_transition"):
        flat = [v for row in arc[name]["values"] for v in row]
        assert all(v > 0 for v in flat)
    lib_text = next(tmp_path.glob("*.lib")).read_text()
    assert "timing_type : rising_edge" in lib_text
