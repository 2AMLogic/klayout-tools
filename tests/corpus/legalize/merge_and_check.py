#!/usr/bin/env python3
"""Issue #784's LVS/DRC-shaped acceptance checks, run against the real
corpus fixtures -- see `compare.py` for the overlap/HPWL/wall-clock table
and this script's own stdout for the rest.

What this actually checks, stated precisely (both DEF variants here are
**pre-route** -- captured/legalized before OpenROAD's own `cts`/`route`
stages ever run, since this spike is placement-only per its own "Not in
Scope" -- see the module docstring below for why that is the right,
honest scope for each check):

1. **DRC**, real: merges each legalized DEF (OpenROAD's own oracle output,
   and this crate's Rust output) into a GDS via `place_and_route.py`'s own
   `_merge_def_to_gds` (the exact DEF->GDS merge production `klt place-
   and-route` uses), then runs real `klt drc --deck sky130` against each
   merged, **placement-only** (no routing-layer shapes yet -- this stage
   only exercises cell-to-cell placement DRC: N/P-well spacing, licon
   enclosure, tap-cell spacing) GDS. Comparing the Rust-legalized variant's
   violation count against the *same-stage* OpenROAD-legalized variant's
   own count (not the fully-routed corpus fixture's count, which also
   reflects `cts`/`route`) is the honest apples-to-apples comparison.

2. **Connectivity invariance** (this spike's actual stand-in for "LVS
   matches"): a full post-route LVS round-trip needs a real `cts`+`route`
   pass this placement-only spike deliberately does not run (Epic #700
   Phase 1's own "Not in Scope": routing is Phase 3). What issue #784's
   acceptance criteria are actually testing for -- "legalization changes
   geometry only, never connectivity" -- is checked more directly here:
   `native/legalize/src/def.rs`'s own DEF reader/writer (see that module's
   own docstring) is structurally incapable of touching `NETS`/`PINS`/
   `SPECIALNETS`, since it never parses them at all, only passes that text
   through byte-for-byte. This script verifies that guarantee held in
   practice by diffing the `NETS`/`PINS` sections of the frozen GP input
   against the Rust-legalized output -- byte-identical is the expected
   (and, by construction, guaranteed) result.
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from klayout_tools.drc import run_drc  # noqa: E402
from klayout_tools.pdk import find_pdk, lef_files  # noqa: E402
from klayout_tools.place_and_route import _merge_def_to_gds  # noqa: E402

CORPUS_DIR = os.path.dirname(os.path.abspath(__file__))
DESIGNS = ("gcd", "modexp")
CELL_LIBRARY = "sky130_fd_sc_hd"


def _read_text(path: str) -> str:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return handle.read()


def _section(text: str, begin: str, end: str) -> str:
    start = text.index(begin)
    stop = text.index(end, start) + len(end)
    return text[start:stop]


def _run_drc(gds_path: str) -> dict:
    return run_drc(gds_path, "sky130")


def main() -> int:
    pdk_info = find_pdk(variant="sky130A")
    lefs = lef_files(CELL_LIBRARY, variant=pdk_info["variant"], root=pdk_info["root"])
    tech_lef, cell_lef = lefs["tech_lef"], lefs["cell_lef"]

    results = []
    for design in DESIGNS:
        design_dir = os.path.join(CORPUS_DIR, design)
        gp_def_gz = os.path.join(design_dir, f"{design}_gp.def.gz")
        oracle_def_gz = os.path.join(design_dir, f"{design}_oracle_legal.def.gz")

        gp_text = _read_text(gp_def_gz)
        oracle_text = _read_text(oracle_def_gz)

        rust_def_path = os.path.join(design_dir, f"{design}_rust_legal.def")
        binary = os.path.join(
            CORPUS_DIR, "..", "..", "..", "native", "legalize", "target", "release", "klt-legalize"
        )
        tech_lef_gz = os.path.join(CORPUS_DIR, "sky130_fd_sc_hd_tech.lef.gz")
        cell_lef_gz = os.path.join(CORPUS_DIR, "sky130_fd_sc_hd_cells_used.lef.gz")
        subprocess.run(
            [binary, "legalize", gp_def_gz, tech_lef_gz, cell_lef_gz, "unithd", rust_def_path],
            check=True,
            capture_output=True,
        )
        rust_text = _read_text(rust_def_path)

        # --- connectivity invariance -----------------------------------
        gp_nets = _section(gp_text, "\nNETS ", "END NETS")
        rust_nets = _section(rust_text, "\nNETS ", "END NETS")
        gp_pins = _section(gp_text, "\nPINS ", "END PINS")
        rust_pins = _section(rust_text, "\nPINS ", "END PINS")
        connectivity_unchanged = gp_nets == rust_nets and gp_pins == rust_pins

        # --- DRC, both variants, placement-only (pre-route) GDS ---------
        drc_counts = {}
        for label, def_path, def_text in (
            ("oracle", oracle_def_gz, oracle_text),
            ("rust", rust_def_path, rust_text),
        ):
            plain_def_path = os.path.join(design_dir, f"{design}_{label}_placement_only.def")
            with open(plain_def_path, "w", encoding="utf-8") as handle:
                handle.write(def_text)
            gds_path = os.path.join(design_dir, f"{design}_{label}_placement_only.gds")
            _merge_def_to_gds(
                def_path=plain_def_path,
                tech_lef=tech_lef,
                cell_lef=cell_lef,
                pdk_info=pdk_info,
                cell_library=CELL_LIBRARY,
                hdl_toplevel=design,
                macros=[],
                out_path=gds_path,
            )
            drc = _run_drc(gds_path)
            drc_counts[label] = {
                "violation_count": drc["violation_count"],
                "rule_ids": sorted({v["rule"] for v in drc["violations"]}),
            }
            os.remove(plain_def_path)
            os.remove(gds_path)

        os.remove(rust_def_path)

        results.append(
            {
                "design": design,
                "connectivity_unchanged": connectivity_unchanged,
                "drc_violation_count": drc_counts,
            }
        )

    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
