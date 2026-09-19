"""Supply identity must come from library declarations, not dangling signals."""

import json

import pytest

from klayout_tools.lvs import run_lvs


def _clock_load_request(tmp_path, *, single_master=False, miswired=False):
    root = tmp_path / "pdk"
    variant = root / "testpdk"
    (variant / "libs.tech").mkdir(parents=True)
    library = variant / "libs.ref" / "mylib"
    (library / "spice").mkdir(parents=True)
    (library / "lef").mkdir()
    (library / "spice" / "mylib.spice").write_text(
        ".subckt inv A Y VGND VPWR\n.ends\n"
        ".subckt buf A X VGND VPWR\n.ends\n"
    )
    lef = library / "lef" / "mylib.lef"
    blocks = []
    for cell, output in (("inv", "Y"), ("buf", "X")):
        pins = [("A", "SIGNAL"), (output, "SIGNAL"), ("VGND", "GROUND"), ("VPWR", "POWER")]
        blocks.append(f"MACRO {cell}\n")
        for pin, use in pins:
            blocks.append(f"  PIN {pin}\n    USE {use} ;\n  END {pin}\n")
        blocks.append(f"END {cell}\n")
    lef.write_text("".join(blocks))

    layout = tmp_path / "layout.spice"
    ground = "bad_ground" if miswired else "VGND"
    buffer_instance = "" if single_master else "Xbuf clk1 out VGND VPWR buf\n"
    layout.write_text(
        ".subckt top clk1 clk2 out VGND VPWR\n"
        "Xload0 clk1 dangling0 VGND VPWR inv\n"
        f"Xload1 clk2 dangling1 {ground} VPWR inv\n"
        f"{buffer_instance}"
        ".ends\n"
        ".subckt inv A Y VGND VPWR\n.ends\n"
        ".subckt buf A X VGND VPWR\n.ends\n"
    )
    reference = tmp_path / "reference.v"
    buffer_reference = "" if single_master else "  buf buffer0 (.A(clk1), .X(out));\n"
    reference.write_text(
        "module top(clk1, clk2, out);\n"
        "  input clk1, clk2;\n  output out;\n"
        "  inv load0 (.A(clk1));\n"
        "  inv load1 (.A(clk2));\n"
        f"{buffer_reference}"
        "endmodule\n"
    )
    return {
        "layout": {"netlist": str(layout), "top": "top"},
        "reference": {
            "netlist": str(reference),
            "top": "top",
            "form": "gate-level-verilog",
            "library": "mylib",
            "pdk": "testpdk",
            "pdk_root": str(root),
        },
    }, lef


@pytest.mark.parametrize("single_master", [False, True])
def test_dangling_outputs_are_not_supplies(tmp_path, single_master):
    request, _ = _clock_load_request(tmp_path, single_master=single_master)
    power = run_lvs(json.dumps(request))["power_connectivity"]
    assert power["status"] == "match"
    assert power["power_pins"] == ["VGND", "VPWR"]
    assert power["instance_count"] == (2 if single_master else 3)
    assert power["findings"] == []


def test_dangling_outputs_do_not_hide_real_supply_miswires(tmp_path):
    request, _ = _clock_load_request(tmp_path, miswired=True)
    power = run_lvs(json.dumps(request))["power_connectivity"]
    assert power["status"] == "mismatch"
    assert power["power_pins"] == ["VGND", "VPWR"]
    assert {finding["pin"] for finding in power["findings"]} == {"VGND"}


@pytest.mark.parametrize("missing", ["file", "output_pin"])
def test_incomplete_library_pin_roles_are_disclosed(tmp_path, missing):
    request, lef = _clock_load_request(tmp_path, single_master=True)
    if missing == "file":
        lef.unlink()
    else:
        lef.write_text(lef.read_text().replace("  PIN Y\n    USE SIGNAL ;\n  END Y\n", ""))
    power = run_lvs(json.dumps(request))["power_connectivity"]
    assert power["status"] == "unchecked"
    assert power["power_pins"] == []
    assert "LEF" in power["reason"]
