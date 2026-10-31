"""Tests for the shared Verilog top-level parameter override helper
(issue #3466): script rendering, and the `klt equiv` side that consumes it."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from klayout_tools import equiv
from klayout_tools._verilog_params import (
    chparam_lines,
    read_verilog_line,
    validate_parameters,
)


def test_chparam_lines_empty_when_no_parameters():
    assert chparam_lines({}, "top") == []


def test_chparam_lines_renders_ints_and_quoted_strings():
    assert chparam_lines({"W": 8, "M": "fast", "N": -2}, "top") == [
        'chparam -set W 8 -set M "fast" -set N -2 top'
    ]


def test_validate_parameters_none_is_empty():
    assert validate_parameters(None, ValueError, "p") == {}


def test_equiv_side_validates_and_emits_parameters(tmp_path):
    rtl = tmp_path / "t.v"
    rtl.write_text("module t #(parameter W=4)(input [W-1:0] a); endmodule\n")
    side = equiv._resolve_side(
        {"sources": [str(rtl)], "top": "t", "parameters": {"W": 8}},
        str(tmp_path),
        "gold",
    )
    lines = equiv._side_prep_lines("gold", side, {})
    assert lines.index("chparam -set W 8 t") < lines.index("hierarchy -check -top t")

    with pytest.raises(equiv.EquivError, match="request.gold.parameters"):
        equiv._resolve_side(
            {"sources": [str(rtl)], "top": "t", "parameters": {"W": 1.5}},
            str(tmp_path),
            "gold",
        )


@pytest.mark.skipif(shutil.which("yosys") is None, reason="yosys not installed")
def test_chparam_script_changes_elaboration_in_real_yosys(tmp_path):
    rtl = tmp_path / "t.v"
    rtl.write_text(
        'module t #(parameter W=4, parameter M="a")'
        "(input [W-1:0] a, output [W-1:0] y);\n"
        "  assign y = a;\nendmodule\n"
    )

    def wire_bits(parameters):
        script = tmp_path / "s.ys"
        out = tmp_path / "stat.txt"
        lines = [f"read_verilog {rtl}", *chparam_lines(parameters, "t")]
        lines += ["hierarchy -check -top t", "proc", f"tee -q -o {out} stat"]
        script.write_text("\n".join(lines) + "\n")
        subprocess.run(["yosys", "-q", "-s", str(script)], check=True)
        return next(
            int(ln.split()[0])
            for ln in out.read_text().splitlines()
            if "wire bits" in ln
        )

    assert wire_bits({}) < wire_bits({"W": 16, "M": "b"})


@pytest.mark.parametrize(
    "path, expected",
    [
        ("a.sv", "read_verilog -sv a.sv"),
        ("dir/A.SV", "read_verilog -sv dir/A.SV"),
        ("defs.svh", "read_verilog -sv defs.svh"),
        ("a.v", "read_verilog a.v"),
        ("noext", "read_verilog noext"),
    ],
)
def test_read_verilog_line_picks_front_end_by_extension(path, expected):
    assert read_verilog_line(path) == expected


def test_equiv_side_reads_sv_with_sv_front_end_and_v_plain(tmp_path):
    sv = tmp_path / "t.sv"
    v = tmp_path / "u.v"
    sv.write_text("module t(input logic a); endmodule\n")
    v.write_text("module u(input a); endmodule\n")
    side = equiv._resolve_side(
        {"sources": [str(sv), str(v)], "top": "t"}, str(tmp_path), "gold"
    )
    lines = equiv._side_prep_lines("gold", side, {})
    assert f"read_verilog -sv {sv}" in lines
    assert f"read_verilog {v}" in lines


@pytest.mark.skipif(shutil.which("yosys") is None, reason="yosys not installed")
def test_sv_source_parses_in_real_yosys_with_sv_front_end(tmp_path):
    rtl = tmp_path / "c.sv"
    rtl.write_text(
        "module c(\n  input  logic clk,\n  input  logic d,\n"
        "  output logic q\n);\n  always_ff @(posedge clk) q <= d;\nendmodule\n"
    )
    script = tmp_path / "s.ys"
    script.write_text(f"{read_verilog_line(rtl)}\nhierarchy -check -top c\n")
    subprocess.run(["yosys", "-q", "-s", str(script)], check=True)


def test_chparam_lines_module_qualified_keys():
    assert chparam_lines({"W": 8, "sub.F": "i.hex", "sub.G": 2, "a.X": 1}, "top") == [
        "chparam -set W 8 top",
        "select -assert-any $abstract\\a",
        "chparam -set X 1 a",
        "select -assert-any $abstract\\sub",
        'chparam -set F "i.hex" -set G 2 sub',
    ]


def test_validate_parameters_accepts_qualified_and_rejects_deeper_keys():
    assert validate_parameters({"sub.F": 1}, ValueError, "p") == {"sub.F": 1}
    for bad in ("a.b.c", ".F", "sub.", "sub .F"):
        with pytest.raises(ValueError, match="module-qualified"):
            validate_parameters({bad: 1}, ValueError, "p")


def test_read_verilog_line_defers_only_for_qualified_parameters():
    assert read_verilog_line("t.sv", None, {"W": 1}) == "read_verilog -sv t.sv"
    assert (
        read_verilog_line("t.sv", None, {"sub.F": 1}) == "read_verilog -sv -defer t.sv"
    )


@pytest.mark.skipif(shutil.which("yosys") is None, reason="needs yosys")
def test_submodule_parameter_reaches_readmemh_in_real_yosys(tmp_path):
    (tmp_path / "t.sv").write_text(
        'module sub #(parameter F = "") (input logic [1:0] a, output logic [7:0] y);\n'
        "  logic [7:0] m [0:3];\n  initial $readmemh(F, m);\n  assign y = m[a];\n"
        "endmodule\n"
        "module top (input logic [1:0] a, output logic [7:0] y);\n"
        "  sub u(.a(a), .y(y));\nendmodule\n"
    )
    (tmp_path / "i.hex").write_text("01\n02\n03\n04\n")

    def run(parameters):
        script = [read_verilog_line("t.sv", None, parameters)]
        script += chparam_lines(parameters, "top") + ["hierarchy -check -top top"]
        return subprocess.run(
            ["yosys", "-q", "-p", "; ".join(script)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

    assert run({"sub.F": "i.hex"}).returncode == 0
    missing = run({"nosuch.F": "i.hex"})
    assert missing.returncode != 0
    assert "selection is empty: $abstract\\nosuch" in missing.stdout + missing.stderr
