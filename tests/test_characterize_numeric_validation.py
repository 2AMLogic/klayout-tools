"""Non-finite numbers in a ``klt characterize`` request are refused (issue #7812).

``json.loads`` accepts the bare literals ``NaN`` / ``Infinity`` / ``-Infinity``
and decodes an arbitrarily long integer literal into an ``int`` too large for a
``float``. Every one-sided range guard in the request validators is a
comparison, and every comparison against NaN is ``False``, so a NaN sailed
through ``supply_v <= 0`` and friends into the generated SPICE deck; an
infinity did the same; and a 400-digit integer escaped as an uncaught
``OverflowError`` from ``float(...)`` instead of the JSON error envelope.

These tests need no simulator: every case is refused during request
validation, before a deck is built. ``_never_simulate`` makes that a
guarantee -- a validator that lets a bad value through fails the test rather
than launching a simulation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import characterize, liberty_writer
from klayout_tools import characterize_constraints as cc
from klayout_tools.cli import main

NONFINITE = [
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="inf"),
    pytest.param(float("-inf"), id="-inf"),
    pytest.param(10**400, id="huge-int"),
]


@pytest.fixture(autouse=True)
def _never_simulate(monkeypatch):
    def refuse(**_kwargs):
        raise AssertionError("a non-finite request value reached simulation")

    monkeypatch.setattr(characterize, "_characterize_cell", refuse)


def _request(tmp_path: Path) -> dict:
    netlist = tmp_path / "cells.spice"
    netlist.write_text(
        ".subckt inv_demo Y A VDD VSS\n"
        "MN Y A VSS VSS nfet_demo w=1u l=0.15u\n"
        "MP Y A VDD VDD pfet_demo w=2u l=0.15u\n"
        ".ends inv_demo\n"
    )
    return {
        "cell": {
            "name": "inv_demo",
            "netlist": str(netlist),
            "pins": [
                {"name": "A", "direction": "input", "capacitance_pf": 0.002},
                {"name": "Y", "direction": "output", "function": "!A"},
            ],
            "power_pins": {"vdd": "VDD", "gnd": "VSS"},
        },
        "corner": {"name": "tt", "supply_v": 1.8, "temperature_c": 25},
        "grid": {"input_transition_ns": [0.02, 0.08], "output_load_pf": [0.005, 0.03]},
    }


def _run(tmp_path: Path, request: dict, **kwargs) -> dict:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return characterize.run_characterize(str(path), **kwargs)


def _refused(tmp_path: Path, request: dict, match: str, **kwargs) -> None:
    with pytest.raises(characterize.CharacterizeError, match=match):
        _run(tmp_path, request, **kwargs)


# --------------------------------------------------------------------------- #
# characterize.py: the individual validators
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", NONFINITE)
@pytest.mark.parametrize("field", ["supply_v", "temperature_c", "nom_process"])
def test_corner_refuses_a_nonfinite_number(field, bad):
    corner = {"name": "tt", "supply_v": 1.8, "temperature_c": 25, "nom_process": 1.0}
    corner[field] = bad
    with pytest.raises(characterize.CharacterizeError, match=rf"corner\.{field}"):
        characterize._resolve_corner({"corner": corner})


def test_corner_still_accepts_finite_values_including_zero_and_negative():
    corner = characterize._resolve_corner(
        {
            "corner": {
                "name": "ff",
                "supply_v": 1,
                "temperature_c": -40,
                "nom_process": 0,
            }
        }
    )
    assert corner["supply_v"] == 1.0
    assert corner["temperature_c"] == -40.0
    assert corner["nom_process"] == 0.0
    corner = characterize._resolve_corner(
        {"corner": {"name": "tt", "supply_v": 1.8, "temperature_c": 0}}
    )
    assert corner["temperature_c"] == 0.0
    assert corner["nom_process"] == 1.0


@pytest.mark.parametrize("bad", NONFINITE)
@pytest.mark.parametrize("field", list(liberty_writer.Thresholds.__dataclass_fields__))
def test_thresholds_refuse_a_nonfinite_number(field, bad):
    with pytest.raises(
        characterize.CharacterizeError, match=rf"thresholds\.{field} must be a number"
    ):
        characterize._resolve_thresholds({"thresholds": {field: bad}})


def test_thresholds_still_accept_finite_overrides():
    thresholds = characterize._resolve_thresholds(
        {"thresholds": {"input_threshold_pct_rise": 40, "slew_derate_from_library": 2}}
    )
    assert thresholds.input_threshold_pct_rise == 40.0
    assert thresholds.slew_derate_from_library == 2.0


@pytest.mark.parametrize("bad", NONFINITE)
@pytest.mark.parametrize("position", [0, 1, 2])
def test_axis_refuses_a_nonfinite_entry(position, bad):
    values = [0.01, 0.02, 0.03]
    values[position] = bad
    with pytest.raises(
        characterize.CharacterizeError,
        match=rf"request\.grid\.x\[{position}\] must be a number",
    ):
        characterize._resolve_axis(values, "request.grid.x")


def test_axis_still_accepts_a_finite_ascending_list():
    assert characterize._resolve_axis([1, 2.5, 3], "request.grid.x") == (1.0, 2.5, 3.0)


@pytest.mark.parametrize("bad", NONFINITE)
def test_positive_number_and_optional_positive_refuse_a_nonfinite_number(bad):
    with pytest.raises(characterize.CharacterizeError, match=r"my\.field must be a"):
        characterize._positive_number(bad, "my.field")
    with pytest.raises(characterize.CharacterizeError, match=r"my\.field must be a"):
        characterize._optional_positive(bad, "my.field")


def test_positive_number_and_optional_positive_still_accept_finite_values():
    assert characterize._positive_number(3, "my.field") == 3.0
    assert characterize._optional_positive(0.5, "my.field") == 0.5
    assert characterize._optional_positive(None, "my.field") is None


# --------------------------------------------------------------------------- #
# characterize_constraints.py: ``resolve_constraint_options``
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", NONFINITE)
@pytest.mark.parametrize("key", ["constraint_degradation", "constraint_tolerance_ns"])
def test_constraint_scalars_refuse_a_nonfinite_number(key, bad):
    with pytest.raises(cc.ConstraintError, match=key):
        cc.resolve_constraint_options({key: bad})


@pytest.mark.parametrize("bad", NONFINITE)
@pytest.mark.parametrize("slot", [0, 1])
def test_constraint_window_refuses_a_nonfinite_bound(slot, bad):
    window = [-1.0, 2.0]
    window[slot] = bad
    with pytest.raises(cc.ConstraintError, match="constraint_window_ns"):
        cc.resolve_constraint_options({"constraint_window_ns": window})


@pytest.mark.parametrize(
    "window",
    [
        pytest.param([float("-inf"), float("inf")], id="infinite-both-sides"),
        pytest.param([-(10**400), 10**400], id="huge-int-both-sides"),
    ],
)
def test_constraint_window_refuses_a_window_that_only_looks_two_sided(window):
    """``low < 0 < high`` holds for these, so the shape check alone accepted
    them (and the huge ints then escaped as ``OverflowError``)."""
    with pytest.raises(cc.ConstraintError, match="constraint_window_ns"):
        cc.resolve_constraint_options({"constraint_window_ns": window})


def test_constraint_options_still_accept_finite_values():
    options = cc.resolve_constraint_options(
        {
            "constraint_degradation": 1,
            "constraint_window_ns": [-3, 4],
            "constraint_tolerance_ns": 1,
        }
    )
    assert options == {
        "degradation": 1.0,
        "window_ns": (-3.0, 4.0),
        "tolerance_ns": 1.0,
    }


# --------------------------------------------------------------------------- #
# Through ``run_characterize`` (the request file / ``--timeout-s`` entry)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", NONFINITE)
@pytest.mark.parametrize(
    "option", ["settle_ns", "tran_step_ns", "timeout_s", "constraint_tolerance_ns"]
)
def test_run_refuses_a_nonfinite_option(tmp_path, option, bad):
    request = _request(tmp_path)
    request["options"] = {option: bad}
    _refused(tmp_path, request, rf"request\.options\.{option}")


@pytest.mark.parametrize("bad", NONFINITE)
def test_run_refuses_a_nonfinite_constraint_window(tmp_path, bad):
    request = _request(tmp_path)
    request["options"] = {"constraint_window_ns": [bad, 1.0]}
    _refused(tmp_path, request, r"request\.options\.constraint_window_ns")


@pytest.mark.parametrize("bad", NONFINITE)
def test_run_refuses_a_nonfinite_timeout_flag(tmp_path, bad):
    _refused(
        tmp_path, _request(tmp_path), r"--timeout-s must be a number", timeout_s=bad
    )


@pytest.mark.parametrize("bad", NONFINITE)
@pytest.mark.parametrize("axis", ["input_transition_ns", "output_load_pf"])
def test_run_refuses_a_nonfinite_grid_value(tmp_path, axis, bad):
    request = _request(tmp_path)
    request["grid"][axis] = [0.01, bad]
    _refused(tmp_path, request, rf"request\.grid\.{axis}\[1\] must be a number")


@pytest.mark.parametrize("bad", NONFINITE)
def test_run_refuses_a_nonfinite_pin_capacitance(tmp_path, bad):
    request = _request(tmp_path)
    request["cell"]["pins"][0]["capacitance_pf"] = bad
    _refused(tmp_path, request, r"pins\[0\]\.capacitance_pf must be a number")


@pytest.mark.parametrize(
    "literal", ["NaN", "Infinity", "-Infinity", "1" + "0" * 400], ids=lambda s: s[:9]
)
def test_request_file_literal_is_refused_with_the_error_envelope(
    tmp_path, capsys, literal
):
    """The literal is spliced into the file text, so this is exactly what an
    agent's hand-written request carries -- not a Python float."""
    request = _request(tmp_path)
    request["corner"]["temperature_c"] = "@@"
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request).replace('"@@"', literal))
    assert literal in path.read_text()

    exit_code = main(["characterize", str(path), "--format", "json"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["error"]["command"] == "characterize"
    assert "request.corner.temperature_c" in error["error"]["message"]
