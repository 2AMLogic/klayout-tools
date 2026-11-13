"""Liberty rendering, recovery/removal shapes and the end-to-end run for
``klt characterize``'s asynchronous set/reset arcs (issue #5520).

Split from :mod:`test_characterize_async` to keep each file under the
per-file size ratchet; the request validation, case derivation and probe-deck
tests stay there, and this module reuses its fixtures.
"""

import json
from pathlib import Path

import pytest

from klayout_tools import characterize, liberty_writer
from klayout_tools import characterize_async as ca
from klayout_tools import characterize_bisect as bisect
from klayout_tools import characterize_constraints as cc
from test_characterize_async import (
    _OPTIONS,
    _SKIP_NO_NGSPICE,
    _arcs,
    _cell,
    _deck_cell,
)


def _library(arcs, constraints=()):
    table = liberty_writer.Table2D((0.02,), (0.005,), ((0.1,),))
    clk_to_q = liberty_writer.TimingArc(
        "CLK", "non_unate", table, table, table, table, timing_type="rising_edge"
    )
    out = liberty_writer.Pin("Q", "output", arcs=(clk_to_q, *arcs))
    pin = liberty_writer.Pin("R", "input", constraints=constraints)
    return liberty_writer.Library(
        name="t",
        operating_conditions=liberty_writer.OperatingConditions("tt", 1.0, 25.0, 1.8),
        cells=(liberty_writer.Cell("dffsr", (pin, out)),),
    )


def test_liberty_writer_renders_a_one_direction_forced_arc():
    table = liberty_writer.Table2D((0.02,), (0.005,), ((0.1,),))
    clear = liberty_writer.TimingArc(
        "R",
        "positive_unate",
        cell_fall=table,
        fall_transition=table,
        timing_type="clear",
    )
    text = liberty_writer.render_library(_library((clear,)))
    assert "timing_type : clear;" in text
    assert 'related_pin : "R";' in text
    # Nothing is fabricated for the direction the pin does not force.
    forced = text.split('related_pin : "R";')[1]
    assert "cell_fall" in forced and "cell_rise" not in forced


def test_liberty_writer_refuses_a_timing_arc_with_no_table():
    empty = liberty_writer.TimingArc("R", "positive_unate", timing_type="clear")
    with pytest.raises(
        liberty_writer.LibertyWriteError, match="needs at least one table"
    ):
        liberty_writer.render_library(_library((empty,)))


def test_liberty_writer_renders_recovery_and_removal_groups():
    table = liberty_writer.Table2D((0.02,), (0.02,), ((0.1,),))
    arcs = tuple(
        liberty_writer.ConstraintArc("CLK", f"{kind}_rising", rise_constraint=table)
        for kind in ("recovery", "removal")
    )
    text = liberty_writer.render_library(_library((), constraints=arcs))
    assert "timing_type : recovery_rising;" in text
    assert "timing_type : removal_rising;" in text
    assert "fall_constraint" not in text


# --------------------------------------------------------------------------- #
# Recovery / removal cases
# --------------------------------------------------------------------------- #


def _measured(cell, forced):
    """The async-propagation records :func:`cc.derive_async_cases` reads,
    with ``forced`` giving each output's measured ``out_rising``."""
    cases = ca.derive_async_cases(cell, _arcs(cell))
    return [{"case": case, "out_rising": forced[case.output_pin]} for case in cases]


def test_recovery_and_removal_constrain_the_release_edge():
    cell = _cell(active="low")
    cases = cc.derive_async_cases(cell, _measured(cell, {"Q": False, "QN": True}))
    assert [(c.kind, c.edge) for c in cases] == [
        ("recovery", "rise"),
        ("removal", "rise"),
    ]
    for case in cases:
        assert case.pin == "R" and case.output_pin == "Q"
        # R forces Q low, so the data must make the clock capture a HIGH.
        assert dict(case.others)["D"] is True
        assert case.out_rising is True


def test_an_active_high_async_pin_is_released_on_a_falling_edge():
    cell = _cell(active="high")
    cases = cc.derive_async_cases(cell, _measured(cell, {"Q": True, "QN": False}))
    assert {c.edge for c in cases} == {"fall"}
    # Q is forced high, so the clock must be made to capture a LOW.
    assert dict(cases[0].others)["D"] is False
    assert cases[0].out_rising is False


def test_release_only_groups_carry_a_single_constraint_table():
    cell = _cell(active="low")
    cases = cc.derive_async_cases(cell, _measured(cell, {"Q": False, "QN": True}))
    table = liberty_writer.Table2D((0.02,), (0.02,), ((0.1,),))
    entries = [{"case": case, "table": table} for case in cases]
    grouped = cc.liberty_constraints(entries, "CLK", "rising")
    assert [arc.timing_type for arc in grouped["R"]] == [
        "recovery_rising",
        "removal_rising",
    ]
    for arc in grouped["R"]:
        assert arc.rise_constraint is table and arc.fall_constraint is None


