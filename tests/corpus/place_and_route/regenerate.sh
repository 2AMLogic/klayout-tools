#!/usr/bin/env bash
# Regenerate tests/corpus/place_and_route/gcd.gds.gz -- a deliberate,
# reviewed act (issue #436), never a CI step. See ../README.md's
# "Machine-generated macro-scale fixture" section for what this fixture is
# and why it exists.
#
# Requires, on the host (or reachable via Docker):
#   - yosys (for `klt synthesize`)
#   - a real, volare-fetched `sky130A` PDK install (`~/.volare/sky130A` or
#     any install `klt pdk find --pdk sky130A` resolves)
#   - `openroad` on $PATH, OR docker with the `openroad/orfs:latest` image
#     (this script uses the Docker path, matching PR #431's own worked
#     example -- swap the `docker run ...` block below for a bare
#     `openroad ...` invocation if you have a native `openroad` binary)
#
# Usage: run from the repo root:
#   ./tests/corpus/place_and_route/regenerate.sh
#
# Review the resulting diff (`git diff --stat tests/corpus/place_and_route/`)
# before committing -- a regenerated fixture nobody reviewed is not a
# meaningful regression net.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

echo "Scratch dir: $SCRATCH"

cp "$REPO_ROOT/examples/functional-verification/gcd.v" "$SCRATCH/gcd.v"

cat > "$SCRATCH/synth_request.json" <<'JSON'
{
  "schema": "klt.synthesize.request/1",
  "engine": "yosys",
  "sources": ["gcd.v"],
  "hdl_toplevel": "gcd",
  "pdk": { "cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80" },
  "constraints": { "clock_period_ns": null }
}
JSON

cat > "$SCRATCH/par_request.json" <<'JSON'
{
  "schema": "klt.place_and_route.request/1",
  "engine": "openroad",
  "netlist": ".klt/synthesize/gcd_synth.v",
  "hdl_toplevel": "gcd",
  "pdk": { "cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80" },
  "floorplan": {
    "method": "utilization",
    "utilization_pct": 38,
    "aspect_ratio": 1.0,
    "core_margin_um": 2.0,
    "site": "unithd"
  },
  "io": { "layer_h": "met3", "layer_v": "met2" },
  "constraints": { "clock_port": "clk", "clock_period_ns": 1.1 },
  "seed": 1,
  "target_stage": "route"
}
JSON

echo "==> klt synthesize (real Yosys, on host)"
( cd "$REPO_ROOT" && PDK=sky130A uv run klt synthesize "$SCRATCH/synth_request.json" --format json )

echo "==> klt place-and-route (real OpenROAD, via openroad/orfs:latest Docker image)"
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

GDS="$SCRATCH/.klt/place-and-route/gcd.gds"
if [[ ! -f "$GDS" ]]; then
  echo "error: expected $GDS to exist after place-and-route" >&2
  exit 1
fi

gzip -9 -c "$GDS" > "$REPO_ROOT/tests/corpus/place_and_route/gcd.gds.gz"
echo "wrote $REPO_ROOT/tests/corpus/place_and_route/gcd.gds.gz"
