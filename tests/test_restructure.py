"""Hermetic tests for `klayout_tools.restructure` -- the bounded
cell-resizing loop backing `klt synthesize`'s `--restructure-timing` flag
(issue #926, Epic #704 Phase 3).

Entirely hermetic, mirroring `tests/test_sta.py`'s own tier-1 posture: no
Rust toolchain, no PDK, no real liberty. `compute_critical_path` (imported
from `klayout_tools.sta` into `klayout_tools.restructure`) is monkeypatched
directly, so these tests exercise this module's own logic -- the liberty
pin-compatibility check, the netlist text-substitution, and the bounded
greedy loop -- without depending on whether `klt_statime_native` happens to
be built on the machine running the suite.
"""

from __future__ import annotations

import re

import pytest

from klayout_tools import restructure as restructure_module
from klayout_tools.restructure import (
    DEFAULT_MAX_ITERATIONS,
    RestructureError,
    _cell_family_and_drive,
    _parse_liberty_pins,
    _swap_instance_cell_type,
    _upsize_candidates,
    restructure_for_timing,
)
from klayout_tools.sta import StaError

# ---------------------------------------------------------------------------
# Fixtures shared across this module -- a tiny synthetic "buf" family with a
# same-family, pin-incompatible sibling (`buf_3`) to prove the compatibility
# check actually looks at pins, not just the naming convention.
# ---------------------------------------------------------------------------

_BUF_PINS = {"A": "input", "Y": "output"}


def _liberty_text(cells: dict[str, dict[str, str]]) -> str:
    lines = ["library(test_lib) {"]
    for cell_name, pins in cells.items():
        lines.append(f"  cell ({cell_name}) {{")
        lines.append("    pg_pin(VPWR) { pg_type : primary_power; }")
        for pin_name, direction in pins.items():
            lines.append(f"    pin({pin_name}) {{")
            lines.append(f"      direction : {direction};")
            lines.append("    }")
        lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


_LIBERTY_CELLS = {
    "mylib__buf_1": _BUF_PINS,
    "mylib__buf_2": _BUF_PINS,
    "mylib__buf_4": _BUF_PINS,
    "mylib__buf_8": _BUF_PINS,
    # Same naming convention (family "mylib__buf", drive "3") but a
    # different pin set -- must never be proposed as an upsize target.
    "mylib__buf_3": {"A": "input", "B": "input", "Y": "output"},
    # A different family entirely -- must never be conflated with "buf".
    "mylib__inv_1": {"A": "input", "Y": "output"},
    # No trailing `_<digits>` at all -- e.g. a sequential cell.
    "mylib__dfrtp": {"D": "input", "CLK": "input", "Q": "output"},
}
_LIBERTY_TEXT = _liberty_text(_LIBERTY_CELLS)

_NETLIST_TEXT = """\
module top(a, y);
  input a;
  wire a;
  output y;
  wire y;
  mylib__buf_1 _100_ (
    .A(a),
    .Y(y)
  );
endmodule
"""


def _worst_path_for(cell_type: str, delay_ns: float) -> dict:
    """A minimal but structurally real `worst_path` -- the exact shape
    `native/statime/src/sta.rs` serialises (see `tests/test_sta.py`'s own
    `_FAKE_RESULT`) -- for a single-hop design driven by ``cell_type``."""
    return {
        "startpoint": "a",
        "startpoint_kind": "input port",
        "endpoint": "y",
        "endpoint_kind": "output port",
        "delay_ns": delay_ns,
        "hops": [
            {
                "point": "a (in)",
                "cell": None,
                "edge": "rise",
                "arrival_ns": 0.0,
                "slew_ns": 0.05,
            },
            {
                "point": "_100_/Y",
                "cell": cell_type,
                "edge": "rise",
                "arrival_ns": delay_ns,
                "slew_ns": 0.1,
            },
        ],
    }


_INSTANCE_CELL_RE = re.compile(r"^\s*(mylib__\S+)\s+_100_\s*\(", re.MULTILINE)


