#!/usr/bin/env bash
# place-and-route-smoke.sh -- end-to-end synth -> place-and-route -> GDS ->
# `klt lvs` -> `klt drc` smoke pipeline (issue #1328, Epic #700 Phase 4): the
# CI-runnable proof that `klt place-and-route`'s output is genuinely
# LVS-clean and DRC-clean, not just "did not error".
#
# Scoped to `gcd` and `mult8` only -- the only two designs with a committed,
# reviewed regenerate.sh recipe under tests/corpus/place_and_route/. There
# is NO committed `modexp` fixture (issue #1328's own "Verified corrections"
# section) -- real `modexp`/fleet-canary validation is tracked separately by
# issue #1329, not folded into this script.
#
# Adapts tests/corpus/place_and_route/regenerate.sh's request-document
# shapes (that script is fixture regeneration, "never a CI step" per its own
# header -- this script is the CI-runnable sibling it does not replace).
#
# LVS methodology: self-compare. Each design's routed GDS is extracted TWICE
# -- once to a reference SPICE file via `klt extract`, once again inline by
# `klt lvs` itself -- and the two extractions are asserted equivalent. This
# is the same oracle
# tests/test_lvs.py::test_pnr_gcd_fixture_self_compare_matches_cleanly
# already established for this exact `gcd` fixture (issue #389): it proves
# extraction at macro scale does not silently drop/misconnect devices, and
# is a real `klt lvs status` assertion, not a "did not error" check.
#
# Gate-level LVS methodology (issue #1336): a SECOND, independent LVS stage
# runs per design, against the as-built (`verilog_path`) gate-level Verilog
# netlist `klt place-and-route` itself wrote -- the compare the self-compare
# above cannot make, because a self-compare's two sides come from the same
# artifact and therefore cannot catch a layout that disagrees with the
# netlist it was built from. The layout side is `klt extract
# --abstract-cells 'sky130_fd_sc_hd__*'` (issue #620: every standard cell a
# pin-only black box); the reference side is `verilog_path` itself, read
# through `klt lvs`'s `reference.form: "gate-level-verilog"` (issue #1336),
# which converts it to matching black-box SPICE in-process. Before #1336
# this stage was impossible -- `klt lvs` compares SPICE netlists only and
# nothing converted the Verilog -- which is why this script's earlier
# revisions documented it as deferred.
#
# Comparing against `klt synthesize`'s PRE-route netlist is still NOT done,
# and is not what this stage is: docs/cli/place-and-route.md's "As-built
# netlist (verilog_path)" section documents why a pre-route netlist produces
# mismatches that are not real defects (CTS buffers, timing-repair resizes,
# antenna diodes all post-date synthesis). `verilog_path` is written from
# the same linked design `write_def` dumped, so it has no such divergence.
#
# Requires, on $PATH:
#   - a real `yosys` (`klt synthesize`)
#   - a real `openroad` (`klt place-and-route`) -- see
#     scripts/install-openroad-docker.sh for this repo's documented
#     Docker-wrapper recipe (docs/cli/place-and-route.md, "Installing
#     OpenROAD")
#   - `jq`
#
# Requires, in the environment:
#   - $PDK / $PDK_ROOT resolving a FULL open_pdks-layout sky130A install
#     (LEF + tech-LEF + liberty) -- NOT the liberty-only subset
#     pdks/sky130-liberty/ the `test` job in .github/workflows/ci.yml
#     fetches for `klt synthesize`'s own worked example. See
#     .github/workflows/place-and-route-smoke.yml's `volare enable` step
#     for how CI provisions this.
#
# Usage: scripts/place-and-route-smoke.sh [DESIGN...]
#   With no arguments, runs both `gcd` and `mult8`.
#
# Exit codes: 0 every requested design reached a clean LVS + clean DRC
# result; 1 any design failed at any stage (message on stderr names the
# design, stage, and offending JSON field -- never just "something failed").

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v jq >/dev/null 2>&1 || {
    echo "::error::jq is required on \$PATH" >&2
    exit 1
}

