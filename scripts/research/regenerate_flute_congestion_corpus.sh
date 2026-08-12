#!/usr/bin/env bash
# Regenerate the corpus + manifest behind
# `docs/design/flute-congestion-precheck-results.md` (issue #785, Epic #700
# Phase 1 Section 3.6) -- runs the real `gcd` example through `klt
# synthesize` + `klt place-and-route` at three utilization targets (a
# comfortable candidate, a tight-but-routable candidate, and a candidate
# tight enough that detailed routing does not converge in a reasonable
# wall-clock budget), producing the placement-stage DEFs and route outcomes
# `flute_congestion_correlation.py`'s manifest schema expects.
#
# Deliberate, reviewed, manually run -- never a CI step, mirroring
# `tests/corpus/place_and_route/regenerate.sh`'s own posture (issue #436)
# and reusing its exact `openroad/orfs:latest` Docker recipe. See that
# script's own header for the toolchain requirements (yosys, a real
# volare-fetched sky130A install, and either a native `openroad` binary or
# Docker with the `openroad/orfs:latest` image).
#
# Usage: run from the repo root:
#   ./scripts/research/regenerate_flute_congestion_corpus.sh <output-dir>
#
# Writes <output-dir>/trial-<name>/.klt/place-and-route/gcd.place.def (one
# per trial) and <output-dir>/manifest.json (ready for
# `flute_congestion_correlation.py --manifest`). A `did_not_converge` trial
# is deliberately allowed to run for a bounded wall-clock budget
# (ROUTE_TIMEOUT_S below) before this script gives up on it and records the
# lower-bound wall-clock instead of the (unknown) true completion time --
# this is itself the real-world case the pre-check exists to avoid paying
# for, not a limitation of this script.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="${1:?usage: $0 <output-dir>}"
mkdir -p "$OUT_DIR"

#: Utilization targets (`floorplan.utilization_pct` in the P&R request) --
#: not the *reported* post-legalization utilization, which for this design
#: runs meaningfully higher than the requested target (see the results doc
#: for the observed gap). Chosen empirically for the `gcd` corpus fixture:
#: 38 reproduces `tests/corpus/place_and_route/gcd.gds.gz`'s own generation
#: request (issue #436) as the "comfortable" trial; 60 is the tightest
#: value observed to still route cleanly; 68 is the tightest value observed
#: to legalize (detailed placement) but not converge in detailed routing
#: within `ROUTE_TIMEOUT_S`. 75 was also tried and rejected outright by
#: `detailed_placement` itself (`DPL-0038`, no room to legalize) -- not
#: included as a trial since it has no placement-stage DEF to feed the
#: pre-check (the failure happens before the `place` stage's own
#: `write_def` line runs).
TRIALS=(
  "util38:38:1"
  "util60:60:2"
  "util68:68:3"
)

#: Wall-clock budget (seconds) before a trial's route stage is treated as
#: "did not converge" and killed. 2400s (40 min) matches this study's own
#: real run (see the results doc) -- generous for a design this small
#: (`gcd`, 384 cells), so hitting it is itself the finding, not an artifact
#: of an unreasonably tight budget.
ROUTE_TIMEOUT_S="${ROUTE_TIMEOUT_S:-2400}"

manifest_entries=()

for trial_spec in "${TRIALS[@]}"; do
  IFS=':' read -r name util seed <<<"$trial_spec"
  scratch="$OUT_DIR/trial-$name"
  mkdir -p "$scratch"
  cp "$REPO_ROOT/examples/functional-verification/gcd.v" "$scratch/gcd.v"

  cat >"$scratch/synth_request.json" <<JSON
{
  "schema": "klt.synthesize.request/1",
  "engine": "yosys",
  "sources": ["gcd.v"],
  "hdl_toplevel": "gcd",
  "pdk": { "cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80" },
  "constraints": { "clock_period_ns": null }
}
JSON

  cat >"$scratch/par_request.json" <<JSON
{
  "schema": "klt.place_and_route.request/1",
  "engine": "openroad",
  "netlist": ".klt/synthesize/gcd_synth.v",
  "hdl_toplevel": "gcd",
  "pdk": { "cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80" },
  "floorplan": {
    "method": "utilization",
    "utilization_pct": $util,
    "aspect_ratio": 1.0,
    "core_margin_um": 2.0,
    "site": "unithd"
  },
  "io": { "layer_h": "met3", "layer_v": "met2" },
  "constraints": { "clock_port": "clk", "clock_period_ns": 1.1 },
  "seed": $seed,
  "target_stage": "route"
}
JSON

  echo "==> [$name] klt synthesize (real Yosys, on host)"
  ( cd "$REPO_ROOT" && PDK=sky130A uv run klt synthesize "$scratch/synth_request.json" --format json ) \
    >"$scratch/synth_result.json"

  echo "==> [$name] klt place-and-route (real OpenROAD, via openroad/orfs:latest Docker image, budget ${ROUTE_TIMEOUT_S}s)"
  place_start=$(date +%s)
  timeout "$ROUTE_TIMEOUT_S" docker run --rm --platform linux/amd64 \
    -v "$REPO_ROOT:/workdir/repo:ro" \
    -v "$scratch:/workdir/scratch" \
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
"' >"$scratch/par_result.json" 2>"$scratch/par_stderr.log"
  route_status=$?
  place_end=$(date +%s)
  wallclock_s=$((place_end - place_start))

  place_def="$scratch/.klt/place-and-route/gcd.place.def"
  drc_report="$scratch/.klt/place-and-route/gcd_route_drc.rpt"

  if [[ ! -f "$place_def" ]]; then
    echo "warning: [$name] no placement-stage DEF produced -- skipping (see par_stderr.log)" >&2
    continue
  fi

  if [[ $route_status -eq 124 ]]; then
    status="did_not_converge"
    drc_count="null"
  elif [[ $route_status -ne 0 ]]; then
    echo "warning: [$name] place-and-route failed (exit $route_status), but a placement DEF exists -- recording as did_not_converge" >&2
    status="did_not_converge"
    drc_count="null"
  elif [[ -f "$drc_report" ]]; then
    status="routed"
    drc_count=$(grep -ci "violation type:" "$drc_report" || true)
  else
    echo "warning: [$name] route stage reported success but no DRC report found" >&2
    status="did_not_converge"
    drc_count="null"
  fi

  manifest_entries+=("{\"name\": \"$name\", \"place_def\": \"$place_def\", \"status\": \"$status\", \"drc_violation_count\": $drc_count, \"route_wallclock_s\": $wallclock_s}")
done

python3 - "$OUT_DIR/manifest.json" "${manifest_entries[@]}" <<'PYEOF'
import json
import sys

out_path = sys.argv[1]
entries = [json.loads(e) for e in sys.argv[2:]]
with open(out_path, "w", encoding="utf-8") as handle:
    json.dump(entries, handle, indent=2)
print(f"wrote {out_path}")
PYEOF
