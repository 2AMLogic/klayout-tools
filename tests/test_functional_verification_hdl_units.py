"""Time units of an SDF-annotated Icarus module in its compilation unit."""

import json
from pathlib import Path

import pytest

from helpers.tool_probes import SKIP_NO_ICARUS as needs_icarus
from klayout_tools.functional_verification import run_functional_verification
from klayout_tools.functional_verification_base import FunctionalVerificationError
from klayout_tools.functional_verification_hdl_units import (
    _active_sources,
    _verilog_time_units,
)
from test_functional_verification_verilog_top import _stage_sdf_units


@needs_icarus
def test_matching_sdf_inherits_compilation_unit(tmp_path):
    request = _stage_sdf_units(tmp_path, sdf_timescale="1ps", delay="500")
    leaf = tmp_path / "and_cell.v"
    leaf.write_text(leaf.read_text().replace("`timescale 1ns/1ps", "timeunit 1ps/1ps;"))
    report = run_functional_verification(request)
    sdf = report["environment"]["sdf"]
    assert report["status"] == "pass" and sdf["annotated"]
    assert (sdf["timescale"], sdf["time_unit"]) == ("1ps", "1ps")


@needs_icarus
@pytest.mark.parametrize(
    "flags",
    [
        ["-v"],
        *[
            [f"-g{prefix}{feature}"]
            for feature in (
                "strict-ca-eval",
                "shared-loop-index",
                "io-range-error",
                "xtypes",
                "icarus-misc",
            )
            for prefix in ("", "no-")
        ],
        ["-gassertions"],
        ["-gsupported-assertions"],
        ["-gno-assertions"],
        ["-gstrict-expr-width"],
        ["-gno-strict-expr-width"],
        ["-Ttyp"],
        ["-T", "typ"],
    ],
)
def test_matching_sdf_with_unit_neutral_build_arg(tmp_path, flags):
    request = Path(_stage_sdf_units(tmp_path))
    spec = json.loads(request.read_text())
    spec["options"]["build_args"] = flags
    request.write_text(json.dumps(spec))
    report = run_functional_verification(str(request))
    sdf = report["environment"]["sdf"]
    assert report["status"] == "pass" and sdf["annotated"]
    assert (sdf["timescale"], sdf["time_unit"]) == ("1ns", "1ns")


def test_verilog_time_units_follow_the_last_directive_in_compile_order(tmp_path):
    """What Icarus 13.0 elaborates, each rule measured live on raw ``iverilog``:
    a directive persists from one source into the next, ``` `resetall ```
    restores the compile-time default, and a comment declares nothing."""
    first = tmp_path / "a.v"
    first.write_text(
        "`timescale 1ns/10ps\n"
        "module first(input a, output y); endmodule\n"
        "// `timescale 100ps/1ps\n"
        "/* module commented_out; endmodule */\n"
        "`timescale 100 ps / 1 ps\n"
        "module second(input a, output y); endmodule\n"
    )
    second = tmp_path / "b.v"
    second.write_text(
        "module third(input a, output y); endmodule\n"
        "`resetall\n"
        "module fourth(input a, output y); endmodule\n"
        "`timescale 1ps/1ps\n"
        "module fifth(input a, output y); endmodule\n"
    )
    texts = _active_sources([str(first), str(second)], [], {}, [], None)

    modules = _verilog_time_units(texts, "1ns").top
    assert {name: mod.unit for name, mod in modules.items()} == {
        "first": "1ns",
        "second": "100ps",
        "third": "100ps",
        "fourth": "1ns",
        "fifth": "1ps",
    }
    assert _verilog_time_units(texts, None).top["fourth"].unit is None


@pytest.mark.parametrize("scope", ["package", "interface", "program"])
@pytest.mark.parametrize(
    "declaration,expected", [("", "100ns"), ("timeunit 10ps;", "10ps")]
)
def test_scoped_declaration_preserves_compilation_default(scope, declaration, expected):
    texts = [
        declaration,
        f"{scope} other; timeunit 1ns/1ps; end{scope}",
        "module leaf; endmodule",
    ]
    assert _verilog_time_units(texts, "100ns").top["leaf"].unit == expected


def test_resetall_restores_compilation_declaration():
    texts = [
        "timeprecision 1ps; timeunit 10ps;",
        "`timescale 1ns/1ps\nmodule a; endmodule",
        "`resetall\nmodule b; endmodule",
    ]
    modules = _verilog_time_units(texts, "100ns").top
    assert {name: mod.unit for name, mod in modules.items()} == {
        "a": "1ns",
        "b": "10ps",
    }


@needs_icarus
@pytest.mark.parametrize(
    "scale,delay", [("1", "0.5"), ("10", "0.05"), ("100", "0.005")]
)
def test_matching_decimal_sdf_timescale(tmp_path, scale, delay):
    request = _stage_sdf_units(
        tmp_path,
        sdf_timescale=f"{scale}.0 ns",
        leaf_timescale=f"{scale}ns/1ps",
        delay=delay,
    )
    report = run_functional_verification(request)
    sdf = report["environment"]["sdf"]
    assert report["status"] == "pass" and sdf["annotated"]
    assert sdf["timescale"] == sdf["time_unit"] == f"{scale}ns"


