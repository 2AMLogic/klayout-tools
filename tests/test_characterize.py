"""Tests for `klt characterize` and the modules behind it
(`klayout_tools.characterize`, `characterize_arcs`, `liberty_writer`).

Two tiers, mirroring `tests/test_sim.py`'s split (issue #2502):

- **Unit tests** (the majority) exercise the pieces that decide whether the
  emitted model is *correct* as pure functions, with no simulator anywhere:
  Liberty `function` parsing and arc/side-input derivation, the Liberty
  writer's rendering and its refusal to write a ragged or non-finite table,
  request validation, the generated testbench and `.meas` card set, the
  seconds -> nanoseconds table reshape, and the settle-window check. These
  always run, everywhere.
- **Integration tests** (`@pytest.mark.skipif(not HAVE_NGSPICE, ...)`) run
  the worked example end to end through the real `ngspice -b`: grid sweep,
  `.meas` extraction, `.lib` emission, and the **round-trip acceptance bar**
  the issue names explicitly -- the emitted file parsing back through
  `native/statime`'s own Liberty reader (`liberty.rs`/`nldm.rs`). CI installs
  `ngspice` (`.github/workflows/ci.yml`) so these run there; they skip with a
  clear reason on a machine without it rather than silently passing.

The end-to-end tier deliberately runs the *committed worked example*
(`examples/characterize/`) rather than a bespoke in-test fixture, so the
example the docs point a reader at cannot rot without a test noticing.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from klayout_tools import characterize, liberty_writer
from klayout_tools.characterize_arcs import FunctionError, derive_arcs, parse_function
from klayout_tools.cli import main

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

#: `KLT_SKIP_NGSPICE_TESTS=1` (local-only, set by `npm run check:ci` -- never
#: by CI) opts a host with ngspice installed out of this slow tier too; see
#: `tests/test_sim.py`'s `HAVE_NGSPICE` for the full rationale (issue #1651).
HAVE_NGSPICE = shutil.which("ngspice") is not None and (
    os.environ.get("KLT_SKIP_NGSPICE_TESTS") != "1"
)
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)

EXAMPLES_DIR = Path(__file__).parent.parent / "examples" / "characterize"

_TABLES = ("cell_rise", "cell_fall", "rise_transition", "fall_transition")


# --------------------------------------------------------------------------- #
# Fixture helpers (no simulator required)
# --------------------------------------------------------------------------- #


def _write_cells(tmp_path: Path) -> Path:
    """A synthetic inverter + NAND2 netlist, same shape as the worked
    example's (`.subckt` with named supply terminals)."""
    path = tmp_path / "cells.spice"
    path.write_text(
        ".subckt inv_demo Y A VDD VSS\n"
        "MN Y A VSS VSS nfet_demo w=1u l=0.15u\n"
        "MP Y A VDD VDD pfet_demo w=2u l=0.15u\n"
        ".ends inv_demo\n"
        "\n"
        ".subckt nand2_demo Y A B VDD VSS\n"
        "MN0 Y A net1 VSS nfet_demo w=2u l=0.15u\n"
        "MN1 net1 B VSS VSS nfet_demo w=2u l=0.15u\n"
        "MP0 Y A VDD VDD pfet_demo w=2u l=0.15u\n"
        "MP1 Y B VDD VDD pfet_demo w=2u l=0.15u\n"
        ".ends nand2_demo\n"
    )
    return path


def _inverter_request(tmp_path: Path, **overrides) -> dict:
    request = {
        "cell": {
            "name": "inv_demo",
            "netlist": str(_write_cells(tmp_path)),
            "pins": [
                {"name": "A", "direction": "input", "capacitance_pf": 0.002},
                {"name": "Y", "direction": "output", "function": "!A"},
            ],
            "power_pins": {"vdd": "VDD", "gnd": "VSS"},
        },
        "corner": {"name": "tt", "supply_v": 1.8, "temperature_c": 25},
        "grid": {"input_transition_ns": [0.02, 0.08], "output_load_pf": [0.005, 0.03]},
    }
    request.update(overrides)
    return request


def _write_request(tmp_path: Path, request: dict, name: str = "request.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(request))
    return path


def _table(rows: int = 2, cols: int = 2) -> liberty_writer.Table2D:
    return liberty_writer.Table2D(
        index_1=tuple(0.01 * (index + 1) for index in range(rows)),
        index_2=tuple(0.005 * (index + 1) for index in range(cols)),
        values=tuple(
            tuple(0.1 * (row + 1) * (col + 1) for col in range(cols))
            for row in range(rows)
        ),
    )


def _library(**overrides) -> liberty_writer.Library:
    arc = liberty_writer.TimingArc(
        related_pin="A",
        timing_sense="negative_unate",
        cell_rise=_table(),
        cell_fall=_table(),
        rise_transition=_table(),
        fall_transition=_table(),
    )
    kwargs = {
        "name": "unit_lib",
        "operating_conditions": liberty_writer.OperatingConditions(
            name="tt", process=1.0, temperature_c=25.0, voltage_v=1.8
        ),
        "cells": (
            liberty_writer.Cell(
                name="inv_demo",
                pins=(
                    liberty_writer.Pin(
                        name="A", direction="input", capacitance_pf=0.002
                    ),
                    liberty_writer.Pin(
                        name="Y", direction="output", function="!A", arcs=(arc,)
                    ),
                ),
            ),
        ),
    }
    kwargs.update(overrides)
    return liberty_writer.Library(**kwargs)


# --------------------------------------------------------------------------- #
# Liberty function parsing and arc derivation (characterize_arcs.py)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        # Inverter: prefix `!`, and the postfix `'` spelling of the same.
        ("!A", {(False,): True, (True,): False}),
        ("A'", {(False,): True, (True,): False}),
        # Buffer / identity.
        ("A", {(False,): False, (True,): True}),
    ],
)
def test_parse_function_single_input_truth_table(expression, expected):
    expr = parse_function(expression, allowed_pins=frozenset({"A"}))
    for (value,), want in expected.items():
        assert expr.evaluate({"A": value}) is want


