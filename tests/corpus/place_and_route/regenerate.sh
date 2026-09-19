#!/usr/bin/env bash
# Regenerate tests/corpus/place_and_route/{gcd.gds.gz,gcd-pdn.gds.gz,
# mult8/mult8.gds.gz} -- a deliberate, reviewed act (issue #436; mult8 added
# by issue #938; gcd-pdn by issue #2079), never a CI step. See ../README.md's
# "Machine-generated macro-scale fixture" section for what these fixtures are
# and why they exist.
#
# Requires, on the host (or reachable via Docker):
#   - yosys (for `klt synthesize`)
#   - a real, volare-fetched `sky130A` PDK install (`~/.volare/sky130A` or
#     any install `klt pdk find --pdk sky130A` resolves)
#   - `openroad` on $PATH, OR docker with the `openroad/orfs:latest` image
#     (issue #1443: the place-and-route step auto-detects a native
#     `openroad` binary on $PATH and invokes `klt place-and-route` directly
#     against it, matching the native-`yosys` path `klt synthesize` already
#     uses above; it falls back to the `openroad/orfs:latest` Docker image,
#     matching PR #431's own worked example, only when no native `openroad`
#     is found)
#
# Usage: run from the repo root:
#   ./tests/corpus/place_and_route/regenerate.sh              # every fixture
#   ./tests/corpus/place_and_route/regenerate.sh gcd-pdn      # just one
#
# The optional design-name arguments exist so regenerating *one* fixture
# never silently rewrites the others (issue #2079 added `gcd-pdn` without
# touching the two fixtures whose exact contents dozens of tests/docs pin).
# Valid names are the keys of DESIGN_SRC below.
#
# Review the resulting diff (`git diff --stat tests/corpus/place_and_route/`)
# before committing -- a regenerated fixture nobody reviewed is not a
# meaningful regression net.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

