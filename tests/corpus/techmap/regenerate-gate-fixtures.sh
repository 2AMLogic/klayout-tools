#!/usr/bin/env bash
# Regenerate the technology-mapping *acceptance gate* fixtures (issue #875,
# Phase 2c of Epic #704) -- the (generic netlist, mapped netlist) pairs
# tests/test_techmap_equiv_gate.py proves the gate against:
#
#   adder4_generic.json         <- yosys coarse synth of adder4.v, converted
#   adder4_mapped.v             <- native/techmap's own mapping of it
#   adder4_broken_generic.json  <- same, for adder4_broken.v (carry-in dropped)
#   adder4_broken_mapped.v      <- native/techmap's own mapping of *that*
#
# `adder4_broken_mapped.v` is the seeded mismatch: a real, structurally
# valid mapper output that is functionally wrong relative to
# `adder4_generic.json`. It is deliberately produced by the real mapper
# from a real broken design rather than hand-edited -- hand-edited
# gate-level Verilog risks producing something Yosys refuses to parse
# instead of a genuine logical divergence (the discipline issue #808
# established for the RTL-stage gate's own fixtures).
#
# Kept separate from regenerate.sh because it needs strictly more than that
# script does:
#   - yosys (real Yosys, not a WASI-sandboxed build)
#   - cargo (builds native/techmap's own klt-techmap binary)
#   - a real sky130_fd_sc_hd tt_025C_1v80 liberty resolvable via `klt pdk`
#     (a volare/ciel install; PDK=sky130A)
#   - python3 + uv
#
# A deliberate, reviewed act, never a CI step -- same convention as
# regenerate.sh and tests/corpus/statime/regenerate.sh. Review the
# resulting diff before committing.
#
# Usage, from the repo root:
#   PDK=sky130A ./tests/corpus/techmap/regenerate-gate-fixtures.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
CORPUS_DIR="$REPO_ROOT/tests/corpus/techmap"
CRATE_DIR="$REPO_ROOT/native/techmap"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

echo "Scratch dir: $SCRATCH"

LIBERTY="$(cd "$REPO_ROOT" && uv run python -c "
from klayout_tools import synthesize
print(synthesize._resolve_liberty('sky130_fd_sc_hd', 'tt_025C_1v80')[0])
")"
echo "Liberty: $LIBERTY"

echo "==> building klt-techmap (cargo build --release)"
(cd "$CRATE_DIR" && cargo build --release)
TECHMAP="$CRATE_DIR/target/release/klt-techmap"

for DESIGN in adder4 adder4_broken; do
  echo "==> yosys coarse synth + techmap + simplemap: $DESIGN"
  # Same YoWASP-sandbox note as regenerate.sh: prefer a native yosys build
  # over a WASI-sandboxed one, if present. Both designs declare the same
  # top module name (`adder4`) -- the mutation is in the logic, not the
  # interface -- so each gets its own scratch directory below.
  PATH="/usr/bin:$PATH" yosys -q -p "
    read_verilog $CORPUS_DIR/$DESIGN.v
    hierarchy -top adder4
    synth -top adder4 -run coarse:fine
    techmap
    simplemap
    opt -full
    opt_clean
    write_json $SCRATCH/${DESIGN}_yosys.json
  "

  echo "==> converting to klt.synth.generic-netlist/1: $DESIGN"
  python3 "$CORPUS_DIR/yosys_to_generic.py" \
    "$SCRATCH/${DESIGN}_yosys.json" adder4 \
    -o "$CORPUS_DIR/${DESIGN}_generic.json"

  echo "==> mapping with native/techmap: $DESIGN"
  mkdir -p "$SCRATCH/$DESIGN"
  cp "$CORPUS_DIR/${DESIGN}_generic.json" "$SCRATCH/$DESIGN/"
  cat >"$SCRATCH/$DESIGN/request.json" <<EOF
{
  "schema": "klt.synth.techmap.request/1",
  "generic_netlist": "${DESIGN}_generic.json",
  "liberty": "$LIBERTY",
  "constraints": { "clock_period_ns": null }
}
EOF
  "$TECHMAP" "$SCRATCH/$DESIGN/request.json" >"$SCRATCH/$DESIGN/response.json"
  cat "$SCRATCH/$DESIGN/response.json"
  cp "$SCRATCH/$DESIGN/adder4_mapped.v" "$CORPUS_DIR/${DESIGN}_mapped.v"
done

echo "Done. Review the diff:"
echo "  git -C '$REPO_ROOT' diff -- tests/corpus/techmap/adder4*"
