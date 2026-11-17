"""Asynchronous set/reset arcs for sequential cells (issue #5520):
``preset``/``clear`` propagation and ``recovery``/``removal`` constraints."""

import json
import shutil
from pathlib import Path

import pytest

from klayout_tools import characterize, liberty_writer
from klayout_tools import characterize_async as ca
from klayout_tools import characterize_bisect as bisect
from klayout_tools import characterize_sequential as cs
from klayout_tools.characterize_cards import spice_number

_SKIP_NO_NGSPICE = pytest.mark.skipif(
    shutil.which("ngspice") is None, reason="ngspice is not installed"
)

_OPTIONS = {"degradation": 0.1, "window_ns": (-1.0, 2.0), "tolerance_ns": 0.002}


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


def _dffsr_request(tmp_path: Path) -> dict:
    """A minimal request shaped like the dffsr example (never simulated --
    these tests only exercise the request-validation path)."""
    netlist = tmp_path / "dffsr.spice"
    netlist.write_text(
        ".subckt dffsr_demo Q CLK D R S VDD VSS\n"
        "MN Q D VSS VSS nfet_demo w=1u l=0.15u\n.ends\n"
    )
    return {
        "cell": {
            "name": "dffsr_demo",
            "netlist": str(netlist),
            "clock_pin": "CLK",
            "async_pins": [
                {"pin": "R", "kind": "clear", "active": "low"},
                {"pin": "S", "kind": "preset", "active": "low"},
            ],
            "pins": [
                {"name": "CLK", "direction": "input", "capacitance_pf": 0.002},
                {"name": "D", "direction": "input", "capacitance_pf": 0.002},
                {"name": "R", "direction": "input", "capacitance_pf": 0.002},
                {"name": "S", "direction": "input", "capacitance_pf": 0.002},
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


def test_async_pins_requires_clock_pin(tmp_path):
    request = _dffsr_request(tmp_path)
    del request["cell"]["clock_pin"]
    _expect_error(tmp_path, request, "async_pins requires")


@pytest.mark.parametrize(
    "pin,match",
    [
        ("CLK", "not a declared non-clock input pin"),
        ("ZZ", "not a declared non-clock input pin"),
        (None, "not a declared non-clock input pin"),
    ],
)
def test_async_pin_must_be_a_declared_non_clock_input(tmp_path, pin, match):
    request = _dffsr_request(tmp_path)
    request["cell"]["async_pins"] = [{"pin": pin, "kind": "clear", "active": "low"}]
    _expect_error(tmp_path, request, match)


def test_async_pin_may_not_be_listed_twice(tmp_path):
    request = _dffsr_request(tmp_path)
    request["cell"]["async_pins"] = [
        {"pin": "R", "kind": "clear", "active": "low"},
        {"pin": "R", "kind": "preset", "active": "high"},
    ]
    _expect_error(tmp_path, request, "lists 'R' twice")


def test_async_pin_may_not_also_be_static(tmp_path):
    request = _dffsr_request(tmp_path)
    request["cell"]["static_inputs"] = {"R": True}
    _expect_error(tmp_path, request, "static_inputs also ties off")


def test_async_pin_may_not_also_be_constrained(tmp_path):
    request = _dffsr_request(tmp_path)
    request["cell"]["constrained_pins"] = ["R"]
    _expect_error(tmp_path, request, "constrained_pins")


def test_async_pin_may_not_be_referenced_by_an_output_function(tmp_path):
    request = _dffsr_request(tmp_path)
    request["cell"]["pins"][4]["function"] = "D * R"
    _expect_error(tmp_path, request, "next-state function also")


@pytest.mark.parametrize(
    "entry,match",
    [
        ({"pin": "R", "kind": "reset", "active": "low"}, r"kind must be"),
        ({"pin": "R", "kind": "clear", "active": "lo"}, r"active must be"),
        ("R", "must be an object"),
    ],
)
def test_async_pin_fields_are_validated(tmp_path, entry, match):
    request = _dffsr_request(tmp_path)
    request["cell"]["async_pins"] = [entry]
    _expect_error(tmp_path, request, match)


def test_async_pins_must_be_a_non_empty_array(tmp_path):
    request = _dffsr_request(tmp_path)
    request["cell"]["async_pins"] = []
    _expect_error(tmp_path, request, "non-empty array")


# --------------------------------------------------------------------------- #
# Derivation
# --------------------------------------------------------------------------- #


def _cell(active="low", edge="rising", outputs=(("Q", "D"), ("QN", "!D"))):
    return {
        "name": "dffsr",
        "clock_pin": "CLK",
        "clock_edge": edge,
        "input_pins": ("CLK", "D", "R"),
        "output_pins": tuple(name for name, _ in outputs),
        "pins": (
            {"name": "CLK", "direction": "input"},
            {"name": "D", "direction": "input"},
            {"name": "R", "direction": "input"},
            *(
                {"name": name, "direction": "output", "function": fn}
                for name, fn in outputs
            ),
        ),
        "static_inputs": (),
        "constrained_pins": (),
        "async_pins": (cs.AsyncPin(pin="R", kind="clear", active=active),),
    }


def _arcs(cell):
    return cs.derive_sequential_arcs(cell)


def test_declared_async_pins_are_held_inactive_in_every_other_measurement():
    arcs = _arcs(_cell(active="low"))
    for arc in arcs:
        assert dict(arc.side_inputs)["R"] is True
        assert dict(arc.fall_side_inputs)["R"] is True
    # ... and at the other level for an active-high pin.
    for arc in _arcs(_cell(active="high")):
        assert dict(arc.side_inputs)["R"] is False


def test_async_pins_are_not_part_of_the_next_state_cube():
    cell = _cell()
    arcs = _arcs(cell)
    # The functions reference D alone; R shows up only as a held level.
    assert {a.output_pin for a in arcs} == {"Q", "QN"}
    assert all(a.related_pin == "CLK" for a in arcs)


def test_async_cases_cover_every_arc_and_pin_with_both_primings():
    cell = _cell()
    cases = ca.derive_async_cases(cell, _arcs(cell))
    assert [(c.output_pin, c.pin) for c in cases] == [("Q", "R"), ("QN", "R")]
    q = cases[0]
    # Q is high with D high; the priming that can show a RISE is the low one.
    assert dict(q.high_state)["D"] is True
    assert dict(q.low_state)["D"] is False
    assert q.state(True) == q.low_state and q.state(False) == q.high_state
    # The driven pin is never in the held state.
    assert "R" not in dict(q.high_state)


# --------------------------------------------------------------------------- #
# Propagation deck and direction resolution
# --------------------------------------------------------------------------- #


def _deck_cell(cell):
    return {
        **cell,
        "netlist_path": "dffsr.spice",
        "subckt": "dffsr",
        "terminals": ("Q", "QN", "CLK", "D", "R", "VDD", "VSS"),
    }


def _deck_kwargs(cell, cases):
    return dict(
        cell=_deck_cell(cell),
        cases=cases,
        rail_nodes={"VDD": "vdd", "VSS": "0"},
        vdd=1.8,
        thresholds=liberty_writer.Thresholds(),
        ramps_ns=(0.1,),
        settle_ns=2.0,
        loads_pf=(0.005,),
        edge=cell["clock_edge"],
    )


def test_async_deck_asserts_the_pin_after_the_priming_clock_pulse():
    cell = _cell(active="low")
    cases = ca.derive_async_cases(cell, _arcs(cell))
    text, meas, stop = ca.build_async_deck(
        probes=[(0, 0, 0, False)], **_deck_kwargs(cell, cases)
    )
    _prime, _w, assert_ns, expected_stop = ca._schedule(2.0, 0.1)
    assert stop == expected_stop
    at = spice_number(assert_ns)
    # The pin idles HIGH (active low) and falls at the assert time.
    pin_line = next(ln for ln in text.splitlines() if ln.startswith("Van0 "))
    assert pin_line.startswith(f"Van0 ap_n0 0 PWL(0n 1.8 {at}n 1.8 ")
    assert pin_line.endswith("n 0)")
    # Both cards are gated past the priming pulse, and the delay is measured
    # on the pin's FALLing (asserting) edge.
    assert all(f"TD={at}n" in m["spice"] for m in meas)
    assert "TRIG v(ap_n0) VAL=0.9 FALL=1" in meas[0]["spice"]
    assert "FALL=1" in meas[0]["spice"].split("TARG")[1]


def test_async_deck_asserts_an_active_high_pin_with_a_rising_edge():
    cell = _cell(active="high")
    cases = ca.derive_async_cases(cell, _arcs(cell))
    text, meas, _ = ca.build_async_deck(
        probes=[(0, 0, 0, True)], **_deck_kwargs(cell, cases)
    )
    assert "Van0 ap_n0 0 PWL(0n 0 " in text
    assert "TRIG v(ap_n0) VAL=0.9 RISE=1" in meas[0]["spice"]
    assert "RISE=1" in meas[0]["spice"].split("TARG")[1]


def _probes(cases, slews=1, loads=1):
    return [
        (ci, si, li, rising)
        for ci in range(len(cases))
        for si in range(slews)
        for li in range(loads)
        for _, rising in ca.PRIMINGS
    ]


def _raw(probes, answering):
    """A fake deck result where only probes whose ``out_rising`` is in
    ``answering`` (per case index) crossed."""
    out = {}
    for k, (ci, _si, _li, rising) in enumerate(probes):
        hit = rising in answering.get(ci, ())
        out[f"n{k}_d"] = 1e-10 if hit else None
        out[f"n{k}_t"] = 5e-11 if hit else None
    return out


def test_direction_is_taken_from_whichever_priming_moved():
    cell = _cell()
    cases = ca.derive_async_cases(cell, _arcs(cell))
    probes = _probes(cases)
    # Q falls (clear), QN rises (preset on the inverted output).
    raw = _raw(probes, {0: (False,), 1: (True,)})
    entries = ca.build_async_arcs(
        cases=cases,
        raw=raw,
        probes=probes,
        grid_ns=(0.02,),
        loads_pf=(0.005,),
        thresholds=liberty_writer.Thresholds(),
        clock_pin="CLK",
    )
    assert [(e["case"].output_pin, e["timing_type"]) for e in entries] == [
        ("Q", "clear"),
        ("QN", "preset"),
    ]
    # An active-low pin forcing Q low moves the same way the pin does.
    assert entries[0]["timing_sense"] == "positive_unate"
    assert entries[1]["timing_sense"] == "negative_unate"


def test_an_active_high_pin_inverts_the_reported_sense():
    cell = _cell(active="high")
    cases = ca.derive_async_cases(cell, _arcs(cell))
    probes = _probes(cases)
    entries = ca.build_async_arcs(
        cases=cases,
        raw=_raw(probes, {0: (False,), 1: (True,)}),
        probes=probes,
        grid_ns=(0.02,),
        loads_pf=(0.005,),
        thresholds=liberty_writer.Thresholds(),
        clock_pin="CLK",
    )
    assert entries[0]["timing_sense"] == "negative_unate"
    assert entries[1]["timing_sense"] == "positive_unate"


@pytest.mark.parametrize(
    "answering,match",
    [
        ({}, "does not force this output"),
        ({0: (True, False)}, "one consistent direction"),
    ],
)
def test_an_unresolvable_direction_is_a_named_error(answering, match):
    cell = _cell(outputs=(("Q", "D"),))
    cases = ca.derive_async_cases(cell, _arcs(cell))
    probes = _probes(cases)
    with pytest.raises(bisect.ConstraintError, match=match):
        ca.build_async_arcs(
            cases=cases,
            raw=_raw(probes, answering),
            probes=probes,
            grid_ns=(0.02,),
            loads_pf=(0.005,),
            thresholds=liberty_writer.Thresholds(),
            clock_pin="CLK",
        )


def test_a_forced_arc_carries_one_direction_only():
    cell = _cell()
    cases = ca.derive_async_cases(cell, _arcs(cell))
    probes = _probes(cases)
    entries = ca.build_async_arcs(
        cases=cases,
        raw=_raw(probes, {0: (False,), 1: (True,)}),
        probes=probes,
        grid_ns=(0.02,),
        loads_pf=(0.005,),
        thresholds=liberty_writer.Thresholds(),
        clock_pin="CLK",
    )
    liberty = ca.liberty_async(entries)
    (clear,) = liberty["Q"]
    assert clear.timing_type == "clear"
    assert clear.cell_fall is not None and clear.fall_transition is not None
    assert clear.cell_rise is None and clear.rise_transition is None
    (preset,) = liberty["QN"]
    assert preset.timing_type == "preset"
    assert preset.cell_rise is not None and preset.cell_fall is None
    # The JSON entry names only the tables that were measured.
    (q_entry, qn_entry) = ca.response_entries(entries)
    assert "cell_fall" in q_entry and "cell_rise" not in q_entry
    assert q_entry["forced_level"] == "low" and qn_entry["forced_level"] == "high"


# --------------------------------------------------------------------------- #
# Liberty rendering
# --------------------------------------------------------------------------- #
