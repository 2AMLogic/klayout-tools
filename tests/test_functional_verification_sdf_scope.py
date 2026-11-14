"""Issue #4521: `options.sdf.scope`, the mis-scoped-SDF diagnostic, and a
parameterised toplevel declaration under `options.sdf`."""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from helpers.cocotb_fakes import FakeCocotbRunner as _FakeRunner
from klayout_tools import functional_verification as fv
from klayout_tools import functional_verification_sdf as fvs
from klayout_tools.functional_verification import (
    FunctionalVerificationError,
    run_functional_verification,
)

_NETLIST_V = """\
module net (a, y);
  input a;
  output y;
  wire n1;
  buf u1 (n1, a);
  buf u2 (y, n1);
endmodule
"""

_BENCH_V = """\
module tb (a, y);
  input a;
  output y;
  net dut (.a(a), .y(y));
endmodule
"""

_PARAM_BENCH_V = """\
module tb #(parameter N = 20, parameter integer M = (2+3)) (
  input wire [N-1:0] a,
  output wire [N-1:0] y
);
  assign y = a;
endmodule
"""

_RESULTS_XML = """\
<testsuites name="results">
  <testsuite name="all" package="all">
    <property name="random_seed" value="1" />
    <testcase name="test_one" classname="test_tb" time="0.01" sim_time_ns="1.0" />
  </testsuite>
</testsuites>
"""

_SDF = (
    '(DELAYFILE\n  (SDFVERSION "3.0")\n  (DESIGN "net")\n  (DIVIDER /)\n'
    '  (CELL\n    (CELLTYPE "net")\n    (INSTANCE {instance})\n'
    "    (DELAY (ABSOLUTE (INTERCONNECT u1/Z u2/A (0.1:0.1:0.1))))\n  )\n)\n"
)


def _stage(tmp_path: Path, instance: str = "") -> tuple[str, list[str]]:
    (tmp_path / "net.v").write_text(_NETLIST_V, encoding="utf-8")
    (tmp_path / "tb.v").write_text(_BENCH_V, encoding="utf-8")
    sdf = tmp_path / "route.sdf"
    sdf.write_text(_SDF.format(instance=instance), encoding="utf-8")
    return str(sdf), [str(tmp_path / "tb.v"), str(tmp_path / "net.v")]


def test_scope_option_is_validated(tmp_path):
    sdf = tmp_path / "x.sdf"
    sdf.write_text("", encoding="utf-8")
    ok = fvs._resolve_sdf_option(
        {"file": "x.sdf", "scope": "a.b"}, "icarus", str(tmp_path)
    )
    assert ok is not None and ok["scope"] == "a.b"
    assert "scope" not in fvs._resolve_sdf_option(
        {"file": "x.sdf"}, "icarus", str(tmp_path)
    )
    for bad in ("", ".a", "a.", "a..b", "1a", "a b", 3):
        with pytest.raises(FunctionalVerificationError, match="scope"):
            fvs._resolve_sdf_option(
                {"file": "x.sdf", "scope": bad}, "icarus", str(tmp_path)
            )


def test_scope_reference_and_annotate_scope():
    assert fvs._sdf_scope_reference("a.b") == "a.b"
    assert fv._sdf_annotate_scope(None) == "klt_sdf_dut_wrapper.klt_sdf_dut"
    assert fv._sdf_annotate_scope(None, "dut") == "klt_sdf_dut_wrapper.klt_sdf_dut.dut"
    assert fvs._split_sdf_scope(r"\g[0].u") == [r"\g[0]", "u"]
    assert fvs._sdf_scope_reference(r"\g[0].u") == "\\g[0] .u"


def test_mis_scoped_sdf_is_named_before_vvp(tmp_path):
    sdf, sources = _stage(tmp_path)
    with pytest.raises(FunctionalVerificationError) as excinfo:
        fvs._reject_sdf_mis_scoped_top_cell(sdf, sources, "tb", None)
    message = str(excinfo.value)
    assert "'net'" in message and "dut" in message and "options.sdf.scope" in message


