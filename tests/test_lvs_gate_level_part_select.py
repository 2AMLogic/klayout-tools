"""Gate-level part selects and reference validation before extraction."""

import hashlib
import json
import re

import pytest

from klayout_tools.lvs import LvsError, run_lvs
from klayout_tools.verilog_netlist import (
    VerilogNetlistError,
    convert_gate_level_verilog,
    parse_gate_level_verilog,
)
from test_lvs import (
    _GATE_LEVEL_LAYOUT_SPICE,
    _GATE_LEVEL_LIBRARY_SPICE,
    _GATE_LEVEL_REFERENCE_VERILOG,
    _assign_aliases,
    _make_fake_pdk_library,
    _make_single_nmos_layout,
    _write,
)


@pytest.mark.parametrize(
    ("assign", "reason"),
    [
        ("assign b = {0{a}};", "positive integer"),
        ("assign b = {N{a}};", "replication or nested"),
        ("assign b = { a, {c };", "found"),
        ("assign b = { a, c}};", "unbalanced"),
        ("assign b = { a, 1'b0 };", "literal"),
        # Issue #7822: a *fixed* part-select of a declared bus is now a
        # legal RHS operand (see the positive tests below) -- what stays
        # rejected is a slice whose bounds are not plain integers (no
        # grammar here resolves it, so it falls through to the plain
        # operand's part-select rejection) and, via `_range_slice_bits`,
        # a slice that misses the declaration: out-of-range/reversed
        # against the declared direction, or an undeclared base.
        ("assign b = { a[x:0], c };", "part-select"),
        ("assign b = { a[2:1], c };", "declared direction"),
        ("assign b = { a[0:1], c };", "declared direction"),
        ("assign b = { q[1:0], c };", "no 'wire'"),
        ("assign b = { a, undeclared };", "no 'wire'"),
        ("assign b = { a, c, c };", "bit\\(s\\) wide"),
        ("assign {b, c} = { a, c };", "left-hand side"),
        ("assign b = {};", "empty"),
        ("assign b = a & c;", "found"),
    ],
)
def test_parse_gate_level_verilog_unsupported_assign_still_rejected(assign, reason):
    """Issue #2372/#4117 handle plain, nested and replicated concatenations;
    literals, width mismatches and general expressions keep failing loudly
    with the "only a plain ... is supported" error -- and, since issue
    #7822, so do malformed or undeclared part-select operands (a *fixed*
    part-select of a declared bus, in its declared direction, is accepted)."""
    text = (
        "module m(a, b);\n  input [1:0] a;\n  output [2:0] b;\n  wire c;\n"
        f"  {assign}\n  cellx u0 (.A(c), .Y(b[0]));\nendmodule\n"
    )
    with pytest.raises(VerilogNetlistError, match="only a plain 'assign") as exc:
        parse_gate_level_verilog(text)
    assert re.search(reason, str(exc.value))


def test_parse_gate_level_verilog_concatenation_assign_part_select_operand():
    """Issue #7822: a fixed part-select is a legal RHS concatenation
    operand -- the reporter's `assign x = { a[7:0], a };` -- expanded
    MSB-first through the bus declaration's own direction."""
    aliases = _assign_aliases(
        "  wire [15:0] a;\n  wire [23:0] x;",
        "assign x = { a[7:0], a };",
    )
    assert [aliases[f"x[{i}]"] for i in range(23, 15, -1)] == [
        f"a[{i}]" for i in range(7, -1, -1)
    ]
    assert [aliases[f"x[{i}]"] for i in range(15, -1, -1)] == [
        f"a[{i}]" for i in range(15, -1, -1)
    ]


def test_parse_gate_level_verilog_concatenation_assign_part_select_ascending():
    """An ascending declaration (`[0:7]`) slices in its declared direction:
    `a[0:3]` supplies `a[0]` as the slice's most-significant bit."""
    aliases = _assign_aliases(
        "  wire [0:7] a;\n  wire [7:0] x;",
        "assign x = { a[0:3], a[4:7] };",
    )
    assert [aliases[f"x[{i}]"] for i in range(7, -1, -1)] == [
        f"a[{i}]" for i in range(0, 8)
    ]


def test_parse_gate_level_verilog_concatenation_assign_part_select_nested():
    """A part-select works inside an already-supported nested or replicated
    RHS (issue #4117's shapes), MSB-first like any other operand."""
    aliases = _assign_aliases(
        "  wire [3:0] a;\n  wire c;\n  wire [4:0] y;\n  wire [3:0] z;",
        "assign y = { { a[3:2] }, c, a[1:0] }; assign z = {2{a[1:0]}};",
    )
    assert [aliases[f"y[{i}]"] for i in (4, 3, 2, 1, 0)] == [
        "a[3]",
        "a[2]",
        "c",
        "a[1]",
        "a[0]",
    ]
    assert [aliases[f"z[{i}]"] for i in (3, 2, 1, 0)] == [
        "a[1]",
        "a[0]",
        "a[1]",
        "a[0]",
    ]


def test_parse_gate_level_verilog_concatenation_assign_part_select_escaped():
    aliases = _assign_aliases(
        "  wire [1:0] \\esc ;\n  wire c;\n  wire [2:0] b;",
        "assign b = { \\esc [1:0], c };",
    )
    assert [aliases[f"b[{i}]"] for i in (2, 1, 0)] == ["esc[1]", "esc[0]", "c"]


_PART_SELECT_CONCAT_VERILOG = """
module m(a, b);
  input [15:0] a;
  output [2:0] b;
  assign b = { a[15:14], a[0] };
  cellx u0 (.A(b[2]), .Y(b[0]));
endmodule
"""


