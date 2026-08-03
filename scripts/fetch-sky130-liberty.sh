#!/usr/bin/env bash
# Fetch the real sky130_fd_sc_hd standard-cell liberty view CI needs to run
# `klt synthesize`'s GCD worked example (docs/design/yosys-synthesis-spike.md
# section 4) against a real, Yosys-parseable timing library -- issue #417,
# Phase 2 of Epic #391.
#
# `scripts/fetch-pdks.sh` (lambdapdk) does **not** ship this file (the
# survey's own section 0 finding: lambdapdk's sky130 package covers
# sky130io/sky130sram, not the digital standard-cell library), and the
# upstream `google/skywater-pdk-libs-sky130_fd_sc_hd` repo only publishes a
# custom `.lib.json` format, not Yosys-consumable Liberty text. The one
# real, Yosys-parseable `.lib` text file comes from a volare-built open_pdks
# release -- the exact same distribution the spike's worked example used
# (`~/.volare/sky130A`, tagged `bdc9412b3e468c102d01b7cf6337be06ec6e9c9a`,
# per the survey's section 0 provenance note).
#
# Unlike `fetch-pdks.sh`'s whole-release-tarball pin, this does not vendor
# the full `sky130_fd_sc_hd.tar.zst` release asset (~166 MB, mostly GDS/LEF
# views a synthesis-only flow never touches): it downloads that asset once
# (checksum-verified against the pinned sha256 below), extracts only the
# one liberty view `klt synthesize`'s worked example needs
# (`tt_025C_1v80`, matching `tests/test_synthesize.py`'s own request
# fixture), and discards the rest. The temporary full download is not kept.
#
# Output lands in `pdks/sky130-liberty/sky130A/` shaped as a minimal
# open_pdks-layout variant -- `libs.tech/` (an empty marker directory, all
# `find_pdk()`'s `_probe_root` requires to recognise a variant) plus
# `libs.ref/sky130_fd_sc_hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib` -- so
# `PDK_ROOT=pdks/sky130-liberty PDK=sky130A` resolves via
# `klayout_tools.pdk.find_pdk()`/`list_pdks()` exactly like a real install,
# with no new discovery code needed.
#
# Usage: scripts/fetch-sky130-liberty.sh [--force]

set -euo pipefail

# Pinned upstream release -- bump this tag, the asset checksum below, and
# CHANGELOG.md together in the same change if this is ever refreshed. Fails
# closed on checksum mismatch, same discipline as fetch-pdks.sh/
# fetch-cell-netlists.sh.
VOLARE_TAG="sky130-bdc9412b3e468c102d01b7cf6337be06ec6e9c9a"
ASSET_URL="https://github.com/efabless/volare/releases/download/${VOLARE_TAG}/sky130_fd_sc_hd.tar.zst"
# Computed by downloading the pinned asset once and hashing it:
#   curl -fL -o /tmp/sky130_fd_sc_hd.tar.zst "$ASSET_URL"
#   shasum -a 256 /tmp/sky130_fd_sc_hd.tar.zst
ASSET_SHA256="b32d11d4905432ebc6d96b464677e2a0cc604ccfa49dae5f5824c1455999606f"

# The one liberty view fetched -- the survey's worked example / this repo's
# `tests/test_synthesize.py::test_integration_real_yosys_gcd_worked_example`
# both target this exact corner. Add more corners here (and re-verify the
# tarball member path/checksum) only if a future consumer actually needs one.
CELL_LIBRARY="sky130_fd_sc_hd"
CORNER="tt_025C_1v80"
MEMBER_PATH="sky130A/libs.ref/${CELL_LIBRARY}/lib/${CELL_LIBRARY}__${CORNER}.lib"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_ROOT="$REPO_ROOT/pdks/sky130-liberty"
VARIANT_DIR="$DEST_ROOT/sky130A"
MARKER="$DEST_ROOT/.fetched-version"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

if [[ -f "$MARKER" && $FORCE -eq 0 ]]; then
    have="$(cat "$MARKER")"
    if [[ "$have" == "$VOLARE_TAG" ]]; then
        echo "sky130_fd_sc_hd liberty already present at $VARIANT_DIR ($VOLARE_TAG; use --force to refetch)"
        exit 0
    fi
fi

sha256_of() {
    if command -v shasum &>/dev/null; then
        shasum -a 256 "$1" | awk '{print $1}'
    elif command -v sha256sum &>/dev/null; then
        sha256sum "$1" | awk '{print $1}'
    else
        echo "error: no sha256 command found (need shasum or sha256sum)" >&2
        return 1
    fi
}

if ! command -v zstd &>/dev/null && ! command -v unzstd &>/dev/null; then
    echo "error: zstd (or unzstd) is required to unpack $ASSET_URL" >&2
    echo "  Ubuntu/Debian: sudo apt-get install -y zstd" >&2
    echo "  macOS (Homebrew): brew install zstd" >&2
    exit 1
fi

tmp_tarball="$(mktemp)"
extract_dir="$(mktemp -d)"
trap 'rm -f "$tmp_tarball"; rm -rf "$extract_dir"' EXIT

echo "Fetching $ASSET_URL ..."
if ! curl -fsL --retry 3 -o "$tmp_tarball" "$ASSET_URL"; then
    echo "error: failed to fetch $ASSET_URL" >&2
    exit 1
fi

actual_sha256="$(sha256_of "$tmp_tarball")"
if [[ "$actual_sha256" != "$ASSET_SHA256" ]]; then
    echo "error: checksum mismatch for $ASSET_URL" >&2
    echo "  expected: $ASSET_SHA256" >&2
    echo "  actual:   $actual_sha256" >&2
    exit 1
fi

# `tar`, given a single named member, exits as soon as it has extracted it
# without draining the rest of the (large, ~700 MB decompressed) stream --
# the upstream decompressor then gets SIGPIPE (exit 141), which is expected,
# not a real failure. Check `tar`'s own exit status (`PIPESTATUS[1]`), not
# the pipeline's overall status, and disable `pipefail` around it so the
# expected SIGPIPE doesn't trip `set -e`.
set +o pipefail
if command -v zstd &>/dev/null; then
    zstd -d -c "$tmp_tarball" | tar -x -C "$extract_dir" "$MEMBER_PATH"
else
    unzstd -c "$tmp_tarball" | tar -x -C "$extract_dir" "$MEMBER_PATH"
fi
tar_status="${PIPESTATUS[1]}"
set -o pipefail
if [[ "$tar_status" -ne 0 ]]; then
    echo "error: failed to extract $MEMBER_PATH from $ASSET_URL (tar exit $tar_status)" >&2
    exit 1
fi

rm -rf "$VARIANT_DIR"
mkdir -p "$VARIANT_DIR/libs.tech"
mkdir -p "$(dirname "$DEST_ROOT/$MEMBER_PATH")"
mv "$extract_dir/$MEMBER_PATH" "$DEST_ROOT/$MEMBER_PATH"

echo "$VOLARE_TAG" >"$MARKER"
echo "Fetched $CELL_LIBRARY $CORNER liberty into $VARIANT_DIR"
echo "  PDK_ROOT=$DEST_ROOT PDK=sky130A"