def _install_fake_compute(monkeypatch, delay_by_cell: dict[str, float]):
    """Monkeypatch `restructure.compute_critical_path` with a fake that
    reads the netlist file at the given path and returns a delay keyed by
    whichever cell type instance `_100_` currently uses -- so the fake
    genuinely reacts to the text substitution `restructure_for_timing`
    performs, rather than being scripted call-by-call."""

    def _fake(netlist_path, liberty_path, top, **kwargs):
        with open(netlist_path, encoding="utf-8") as handle:
            text = handle.read()
        match = _INSTANCE_CELL_RE.search(text)
        assert match is not None, "fixture netlist must instantiate _100_"
        cell_type = match.group(1)
        delay = delay_by_cell[cell_type]
        return {
            "top": top,
            "num_cells": 1,
            "num_nets": 2,
            "worst_path": _worst_path_for(cell_type, delay),
            "worst_reg_to_reg_path": None,
        }

    monkeypatch.setattr(restructure_module, "compute_critical_path", _fake)


# ---------------------------------------------------------------------------
# Liberty pin parsing
# ---------------------------------------------------------------------------


def test_parse_liberty_pins_extracts_direction_and_ignores_power_pins():
    pins_by_cell = _parse_liberty_pins(_LIBERTY_TEXT)

    assert pins_by_cell["mylib__buf_1"] == {"A": "input", "Y": "output"}
    # `pg_pin(VPWR)` is a *power* pin -- a different liberty keyword this
    # module's regex never matches -- so it must never show up here.
    assert "VPWR" not in pins_by_cell["mylib__buf_1"]


def test_parse_liberty_pins_handles_multiple_cells_independently():
    pins_by_cell = _parse_liberty_pins(_LIBERTY_TEXT)

    assert pins_by_cell["mylib__buf_3"] == {
        "A": "input",
        "B": "input",
        "Y": "output",
    }
    assert pins_by_cell["mylib__inv_1"] == {"A": "input", "Y": "output"}
    assert pins_by_cell["mylib__dfrtp"] == {
        "D": "input",
        "CLK": "input",
        "Q": "output",
    }


def test_parse_liberty_pins_unbalanced_braces_raises():
    with pytest.raises(RestructureError, match="unbalanced"):
        _parse_liberty_pins("cell (broken) { pin(A) { direction : input; }")


# ---------------------------------------------------------------------------
# Drive-strength family/naming convention
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cell_name,expected",
    [
        ("mylib__buf_1", ("mylib__buf", 1)),
        ("mylib__buf_16", ("mylib__buf", 16)),
        ("mylib__nand2_2", ("mylib__nand2", 2)),
        ("mylib__dfrtp", None),  # no trailing `_<digits>` at all
        ("no_digits_here", None),
    ],
)
def test_cell_family_and_drive(cell_name, expected):
    assert _cell_family_and_drive(cell_name) == expected


# ---------------------------------------------------------------------------
# Upsize candidate selection
# ---------------------------------------------------------------------------


def test_upsize_candidates_ascending_same_family_only():
    pins_by_cell = _parse_liberty_pins(_LIBERTY_TEXT)
    candidates = _upsize_candidates("mylib__buf_1", pins_by_cell)

    # buf_3 is excluded (pin-incompatible), buf_2/buf_4/buf_8 are the real
    # pin-compatible siblings, ascending by drive strength.
    assert candidates == ["mylib__buf_2", "mylib__buf_4", "mylib__buf_8"]


def test_upsize_candidates_excludes_lower_and_equal_drive():
    pins_by_cell = _parse_liberty_pins(_LIBERTY_TEXT)
    candidates = _upsize_candidates("mylib__buf_4", pins_by_cell)

    assert candidates == ["mylib__buf_8"]
    assert "mylib__buf_1" not in candidates
    assert "mylib__buf_2" not in candidates


