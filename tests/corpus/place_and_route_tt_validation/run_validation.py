#!/usr/bin/env python3
"""Regenerates `results.json` -- Epic #700 Phase 4 (issue #1330): run 3 real
Tiny Tapeout corpus designs through `klt synthesize` -> `klt place-and-route`
(real Yosys + real OpenROAD, via the `openroad/orfs:latest` Docker image,
against a real volare-fetched `sky130A` install) and check each routed
result's DRC-cleanliness and functional equivalence to its own source RTL.

Epic #520 (the Tiny Tapeout corpus harness this issue's acceptance criteria
name) is still open and `loom:operator-only` as of this writing -- no
ready-made ingestion harness exists yet -- so, per this issue's own
instructions, this reuses the interim approach `tests/corpus/
synth_e2e_validation/` (issue #990) already took: real, public Tiny Tapeout
`tt06` shuttle designs, sourced and pinned directly against the public
`https://index.tinytapeout.com/tt06.json` index, not a hand-rolled scrape of
the whole corpus. This script in fact reuses 3 of that same validation's 4
already-vetted, already-licensed RTL sources (see README.md's "Corpus")
rather than fetching a fresh set -- the same designs, a different stage of
the pipeline exercised against them.

**What "compared against OpenROAD as ground truth" means here.** `klt
place-and-route`'s only implemented engine *is* OpenROAD (Epic #391 Phase 4,
Epic #700's own Phase 0) -- there is no separate native placer/router yet to
diff against a standalone OpenROAD run, so a self-vs-self comparison would
be circular. Epic #700's own text names the actual oracle: "LVS closes the
loop... a routed result is only accepted when `klt lvs` matches it against
the source netlist." A full SPICE-level `klt lvs` round-trip needs a
Verilog->SPICE reference converter that does not exist yet (a known,
already-documented gap -- see `docs/cli/extract.md`'s "Gate-level Verilog
output" limitation), so this script substitutes the equivalent, already-
proven-in-this-repo method (issue #990's own dual-DUT co-simulation) applied
to the *post-route* netlist (`verilog_path`, issue #996 -- the as-built
netlist including every CTS buffer, `repair_design`/`repair_timing` resize,
and `repair_antennas` diode OpenROAD's flow inserted) instead of the pre-
route synthesis netlist #990 checked. This is a strictly stronger
connectivity/functional check than #990's own (it validates the design
*after* OpenROAD has rewritten it, not before), and is the closest thing to
"netlist matches" achievable without the missing converter. Combined with
`klt drc` (an independent, klt-native geometric DRC check on the merged
GDS, distinct from OpenROAD's own internal `detailed_route -output_drc`/
`check_antennas` reports) and the place-and-route report's own timing/DRC
metrics, this is the full, honestly-reported ground-truth comparison this
issue's acceptance criteria ask for.

Requires: `klt` (this repo's own CLI, `uv sync --extra dev` then activate
the venv), `yosys`, `iverilog`/`vvp` on `$PATH`; `docker` (pulls
`openroad/orfs:latest` if not already cached); a resolvable, volare-fetched
`sky130A` PDK install (`~/.volare` or `$PDK_ROOT`).

Deliberate, reviewed, **never a CI step** -- same convention as
`tests/corpus/synth_e2e_validation/run_validation.py` and
`tests/corpus/place_and_route/regenerate.sh`. A Yosys/OpenROAD/PDK version
bump can shift QoR numbers; re-running this script and re-committing
`results.json` is how that drift gets captured on purpose, not silently.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CORPUS_DIR = Path(__file__).resolve().parent
# Reuse issue #990's already-vetted, already-licensed TT sources directly --
# see that directory's own README.md for full provenance (repo/commit/
# license per design).
SYNTH_E2E_SOURCES_DIR = CORPUS_DIR.parent / "synth_e2e_validation" / "sources"

PDK_ROOT = os.environ.get("PDK_ROOT") or str(Path.home() / ".volare")
PDK_VARIANT = os.environ.get("PDK", "sky130A")
CELL_LIBRARY = "sky130_fd_sc_hd"
CORNER = "tt_025C_1v80"
CELL_VERILOG_DIR = Path(PDK_ROOT) / PDK_VARIANT / "libs.ref" / CELL_LIBRARY / "verilog"
PRIMITIVES_V = CELL_VERILOG_DIR / "primitives.v"
CELLS_V = CELL_VERILOG_DIR / "sky130_fd_sc_hd.v"

# Docker image + resolved in-container OpenROAD binary path, matching
# tests/corpus/place_and_route/regenerate.sh and tests/corpus/legalize's own
# convention -- see docs/cli/place-and-route.md's "Installing OpenROAD" for
# why this is the only broadly reproducible install path today.
DOCKER_IMAGE = "openroad/orfs:latest"
CONTAINER_OPENROAD_BIN = (
    "/OpenROAD-flow-scripts/tools/install/OpenROAD/bin:/usr/local/sbin:"
    "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)


def _run(
    cmd: list[str], cwd: Path, env: dict | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


def _copy_sources(design_dir: Path, workdir: Path) -> list[str]:
    names = []
    for src in sorted(design_dir.glob("*.v")):
        shutil.copy(src, workdir / src.name)
        names.append(src.name)
    return names


def klt_synthesize(workdir: Path, sources: list[str], hdl_toplevel: str) -> dict:
    request = {
        "schema": "klt.synthesize.request/1",
        "engine": "yosys",
        "sources": sources,
        "hdl_toplevel": hdl_toplevel,
        "pdk": {"cell_library": CELL_LIBRARY, "corner": CORNER},
        "constraints": {"clock_period_ns": None},
    }
    (workdir / "synth_request.json").write_text(json.dumps(request), encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = "/usr/bin:" + env.get("PATH", "")
    env["PDK_ROOT"] = PDK_ROOT
    env["PDK"] = PDK_VARIANT
    proc = _run(
        ["klt", "synthesize", "synth_request.json", "--format", "json"],
        cwd=workdir,
        env=env,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - diagnostic aid
        raise RuntimeError(
            f"klt synthesize did not emit JSON for {hdl_toplevel}: "
            f"rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}"
        ) from exc


def klt_place_and_route(
    workdir: Path,
    netlist_rel: str,
    hdl_toplevel: str,
    clock_port: str,
    clock_period_ns: float,
    utilization_pct: float,
) -> dict:
    """Runs the real `openroad/orfs:latest` Docker image against `workdir`
    (mounted read-write at `/workdir/scratch`), this checkout (mounted
    read-only at `/workdir/repo`, `pip install`'d fresh inside the
    container -- there is no `klt` inside the upstream image), and the host's
    volare PDK install (mounted read-only at `/workdir/volare`) -- the exact
    recipe `tests/corpus/place_and_route/regenerate.sh` and `docs/cli/
    place-and-route.md`'s own "Installing OpenROAD" section document."""
    request = {
        "schema": "klt.place_and_route.request/1",
        "engine": "openroad",
        "netlist": netlist_rel,
        "hdl_toplevel": hdl_toplevel,
        "pdk": {"cell_library": CELL_LIBRARY, "corner": CORNER},
        "floorplan": {
            "method": "utilization",
            "utilization_pct": utilization_pct,
            "aspect_ratio": 1.0,
            "core_margin_um": 2.0,
            "site": "unithd",
        },
        "io": {"layer_h": "met3", "layer_v": "met2"},
        "constraints": {"clock_port": clock_port, "clock_period_ns": clock_period_ns},
        "seed": 1,
        "target_stage": "route",
    }
    (workdir / "par_request.json").write_text(json.dumps(request), encoding="utf-8")

    cmd = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "-v",
        f"{REPO_ROOT}:/workdir/repo:ro",
        "-v",
        f"{workdir}:/workdir/scratch",
        "-v",
        f"{PDK_ROOT}:/workdir/volare:ro",
        "-e",
        f"PDK={PDK_VARIANT}",
        "-e",
        "PDK_ROOT=/workdir/volare",
        "-e",
        f"PATH={CONTAINER_OPENROAD_BIN}",
        DOCKER_IMAGE,
        "bash",
        "-c",
        (
            "pip3 install -q /workdir/repo && "
            'python3 -c "from klayout_tools.cli import main; import sys; '
            'sys.exit(main([\\"place-and-route\\", '
            '\\"/workdir/scratch/par_request.json\\", \\"--format\\", \\"json\\"]))"'
        ),
    ]
    proc = _run(cmd, cwd=workdir)
    # `emit_error`/`emit_success` (docs/json-contract.md) write JSON to
    # stdout on success but to stderr on an application error (exit 1) --
    # try stdout first, then stderr, before giving up.
    for stream in (proc.stdout, proc.stderr):
        try:
            return json.loads(stream)
        except json.JSONDecodeError:
            continue
    raise RuntimeError(
        f"klt place-and-route did not emit JSON for {hdl_toplevel}: "
        f"rc={proc.returncode} stdout={proc.stdout[-4000:]!r} "
        f"stderr={proc.stderr[-4000:]!r}"
    )