@pytest.mark.parametrize(
    ("expression", "truth"),
    [
        # NAND2, in each of the three spellings the grammar accepts for AND.
        ("!(A*B)", [True, True, True, False]),
        ("!(A&B)", [True, True, True, False]),
        ("!(A B)", [True, True, True, False]),
        # NOR2, both OR spellings.
        ("!(A+B)", [True, False, False, False]),
        ("!(A|B)", [True, False, False, False]),
        # XOR / XNOR -- the non-unate cases.
        ("A^B", [False, True, True, False]),
        ("!(A^B)", [True, False, False, True]),
        # Juxtaposition as AND, the sg13g2_a21o_1 `(A1 A2) + B1` spelling.
        # `(A B) + B` simplifies to `B`, which is exactly the point: the
        # parser must apply juxtaposition, not concatenate identifiers.
        ("(A B) + B", [False, True, False, True]),
    ],
)
def test_parse_function_two_input_truth_table(expression, truth):
    expr = parse_function(expression, allowed_pins=frozenset({"A", "B"}))
    got = [
        expr.evaluate({"A": a, "B": b})
        for a, b in ((False, False), (False, True), (True, False), (True, True))
    ]
    assert got == truth


def test_parse_function_rejects_undeclared_name():
    """A `function` naming something that is not a declared input pin is the
    signal that the cell is sequential (an internal `IQ` node) or that pin
    metadata is missing -- a hard error, never a silently-free variable."""
    with pytest.raises(FunctionError, match="not a declared input pin"):
        parse_function("IQ", allowed_pins=frozenset({"D", "CLK"}))


@pytest.mark.parametrize(
    "expression", ["", "A +", "(A", "A B)", "A $ B", "!(A) extra_tok"]
)
def test_parse_function_rejects_malformed_expression(expression):
    with pytest.raises(FunctionError):
        parse_function(expression, allowed_pins=frozenset({"A", "B"}))


def test_derive_arcs_inverter_is_one_negative_unate_arc():
    arcs = derive_arcs(output_pin="Y", function="!A", input_pins=("A",))
    assert len(arcs) == 1
    assert (arcs[0].related_pin, arcs[0].timing_sense) == ("A", "negative_unate")
    assert arcs[0].measured_sense == "negative_unate"
    assert arcs[0].side_inputs == ()


def test_derive_arcs_nand2_holds_the_other_input_at_its_non_controlling_value():
    """The whole point of deriving arcs from `function`: an A->Y arc measured
    with B low measures nothing, because the NAND's output never moves."""
    arcs = derive_arcs(output_pin="Y", function="!(A*B)", input_pins=("A", "B"))

    assert [arc.related_pin for arc in arcs] == ["A", "B"]
    assert {arc.timing_sense for arc in arcs} == {"negative_unate"}
    assert arcs[0].side_input_map == {"B": True}  # non-controlling for AND
    assert arcs[1].side_input_map == {"A": True}


def test_derive_arcs_nor2_holds_the_other_input_low():
    arcs = derive_arcs(output_pin="Y", function="!(A+B)", input_pins=("A", "B"))

    assert arcs[0].side_input_map == {"B": False}  # non-controlling for OR
    assert arcs[1].side_input_map == {"A": False}


def test_derive_arcs_and2_is_positive_unate():
    arcs = derive_arcs(output_pin="Y", function="A*B", input_pins=("A", "B"))

    assert {arc.timing_sense for arc in arcs} == {"positive_unate"}
    assert arcs[0].side_input_map == {"B": True}


def test_derive_arcs_xor_is_non_unate_with_a_single_measured_polarity():
    """`timing_sense` reports the arc across *every* sensitizing vector, while
    `measured_sense` reports the polarity at the one vector the run drives --
    they differ exactly here."""
    arcs = derive_arcs(output_pin="Y", function="A^B", input_pins=("A", "B"))

    assert len(arcs) == 2
    for arc in arcs:
        assert arc.timing_sense == "non_unate"
        assert arc.measured_sense in ("positive_unate", "negative_unate")
        # The measured polarity must be consistent with the vector chosen.
        ((side_pin, side_value),) = arc.side_inputs
        expected = "negative_unate" if side_value else "positive_unate"
        assert arc.measured_sense == expected


def test_derive_arcs_aoi_covers_every_referenced_input():
    arcs = derive_arcs(
        output_pin="Y", function="!((A1 A2) + B1)", input_pins=("A1", "A2", "B1")
    )
    assert [arc.related_pin for arc in arcs] == ["A1", "A2", "B1"]
    # A1's arc needs A2 high (so the AND term can propagate) and B1 low (so
    # the OR term is not already forcing the output).
    assert arcs[0].side_input_map == {"A2": True, "B1": False}


def test_derive_arcs_tie_cell_has_no_arc():
    assert derive_arcs(output_pin="Y", function="1", input_pins=("A",)) == ()


def test_derive_arcs_is_deterministic():
    first = derive_arcs(output_pin="Y", function="!(A*B*C)", input_pins=("A", "B", "C"))
    second = derive_arcs(
        output_pin="Y", function="!(A*B*C)", input_pins=("A", "B", "C")
    )
    assert first == second


# --------------------------------------------------------------------------- #
# Liberty writer (liberty_writer.py)
# --------------------------------------------------------------------------- #


def test_render_library_emits_every_required_nldm_group():
    text = liberty_writer.render_library(_library())

    # The six names the issue's acceptance criterion enumerates.
    for group in ("cell_rise", "cell_fall", "rise_transition", "fall_transition"):
        assert f"{group} (" in text
    assert 'related_pin : "A";' in text
    assert "timing_sense : negative_unate;" in text
    # Plus the library-level scaffolding a reader needs to interpret them.
    assert "delay_model : table_lookup;" in text
    assert "lu_table_template (klt_char_template) {" in text
    assert "variable_1 : input_net_transition;" in text
    assert "variable_2 : total_output_net_capacitance;" in text
    assert 'direction : "output";' in text
    assert 'function : "!A";' in text