def test_convert_gate_level_verilog_concatenation_part_select_end_to_end():
    out = convert_gate_level_verilog(
        _PART_SELECT_CONCAT_VERILOG, pin_order_lookup={"cellx": ["A", "Y"]}.get
    )
    assert "Xu0 a[15] a[0] cellx" in out


def _gate_level_extraction_request(tmp_path, reference_text: str) -> dict:
    """Issue #7822's request shape: a layout that must be *extracted* (a
    real GDS plus a curated deck, not a pre-extracted `layout.netlist`)
    compared against a gate-level Verilog reference over the hermetic fake
    PDK library."""
    root = _make_fake_pdk_library(
        tmp_path, "myvariant", "mylib", _GATE_LEVEL_LIBRARY_SPICE
    )
    gds_path = tmp_path / "nmos.gds"
    _make_single_nmos_layout().write(str(gds_path))
    reference_path = _write(tmp_path / "ref.v", reference_text)
    return {
        "layout": {"file": str(gds_path), "deck": "sky130"},
        "reference": {
            "netlist": reference_path,
            "top": "top",
            "form": "gate-level-verilog",
            "library": "mylib",
            "pdk": "myvariant",
            "pdk_root": root,
        },
    }


def test_run_lvs_gate_level_verilog_reference_error_precedes_extraction(
    tmp_path, monkeypatch
):
    """Issue #7822: a gate-level reference the converter rejects fails
    *before* layout extraction starts -- the diagnostic names the failing
    reference input, and the extractor is never called (previously the
    refusal arrived only after a multi-minute extraction)."""
    import klayout_tools.lvs as lvs_mod

    calls = []
    real = lvs_mod.extract_netlist_from_layout

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(lvs_mod, "extract_netlist_from_layout", spy)
    request = _gate_level_extraction_request(
        tmp_path,
        "module top(in, out);\n  input in;\n  output out;\n"
        "  wire [1:0] b;\n  assign b = { in, 1'b0 };\n"
        "  mylib__inv_1 u1 (.A(b[1]), .Y(out));\nendmodule\n",
    )
    with pytest.raises(LvsError, match="could not convert gate-level-verilog"):
        run_lvs(json.dumps(request))
    assert calls == []


def test_run_lvs_gate_level_verilog_valid_reference_proceeds_through_extraction(
    tmp_path, monkeypatch
):
    """Issue #7822's flip side: a valid gate-level reference is prepared
    once up front and then *reused* -- extraction runs exactly once and the
    run still produces a full report (the matching fixture
    `test_run_lvs_gate_level_verilog_reference_converts_and_matches` above
    is this shape's clean-layout positive control)."""
    import klayout_tools.lvs as lvs_mod

    calls = []
    real_extract = lvs_mod.extract_netlist_from_layout
    real_convert = lvs_mod._convert_gate_level_reference

    def extract(*args, **kwargs):
        calls.append("extract")
        return real_extract(*args, **kwargs)

    def convert(*args, **kwargs):
        calls.append("convert")
        return real_convert(*args, **kwargs)

    monkeypatch.setattr(lvs_mod, "extract_netlist_from_layout", extract)
    monkeypatch.setattr(lvs_mod, "_convert_gate_level_reference", convert)
    request = _gate_level_extraction_request(
        tmp_path,
        "module top(in, out);\n  input in;\n  output out;\n"
        "  mylib__inv_1 u1 (.A(in), .Y(out));\nendmodule\n",
    )
    report = run_lvs(json.dumps(request))
    assert calls == ["convert", "extract"]
    assert report["reference_conversion"]["form"] == "gate-level-verilog"


def test_report_uses_verilog_bytes_converted_before_reference_rewrite(
    tmp_path, monkeypatch
):
    from klayout_tools import lvs_layout_stage

    original = (
        _GATE_LEVEL_REFERENCE_VERILOG
        + r"""
module \old$cell (a); input a; endmodule
"""
    )
    replacement = original.replace(".A(mid), .Y(out)", ".A(out), .Y(mid)").replace(
        "old$cell", "new$cell"
    )
    path = tmp_path / "ref.v"
    request = {
        "layout": {
            "netlist": _write(tmp_path / "layout.spice", _GATE_LEVEL_LAYOUT_SPICE)
        },
        "reference": {
            "netlist": str(path),
            "top": "top",
            "form": "gate-level-verilog",
            "pdk": "myvariant",
            "library": "mylib",
            "pdk_root": _make_fake_pdk_library(
                tmp_path, "myvariant", "mylib", _GATE_LEVEL_LIBRARY_SPICE
            ),
        },
    }
    path.write_text(replacement, encoding="utf-8")
    changed = run_lvs(json.dumps(request))
    assert changed["status"] == "mismatch"
    assert changed["environment"]["reference_module_names"] == {"new$cell": "new_cell"}

    original_bytes = original.replace("\n", "\r\n").encode("utf-8")
    path.write_bytes(original_bytes)
    resolve_layout = lvs_layout_stage._resolve_layout_stage

    def regenerate(*args, **kwargs):
        result = resolve_layout(*args, **kwargs)
        path.write_text(replacement, encoding="utf-8")
        return result

    monkeypatch.setattr(lvs_layout_stage, "_resolve_layout_stage", regenerate)
    report = run_lvs(json.dumps(request))
    assert report["status"] == "match"
    assert report["error_count"] == 0
    assert (
        report["environment"]["reference_sha256"]
        == hashlib.sha256(original_bytes).hexdigest()
    )
    assert report["environment"]["reference_module_names"] == {"old$cell": "old_cell"}
