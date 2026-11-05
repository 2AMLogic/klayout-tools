"""Issue #5002: an SDF top `CELL` spelled `(INSTANCE <design name>)` aborts
`vvp` (`NULL handle passed to vpi_scan.`, rc -6) at the start of the first
cocotb test, even though every INTERCONNECT endpoint resolves.

Measured with cocotb 2.1.0 + Icarus 13.0: the same flat netlist and entries
with the root cell spelled `(INSTANCE)` annotate and pass.
`_reject_sdf_unresolvable_cell_instance` turns the abort into a named
`FunctionalVerificationError` before `vvp` starts.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from klayout_tools import functional_verification_sdf_instance as fvi
from klayout_tools.functional_verification import FunctionalVerificationError

HAVE_ICARUS = shutil.which("iverilog") is not None and shutil.which("vvp") is not None

_CELLS_V = """\
module klt_buf (A, Z);
  input A;
  output Z;
  assign Z = A;
  specify
    (A => Z) = (0.0, 0.0);
  endspecify
endmodule
"""

# Flat netlist with a scalar and a bus top-level port, one decl per line.
_TOP_V = """\
module top (a, d, y, q);
  input a;
  input [1:0] d;
  output y;
  output [1:0] q;
  wire n1;
  wire n2;
  klt_buf u1 (.A(a), .Z(n1));
  klt_buf u2 (.A(n1), .Z(y));
  klt_buf u3 (.A(d[0]), .Z(n2));
  klt_buf u4 (.A(n2), .Z(q[0]));
endmodule
"""

_ENTRIES = (
    "(INTERCONNECT u1/Z u2/A (0.1:0.1:0.1)) (INTERCONNECT u4/Z q[0] (0.1:0.1:0.1))"
)


def _sdf(instance: str, entries: str = _ENTRIES, divider: str = "/") -> str:
    return (
        '(DELAYFILE (SDFVERSION "3.0") (DESIGN "top")\n'
        f"  (DIVIDER {divider}) (TIMESCALE 1ns)\n"
        f'  (CELL (CELLTYPE "top") (INSTANCE {instance})\n'
        f"    (DELAY (ABSOLUTE\n      {entries}\n    ))\n  )\n)\n"
    )


def _stage(tmp_path: Path, sdf_text: str) -> tuple[str, list[str]]:
    (tmp_path / "cells.v").write_text(_CELLS_V, encoding="utf-8")
    (tmp_path / "top.v").write_text(_TOP_V, encoding="utf-8")
    sdf_path = tmp_path / "route.sdf"
    sdf_path.write_text(sdf_text, encoding="utf-8")
    return str(sdf_path), [str(tmp_path / "top.v"), str(tmp_path / "cells.v")]


def _check(tmp_path: Path, sdf_text: str, scope: str | None = None) -> None:
    sdf_path, sources = _stage(tmp_path, sdf_text)
    fvi._reject_sdf_unresolvable_cell_instance(sdf_path, sources, "top", scope)


@pytest.mark.parametrize("instance", ["top", "nosuch", "nosuch/u1"])
def test_cell_instance_not_in_the_netlist_is_rejected(tmp_path, instance):
    with pytest.raises(FunctionalVerificationError) as excinfo:
        _check(tmp_path, _sdf(instance))
    message = str(excinfo.value)
    assert f"(INSTANCE {instance})" in message
    assert "line 5" in message
    assert "vpi_scan" in message


def test_dot_divider_cell_instance_is_checked(tmp_path):
    with pytest.raises(FunctionalVerificationError, match=r"INSTANCE nosuch\.u1"):
        _check(tmp_path, _sdf("nosuch.u1", divider="."))


@pytest.mark.parametrize("instance", ["", "*", "u1", "u1/inner"])
def test_root_and_real_instances_pass(tmp_path, instance):
    _check(tmp_path, _sdf(instance))


def test_cell_without_interconnect_entries_is_not_checked(tmp_path):
    _check(tmp_path, _sdf("nosuch", entries="(IOPATH A Z (0.1:0.1:0.1))"))


def test_noop_when_the_instance_list_cannot_be_derived(tmp_path):
    sdf_path, sources = _stage(tmp_path, _sdf("top"))
    fvi._reject_sdf_unresolvable_cell_instance(sdf_path, sources, "absent")


def test_noop_on_an_unreadable_sdf(tmp_path):
    _, sources = _stage(tmp_path, "")
    fvi._reject_sdf_unresolvable_cell_instance(
        str(tmp_path / "missing.sdf"), sources, "top"
    )


def test_combined_guard_still_names_the_endpoint_first(tmp_path):
    """The #3740 endpoint guard runs ahead of the CELL-instance guard."""
    sdf_path, sources = _stage(
        tmp_path, _sdf("", entries="(INTERCONNECT top/a u1/A (0.1:0.1:0.1))")
    )
    with pytest.raises(FunctionalVerificationError, match=r"top/a"):
        fvi._reject_sdf_unresolvable_scopes(sdf_path, sources, "top")


def test_combined_guard_names_the_cell_instance(tmp_path):
    sdf_path, sources = _stage(tmp_path, _sdf("top"))
    with pytest.raises(FunctionalVerificationError, match=r"INSTANCE top"):
        fvi._reject_sdf_unresolvable_scopes(sdf_path, sources, "top")


@pytest.mark.skipif(not HAVE_ICARUS, reason="Icarus Verilog not installed")
def test_real_icarus_aborts_on_the_guarded_shape(tmp_path):
    """Pins the upstream behaviour: with the SDF's CELL scoped at a missing
    instance raw `vvp` dies with the `vpi_scan` assertion; the guard names
    it first. (`(INSTANCE)` annotates cleanly -- the control run.)"""
    sdf_path, sources = _stage(tmp_path, _sdf("top"))
    (tmp_path / "w.v").write_text(
        "module w (input a, input [1:0] d, output y, output [1:0] q);\n"
        "  top dut (.a(a), .d(d), .y(y), .q(q));\nendmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "ann.v").write_text(
        f'module ann; initial $sdf_annotate("{sdf_path}", w.dut); endmodule\n',
        encoding="utf-8",
    )
    vvp_out = tmp_path / "sim.vvp"
    build = subprocess.run(
        ["iverilog", "-g2012", "-gspecify", "-ginterconnect", "-s", "w", "-s", "ann"]
        + [
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
        fvi._reject_sdf_unresolvable_cell_instance(sdf_path, sources, "top")
