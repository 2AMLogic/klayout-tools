"""Clock-to-output arcs for sequential cells (issue #4755), and the
level-sensitive latch declaration layered on them (issue #5521)."""

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


def _latch_request(tmp_path: Path, level: str | None = None) -> dict:
    """A latch whose ``Q`` tracks ``D`` while the enable is active.

    The netlist is a stub -- these tests exercise spec resolution, arc
    derivation and the generated deck, none of which simulate. The real
    transparent latch is ``examples/characterize/dlat.spice``, run end to end
    by the ngspice test at the bottom of this file.
    """
    cells = tmp_path / "dlat.spice"
    cells.write_text(
        ".subckt dlat_demo Q E D VDD VSS\n"
        "MN Q D VSS VSS nfet_demo w=1u l=0.15u\n"
        ".ends dlat_demo\n"
    )
    cell: dict = {
        "name": "dlat_demo",
        "netlist": str(cells),
        "enable_pin": "E",
        "pins": [
            {"name": "E", "direction": "input", "capacitance_pf": 0.002},
            {"name": "D", "direction": "input", "capacitance_pf": 0.002},
            {"name": "Q", "direction": "output", "function": "D"},
        ],
        "power_pins": {"vdd": "VDD", "gnd": "VSS"},
    }
    if level is not None:
        cell["enable_level"] = level
    return {
        "cell": cell,
        "corner": {"name": "tt", "supply_v": 1.8, "temperature_c": 25},
        "grid": {"input_transition_ns": [0.02], "output_load_pf": [0.005]},
    }


def test_enable_pin_and_clock_pin_are_mutually_exclusive(tmp_path):
    request = _latch_request(tmp_path)
    request["cell"]["clock_pin"] = "E"
    _expect_error(tmp_path, request, "mutually exclusive")


def test_enable_pin_must_be_a_declared_input(tmp_path):
    request = _latch_request(tmp_path)
    request["cell"]["enable_pin"] = "Q"
    _expect_error(tmp_path, request, "enable_pin must name a declared input pin")
    request["cell"]["enable_pin"] = "NOPE"
    _expect_error(tmp_path, request, "enable_pin must name a declared input pin")


def test_enable_level_requires_enable_pin(tmp_path):
    request = _inverter_request(tmp_path)
    request["cell"]["enable_level"] = "high"
    _expect_error(tmp_path, request, "enable_level requires")


def test_enable_level_must_be_high_or_low(tmp_path):
    request = _latch_request(tmp_path, level="active")
    _expect_error(tmp_path, request, 'enable_level must be "high" or "low"')


def test_clock_edge_does_not_apply_to_a_latch(tmp_path):
    """`clock_edge` asserts edge-triggered semantics; a latch's two edges both
    follow from `enable_level`, so accepting it would let the two disagree."""
    request = _latch_request(tmp_path)
    request["cell"]["clock_edge"] = "falling"
    _expect_error(tmp_path, request, "clock_edge does not apply to a latch")


@pytest.mark.parametrize(
    "level,opening,active",
    [("high", "rising_edge", True), ("low", "falling_edge", False)],
)
def test_latch_derives_an_enable_arc_and_a_transparent_arc(
    tmp_path, level, opening, active
):
    """Per output: the enable->Q arc on the *opening* edge, plus the
    transparent D->Q combinational arc with the enable held active."""
    from klayout_tools import characterize as ch

    request = _latch_request(tmp_path, level=level)
    cell = ch._resolve_cell_spec(request["cell"], str(tmp_path), "request.cell")
    assert cell["latch"] is True
    arcs = ch._resolve_arcs({}, cell)
    assert [(a.output_pin, a.related_pin, a.timing_type) for a in arcs] == [
        ("Q", "E", opening),
        ("Q", "D", "combinational"),
    ]
    # the enable arc toggles D between the output-high and output-low states
    assert dict(arcs[0].side_inputs) == {"D": True}
    assert dict(arcs[0].fall_side_inputs) == {"D": False}
    # the transparent arc holds the enable at its ACTIVE level
    assert dict(arcs[1].side_inputs) == {"E": active}
    assert arcs[1].timing_sense == "positive_unate"


