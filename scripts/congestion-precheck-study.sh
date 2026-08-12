#!/usr/bin/env bash
# Run the OpenROAD half of the congestion pre-check study (issue #785,
# Epic #700 Phase 1; docs/design/place-and-route-improvements-survey.md
# section 3.6).
#
# For every candidate directory under <runs-dir>, this runs `klt
# place-and-route` TWICE against the same request:
#
#   1. `target_stage: "place"`  -- times the cheap prefix a congestion
#      pre-check would pay for, and produces the post-placement DEF the
#      estimator reads (`<top>_place.def`, issue #785).
#   2. `target_stage: "route"`  -- times the full candidate evaluation and
#      produces the oracle: OpenROAD's own `detailed_route -output_drc`
#      report.
#
# Running both is what makes acceptance criterion 2 ("the DSE-sweep
# wall-clock saved") a measurement rather than an estimate: the saving per
# rejected candidate is exactly (2) minus (1). Each pair's timings land in
# <run>/timings.json; `scripts/congestion_precheck_study.py` reads them.
#
# Requires, on the host (or reachable via Docker):
#   - a real, volare-fetched PDK install (`~/.volare/sky130A` by default)
#   - docker with the `openroad/orfs:latest` image -- the same path
#     `tests/corpus/place_and_route/regenerate.sh` uses. There is no
#     host-`openroad` variant here on purpose: the study's wall-clock
#     numbers are only comparable if every candidate ran in the same
#     container.
#
# Each <runs-dir>/<run-id>/ must already contain:
#   request.json   a `klt place-and-route` request (any target_stage; this
#                  script rewrites it in place for each of the two passes)
#   <netlist>      whatever `request.netlist` names, relative to that dir
#
# Usage:
#   ./scripts/congestion-precheck-study.sh <runs-dir>
#
# Re-runnable: a run directory that already has timings.json is skipped, so
# an interrupted sweep resumes instead of restarting.

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <runs-dir>" >&2
  exit 2
fi

RUNS_DIR="$(cd "$1" && pwd)"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PDK_NAME="${PDK:-sky130A}"
VOLARE_ROOT="${VOLARE_ROOT:-$HOME/.volare}"

if [[ ! -d "$VOLARE_ROOT/$PDK_NAME" ]]; then
  echo "error: no PDK install at $VOLARE_ROOT/$PDK_NAME" >&2
  exit 1
fi

CONTAINER_SCRIPT="$(mktemp)"
trap 'rm -f "$CONTAINER_SCRIPT"' EXIT

cat > "$CONTAINER_SCRIPT" <<'INNER'
set -uo pipefail
pip3 install -q /workdir/repo || exit 1

klt_run() { python3 -c '
from klayout_tools.cli import main
import sys
sys.exit(main(sys.argv[1:]))
' "$@"; }

set_stage() { python3 -c '
import json, sys
path = sys.argv[1] + "/request.json"
request = json.load(open(path))
request["target_stage"] = sys.argv[2]
json.dump(request, open(path, "w"), indent=2)
' "$1" "$2"; }

now() { python3 -c 'import time; print(time.time())'; }

for run in /workdir/runs/*/; do
  id="$(basename "$run")"
  if [[ -f "$run/timings.json" ]]; then
    echo "SKIP $id (timings.json present)"
    continue
  fi
  echo "=== $id"

  set_stage "$run" place
  t0="$(now)"
  klt_run place-and-route "$run/request.json" --format json \
    > "$run/place_response.json" 2> "$run/place_stderr.txt"
  rc_place=$?
  t1="$(now)"

  set_stage "$run" route
  t2="$(now)"
  klt_run place-and-route "$run/request.json" --format json \
    > "$run/route_response.json" 2> "$run/route_stderr.txt"
  rc_route=$?
  t3="$(now)"

  python3 -c '
import json, sys
run, t0, t1, t2, t3, rc_place, rc_route = sys.argv[1:]
json.dump({
    "place_seconds": float(t1) - float(t0),
    "full_route_seconds": float(t3) - float(t2),
    "place_returncode": int(rc_place),
    "route_returncode": int(rc_route),
}, open(run + "/timings.json", "w"), indent=2)
' "$run" "$t0" "$t1" "$t2" "$t3" "$rc_place" "$rc_route"
  echo "    place rc=$rc_place route rc=$rc_route"
done
echo "STUDY SWEEP DONE"
INNER

echo "==> sweeping $(find "$RUNS_DIR" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ') candidate(s) in $RUNS_DIR"
docker run --rm --platform linux/amd64 \
  -v "$REPO_ROOT:/workdir/repo:ro" \
  -v "$RUNS_DIR:/workdir/runs" \
  -v "$CONTAINER_SCRIPT:/workdir/sweep.sh:ro" \
  -v "$VOLARE_ROOT:/workdir/volare:ro" \
  -e "PDK=$PDK_NAME" \
  -e PDK_ROOT=/workdir/volare \
  -e PATH="/OpenROAD-flow-scripts/tools/install/OpenROAD/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
  openroad/orfs:latest \
  bash /workdir/sweep.sh

echo
echo "==> now analyse with:"
echo "    uv run python scripts/congestion_precheck_study.py $RUNS_DIR \\"
echo "        --tech-lef $VOLARE_ROOT/$PDK_NAME/libs.ref/sky130_fd_sc_hd/techlef/sky130_fd_sc_hd__nom.tlef \\"
echo "        --cell-lef $VOLARE_ROOT/$PDK_NAME/libs.ref/sky130_fd_sc_hd/lef/sky130_fd_sc_hd.lef"