# `mult8` (`tests/corpus/statime/mult8.v`, issue #809's STA-spike source) is
# purely combinational -- "No handshake, no clock: `p` is a pure combinational
# function of `a`/`b`" per its own header comment. `klt place-and-route`
# still requires a `constraints.clock_port` naming a *real* port once
# `target_stage` reaches `"place"` or later (no virtual/unconnected-clock
# support exists in this command today) -- there is no register in this
# design for a clock tree to drive either way, so the port choice only needs
# to avoid corrupting the fixture, not to model a real clock.
#
# Verified live (issue #938): naming an *input* port (e.g. `a[0]`, which
# fans out through the entire multiplier tree) makes `clock_tree_synthesis`
# treat that broad combinational fanout as a clock network and build a real
# (spurious) buffer tree across it -- utilization jumped from 39.6% to
# 68.6% and wirelength from ~4.3k to ~10.1k um in that trial, a corrupted,
# unrepresentative fixture. Naming an *output* port instead (`p[15]` here)
# is a clean no-op: an output port is a sink with no fanout inside the
# design, so `clock_tree_synthesis` has nothing to build (confirmed:
# utilization/wirelength stay exactly at their post-placement values through
# `"cts"`), and `report_checks` correctly reports the whole design as
# unconstrained (`worst_slack_ns: 1e+39`, OpenSTA's own sentinel) --
# consistent with `tests/corpus/statime/oracle_results.json`'s own
# unconstrained OpenSTA boundary condition for this same design. `p[15]` is
# an arbitrary choice among mult8's 16 output bits; any other output bit
# would work identically.
declare -A DESIGN_SRC=(
  [gcd]="$REPO_ROOT/examples/functional-verification/gcd.v"
  [gcd-pdn]="$REPO_ROOT/examples/functional-verification/gcd.v"
  [mult8]="$REPO_ROOT/tests/corpus/statime/mult8.v"
)
# The HDL top module each fixture is built from -- identical to the fixture
# name except for `gcd-pdn`, which is the *same* `gcd` design routed with a
# real PDN (issue #2079), so its top cell is `gcd` too. Every downstream
# consumer compares top cells, not file names.
declare -A DESIGN_TOPLEVEL=(
  [gcd]="gcd"
  [gcd-pdn]="gcd"
  [mult8]="mult8"
)
declare -A DESIGN_CLOCK_PORT=(
  [gcd]="clk"
  [gcd-pdn]="clk"
  [mult8]="p[15]"
)
declare -A DESIGN_CLOCK_PERIOD_NS=(
  [gcd]="1.1"
  [gcd-pdn]="1.1"
  [mult8]="6.0"
)
# Per-design `request.power` fragment (issue #2079), spliced into
# par_request.json below -- empty for a fixture that deliberately has no
# power delivery.
#
# `gcd` and `mult8` stay deliberately power-less: `gcd.gds.gz` is `klt
# power`'s own "no PDN, fragmented per-row islands" regression fixture
# (docs/cli/power.md's worked example, 17 VPWR + 17 VGND islands) and the
# negative control `docs/cli/place-and-route.md`'s "Design decision: the
# `klt power` `gcd` fixture" note asks for. `gcd-pdn.gds.gz` is the positive
# control that note's follow-up (this issue) adds beside it.
#
# Strap geometry is ORFS's own `platforms/sky130hd/pdn.tcl` standard-cell
# grid, not invented here -- the same three stripes this repo already pins
# in `tests/test_place_and_route.py`'s `_BASE_STRAPS`, and whose met1 entry
# `place_and_route.py`'s own `_ROW_RAIL_STRAP` table cites line-by-line
# (`add_pdn_stripe -grid {grid} -layer {met1} -width {0.48} -pitch {5.44}
# -offset {0} -followpins`). met4/met5 are the vertical/horizontal straps
# that actually tie the per-row met1 rails together -- the whole point of
# this fixture.
#
# `power_net`/`ground_net` are deliberately `VPWR`/`VGND`, the library's own
# standard-cell pin names, rather than the `VDD`/`VSS` defaults: that keeps
# this fixture's extracted net names identical to the gridless `gcd`
# fixture's (which gets them from the `_ROW_RAIL_STRAP` fallback's own
# unaliased VPWR/VGND naming), so a consumer moving from one to the other
# changes topology only, never net names.
declare -A DESIGN_POWER_JSON=(
  [gcd]=""
  [gcd-pdn]='  "power": {
    "power_net": "VPWR",
    "ground_net": "VGND",
    "straps": [
      { "layer": "met1", "width_um": 0.48, "pitch_um": 5.44, "offset_um": 0.0, "followpins": true },
      { "layer": "met4", "width_um": 1.6, "pitch_um": 27.14, "offset_um": 13.57 },
      { "layer": "met5", "width_um": 1.6, "pitch_um": 27.2, "offset_um": 13.6 }
    ]
  },'
  [mult8]=""
)
declare -A DESIGN_OUT_GDS_GZ=(
  [gcd]="$REPO_ROOT/tests/corpus/place_and_route/gcd.gds.gz"
  [gcd-pdn]="$REPO_ROOT/tests/corpus/place_and_route/gcd-pdn.gds.gz"
  [mult8]="$REPO_ROOT/tests/corpus/place_and_route/mult8/mult8.gds.gz"
)

