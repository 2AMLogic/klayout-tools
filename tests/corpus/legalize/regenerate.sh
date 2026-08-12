#!/usr/bin/env bash
# Regenerate the tests/corpus/legalize/<design>/ fixtures issue #784's spike
# uses -- a deliberate, reviewed act, never a CI step. See ./README.md for
# what these fixtures are and why they exist.
#
# Adapted from tests/corpus/place_and_route/regenerate.sh (same Docker/
# volare/PDK_ROOT setup), but drives generate_checkpoints.py instead of the
# production `klt place-and-route` CLI, since production has no point in its
# own flow where a DEF is written between `global_placement` and
# `detailed_placement` (there is nothing to intercept there to reuse).
#
# Requires, on the host (or reachable via Docker):
#   - yosys (for `klt synthesize`)
#   - a real, volare-fetched `sky130A` PDK install (`~/.volare/sky130A` or
#     any install `klt pdk find --pdk sky130A` resolves)
#   - `openroad` on $PATH, OR docker with the `openroad/orfs:latest` image
#     (this script uses the Docker path, matching regenerate.sh's own
#     worked example)
#
# Usage: run from the repo root:
#   ./tests/corpus/legalize/regenerate.sh
#
# Review the resulting diff before committing -- a regenerated fixture
# nobody reviewed is not a meaningful regression net.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

echo "Scratch dir: $SCRATCH"

for DESIGN in gcd modexp; do
  echo "=== $DESIGN ==="
  cp "$REPO_ROOT/examples/functional-verification/$DESIGN.v" "$SCRATCH/$DESIGN.v"

  cat > "$SCRATCH/${DESIGN}_synth_request.json" <<JSON
{
  "schema": "klt.synthesize.request/1",
  "engine": "yosys",
  "sources": ["$DESIGN.v"],
  "hdl_toplevel": "$DESIGN",
  "pdk": { "cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80" },
  "constraints": { "clock_period_ns": null }
}
JSON

  echo "==> klt synthesize $DESIGN (real Yosys, on host)"
  # `/usr/bin` prepended: this host's default `yosys` on $PATH resolves to a
  # YoWASP (WASI-sandboxed) build whose sandbox only preopens the process's
  # own cwd, rejecting the absolute `/tmp/...` paths `synthesize.py` always
  # writes into its generated `.ys` script (verified live) -- the native
  # `/usr/bin/yosys` build has no such sandbox and handles the same absolute
  # paths correctly. A host without a YoWASP `yosys` shadowing the native
  # build does not need this.
  ( cd "$REPO_ROOT" && PATH="/usr/bin:$PATH" PDK=sky130A uv run klt synthesize \
      "$SCRATCH/${DESIGN}_synth_request.json" --format json )
done

echo "==> generate_checkpoints.py (real OpenROAD, via openroad/orfs:latest Docker image)"
# `${DOCKER:-docker}`: some hosts only grant the invoking user access to the
# Docker daemon via `sudo` (verified live in this spike's own dev sandbox) --
# override with `DOCKER='sudo docker'` when plain `docker` reports
# "permission denied ... docker.sock".
${DOCKER:-docker} run --rm --platform linux/amd64 \
  -v "$REPO_ROOT:/workdir/repo:ro" \
  -v "$SCRATCH:/workdir/scratch" \
  -v "$HOME/.volare:/workdir/volare:ro" \
  -e PDK=sky130A \
  -e PDK_ROOT=/workdir/volare \
  -e PATH="/OpenROAD-flow-scripts/tools/install/OpenROAD/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
  openroad/orfs:latest \
  bash -c '
    set -euo pipefail
    pip3 install -q /workdir/repo
    for DESIGN in gcd modexp; do
      echo "--> $DESIGN"
      python3 /workdir/repo/tests/corpus/legalize/generate_checkpoints.py \
        --netlist "/workdir/scratch/.klt/synthesize/${DESIGN}_synth.v" \
        --toplevel "$DESIGN" \
        --cell-library sky130_fd_sc_hd --corner tt_025C_1v80 \
        --utilization 38 --aspect-ratio 1.0 --core-margin 2.0 --site unithd \
        --io-h met3 --io-v met2 \
        --clock-port clk --clock-period-ns 1.1 --seed 1 \
        --output-dir "/workdir/scratch/out/${DESIGN}"
    done
  '

echo "==> gzipping DEFs, trimming + gzipping the cell LEF to only referenced MACROs"
# The full sky130_fd_sc_hd cell LEF is ~1.4 MB (~440 MACRO blocks); gcd+modexp
# together reference under 80 of them. _filter_cell_lef.py keeps every
# referenced block byte-identical, it just drops the unreferenced ones --
# gzip then shrinks the DEFs/LEF further (matches
# tests/corpus/place_and_route/gcd.gds.gz's own gzip convention).
for DESIGN in gcd modexp; do
  OUT_DIR="$REPO_ROOT/tests/corpus/legalize/$DESIGN"
  mkdir -p "$OUT_DIR"
  gzip -9 -c "$SCRATCH/out/$DESIGN/${DESIGN}_gp.def" > "$OUT_DIR/${DESIGN}_gp.def.gz"
  gzip -9 -c "$SCRATCH/out/$DESIGN/${DESIGN}_oracle_legal.def" \
    > "$OUT_DIR/${DESIGN}_oracle_legal.def.gz"
  cp "$SCRATCH/out/$DESIGN/${DESIGN}_gen_metrics.json" "$OUT_DIR/"
  echo "wrote $OUT_DIR/"
done

python3 "$REPO_ROOT/tests/corpus/legalize/_filter_cell_lef.py" \
  "$SCRATCH/out/gcd/gcd_cell.lef" "$SCRATCH/sky130_fd_sc_hd_cells_used.lef" \
  "$SCRATCH"/out/*/*_gp.def "$SCRATCH"/out/*/*_oracle_legal.def
gzip -9 -c "$SCRATCH/sky130_fd_sc_hd_cells_used.lef" \
  > "$REPO_ROOT/tests/corpus/legalize/sky130_fd_sc_hd_cells_used.lef.gz"
gzip -9 -c "$SCRATCH/out/gcd/gcd_tech.lef" \
  > "$REPO_ROOT/tests/corpus/legalize/sky130_fd_sc_hd_tech.lef.gz"
echo "wrote $REPO_ROOT/tests/corpus/legalize/sky130_fd_sc_hd_{cells_used,tech}.lef.gz"