def test_a_falling_edge_clock_names_the_falling_groups():
    cell = _cell(active="low", edge="falling")
    cases = cc.derive_async_cases(cell, _measured(cell, {"Q": False, "QN": True}))
    table = liberty_writer.Table2D((0.02,), (0.02,), ((0.1,),))
    entries = [{"case": case, "table": table} for case in cases]
    grouped = cc.liberty_constraints(entries, "CLK", "falling")
    assert [arc.timing_type for arc in grouped["R"]] == [
        "recovery_falling",
        "removal_falling",
    ]
    outcome = {
        "values": {(c, 0, 0): 0.1 for c in range(len(cases))},
        "reference_ns": {(c, 0, 0): None for c in range(len(cases))},
        "iterations": 3,
    }
    rows = cc.response_entries(entries, outcome, "CLK", "falling", _OPTIONS)
    assert [r["timing_type"] for r in rows] == [
        "recovery_falling",
        "removal_falling",
    ]
    # Removal passes on the *absence* of a transition: no reference delay,
    # no degradation.
    removal = rows[1]
    assert removal["reference_delay_ns"] is None
    assert removal["degradation"] is None


# --------------------------------------------------------------------------- #
# Bisection: the removal criterion is the inverse of setup/hold's
# --------------------------------------------------------------------------- #


