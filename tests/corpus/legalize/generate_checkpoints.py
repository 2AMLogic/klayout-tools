#!/usr/bin/env python3
"""Generate a real, pre-legalization "global-placement" DEF checkpoint plus
OpenROAD's own oracle-legalized DEF for one synthesized netlist -- run
**inside** a real OpenROAD process (`openroad -no_init -exit <script.tcl>`),
never mocked.

This intentionally does **not** reuse `place_and_route.py`'s own
`run_place_and_route`/`_stage_script_lines` (issue #784's Curator-comment
enrichment point 3: "there is no point in the current flow where `write_def`
happens *between* `global_placement` and `detailed_placement`" -- production
never needs this checkpoint, so there is nothing to intercept). Instead this
is a bespoke, one-off Tcl script -- built the same way
`tests/corpus/place_and_route/regenerate.sh` builds the production fixture,
just with one extra `write_def` spliced in before `detailed_placement` --
that reuses `place_and_route.py`'s own *path-resolution* helpers
(`_resolve_liberty`/`_resolve_lef`) plus its exact `floorplan`/`place` stage
Tcl lines (`_floorplan_init_lines`/`_clock_lines`) so the flow this script
runs is provably the same one `klt place-and-route` runs in production,
line for line, up to the one deliberate `write_def` insertion.

Usage (matches `tests/corpus/place_and_route/regenerate.sh`'s own
docker/volare/PDK_ROOT setup -- see `regenerate.sh` in this directory):

    python3 generate_checkpoints.py \\
        --netlist <synthesized.v> --toplevel gcd \\
        --cell-library sky130_fd_sc_hd --corner tt_025C_1v80 \\
        --utilization 38 --aspect-ratio 1.0 --core-margin 2.0 --site unithd \\
        --io-h met3 --io-v met2 \\
        --clock-port clk --clock-period-ns 1.1 --seed 1 \\
        --output-dir <dir>

Writes, under ``--output-dir``:

- ``<toplevel>_gp.def`` -- the frozen global-placement solution, captured
  immediately after ``repair_timing`` and before ``detailed_placement`` --
  i.e. exactly the state OpenROAD's own `detailed_placement` legalizes in
  production (`place_and_route.py`'s ``place`` stage runs
  `repair_design`/`repair_timing` before `detailed_placement` too), so this
  is a faithful "one real global-placement solution", not a simplified one.
- ``<toplevel>_oracle_legal.def`` -- OpenROAD's own `detailed_placement`
  result on that identical input (the oracle for this spike's comparison).
- ``<toplevel>_tech.lef`` / ``<toplevel>_cell.lef`` -- copies of the
  resolved tech/cell LEF this run actually used (needed downstream by the
  Rust legalizer and by the harness's own DEF/LEF parsing -- redistributed
  under the same Apache-2.0 terms `tests/corpus/README.md` already
  documents for the rest of this repo's sky130 corpus).
- ``<toplevel>_gen_metrics.json`` -- wall-clock + basic provenance
  (measured, not asserted): the `detailed_placement` command's own
  wall-clock (isolated via a `clock milliseconds` pair bracketing just that
  one Tcl command, not the whole process), the resolved OpenROAD version,
  and the resolved PDK/liberty/LEF paths used.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from klayout_tools.place_and_route import (  # noqa: E402
    _GLOBAL_PLACEMENT_DENSITY,
    _clock_lines,
    _floorplan_init_lines,
    _openroad_version,
    _resolve_lef,
    _resolve_liberty,
    _validate_floorplan,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--netlist", required=True)
    parser.add_argument("--toplevel", required=True)
    parser.add_argument("--cell-library", required=True)
    parser.add_argument("--corner", required=True)
    parser.add_argument("--utilization", type=float, required=True)
    parser.add_argument("--aspect-ratio", type=float, required=True)
    parser.add_argument("--core-margin", type=float, required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--io-h", required=True)
    parser.add_argument("--io-v", required=True)
    parser.add_argument("--clock-port", required=True)
    parser.add_argument("--clock-period-ns", type=float, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--pdk-variant", default=None)
    parser.add_argument("--pdk-root", default=None)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    os.makedirs(args.output_dir, exist_ok=True)

    liberty_path, corner, pdk_info = _resolve_liberty(
        args.cell_library, args.corner, variant=args.pdk_variant, root=args.pdk_root
    )
    tech_lef, cell_lef = _resolve_lef(args.cell_library, pdk_info)

    floorplan = _validate_floorplan(
        {
            "method": "utilization",
            "utilization_pct": args.utilization,
            "aspect_ratio": args.aspect_ratio,
            "core_margin_um": args.core_margin,
            "site": args.site,
        }
    )

    gp_def = os.path.join(args.output_dir, f"{args.toplevel}_gp.def")
    oracle_def = os.path.join(args.output_dir, f"{args.toplevel}_oracle_legal.def")
    checkpoint_db = os.path.join(args.output_dir, f"{args.toplevel}_oracle_legal.odb")

    lines: list[str] = [
        f"read_liberty {liberty_path}",
        f"read_lef {tech_lef}",
        f"read_lef {cell_lef}",
        f"read_verilog {args.netlist}",
        f"link_design {args.toplevel}",
    ]
    lines += _clock_lines(args.clock_port, args.clock_period_ns)
    lines += _floorplan_init_lines(floorplan)
    lines += ["make_tracks"]
    lines += [
        f"place_pins -hor_layers {args.io_h} -ver_layers {args.io_v}",
        f"set_wire_rc -layer {args.io_v}",
        f"global_placement -density {_GLOBAL_PLACEMENT_DENSITY} "
        f"-routability_driven -timing_driven -random_seed {args.seed}",
        "estimate_parasitics -placement",
        "repair_design",
        "repair_timing",
        # This is the ONE line production's own `place` stage never has --
        # everything above and everything below is byte-identical to
        # `_stage_script_lines`'s own `floorplan` + `place` branches.
        f"write_def {gp_def}",
        'puts "LEGALIZE_TIMING_BEGIN"',
        "set _klt_spike_t0 [clock milliseconds]",
        "detailed_placement",
        "set _klt_spike_t1 [clock milliseconds]",
        'puts "LEGALIZE_WALLCLOCK_MS [expr {$_klt_spike_t1 - $_klt_spike_t0}]"',
        'puts "LEGALIZE_TIMING_END"',
        "estimate_parasitics -placement",
        "report_worst_slack_metric -setup",
        "report_tns_metric -setup",
        "report_design_area_metrics",
        "report_fmax_metric",
        f"write_def {oracle_def}",
        f"write_db {checkpoint_db}",
    ]

    script_path = os.path.join(args.output_dir, f"{args.toplevel}_gen_checkpoints.tcl")
    with open(script_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    t_wall0 = time.monotonic()
    completed = subprocess.run(
        ["openroad", "-no_init", "-exit", script_path],
        capture_output=True,
        text=True,
    )
    t_wall1 = time.monotonic()

    print(completed.stdout)
    print(completed.stderr, file=sys.stderr)

    if completed.returncode != 0:
        print(f"error: openroad exited {completed.returncode}", file=sys.stderr)
        return 1

    legalize_ms = None
    for line in completed.stdout.splitlines():
        if line.startswith("LEGALIZE_WALLCLOCK_MS "):
            legalize_ms = int(line.split()[1])

    if not os.path.isfile(gp_def) or not os.path.isfile(oracle_def):
        print("error: expected DEF checkpoints were not written", file=sys.stderr)
        return 1

    shutil.copy(tech_lef, os.path.join(args.output_dir, f"{args.toplevel}_tech.lef"))
    shutil.copy(cell_lef, os.path.join(args.output_dir, f"{args.toplevel}_cell.lef"))

    metrics = {
        "toplevel": args.toplevel,
        "cell_library": args.cell_library,
        "corner": corner,
        "seed": args.seed,
        "openroad_version": _openroad_version(),
        "pdk_variant": pdk_info["variant"],
        "pdk_root": pdk_info["root"],
        "liberty_path": liberty_path,
        "tech_lef": tech_lef,
        "cell_lef": cell_lef,
        "oracle_detailed_placement_wallclock_ms": legalize_ms,
        "full_process_wallclock_s": round(t_wall1 - t_wall0, 3),
        "gp_def": gp_def,
        "oracle_legal_def": oracle_def,
    }
    metrics_path = os.path.join(args.output_dir, f"{args.toplevel}_gen_metrics.json")
    with open(metrics_path, "w", encoding="utf-8") as handle:
        json.dump(metrics, handle, indent=2)
        handle.write("\n")

    print(f"wrote {gp_def}")
    print(f"wrote {oracle_def}")
    print(f"wrote {metrics_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