def test_render_library_balances_its_braces():
    text = liberty_writer.render_library(_library())
    assert text.count("{") == text.count("}")
    assert text.endswith("}\n")


def test_render_library_rejects_a_ragged_values_block():
    """A ragged table parses structurally and then silently interpolates
    against the wrong grid -- far worse than refusing to write it."""
    bad = liberty_writer.Table2D(
        index_1=(0.01, 0.02), index_2=(0.005, 0.03), values=((0.1, 0.2),)
    )
    library = _library_with_cell_rise(bad)
    with pytest.raises(liberty_writer.LibertyWriteError, match="value rows"):
        liberty_writer.render_library(library)


def test_render_library_rejects_a_short_row():
    bad = liberty_writer.Table2D(
        index_1=(0.01, 0.02), index_2=(0.005, 0.03), values=((0.1, 0.2), (0.3,))
    )
    with pytest.raises(liberty_writer.LibertyWriteError, match="value row 1"):
        liberty_writer.render_library(_library_with_cell_rise(bad))


def test_render_library_rejects_an_empty_index_axis():
    bad = liberty_writer.Table2D(index_1=(), index_2=(0.005,), values=())
    with pytest.raises(liberty_writer.LibertyWriteError, match="non-empty"):
        liberty_writer.render_library(_library_with_cell_rise(bad))


def test_render_library_rejects_an_unknown_timing_sense():
    arc = liberty_writer.TimingArc(
        related_pin="A",
        timing_sense="mostly_unate",
        cell_rise=_table(),
        cell_fall=_table(),
        rise_transition=_table(),
        fall_transition=_table(),
    )
    library = _library(
        cells=(
            liberty_writer.Cell(
                name="inv_demo",
                pins=(
                    liberty_writer.Pin(
                        name="Y", direction="output", function="!A", arcs=(arc,)
                    ),
                ),
            ),
        )
    )
    with pytest.raises(liberty_writer.LibertyWriteError, match="unknown timing_sense"):
        liberty_writer.render_library(library)


def test_render_library_rejects_an_unknown_direction():
    arc = liberty_writer.TimingArc(
        related_pin="A",
        timing_sense="negative_unate",
        cell_rise=_table(),
        cell_fall=_table(),
        rise_transition=_table(),
        fall_transition=_table(),
    )
    library = _library(
        cells=(
            liberty_writer.Cell(
                name="inv_demo",
                pins=(
                    liberty_writer.Pin(
                        name="Y", direction="output", function="!A", arcs=(arc,)
                    ),
                    liberty_writer.Pin(name="A", direction="sideways"),
                ),
            ),
        )
    )
    with pytest.raises(liberty_writer.LibertyWriteError, match="unknown direction"):
        liberty_writer.render_library(library)


def test_render_library_rejects_a_library_with_no_cell():
    with pytest.raises(liberty_writer.LibertyWriteError, match="at least one cell"):
        liberty_writer.render_library(_library(cells=()))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_format_number_rejects_non_finite(value):
    """`nan`/`inf` would render as a bare identifier the reader's number
    parser silently drops -- a failed measurement must be an error, never a
    missing table entry."""
    with pytest.raises(liberty_writer.LibertyWriteError, match="non-finite"):
        liberty_writer.format_number(value)


def test_format_number_normalises_negative_zero():
    assert liberty_writer.format_number(-0.0) == "0"


def test_format_number_is_fixed_precision():
    assert liberty_writer.format_number(1.0 / 3.0) == "0.333333"
    assert liberty_writer.format_number(1.0 / 3.0, precision=3) == "0.333"


def test_render_library_escapes_a_quote_in_a_function():
    library = _library(
        cells=(
            liberty_writer.Cell(
                name="c",
                pins=(
                    liberty_writer.Pin(name="Y", direction="output", function='A"B'),
                ),
            ),
        )
    )
    # No arcs -> no template source; that is its own named error, so assert on
    # the escaping via the pin renderer instead.
    with pytest.raises(
        liberty_writer.LibertyWriteError, match="at least one timing arc"
    ):
        liberty_writer.render_library(library)


def _library_with_cell_rise(table: liberty_writer.Table2D) -> liberty_writer.Library:
    arc = liberty_writer.TimingArc(
        related_pin="A",
        timing_sense="negative_unate",
        cell_rise=table,
        cell_fall=_table(),
        rise_transition=_table(),
        fall_transition=_table(),
    )
    return _library(
        cells=(
            liberty_writer.Cell(
                name="inv_demo",
                pins=(
                    liberty_writer.Pin(
                        name="Y", direction="output", function="!A", arcs=(arc,)
                    ),
                ),
            ),
        )
    )


# --------------------------------------------------------------------------- #
# Request validation (no simulator required)
# --------------------------------------------------------------------------- #


def _expect_error(tmp_path: Path, request: dict, match: str) -> None:
    path = _write_request(tmp_path, request)
    with pytest.raises(characterize.CharacterizeError, match=match):
        characterize.run_characterize(str(path))


def test_request_must_be_an_object(tmp_path):
    path = tmp_path / "request.json"
    path.write_text("[]")
    with pytest.raises(characterize.CharacterizeError, match="must be a JSON object"):
        characterize.run_characterize(str(path))


@pytest.mark.parametrize("field", ["cell", "corner", "grid"])
def test_request_requires_its_three_mandatory_blocks(tmp_path, field):
    request = _inverter_request(tmp_path)
    del request[field]
    _expect_error(tmp_path, request, f"request.{field} is required")


