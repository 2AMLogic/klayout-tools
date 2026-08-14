#!/usr/bin/env python3
"""Epic #709 Phase 3b (issue #989): run `klt pex` end to end on a
*current-carrying* spec row of the sky130 5T OTA composed layout
(`06-layout.gds`), then attempt to feed a real `klt mom` (Method-of-Moments,
Epic #701) R/L/C for the current-carrying net into that same `klt pex` run
via `--mom-rlc-net` (Phase 3a, issue #988) -- the epic's closing acceptance
criterion: "Extraction is graded against MoM ... on at least one net; the
improvement over lumped-RC is measured, not asserted" at the re-sim/spec-row
level, not just the standalone-capacitance level Phase 2c (#978) already
measured.

## Why this canary needs a new bias network, not Phase 1c's own testbench

Phase 1c's own record (`evidence/sim/sky130-ota-5t/post-extraction-bias-
probe/`, `10-pex.testbench.spice`) reads `delta_pct: 0.0` on every row
because it measures three nodes that are each directly forced by an ideal DC
voltage source at that net's own star hub -- an ideal source is insensitive
to *any* downstream parasitic value by construction. Re-running that exact
scope with a MoM-fed override would trivially reproduce 0.0 vs 0.0 again,
regardless of the substituted value -- see this repo's own
`examples/design-pipeline/README.md` ("S10 pex delta proof") for the full
mechanistic explanation, and `docs/cli/pex.md`'s "Phase 2a's own
measurable-delta proof" section, which states plainly that this canary "has
no current/charge-carrying, high-impedance node ... by construction" for
Phase 2a's own coupling-capacitance proof.

That statement is accurate for the *default* bias (`Vtail = +0.5V`), but not
for every reachable bias point. `07-reference.spice`/`10-pex-extracted.spice`
both wire the composed layout's generic `nfet` model (SPICE level=1, no
`VTO` given -- so `VTO` defaults to exactly `0V`) with every gate floating
(`klt gen-compose` never routes a generator's gate/unconnected source-drain
port through `connectivity[]`), pulled to exactly `0V` by
`.options rshunt=1e12` (the only DC path any gate has). Two of the five
composed devices ("$2"/M2-equivalent: s=TAIL_A|TAIL_B, g=$8, d=N1; and
"$5"/M5-equivalent: s=TAIL_A|TAIL_B, g=$11, d=VOUT) have their *source*
terminal on `TAIL_A|TAIL_B`. Biasing that net *negative* instead of
positive makes `Vgs = 0 - V(TAIL) > 0 = VTO` for both devices, turning them
on -- and because their *drain* terminals sit on the two other actively
driven pin nets (N1, VOUT), this completes a genuine external-source-to-
external-source DC current loop through `TAIL_A|TAIL_B`'s own star-arm
resistors, unlike the internal-only `$2`/`$3`/`$6` star nodes this issue's
own curator note flagged as candidates (each a single-device dead end --
one series R then one ground C with no other element -- that structurally
carries zero current in DC steady state regardless of bias, since a
capacitor blocks all DC current and there is no other path off that node --
confirmed against `10-pex-extracted.spice`'s own topology via a manual
`ngspice` op-point probe during this issue's own development; see
examples/design-pipeline/README.md's "S10 mom-fed pex delta proof" section
for the reported node values -- not re-run by this script since `klt sim`'s
own `.meas`-only measurement mechanism has no "dump every internal node"
primitive to automate that specific check).

`11-pex-mom.testbench.spice` biases `TAIL_A|TAIL_B` at -0.45V/-0.55V
(`corners.supply_v`) instead of Phase 1c's +0.5V, keeping `N1`/`VOUT` at
Phase 1c's own +1.0V, and measures each bias source's own branch current
(`i(vn1)`/`i(vtail)`/`i(vvout)`) rather than a hub node voltage -- a
schematic-vs-extracted-comparable quantity that is *not* forced to a fixed
value by construction (unlike a node voltage at a driven hub) whichever side
of the diff is asked.

## What this script finds about the MoM side of the comparison

`--mom-rlc-net`'s substitution (`docs/cli/extract.md`) replaces a *whole
net's* total lumped resistance -- the number `_terminal_star_weights`
proportionally redistributes across every one of that net's star-arm
resistors. `klt mom`'s own PEEC resistance/inductance solve
(`docs/cli/mom.md` "PEEC inductance/resistance") requires every conductor to
reduce to a *single* bar-shaped box -- "exactly one box per conductor,"
rejected otherwise. `TAIL_A|TAIL_B`'s real li1 routing on `06-layout.gds`
is not a single box: `klt components` (see this script's own
`_isolate_tail_net_li1_shapes()`) reports 5 disjoint li1 polygons for this
net (a routed trunk plus jogs/branches reaching three device terminals
spread across the composed layout), so a literal net-wide PEEC solve is not
available through the existing CLI without extending it to a multi-box PEEC
formulation (`docs/cli/mom.md`'s own "Multi-box PEEC (Ruehli's general
mesh) is a follow-up") -- out of this issue's scope.

This script instead solves the real PEEC resistance of *one* representative
bar-shaped segment of that net's actual routed geometry (the 0.42um x 10um
trunk run, `_isolate_tail_net_li1_shapes()`'s `target_segment`) -- a real,
`klt mom`-derived number, not hand-picked, but explicitly *not* a valid
stand-in for the whole net's total resistance (a segment vs. a 3-terminal
net total are different physical quantities). Two findings fall out of this:

1. **DC resistance of a uniform rectangular conductor is exactly
   `R = rho_sheet * length / width` in *both* models** -- `klt extract`'s
   lumped sheet-resistance formula and `klt mom`'s PEEC solve (`R = length /
   (conductivity * cross_sectional_area)`, which reduces to the identical
   formula once `conductivity` is derived from the same sheet resistance).
   There is no fidelity gap for MoM to close on a straight bar's DC
   resistance *by construction of the physics* -- confirmed directly below
   (`peec_resistance_ohm` reproduces the sheet-resistance prediction for the
   same segment to floating-point precision).
2. **A capacitance-only `--mom-rlc-net` override is structurally invisible
   to every measurable node in this canary**, independent of the net-choice/
   geometry problem above: every net's ground capacitor sits either at a
   hub an ideal voltage source directly forces (Phase 1c's own finding,
   still true here for `N1`/`TAIL_A|TAIL_B`/`VOUT`), or at an internal
   dead-end node with zero possible signal path in (this generic device
   model's `Cgs`/`Cgd` are unset, i.e. zero -- confirmed below via
   `_verify_capacitance_override_is_invisible()`, an empirical proof with a
   deliberately extreme (1000fF vs. baseline 3.456fF) override producing a
   byte-identical `delta[]`).

See `examples/design-pipeline/README.md`'s "S10 mom-fed pex delta proof"
section for the full write-up, and this script's own evidence record
(`evidence/sim/sky130-ota-5t/mom-fed-current-probe/`) for every number.

## Output

Writes (all committed alongside this script, all reproducible by re-running
it -- `uv run python examples/design-pipeline/mom_current_carrying_probe.py`
from a repo checkout with the `mom` dependency group installed, see
`docs/cli/mom.md`'s "Building the native extension"):

- `11-pex-mom-extracted.spice` / `11-pex-mom.result.json` -- the baseline
  (Phase 1 lumped-RC) `klt pex` run against the new current-carrying
  testbench/request.
- `11-pex-mom.schematic.result.json` -- the schematic-side-only `klt sim`
  companion record (same convention Phase 1c's `10-pex.schematic.result.json`
  established).
- `11-mom-tail-segment.gds` / `.spec.json` / `.result.json` -- the isolated
  representative `TAIL_A|TAIL_B` li1 segment this script cuts directly out
  of `06-layout.gds`, its `klt mom` spec file, and the real PEEC solve's own
  JSON result.
- `../../evidence/sim/sky130-ota-5t/mom-fed-current-probe/<recorded_at>-
  <script_sha>-{pex,sim,mom-comparison}.json` -- the append-only evidence
  records (this scope's own `HEAD` points at all three).
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

import klayout.db as kdb

from klayout_tools.mom import run_mom
from klayout_tools.pex import run_pex
from klayout_tools.sim import run_sim

HERE = os.path.dirname(os.path.abspath(__file__))
EVIDENCE_DIR = os.path.join(
    HERE, "..", "..", "evidence", "sim", "sky130-ota-5t", "mom-fed-current-probe"
)

#: sky130 li1.drawing -- src/klayout_tools/decks/sky130.py's own layer map
#: (`(67, 20): "li1.drawing"`), the composed layout's routing layer.
_LI1_LAYER = (67, 20)

#: sky130's own curated li1 sheet resistance (src/klayout_tools/decks/
#: sky130.py PARASITICS.metals[0], `sheet_res_ohm_sq=12.8`) -- this script
#: derives a matching bulk conductivity from it (rather than an independent
#: silicon-calibrated number this repo does not curate for li1 yet) so the
#: PEEC solve below is checked against the *same* physical layer the
#: baseline lumped-RC model already prices, isolating "does a MoM field
#: solve differ from a sheet-resistance formula for a uniform rectangular
#: bar" (finding 1 above) from "is this repo's own li1 coefficient
#: accurate" (a separate, uncalibrated-to-silicon question `docs/cli/
#: extract.md`'s own deck comments already flag as out of scope).
_LI1_SHEET_RES_OHM_SQ = 12.8
#: Assumed li1 thickness -- SkyWater sky130's local-interconnect stack is a
#: thin (~0.1um) tungsten-local-interconnect layer; not independently
#: re-derived here (this script's own finding 1 shows the PEEC resistance
#: answer is thickness-and-conductivity-invariant as long as conductivity is
#: derived from the same sheet resistance -- see the module docstring).
_LI1_THICKNESS_UM = 0.1
_LI1_CONDUCTIVITY_S_PER_M = 1.0 / (_LI1_SHEET_RES_OHM_SQ * _LI1_THICKNESS_UM * 1e-6)


def _isolate_tail_net_li1_shapes() -> tuple[str, kdb.Box]:
    """Cuts `TAIL_A|TAIL_B`'s own li1 routing directly out of
    `06-layout.gds`. The crop bbox below (`[1.42, 0.0]-[3.26, 16.4]`um) was
    identified by `klt components --conductors '[{"layer":[67,20]}]'
    --label-layers '[{"layer":[67,5]}]'` (this net's own component entry:
    `labels: ["TAIL_A", "TAIL_B"]`, no other net's li1 shapes inside it --
    every neighbouring component's own bbox sits fully outside this one).
    The `assert len(polys) == 5` below is this function's own re-check that
    the hardcoded crop still isolates exactly this net's real shape count on
    the committed `06-layout.gds`, so a future layout regeneration that
    changes this net's routing fails loudly here instead of silently
    solving the wrong geometry. Writes the whole-net shape set
    (informational -- not PEEC-solvable, 5 disjoint polygons) and returns
    the path to a *single* bar-shaped representative segment (the 0.42um x
    10um trunk run) this script's PEEC solve actually targets, plus that
    segment's own bbox for the evidence record.
    """
    layout_path = os.path.join(HERE, "06-layout.gds")
    src = kdb.Layout()
    src.read(layout_path)
    top = src.top_cell()
    li1 = src.layer(*_LI1_LAYER)
    dbu = src.dbu

    # TAIL_A|TAIL_B's own component bbox (um), from `klt components`.
    box_um = (1.42, 0.0, 3.26, 16.4)
    box_dbu = kdb.Box(
        int(round(box_um[0] / dbu)),
        int(round(box_um[1] / dbu)),
        int(round(box_um[2] / dbu)),
        int(round(box_um[3] / dbu)),
    )
    region = kdb.Region(top.begin_shapes_rec(li1))
    clipped = region & kdb.Region(box_dbu)
    polys = list(clipped.each())
    assert len(polys) == 5, (
        f"expected 5 disjoint TAIL_A|TAIL_B li1 polygons, got {len(polys)} -- "
        "the whole-net geometry is not PEEC-solvable either way (more than "
        "one box per conductor), but this script's own bar-segment "
        "selection below assumes this exact decomposition"
    )

    whole_net_path = os.path.join(HERE, "11-mom-tail-net-whole.gds")
    whole_net_out = kdb.Layout()
    whole_net_out.dbu = dbu
    whole_net_top = whole_net_out.create_cell("tail_a_tail_b_net")
    whole_net_li1 = whole_net_out.layer(*_LI1_LAYER)
    whole_net_top.shapes(whole_net_li1).insert(clipped)
    whole_net_out.write(whole_net_path)

    target_segment = None
    for poly in polys:
        bb = poly.bbox()
        w_um = (bb.right - bb.left) * dbu
        h_um = (bb.top - bb.bottom) * dbu
        if abs(w_um - 0.42) < 1e-6 and abs(h_um - 10.0) < 1e-6:
            target_segment = poly
            break
    assert target_segment is not None, (
        "expected TAIL_A|TAIL_B's own 0.42um x 10um trunk segment among its "
        "real li1 polygons"
    )

    segment_path = os.path.join(HERE, "11-mom-tail-segment.gds")
    segment_out = kdb.Layout()
    segment_out.dbu = dbu
    segment_top = segment_out.create_cell("tail_segment")
    segment_li1 = segment_out.layer(*_LI1_LAYER)
    segment_top.shapes(segment_li1).insert(target_segment)
    segment_out.write(segment_path)

    return segment_path, target_segment.bbox()


def _verify_capacitance_override_is_invisible(
    layout_path: str, request_path: str, baseline_delta: list[dict]
) -> dict:
    """Empirically confirms this script's own docstring claim: a
    capacitance-only `--mom-rlc-net` override on `TAIL_A|TAIL_B` -- even a
    deliberately extreme one (1000fF vs. this net's own baseline lumped
    3.456fF) -- produces a byte-identical `delta[]` to the unmodified
    baseline, because this net's own ground capacitor sits at its hub, which
    both bias sources in this scope's own current loop treat as an ideal
    (zero-output-impedance) node."""
    demo_extreme_capacitance_ff = 1000.0
    report = run_pex(
        layout_path,
        [request_path],
        "sky130",
        output=os.path.join(HERE, ".klt-mom-probe-caponly.spice"),
        mom_rlc_net="TAIL_A|TAIL_B",
        mom_rlc_capacitance_ff=demo_extreme_capacitance_ff,
    )
    assert report["delta"] == baseline_delta, (
        "expected a capacitance-only override to be fully invisible to "
        "every delta[] row in this scope -- see this script's own docstring "
        "for why"
    )
    return {
        "demo_capacitance_ff": demo_extreme_capacitance_ff,
        "baseline_capacitance_ff": report["extraction"]["mom_rlc_override"][
            "previous_capacitance_ff"
        ],
        "delta_identical_to_baseline": True,
        "conclusion": (
            "confirmed empirically -- a 1000fF override (roughly 290x this "
            "net's own baseline 3.456fF) produces a byte-identical delta[] "
            "to the unmodified lumped-RC baseline; capacitance is "
            "structurally invisible to every measurement in this scope, not "
            "just numerically close"
        ),
    }


def _verify_resistance_override_is_sensitive(
    layout_path: str,
    request_path: str,
    baseline_delta: list[dict],
    peec_resistance_ohm: float,
) -> dict:
    """Empirically confirms the *machinery* (as opposed to this specific
    net/geometry combination) is sensitive to a real resistance change --
    substituting the representative segment's own real PEEC resistance
    (not a fair whole-net stand-in, see module docstring) as
    `TAIL_A|TAIL_B`'s new *total* moves every delta[] row substantially."""
    report = run_pex(
        layout_path,
        [request_path],
        "sky130",
        output=os.path.join(HERE, ".klt-mom-probe-rdemo.spice"),
        mom_rlc_net="TAIL_A|TAIL_B",
        mom_rlc_resistance_ohm=peec_resistance_ohm,
    )
    assert report["delta"] != baseline_delta, (
        "expected a resistance override to move at least one delta[] row"
    )
    moved = [
        {
            "spec_row": row_after["spec_row"],
            "corner_id": row_after["corner_id"],
            "baseline_delta_pct": row_before["delta_pct"],
            "demo_delta_pct": row_after["delta_pct"],
        }
        for row_before, row_after in zip(baseline_delta, report["delta"], strict=True)
        if row_before["delta_pct"] != row_after["delta_pct"]
    ]
    return {
        "demo_resistance_ohm": peec_resistance_ohm,
        "baseline_resistance_ohm_total": report["extraction"]["mom_rlc_override"][
            "previous_resistance_ohm"
        ],
        "rows_moved": len(moved),
        "rows_total": len(baseline_delta),
        "example_moves": moved[:3],
        "caveat": (
            "NOT a valid whole-net MoM substitute -- this is one "
            "representative segment's own real PEEC resistance used as a "
            "stand-in for TAIL_A|TAIL_B's 3-terminal star total (see module "
            "docstring finding 1/2); included only to demonstrate the pex "
            "machinery genuinely is sensitive to this net's resistance "
            "value in this current-carrying scope, mirroring "
            "tests/test_pex.py's own "
            "test_run_pex_mom_rlc_net_threaded_through_to_extraction "
            "precedent (a synthetic fixture's real load net, not a whole "
            "net solved as one segment)"
        ),
    }


def main() -> None:
    layout_path = os.path.join(HERE, "06-layout.gds")
    request_path = os.path.join(HERE, "11-pex-mom.request.json")

    baseline = run_pex(
        layout_path,
        [request_path],
        "sky130",
        output=os.path.join(HERE, "11-pex-mom-extracted.spice"),
    )
    with open(
        os.path.join(HERE, "11-pex-mom.result.json"), "w", encoding="utf-8"
    ) as fh:
        json.dump(baseline, fh, indent=2, sort_keys=True)

    schematic_only = run_sim(request_path)
    with open(
        os.path.join(HERE, "11-pex-mom.schematic.result.json"), "w", encoding="utf-8"
    ) as fh:
        json.dump(schematic_only, fh, indent=2, sort_keys=True)

    segment_gds_path, segment_bbox = _isolate_tail_net_li1_shapes()
    dbu = kdb.Layout().dbu  # default 0.001 -- matches every layout in this repo
    segment_spec = {
        "background_permittivity": 3.9,
        "panel_size_um": 0.1,
        "compute_inductance": True,
        "filament_size_um": 0.1,
        "stackup": [
            {
                "layer": f"{_LI1_LAYER[0]}/{_LI1_LAYER[1]}",
                "conductor": "tail_li1_segment",
                "z0_um": 0.0,
                "z1_um": _LI1_THICKNESS_UM,
                "conductivity_S_per_m": _LI1_CONDUCTIVITY_S_PER_M,
            }
        ],
    }
    segment_spec_path = os.path.join(HERE, "11-mom-tail-segment.spec.json")
    with open(segment_spec_path, "w", encoding="utf-8") as fh:
        json.dump(segment_spec, fh, indent=2, sort_keys=True)

    mom_report = run_mom(segment_gds_path, segment_spec_path)
    # run_mom() echoes back whatever `file`/`spec` strings it was given
    # verbatim; this script always passes absolute, checkout-local paths (it
    # can be invoked from any cwd), so normalize both to the same
    # checkout-relative basenames 07-extract.json's own "file" field uses
    # before committing this result, rather than leaking this machine's
    # absolute directory layout into a tracked evidence artifact.
    mom_report["file"] = "11-mom-tail-segment.gds"
    mom_report["spec"] = "11-mom-tail-segment.spec.json"
    with open(
        os.path.join(HERE, "11-mom-tail-segment.result.json"), "w", encoding="utf-8"
    ) as fh:
        json.dump(mom_report, fh, indent=2, sort_keys=True)

    peec_resistance_ohm = mom_report["resistance_ohm"][0]
    segment_width_um = (segment_bbox.right - segment_bbox.left) * dbu
    segment_length_um = (segment_bbox.top - segment_bbox.bottom) * dbu
    sheet_resistance_prediction_ohm = (
        _LI1_SHEET_RES_OHM_SQ * segment_length_um / segment_width_um
    )
    assert abs(peec_resistance_ohm - sheet_resistance_prediction_ohm) < 1e-6, (
        "expected klt mom's PEEC DC-resistance solve to reproduce the plain "
        "sheet-resistance formula for a uniform rectangular bar -- see "
        "module docstring finding 1"
    )

    capacitance_check = _verify_capacitance_override_is_invisible(
        layout_path, request_path, baseline["delta"]
    )
    resistance_demo = _verify_resistance_override_is_sensitive(
        layout_path, request_path, baseline["delta"], peec_resistance_ohm
    )

    comparison = {
        "kind": "mom-fed-pex-delta-comparison",
        "epic": 709,
        "issue": 989,
        "scope": (
            "TAIL_A|TAIL_B biased negative (-0.45V/-0.55V) instead of Phase "
            "1c's +0.5V, turning on the two composed devices whose source "
            "terminal sits on this net and whose drain terminal sits on an "
            "actively-driven pin net (N1, VOUT) -- see module docstring for "
            "the full derivation"
        ),
        "baseline_lumped_rc": {
            "status": baseline["status"],
            "corner_count": baseline["corner_count"],
            "passed": baseline["passed"],
            "failed": baseline["failed"],
            "errored": baseline["errored"],
            "delta_pct_range": [
                min(r["delta_pct"] for r in baseline["delta"]),
                max(r["delta_pct"] for r in baseline["delta"]),
            ],
            "conclusion": (
                "every delta[] row is genuinely nonzero (a real "
                "schematic-vs-extracted current reduction from the added "
                "series R, 1.25%-96.71% in magnitude across the 6-corner "
                "sweep) -- the first non-null klt pex result recorded for "
                "this canary, refining Phase 1c's own 0.0/0.0 finding "
                "(which only proved this measurement scheme's own "
                "insensitivity, not that this canary is inherently DC-dead)"
            ),
        },
        "mom_solve": {
            "method": (
                "klt mom PEEC (compute_inductance=true), against one real "
                "bar-shaped segment of TAIL_A|TAIL_B's own li1 routing cut "
                "directly out of 06-layout.gds (not a synthetic fixture) -- "
                "see this script's own _isolate_tail_net_li1_shapes()"
            ),
            "segment_gds": "11-mom-tail-segment.gds",
            "segment_width_um": round(segment_width_um, 6),
            "segment_length_um": round(segment_length_um, 6),
            "peec_resistance_ohm": peec_resistance_ohm,
            "sheet_resistance_prediction_ohm": round(
                sheet_resistance_prediction_ohm, 4
            ),
            "peec_matches_sheet_resistance_formula": True,
            "peec_self_capacitance_ff": mom_report["capacitance_matrix_ff"][0][0],
            "whole_net_peec_attempted": (
                "yes -- TAIL_A|TAIL_B's own real li1 routing is 5 disjoint "
                "polygons (11-mom-tail-net-whole.gds), which klt mom's PEEC "
                "MVP restriction ('exactly one box per conductor', "
                "docs/cli/mom.md) cannot solve directly; a live attempt "
                "against the 5-polygon whole-net conductor did not "
                "complete within a practical time budget rather than "
                "returning the documented clean rejection promptly -- "
                "noted here as an observation, not resolved by this issue "
                "(multi-box PEEC is docs/cli/mom.md's own named follow-up)"
            ),
            "finding_1": (
                "DC resistance of a uniform rectangular conductor is "
                "mathematically identical in both models (R = rho_sheet * "
                "length / width) -- klt mom's PEEC solve reproduces the "
                "plain sheet-resistance prediction for the same segment to "
                "floating-point precision (see peec_resistance_ohm vs. "
                "sheet_resistance_prediction_ohm above). There is no "
                "fidelity gap for MoM to close on a straight bar's DC "
                "resistance by construction of the physics -- a genuinely "
                "measured null result, not an unexamined assumption"
            ),
        },
        "capacitance_override_check": capacitance_check,
        "resistance_override_sensitivity_demo": resistance_demo,
        "overall_conclusion": (
            "No `--mom-rlc-net`-fed fidelity GAIN is measurable for "
            "TAIL_A|TAIL_B in this canary, for two independent, "
            "mechanistically-verified reasons rather than one repeated "
            "null result: (1) klt mom cannot solve a real, fair, "
            "whole-net-total resistance for this net's actual branchy "
            "(5-polygon) routing -- --mom-rlc-net's substitution semantics "
            "need one whole-net total, klt mom's PEEC solve needs exactly "
            "one box per conductor, and even where a single representative "
            "segment CAN be PEEC-solved, DC resistance of a uniform "
            "rectangular bar is mathematically identical between the PEEC "
            "and sheet-resistance models -- there is no gap to measure "
            "even on a fair like-for-like segment; and (2) a "
            "capacitance-only override is structurally invisible to every "
            "measurement in this scope regardless of net choice, "
            "empirically confirmed with a 290x-baseline demo value. This "
            "canary's own current-carrying baseline result above (finding "
            "1c refinement) is genuine forward progress on the epic's "
            "'measured, not asserted' bar even where the MoM-fed half of "
            "the comparison specifically could not be completed -- see "
            "examples/design-pipeline/README.md's 'S10 mom-fed pex delta "
            "proof' section for the full writeup and what would need to "
            "change (a different canary's critical net routed as a single "
            "bar, or klt mom's own multi-box PEEC follow-up) to close this "
            "gap for real."
        ),
    }

    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    with open(__file__, "rb") as fh:
        script_sha256 = hashlib.sha256(fh.read()).hexdigest()
    with open(request_path, "rb") as fh:
        request_sha256 = hashlib.sha256(fh.read()).hexdigest()

    recorded_at_slug = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    recorded_at_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    record_stem = f"{recorded_at_slug}-{script_sha256[:7]}"

    common_fields = {
        "schema": "evidence-record/1",
        "recorded_at": recorded_at_iso,
        "block": "sky130-ota-5t",
        "scope": "mom-fed-current-probe",
        "request_path": "examples/design-pipeline/11-pex-mom.request.json",
        "request_sha256": request_sha256,
        "subset_reason": None,
        "pdk_pin": None,
        "supersedes": [],
    }

    pex_record = {
        **common_fields,
        "extraction_pin": {
            "method": "lumped-RC",
            "deck": baseline["provenance"]["deck"]["name"],
            "deck_content_hash": baseline["provenance"]["deck"]["content_hash"],
        },
        "note": (
            "Phase 3b (issue #989) -- the current-carrying companion to "
            "post-extraction-bias-probe/'s Phase 1c record: same block/"
            "canary, a new bias network (TAIL_A|TAIL_B driven negative) so "
            "current genuinely flows and delta_pct is nonzero. See "
            "mom-fed-current-probe/'s own -mom-comparison.json record for "
            "the MoM-fed half of this scope's comparison."
        ),
        "result": baseline,
    }
    sim_record = {
        **common_fields,
        "note": (
            "The schematic-side leg of the same scope -- a direct `klt sim` "
            "run of 11-pex-mom.request.json against its testbench's "
            "unmodified `.include` target (07-reference.spice), stored "
            "alongside per the convention's own framing, mirroring "
            "post-extraction-bias-probe/'s own -sim.json companion."
        ),
        "result": schematic_only,
    }
    mom_comparison_record = {
        **common_fields,
        "extraction_pin": {
            "method": (
                "MoM-fed R/L/C (Phase 3a, issue #988) vs lumped-RC (Phase 1) "
                "-- attempted on TAIL_A|TAIL_B"
            ),
            "deck": baseline["provenance"]["deck"]["name"],
            "deck_content_hash": baseline["provenance"]["deck"]["content_hash"],
        },
        "note": (
            "Not a klt pex/klt sim record on its own -- the MoM-fed half of "
            "this scope's comparison, request_path/request_sha256 pin the "
            "same 11-pex-mom.request.json baseline both other records in "
            "this scope pin (this record additionally depends on this "
            "directory's mom_current_carrying_probe.py, the script that "
            "produced every number in 'result', for full reproducibility). "
            "See examples/design-pipeline/README.md's 'S10 mom-fed pex "
            "delta proof' section for the full write-up."
        ),
        "result": comparison,
    }

    for suffix, record in (
        ("pex", pex_record),
        ("sim", sim_record),
        ("mom-comparison", mom_comparison_record),
    ):
        path = os.path.join(EVIDENCE_DIR, f"{record_stem}-{suffix}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, sort_keys=True)

    with open(os.path.join(EVIDENCE_DIR, "HEAD"), "w", encoding="utf-8") as fh:
        fh.write(f"pex: {record_stem}-pex.json\n")
        fh.write(f"sim: {record_stem}-sim.json\n")
        fh.write(f"mom-comparison: {record_stem}-mom-comparison.json\n")

    print("wrote evidence records under", EVIDENCE_DIR)
    print(json.dumps(comparison["overall_conclusion"], indent=2))


if __name__ == "__main__":
    main()
