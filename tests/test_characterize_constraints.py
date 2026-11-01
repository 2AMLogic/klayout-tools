"""Setup/hold constraint arcs for sequential cells (issue #4756)."""

import json
import shutil
from pathlib import Path

import pytest

from klayout_tools import characterize, liberty_writer
from klayout_tools import characterize_bisect as bisect
from klayout_tools import characterize_constraints as cc
from klayout_tools.characterize_arcs import derive_sequential_arc

_SKIP_NO_NGSPICE = pytest.mark.skipif(
    shutil.which("ngspice") is None, reason="ngspice is not installed"
)

_OPTIONS = {"degradation": 0.1, "window_ns": (-1.0, 2.0), "tolerance_ns": 0.002}


def _cell(constrained=("D",)):
    return {
        "name": "dff",
        "clock_pin": "CLK",
        "clock_edge": "rising",
        "input_pins": ("CLK", "D"),
        "constrained_pins": tuple(constrained),
    }


def _arcs(next_state="D", out="Q"):
    return (
        derive_sequential_arc(
            output_pin=out,
            next_state=next_state,
            data_pins=("D",),
            clock_pin="CLK",
            clock_edge="rising",
        ),
    )


def _dff_request(tmp_path: Path) -> dict:
    cells = tmp_path / "dff.spice"
    cells.write_text(
        ".subckt dff_demo Q CLK D VDD VSS\n"
        "MN Q D VSS VSS nfet_demo w=1u l=0.15u\n.ends\n"
    )
    return {
        "cell": {
            "name": "dff_demo",
            "netlist": str(cells),
            "clock_pin": "CLK",
            "constrained_pins": ["D"],
            "pins": [
                {"name": "CLK", "direction": "input", "capacitance_pf": 0.002},
                {"name": "D", "direction": "input", "capacitance_pf": 0.002},
                {"name": "Q", "direction": "output", "function": "D"},
            ],
            "power_pins": {"vdd": "VDD", "gnd": "VSS"},
        },
        "corner": {"name": "tt", "supply_v": 1.8, "temperature_c": 25},
        "grid": {"input_transition_ns": [0.02], "output_load_pf": [0.005]},
    }


def _expect_error(tmp_path: Path, request: dict, match: str) -> None:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    with pytest.raises(characterize.CharacterizeError, match=match):
        characterize.run_characterize(str(path))


def test_constrained_pins_requires_clock_pin(tmp_path):
    request = _dff_request(tmp_path)
    del request["cell"]["clock_pin"]
    request["cell"]["pins"][2]["function"] = "D"
    _expect_error(tmp_path, request, "constrained_pins requires")


def test_constrained_pins_may_not_name_the_clock_or_unknown_pins(tmp_path):
    request = _dff_request(tmp_path)
    request["cell"]["constrained_pins"] = ["CLK"]
    _expect_error(tmp_path, request, "not a declared non-clock input pin")
    request["cell"]["constrained_pins"] = ["Z"]
    _expect_error(tmp_path, request, "not a declared non-clock input pin")


@pytest.mark.parametrize(
    "key,value",
    [
        ("constraint_degradation", 0),
        ("constraint_window_ns", [1.0, 2.0]),
        ("constraint_tolerance_ns", -1),
    ],
)
def test_constraint_options_are_validated(key, value):
    with pytest.raises(bisect.ConstraintError, match=key):
        cc.resolve_constraint_options({key: value})


def test_cases_cover_both_edges_and_kinds_with_output_polarity():
    cases = cc.derive_cases(_cell(), _arcs("!D", "QN"))
    assert {(c.edge, c.kind) for c in cases} == {
        ("rise", "setup"),
        ("rise", "hold"),
        ("fall", "setup"),
        ("fall", "hold"),
    }
    by_key = {(c.edge, c.kind): c for c in cases}
    # QN = !D: setup captures the new value, so a rising D makes QN fall.
    assert by_key[("rise", "setup")].out_rising is False
    # hold keeps the old value, so a rising (late) D leaves QN rising.
    assert by_key[("rise", "hold")].out_rising is True
    assert by_key[("fall", "setup")].out_rising is True


def _fake_bisect(monkeypatch, boundary):
    """Drive run_bisection with a synthetic cell: capture succeeds (delay 100 ps,
    degrading toward the boundary) for offsets above `boundary`."""

    def fake_build_deck(**kwargs):
        return "", [("probe", p) for p in kwargs["probes"]], 1.0

    monkeypatch.setattr(bisect, "build_deck", fake_build_deck)

    def run_deck(text, measurements, label, stop):
        out = {}
        for k, (_, probe) in enumerate(measurements):
            out[f"c{k}_d"] = 1e-10 if probe[3] >= boundary else None
        return out

    cell = _cell()
    cases = cc.derive_cases(cell, _arcs())[:1]
    return dict(
        cell=cell,
        cases=cases,
        grid_ns=(0.02, 0.24),
        rail_nodes={},
        vdd=1.8,
        thresholds=liberty_writer.Thresholds(),
        ramps_ns=(0.03, 0.4),
        settle_ns=2.0,
        load_pf=0.005,
        options=_OPTIONS,
        run_deck=run_deck,
    )