die() {
    echo "::error::$*" >&2
    exit 1
}

declare -A DESIGN_SRC=(
    [gcd]="$REPO_ROOT/examples/functional-verification/gcd.v"
    [mult8]="$REPO_ROOT/tests/corpus/statime/mult8.v"
)
# See tests/corpus/place_and_route/regenerate.sh's own header comment for
# why mult8's clock port names an OUTPUT bit (`p[15]`) rather than a real
# clock -- mult8 is purely combinational, and an input-port choice corrupts
# `clock_tree_synthesis` into building a spurious buffer tree.
declare -A DESIGN_CLOCK_PORT=(
    [gcd]="clk"
    [mult8]="p[15]"
)
declare -A DESIGN_CLOCK_PERIOD_NS=(
    [gcd]="1.1"
    [mult8]="6.0"
)

DESIGNS=("$@")
if [[ ${#DESIGNS[@]} -eq 0 ]]; then
    DESIGNS=(gcd mult8)
fi
for DESIGN in "${DESIGNS[@]}"; do
    [[ -n "${DESIGN_SRC[$DESIGN]+x}" ]] || die "unknown design '$DESIGN' (supported: ${!DESIGN_SRC[*]})"
done

run_klt() {
    # $1 = scratch dir to run from, rest = klt argv.
    local scratch="$1"
    shift
    ( cd "$scratch" && uv run --project "$REPO_ROOT" klt "$@" )
}

for DESIGN in "${DESIGNS[@]}"; do
    echo "=== $DESIGN ==="
    SCRATCH="$(mktemp -d)"
    trap 'rm -rf "$SCRATCH"' EXIT

    cp "${DESIGN_SRC[$DESIGN]}" "$SCRATCH/$DESIGN.v"

    cat >"$SCRATCH/synth_request.json" <<JSON
{
  "schema": "klt.synthesize.request/1",
  "engine": "yosys",
  "sources": ["$DESIGN.v"],
  "hdl_toplevel": "$DESIGN",
  "pdk": { "cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80" },
  "constraints": { "clock_period_ns": null }
}
JSON

    echo "--> klt synthesize $DESIGN"
    SYNTH_JSON="$SCRATCH/synth_report.json"
    run_klt "$SCRATCH" synthesize synth_request.json --format json >"$SYNTH_JSON"
    NETLIST_PATH="$(jq -r '.netlist_path' "$SYNTH_JSON")"
    [[ -n "$NETLIST_PATH" && "$NETLIST_PATH" != "null" ]] || die "$DESIGN: klt synthesize did not report a netlist_path"
    [[ -f "$NETLIST_PATH" ]] || die "$DESIGN: synthesize's reported netlist_path '$NETLIST_PATH' does not exist"

    cat >"$SCRATCH/par_request.json" <<JSON
{
  "schema": "klt.place_and_route.request/1",
  "engine": "openroad",
  "netlist": "$NETLIST_PATH",
  "hdl_toplevel": "$DESIGN",
  "pdk": { "cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80" },
  "floorplan": {
    "method": "utilization",
    "utilization_pct": 38,
    "aspect_ratio": 1.0,
    "core_margin_um": 2.0,
    "site": "unithd"
  },
  "io": { "layer_h": "met3", "layer_v": "met2" },
  "constraints": { "clock_port": "${DESIGN_CLOCK_PORT[$DESIGN]}", "clock_period_ns": ${DESIGN_CLOCK_PERIOD_NS[$DESIGN]} },
  "seed": 1,
  "target_stage": "route"
}
JSON

    echo "--> klt place-and-route $DESIGN (target_stage: route)"
    PAR_JSON="$SCRATCH/par_report.json"
    run_klt "$SCRATCH" place-and-route par_request.json --format json >"$PAR_JSON"

    STAGE_REACHED="$(jq -r '.stage_reached' "$PAR_JSON")"
    [[ "$STAGE_REACHED" == "route" ]] || die "$DESIGN: place-and-route stage_reached='$STAGE_REACHED', expected 'route' (see $PAR_JSON)"

    GDS_PATH="$(jq -r '.gds_path' "$PAR_JSON")"
    [[ -n "$GDS_PATH" && "$GDS_PATH" != "null" ]] || die "$DESIGN: place-and-route did not report a gds_path at stage_reached='route'"
    [[ -f "$GDS_PATH" ]] || die "$DESIGN: place-and-route's reported gds_path '$GDS_PATH' does not exist"

    ANTENNA_COUNT="$(jq -r '.antenna_violation_count' "$PAR_JSON")"
    ROUTE_DRC_COUNT="$(jq -r '.route_drc_violation_count' "$PAR_JSON")"
    [[ "$ANTENNA_COUNT" == "0" ]] || die "$DESIGN: place-and-route reported antenna_violation_count=$ANTENNA_COUNT (see $PAR_JSON)"
    [[ "$ROUTE_DRC_COUNT" == "0" ]] || die "$DESIGN: place-and-route reported route_drc_violation_count=$ROUTE_DRC_COUNT (see $PAR_JSON)"

    echo "--> klt extract $DESIGN (reference side, sky130 curated deck)"
    REFERENCE_SPICE="$SCRATCH/$DESIGN.reference.spice"
    EXTRACT_JSON="$SCRATCH/extract_report.json"
    run_klt "$SCRATCH" extract "$GDS_PATH" --deck sky130 -o "$REFERENCE_SPICE" --format json >"$EXTRACT_JSON"
    EXTRACT_TOP="$(jq -r '.top' "$EXTRACT_JSON")"
    [[ -n "$EXTRACT_TOP" && "$EXTRACT_TOP" != "null" ]] || die "$DESIGN: klt extract did not report a top cell"
    [[ -f "$REFERENCE_SPICE" ]] || die "$DESIGN: klt extract did not write $REFERENCE_SPICE"

    cat >"$SCRATCH/lvs_request.json" <<JSON
{
  "schema": "klt.lvs.request/1",
  "engine": "klayout",
  "layout": { "file": "$GDS_PATH", "deck": "sky130", "top": "$EXTRACT_TOP" },
  "reference": { "netlist": "$REFERENCE_SPICE", "top": "$EXTRACT_TOP" }
}
JSON

    echo "--> klt lvs $DESIGN (routed GDS self-compare)"
    LVS_JSON="$SCRATCH/lvs_report.json"
    run_klt "$SCRATCH" lvs lvs_request.json --format json >"$LVS_JSON"

    LVS_STATUS="$(jq -r '.status' "$LVS_JSON")"
    LVS_ERROR_COUNT="$(jq -r '.error_count' "$LVS_JSON")"
    if [[ "$LVS_STATUS" != "match" || "$LVS_ERROR_COUNT" != "0" ]]; then
        echo "$DESIGN: klt lvs report:" >&2
        jq '.' "$LVS_JSON" >&2 || true
        die "$DESIGN: klt lvs status='$LVS_STATUS' error_count=$LVS_ERROR_COUNT (expected status='match' error_count=0)"
    fi

    # --- gate-level LVS against the as-built netlist (issue #1336) --------- #
    VERILOG_PATH="$(jq -r '.verilog_path' "$PAR_JSON")"
    [[ -n "$VERILOG_PATH" && "$VERILOG_PATH" != "null" ]] || die "$DESIGN: place-and-route did not report a verilog_path at stage_reached='route'"
    [[ -f "$VERILOG_PATH" ]] || die "$DESIGN: place-and-route's reported verilog_path '$VERILOG_PATH' does not exist"

    echo "--> klt extract $DESIGN (gate-level layout side, standard cells abstracted)"
    GATE_LAYOUT_SPICE="$SCRATCH/$DESIGN.gate.spice"
    GATE_EXTRACT_JSON="$SCRATCH/gate_extract_report.json"
    # `--abstract-cells` makes every standard cell a pin-only black box
    # (issue #620) so the layout side has the same shape as the converted
    # Verilog reference -- comparing a real-devices extraction against a
    # gate-level reference could never be clean. `--def-net-names` (issue
    # #951) recovers the design's own DEF net names from the routed GDS, so
    # the layout's top-level boundary pins carry the same names the Verilog
    # module's ports do.
    run_klt "$SCRATCH" extract "$GDS_PATH" --deck sky130 \
        --abstract-cells 'sky130_fd_sc_hd__*' --def-net-names \
        -o "$GATE_LAYOUT_SPICE" --format json >"$GATE_EXTRACT_JSON"
    GATE_EXTRACT_TOP="$(jq -r '.top' "$GATE_EXTRACT_JSON")"
    [[ -f "$GATE_LAYOUT_SPICE" ]] || die "$DESIGN: klt extract --abstract-cells did not write $GATE_LAYOUT_SPICE"
    ABSTRACTED_INSTANCES="$(jq -r '[.abstracted_cells[].instance_count] | add // 0' "$GATE_EXTRACT_JSON")"
    [[ "$ABSTRACTED_INSTANCES" -gt 0 ]] || die "$DESIGN: klt extract --abstract-cells abstracted 0 instances -- the layout side would be an empty gate-level netlist (see $GATE_EXTRACT_JSON)"

    cat >"$SCRATCH/gate_lvs_request.json" <<JSON
{
  "schema": "klt.lvs.request/1",
  "engine": "klayout",
  "layout": { "netlist": "$GATE_LAYOUT_SPICE", "top": "$GATE_EXTRACT_TOP" },
  "reference": {
    "netlist": "$VERILOG_PATH",
    "top": "$DESIGN",
    "form": "gate-level-verilog",
    "library": "sky130_fd_sc_hd"
  }
}
JSON

    echo "--> klt lvs $DESIGN (gate-level: routed GDS vs. as-built verilog_path)"
    GATE_LVS_JSON="$SCRATCH/gate_lvs_report.json"
    run_klt "$SCRATCH" lvs gate_lvs_request.json --format json >"$GATE_LVS_JSON"

    GATE_LVS_STATUS="$(jq -r '.status' "$GATE_LVS_JSON")"
    GATE_LVS_ERROR_COUNT="$(jq -r '.error_count' "$GATE_LVS_JSON")"
    if [[ "$GATE_LVS_STATUS" != "match" || "$GATE_LVS_ERROR_COUNT" != "0" ]]; then
        echo "$DESIGN: klt lvs (gate-level) report:" >&2
        jq '.' "$GATE_LVS_JSON" >&2 || true
        die "$DESIGN: gate-level klt lvs status='$GATE_LVS_STATUS' error_count=$GATE_LVS_ERROR_COUNT (expected status='match' error_count=0)"
    fi

    echo "--> klt drc $DESIGN (merged routed GDS)"
    DRC_JSON="$SCRATCH/drc_report.json"
    run_klt "$SCRATCH" drc "$GDS_PATH" --deck sky130 --format json >"$DRC_JSON"

    DRC_STATUS="$(jq -r '.status' "$DRC_JSON")"
    DRC_VIOLATION_COUNT="$(jq -r '.violation_count' "$DRC_JSON")"
    if [[ "$DRC_STATUS" != "clean" || "$DRC_VIOLATION_COUNT" != "0" ]]; then
        echo "$DESIGN: klt drc report:" >&2
        jq '.' "$DRC_JSON" >&2 || true
        die "$DESIGN: klt drc status='$DRC_STATUS' violation_count=$DRC_VIOLATION_COUNT (expected status='clean' violation_count=0)"
    fi

    echo "$DESIGN: OK -- place-and-route reached 'route', LVS status='match' error_count=0 (self-compare AND gate-level vs. verilog_path), DRC status='clean' violation_count=0"

    rm -rf "$SCRATCH"
    trap - EXIT
done

echo "All designs (${DESIGNS[*]}) are LVS-clean and DRC-clean."
