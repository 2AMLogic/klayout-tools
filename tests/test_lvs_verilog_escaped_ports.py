"""Bit-blasted scalar port names in gate-level Verilog references (issue #5693)."""

import json

import pytest

from klayout_tools.lvs import run_lvs
from klayout_tools.verilog_netlist import VerilogNetlistError, parse_gate_level_verilog
from test_lvs import _make_fake_pdk_library

_CELLS = """.SUBCKT sky130_fd_sc_hd__inv_2 A Y
.ENDS
.SUBCKT sky130_fd_sc_hd__buf_2 A X
.ENDS
"""
_LAYOUT = (
    _CELLS
    + """.SUBCKT probe I N1 N2
X_u1 I net_1 sky130_fd_sc_hd__inv_2
X_u2 net_1 N1 sky130_fd_sc_hd__inv_2
X_u3 net_1 N2 sky130_fd_sc_hd__buf_2
.ENDS
"""
)
_REFERENCE = r"""
module probe(I, \O[0] , O1);
  input I;
  output \O[0] , O1;
  wire net_1;
  sky130_fd_sc_hd__inv_2 u1 (.A(I), .Y(net_1));
  sky130_fd_sc_hd__inv_2 u2 (.A(net_1), .Y(\O[0] ));
  sky130_fd_sc_hd__buf_2 u3 (.A(net_1), .X(O1));
endmodule
"""


def test_escaped_scalar_port_keeps_name_and_connection():
    (module,) = parse_gate_level_verilog(_REFERENCE)
    assert module["ports"] == ["I", "O[0]", "O1"]
    assert module["instances"][1]["connections"]["Y"] == "O[0]"


@pytest.mark.parametrize("spelling", [r"\O[0],pad", r"\lane["])
def test_escaped_punctuation_does_not_split_ports(spelling):
    text = f"module probe({spelling} , tail); output {spelling} , tail; endmodule"
    assert parse_gate_level_verilog(text)[0]["ports"] == [spelling[1:], "tail"]


@pytest.mark.parametrize("direction", ["input", "output", "inout"])
def test_escaped_port_and_real_bus_keep_their_widths(direction):
    text = rf"""module probe(\lane[3].pad , data);
        {direction} \lane[3].pad ;
        input [1:0] data;
        endmodule"""
    assert parse_gate_level_verilog(text)[0]["ports"] == [
        "lane[3].pad",
        "data[1]",
        "data[0]",
    ]


@pytest.mark.parametrize("in_header", [False, True])
def test_unescaped_bit_select_explains_declaration(in_header):
    header = "O[0]" if in_header else r"\O[0] "
    with pytest.raises(VerilogNetlistError) as exc:
        parse_gate_level_verilog(f"module probe({header}); output O[0]; endmodule")
    assert "O[0]" in str(exc.value) and "escape" in str(exc.value).lower()
    assert "ANSI" not in str(exc.value)


@pytest.mark.parametrize("shorted", [False, True], ids=["match", "shorted-outputs"])
def test_escaped_reference_runs_through_lvs(tmp_path, shorted):
    library = "sky130_fd_sc_hd"
    root = _make_fake_pdk_library(tmp_path, "sky130A", library, _CELLS)
    layout = tmp_path / "layout.spice"
    layout.write_text(_LAYOUT.replace("net_1 N2", "net_1 N1") if shorted else _LAYOUT)
    ref = tmp_path / "ref.v"
    ref.write_text(_REFERENCE)
    request = {
        "layout": {"netlist": str(layout), "top": "probe"},
        "reference": {
            "netlist": str(ref),
            "top": "probe",
            "form": "gate-level-verilog",
            "pdk": "sky130A",
            "pdk_root": root,
            "library": library,
        },
    }
    report = run_lvs(json.dumps(request))
    assert report["status"] == ("mismatch" if shorted else "match")
    assert (report["error_count"] > 0) == shorted
