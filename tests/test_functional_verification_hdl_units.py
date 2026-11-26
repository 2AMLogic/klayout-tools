"""Time units of an SDF-annotated Icarus module in its compilation unit."""

import json
from pathlib import Path

import pytest

from helpers.tool_probes import SKIP_NO_ICARUS as needs_icarus
from klayout_tools.functional_verification import run_functional_verification
from klayout_tools.functional_verification_hdl_units import _verilog_time_units
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
    assert _verilog_time_units(texts, "100ns") == {"leaf": expected}


def test_resetall_restores_compilation_declaration():
    texts = [
        "timeprecision 1ps; timeunit 10ps;",
        "`timescale 1ns/1ps\nmodule a; endmodule",
        "`resetall\nmodule b; endmodule",
    ]
    assert _verilog_time_units(texts, "100ns") == {"a": "1ns", "b": "10ps"}