def test_upsize_candidates_no_naming_convention_match_is_empty():
    pins_by_cell = _parse_liberty_pins(_LIBERTY_TEXT)
    assert _upsize_candidates("mylib__dfrtp", pins_by_cell) == []


def test_upsize_candidates_unknown_cell_is_empty():
    pins_by_cell = _parse_liberty_pins(_LIBERTY_TEXT)
    assert _upsize_candidates("mylib__nonesuch_1", pins_by_cell) == []


def test_upsize_candidates_largest_drive_has_no_further_candidates():
    pins_by_cell = _parse_liberty_pins(_LIBERTY_TEXT)
    assert _upsize_candidates("mylib__buf_8", pins_by_cell) == []


# ---------------------------------------------------------------------------
# Netlist instance-type substitution
# ---------------------------------------------------------------------------


def test_swap_instance_cell_type_rewrites_only_the_named_instance():
    text = (
        "  mylib__buf_1 _100_ (\n    .A(a),\n    .Y(y)\n  );\n"
        "  mylib__buf_1 _101_ (\n    .A(b),\n    .Y(z)\n  );\n"
    )
    new_text = _swap_instance_cell_type(text, "_100_", "mylib__buf_1", "mylib__buf_2")

    assert "mylib__buf_2 _100_ (" in new_text
    # The *other* instance of the same original cell type is untouched.
    assert "mylib__buf_1 _101_ (" in new_text
    assert new_text.count("mylib__buf_1") == 1


def test_swap_instance_cell_type_preserves_indentation_and_port_list():
    new_text = _swap_instance_cell_type(
        _NETLIST_TEXT, "_100_", "mylib__buf_1", "mylib__buf_2"
    )
    assert "  mylib__buf_2 _100_ (\n    .A(a),\n    .Y(y)\n  );\n" in new_text


def test_swap_instance_cell_type_missing_instance_raises():
    with pytest.raises(RestructureError, match="found 0"):
        _swap_instance_cell_type(_NETLIST_TEXT, "_999_", "mylib__buf_1", "mylib__buf_2")


def test_swap_instance_cell_type_wrong_cell_type_raises():
    with pytest.raises(RestructureError, match="found 0"):
        _swap_instance_cell_type(_NETLIST_TEXT, "_100_", "mylib__buf_2", "mylib__buf_4")


# ---------------------------------------------------------------------------
# The bounded restructuring loop
# ---------------------------------------------------------------------------


def test_no_violation_is_a_no_op(tmp_path, monkeypatch):
    """The starting delay already meets the target -- nothing is written,
    and the loop never even attempts a resize."""
    netlist_path = tmp_path / "top.v"
    netlist_path.write_text(_NETLIST_TEXT, encoding="utf-8")
    liberty_path = tmp_path / "test.lib"
    liberty_path.write_text(_LIBERTY_TEXT, encoding="utf-8")
    output_path = tmp_path / "top_restructured.v"

    _install_fake_compute(monkeypatch, {"mylib__buf_1": 1.0})

    report = restructure_for_timing(
        str(netlist_path),
        str(liberty_path),
        "top",
        2.0,
        output_netlist_path=str(output_path),
    )

    assert report["converged"] is True
    assert report["iterations_used"] == 0
    assert report["resizes_applied"] == []
    assert report["gave_up_reason"] is None
    assert report["restructured_netlist_path"] is None
    assert report["initial_worst_path_delay_ns"] == 1.0
    assert report["final_worst_path_delay_ns"] == 1.0
    assert not output_path.exists()