def test_the_release_edge_moves_to_the_other_side_of_the_clock_for_removal():
    cell = _cell(active="low")
    cases = cc.derive_async_cases(cell, _measured(cell, {"Q": False, "QN": True}))
    kwargs = dict(
        cell=_deck_cell(cell),
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
    recovery, _, _ = bisect.build_deck(probes=[(0, 0, 0, 0.5)], **kwargs)
    removal, _, _ = bisect.build_deck(probes=[(1, 0, 0, 0.5)], **kwargs)

    def release(text):
        line = next(ln for ln in text.splitlines() if ln.startswith("Vdc0"))
        points = line.split("PWL(")[1].rstrip(")").split()
        return float(points[-4].rstrip("n"))

    # Same offset, opposite sides of the clock edge; both are a single edge
    # from the asserted level (0 V for an active-low pin) to the idle one.
    assert release(recovery) < release(removal)
    assert "Vdc0 dd_c0 0 PWL(0n 0 " in recovery
    assert "Vdc0 dd_c0 0 PWL(0n 0 " in removal


def _fake_bisect(monkeypatch, cases, outcome_of):
    def fake_build_deck(**kwargs):
        return "", [("probe", p) for p in kwargs["probes"]], 1.0

    monkeypatch.setattr(bisect, "build_deck", fake_build_deck)

    def run_deck(text, measurements, label, stop):
        return {
            f"c{k}_d": outcome_of(cases[probe[0]], probe[3])
            for k, (_, probe) in enumerate(measurements)
        }

    return dict(
        cell={"clock_edge": "rising"},
        cases=cases,
        grid_ns=(0.02,),
        rail_nodes={},
        vdd=1.8,
        thresholds=liberty_writer.Thresholds(),
        ramps_ns=(0.03,),
        settle_ns=2.0,
        load_pf=0.005,
        options=_OPTIONS,
        run_deck=run_deck,
    )


def _async_cases(active="low"):
    cell = _cell(active=active)
    return cc.derive_async_cases(cell, _measured(cell, {"Q": False, "QN": True}))


def test_removal_passes_when_the_clock_edge_is_ignored(monkeypatch):
    (removal,) = [c for c in _async_cases() if c.kind == "removal"]
    boundary = 0.3

    # The clock edge is captured (a delay is measured) only while the pin is
    # released EARLY; past the boundary it is ignored and there is no
    # crossing at all -- which for removal is the passing outcome.
    def outcome(case, offset):
        return None if offset >= boundary else 1e-10

    outcome_dict = bisect.run_bisection(
        **_fake_bisect(monkeypatch, (removal,), outcome)
    )
    (value,) = outcome_dict["values"].values()
    assert boundary <= value <= boundary + _OPTIONS["tolerance_ns"]
    # Removal has no reference delay to report.
    assert set(outcome_dict["reference_ns"].values()) == {None}


def test_recovery_keeps_the_setup_hold_pass_criterion(monkeypatch):
    (recovery,) = [c for c in _async_cases() if c.kind == "recovery"]
    boundary = 0.25

    def outcome(case, offset):
        return 1e-10 if offset >= boundary else None

    result = bisect.run_bisection(**_fake_bisect(monkeypatch, (recovery,), outcome))
    (value,) = result["values"].values()
    assert boundary <= value <= boundary + _OPTIONS["tolerance_ns"]
    (reference,) = result["reference_ns"].values()
    assert reference == pytest.approx(0.1)


@pytest.mark.parametrize(
    "outcome,match",
    [
        # Captured even at the wide end: never ignored, cannot be bracketed.
        (lambda case, offset: 1e-10, "cannot be bracketed"),
        # Ignored even at the narrow end: the boundary is below the window.
        (lambda case, offset: None, "below the search window"),
    ],
)
def test_removal_that_cannot_be_bracketed_is_a_named_error(monkeypatch, outcome, match):
    (removal,) = [c for c in _async_cases() if c.kind == "removal"]
    with pytest.raises(bisect.ConstraintError, match=match):
        bisect.run_bisection(**_fake_bisect(monkeypatch, (removal,), outcome))


def test_removal_errors_name_the_release_edge_not_a_data_edge(monkeypatch):
    (removal,) = [c for c in _async_cases() if c.kind == "removal"]
    with pytest.raises(bisect.ConstraintError, match="rise release edge"):
        bisect.run_bisection(
            **_fake_bisect(monkeypatch, (removal,), lambda case, offset: 1e-10)
        )


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #


@_SKIP_NO_NGSPICE
def test_dffsr_async_arcs_end_to_end(tmp_path):
    example = Path(__file__).parent.parent / "examples" / "characterize"
    request = json.loads((example / "request-dffsr.json").read_text())
    request["grid"] = {"input_transition_ns": [0.08], "output_load_pf": [0.01]}
    request["cell"]["netlist"] = str(example / "dffsr.spice")
    request["models"]["lib"] = str(example / "models.lib")
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    report = characterize.run_characterize(
        str(path), outdir=str(tmp_path / "out"), backend="local"
    )
    cell = report["cells"][0]

    # AC3: a propagation arc per (async pin, forced output), with the
    # timing_type following the OUTPUT's direction -- so the active-low
    # clear R reads `clear` on Q and `preset` on the inverted QN.
    arcs = {(a["pin"], a["output_pin"]): a for a in cell["async_arcs"]}
    assert set(arcs) == {("R", "Q"), ("R", "QN"), ("S", "Q"), ("S", "QN")}
    assert arcs[("R", "Q")]["timing_type"] == "clear"
    assert arcs[("R", "QN")]["timing_type"] == "preset"
    assert arcs[("S", "Q")]["timing_type"] == "preset"
    assert arcs[("S", "QN")]["timing_type"] == "clear"
    assert arcs[("R", "Q")]["timing_sense"] == "positive_unate"
    assert arcs[("R", "QN")]["timing_sense"] == "negative_unate"
    for key, arc in arcs.items():
        rising = arc["forced_level"] == "high"
        delay = arc["cell_rise" if rising else "cell_fall"]
        transition = arc["rise_transition" if rising else "fall_transition"]
        # Only the forced direction is reported, and it is finite.
        assert ("cell_fall" in arc) is not rising, key
        for table in (delay, transition):
            for row in table["values"]:
                assert all(0.0 < v < 100.0 for v in row), (key, row)

    # AC4: recovery and removal of each async pin against the clock.
    entries = {(e["pin"], e["kind"]) for e in cell["constraints"]}
    assert entries == {
        ("R", "recovery"),
        ("R", "removal"),
        ("S", "recovery"),
        ("S", "removal"),
    }
    for entry in cell["constraints"]:
        assert entry["timing_type"] in ("recovery_rising", "removal_rising")
        # Active-low pins are released on a rising edge.
        assert entry["data_edge"] == "rise"
        (value,) = entry["constraint"]["values"][0]
        assert -1.0 < value < 2.0

    lib = next((tmp_path / "out").glob("*.lib")).read_text()
    for group in ("clear", "preset", "recovery_rising", "removal_rising"):
        assert f"timing_type : {group};" in lib

    # AC6: the emitted Liberty still parses back through statime's reader.
    roundtrip = report["liberty"]["roundtrip"]
    assert roundtrip["status"] in ("pass", "skipped")
    assert roundtrip["status"] != "fail", roundtrip["message"]
    try:
        import klt_statime_native  # noqa: F401
    except ImportError:
        pass
    else:
        assert roundtrip["status"] == "pass", roundtrip["message"]


@_SKIP_NO_NGSPICE
def test_a_request_without_async_pins_keeps_its_response_shape(tmp_path):
    """AC2's other half: nothing is added to a cell that declares none."""
    example = Path(__file__).parent.parent / "examples" / "characterize"
    request = json.loads((example / "request-dff.json").read_text())
    request["grid"] = {"input_transition_ns": [0.08], "output_load_pf": [0.01]}
    request["cell"]["netlist"] = str(example / "dff.spice")
    request["models"]["lib"] = str(example / "models.lib")
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    report = characterize.run_characterize(
        str(path), outdir=str(tmp_path / "out"), backend="local"
    )
    assert "async_arcs" not in report["cells"][0]
    lib = next((tmp_path / "out").glob("*.lib")).read_text()
    for group in ("clear", "preset", "recovery", "removal"):
        assert f"timing_type : {group}" not in lib