def test_malformed_netlist_is_a_named_error(tmp_path):
    """The issue's test plan names this case explicitly: a malformed input
    netlist must produce the documented error envelope, not a traceback."""
    netlist = tmp_path / "broken.spice"
    netlist.write_text("this file declares no subcircuit at all\n")
    request = _inverter_request(tmp_path)
    request["cell"]["netlist"] = str(netlist)
    _expect_error(tmp_path, request, r"no `\.subckt inv_demo` found")


def test_missing_netlist_is_a_named_error(tmp_path):
    request = _inverter_request(tmp_path)
    request["cell"]["netlist"] = str(tmp_path / "nope.spice")
    _expect_error(tmp_path, request, "cell netlist not found")


def test_pin_not_declared_by_the_subcircuit_is_refused(tmp_path):
    request = _inverter_request(tmp_path)
    request["cell"]["pins"].append({"name": "Z", "direction": "input"})
    _expect_error(tmp_path, request, "does not declare as a terminal")


def test_unaccounted_terminal_is_refused(tmp_path):
    """Every terminal must be a signal pin or a named supply, so the
    generated testbench can never leave one floating."""
    request = _inverter_request(tmp_path)
    request["cell"]["power_pins"] = {"vdd": "VDD", "gnd": "VDD"}
    _expect_error(tmp_path, request, "VSS")


def test_output_pin_without_a_function_is_refused(tmp_path):
    request = _inverter_request(tmp_path)
    request["cell"]["pins"][1].pop("function")
    _expect_error(tmp_path, request, "needs a Liberty `function`")


def test_sequential_cell_function_is_refused(tmp_path):
    """A `function` referencing an internal state node is out of scope -- and
    must say so, rather than be characterized as if it were combinational."""
    request = _inverter_request(tmp_path)
    request["cell"]["pins"][1]["function"] = "IQ"
    _expect_error(tmp_path, request, "not a declared input pin")


def test_tie_cell_has_no_arc_to_characterize(tmp_path):
    request = _inverter_request(tmp_path)
    request["cell"]["pins"][1]["function"] = "1"
    _expect_error(tmp_path, request, "no combinational timing arc")


def test_cell_with_no_output_pin_is_refused(tmp_path):
    request = _inverter_request(tmp_path)
    request["cell"]["pins"] = [{"name": "A", "direction": "input"}]
    _expect_error(tmp_path, request, "nothing to measure")


def test_duplicate_pin_is_refused(tmp_path):
    request = _inverter_request(tmp_path)
    request["cell"]["pins"].append({"name": "A", "direction": "input"})
    _expect_error(tmp_path, request, "more than once")


def test_inout_pin_is_out_of_scope(tmp_path):
    request = _inverter_request(tmp_path)
    request["cell"]["pins"][0]["direction"] = "inout"
    _expect_error(tmp_path, request, "out of scope")


@pytest.mark.parametrize("axis", ["input_transition_ns", "output_load_pf"])
def test_grid_axis_must_be_strictly_ascending(tmp_path, axis):
    """An NLDM index axis is interpolated over, so a repeated or out-of-order
    entry silently corrupts every lookup against it."""
    request = _inverter_request(tmp_path)
    request["grid"][axis] = [0.02, 0.02, 0.01]
    _expect_error(tmp_path, request, "strictly ascending")


@pytest.mark.parametrize("axis", ["input_transition_ns", "output_load_pf"])
def test_grid_axis_must_be_positive(tmp_path, axis):
    request = _inverter_request(tmp_path)
    request["grid"][axis] = [0.0, 0.02]
    _expect_error(tmp_path, request, "must be positive")


@pytest.mark.parametrize("axis", ["input_transition_ns", "output_load_pf"])
def test_grid_axis_is_required(tmp_path, axis):
    request = _inverter_request(tmp_path)
    del request["grid"][axis]
    _expect_error(tmp_path, request, f"request.grid.{axis} is required")


def test_corner_requires_supply_and_temperature(tmp_path):
    request = _inverter_request(tmp_path)
    del request["corner"]["supply_v"]
    _expect_error(tmp_path, request, "request.corner.supply_v is required")


def test_negative_supply_is_refused(tmp_path):
    request = _inverter_request(tmp_path)
    request["corner"]["supply_v"] = -1.8
    _expect_error(tmp_path, request, "must be positive")


def test_process_axis_without_a_model_library_is_refused(tmp_path):
    """A process axis needs a model library to select the section from."""
    request = _inverter_request(tmp_path)
    request["corner"]["process"] = "tt"
    _expect_error(tmp_path, request, "request.models is absent")


def test_unknown_threshold_field_is_refused(tmp_path):
    request = _inverter_request(tmp_path)
    request["thresholds"] = {"input_threshold_pct_rise": 50.0, "typo_pct": 1.0}
    _expect_error(tmp_path, request, "unknown field")


def test_inverted_slew_thresholds_are_refused(tmp_path):
    request = _inverter_request(tmp_path)
    request["thresholds"] = {
        "slew_lower_threshold_pct_rise": 80.0,
        "slew_upper_threshold_pct_rise": 20.0,
    }
    _expect_error(tmp_path, request, "0 < lower < upper < 100")


def test_arc_override_must_name_declared_pins(tmp_path):
    request = _inverter_request(tmp_path)
    request["arcs"] = [
        {"output_pin": "Y", "related_pin": "NOPE", "timing_sense": "negative_unate"}
    ]
    _expect_error(tmp_path, request, "must name a declared .*input pin")


def test_arc_override_rejects_a_non_unate_measured_sense(tmp_path):
    request = _inverter_request(tmp_path)
    request["arcs"] = [
        {
            "output_pin": "Y",
            "related_pin": "A",
            "timing_sense": "non_unate",
            "measured_sense": "non_unate",
        }
    ]
    _expect_error(tmp_path, request, "measured_sense must be")


def test_missing_osdi_preload_file_is_a_named_error(tmp_path):
    request = _inverter_request(tmp_path)
    request["options"] = {"osdi_preload": [str(tmp_path / "absent.osdi")]}
    _expect_error(tmp_path, request, "osdi_preload file not found")


