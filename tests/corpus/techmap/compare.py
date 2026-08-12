#!/usr/bin/env python3
"""Issue #874's own benchmark: `native/techmap`'s Liberty-driven cell
selection vs. Yosys/`abc`'s own technology mapping, on the same RTL corpus
(gcd, mult8, modexp -- the same 3-design proxy for the #520 Tiny Tapeout
corpus every prior Epic #704 phase measures against: #520 itself is
`loom:operator-only`/not yet ingested, and no dedicated synthesis-corpus
harness exists yet per `docs/design/synthesize-qor-improvements-survey.md`
section 4.1, so this reuses `native/statime`'s own corpus convention
rather than inventing a fourth one).

For each design:
  1. `klt synthesize` (Yosys + `abc`, this repo's shipped default flow, no
     `-constr`/`-D`) -- the oracle's own `instance_count`/`area_um2`, and
     its mapped netlist.
  2. `klt-techmap` (this issue's own crate) against the *same* design's
     checked-in `<design>_generic.json` (the interim on-ramp fixture,
     `docs/design/synth-techmap-stage-contract.md` section 2.4) and the
     same resolved liberty -- this stage's own `instance_count`/`area_um2`.
  3. `klt-statime critical-path` (the accepted native-Rust gate-level STA
     engine, issue #809) against *both* mapped netlists, same liberty, same
     boundary condition -- a single, consistent delay oracle for both
     sides, so the delay comparison isn't confounded by using two
     different STA engines/settings on the two sides.

Every number below is measured fresh in this run -- nothing is asserted
from memory, and results are reported (not asserted) per the epic's own
reality-grounding discipline.

Usage (from the repo root):
    uv run python3 tests/corpus/techmap/compare.py [--format json]
    uv run python3 tests/corpus/techmap/compare.py --delay-driven

Needs a real, volare-fetched `sky130A` PDK install (`klt pdk find` must
resolve it) and a real `yosys` on `$PATH` -- same requirement as
`tests/corpus/statime/compare.py`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
TECHMAP_CRATE_DIR = os.path.join(REPO_ROOT, "native", "techmap")
STATIME_CRATE_DIR = os.path.join(REPO_ROOT, "native", "statime")
CORPUS_DIR = os.path.dirname(os.path.abspath(__file__))
DESIGNS = ("gcd", "mult8", "modexp")

INPUT_TRANSITION_NS = 0.05
OUTPUT_LOAD_PF = 0.03


def _ensure_binary(crate_dir: str, name: str) -> str:
    binary = os.path.join(crate_dir, "target", "release", name)
    if not os.path.isfile(binary):
        print(f"Building {name} (cargo build --release)...", file=sys.stderr)
        subprocess.run(["cargo", "build", "--release"], cwd=crate_dir, check=True)
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


def run_yosys_oracle(design: str, liberty: str, scratch: str) -> dict:
    """`klt synthesize` (Yosys + abc) on the design's own RTL source."""
    src_dir = os.path.join(REPO_ROOT, "examples", "functional-verification")
    src_name = f"{design}.v"
    src_path = os.path.join(src_dir, src_name)
    if not os.path.isfile(src_path):
        src_path = os.path.join(REPO_ROOT, "tests", "corpus", "statime", src_name)
    design_dir = os.path.join(scratch, f"{design}_oracle")
    os.makedirs(design_dir, exist_ok=True)
    shutil.copy(src_path, os.path.join(design_dir, src_name))

    request = {
        "schema": "klt.synthesize.request/1",
        "engine": "yosys",
        "sources": [src_name],
        "hdl_toplevel": design,
        "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
        "constraints": {"clock_period_ns": None},
    }
    request_path = os.path.join(design_dir, f"{design}_synth_request.json")
    with open(request_path, "w", encoding="utf-8") as fh:
        json.dump(request, fh)

    env = dict(os.environ)
    env["PATH"] = "/usr/bin:" + env.get("PATH", "")
    env["PDK"] = "sky130A"
    result = subprocess.run(
        [
            "uv",
            "--project",
            REPO_ROOT,
            "run",
            "klt",
            "synthesize",
            request_path,
            "--format",
            "json",
        ],
        cwd=design_dir,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return json.loads(result.stdout)


def run_techmap(
    binary: str, design: str, liberty: str, scratch: str, clock_period_ns
) -> dict:
    """`klt-techmap` on the design's checked-in `<design>_generic.json`."""
    design_dir = os.path.join(scratch, f"{design}_techmap")
    os.makedirs(design_dir, exist_ok=True)
    generic_src = os.path.join(CORPUS_DIR, f"{design}_generic.json")
    generic_dst = os.path.join(design_dir, f"{design}_generic.json")
    shutil.copy(generic_src, generic_dst)

    request = {
        "schema": "klt.synth.techmap.request/1",
        "generic_netlist": f"{design}_generic.json",
        "liberty": liberty,
        "constraints": {"clock_period_ns": clock_period_ns},
    }
    request_path = os.path.join(design_dir, "request.json")
    with open(request_path, "w", encoding="utf-8") as fh:
        json.dump(request, fh)

    result = subprocess.run(
        [binary, request_path],
        cwd=design_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def run_statime(
    binary: str, netlist_path: str, liberty: str, top: str, scratch: str
) -> dict:
    # `--json` takes a real output path (no stdout mode) --
    # tests/corpus/statime/compare.py's own convention: write to a scratch
    # file, read it back, done.
    out_json = os.path.join(
        scratch, f".{top}_{os.path.basename(netlist_path)}_sta.json"
    )
    subprocess.run(
        [
            binary,
            "critical-path",
            netlist_path,
            liberty,
            "--top",
            top,
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
    with open(out_json, encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument(
        "--delay-driven",
        action="store_true",
        help=(
            "pass a clock_period_ns constraint to klt-techmap "
            "(delay-weighted cell selection) instead of area-only"
        ),
    )
    args = parser.parse_args()

    techmap_binary = _ensure_binary(TECHMAP_CRATE_DIR, "klt-techmap")
    statime_binary = _ensure_binary(STATIME_CRATE_DIR, "klt-statime")
    liberty = _resolve_liberty()

    clock_period_ns = 5.0 if args.delay_driven else None

    rows = []
    with tempfile.TemporaryDirectory(prefix="klt-techmap-bench-") as scratch:
        for design in DESIGNS:
            oracle = run_yosys_oracle(design, liberty, scratch)
            mapped = run_techmap(
                techmap_binary, design, liberty, scratch, clock_period_ns
            )

            oracle_sta = run_statime(
                statime_binary, oracle["netlist_path"], liberty, design, scratch
            )
            mapped_sta = run_statime(
                statime_binary, mapped["mapped_netlist_path"], liberty, design, scratch
            )

            oracle_ns = oracle_sta["result"]["worst_path"]["delay_ns"]
            mapped_ns = mapped_sta["result"]["worst_path"]["delay_ns"]

            rows.append(
                {
                    "design": design,
                    "oracle_instance_count": oracle["instance_count"],
                    "techmap_instance_count": mapped["instance_count"],
                    "instance_count_delta_pct": round(
                        (mapped["instance_count"] - oracle["instance_count"])
                        / oracle["instance_count"]
                        * 100.0,
                        1,
                    ),
                    "oracle_area_um2": oracle["area_um2"],
                    "techmap_area_um2": mapped["area_um2"],
                    "area_delta_pct": round(
                        (mapped["area_um2"] - oracle["area_um2"])
                        / oracle["area_um2"]
                        * 100.0,
                        1,
                    ),
                    "oracle_delay_ns": round(oracle_ns, 4),
                    "techmap_delay_ns": round(mapped_ns, 4),
                    "delay_delta_pct": round(
                        (mapped_ns - oracle_ns) / oracle_ns * 100.0, 1
                    ),
                }
            )

    if args.format == "json":
        print(json.dumps(rows, indent=2))
        return 0

    header = (
        f"{'design':<8} {'cells (oracle/techmap)':>24} {'delta%':>7}  "
        f"{'area um2 (oracle/techmap)':>26} {'delta%':>7}  "
        f"{'delay ns (oracle/techmap)':>26} {'delta%':>7}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        cells = (
            f"{row['oracle_instance_count']:>11}/{row['techmap_instance_count']:<11}"
        )
        area = f"{row['oracle_area_um2']:>12.1f}/{row['techmap_area_um2']:<12.1f}"
        delay = f"{row['oracle_delay_ns']:>12.4f}/{row['techmap_delay_ns']:<12.4f}"
        print(
            f"{row['design']:<8} "
            f"{cells} {row['instance_count_delta_pct']:>+6.1f}%  "
            f"{area} {row['area_delta_pct']:>+6.1f}%  "
            f"{delay} {row['delay_delta_pct']:>+6.1f}%"
        )
    print()
    print(
        "Positive delta% = native/techmap uses more cells/area/delay than "
        "Yosys/abc on the same design (this is the expected direction for a "
        "v1 mapper restricted to the 10-primitive generic vocabulary vs. "
        "ABC's full compound-gate library -- see native/techmap/README.md)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