def test_single_resize_closes_the_violation(tmp_path, monkeypatch):
    """A single upsize is enough to meet the target -- the loop keeps it,
    converges, and the restructured netlist reflects the new cell type."""
    netlist_path = tmp_path / "top.v"
    netlist_path.write_text(_NETLIST_TEXT, encoding="utf-8")
    liberty_path = tmp_path / "test.lib"
    liberty_path.write_text(_LIBERTY_TEXT, encoding="utf-8")
    output_path = tmp_path / "top_restructured.v"

    _install_fake_compute(
        monkeypatch,
        {"mylib__buf_1": 5.0, "mylib__buf_2": 2.0},
    )

    report = restructure_for_timing(
        str(netlist_path),
        str(liberty_path),
        "top",
        3.0,
        output_netlist_path=str(output_path),
    )

    assert report["converged"] is True
    assert report["gave_up_reason"] is None
    assert report["iterations_used"] == 1
    assert report["resizes_applied"] == [
        {"instance": "_100_", "from_cell": "mylib__buf_1", "to_cell": "mylib__buf_2"}
    ]
    assert report["initial_worst_path_delay_ns"] == 5.0
    assert report["final_worst_path_delay_ns"] == 2.0
    assert report["restructured_netlist_path"] == str(output_path)

    written = output_path.read_text(encoding="utf-8")
    assert "mylib__buf_2 _100_ (" in written
    assert "mylib__buf_1" not in written


def test_multiple_resizes_until_convergence(tmp_path, monkeypatch):
    """Diminishing-but-real improvements across several candidates --
    the loop keeps applying resizes until the target is met, never giving
    up early just because one step wasn't enough on its own."""
    netlist_path = tmp_path / "top.v"
    netlist_path.write_text(_NETLIST_TEXT, encoding="utf-8")
    liberty_path = tmp_path / "test.lib"
    liberty_path.write_text(_LIBERTY_TEXT, encoding="utf-8")
    output_path = tmp_path / "top_restructured.v"

    _install_fake_compute(
        monkeypatch,
        {
            "mylib__buf_1": 5.0,
            "mylib__buf_2": 4.0,
            "mylib__buf_4": 3.0,
            "mylib__buf_8": 2.0,
        },
    )

    report = restructure_for_timing(
        str(netlist_path),
        str(liberty_path),
        "top",
        2.5,
        output_netlist_path=str(output_path),
    )

    assert report["converged"] is True
    assert report["iterations_used"] == 3
    assert [r["to_cell"] for r in report["resizes_applied"]] == [
        "mylib__buf_2",
        "mylib__buf_4",
        "mylib__buf_8",
    ]
    assert report["final_worst_path_delay_ns"] == 2.0


def test_gives_up_when_max_iterations_reached(tmp_path, monkeypatch):
    """Bounded iteration (acceptance criterion 4): even though further
    candidates exist, the loop stops at `max_iterations` and reports it
    never converged, rather than looping until candidates are exhausted."""
    netlist_path = tmp_path / "top.v"
    netlist_path.write_text(_NETLIST_TEXT, encoding="utf-8")
    liberty_path = tmp_path / "test.lib"
    liberty_path.write_text(_LIBERTY_TEXT, encoding="utf-8")
    output_path = tmp_path / "top_restructured.v"

    _install_fake_compute(
        monkeypatch,
        {
            "mylib__buf_1": 5.0,
            "mylib__buf_2": 4.0,
            "mylib__buf_4": 3.0,
            "mylib__buf_8": 2.0,
        },
    )

    report = restructure_for_timing(
        str(netlist_path),
        str(liberty_path),
        "top",
        2.5,
        output_netlist_path=str(output_path),
        max_iterations=2,
    )

    assert report["converged"] is False
    assert report["iterations_used"] == 2
    assert report["gave_up_reason"] == (
        "reached max_iterations (2) without meeting the target period"
    )
    assert [r["to_cell"] for r in report["resizes_applied"]] == [
        "mylib__buf_2",
        "mylib__buf_4",
    ]
    assert report["final_worst_path_delay_ns"] == 3.0
    # A partial improvement is still worth handing back -- the file exists
    # even though the target was not met.
    assert output_path.exists()