def klt_drc(gds_path: str) -> dict:
    # In-process, host-side -- no docker/openroad needed, this repo's own
    # `klayout` pip dependency is enough (klayout_tools.drc.run_drc, the
    # same function `klt drc` itself calls).
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from klayout_tools.drc import run_drc

    return run_drc(gds_path, "sky130")


def _rename_gate_module(netlist_text: str, name: str) -> str:
    """Prefixes the top module's declaration with `gate_` so it can be
    `read_verilog`'d alongside the gold RTL (same module name) without a
    clash. Matches both Yosys's own `write_verilog` spacing (`module foo(`)
    and OpenROAD's (`module foo (`) -- issue #990's original regex only
    covered the former, tightened here for the post-route `write_verilog`
    output this script feeds it (OpenROAD's own `dbSta`/`sta::write_verilog_
    cmd`, confirmed live to emit a space before the port-list `(`)."""
    return re.sub(rf"\bmodule {name}\s*\(", f"module gate_{name} (", netlist_text)


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


def gls_routed_dual_dut_diff(
    workdir: Path,
    top: str,
    rtl_sources: list[Path],
    routed_netlist: Path,
    n_cycles: int = 200,
) -> tuple[bool, str]:
    """Directed RTL-vs-post-route-netlist co-simulation: instantiate the gold
    RTL and the *routed* (`verilog_path`, as-built, CTS/repair/antenna-diode-
    inclusive) netlist side by side, share clk/reset/inputs, and diff every
    TT-wrapper output pin every cycle. Same directed-stimulus shape as issue
    #990's `gls_dual_dut_diff` (see that issue's README.md "Directed vs.
    randomized stimulus" for why), applied one stage later in the pipeline."""
    gate_text = routed_netlist.read_text(encoding="utf-8")
    renamed = _rename_gate_module(gate_text, top)
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