def test_latch_without_enable_declaration_is_still_combinational(tmp_path):
    """`enable_pin` is what makes the cell a latch: the plain combinational
    path is untouched."""
    from klayout_tools import characterize as ch

    request = _inverter_request(tmp_path)
    cell = ch._resolve_cell_spec(request["cell"], str(tmp_path), "request.cell")
    assert cell["latch"] is False and cell["clock_pin"] is None
    (arc,) = ch._resolve_arcs({}, cell)
    assert arc.timing_type == "combinational"


def test_latch_deck_drives_each_arc_by_its_own_kind(tmp_path):
    """One deck, two stimulus shapes: the enable arc gets the three-pulse
    clock and a data PWL, the transparent arc gets the plain ramp with the
    enable tied to a rail."""
    from klayout_tools import characterize as ch
    from klayout_tools import liberty_writer as lw

    request = _latch_request(tmp_path)
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
    lines = plan["netlist_text"].splitlines()
    # arc 0 (E -> Q): three clock pulses, and D on its own data source
    enable = next(ln for ln in lines if ln.startswith("Va0s0l0 i_"))
    assert enable.count("1.8") == 6
    assert any(ln.startswith("Va0s0l0d_D") for ln in lines)
    # arc 1 (D -> Q): one low->high->low ramp, no data source, E on `vdd`
    data = next(ln for ln in lines if ln.startswith("Va1s0l0 i_"))
    assert data.count("1.8") == 2
    assert not any(ln.startswith("Va1s0l0d_") for ln in lines)
    instance = next(ln for ln in lines if ln.startswith("Xa1s0l0 "))
    assert instance.split()[2] == "vdd"
    # the transparent arc is measured without the sequential TD scoping
    cards = {m["name"]: m["spice"] for m in plan["measurements"]}
    assert "TD=" in cards["a0s0l0_dr"] and "TD=" not in cards["a1s0l0_dr"]


@_SKIP_NO_NGSPICE
def test_latch_example_yields_all_three_latch_shapes_end_to_end(tmp_path):
    """The latch worked example, through a real single-corner ngspice run:
    the transparent arc, the enable arc, and setup/hold on the closing edge."""
    example = Path(__file__).parent.parent / "examples" / "characterize"
    report = characterize.run_characterize(
        str(example / "request-dlat.json"), outdir=str(tmp_path), backend="local"
    )
    cell = report["cells"][0] if "cells" in report else report
    assert {(a["related_pin"], a["timing_type"]) for a in cell["arcs"]} == {
        ("E", "rising_edge"),
        ("D", "combinational"),
    }
    by_pin = {a["related_pin"]: a for a in cell["arcs"]}
    for arc in cell["arcs"]:
        for name in ("cell_rise", "cell_fall", "rise_transition", "fall_transition"):
            flat = [v for row in arc[name]["values"] for v in row]
            assert all(v > 0 for v in flat), (arc["related_pin"], name)
    # The transparent path is the short one: the enable has to travel the
    # cell's own clock buffers before the output can move.
    assert (
        by_pin["D"]["cell_rise"]["values"][0][0]
        < by_pin["E"]["cell_rise"]["values"][0][0]
    )
    # An active-high enable closes falling, which is what D is constrained to.
    assert {e["timing_type"] for e in cell["constraints"]} == {
        "setup_falling",
        "hold_falling",
    }
    lib_text = next(tmp_path.glob("*.lib")).read_text()
    assert "timing_type : rising_edge" in lib_text
    assert "timing_type : combinational" in lib_text
    assert "timing_type : setup_falling" in lib_text

    # AC: the emitted .lib must round-trip through statime's Liberty reader.
    # "skipped" (extension not built) is honest; "fail" never is.
    roundtrip = report["liberty"]["roundtrip"]
    assert roundtrip["status"] != "fail", roundtrip["message"]
    try:
        import klt_statime_native  # noqa: F401
    except ImportError:
        pass
    else:
        assert roundtrip["status"] == "pass", roundtrip["message"]
