#!/usr/bin/env python3
"""Reproduce docs/design/yosys-synthesis-spike.md §4's GCD worked example
against a pinned Yosys build + a `sky130_fd_sc_hd` liberty file, and assert
the reported cell_count/area/cell-type breakdown match the spike exactly.

This closes the CI-reproducibility gap the spike flagged in §3.4/§3.5
(issue #417): CI must not rely on `apt-get install yosys` (Ubuntu 24.04's
apt package is ~30 releases stale relative to the spike's build). See
scripts/fetch-yosys.sh for how the pinned Yosys build is installed, and
.github/workflows/ci.yml's Yosys-pin job for how the pinned
`sky130_fd_sc_hd` liberty is fetched.

`klt synthesize` itself (the verb wrapping this exact invocation sequence)
is issue #416, not yet implemented -- this script invokes Yosys directly
via `yosys -s <script>`, matching the spike's own invocation surface
(docs/design/yosys-synthesis-spike.md §1), not through `klt`.

Usage:
    python3 scripts/verify-yosys-pin.py <path-to-sky130_fd_sc_hd__tt_025C_1v80.lib> \
        [--yosys-bin yosys]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GCD_RTL = REPO_ROOT / "examples" / "synthesis" / "gcd.v"

# Exact expected output from docs/design/yosys-synthesis-spike.md §4,
# produced there with Yosys 0.67+post (Homebrew) against open_pdks commit
# bdc9412b3e468c102d01b7cf6337be06ec6e9c9a. Re-verified byte-for-byte against
# the pinned CI build (yowasp-yosys 0.67.0.0.post1190, upstream Yosys 0.67
# release tag) while implementing #417 -- zero drift found; if that ever
# changes (a future pin bump, a liberty update), this assertion is what
# should fail and be updated deliberately, not silently pass.
EXPECTED_NUM_CELLS = 335
EXPECTED_AREA_UM2 = 2951.5808
EXPECTED_SEQUENTIAL_AREA_UM2 = 1251.2
EXPECTED_CELLS_BY_TYPE = {
    "sky130_fd_sc_hd__a211o_1": 1,
    "sky130_fd_sc_hd__a211oi_1": 1,
    "sky130_fd_sc_hd__a21boi_0": 2,
    "sky130_fd_sc_hd__a21o_1": 5,
    "sky130_fd_sc_hd__a21oi_1": 30,
    "sky130_fd_sc_hd__a22oi_1": 1,
    "sky130_fd_sc_hd__a311oi_1": 6,
    "sky130_fd_sc_hd__a31oi_1": 7,
    "sky130_fd_sc_hd__and2_0": 5,
    "sky130_fd_sc_hd__and3_1": 2,
    "sky130_fd_sc_hd__clkinv_1": 4,
    "sky130_fd_sc_hd__dfrtp_1": 50,
    "sky130_fd_sc_hd__lpflow_inputiso1p_1": 3,
    "sky130_fd_sc_hd__lpflow_isobufsrc_1": 16,
    "sky130_fd_sc_hd__maj3_1": 3,
    "sky130_fd_sc_hd__mux2_1": 16,
    "sky130_fd_sc_hd__mux2i_1": 2,
    "sky130_fd_sc_hd__nand2_1": 35,
    "sky130_fd_sc_hd__nand2b_1": 18,
    "sky130_fd_sc_hd__nand3_1": 9,
    "sky130_fd_sc_hd__nand4_1": 3,
    "sky130_fd_sc_hd__nor2_1": 36,
    "sky130_fd_sc_hd__nor2b_1": 1,
    "sky130_fd_sc_hd__nor3_1": 6,
    "sky130_fd_sc_hd__nor3b_1": 1,
    "sky130_fd_sc_hd__nor4_1": 3,
    "sky130_fd_sc_hd__o211ai_1": 3,
    "sky130_fd_sc_hd__o21a_1": 3,
    "sky130_fd_sc_hd__o21ai_0": 32,
    "sky130_fd_sc_hd__o22ai_1": 1,
    "sky130_fd_sc_hd__o311ai_0": 5,
    "sky130_fd_sc_hd__o31ai_1": 3,
    "sky130_fd_sc_hd__or3_1": 2,
    "sky130_fd_sc_hd__or4_1": 1,
    "sky130_fd_sc_hd__xnor2_1": 13,
    "sky130_fd_sc_hd__xor2_1": 6,
}

# The exact synthesis script from docs/design/yosys-synthesis-spike.md §1/§4.
SYNTH_SCRIPT_TEMPLATE = """\
read_verilog gcd.v
hierarchy -check -top gcd
synth -top gcd
dfflibmap -liberty {liberty}
abc -liberty {liberty}
clean
tee -q -o stats.json stat -liberty {liberty} -json -top gcd
write_json gcd_netlist.json
write_verilog -noattr gcd_synth.v
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "liberty",
        type=Path,
        help="Path to a fetched sky130_fd_sc_hd__tt_025C_1v80.lib",
    )
    parser.add_argument(
        "--yosys-bin",
        default="yosys",
        help="Yosys executable to invoke (default: yosys)",
    )
    args = parser.parse_args()

    liberty = args.liberty.resolve()
    if not liberty.is_file():
        print(f"error: liberty file not found: {liberty}", file=sys.stderr)
        return 1
    if not GCD_RTL.is_file():
        print(f"error: GCD RTL fixture not found: {GCD_RTL}", file=sys.stderr)
        return 1

    yosys_bin = shutil.which(args.yosys_bin)
    if yosys_bin is None:
        print(f"error: '{args.yosys_bin}' not found on PATH", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="klt-yosys-pin-check-") as tmpdir:
        workdir = Path(tmpdir)
        shutil.copy(GCD_RTL, workdir / "gcd.v")
        script_path = workdir / "synth_gcd.ys"
        script_path.write_text(SYNTH_SCRIPT_TEMPLATE.format(liberty=liberty))

        result = subprocess.run(
            [yosys_bin, "-s", "synth_gcd.ys"],
            cwd=workdir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"error: {yosys_bin} exited {result.returncode}", file=sys.stderr)
            print(result.stdout[-4000:], file=sys.stderr)
            print(result.stderr[-4000:], file=sys.stderr)
            return 1

        stats_path = workdir / "stats.json"
        if not stats_path.is_file():
            print(
                "error: stats.json was not written -- "
                "the 'tee -q -o' stat pass did not run",
                file=sys.stderr,
            )
            return 1

        stats = json.loads(stats_path.read_text())
        module = stats.get("modules", {}).get("\\gcd")
        if module is None:
            found = sorted(stats.get("modules", {}))
            print(
                f"error: stats.json has no '\\\\gcd' module entry: {found}",
                file=sys.stderr,
            )
            return 1

        failures = []

        actual_num_cells = module["num_cells"]
        if actual_num_cells != EXPECTED_NUM_CELLS:
            failures.append(
                f"num_cells: expected {EXPECTED_NUM_CELLS}, got {actual_num_cells}"
            )

        actual_area = module["area"]
        if actual_area != EXPECTED_AREA_UM2:
            failures.append(f"area: expected {EXPECTED_AREA_UM2}, got {actual_area}")

        actual_seq_area = module["sequential_area"]
        if actual_seq_area != EXPECTED_SEQUENTIAL_AREA_UM2:
            failures.append(
                f"sequential_area: expected {EXPECTED_SEQUENTIAL_AREA_UM2}, "
                f"got {actual_seq_area}"
            )

        actual_by_type = module["num_cells_by_type"]
        if actual_by_type != EXPECTED_CELLS_BY_TYPE:
            all_keys = set(actual_by_type) | set(EXPECTED_CELLS_BY_TYPE)
            diffs = {
                k: {
                    "expected": EXPECTED_CELLS_BY_TYPE.get(k),
                    "actual": actual_by_type.get(k),
                }
                for k in sorted(all_keys)
                if EXPECTED_CELLS_BY_TYPE.get(k) != actual_by_type.get(k)
            }
            failures.append(f"num_cells_by_type drift: {diffs}")

        if failures:
            print(
                "DRIFT DETECTED between the pinned CI Yosys build and "
                "docs/design/yosys-synthesis-spike.md §4:",
                file=sys.stderr,
            )
            for failure in failures:
                print(f"  - {failure}", file=sys.stderr)
            return 1

        print(
            f"OK: {yosys_bin} reproduces "
            "docs/design/yosys-synthesis-spike.md §4 exactly -- "
            f"num_cells={actual_num_cells}, area={actual_area} um2, "
            f"sequential_area={actual_seq_area} um2"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