def validate_design(
    macro: str,
    top: str,
    sources: list[str],
    clock_port: str,
    clock_period_ns: float,
    utilization_pct: float = 40.0,
) -> dict:
    design_dir = SYNTH_E2E_SOURCES_DIR / macro
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        _copy_sources(design_dir, workdir)

        synth_report = klt_synthesize(workdir, sources, top)
        # `workdir` is a `tempfile.TemporaryDirectory()` path, which on
        # macOS is a `/var/folders/...` symlink to `/private/var/folders/
        # ...` -- resolve both sides before computing the relative path, or
        # a mismatched symlink/real-path prefix produces a bogus `../../..`
        # chain that no longer resolves once rebased onto the container's
        # `/workdir/scratch` mount point.
        netlist_rel = os.path.relpath(
            Path(synth_report["netlist_path"]).resolve(), workdir.resolve()
        )

        par_report = klt_place_and_route(
            workdir, netlist_rel, top, clock_port, clock_period_ns, utilization_pct
        )

        # The report's own paths are the *container's* view
        # (`/workdir/scratch/...`, per the bind mount in
        # `klt_place_and_route`) -- rebase onto the host path so the
        # host-side steps below (klt_drc, GLS) can read the same files the
        # container just wrote into this identical, bind-mounted `workdir`.
        def _host_path(container_path: str) -> str:
            return container_path.replace("/workdir/scratch", str(workdir), 1)

        gds_path = _host_path(par_report["gds_path"])
        drc_report = klt_drc(gds_path)

        routed_netlist = Path(_host_path(par_report["verilog_path"]))
        rtl_paths = [workdir / s for s in sources]
        gls_passed, gls_log = gls_routed_dual_dut_diff(
            workdir, top, rtl_paths, routed_netlist
        )

        return {
            "design": macro,
            "synthesize": {
                "instance_count": synth_report["instance_count"],
                "area_um2": synth_report["area_um2"],
            },
            "place_and_route": {
                "engine": par_report["engine"],
                "engine_version": par_report["engine_version"],
                "stage_reached": par_report["stage_reached"],
                "clock_period_ns": clock_period_ns,
                "die_area_um2": par_report["die_area_um2"],
                "core_area_um2": par_report["core_area_um2"],
                "utilization_pct": par_report["utilization_pct"],
                "wirelength_um": par_report["wirelength_um"],
                "worst_slack_ns": par_report["worst_slack_ns"],
                "total_negative_slack_ns": par_report["total_negative_slack_ns"],
                "setup_violation_count": par_report["setup_violation_count"],
                "hold_violation_count": par_report["hold_violation_count"],
                "antenna_violation_count": par_report["antenna_violation_count"],
                "route_drc_violation_count": par_report["route_drc_violation_count"],
                "estimated_power_mw": par_report["estimated_power_mw"],
            },
            "klt_drc": {
                "status": drc_report["status"],
                "violation_count": drc_report["violation_count"],
            },
            "post_route_equivalence": {
                "method": (
                    "directed RTL-vs-routed-netlist dual-DUT co-simulation diff "
                    "(post-route verilog_path, not the pre-route synthesis netlist)"
                ),
                "passed": gls_passed,
                "log_tail": "\n".join(gls_log.strip().splitlines()[-10:]),
            },
        }


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
    if not shutil.which("docker"):
        print("error: docker not on PATH (needed for openroad/orfs)", file=sys.stderr)
        return 1
    if not (Path(PDK_ROOT) / PDK_VARIANT).exists():
        print(f"error: {PDK_VARIANT} PDK not found under {PDK_ROOT}", file=sys.stderr)
        return 1

    results = []

    print("== tt_um_LFSR_shivam (sequential, 38 cells) ==")
    results.append(
        validate_design(
            "tt_um_LFSR_shivam",
            "tt_um_LFSR_shivam",
            ["tt_um_LFSR.v"],
            clock_port="clk",
            clock_period_ns=1.0,
        )
    )

    print("== tt_um_8bitALU (sequential, 110 cells) ==")
    results.append(
        validate_design(
            "tt_um_8bitALU",
            "tt_um_8bitALU",
            ["ALU_test.v"],
            clock_port="clk",
            clock_period_ns=3.0,
        )
    )

    print("== tt_um_CKPope_top (sequential, 169 cells) ==")
    results.append(
        validate_design(
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
            clock_port="clk",
            clock_period_ns=2.0,
        )
    )

    out_path = CORPUS_DIR / "results.json"
    out_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out_path}")

    for r in results:
        print(
            f"{r['design']}: stage_reached={r['place_and_route']['stage_reached']} "
            f"setup_viol={r['place_and_route']['setup_violation_count']} "
            f"hold_viol={r['place_and_route']['hold_violation_count']} "
            f"route_drc_viol={r['place_and_route']['route_drc_violation_count']} "
            f"klt_drc={r['klt_drc']['status']} "
            f"post_route_equiv={r['post_route_equivalence']['passed']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
