"""Issue #3740: an `options.sdf` INTERCONNECT endpoint naming an instance the
netlist does not have aborts `vvp` (`ERROR: NULL handle passed to vpi_scan.`,
SIGABRT) inside Icarus 13.0's `$sdf_annotate`.

Measured on raw `iverilog -ginterconnect`/`vvp`: any `<inst><DIVIDER><pin>`
endpoint whose `<inst>` is not in scope aborts -- on either side of the
entry, for both DIVIDER characters -- while a bare unknown name fails soft.
`_reject_sdf_unresolvable_interconnect_instances` turns that abort into a
named `FunctionalVerificationError` before `vvp` starts.
"""

from __future__ import annotations

import json
import shutil
import subprocess
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

HAVE_ICARUS = shutil.which("iverilog") is not None and shutil.which("vvp") is not None

_CELLS_V = """\
module klt_buf (A, Z);
  input A;
  output Z;
  assign Z = A;
  specify
    (A => Z) = (1.0, 1.0);
  endspecify
endmodule
"""

_TOP_V = """\
module top (a, y);
  input a;
  output y;
  wire n1;
  klt_buf u1 (.A(a), .Z(n1));
  klt_buf u2 (.A(n1), .Z(y));
endmodule
"""

_RESULTS_XML = """\
<testsuites name="results">
  <testsuite name="all" package="all">
    <property name="random_seed" value="1" />
    <testcase name="test_one" classname="test_top" time="0.01" sim_time_ns="1.0" />
  </testsuite>
</testsuites>
"""


def _sdf(entry: str, *, divider: str = "/", instance: str = "") -> str:
    return (
        '(DELAYFILE\n  (SDFVERSION "3.0")\n  (DESIGN "top")\n'
        f"  (DIVIDER {divider})\n  (TIMESCALE 1ns)\n"
        f'  (CELL\n    (CELLTYPE "top")\n    (INSTANCE {instance})\n'
        f"    (DELAY (ABSOLUTE\n      {entry}\n    ))\n  )\n)\n"
    )


def _stage(tmp_path: Path, sdf_text: str) -> tuple[str, list[str]]:
    (tmp_path / "cells.v").write_text(_CELLS_V, encoding="utf-8")
    (tmp_path / "top.v").write_text(_TOP_V, encoding="utf-8")
    sdf_path = tmp_path / "route.sdf"
    sdf_path.write_text(sdf_text, encoding="utf-8")
    return str(sdf_path), [str(tmp_path / "top.v"), str(tmp_path / "cells.v")]


def _check(tmp_path: Path, sdf_text: str) -> None:
    sdf_path, sources = _stage(tmp_path, sdf_text)
    fvs._reject_sdf_unresolvable_interconnect_instances(sdf_path, sources, "top")


_IC = "(INTERCONNECT {a} {b} (0.1:0.1:0.1))"


@pytest.mark.parametrize(
    ("a", "b", "token"),
    [
        ("top/a", "u1/A", "top/a"),  # design name used as a port prefix
        ("u1/Z", "nosuch/A", "nosuch/A"),
        ("nosuch/Z", "y", "nosuch/Z"),
    ],
)
def test_unresolvable_endpoint_is_rejected_naming_line_and_token(tmp_path, a, b, token):
    with pytest.raises(FunctionalVerificationError) as excinfo:
        _check(tmp_path, _sdf(_IC.format(a=a, b=b)))
    message = str(excinfo.value)
    assert token in message
    assert "line 10" in message
    assert "vpi_scan" in message


def test_dot_divider_endpoint_is_checked(tmp_path):
    with pytest.raises(FunctionalVerificationError, match=r"nosuch\.Z"):
        _check(
            tmp_path,
            _sdf(_IC.format(a="nosuch.Z", b="y"), divider="."),
        )


@pytest.mark.parametrize(
    "entry",
    [
        _IC.format(a="a", b="u1/A"),  # bare top-level port
        _IC.format(a="u1/Z", b="u2/A"),  # ordinary internal net
        _IC.format(a="u1/Z", b="nosuch"),  # bare unknown name fails soft
        _IC.format(a="u1/Z", b="y"),
    ],
)
def test_resolvable_or_soft_failing_endpoints_pass(tmp_path, entry):
    _check(tmp_path, _sdf(entry))