@needs_icarus
@pytest.mark.parametrize("sdf_unit", ["1ns", "1ps"])
def test_repeated_module_timeunit(tmp_path, sdf_unit):
    request = _stage_sdf_units(tmp_path, sdf_timescale=sdf_unit)
    leaf = tmp_path / "and_cell.v"
    code = leaf.read_text().replace("`timescale 1ns/1ps", "`timescale 100ps/1ps")
    leaf.write_text(
        code.replace("  and (", "  timeunit 1ns/1ps;\n  and (").replace(
            "  specify", "  timeunit 1ns/1ps;\n  specify"
        )
    )
    if sdf_unit == "1ps":
        with pytest.raises(FunctionalVerificationError, match="1000x too large"):
            run_functional_verification(request)
    else:
        report = run_functional_verification(request)
        assert report["status"] == "pass"
        assert report["environment"]["sdf"]["time_unit"] == sdf_unit


def test_disagreeing_module_redeclaration_keeps_unit_unknown():
    text = "module leaf; timeunit 1ns/1ps; wire x; timeunit 10ps; endmodule"
    assert _verilog_time_units([text], "1ns").top["leaf"].unit is None


def _assert_matching_ns_edge(tmp_path, request):
    bench = tmp_path / "tb_and2.v"
    bench.write_text(
        bench.read_text().replace(
            "  task check",
            "  always @(posedge y) if ($realtime != 10.5)\n"
            '    $fatal(1, "unexpected SDF delay");\n  task check',
        )
    )
    report = run_functional_verification(request)
    assert report["status"] == "pass"
    sdf = report["environment"]["sdf"]
    assert sdf["annotated"] and sdf["timescale"] == sdf["time_unit"] == "1ns"


@needs_icarus
@pytest.mark.parametrize("scope", ["interface", "program"])
def test_nested_time_declaration_belongs_to_its_own_scope(tmp_path, scope):
    request = _stage_sdf_units(tmp_path)
    leaf = tmp_path / "and_cell.v"
    leaf.write_text(
        leaf.read_text().replace(
            "  and (",
            f"  {scope} nested; timeunit 1ps/1ps; end{scope}\n  and (",
        )
    )
    _assert_matching_ns_edge(tmp_path, request)


@needs_icarus
def test_timeunit_identifier_in_verilog_2005(tmp_path):
    request = _stage_sdf_units(tmp_path)
    leaf = tmp_path / "and_cell.v"
    leaf.write_text(
        '`begin_keywords "1364-2005"\n'
        + leaf.read_text().replace("  and (", "  wire timeunit;\n  and (")
        + "`end_keywords\n"
    )
    _assert_matching_ns_edge(tmp_path, request)


def test_end_keywords_restores_the_enclosing_language_across_files():
    texts = [
        '`begin_keywords "1364-2005"\nmodule a; wire timeunit; endmodule',
        '`begin_keywords "1800-2012"\nmodule b; timeunit 1ps; endmodule\n`end_keywords',
        "module c; wire timeunit; endmodule\n`end_keywords",
        "module d; timeunit 10ps; endmodule",
    ]
    modules = _verilog_time_units(texts, "1ns").top
    assert {name: mod.unit for name, mod in modules.items()} == {
        "a": "1ns",
        "b": "1ps",
        "c": "1ns",
        "d": "10ps",
    }


@needs_icarus
@pytest.mark.parametrize("leaf_unit,unused_unit", [("1ps", "1ns"), ("1ns", "1ps")])
def test_unrelated_nested_module_with_same_name(tmp_path, leaf_unit, unused_unit):
    request = _stage_sdf_units(tmp_path, leaf_timescale=f"{leaf_unit}/1ps")
    leaf = tmp_path / "and_cell.v"
    with leaf.open("a") as source:
        source.write(
            f"\n`timescale {unused_unit}/1ps\n"
            "module unrelated; module and_cell; endmodule endmodule\n"
        )
    if leaf_unit == "1ps":
        with pytest.raises(FunctionalVerificationError, match="'u1'.*1000x too small"):
            run_functional_verification(request)
        assert not (tmp_path / ".klt/functional-verification/build_icarus.log").exists()
    else:
        _assert_matching_ns_edge(tmp_path, request)


@needs_icarus
@pytest.mark.parametrize("sdf_unit", ["1ns", "1ps"])
def test_module_header_package_imports_before_local_unit(tmp_path, sdf_unit):
    request = _stage_sdf_units(
        tmp_path, leaf_timescale="1ps/1ps", sdf_timescale=sdf_unit
    )
    leaf = tmp_path / "and_cell.v"
    leaf.write_text(
        leaf.read_text()
        .replace(
            "module and_cell(",
            "package model_types; typedef logic pin_t; endpackage\n"
            "package model_params; parameter N=1; endpackage\n"
            "module and_cell import model_types::*; import model_params::N; "
            "#(parameter WIDTH=N) (",
        )
        .replace("  and (", "  timeunit 1ns/1ps;\n  and (")
    )
    if sdf_unit == "1ps":
        with pytest.raises(FunctionalVerificationError, match="'u1'.*1000x too large"):
            run_functional_verification(request)
        assert not (tmp_path / ".klt/functional-verification/build_icarus.log").exists()
    else:
        _assert_matching_ns_edge(tmp_path, request)