def test_correct_scope_and_named_instance_pass(tmp_path):
    sdf, sources = _stage(tmp_path)
    fvs._reject_sdf_mis_scoped_top_cell(sdf, sources, "tb", "dut")
    fvs._reject_sdf_unresolvable_interconnect_instances(sdf, sources, "tb", "dut")
    named, sources = _stage(tmp_path, instance="dut")
    fvs._reject_sdf_mis_scoped_top_cell(named, sources, "tb", None)
    # same module as the toplevel: unchanged behaviour
    fvs._reject_sdf_mis_scoped_top_cell(sdf, [sources[1]], "net", None)


def test_unresolvable_scope_is_a_noop(tmp_path):
    sdf, sources = _stage(tmp_path)
    fvs._reject_sdf_mis_scoped_top_cell(sdf, sources, "tb", "nosuch")


def test_parameterised_header_parses(tmp_path):
    path = tmp_path / "tb.v"
    path.write_text(_PARAM_BENCH_V, encoding="utf-8")
    ports = fvs._parse_toplevel_ports([str(path)], "tb")
    assert ports == [("input", "[N-1:0]", "a"), ("output", "[N-1:0]", "y")]
    declaration, names = fvs._parse_toplevel_parameters([str(path)], "tb")
    assert names == ["N", "M"]
    assert "parameter N = 20" in declaration


def test_localparam_is_declared_not_forwarded(tmp_path):
    path = tmp_path / "tb.v"
    path.write_text(
        "module tb #(parameter N = 4, localparam W = N*2) (input a, output y);\n"
        "endmodule\n",
        encoding="utf-8",
    )
    assert fvs._parse_toplevel_parameters([str(path)], "tb")[1] == ["N"]


def test_wrapper_declares_and_forwards_parameters(tmp_path):
    path = tmp_path / "tb.v"
    path.write_text(_PARAM_BENCH_V, encoding="utf-8")
    wrapper = tmp_path / "w.v"
    fvs._write_sdf_dut_wrapper(
        str(wrapper),
        hdl_toplevel="tb",
        ports=fvs._parse_toplevel_ports([str(path)], "tb"),
        parameters=fvs._parse_toplevel_parameters([str(path)], "tb"),
    )
    text = wrapper.read_text(encoding="utf-8")
    assert "module klt_sdf_dut_wrapper #(parameter N = 20" in text
    assert "tb #(.N(N), .M(M)) klt_sdf_dut (" in text


def test_plain_header_wrapper_is_unchanged(tmp_path):
    (tmp_path / "net.v").write_text(_NETLIST_V, encoding="utf-8")
    wrapper = tmp_path / "w.v"
    fvs._write_sdf_dut_wrapper(
        str(wrapper),
        hdl_toplevel="net",
        ports=fvs._parse_toplevel_ports([str(tmp_path / "net.v")], "net"),
    )
    text = wrapper.read_text(encoding="utf-8")
    assert "module klt_sdf_dut_wrapper (" in text
    assert "  net klt_sdf_dut (" in text


def test_scope_rejected_with_verilog_top():
    tb = {"verilog_top": "tb", "dut_instance": "tb.dut"}
    with pytest.raises(FunctionalVerificationError, match="scope"):
        fv._check_verilog_top_sdf(tb, {"file": "x", "scope": "dut"}, None)


def test_run_rejects_mis_scoped_sdf_and_reports_scope(tmp_path, monkeypatch):
    _stage(tmp_path)
    (tmp_path / "test_tb.py").write_text("# stub\n", encoding="utf-8")
    runner = _FakeRunner(_RESULTS_XML)
    monkeypatch.setattr(
        fv,
        "_import_runner",
        lambda: types.SimpleNamespace(get_runner=lambda name: runner),
    )
    monkeypatch.setattr(fv, "_engine_version", lambda engine: "13.0")
    monkeypatch.setattr(fv, "_cocotb_version", lambda: "2.0.1")

    def request(sdf: dict) -> str:
        path = tmp_path / "request.json"
        path.write_text(
            json.dumps(
                {
                    "engine": "icarus",
                    "sources": ["tb.v", "net.v"],
                    "hdl_toplevel": "tb",
                    "testbench": {"module": "test_tb", "testcase": None},
                    "options": {"sdf": sdf},
                }
            ),
            encoding="utf-8",
        )
        return str(path)

    with pytest.raises(FunctionalVerificationError, match=r"options\.sdf\.scope"):
        run_functional_verification(request({"file": "route.sdf"}))
