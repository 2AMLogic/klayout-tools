#!/usr/bin/env bash
# Regenerate tests/corpus/techmap/*_generic.json -- issue #874's own
# interim on-ramp fixtures (docs/design/synth-techmap-stage-contract.md
# section 2.4), the generic-netlist inputs `native/techmap`'s benchmark
# (compare.py) maps and compares against Yosys/abc. A deliberate, reviewed
# act, never a CI step -- same convention as
# tests/corpus/statime/regenerate.sh.
#
# Requires, on the host:
#   - yosys (real Yosys, not a WASI-sandboxed build)
#   - python3, no extra packages
#
# Usage: run from the repo root:
#   ./tests/corpus/techmap/regenerate.sh
#
# Review the resulting diff before committing -- a regenerated fixture
# nobody reviewed is not a meaningful regression net.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
CORPUS_DIR="$REPO_ROOT/tests/corpus/techmap"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

echo "Scratch dir: $SCRATCH"

cp "$REPO_ROOT/examples/functional-verification/gcd.v" "$SCRATCH/gcd.v"
cp "$REPO_ROOT/examples/functional-verification/modexp.v" "$SCRATCH/modexp.v"
cp "$REPO_ROOT/tests/corpus/statime/mult8.v" "$SCRATCH/mult8.v"

for DESIGN in gcd mult8 modexp; do
  echo "==> yosys coarse synth + techmap + simplemap: $DESIGN"
  # Same YoWASP-sandbox note as tests/corpus/statime/regenerate.sh: prefer a
  # native yosys build over a WASI-sandboxed one, if present.
  PATH="/usr/bin:$PATH" yosys -q -p "
    read_verilog $SCRATCH/$DESIGN.v
    hierarchy -top $DESIGN
    synth -top $DESIGN -run coarse:fine
    techmap
    simplemap
    opt -full
    opt_clean
    write_json $SCRATCH/${DESIGN}_yosys.json
  "

  echo "==> converting to klt.synth.generic-netlist/1: $DESIGN"
  python3 "$CORPUS_DIR/yosys_to_generic.py" \
    "$SCRATCH/${DESIGN}_yosys.json" "$DESIGN" \
    -o "$CORPUS_DIR/${DESIGN}_generic.json"
done

echo "Done. Review the diff:"
echo "  git -C '$REPO_ROOT' diff -- tests/corpus/techmap/*_generic.json"