def test_escaped_flattened_instance_name_resolves(tmp_path):
    (tmp_path / "cells.v").write_text(_CELLS_V, encoding="utf-8")
    top = _TOP_V.replace("klt_buf u1", "klt_buf \\u_div.c1 ")
    (tmp_path / "top.v").write_text(top, encoding="utf-8")
    sdf_path = tmp_path / "route.sdf"
    sdf_path.write_text(_sdf(_IC.format(a="a", b="u_div.c1/A")), encoding="utf-8")
    fvs._reject_sdf_unresolvable_interconnect_instances(
        str(sdf_path), [str(tmp_path / "top.v"), str(tmp_path / "cells.v")], "top"
    )


def test_cell_scoped_to_a_sub_instance_is_not_checked(tmp_path):
    _check(tmp_path, _sdf(_IC.format(a="x/Z", b="y/A"), instance="u1"))


def test_noop_when_the_instance_list_cannot_be_derived(tmp_path):
    sdf_path, sources = _stage(tmp_path, _sdf(_IC.format(a="nosuch/Z", b="y")))
    fvs._reject_sdf_unresolvable_interconnect_instances(sdf_path, sources, "absent")


def test_noop_on_an_unreadable_sdf(tmp_path):
    _, sources = _stage(tmp_path, "")
    fvs._reject_sdf_unresolvable_interconnect_instances(
        str(tmp_path / "missing.sdf"), sources, "top"
    )


def test_run_rejects_before_launching_the_simulator(tmp_path, monkeypatch):
    _stage(tmp_path, _sdf(_IC.format(a="top/a", b="u1/A")))
    (tmp_path / "test_top.py").write_text("# stub\n", encoding="utf-8")
    runner = _FakeRunner(_RESULTS_XML)
    monkeypatch.setattr(
        fv,
        "_import_runner",
        lambda: types.SimpleNamespace(get_runner=lambda name: runner),
    )
    monkeypatch.setattr(fv, "_engine_version", lambda engine: "13.0")
    monkeypatch.setattr(fv, "_cocotb_version", lambda: "2.0.1")
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "engine": "icarus",
                "sources": ["top.v", "cells.v"],
                "hdl_toplevel": "top",
                "testbench": {"module": "test_top", "testcase": None},
                "options": {"sdf": {"file": "route.sdf"}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(FunctionalVerificationError, match=r"top/a"):
        run_functional_verification(str(request))


@pytest.mark.skipif(not HAVE_ICARUS, reason="Icarus Verilog not installed")
@pytest.mark.parametrize(
    "entry", [_IC.format(a="top/a", b="u1/A"), _IC.format(a="u1/Z", b="nosuch/A")]
)
def test_real_icarus_aborts_on_the_guarded_shape(tmp_path, entry):
    """Pins the upstream behaviour the guard exists for: raw `vvp` dies with
    the `vpi_scan` assertion on the repro, and the guard names it first."""
    sdf_path, sources = _stage(tmp_path, _sdf(entry))
    (tmp_path / "w.v").write_text(
        "module w (input a, output y);\n  top dut (.a(a), .y(y));\nendmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "ann.v").write_text(
        f'module ann; initial $sdf_annotate("{sdf_path}", w.dut); endmodule\n',
        encoding="utf-8",
    )
    vvp_out = tmp_path / "sim.vvp"
    build = subprocess.run(
        [
            "iverilog",
            "-g2012",
            "-gspecify",
            "-ginterconnect",
            "-s",
            "w",
            "-s",
            "ann",
            "-o",
            str(vvp_out),
            *sources,
            str(tmp_path / "w.v"),
            str(tmp_path / "ann.v"),
        ],
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        pytest.skip(f"iverilog lacks -ginterconnect: {build.stderr.strip()[:120]}")
    sim = subprocess.run(["vvp", str(vvp_out)], capture_output=True, text=True)
    if "NULL handle passed to vpi_scan" not in sim.stdout + sim.stderr:
        pytest.skip("this Icarus no longer aborts on the shape; guard is redundant")
    assert sim.returncode != 0

    with pytest.raises(FunctionalVerificationError, match="not in 'top'"):
        fvs._reject_sdf_unresolvable_interconnect_instances(sdf_path, sources, "top")
