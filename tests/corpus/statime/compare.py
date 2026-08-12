#!/usr/bin/env python3
"""Issue #809's own comparison harness: runs this spike's native-Rust STA
engine (`native/statime`'s `klt-statime critical-path` binary, built fresh
here) against each corpus design's checked-in mapped netlist
(`<design>_netlist.v`) and the live-resolved `sky130_fd_sc_hd` liberty, then
diffs the reported worst-path delay against the OpenSTA oracle numbers
captured in `oracle_results.json` -- the accuracy half of the go/no-go
comparison table `native/statime/README.md` reports. Every delta below is
computed by this script against real numbers; nothing here is asserted from
memory.

Usage (from the repo root -- `uv run` so `klayout_tools` resolves as an
installed package, matching this repo's other corpus scripts):

    uv run python3 tests/corpus/statime/compare.py [--format json]

Needs a real, volare-fetched `sky130A` PDK install (`klt pdk find` must
resolve it) -- the liberty file itself is too large (~13 MB) to check into
this repo, per `docs/design-evidence-tiers.md`'s own corpus-size discipline
(mirrors `tests/corpus/legalize/README.md`'s equivalent note about LEF).
Does **not** need Docker/OpenROAD to run -- `oracle_results.json` is a
checked-in snapshot of a real OpenSTA run; see that file and README.md
"Reproducing the OpenSTA oracle numbers" for how to regenerate it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
CRATE_DIR = os.path.join(REPO_ROOT, "native", "statime")
CORPUS_DIR = os.path.dirname(os.path.abspath(__file__))
DESIGNS = ("gcd", "mult8", "modexp")

INPUT_TRANSITION_NS = 0.05
OUTPUT_LOAD_PF = 0.03


def _ensure_binary() -> str:
    binary = os.path.join(CRATE_DIR, "target", "release", "klt-statime")
    if not os.path.isfile(binary):
        print("Building klt-statime (cargo build --release)...", file=sys.stderr)
        subprocess.run(["cargo", "build", "--release"], cwd=CRATE_DIR, check=True)
    return binary


def _resolve_liberty() -> str:
    sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
    from klayout_tools.pdk import find_pdk  # noqa: E402

    pdk = find_pdk(variant="sky130A")
    libs_ref = pdk["assets"]["libs_ref"]
    path = os.path.join(
        libs_ref, "sky130_fd_sc_hd", "lib", "sky130_fd_sc_hd__tt_025C_1v80.lib"
    )
    if not os.path.isfile(path):
        raise SystemExit(f"liberty not found at {path} -- is sky130A really installed?")
    return path


def run_design(binary: str, liberty: str, design: str) -> dict:
    netlist = os.path.join(CORPUS_DIR, f"{design}_netlist.v")
    out_json = os.path.join(CORPUS_DIR, f".{design}_rust_result.json")
    t0 = time.monotonic()
    subprocess.run(
        [
            binary,
            "critical-path",
            netlist,
            liberty,
            "--top",
            design,
            "--input-transition-ns",
            str(INPUT_TRANSITION_NS),
            "--output-load-pf",
            str(OUTPUT_LOAD_PF),
            "--json",
            out_json,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    wall_ms = (time.monotonic() - t0) * 1000.0
    with open(out_json, encoding="utf-8") as fh:
        data = json.load(fh)
    os.remove(out_json)
    data["_wall_ms"] = wall_ms
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    binary = _ensure_binary()
    liberty = _resolve_liberty()

    with open(os.path.join(CORPUS_DIR, "oracle_results.json"), encoding="utf-8") as fh:
        oracle = json.load(fh)["designs"]

    rows = []
    for design in DESIGNS:
        result = run_design(binary, liberty, design)
        worst = result["result"]["worst_path"]
        oracle_ns = oracle[design]["worst_path_delay_ns"]
        rust_ns = worst["delay_ns"]
        delta_pct = (oracle_ns - rust_ns) / oracle_ns * 100.0
        rows.append(
            {
                "design": design,
                "cells": result["result"]["num_cells"],
                "oracle_ns": oracle_ns,
                "rust_ns": round(rust_ns, 4),
                "delta_pct": round(delta_pct, 2),
                "rust_startpoint": worst["startpoint"],
                "rust_endpoint": worst["endpoint"],
                "oracle_startpoint": oracle[design]["startpoint"],
                "oracle_endpoint": oracle[design]["endpoint"],
                "timings_ms": result["timings_ms"],
                "wall_ms": round(result["_wall_ms"], 1),
            }
        )

    if args.format == "json":
        print(json.dumps(rows, indent=2))
        return 0

    header = (
        f"{'design':<8} {'cells':>6} {'oracle (ns)':>12} "
        f"{'rust (ns)':>10} {'delta %':>8} {'wall (ms)':>10}"
    )
    print(header)
    print("-" * 60)
    for row in rows:
        print(
            f"{row['design']:<8} {row['cells']:>6} {row['oracle_ns']:>12.4f} "
            f"{row['rust_ns']:>10.4f} {row['delta_pct']:>7.2f}% {row['wall_ms']:>10.1f}"
        )
    print()
    for row in rows:
        rust_side = f"rust path {row['rust_startpoint']} -> {row['rust_endpoint']}"
        oracle_side = (
            f"oracle path {row['oracle_startpoint']} -> {row['oracle_endpoint']}"
        )
        print(f"{row['design']}: {rust_side} | {oracle_side}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
