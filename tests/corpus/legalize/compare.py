#!/usr/bin/env python3
"""Issue #784's own comparison harness: legalizes each corpus design's
frozen global-placement DEF (`<design>_gp.def.gz`) two ways -- OpenROAD's
own `detailed_placement` (already captured by `regenerate.sh` as
`<design>_oracle_legal.def.gz`) and this repo's new Rust Abacus legalizer
(`native/legalize`'s `klt-legalize legalize` binary, built fresh here) --
then reports overlap count, total displacement, HPWL, and wall-clock for
both, as the go/no-go comparison table Epic #700 Phase 1's acceptance
criteria ask for. Every number below is measured by this script, run
against the real checked-in corpus fixtures -- nothing here is asserted
from memory.

Usage (from the repo root):

    python3 tests/corpus/legalize/compare.py [--format json]

No PDK/OpenROAD/Docker required to *run* this script -- it only reads the
already-generated, checked-in `tests/corpus/legalize/` fixtures and drives
the Rust binary (`cargo build --release` runs automatically if the binary
is missing). Regenerating those fixtures from scratch is a separate,
deliberate step: `tests/corpus/legalize/regenerate.sh`.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import subprocess
import sys
import time

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
LEGALIZE_CRATE = os.path.join(REPO_ROOT, "native", "legalize")
CORPUS_DIR = os.path.dirname(os.path.abspath(__file__))
DESIGNS = ("gcd", "modexp")

_COMPONENT_RE = re.compile(r"^\s*-\s+(\S+)\s+\S+\s+\+\s+PLACED\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)")


def _read_text(path: str) -> str:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return handle.read()


def _component_positions(def_text: str) -> dict[str, tuple[int, int]]:
    positions: dict[str, tuple[int, int]] = {}
    in_components = False
    for line in def_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("COMPONENTS "):
            in_components = True
            continue
        if stripped == "END COMPONENTS":
            break
        if not in_components:
            continue
        match = _COMPONENT_RE.match(line)
        if match:
            positions[match.group(1)] = (int(match.group(2)), int(match.group(3)))
    return positions


def _total_displacement_um(gp_text: str, legal_text: str, dbu: int) -> tuple[float, float]:
    gp_pos = _component_positions(gp_text)
    legal_pos = _component_positions(legal_text)
    total_dbu = 0
    max_dbu = 0
    for inst, (x0, y0) in gp_pos.items():
        x1, y1 = legal_pos.get(inst, (x0, y0))
        d = abs(x1 - x0) + abs(y1 - y0)
        total_dbu += d
        max_dbu = max(max_dbu, d)
    return total_dbu / dbu, max_dbu / dbu


def _dbu(def_text: str) -> int:
    for line in def_text.splitlines():
        if "UNITS DISTANCE MICRONS" in line:
            return int(line.split()[-2])
    raise ValueError("no UNITS DISTANCE MICRONS line")


def _build_rust_binary() -> str:
    binary = os.path.join(LEGALIZE_CRATE, "target", "release", "klt-legalize")
    if not os.path.isfile(binary):
        subprocess.run(["cargo", "build", "--release"], cwd=LEGALIZE_CRATE, check=True)
    return binary


def _run_json(args: list[str]) -> dict:
    completed = subprocess.run(args, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {completed.stderr}")
    return json.loads(completed.stdout)


def compare_design(binary: str, design: str) -> dict:
    design_dir = os.path.join(CORPUS_DIR, design)
    gp_def_gz = os.path.join(design_dir, f"{design}_gp.def.gz")
    oracle_def_gz = os.path.join(design_dir, f"{design}_oracle_legal.def.gz")
    tech_lef_gz = os.path.join(CORPUS_DIR, "sky130_fd_sc_hd_tech.lef.gz")
    cell_lef_gz = os.path.join(CORPUS_DIR, "sky130_fd_sc_hd_cells_used.lef.gz")
    gen_metrics_path = os.path.join(design_dir, f"{design}_gen_metrics.json")

    with open(gen_metrics_path, encoding="utf-8") as handle:
        gen_metrics = json.load(handle)

    gp_text = _read_text(gp_def_gz)
    oracle_text = _read_text(oracle_def_gz)
    dbu = _dbu(gp_text)

    rust_out_def = os.path.join(design_dir, f"{design}_rust_legal.def")
    t0 = time.monotonic()
    rust_legalize_result = _run_json(
        [binary, "legalize", gp_def_gz, tech_lef_gz, cell_lef_gz, "unithd", rust_out_def]
    )
    rust_process_wallclock_ms = (time.monotonic() - t0) * 1000.0

    rust_metrics = _run_json(
        [binary, "metrics", rust_out_def, tech_lef_gz, cell_lef_gz, "unithd"]
    )
    oracle_metrics = _run_json(
        [binary, "metrics", oracle_def_gz, tech_lef_gz, cell_lef_gz, "unithd"]
    )
    gp_metrics = _run_json([binary, "metrics", gp_def_gz, tech_lef_gz, cell_lef_gz, "unithd"])

    oracle_total_um, oracle_max_um = _total_displacement_um(gp_text, oracle_text, dbu)

    os.remove(rust_out_def)

    return {
        "design": design,
        "component_count": rust_metrics["component_count"],
        "gp_overlap_count": gp_metrics["overlap_count"],
        "gp_hpwl_um": gp_metrics["hpwl_um"],
        "oracle": {
            "overlap_count": oracle_metrics["overlap_count"],
            "hpwl_um": oracle_metrics["hpwl_um"],
            "total_displacement_um": round(oracle_total_um, 3),
            "max_displacement_um": round(oracle_max_um, 3),
            "detailed_placement_wallclock_ms": gen_metrics[
                "oracle_detailed_placement_wallclock_ms"
            ],
        },
        "rust": {
            "overlap_count": rust_metrics["overlap_count"],
            "hpwl_um": rust_metrics["hpwl_um"],
            "total_displacement_um": rust_legalize_result["total_displacement_um"],
            "max_displacement_um": rust_legalize_result["max_displacement_um"],
            "safety_net_moves": rust_legalize_result["safety_net_moves"],
            "legalize_wallclock_ms": rust_legalize_result["legalize_wallclock_ms"],
            "process_wallclock_ms": round(rust_process_wallclock_ms, 3),
        },
        "delta_vs_oracle": {
            "hpwl_pct": round(
                100.0 * (rust_metrics["hpwl_um"] - oracle_metrics["hpwl_um"]) / oracle_metrics["hpwl_um"],
                2,
            ),
            "total_displacement_pct": round(
                100.0
                * (rust_legalize_result["total_displacement_um"] - oracle_total_um)
                / oracle_total_um,
                2,
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    binary = _build_rust_binary()
    results = [compare_design(binary, design) for design in DESIGNS]

    if args.format == "json":
        print(json.dumps(results, indent=2))
        return 0

    print(
        f"{'design':<8} {'engine':<8} {'overlaps':<9} {'hpwl_um':<11} "
        f"{'disp_um':<10} {'wallclock_ms':<13}"
    )
    for r in results:
        print(
            f"{r['design']:<8} {'oracle':<8} {r['oracle']['overlap_count']:<9} "
            f"{r['oracle']['hpwl_um']:<11.1f} {r['oracle']['total_displacement_um']:<10.1f} "
            f"{r['oracle']['detailed_placement_wallclock_ms']:<13}"
        )
        print(
            f"{r['design']:<8} {'rust':<8} {r['rust']['overlap_count']:<9} "
            f"{r['rust']['hpwl_um']:<11.1f} {r['rust']['total_displacement_um']:<10.1f} "
            f"{r['rust']['legalize_wallclock_ms']:<13.3f}"
        )
        print(
            f"         delta:   hpwl {r['delta_vs_oracle']['hpwl_pct']:+.1f}%   "
            f"displacement {r['delta_vs_oracle']['total_displacement_pct']:+.1f}%"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
