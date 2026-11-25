"""Issue #7831: SDF instance checks on a structural netlist with bus assignments."""

from pathlib import Path

import pytest

from klayout_tools.functional_verification_base import FunctionalVerificationError
from klayout_tools.functional_verification_sdf import _find_gate_level_instances
from klayout_tools.functional_verification_sdf_instance import (
    _reject_sdf_unresolvable_scopes,
)

_NETLIST = r"""
module top(a, y);
  input [3:0] a;
  output [3:0] y;
  wire buf_z;
  wire \name;with;semicolons ;
  assign \name;with;semicolons = buf_z; /* assign ignored; */
  assign y[3:0] = {a[3], a[2], a[1], \name;with;semicolons };
  buf_cell u1 (.A(a[0]), .Z(buf_z));
endmodule
"""
_WRAPPER = (
    "module bench(a, y); input [3:0] a; output [3:0] y; "
    "top dut (.a(a), .y(y)); endmodule"
)


def _check(
    tmp_path: Path, netlist: str, cell: str, endpoint: str, scope: str | None = None
):
    source = tmp_path / "top.v"
    source.write_text(netlist + "\n" + _WRAPPER)
    sdf = tmp_path / "timing.sdf"
    sdf.write_text(
        "(DELAYFILE (DIVIDER /)\n"
        f'(CELL (CELLTYPE "top") (INSTANCE {cell})\n'
        f"(DELAY (ABSOLUTE (INTERCONNECT {endpoint}/Z y (0.1:0.1:0.1))))))\n"
    )
    _reject_sdf_unresolvable_scopes(
        str(sdf), [str(source)], "bench" if scope else "top", scope
    )


def test_inventory_survives_part_select_assignment():
    instances = _find_gate_level_instances([_NETLIST], "top")
    assert [(item["cell"], item["name"]) for item in instances] == [("buf_cell", "u1")]


@pytest.mark.parametrize("scope", [None, "dut"])
def test_present_endpoint_and_cell(tmp_path, scope):
    _check(tmp_path, _NETLIST, "", "u1", scope)
    _check(tmp_path, _NETLIST, "u1", "u1", scope)


@pytest.mark.parametrize("scope", [None, "dut"])
@pytest.mark.parametrize(
    ("cell", "endpoint", "diagnostic"),
    [
        ("", "missing", "missing/Z"),
        ("missing", "u1", r"\(INSTANCE missing\)"),
    ],
)
def test_missing_endpoint_or_cell_has_location(
    tmp_path, cell, endpoint, diagnostic, scope
):
    with pytest.raises(FunctionalVerificationError, match=rf"line 3: {diagnostic}"):
        _check(tmp_path, _NETLIST, cell, endpoint, scope)


@pytest.mark.parametrize(
    "netlist",
    [
        _NETLIST.replace("top(a, y)", "top(input [3:0] a, output [3:0] y)"),
        _NETLIST.replace("endmodule", "always @(a) y <= a;\nendmodule"),
    ],
)
def test_unreadable_module_remains_a_noop(tmp_path, netlist):
    assert _find_gate_level_instances([netlist], "top") == []
    _check(tmp_path, netlist, "", "missing")


def test_diagnostic_groups_repeated_endpoint_references_by_instance(tmp_path):
    source = tmp_path / "top.v"
    source.write_text(_NETLIST)
    sdf = tmp_path / "timing.sdf"
    sdf.write_text(
        '(DELAYFILE (DIVIDER /) (CELL (CELLTYPE "top") (INSTANCE) (DELAY (ABSOLUTE\n'
        "(INTERCONNECT absent/Z other/A (0.1:0.1:0.1))\n"
        "(INTERCONNECT absent/Z y (0.1:0.1:0.1))))))\n"
    )
    with pytest.raises(FunctionalVerificationError) as error:
        _reject_sdf_unresolvable_scopes(str(sdf), [str(source)], "top")
    assert "line 2: absent/Z" in str(error.value)
    assert "line 2: other/A" in str(error.value)
    assert "3 unresolvable endpoint(s)" in str(error.value)
    assert "2 distinct instance path(s)" in str(error.value)
