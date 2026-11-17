#!/usr/bin/env bash
# Install a pinned, checksum-verified sv2v for CI -- issue #5956.
#
# `klt synthesize`'s `sv_frontend: "sv2v"` route shells out to the real
# `sv2v` binary, and tests/test_synthesize_netlist_normalize.py's two
# real-sv2v end-to-end reproducibility tests (issue #5594's AC2/AC4)
# skip whenever `sv2v` is absent from PATH -- so before this script no CI
# leg ever exercised the real sv2v front end (the route almost every real
# design hits; sv2v emits `sv2v_cast_*` functions, so `$func$` names are
# near-universal there). `.github/workflows/ci.yml`'s `test` job runs
# this so those tests run (not skip) there.
#
# Unlike the sibling install-yosys.sh / install-icarus-verilog.sh source
# builds, sv2v is a Haskell/Stack application whose source build needs a
# ~GB toolchain CI has no reason to carry; upstream's own releases ship
# prebuilt binaries, so this installs the pinned release's
# `sv2v-Linux.zip` asset -- same fetch/checksum/marker provenance
# discipline, with upstream doing the compiling. Linux only (the CI
# runner is ubuntu-24.04 x86_64); macOS dev hosts install sv2v via
# `brew install sv2v`.
#
# Verified end-to-end while developing this script: the pinned asset's
# `sv2v` reports `sv2v v0.0.13` -- the exact version PR #5832's review
# verified the two tests against.
#
# The fetch/checksum/marker boilerplate shared with the other
# install-*.sh scripts lives in _install_common.sh (issue #687).
#
# Usage: scripts/install-sv2v.sh [--force]
#   Installs into $SV2V_INSTALL_PREFIX (default: ~/.cache/sv2v-<version>).
#   Add "$SV2V_INSTALL_PREFIX/bin" to PATH after running. Idempotent: a
#   prior successful install for the same pinned version is left in place
#   unless --force is given (matches install-yosys.sh's own --force
#   convention).

set -euo pipefail

# shellcheck source=scripts/_install_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_install_common.sh"

# Pinned release -- bump the tag, asset checksum, and the ci.yml cache
# path/key together in the same change if this is ever refreshed. Fails
# closed on mismatch.
SV2V_VERSION="0.0.13"
SV2V_TAG="v0.0.13"
SV2V_ASSET_URL="https://github.com/zachjs/sv2v/releases/download/${SV2V_TAG}/sv2v-Linux.zip"
# Computed by downloading the pinned release's asset once and hashing it:
#   curl -fL -o /tmp/sv2v-v0.0.13-linux.zip "$SV2V_ASSET_URL"
#   sha256sum /tmp/sv2v-v0.0.13-linux.zip
SV2V_ASSET_SHA256="552799a1d76cd177b9b4cc63a3e77823a3d2a6eb4ec006569288abeff28e1ff8"

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "error: install-sv2v.sh installs upstream's sv2v-Linux.zip release asset; $(uname -s) is not supported (macOS hosts: brew install sv2v)" >&2
    exit 1
fi
if ! command -v unzip &>/dev/null; then
    echo "error: unzip is required to extract the sv2v release asset (apt-get install unzip)" >&2
    exit 1
fi

PREFIX="${SV2V_INSTALL_PREFIX:-$HOME/.cache/sv2v-${SV2V_VERSION}}"
MARKER="$PREFIX/.installed-version"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

check_marker "$MARKER" "$SV2V_TAG" "$FORCE" "sv2v"

tmp_zip="$(mktemp)"
extract_dir="$(mktemp -d)"
trap 'rm -f "$tmp_zip"; rm -rf "$extract_dir"' EXIT

fetch_and_verify "$SV2V_ASSET_URL" "$SV2V_ASSET_SHA256" "$tmp_zip"

echo "Extracting into $extract_dir ..."
unzip -q "$tmp_zip" -d "$extract_dir"

echo "Installing into $PREFIX ..."
rm -rf "$PREFIX"
mkdir -p "$PREFIX/bin"
# The asset unpacks to sv2v-Linux/{sv2v,LICENSE,NOTICE,CHANGELOG.md}; only
# the binary is installed (the other files stay in the extracted copy).
install -m 0755 "$extract_dir/sv2v-Linux/sv2v" "$PREFIX/bin/sv2v"

finish_install "$MARKER" "$SV2V_TAG" "sv2v" "$PREFIX"
