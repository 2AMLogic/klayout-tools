#!/usr/bin/env bash
# Regenerate the mapped-netlist fixtures in tests/corpus/sta/netlists/ that
# issue #809's STA spike compares OpenSTA and `native/sta/` against -- a
# deliberate, reviewed act, never a CI step. See ./README.md for what these
# are, and native/sta/README.md for the spike's own verdict.
#
# Requires on the host:
#   - `yosys` (native build; see the PATH note below)
#   - a real, volare-fetched `sky130A` install (`klt pdk find` must resolve it)
#
# Deliberately does NOT need OpenROAD, Docker, or a PDK LEF: this spike's
# comparison is netlist + liberty only, with no placement and no parasitics.
#
# Usage, from the repo root:
#   ./tests/corpus/sta/regenerate.sh
#
# Review the resulting diff before committing -- a regenerated fixture nobody
# reviewed is not a meaningful regression net.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
OUT_DIR="$REPO_ROOT/tests/corpus/sta/netlists"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

mkdir -p "$OUT_DIR"
echo "Scratch dir: $SCRATCH"

# design:source-path pairs. `gcd`/`modexp` are the repo's existing worked
# examples (shared with tests/corpus/legalize/, so the two spikes' corpora
# overlap deliberately); the rest were written for this slice, see ./README.md.
DESIGNS=(
  "gcd:examples/functional-verification/gcd.v"
  "modexp:examples/functional-verification/modexp.v"
  "alu8:tests/corpus/sta/rtl/alu8.v"
  "crc32:tests/corpus/sta/rtl/crc32.v"
  "fifo16:tests/corpus/sta/rtl/fifo16.v"
  "mac16:tests/corpus/sta/rtl/mac16.v"
)

for ENTRY in "${DESIGNS[@]}"; do
  DESIGN="${ENTRY%%:*}"
  SRC="${ENTRY#*:}"
  echo "=== $DESIGN ==="
  cp "$REPO_ROOT/$SRC" "$SCRATCH/$DESIGN.v"

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

  # `/opt/homebrew/bin` (or `/usr/bin`) prepended: some hosts' default `yosys`
  # is a YoWASP (WASI-sandboxed) build whose sandbox only preopens the
  # process's own cwd and rejects the absolute paths `synthesize.py` writes
  # into its generated `.ys` script -- the same note tests/corpus/legalize/
  # regenerate.sh carries, for the same reason.
  ( cd "$REPO_ROOT" && PATH="/opt/homebrew/bin:/usr/bin:$PATH" PDK=sky130A \
      uv run klt synthesize "$SCRATCH/${DESIGN}_synth_request.json" \
      --format json > "$SCRATCH/${DESIGN}_synth_response.json" )

  python3 - "$SCRATCH/${DESIGN}_synth_response.json" "$OUT_DIR/${DESIGN}.meta.json" <<'PY'
import json, sys
resp = json.load(open(sys.argv[1]))
# Keep only what the comparison harness reports alongside the timing numbers;
# the full response (with its absolute scratch paths) is not reproducible.
json.dump(
    {
        "design": resp["hdl_toplevel"],
        "instance_count": resp["instance_count"],
        "area_um2": resp["area_um2"],
        "sequential_area_um2": resp.get("sequential_area_um2"),
        "engine": resp["engine"],
        "engine_version": resp["engine_version"],
        "deck": resp["provenance"]["deck"],
        "pdk_version": resp["provenance"]["pdk"]["version"],
    },
    open(sys.argv[2], "w"),
    indent=2,
    sort_keys=True,
)
open(sys.argv[2], "a").write("\n")
PY

  gzip -9 -c "$SCRATCH/.klt/synthesize/${DESIGN}_synth.v" > "$OUT_DIR/${DESIGN}_synth.v.gz"
  echo "wrote $OUT_DIR/${DESIGN}_synth.v.gz"
done

echo
echo "Done. Next: ./tests/corpus/sta/compare.py --help"