# --------------------------------------------------------------------------- #
# Stimulus plan (generated deck + .meas cards; no simulator required)
# --------------------------------------------------------------------------- #


def _plan_for(tmp_path: Path, request: dict) -> tuple[dict, dict]:
    """Resolve `request` far enough to build its stimulus plan, without
    running anything."""
    request_dir = str(tmp_path)
    cell = characterize._resolve_cell(request, request_dir)
    corner = characterize._resolve_corner(request)
    grid = characterize._resolve_grid(request)
    thresholds = characterize._resolve_thresholds(request)
    options = characterize._resolve_options(request, grid, thresholds, request_dir)
    arcs = characterize._resolve_arcs(request, cell)
    plan = characterize._build_stimulus_plan(
        cell=cell,
        corner=corner,
        grid=grid,
        thresholds=thresholds,
        options=options,
        arcs=arcs,
    )
    return plan, {"cell": cell, "grid": grid, "arcs": arcs, "options": options}


def test_stimulus_plan_is_one_deck_covering_the_whole_grid(tmp_path):
    """The grid is measured in ONE simulator invocation, not one per point --
    a per-point sweep would be the hand-rolled-grid pattern `klt sim`'s
    corner matrix exists to replace."""
    request = _inverter_request(tmp_path)
    plan, _ = _plan_for(tmp_path, request)

    # 1 arc x 2 slews x 2 loads = 4 grid points, 4 tables each.
    assert len(plan["points"]) == 4
    assert len(plan["measurements"]) == 16
    # One cell instance per grid point, all in the one deck.
    lines = plan["netlist_text"].splitlines()
    assert sum(1 for line in lines if line.startswith("X")) == 4
    assert sum(1 for line in lines if line.startswith("C")) == 4  # private load
    # One private ramp source per point, plus the single shared supply.
    ramps = [line for line in lines if line.startswith("V")]
    assert len(ramps) == 5
    assert sum(1 for line in ramps if line.startswith("Vdd ")) == 1


def test_stimulus_plan_binds_subcircuit_terminals_positionally(tmp_path):
    """A SPICE `X` line is positional, so nodes must bind by the order the
    `.subckt` declares its terminals -- never by the request's pin order."""
    request = _inverter_request(tmp_path)
    # `.subckt inv_demo Y A VDD VSS` -- output first, then input, then rails.
    plan, _ = _plan_for(tmp_path, request)

    instance = next(
        line for line in plan["netlist_text"].splitlines() if line.startswith("X")
    )
    _, out_node, in_node, vdd_node, vss_node, subckt = instance.split()
    assert subckt == "inv_demo"
    assert out_node.startswith("o_")
    assert in_node.startswith("i_")
    assert (vdd_node, vss_node) == ("vdd", "0")


def test_stimulus_plan_holds_side_inputs_at_their_derived_rails(tmp_path):
    """The NAND2's A->Y arc must be measured with B tied to the supply."""
    request = _inverter_request(tmp_path)
    request["cell"].update(
        {
            "name": "nand2_demo",
            "pins": [
                {"name": "A", "direction": "input"},
                {"name": "B", "direction": "input"},
                {"name": "Y", "direction": "output", "function": "!(A*B)"},
            ],
        }
    )
    plan, resolved = _plan_for(tmp_path, request)

    assert len(resolved["arcs"]) == 2
    # `.subckt nand2_demo Y A B VDD VSS`: for the A arc B is a constant, for
    # the B arc A is -- and the constant is `vdd` (non-controlling for AND).
    instances = [
        line.split() for line in plan["netlist_text"].splitlines() if line[:1] == "X"
    ]
    a_arc = [row for row in instances if row[2].startswith("i_")]
    b_arc = [row for row in instances if row[3].startswith("i_")]
    assert len(a_arc) == 4 and len(b_arc) == 4
    assert all(row[3] == "vdd" for row in a_arc)
    assert all(row[2] == "vdd" for row in b_arc)


def test_measurement_cards_cover_all_four_nldm_quantities(tmp_path):
    request = _inverter_request(tmp_path)
    plan, _ = _plan_for(tmp_path, request)

    suffixes = sorted(
        {entry["name"].rsplit("_", 1)[1] for entry in plan["measurements"]}
    )
    assert suffixes == ["df", "dr", "tf", "tr"]
    for entry in plan["measurements"]:
        assert entry["spice"].startswith(f".meas tran {entry['name']} ")
        assert entry["unit"] == "s"


def test_measurement_cards_use_the_library_thresholds(tmp_path):
    """Delay is trigger-to-target at the 50% thresholds; transition is the
    20%/80% slew span -- at 1.8 V that is 0.9 / 0.36 / 1.44 V."""
    request = _inverter_request(tmp_path)
    plan, _ = _plan_for(tmp_path, request)
    by_suffix = {
        entry["name"].rsplit("_", 1)[1]: entry["spice"]
        for entry in plan["measurements"]
    }

    assert "VAL=0.9" in by_suffix["dr"] and "VAL=0.9" in by_suffix["df"]
    assert "VAL=0.36" in by_suffix["tr"] and "VAL=1.44" in by_suffix["tr"]
    assert "VAL=0.36" in by_suffix["tf"] and "VAL=1.44" in by_suffix["tf"]


def test_inverter_rise_delay_is_triggered_by_the_falling_input(tmp_path):
    """For a negative-unate arc the *falling* input edge produces the rising
    output edge -- get this backwards and every table is swapped."""
    request = _inverter_request(tmp_path)
    plan, _ = _plan_for(tmp_path, request)
    by_suffix = {
        entry["name"].rsplit("_", 1)[1]: entry["spice"]
        for entry in plan["measurements"]
    }

    assert "FALL=1\nTARG" not in by_suffix["dr"]  # sanity: single line
    trig, targ = by_suffix["dr"].split(" TARG ")
    assert "FALL=1" in trig and "RISE=1" in targ


