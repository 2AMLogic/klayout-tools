#!/usr/bin/env python3
"""Regenerates `results.json` -- the Epic #704 Phase 4 (issue #990) end-to-
end validation snapshot: `klt synthesize` run for real (Yosys + bundled
ABC) against `sky130-modexp`'s canary RTL plus 4 real Tiny Tapeout tt06
corpus designs (`README.md`'s "Corpus" section has full provenance/
licensing), each checked equivalent to its own source RTL against Yosys as
the ground-truth oracle.

Deliberate, reviewed, **never a CI step** -- same convention as
`tests/corpus/statime/regenerate.sh` and `tests/corpus/techmap/regenerate.sh`
(a fresh Yosys/ABC/PDK version can shift QoR numbers a little; re-running
this script and re-committing `results.json` is how that drift gets
captured, on purpose, not silently).

Requires on `$PATH`: `klt` (this repo's own CLI, `uv sync --extra dev` then
activate the venv), `yosys`, `iverilog`/`vvp`, and a resolvable
`sky130_fd_sc_hd` liberty + Verilog cell-model library (`volare`, or set
`PDK_ROOT`/`SKY130_HD_LIB`).

Two equivalence methods, per design's own scope (see README.md
"Equivalence method per design" for the rationale):

- **combinational** (`tt_um_ALU` here) -- `klt synthesize --verify-
  equivalence`, i.e. `klt equiv`'s real Yosys SAT proof, wired in as the
  command's own acceptance gate (issue #808).
- **sequential** (every other design here, including `modexp`) -- `klt
  equiv`'s Phase 0 scope is combinational-only (docs/cli/equiv.md), so this
  script instead (a) attempts `seq_equiv_check.sh`'s bounded Yosys
  `equiv_make`/`equiv_induct` temporal-induction proof, and (b)
  additionally runs a real gate-level simulation (Icarus, the real
  synthesized netlist plus the real sky130 functional cell models) --
  either against the design's own golden reference (`modexp`'s bit-exact
  `pow(base, exp, mod)`) or as a directed RTL-vs-gate co-simulation diff
  (the 3 sequential TT designs). See README.md for why (a) alone is not
  expected to converge for every design in this corpus and is not treated
  as the sole verdict.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CORPUS_DIR = Path(__file__).resolve().parent
SOURCES_DIR = CORPUS_DIR / "sources"
MODEXP_RTL = REPO_ROOT / "examples" / "functional-verification" / "modexp.v"

PDK_ROOT = os.environ.get("PDK_ROOT") or str(Path.home() / ".volare")
PDK_VARIANT = os.environ.get("PDK", "sky130A")
LIB = os.environ.get(
    "SKY130_HD_LIB",
    str(
        Path(PDK_ROOT)
        / PDK_VARIANT
        / "libs.ref"
        / "sky130_fd_sc_hd"
        / "lib"
        / "sky130_fd_sc_hd__tt_025C_1v80.lib"
    ),
)
CELL_VERILOG_DIR = (
    Path(PDK_ROOT) / PDK_VARIANT / "libs.ref" / "sky130_fd_sc_hd" / "verilog"
)
PRIMITIVES_V = CELL_VERILOG_DIR / "primitives.v"
CELLS_V = CELL_VERILOG_DIR / "sky130_fd_sc_hd.v"


def _run(
    cmd: list[str], cwd: Path, env: dict | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


def klt_synthesize(
    workdir: Path,
    sources: list[str],
    hdl_toplevel: str,
    verify_equivalence: bool,
) -> dict:
    request = {
        "sources": sources,
        "hdl_toplevel": hdl_toplevel,
        "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
    }
    (workdir / "req.json").write_text(json.dumps(request), encoding="utf-8")
    env = dict(os.environ)
    env["PDK_ROOT"] = PDK_ROOT
    env["PDK"] = PDK_VARIANT
    cmd = ["klt", "synthesize", "req.json", "--format", "json"]
    if verify_equivalence:
        cmd.append("--verify-equivalence")
    proc = _run(cmd, cwd=workdir, env=env)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - diagnostic aid
        raise RuntimeError(
            f"klt synthesize did not emit JSON for {hdl_toplevel}: "
            f"rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}"
        ) from exc


def seq_equiv_check(
    workdir: Path, top: str, gate_netlist: str, seq_depth: int, gold_files: list[str]
) -> tuple[bool, str]:
    """Returns (proven, tail_of_log)."""
    script = CORPUS_DIR / "seq_equiv_check.sh"
    cmd = [str(script), str(workdir), top, gate_netlist, str(seq_depth), *gold_files]
    env = dict(os.environ)
    env["SKY130_HD_LIB"] = LIB
    proc = _run(cmd, cwd=workdir, env=env)
    log = proc.stdout + proc.stderr
    proven = proc.returncode == 0 and "Equivalence successfully proven!" in log
    tail = "\n".join(log.splitlines()[-15:])
    return proven, tail


def _rename_gate_modules(netlist_text: str, module_names: list[str]) -> str:
    """Prefixes every declaration/instantiation of each hierarchical module
    name in `module_names` with ``gate_`` inside a synthesized netlist, so
    it can be `read_verilog`'d alongside the original RTL (which declares
    the same module names) without a duplicate-definition clash. Standard-
    cell instance types (``sky130_fd_sc_hd__*``) are untouched -- gold never
    declares those, so there is no collision to avoid there."""
    text = netlist_text
    for name in module_names:
        text = re.sub(rf"\bmodule {name}\(", f"module gate_{name}(", text)
        text = re.sub(rf"(?m)^(\s*){name}(\s+\w+\s*\()", rf"\1gate_{name}\2", text)
    return text


def _iverilog_build(workdir: Path, out_vvp: Path, sources: list[Path]) -> None:
    cmd = [
        "iverilog",
        "-g2012",
        "-DFUNCTIONAL",
        "-o",
        str(out_vvp),
        str(PRIMITIVES_V),
        str(CELLS_V),
        *[str(s) for s in sources],
    ]
    proc = _run(cmd, cwd=workdir)
    if proc.returncode != 0:
        raise RuntimeError(f"iverilog failed: {proc.stderr}")


def _vvp_run(vvp_path: Path) -> str:
    proc = _run(["vvp", str(vvp_path)], cwd=vvp_path.parent)
    return proc.stdout + proc.stderr


def gls_modexp_oracle(workdir: Path, gate_netlist: Path) -> tuple[bool, int, str]:
    """Gate-level simulation of the real synthesized `modexp` netlist,
    checked bit-exact against Python's own `pow(base, exp, mod)` -- the
    same golden reference `test_modexp.py` (klt functional-verification)
    already validates the RTL against, now exercised against the netlist
    Yosys/ABC actually produced."""
    rng = random.Random(0)
    vectors = [
        (2, 10, 1000),
        (3, 7, 100),
        (0, 0, 7),
        (5, 0, 7),
        (0, 5, 7),
        (7, 13, 11),
        (6, 9, 13),
    ]
    for _ in range(20):
        mod = rng.randint(2, 65535)
        base = rng.randint(0, mod - 1)
        exp = rng.randint(0, 65535)
        vectors.append((base, exp, mod))

    run_lines = []
    for b, e, m in vectors:
        want = pow(b, e, m)
        run_lines.append(f"    run({b}, {e}, {m}, {want});")

    tb = f"""\