def test_gives_up_when_no_candidate_available(tmp_path, monkeypatch):
    """Every cell on the path is already at its library's largest drive
    strength -- the loop gives up cleanly instead of erroring."""
    netlist_path = tmp_path / "top.v"
    netlist_path.write_text(
        _NETLIST_TEXT.replace("mylib__buf_1", "mylib__buf_8"), encoding="utf-8"
    )
    liberty_path = tmp_path / "test.lib"
    liberty_path.write_text(_LIBERTY_TEXT, encoding="utf-8")
    output_path = tmp_path / "top_restructured.v"

    _install_fake_compute(monkeypatch, {"mylib__buf_8": 5.0})

    report = restructure_for_timing(
        str(netlist_path),
        str(liberty_path),
        "top",
        2.5,
        output_netlist_path=str(output_path),
    )

    assert report["converged"] is False
    assert report["iterations_used"] == 1
    assert report["gave_up_reason"] == (
        "no upsize candidate is available for any cell on the worst path"
    )
    assert report["resizes_applied"] == []
    assert report["restructured_netlist_path"] is None
    assert not output_path.exists()


def test_gives_up_on_non_improving_candidate(tmp_path, monkeypatch):
    """A resize that does not actually reduce the worst-path delay (e.g. the
    critical path runs through a different, unresized cell) ends the loop
    rather than thrashing on a change that does not help."""
    netlist_path = tmp_path / "top.v"
    netlist_path.write_text(_NETLIST_TEXT, encoding="utf-8")
    liberty_path = tmp_path / "test.lib"
    liberty_path.write_text(_LIBERTY_TEXT, encoding="utf-8")
    output_path = tmp_path / "top_restructured.v"

    # buf_2 is no faster than buf_1 in this fake -- the resize should not be
    # kept.
    _install_fake_compute(monkeypatch, {"mylib__buf_1": 5.0, "mylib__buf_2": 5.0})

    report = restructure_for_timing(
        str(netlist_path),
        str(liberty_path),
        "top",
        2.5,
        output_netlist_path=str(output_path),
    )

    assert report["converged"] is False
    assert report["resizes_applied"] == []
    assert "did not reduce the worst-path delay" in report["gave_up_reason"]
    assert report["restructured_netlist_path"] is None


def test_unreadable_netlist_raises_restructure_error(tmp_path):
    liberty_path = tmp_path / "test.lib"
    liberty_path.write_text(_LIBERTY_TEXT, encoding="utf-8")

    with pytest.raises(RestructureError, match="could not read netlist"):
        restructure_for_timing(
            str(tmp_path / "nonexistent.v"),
            str(liberty_path),
            "top",
            2.0,
            output_netlist_path=str(tmp_path / "out.v"),
        )


def test_unreadable_liberty_raises_restructure_error(tmp_path):
    netlist_path = tmp_path / "top.v"
    netlist_path.write_text(_NETLIST_TEXT, encoding="utf-8")

    with pytest.raises(RestructureError, match="could not read liberty"):
        restructure_for_timing(
            str(netlist_path),
            str(tmp_path / "nonexistent.lib"),
            "top",
            2.0,
            output_netlist_path=str(tmp_path / "out.v"),
        )


def test_sta_error_on_starting_netlist_raises_restructure_error(tmp_path, monkeypatch):
    netlist_path = tmp_path / "top.v"
    netlist_path.write_text(_NETLIST_TEXT, encoding="utf-8")
    liberty_path = tmp_path / "test.lib"
    liberty_path.write_text(_LIBERTY_TEXT, encoding="utf-8")

    def _raise(*args, **kwargs):
        raise StaError("boom")

    monkeypatch.setattr(restructure_module, "compute_critical_path", _raise)

    with pytest.raises(RestructureError, match="could not analyze starting netlist"):
        restructure_for_timing(
            str(netlist_path),
            str(liberty_path),
            "top",
            2.0,
            output_netlist_path=str(tmp_path / "out.v"),
        )


def test_default_max_iterations_is_a_positive_bound():
    """Sanity check that the module-level default is a real, finite cap --
    not `0`/negative (which would mean the loop can never even try) and not
    something absurdly large that defeats the "bounded" acceptance
    criterion."""
    assert 0 < DEFAULT_MAX_ITERATIONS <= 100