def test_buffer_rise_delay_is_triggered_by_the_rising_input(tmp_path):
    request = _inverter_request(tmp_path)
    request["cell"]["pins"][1]["function"] = "A"  # positive unate
    plan, _ = _plan_for(tmp_path, request)
    by_suffix = {
        entry["name"].rsplit("_", 1)[1]: entry["spice"]
        for entry in plan["measurements"]
    }

    trig, targ = by_suffix["dr"].split(" TARG ")
    assert "RISE=1" in trig and "RISE=1" in targ


def test_input_ramp_undoes_the_slew_threshold_span(tmp_path):
    """`index_1` is a 20%-to-80% transition time, so the rail-to-rail ramp
    the stimulus applies is `index / 0.6`."""
    request = _inverter_request(tmp_path)
    _, resolved = _plan_for(tmp_path, request)

    assert resolved["options"]["ramps_ns"] == pytest.approx((0.02 / 0.6, 0.08 / 0.6))


def test_osdi_preload_is_emitted_in_its_own_control_block(tmp_path):
    """`pre_osdi` is a control command, not a netlist card -- sg13g2's PSP103
    MOSFETs will not instantiate without it."""
    osdi = tmp_path / "psp103.osdi"
    osdi.write_text("")
    request = _inverter_request(tmp_path)
    request["options"] = {"osdi_preload": [str(osdi)]}
    plan, _ = _plan_for(tmp_path, request)

    text = plan["netlist_text"]
    assert ".control" in text and ".endc" in text
    assert f"pre_osdi {osdi}" in text
    assert text.index(".control") < text.index(".include")


# --------------------------------------------------------------------------- #
# Table assembly and the settle check (no simulator required)
# --------------------------------------------------------------------------- #


def test_build_tables_converts_seconds_to_the_library_time_unit(tmp_path):
    request = _inverter_request(tmp_path)
    plan, resolved = _plan_for(tmp_path, request)
    # Every `.meas` result is in seconds; the tables are in ns.
    values = {entry["name"]: 1.5e-10 for entry in plan["measurements"]}

    tables = characterize._build_tables(
        plan, resolved["grid"], values, liberty_writer.Thresholds()
    )

    assert set(tables) == {0}
    for name in _TABLES:
        table = tables[0][name]
        assert table.index_1 == (0.02, 0.08)
        assert table.index_2 == (0.005, 0.03)
        assert table.values == ((0.15, 0.15), (0.15, 0.15))


def test_build_tables_applies_the_slew_derate_to_transition_tables_only(tmp_path):
    request = _inverter_request(tmp_path)
    request["thresholds"] = {"slew_derate_from_library": 0.5}
    plan, resolved = _plan_for(tmp_path, request)
    thresholds = characterize._resolve_thresholds(request)
    values = {entry["name"]: 1.0e-9 for entry in plan["measurements"]}

    tables = characterize._build_tables(plan, resolved["grid"], values, thresholds)

    assert tables[0]["cell_rise"].values[0][0] == pytest.approx(1.0)
    assert tables[0]["rise_transition"].values[0][0] == pytest.approx(0.5)


def test_build_tables_refuses_a_grid_hole(tmp_path):
    request = _inverter_request(tmp_path)
    plan, resolved = _plan_for(tmp_path, request)
    values = {entry["name"]: 1.0e-10 for entry in plan["measurements"]}
    values.pop(plan["measurements"][0]["name"])

    with pytest.raises(characterize.CharacterizeError, match="no measured value"):
        characterize._build_tables(
            plan, resolved["grid"], values, liberty_writer.Thresholds()
        )


def test_settle_check_refuses_a_window_the_slowest_point_outgrew(tmp_path):
    """An under-sized transient window produces a table that is *wrong*
    rather than absent, so it is a hard error with an actionable message."""
    request = _inverter_request(tmp_path)
    plan, resolved = _plan_for(tmp_path, request)
    settle = resolved["options"]["settle_ns"]
    # delay + transition far past the window.
    values = {entry["name"]: settle * 1e-9 for entry in plan["measurements"]}
    tables = characterize._build_tables(
        plan, resolved["grid"], values, liberty_writer.Thresholds()
    )

    with pytest.raises(
        characterize.CharacterizeError, match="transient window too short"
    ):
        characterize._check_settle_margin(
            plan, resolved["grid"], tables, resolved["options"]
        )


def test_settle_check_accepts_a_comfortable_window(tmp_path):
    request = _inverter_request(tmp_path)
    plan, resolved = _plan_for(tmp_path, request)
    values = {entry["name"]: 1.0e-11 for entry in plan["measurements"]}
    tables = characterize._build_tables(
        plan, resolved["grid"], values, liberty_writer.Thresholds()
    )

    characterize._check_settle_margin(
        plan, resolved["grid"], tables, resolved["options"]
    )


def test_extract_measurements_refuses_a_failed_sim_corner(tmp_path):
    report = {
        "corners": [
            {
                "corner_id": "tt/1.800V/25C",
                "status": "fail",
                "diagnostics": [{"code": "convergence", "message": "no convergence"}],
            }
        ]
    }
    with pytest.raises(characterize.CharacterizeError, match="reported status 'fail'"):
        characterize._extract_measurements(report, "report.json")


def test_extract_measurements_refuses_a_null_measurement(tmp_path):
    report = {
        "corners": [
            {
                "corner_id": "tt",
                "status": "pass",
                "measurements": [{"name": "a0s0l0_dr", "value": None}],
            }
        ]
    }
    with pytest.raises(characterize.CharacterizeError, match="produced no value"):
        characterize._extract_measurements(report, "report.json")


def test_extract_measurements_refuses_a_multi_corner_report(tmp_path):
    report = {"corners": [{"corner_id": "a"}, {"corner_id": "b"}]}
    with pytest.raises(characterize.CharacterizeError, match="single-corner"):
        characterize._extract_measurements(report, "report.json")


