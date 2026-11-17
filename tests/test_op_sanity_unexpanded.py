"""Measuring the `--op-lint` hierarchy hole: `unsupported_hierarchy`
wording and the `unexpanded_instance_count` field (issue #5644).

An `X` instance whose `.subckt` lives only in the `models.lib` closure --
the normal shape of an open PDK's non-MOS primitives -- cannot be expanded
by the hierarchy walk, so the lint reports one warning per instance and a
top-level count, keeping coverage measurable without parsing
`diagnostics[]`. Split out of test_op_sanity.py for the file-size
ratchet."""

from __future__ import annotations

import pytest

from klayout_tools import op_sanity
from test_op_sanity import (
    _GOOD_NETLIST,
    _SATURATED_NMOS,
    _SATURATED_PMOS,
    _install_fake_ngspice,
    _write_request,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

#: Two instances of one PDK resistor subckt that nothing in the netlist
#: body (or an `.include`d file) defines -- only a model library would.
_RESISTOR_NETLIST = (
    "Vdd vdd 0 DC 1.8\nXr1 vdd mid demo__res_high_po\nXr2 mid 0 demo__res_high_po\n"
)

#: The pre-#5644 wording, kept verbatim whenever no `models.lib` was
#: resolved -- the message must not claim a model library was searched.
_ORIGINAL_WORDING = (
    "could not expand subcircuit instance {name}: its definition was not "
    "found in the netlist or its `.include`d files and it is not a "
    "recognised MOS model, so any devices inside it were not linted."
)


def _unsupported_hierarchy(report: dict) -> list[dict]:
    return [
        entry
        for entry in report["diagnostics"]
        if entry["code"] == "unsupported_hierarchy"
    ]


def test_model_library_instances_are_counted_and_named(tmp_path, monkeypatch):
    (tmp_path / "models.lib").write_text("* PDK model library\n")
    request = _write_request(
        tmp_path,
        _RESISTOR_NETLIST,
        models={"lib": "models.lib"},
        corners={"process": ["tt"], "supply_v": {"vdd": [1.8]}, "temperature_c": [27]},
    )
    _install_fake_ngspice(monkeypatch, {})

    report = op_sanity.run_op_sanity(str(request))

    # One warning per instance -- two of the *same* missing subckt count
    # as two, and the count is measurable without parsing diagnostics.
    assert report["unexpanded_instance_count"] == 2
    warnings = _unsupported_hierarchy(report)
    assert len(warnings) == 2
    for entry, instance in zip(warnings, ["Xr1", "Xr2"], strict=True):
        assert instance in entry["message"]
        assert "model library" in entry["message"]


def test_without_models_lib_the_wording_is_unchanged(tmp_path, monkeypatch):
    request = _write_request(tmp_path, _RESISTOR_NETLIST)
    _install_fake_ngspice(monkeypatch, {})

    report = op_sanity.run_op_sanity(str(request))

    assert report["unexpanded_instance_count"] == 2
    warnings = _unsupported_hierarchy(report)
    assert len(warnings) == 2
    for entry, instance in zip(warnings, ["Xr1", "Xr2"], strict=True):
        assert entry["message"] == _ORIGINAL_WORDING.format(name=instance)


def test_fully_expanded_netlist_reports_zero(tmp_path, monkeypatch):
    request = _write_request(tmp_path, _GOOD_NETLIST)
    _install_fake_ngspice(monkeypatch, {"XM1": _SATURATED_NMOS, "XM2": _SATURATED_PMOS})

    report = op_sanity.run_op_sanity(str(request))

    assert report["unexpanded_instance_count"] == 0
    assert report["status"] == "clean"


def test_corner_all_count_describes_the_netlist_not_a_corner(tmp_path, monkeypatch):
    (tmp_path / "models.lib").write_text("* PDK model library\n")
    request = _write_request(
        tmp_path,
        _RESISTOR_NETLIST,
        models={"lib": "models.lib"},
        corners={
            "process": ["tt", "ss"],
            "supply_v": {"vdd": [1.8]},
            "temperature_c": [27],
        },
    )
    _install_fake_ngspice(monkeypatch, {})

    report = op_sanity.run_op_sanity(str(request), corner="all")

    assert report["unexpanded_instance_count"] == 2
    assert len(report["corner_margins"]) == 2
