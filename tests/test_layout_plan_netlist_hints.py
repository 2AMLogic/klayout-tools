"""Layout-plan netlist-ingestion hints name ``netlist.*`` keys (#5135)."""

from __future__ import annotations

import pytest

from klayout_tools.layout_plan import (
    REQUEST_SCHEMA,
    LayoutPlanError,
    validate_layout_plan_document,
)

_SIM_FORM_NETLIST = """
.subckt analog_block A Y GND VPWR VGND
XM1 Y A VGND VGND sky130_fd_pr__nfet_01v8 L=0.15u W=0.65u
.ends
"""


def test_sim_form_netlist_error_names_netlist_keys_not_reference(tmp_path):
    # issue #5135: the shared reader's hint names `reference.form`, which a
    # layout-plan request does not have; the plan path must say `netlist.form`.
    (tmp_path / "sim.spice").write_text(_SIM_FORM_NETLIST)
    plan = {
        "schema": REQUEST_SCHEMA,
        "netlist": {"path": "sim.spice", "top": "analog_block"},
        "device_groups": [],
    }

    with pytest.raises(LayoutPlanError) as excinfo:
        validate_layout_plan_document(plan, request_dir=str(tmp_path))
    message = str(excinfo.value)
    assert "request.netlist.form" in message
    assert "reference.form" not in message

    plan["netlist"].update({"form": "subckt-call", "deck": "sky130"})
    result = validate_layout_plan_document(plan, request_dir=str(tmp_path))
    assert result["valid"] is True


def test_lvs_reference_parse_hint_unchanged():
    from klayout_tools.lvs import _reference_parse_message

    exc = Exception("Can't find a value for a R, C or L device")
    message = _reference_parse_message("x.spice", exc)
    assert "reference.form" in message and "reference.deck" in message


def test_execute_path_sim_form_netlist_error_names_netlist_keys(tmp_path):
    # issue #5135 reports `klt layout-plan execute`: the execute wrapper must
    # share the validate path's `netlist.*` hint rewrite.
    from klayout_tools.layout_plan_execute import execute_layout_plan_document

    (tmp_path / "sim.spice").write_text(_SIM_FORM_NETLIST)
    plan = {
        "schema": REQUEST_SCHEMA,
        "netlist": {"path": "sim.spice", "top": "analog_block"},
        "device_groups": [],
    }

    with pytest.raises(LayoutPlanError) as excinfo:
        execute_layout_plan_document(plan, request_dir=str(tmp_path))
    message = str(excinfo.value)
    assert "request.netlist.form" in message
    assert "reference.form" not in message