# --------------------------------------------------------------------------- #
# Round-trip probe netlist (no simulator required)
# --------------------------------------------------------------------------- #


def test_probe_netlist_instantiates_the_cell_once(tmp_path):
    text = characterize._probe_netlist(
        cell_name="nand2_demo", input_pins=("A", "B"), output_pins=("Y",)
    )
    assert "module klt_characterize_probe(A, B, Y);" in text
    assert "nand2_demo u0 (" in text
    assert ".A(A)" in text and ".B(B)" in text and ".Y(Y)" in text
    assert text.count("u0") == 1
    assert text.rstrip().endswith("endmodule")


# --------------------------------------------------------------------------- #
# Worked example, end to end through a real ngspice (issue #2502's test plan)
# --------------------------------------------------------------------------- #


@pytest.fixture
def example_run(tmp_path):
    """Run the committed worked example in an isolated copy of its directory,
    so the repo's own `examples/characterize/.klt/` is never written to."""
    workdir = tmp_path / "characterize"
    shutil.copytree(EXAMPLES_DIR, workdir)
    report = characterize.run_characterize(str(workdir / "request.json"))
    return report, workdir


@_SKIP_NO_NGSPICE
def test_example_emits_a_lib_that_round_trips_through_statime(example_run):
    """The issue's explicit acceptance bar: the emitted `.lib` parses through
    `native/statime`'s existing Liberty reader.

    `roundtrip.status` is `"skipped"` (never a fabricated pass) when the
    `klt_statime_native` extension is not built, so assert on `!= "fail"` and
    additionally assert a real `"pass"` when the extension is present.
    """
    report, _ = example_run
    roundtrip = report["liberty"]["roundtrip"]

    assert roundtrip["engine"] == "klt_statime_native"
    assert roundtrip["status"] in ("pass", "skipped")
    assert roundtrip["status"] != "fail", roundtrip["message"]
    assert os.path.isfile(roundtrip["probe_netlist"])

    if _have_statime():
        assert roundtrip["status"] == "pass", roundtrip["message"]
        assert "interpolated" in roundtrip["message"]


@_SKIP_NO_NGSPICE
def test_example_lib_parses_through_statime_when_reread(example_run):
    """Re-run the reader over the emitted file directly, independently of the
    in-run check, so a regression in `roundtrip_check` itself cannot mask a
    genuinely unparseable file."""
    if not _have_statime():
        pytest.skip("klt_statime_native extension is not installed")

    report, workdir = example_run
    result = characterize.roundtrip_check(
        lib_path=report["liberty"]["path"],
        cell_name=report["cell"]["name"],
        input_pins=("A", "B"),
        output_pins=("Y",),
        work_dir=str(workdir),
    )
    assert result["status"] == "pass", result["message"]


@_SKIP_NO_NGSPICE
def test_example_lib_carries_every_required_nldm_group(example_run):
    report, _ = example_run
    text = Path(report["liberty"]["path"]).read_text()

    assert "library (characterize_demo_tt_1p80V_25C) {" in text
    assert "cell (nand2_demo) {" in text
    for group in _TABLES:
        # Two arcs (A->Y, B->Y), one of each table per arc.
        assert text.count(f"{group} (") == 2
    assert text.count('related_pin : "A";') == 1
    assert text.count('related_pin : "B";') == 1
    assert text.count("timing_sense : negative_unate;") == 2
    assert text.count("timing_type : combinational;") == 2
    assert text.count("{") == text.count("}")


@_SKIP_NO_NGSPICE
def test_example_response_matches_the_documented_envelope(example_run):
    """`docs/json-contract.md`: every verb emits a flat payload carrying its
    own per-command `schema_version`, and the payload must be JSON
    serialisable as-is."""
    report, _ = example_run

    assert report["schema_version"] == characterize.SCHEMA_VERSION == 1
    json.dumps(report)  # must round-trip through the serialiser unchanged

    assert set(report) == {
        "schema_version",
        "cell",
        "corner",
        "grid",
        "thresholds",
        "arcs",
        "liberty",
        "simulation",
        "provenance",
    }
    # `cell.netlist` is an *input* being pinned -> {path, scope} envelope.
    assert set(report["cell"]["netlist"]) == {"path", "scope"}
    # Generated artifacts are plain absolute strings (json-contract.md's
    # "Output-artifact path fields").
    for path in (
        report["liberty"]["path"],
        report["simulation"]["testbench"],
        report["simulation"]["request"],
        report["simulation"]["report"],
    ):
        assert os.path.isabs(path) and os.path.isfile(path)
    # Shared provenance block.
    assert report["provenance"]["input"]["role"] == "netlist"
    assert report["provenance"]["input"]["content_hash"].startswith("sha256:")


@_SKIP_NO_NGSPICE
def test_example_reports_both_derived_arcs_with_their_side_state(example_run):
    report, _ = example_run

    assert report["grid"]["arc_count"] == 2
    assert report["grid"]["points"] == 16
    assert report["grid"]["measurement_count"] == 16 * 2 * 4

    arcs = {arc["related_pin"]: arc for arc in report["arcs"]}
    assert set(arcs) == {"A", "B"}
    assert arcs["A"]["side_inputs"] == {"B": True}
    assert arcs["B"]["side_inputs"] == {"A": True}
    for arc in arcs.values():
        assert arc["output_pin"] == "Y"
        assert arc["timing_sense"] == "negative_unate"


@_SKIP_NO_NGSPICE
def test_example_tables_grow_with_output_load(example_run):
    """Not an accuracy claim (that is issue #2503's job) -- just the one
    monotonicity property every NLDM table has unconditionally: a heavier
    output load is slower, for all four quantities at every input slew."""
    report, _ = example_run

    for arc in report["arcs"]:
        for name in _TABLES:
            values = arc[name]["values"]
            assert len(values) == 4 and all(len(row) == 4 for row in values)
            for row in values:
                assert row == sorted(row), (name, row)
                assert row[-1] > row[0], (name, row)


