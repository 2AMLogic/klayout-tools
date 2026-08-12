#!/usr/bin/env bash
# Regenerate the tests/corpus/statime/*_netlist.v fixtures issue #809's
# spike uses -- a deliberate, reviewed act, never a CI step. See
# ./README.md for what these fixtures are and how the spike's own
# comparison (native Rust vs OpenSTA) uses them.
#
# Requires, on the host:
#   - yosys (real Yosys, not a WASI-sandboxed build -- see the `PATH`
#     override below, adapted from tests/corpus/legalize/regenerate.sh's
#     own note)
#   - a real, volare-fetched `sky130A` PDK install (`~/.volare/sky130A` or
#     any install `klt pdk find --pdk sky130A` resolves)
#
# Usage: run from the repo root:
#   ./tests/corpus/statime/regenerate.sh
#
# Review the resulting diff before committing -- a regenerated fixture
# nobody reviewed is not a meaningful regression net. Regenerating the
# OpenSTA oracle numbers in oracle_results.json is a *separate*, Docker-
# dependent step documented in README.md (not part of this script, since
# it needs the `openroad/opensta` image rather than anything this repo
# ships).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

echo "Scratch dir: $SCRATCH"

for DESIGN in gcd modexp; do
  SRC="$REPO_ROOT/examples/functional-verification/$DESIGN.v"
  cp "$SRC" "$SCRATCH/$DESIGN.v"
done
cp "$REPO_ROOT/tests/corpus/statime/mult8.v" "$SCRATCH/mult8.v"

for DESIGN in gcd mult8 modexp; do
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
  # Same YoWASP-sandbox note as tests/corpus/legalize/regenerate.sh: prepend
  # /usr/bin so a native `yosys` build (not a WASI-sandboxed one whose
  # preopens reject synthesize.py's absolute /tmp script paths) is used, if
  # present. Harmless when the host only has one `yosys` already.
  ( cd "$SCRATCH" && PATH="/usr/bin:$PATH" PDK=sky130A uv --project "$REPO_ROOT" run klt synthesize \
      "$SCRATCH/${DESIGN}_synth_request.json" --format json )

  cp "$SCRATCH/.klt/synthesize/${DESIGN}_synth.v" "$REPO_ROOT/tests/corpus/statime/${DESIGN}_netlist.v"
done

echo "Done. Review the diff:"
echo "  git -C '$REPO_ROOT' diff -- tests/corpus/statime/*_netlist.v"