`timescale 1ns/1ps
module gls_tb;
  reg clk = 0;
  reg rst_n = 0;
  reg start = 0;
  reg [15:0] base_in, exp_in, mod_in;
  wire done;
  wire [15:0] result;

  modexp dut (.clk(clk), .rst_n(rst_n), .start(start),
              .base_in(base_in), .exp_in(exp_in), .mod_in(mod_in),
              .done(done), .result(result));

  always #5 clk = ~clk;

  integer fails = 0;
  initial begin
    rst_n = 0; start = 0; base_in = 0; exp_in = 0; mod_in = 0;
    repeat (3) @(posedge clk);
    rst_n = 1;
    @(posedge clk);

{chr(10).join(run_lines)}

    if (fails == 0)
      $display("ALL GLS VECTORS PASSED (%0d cases)", {len(vectors)});
    else
      $display("%0d GLS VECTOR(S) FAILED", fails);
    $finish;
  end

  task run(input [15:0] b, input [15:0] e, input [15:0] m, input [15:0] want);
    integer cyc;
    begin
      @(posedge clk);
      base_in = b; exp_in = e; mod_in = m; start = 1;
      @(posedge clk);
      start = 0;
      cyc = 0;
      while (done !== 1 && cyc < 20000) begin
        @(posedge clk);
        cyc = cyc + 1;
      end
      if (done !== 1) begin
        $display("TIMEOUT for base=%0d exp=%0d mod=%0d", b, e, m);
        fails = fails + 1;
      end else if (result !== want) begin
        $display("MISMATCH base=%0d exp=%0d mod=%0d got=%0d want=%0d",
                  b, e, m, result, want);
        fails = fails + 1;
      end else begin
        $display("OK base=%0d exp=%0d mod=%0d result=%0d", b, e, m, result);
      end
    end
  endtask
endmodule
"""
    tb_path = workdir / "gls_tb.v"
    tb_path.write_text(tb, encoding="utf-8")
    vvp = workdir / "gls.vvp"
    _iverilog_build(workdir, vvp, [gate_netlist, tb_path])
    log = _vvp_run(vvp)
    passed = "ALL GLS VECTORS PASSED" in log
    return passed, len(vectors), log


def gls_dual_dut_diff(
    workdir: Path,
    top: str,
    rtl_sources: list[Path],
    gate_netlist: Path,
    submodules: list[str],
    n_cycles: int = 200,
) -> tuple[bool, str]:
    """Directed RTL-vs-gate co-simulation: instantiate the gold RTL and the
    real synthesized gate netlist side by side, share clk/reset/inputs, and
    diff every TT-wrapper output pin every cycle. Used for the sequential
    TT designs, all of which share the `ui_in/uo_out/uio_in/uio_out/
    uio_oe/ena/clk/rst_n` wrapper interface -- see README.md "Directed vs.
    randomized stimulus" for why this uses held/directed stimulus rather
    than free-running per-cycle random toggling."""
    gate_text = gate_netlist.read_text(encoding="utf-8")
    renamed = _rename_gate_modules(gate_text, [top, *submodules])
    gate_renamed_path = workdir / "gate_netlist_renamed.v"
    gate_renamed_path.write_text(renamed, encoding="utf-8")

    tb = f"""\
`timescale 1ns/1ps
module diff_tb;
  reg clk = 0;
  reg rst_n = 0;
  reg ena = 1;
  reg [7:0] ui_in = 0;
  reg [7:0] uio_in = 0;

  wire [7:0] gold_uo_out, gold_uio_out, gold_uio_oe;
  wire [7:0] gate_uo_out, gate_uio_out, gate_uio_oe;

  {top} gold (
    .ui_in(ui_in), .uo_out(gold_uo_out), .uio_in(uio_in),
    .uio_out(gold_uio_out), .uio_oe(gold_uio_oe),
    .ena(ena), .clk(clk), .rst_n(rst_n)
  );

  gate_{top} gate (
    .ui_in(ui_in), .uo_out(gate_uo_out), .uio_in(uio_in),
    .uio_out(gate_uio_out), .uio_oe(gate_uio_oe),
    .ena(ena), .clk(clk), .rst_n(rst_n)
  );

  always #5 clk = ~clk;

  integer mismatches = 0;
  integer cyc;

  initial begin
    rst_n = 0; ui_in = 0; uio_in = 0;
    repeat (5) @(posedge clk);
    rst_n = 1;
    @(posedge clk);

    // Directed stimulus: a handful of (ui_in, uio_in[0]) plateaus, each
    // held for several cycles so any capture/handshake logic samples a
    // stable value -- see README.md for why free-running per-cycle random
    // stimulus is not used here.
    ui_in = 8'b0101_0011; uio_in[0] = 0;
    repeat (2) @(posedge clk);
    uio_in[0] = 1;
    repeat (3) @(posedge clk);
    uio_in[0] = 0;
    repeat (40) @(posedge clk);

    ui_in = 8'b1010_0110; uio_in[0] = 0;
    repeat (2) @(posedge clk);
    uio_in[0] = 1;
    repeat (3) @(posedge clk);
    uio_in[0] = 0;
    repeat (40) @(posedge clk);

    ui_in = 8'b1111_0000; uio_in = 8'hFF;
    repeat (40) @(posedge clk);

    ui_in = 8'h00; uio_in = 8'h00;
    repeat (40) @(posedge clk);

    for (cyc = 0; cyc < {n_cycles}; cyc = cyc + 1) begin
      @(posedge clk);
      #1;
      if (gold_uo_out !== gate_uo_out
          || gold_uio_out !== gate_uio_out
          || gold_uio_oe !== gate_uio_oe) begin
        mismatches = mismatches + 1;
        if (mismatches <= 10)
          $display("MISMATCH cyc=%0d gold(uo=%b uio=%b oe=%b) gate(uo=%b uio=%b oe=%b)",
                    cyc, gold_uo_out, gold_uio_out, gold_uio_oe,
                    gate_uo_out, gate_uio_out, gate_uio_oe);
      end
    end

    if (mismatches == 0)
      $display("DIFF_TB: ALL %0d CYCLES MATCHED", cyc);
    else
      $display("DIFF_TB: %0d MISMATCH CYCLES OUT OF %0d", mismatches, cyc);
    $finish;
  end