@_SKIP_NO_NGSPICE
def test_example_transitions_grow_with_input_slew(example_run):
    """A slower input edge cannot produce a faster output edge.

    Deliberately asserted for the *transition* tables only: `cell_rise`/
    `cell_fall` are 50%-to-50% delays, which legitimately **shrink** (and can
    go negative) at the slow-slew / light-load corner when the output finishes
    switching before the input crosses its own threshold -- see
    `docs/cli/characterize.md`'s "Negative delays are legitimate". The
    synthetic NAND in this example exhibits exactly that, so asserting
    slew-monotonic delay here would encode a physical falsehood.
    """
    report, _ = example_run

    for arc in report["arcs"]:
        for name in ("rise_transition", "fall_transition"):
            columns = zip(*arc[name]["values"], strict=True)
            for column in columns:
                assert list(column) == sorted(column), (name, column)


@_SKIP_NO_NGSPICE
def test_example_json_matches_the_lib_it_emitted(example_run):
    """The response's tables are documented as "same numbers as the `.lib`",
    so a consumer need not re-parse Liberty to read them."""
    report, _ = example_run
    text = Path(report["liberty"]["path"]).read_text()

    for arc in report["arcs"]:
        for name in _TABLES:
            for row in arc[name]["values"]:
                rendered = ", ".join(
                    liberty_writer.format_number(value) for value in row
                )
                assert f'"{rendered}"' in text


@_SKIP_NO_NGSPICE
def test_example_writes_its_artifacts_next_to_the_request(example_run):
    report, workdir = example_run
    expected = workdir / ".klt" / "characterize"

    assert Path(report["simulation"]["testbench"]).parent == expected
    assert Path(report["liberty"]["path"]).parent == expected
    # The generated sim request is kept, so a failed grid point is debuggable.
    sim_request = json.loads(Path(report["simulation"]["request"]).read_text())
    assert sim_request["engine"] == "ngspice"
    assert sim_request["analysis"]["kind"] == "tran"
    assert len(sim_request["measurements"]) == report["grid"]["measurement_count"]


@_SKIP_NO_NGSPICE
def test_lib_override_writes_where_asked(tmp_path):
    workdir = tmp_path / "characterize"
    shutil.copytree(EXAMPLES_DIR, workdir)
    target = tmp_path / "out" / "custom.lib"

    report = characterize.run_characterize(
        str(workdir / "request.json"), lib_path=str(target)
    )

    assert report["liberty"]["path"] == str(target)
    assert target.is_file()


# --------------------------------------------------------------------------- #
# CLI envelope (issue #2502's test plan: schema_version + error shape)
# --------------------------------------------------------------------------- #


@_SKIP_NO_NGSPICE
def test_cli_json_success_envelope(tmp_path, capsys):
    workdir = tmp_path / "characterize"
    shutil.copytree(EXAMPLES_DIR, workdir)

    exit_code = main(
        ["characterize", str(workdir / "request.json"), "--format", "json"]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 1
    assert payload["cell"]["name"] == "nand2_demo"
    assert captured.err == ""


@_SKIP_NO_NGSPICE
def test_cli_text_success_renders_a_summary(tmp_path, capsys):
    workdir = tmp_path / "characterize"
    shutil.copytree(EXAMPLES_DIR, workdir)

    exit_code = main(["characterize", str(workdir / "request.json")])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "cell: nand2_demo" in out
    assert "arc: A -> Y [negative_unate]" in out
    assert "liberty:" in out


def test_cli_json_error_envelope_on_a_malformed_netlist(tmp_path, capsys):
    """The issue's test plan names this case: a malformed input netlist must
    produce `docs/json-contract.md`'s error envelope on **stderr**, exit `1`,
    and leave stdout empty."""
    netlist = tmp_path / "broken.spice"
    netlist.write_text("not a spice netlist\n")
    request = _inverter_request(tmp_path)
    request["cell"]["netlist"] = str(netlist)
    path = _write_request(tmp_path, request)

    exit_code = main(["characterize", str(path), "--format", "json"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["schema_version"] == 1
    assert error["error"]["command"] == "characterize"
    assert ".subckt inv_demo" in error["error"]["message"]


def test_cli_text_error_envelope(tmp_path, capsys):
    request = _inverter_request(tmp_path)
    del request["grid"]["output_load_pf"]
    path = _write_request(tmp_path, request)

    exit_code = main(["characterize", str(path)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("klt characterize: ")
    assert "request.grid.output_load_pf is required" in captured.err


def test_cli_missing_request_file_is_the_error_envelope(tmp_path, capsys):
    exit_code = main(
        ["characterize", str(tmp_path / "absent.json"), "--format", "json"]
    )

    assert exit_code == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["command"] == "characterize"


def test_cli_verb_is_registered():
    """`klt characterize` is reachable through the shared parser, like every
    other verb (the issue's first acceptance criterion)."""
    from klayout_tools.cli.parser import create_parser

    args = create_parser().parse_args(["characterize", "request.json"])
    assert args.request == "request.json"
    assert args.format in ("text", "json")
    assert callable(args.func)


# --------------------------------------------------------------------------- #
# Worked-example fixtures stay in sync with their generator
# --------------------------------------------------------------------------- #


def test_example_generator_reproduces_the_committed_fixtures(tmp_path, monkeypatch):
    """`examples/characterize/generate.py` is the source of truth for the
    committed fixtures -- regenerating must be a no-op."""
    import importlib.util

    workdir = tmp_path / "characterize"
    shutil.copytree(EXAMPLES_DIR, workdir)
    generator = workdir / "generate.py"

    spec = importlib.util.spec_from_file_location("_char_example_gen", generator)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()

    for name in ("cells.spice", "models.lib", "request.json"):
        assert (workdir / name).read_text() == (EXAMPLES_DIR / name).read_text(), name


def _have_statime() -> bool:
    try:
        import klt_statime_native  # noqa: F401
    except ImportError:
        return False
    return True