def test_bisection_converges_to_boundary_within_tolerance(monkeypatch):
    kwargs = _fake_bisect(monkeypatch, boundary=0.137)
    outcome = bisect.run_bisection(**kwargs)
    assert outcome["iterations"] == 11
    assert len(outcome["values"]) == 4
    for value in outcome["values"].values():
        # conservative side: the reported value always passes
        assert 0.137 <= value <= 0.137 + _OPTIONS["tolerance_ns"]


def test_negative_hold_boundary_is_reported(monkeypatch):
    kwargs = _fake_bisect(monkeypatch, boundary=-0.25)
    outcome = bisect.run_bisection(**kwargs)
    for value in outcome["values"].values():
        assert -0.25 <= value <= -0.25 + _OPTIONS["tolerance_ns"]


def test_wide_end_never_capturing_is_a_named_error(monkeypatch):
    kwargs = _fake_bisect(monkeypatch, boundary=5.0)
    with pytest.raises(bisect.ConstraintError, match="cannot be bracketed"):
        bisect.run_bisection(**kwargs)


def test_boundary_below_window_is_a_named_error(monkeypatch):
    kwargs = _fake_bisect(monkeypatch, boundary=-9.0)
    with pytest.raises(bisect.ConstraintError, match="below the search window"):
        bisect.run_bisection(**kwargs)


def test_build_deck_places_data_edge_relative_to_clock():
    cell = {
        **_cell(),
        "netlist_path": "dff.spice",
        "subckt": "dff",
        "terminals": ("Q", "CLK", "D", "VDD", "VSS"),
        "output_pins": ("Q",),
    }
    cases = cc.derive_cases(cell, _arcs())
    setup = next(
        i for i, c in enumerate(cases) if (c.edge, c.kind) == ("rise", "setup")
    )
    kwargs = dict(
        cell=cell,
        cases=cases,
        rail_nodes={"VDD": "vdd", "VSS": "0"},
        vdd=1.8,
        thresholds=liberty_writer.Thresholds(),
        ramps_ns=(0.1,),
        settle_ns=2.0,
        window_ns=(-1.0, 2.0),
        load_pf=0.005,
        edge="rising",
    )
    early, meas, stop = bisect.build_deck(probes=[(setup, 0, 0, 0.5)], **kwargs)
    late, _, _ = bisect.build_deck(probes=[(setup, 0, 0, 0.1)], **kwargs)
    assert early != late and "XC0".lower() in early.lower()
    assert "TARG v(o_c0_Q) VAL=0.9 RISE=1" in meas[0]["spice"]
    assert stop > 0


def test_liberty_writer_renders_setup_and_hold_groups():
    table = liberty_writer.Table2D((0.02, 0.24), (0.02, 0.24), ((0.1, 0.2), (0.3, 0.4)))
    arc = liberty_writer.ConstraintArc("CLK", "setup_rising", table, table)
    pin = liberty_writer.Pin("D", "input", capacitance_pf=0.002, constraints=(arc,))
    clk_to_q = liberty_writer.TimingArc(
        "CLK", "non_unate", table, table, table, table, timing_type="rising_edge"
    )
    out = liberty_writer.Pin("Q", "output", arcs=(clk_to_q,))
    lib = liberty_writer.Library(
        name="t",
        operating_conditions=liberty_writer.OperatingConditions("tt", 1.0, 25.0, 1.8),
        cells=(liberty_writer.Cell("dff", (pin, out)),),
    )
    text = liberty_writer.render_library(lib)
    assert "timing_type : setup_rising;" in text
    assert "rise_constraint (klt_char_constraint_template)" in text
    assert "variable_1 : constrained_pin_transition;" in text


@_SKIP_NO_NGSPICE
def test_dff_constraints_end_to_end(tmp_path):
    example = Path(__file__).parent.parent / "examples" / "characterize"
    request = json.loads((example / "request-dff-constraints.json").read_text())
    request["grid"] = {"input_transition_ns": [0.08], "output_load_pf": [0.01]}
    request["cell"]["netlist"] = str(example / "dff.spice")
    request["models"]["lib"] = str(example / "models.lib")
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    report = characterize.run_characterize(
        str(path), outdir=str(tmp_path / "out"), backend="local"
    )
    cell = report["cells"][0] if "cells" in report else report
    entries = cell["constraints"]
    assert {(e["kind"], e["data_edge"]) for e in entries} == {
        ("setup", "rise"),
        ("setup", "fall"),
        ("hold", "rise"),
        ("hold", "fall"),
    }
    for e in entries:
        assert e["timing_type"] in ("setup_rising", "hold_rising")
        (value,) = e["constraint"]["values"][0]
        assert -1.0 < value < 2.0
    lib = next((tmp_path / "out").glob("*.lib")).read_text()
    assert "timing_type : setup_rising;" in lib and "timing_type : hold_rising;" in lib