endmodule
"""
    tb_path = workdir / "diff_tb.v"
    tb_path.write_text(tb, encoding="utf-8")
    vvp = workdir / "diff.vvp"
    _iverilog_build(workdir, vvp, [*rtl_sources, gate_renamed_path, tb_path])
    log = _vvp_run(vvp)
    passed = "DIFF_TB: ALL" in log and "MATCHED" in log
    return passed, log


def _copy_sources(design_dir: Path, workdir: Path) -> list[str]:
    names = []
    for src in sorted(design_dir.glob("*.v")):
        shutil.copy(src, workdir / src.name)
        names.append(src.name)
    return names


def validate_modexp() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        shutil.copy(MODEXP_RTL, workdir / "modexp.v")
        report = klt_synthesize(
            workdir, ["modexp.v"], "modexp", verify_equivalence=False
        )
        gate_netlist = Path(report["netlist_path"])

        seq_proven, seq_log_tail = seq_equiv_check(
            workdir, "modexp", str(gate_netlist), 16, ["modexp.v"]
        )
        gls_passed, gls_cases, gls_log = gls_modexp_oracle(workdir, gate_netlist)

        return {
            "design": "modexp",
            "kind": "sequential",
            "source": "examples/functional-verification/modexp.v (WIDTH=16 default)",
            "synthesize": {
                "status": report["status"],
                "instance_count": report["instance_count"],
                "area_um2": report["area_um2"],
                "timing": report.get("timing"),
            },
            "equivalence": {
                "yosys_equiv_induct": {
                    "proven": seq_proven,
                    "seq_depth": 16,
                    "note": (
                        "did not converge within the bounded induction depth "
                        "-- inconclusive, not a counterexample; see README.md"
                        if not seq_proven
                        else "converged"
                    ),
                    "log_tail": seq_log_tail,
                },
                "gate_level_simulation": {
                    "method": (
                        "python pow(base, exp, mod) oracle, same reference "
                        "test_modexp.py checks the RTL against"
                    ),
                    "passed": gls_passed,
                    "cases": gls_cases,
                },
            },
        }


def validate_tt_design(
    macro: str,
    top: str,
    sources: list[str],
    kind: str,
    verify_equivalence: bool,
    submodules: list[str] | None = None,
    seq_depth: int = 16,
) -> dict:
    design_dir = SOURCES_DIR / macro
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        _copy_sources(design_dir, workdir)
        report = klt_synthesize(
            workdir, sources, top, verify_equivalence=verify_equivalence
        )
        gate_netlist = Path(report["netlist_path"])

        result: dict = {
            "design": macro,
            "kind": kind,
            "synthesize": {
                "status": report["status"],
                "instance_count": report["instance_count"],
                "area_um2": report["area_um2"],
                "timing": report.get("timing"),
            },
        }

        if verify_equivalence:
            equivalence = report.get("equivalence")
            if equivalence is not None:
                # Drop the per-run temp-directory artifact paths -- not
                # reproducible across regenerate.sh runs, and the verdict/
                # engine fields are what this snapshot is actually for.
                equivalence = {k: v for k, v in equivalence.items() if k != "artifacts"}
            result["equivalence"] = {"klt_equiv": equivalence}
        else:
            seq_proven, seq_log_tail = seq_equiv_check(
                workdir, top, str(gate_netlist), seq_depth, sources
            )
            rtl_paths = [workdir / s for s in sources]
            gls_passed, gls_log = gls_dual_dut_diff(
                workdir, top, rtl_paths, gate_netlist, submodules or []
            )
            result["equivalence"] = {
                "yosys_equiv_induct": {
                    "proven": seq_proven,
                    "seq_depth": seq_depth,
                    "note": (
                        "did not converge within the bounded induction depth "
                        "-- inconclusive, not a counterexample; see README.md"
                        if not seq_proven
                        else "converged"
                    ),
                    "log_tail": seq_log_tail,
                },
                "gate_level_simulation": {
                    "method": "directed RTL-vs-gate dual-DUT co-simulation diff",
                    "passed": gls_passed,
                },
            }
        return result


def main() -> int:
    if not shutil.which("klt"):
        print(
            "error: klt not on PATH (activate .venv / uv sync --extra dev)",
            file=sys.stderr,
        )
        return 1
    if not shutil.which("yosys") or not shutil.which("iverilog"):
        print("error: yosys and/or iverilog not on PATH", file=sys.stderr)
        return 1
    if not Path(LIB).exists():
        print(f"error: sky130_fd_sc_hd liberty not found at {LIB}", file=sys.stderr)
        return 1

    results = []
    print("== modexp (sky130-modexp canary RTL) ==")
    results.append(validate_modexp())

    print("== tt_um_ALU (combinational) ==")
    results.append(
        validate_tt_design(
            "tt_um_ALU",
            "tt_um_ALU",
            ["project.v", "ALU_3bits_Top.v", "ALU_ArithmeticOperators.v"],
            kind="combinational",
            verify_equivalence=True,
        )
    )

    print("== tt_um_8bitALU (sequential) ==")
    results.append(
        validate_tt_design(
            "tt_um_8bitALU",
            "tt_um_8bitALU",
            ["ALU_test.v"],
            kind="sequential",
            verify_equivalence=False,
            submodules=[],
            seq_depth=12,
        )
    )

    print("== tt_um_LFSR_shivam (sequential) ==")
    results.append(
        validate_tt_design(
            "tt_um_LFSR_shivam",
            "tt_um_LFSR_shivam",
            ["tt_um_LFSR.v"],
            kind="sequential",
            verify_equivalence=False,
            submodules=[],
            seq_depth=12,
        )
    )

    print("== tt_um_CKPope_top (sequential) ==")
    results.append(
        validate_tt_design(
            "tt_um_CKPope_top",
            "tt_um_CKPope_top",
            [
                "tt_um_CKPope_top.v",
                "Compx1.v",
                "Compx4.v",
                "Mealy_SM.v",
                "input_synch.v",
                "target_reg.v",
                "ud_counter.v",
            ],
            kind="sequential",
            verify_equivalence=False,
            submodules=[
                "Compx1",
                "Compx4",
                "Mealy_SM",
                "input_synch",
                "target_reg",
                "ud_counter",
            ],
            seq_depth=16,
        )
    )

    out_path = CORPUS_DIR / "results.json"
    out_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
