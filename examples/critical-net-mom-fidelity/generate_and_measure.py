#!/usr/bin/env python3
"""Epic #709 Phase 2c (issue #978): measure Phase 2's coupling +
distributed-RC extraction fidelity against `klt mom`'s Method-of-Moments
field solver (Epic #701), quantifying the gain over Phase 1's lumped-RC
baseline -- per the epic's reality-grounding discipline ("extraction
accuracy is graded against MoM, not asserted").

## Why a purpose-built fixture, not an existing corpus layout

The natural first candidates -- `examples/design-pipeline/06-layout.gds`
(the `sky130-ota-5t` canary already wired into the `sim/` evidence
convention, #973) and every `tests/corpus/sky130/*.gds` standard cell -- were
tried first (`klt extract --parasitics --critical-net <every non-power net>`
against each) and every one reports `total_coupling_capacitance_ff == 0.0`:
none of their named nets have same-layer facing geometry within sky130's own
`metal_sidewall_lookback_um` (issue #976's own deck-curated lookback
distance). That is itself a genuine, if unhelpful, finding -- there is
nothing to grade Phase 2a against on those fixtures -- so this script builds
the smallest fixture that *does* exercise `--critical-net`'s lateral-coupling
pass with a known, non-zero answer: **the identical geometry
`tests/test_extract.py`'s own `_make_lateral_coupling_layout()` uses** for
issue #976's acceptance test (`test_lateral_coupling_exact_value_and_
lookback_cutoff`), reproduced here (not imported -- `tests/` is not a
package this script can import from) so this survey's own baseline number
(0.044 fF) is checked against the exact same already-shipped, already-tested
deck coefficient math, not a fresh geometry this script would have to
re-derive the expected answer for from scratch.

Three same-layer (met1) nets:

- `AGR` ("aggressor"): a 2x1 um met1 bar at x=[0, 2].
- `VIC` ("victim"): a 2x1 um met1 bar at x=[2.1, 4.1] -- a 0.1 um gap from
  `AGR`, *inside* sky130's met1 lookback (`metal_sidewall_lookback_um[1]` =
  0.28 um) with a fully-facing 1 um edge.
- `FAR`: a 2x1 um met1 bar at x=[4.45, 6.45] -- a 0.35 um gap from `VIC`,
  *outside* that lookback.

## Method

For the `AGR`/`VIC` pair (Phase 2a's own worked example) and the `VIC`/`FAR`
pair (the lookback-cutoff case), this script runs:

1. **Phase 1 baseline** (`run_extract(..., parasitics=True)`, no
   `critical_nets`): lateral coupling is not modelled at all -- `0.0` fF for
   both pairs, by construction (see `docs/cli/extract.md`'s "Lateral
   (same-layer, sidewall) coupling capacitance" section).
2. **Phase 2a** (`critical_nets=["VIC"]`, issue #976): sky130's curated
   `defaultsidewall` coefficient for the facing-edge length within lookback
   -- `AGR`/`VIC` couples (0.044 fF, the deck's own closed-form answer);
   `VIC`/`FAR` does not (outside lookback, stays `0.0` fF).
3. **Phase 2b** (`critical_nets=["VIC"], distributed_rc=True`, issue #977):
   run for completeness and to check a specific claim this survey's own
   write-up makes explicitly -- that the distributed RC ladder redistributes
   an unchanged total R/C across a net's own terminals and does **not**
   change net-to-net coupling capacitance at all (`total_capacitance_ff`/
   `total_coupling_capacitance_ff` are asserted identical to Phase 2a's own
   values below). This fixture has zero device terminals on any net (pure
   geometry + labels, matching `_make_lateral_coupling_layout()`'s own
   docstring), so `--distributed-rc` itself falls back to the star model
   here (fewer than 2 terminals, `docs/cli/extract.md`'s own documented
   tolerance) -- the assertion is checked on `dfxtp_2`'s corpus fixture
   instead (see this script's own `_assert_distributed_rc_preserves_
   capacitance()`), which does have real device terminals.
4. **MoM ground truth** (`klayout_tools.mom.solve_capacitance_matrix`,
   direct two-conductor solve, no GDS/spec-file round trip -- the same
   in-memory API `klt extract --mom-net`'s own crosscheck (issue #798) is
   built on): both bars modelled as zero-thickness plates in the same z=0
   plane (`z0_um == z1_um == 0.0` for both), `background_permittivity=3.9`
   (SiO2, matching `--mom-net`'s own `MOM_CROSSCHECK_BACKGROUND_
   PERMITTIVITY`), swept across panel sizes to a converged estimate (see
   "Convergence" below) with no synthesized ground plate.

**Scope, stated plainly -- no synthesized ground plate.** `--mom-net`'s own
crosscheck (issue #798) places a synthesized ground plate beneath a net at
a z-gap inverted from the deck's own `cap_area_ff_um2` coefficient (about
1.34 um for sky130 met1 -- `_mom_crosscheck_gap_um(0.02578, 3.9)`). Sizing
panels finely enough to resolve this fixture's 0.1 um lateral gap (`panel_
size_um <= gap / 4 = 0.025`) while also covering a ground plate padded to
that ~1.34 um vertical gap's own 3x-padding convention blows the panel count
well past `klt mom`'s 8000-panel safety guard (roughly 175,000 panels at
that combination -- measured directly attempting it during this survey's
own development). This script therefore solves an **isolated two-conductor**
system: `AGR`/`VIC` (or `VIC`/`FAR`) with no third grounded conductor at
all. This is a real, stated idealisation, not a hidden one -- see "What this
does and does not tell you" in the accompanying design doc
(`docs/design/critical-net-mom-fidelity-phase2c.md`) for what it means for
the numbers below.

**Convergence.** Each pair is solved at four panel sizes (halving each
time, capped by the same 8000-panel guard) -- see the `mesh_refinement`
list in the written evidence record. The finest panel size for each pair is
reported as the "converged" MoM estimate; see the design doc for the
measured convergence trend.

## Output

Writes (all committed alongside this script, all reproducible by re-running
it):

- `lateral-coupling.gds` -- the fixture itself.
- `phase1-baseline.json` / `.spice`, `phase2a-critical-net.json` / `.spice`,
  `phase2b-distributed-rc.json` / `.spice` -- the three `klt extract` runs'
  full JSON reports and written netlists.
- `../../evidence/sim/sky130-critical-net-fixture/mom-coupling-fidelity/
  <recorded_at>-<script_sha>.json` -- the quantified comparison, wrapped in
  this repo's `evidence-record/1` shape
  (`docs/design/sim-evidence-discipline-spike.md`). This is not a `klt
  sim`/`klt pex` record (there is no testbench here -- this is a `klt
  extract` + `klt mom` comparison), so `request_path`/`request_sha256` pin
  *this script* (the thing that fully determines the fixture and every
  number below) rather than a `klt sim` request file; `pdk_pin` and
  `extraction_pin` are populated the same way a `klt pex` record's are, from
  the Phase 2a run's own `provenance` block. This is the documented
  "a repo is free to extend the convention as long as its guarantees hold"
  allowance that same design doc's "Non-goals" section states explicitly.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

import klayout.db as kdb

from klayout_tools.extract import run_extract
from klayout_tools.mom import solve_capacitance_matrix

HERE = os.path.dirname(os.path.abspath(__file__))
EVIDENCE_DIR = os.path.join(
    HERE,
    "..",
    "..",
    "evidence",
    "sim",
    "sky130-critical-net-fixture",
    "mom-coupling-fidelity",
)

#: Matches `tests/test_extract.py`'s `_make_lateral_coupling_layout()`
#: exactly (same layer, same box coordinates in nm) -- see this module's
#: docstring for why.
_MET1_LAYER = (68, 20)
_MET1_LABEL_DATATYPE = 5


def _make_lateral_coupling_layout(top_name: str = "TOP") -> kdb.Layout:
    layout = kdb.Layout()
    top = layout.create_cell(top_name)

    def draw(box):
        top.shapes(layout.layer(*_MET1_LAYER)).insert(box)

    def label(text, x, y):
        top.shapes(layout.layer(_MET1_LAYER[0], _MET1_LABEL_DATATYPE)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    draw(kdb.Box(0, 0, 2000, 1000))  # met1, net AGR
    label("AGR", 1000, 500)
    draw(kdb.Box(2100, 0, 4100, 1000))  # met1, net VIC -- 0.1 um gap
    label("VIC", 3100, 500)
    draw(kdb.Box(4450, 0, 6450, 1000))  # met1, net FAR -- 0.35 um gap
    label("FAR", 5450, 500)

    return layout


def _coupling_between(by_net: dict, a: str, b: str) -> float:
    for entry in by_net[a]["coupled"]:
        if entry["net"] == b:
            return entry["capacitance_ff"]
    return 0.0


def _mom_pair_convergence(
    box_a: dict, box_b: dict, panel_sizes: list[float]
) -> list[dict]:
    points = []
    for panel_size_um in panel_sizes:
        response = solve_capacitance_matrix(
            [
                {"name": "a", "boxes": [box_a]},
                {"name": "b", "boxes": [box_b]},
            ],
            background_permittivity=3.9,
            panel_size_um=panel_size_um,
        )
        idx = {name: i for i, name in enumerate(response["conductors"])}
        coupling_ff = -response["capacitance_matrix_ff"][idx["a"]][idx["b"]]
        points.append(
            {
                "panel_size_um": panel_size_um,
                "panel_count": response["panel_count"],
                "coupling_ff": round(coupling_ff, 6),
                "warnings": response["warnings"],
            }
        )
    return points


def _delta_pct(value: float, reference: float) -> float | None:
    if reference == 0:
        return None
    return round(100.0 * (value - reference) / abs(reference), 3)


def _assert_distributed_rc_preserves_capacitance() -> dict:
    """Confirms, on a fixture with real device terminals
    (`tests/corpus/sky130/sky130_fd_sc_hd__dfxtp_2.gds`), that
    `--distributed-rc` (Phase 2b) redistributes an unchanged total R/C
    across a net's own terminals rather than changing net-to-net coupling
    or total capacitance at all -- the claim this script's docstring makes
    about why Phase 2b has no separate capacitance number for `klt mom` to
    grade. Returns the two runs' `total_capacitance_ff`/`total_coupling_
    capacitance_ff` for the evidence record.
    """
    fixture = os.path.join(
        HERE, "..", "..", "tests", "corpus", "sky130", "sky130_fd_sc_hd__dfxtp_2.gds"
    )
    critical_nets = ["$2", "$4", "$5", "$7", "$8", "D"]
    lumped = run_extract(
        fixture,
        "sky130",
        output=os.path.join(HERE, "dfxtp2-lumped.spice"),
        parasitics=True,
        critical_nets=critical_nets,
    )["parasitics"]
    distributed = run_extract(
        fixture,
        "sky130",
        output=os.path.join(HERE, "dfxtp2-distributed.spice"),
        parasitics=True,
        critical_nets=critical_nets,
        distributed_rc=True,
    )["parasitics"]
    assert lumped["total_capacitance_ff"] == distributed["total_capacitance_ff"]
    assert (
        lumped["total_coupling_capacitance_ff"]
        == distributed["total_coupling_capacitance_ff"]
    )
    return {
        "fixture": "tests/corpus/sky130/sky130_fd_sc_hd__dfxtp_2.gds",
        "critical_nets": critical_nets,
        "lumped_total_capacitance_ff": lumped["total_capacitance_ff"],
        "distributed_total_capacitance_ff": distributed["total_capacitance_ff"],
        "lumped_total_coupling_capacitance_ff": lumped["total_coupling_capacitance_ff"],
        "distributed_total_coupling_capacitance_ff": distributed[
            "total_coupling_capacitance_ff"
        ],
        "conclusion": (
            "identical -- --distributed-rc changes how a net's own R/C is "
            "spatially distributed across its terminals, never the total "
            "capacitance (ground or coupling) reported for it, so there is "
            "no separate capacitance number Phase 2b changes for `klt mom` "
            "to grade against; its fidelity contribution is topological "
            "(delay distribution along a routed net), outside the scope of "
            "a capacitance-only field solve"
        ),
    }


def main() -> None:
    layout_path = os.path.join(HERE, "lateral-coupling.gds")
    layout = _make_lateral_coupling_layout()
    layout.write(layout_path)

    phase1 = run_extract(
        layout_path,
        "sky130",
        output=os.path.join(HERE, "phase1-baseline.spice"),
        parasitics=True,
    )
    phase2a = run_extract(
        layout_path,
        "sky130",
        output=os.path.join(HERE, "phase2a-critical-net.spice"),
        parasitics=True,
        critical_nets=["VIC"],
    )
    phase2b = run_extract(
        layout_path,
        "sky130",
        output=os.path.join(HERE, "phase2b-distributed-rc.spice"),
        parasitics=True,
        critical_nets=["VIC"],
        distributed_rc=True,
    )
    for label_, report in (
        ("phase1-baseline", phase1),
        ("phase2a-critical-net", phase2a),
        ("phase2b-distributed-rc", phase2b),
    ):
        with open(os.path.join(HERE, f"{label_}.json"), "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)

    by_net_1 = {e["net"]: e for e in phase1["parasitics"]["nets"]}
    by_net_2a = {e["net"]: e for e in phase2a["parasitics"]["nets"]}

    agr_vic_panel_sizes = [0.1, 0.05, 0.025, 0.0225]
    vic_far_panel_sizes = [0.1, 0.0875, 0.04375, 0.025]

    agr_vic_mom = _mom_pair_convergence(
        {
            "x0_um": 0.0,
            "y0_um": 0.0,
            "x1_um": 2.0,
            "y1_um": 1.0,
            "z0_um": 0.0,
            "z1_um": 0.0,
        },
        {
            "x0_um": 2.1,
            "y0_um": 0.0,
            "x1_um": 4.1,
            "y1_um": 1.0,
            "z0_um": 0.0,
            "z1_um": 0.0,
        },
        agr_vic_panel_sizes,
    )
    vic_far_mom = _mom_pair_convergence(
        {
            "x0_um": 2.1,
            "y0_um": 0.0,
            "x1_um": 4.1,
            "y1_um": 1.0,
            "z0_um": 0.0,
            "z1_um": 0.0,
        },
        {
            "x0_um": 4.45,
            "y0_um": 0.0,
            "x1_um": 6.45,
            "y1_um": 1.0,
            "z0_um": 0.0,
            "z1_um": 0.0,
        },
        vic_far_panel_sizes,
    )

    agr_vic_mom_ff = agr_vic_mom[-1]["coupling_ff"]
    vic_far_mom_ff = vic_far_mom[-1]["coupling_ff"]

    agr_vic_phase1_ff = (
        _coupling_between(by_net_1, "AGR", "VIC") if "AGR" in by_net_1 else 0.0
    )
    agr_vic_phase2a_ff = _coupling_between(by_net_2a, "AGR", "VIC")
    vic_far_phase1_ff = 0.0
    vic_far_phase2a_ff = (
        _coupling_between(by_net_2a, "VIC", "FAR") if "FAR" in by_net_2a else 0.0
    )

    distributed_rc_check = _assert_distributed_rc_preserves_capacitance()

    comparison = {
        "AGR_VIC": {
            "gap_um": 0.1,
            "within_lookback": True,
            "phase1_lumped_rc_coupling_ff": agr_vic_phase1_ff,
            "phase2a_critical_net_coupling_ff": agr_vic_phase2a_ff,
            "mom_coupling_ff": agr_vic_mom_ff,
            "phase1_delta_pct_vs_mom": _delta_pct(agr_vic_phase1_ff, agr_vic_mom_ff),
            "phase2a_delta_pct_vs_mom": _delta_pct(agr_vic_phase2a_ff, agr_vic_mom_ff),
        },
        "VIC_FAR": {
            "gap_um": 0.35,
            "within_lookback": False,
            "phase1_lumped_rc_coupling_ff": vic_far_phase1_ff,
            "phase2a_critical_net_coupling_ff": vic_far_phase2a_ff,
            "mom_coupling_ff": vic_far_mom_ff,
            "phase1_delta_pct_vs_mom": _delta_pct(vic_far_phase1_ff, vic_far_mom_ff),
            "phase2a_delta_pct_vs_mom": _delta_pct(vic_far_phase2a_ff, vic_far_mom_ff),
        },
    }

    layout_sha256 = hashlib.sha256(open(layout_path, "rb").read()).hexdigest()
    with open(__file__, "rb") as fh:
        script_sha256 = hashlib.sha256(fh.read()).hexdigest()

    result = {
        "schema_version": 1,
        "kind": "critical-net-mom-fidelity-comparison",
        "epic": 709,
        "issue": 978,
        "fixture": {
            "layout": "lateral-coupling.gds",
            "layout_sha256": layout_sha256,
            "description": (
                "three same-layer (met1) 2x1 um bars -- AGR/VIC 0.1 um apart "
                "(within sky130's met1 lateral-coupling lookback), VIC/FAR "
                "0.35 um apart (outside it) -- identical geometry to "
                "tests/test_extract.py's _make_lateral_coupling_layout(), "
                "issue #976's own acceptance-test fixture"
            ),
        },
        "extraction": {
            "phase1_baseline": {
                "critical_nets": phase1["parasitics"]["critical_nets"],
                "total_coupling_capacitance_ff": phase1["parasitics"][
                    "total_coupling_capacitance_ff"
                ],
                "provenance": phase1["provenance"],
            },
            "phase2a_critical_net": {
                "critical_nets": phase2a["parasitics"]["critical_nets"],
                "total_coupling_capacitance_ff": phase2a["parasitics"][
                    "total_coupling_capacitance_ff"
                ],
                "provenance": phase2a["provenance"],
            },
            "phase2b_distributed_rc": {
                "critical_nets": phase2b["parasitics"]["critical_nets"],
                "distributed_rc": phase2b["parasitics"]["distributed_rc"],
                "total_coupling_capacitance_ff": phase2b["parasitics"][
                    "total_coupling_capacitance_ff"
                ],
                "note": (
                    "this fixture has zero device terminals on any net, so "
                    "--distributed-rc falls back to the star/lumped model "
                    "here (fewer than 2 terminals) -- see "
                    "distributed_rc_capacitance_invariance below for the "
                    "same claim checked on a fixture that does have real "
                    "device terminals"
                ),
            },
        },
        "distributed_rc_capacitance_invariance": distributed_rc_check,
        "mom_crosscheck": {
            "method": (
                "klt mom (Method of Moments), direct in-memory two-conductor "
                "solve (klayout_tools.mom.solve_capacitance_matrix, no GDS/"
                "spec-file round trip) -- both bars as zero-thickness plates "
                "in the same z=0 plane, background_permittivity=3.9 (SiO2), "
                "no synthesized ground plate (see this script's own "
                "docstring, 'Scope, stated plainly', for why)"
            ),
            "background_permittivity": 3.9,
            "AGR_VIC_mesh_refinement": agr_vic_mom,
            "VIC_FAR_mesh_refinement": vic_far_mom,
        },
        "comparison": comparison,
    }

    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    recorded_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    record_name = f"{recorded_at}-{script_sha256[:7]}.json"
    record = {
        "schema": "evidence-record/1",
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "block": "sky130-critical-net-fixture",
        "scope": "mom-coupling-fidelity",
        "request_path": "examples/critical-net-mom-fidelity/generate_and_measure.py",
        "request_sha256": script_sha256,
        "subset_reason": None,
        "pdk_pin": None,
        "extraction_pin": {
            "method": "coupling-C (Phase 2a, issue #976) vs lumped-RC (Phase 1)",
            "deck": phase2a["provenance"]["deck"]["name"],
            "deck_content_hash": phase2a["provenance"]["deck"]["content_hash"],
        },
        "supersedes": [],
        "note": (
            "not a klt sim/klt pex record -- this is a klt extract + klt mom "
            "comparison (issue #978, Epic #709 Phase 2c), so request_path/"
            "request_sha256 pin this generation script (which fully "
            "determines the fixture and every number in 'result') rather "
            "than a klt sim request file; pdk_pin is null because this "
            "fixture's geometry is authored directly (no --pdk resolution), "
            "matching examples/design-pipeline's own precedent for a run "
            "with no PDK variant selected."
        ),
        "result": result,
    }

    record_path = os.path.join(EVIDENCE_DIR, record_name)
    with open(record_path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)
    with open(os.path.join(EVIDENCE_DIR, "HEAD"), "w", encoding="utf-8") as fh:
        fh.write(record_name + "\n")

    print(f"wrote {record_path}")
    print(json.dumps(comparison, indent=2))


if __name__ == "__main__":
    main()