ALL_DESIGNS=(gcd gcd-pdn mult8)
DESIGNS=("${@:-}")
if [[ ${#DESIGNS[@]} -eq 0 || -z ${DESIGNS[0]} ]]; then
  DESIGNS=("${ALL_DESIGNS[@]}")
fi
for DESIGN in "${DESIGNS[@]}"; do
  if [[ -z ${DESIGN_SRC[$DESIGN]:-} ]]; then
    echo "error: unknown design '$DESIGN' (known: ${ALL_DESIGNS[*]})" >&2
    exit 2
  fi
done

for DESIGN in "${DESIGNS[@]}"; do
  TOP="${DESIGN_TOPLEVEL[$DESIGN]}"
  # Scratch dir under $HOME, not the system default (often /tmp): a native
  # `openroad` on $PATH may itself be a thin Docker-forwarding wrapper that
  # only bind-mounts $HOME into the container (observed in this environment
  # -- see the header comment above), so a /tmp scratch dir is invisible to
  # the containerized `openroad` process even though `klt place-and-route`
  # itself (running on the host) can read/write it fine. Keeping scratch
  # under $HOME works for a genuinely native binary too.
  SCRATCH="$(mktemp -d -p "$HOME" klt-regen-pnr-XXXXXX)"
  # `KLT_REGEN_KEEP_SCRATCH=1` keeps every intermediate (the generated
  # requests, the DEF/GDS/as-built Verilog, and each check's own JSON
  # report) for inspection -- the only way to diagnose a *failed* check
  # below, whose non-zero exit would otherwise take the evidence with it.
  if [[ -z ${KLT_REGEN_KEEP_SCRATCH:-} ]]; then
    trap 'rm -rf "$SCRATCH"' EXIT
  fi

  echo "=== $DESIGN ==="
  echo "Scratch dir: $SCRATCH"

  cp "${DESIGN_SRC[$DESIGN]}" "$SCRATCH/$TOP.v"

  cat > "$SCRATCH/synth_request.json" <<JSON
{
  "schema": "klt.synthesize.request/1",
  "run_id": "corpus-${DESIGN}",
  "engine": "yosys",
  "sources": ["$TOP.v"],
  "hdl_toplevel": "$TOP",
  "pdk": { "cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80" },
  "constraints": { "clock_period_ns": null }
}
JSON

  cat > "$SCRATCH/par_request.json" <<JSON
{
  "schema": "klt.place_and_route.request/1",
  "engine": "openroad",
  "netlist": ".klt/synthesize/corpus-${DESIGN}/${TOP}_synth.v",
  "hdl_toplevel": "$TOP",
  "pdk": { "cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80" },
  "floorplan": {
    "method": "utilization",
    "utilization_pct": 38,
    "aspect_ratio": 1.0,
    "core_margin_um": 2.0,
    "site": "unithd"
  },
  "io": { "layer_h": "met3", "layer_v": "met2" },
${DESIGN_POWER_JSON[$DESIGN]}
  "constraints": { "clock_port": "${DESIGN_CLOCK_PORT[$DESIGN]}", "clock_period_ns": ${DESIGN_CLOCK_PERIOD_NS[$DESIGN]} },
  "seed": 1,
  "target_stage": "route"
}
JSON
  python3 -c "import json, sys; json.load(open('$SCRATCH/par_request.json'))"

  echo "==> klt synthesize $DESIGN (real Yosys, on host)"
  # Same YoWASP-sandbox note as tests/corpus/legalize/regenerate.sh and
  # tests/corpus/statime/regenerate.sh: prepend /usr/bin so a native `yosys`
  # build (not a WASI-sandboxed one whose preopens reject synthesize.py's
  # absolute scratch-dir script paths) is used, if present. Harmless when
  # the host only has one `yosys` already.
  ( cd "$SCRATCH" && PATH="/usr/bin:$PATH" PDK=sky130A uv --project "$REPO_ROOT" run klt synthesize \
      "$SCRATCH/synth_request.json" --format json )

  if command -v openroad >/dev/null 2>&1; then
    echo "==> klt place-and-route $DESIGN (native openroad on \$PATH: $(command -v openroad))"
    ( cd "$SCRATCH" && PATH="/usr/bin:$PATH" PDK=sky130A uv --project "$REPO_ROOT" run klt place-and-route \
        "$SCRATCH/par_request.json" --format json )
  else
    echo "==> klt place-and-route $DESIGN (real OpenROAD, via openroad/orfs:latest Docker image)"
    docker run --rm --platform linux/amd64 \
      -v "$REPO_ROOT:/workdir/repo:ro" \
      -v "$SCRATCH:/workdir/scratch" \
      -v "$HOME/.volare:/workdir/volare:ro" \
      -e PDK=sky130A \
      -e PDK_ROOT=/workdir/volare \
      -e PATH="/OpenROAD-flow-scripts/tools/install/OpenROAD/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
      openroad/orfs:latest \
      bash -c '
        pip3 install -q /workdir/repo &&
        python3 -c "
from klayout_tools.cli import main
import sys
sys.exit(main([\"place-and-route\", \"/workdir/scratch/par_request.json\", \"--format\", \"json\"]))
"'
  fi

  GDS="$SCRATCH/.klt/place-and-route/$TOP.gds"
  if [[ ! -f "$GDS" ]]; then
    echo "error: expected $GDS to exist after place-and-route" >&2
    exit 1
  fi

  echo "==> klt drc --deck sky130 $DESIGN (informational -- review, don't gate on this)"
  uv --project "$REPO_ROOT" run klt drc "$GDS" --deck sky130 --format json \
    > "$SCRATCH/${DESIGN}_drc.json"
  python3 -c "
import json
report = json.load(open('$SCRATCH/${DESIGN}_drc.json'))
print(f\"    status={report['status']!r} violation_count={report['violation_count']} rule_counts={report['rule_counts']}\")
"

  # Connectivity check (issue #1442): `klt drc` cannot see a real electrical
  # short between a power rail and a signal net -- spacing/width checks
  # never fire on touching or overlapping same-layer shapes, which is
  # exactly the geometry a row-rail-obstruction defect produces (see that
  # issue's own "Root cause"/"Evidence" sections). `klt extract`'s own
  # `merged_net_labels[]` *does* see it: any entry whose label set combines
  # one of `power_nets` below with a non-power label is a real short, not a
  # benign label collision -- scan for that class on every regeneration so
  # this defect can never silently ship again, the same way #1442's own
  # investigation first surfaced it (comparing a regenerated fixture's own
  # `merged_net_labels[]` against the committed one).
  echo "==> klt extract --deck sky130 $DESIGN -- connectivity (power/signal short) check"
  uv --project "$REPO_ROOT" run klt extract "$GDS" --deck sky130 --format json \
    > "$SCRATCH/${DESIGN}_extract.json"
  python3 -c "
import json
import sys

power_nets = {'VPWR', 'VGND', 'VDD', 'VSS', 'VPB', 'VNB'}
report = json.load(open('$SCRATCH/${DESIGN}_extract.json'))
shorts = [
    entry
    for entry in report.get('merged_net_labels', [])
    if (set(entry['labels']) & power_nets) and (set(entry['labels']) - power_nets)
]
if shorts:
    print(
        f'error: {len(shorts)} merged_net_labels[] entr'
        + ('y' if len(shorts) == 1 else 'ies')
        + ' combine a power-rail label with a non-power label -- a real'
        ' electrical short, not a benign label collision:',
        file=sys.stderr,
    )
    for entry in shorts:
        print(f\"    {entry['net']}\", file=sys.stderr)
    sys.exit(1)
print(f\"    clean: 0 power/signal shorts across {len(report.get('merged_net_labels', []))} merged_net_labels[] entries\")
"

  # Power-delivery check (issue #2079), for a fixture that asked for a PDN:
  # a routed block is only "power-complete" if every standard-cell supply
  # pin reaches *one* net across the whole design. That is exactly `klt
  # lvs`'s `power_connectivity` block (docs/cli/lvs.md -> "Power/ground
  # connectivity"), run against this same run's own as-built netlist, with
  # the layout side abstracted per that doc's own worked sequence. Gate on
  # it here, not just in a downstream test: a `request.power` run whose grid
  # silently failed to tie the per-row rails together still produces a
  # perfectly routable GDS, and the only place that is cheap to catch is
  # here, before the fixture is committed.
  #
  # `expected_nets` deliberately names VPWR/VGND/VPB but **not** VNB, even
  # though docs/cli/lvs.md's own "Recommended usage" example lists all four:
  # measured live on this design (issue #2079), the abstracted layout's
  # observed `power_pins` are exactly `["VGND", "VPB", "VPWR"]` -- no
  # `sky130_fd_sc_hd` instance carries a probe-able VNB pin, so declaring it
  # buys nothing but a permanent `unchecked_expected_pins: ["VNB"]` entry
  # (that doc's own "a PDK standard cell whose tie pin has no in-cell
  # label/LEF port at all" case). The assertion below treats a non-empty
  # `unchecked_expected_pins` as a failure precisely so this stays honest:
  # every pin named here is a pin this run actually exercised.
  if [[ -n "${DESIGN_POWER_JSON[$DESIGN]}" ]]; then
    echo "==> klt lvs (gate-level) $DESIGN -- power/ground connectivity check"
    PDK=sky130A uv --project "$REPO_ROOT" run klt extract "$GDS" --deck sky130 \
      --abstract-cells 'sky130_fd_sc_hd__*' --def-net-names \
      -o "$SCRATCH/${DESIGN}_gate.spice" --format json \
      > "$SCRATCH/${DESIGN}_gate_extract.json"

    cat > "$SCRATCH/lvs_request.json" <<JSON
{
  "layout": { "netlist": "$SCRATCH/${DESIGN}_gate.spice", "top": "$TOP" },
  "reference": {
    "netlist": "$SCRATCH/.klt/place-and-route/$TOP.v",
    "top": "$TOP",
    "form": "gate-level-verilog",
    "library": "sky130_fd_sc_hd"
  },
  "options": {
    "power_connectivity": {
      "expected_nets": { "VPWR": "VPWR", "VGND": "VGND", "VPB": "VPWR" }
    }
  }
}
JSON
    PDK=sky130A uv --project "$REPO_ROOT" run klt lvs "$SCRATCH/lvs_request.json" \
      --format json > "$SCRATCH/${DESIGN}_lvs.json" || true
    python3 -c "
import json
import sys

report = json.load(open('$SCRATCH/${DESIGN}_lvs.json'))
power = report.get('power_connectivity', {})
print(
    f\"    signal status={report.get('status')!r} mismatch_count={report.get('mismatch_count')}\"
    f\" | power status={power.get('status')!r} finding_count={power.get('finding_count')}\"
    f\" instance_count={power.get('instance_count')}\"
    f\" unchecked_expected_pins={power.get('unchecked_expected_pins')}\"
)
problems = []
if report.get('status') != 'match':
    problems.append(f\"signal LVS status is {report.get('status')!r}, not 'match'\")
if power.get('status') != 'match':
    problems.append(f\"power_connectivity.status is {power.get('status')!r}, not 'match'\")
if power.get('unchecked_expected_pins'):
    problems.append(
        'expected_nets named power pin(s) no instance carries: '
        + repr(power['unchecked_expected_pins'])
    )
if problems:
    print('error: this fixture is not power-complete:', file=sys.stderr)
    for problem in problems:
        print(f'    {problem}', file=sys.stderr)
    for finding in power.get('findings', []):
        print(f\"    finding {finding['rule']} on pin {finding['pin']}: {finding['description']}\", file=sys.stderr)
    sys.exit(1)
print('    clean: every standard-cell supply pin reaches exactly one net')
"
  fi

  mkdir -p "$(dirname "${DESIGN_OUT_GDS_GZ[$DESIGN]}")"
  gzip -9 -c "$GDS" > "${DESIGN_OUT_GDS_GZ[$DESIGN]}"
  echo "wrote ${DESIGN_OUT_GDS_GZ[$DESIGN]}"

  if [[ -z ${KLT_REGEN_KEEP_SCRATCH:-} ]]; then
    rm -rf "$SCRATCH"
    trap - EXIT
  else
    echo "KLT_REGEN_KEEP_SCRATCH is set -- keeping $SCRATCH"
  fi
done
