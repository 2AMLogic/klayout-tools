#!/usr/bin/env bash
# Build and install a pinned, reproducible Yosys for CI -- issue #417,
# Phase 2 of Epic #391.
#
# `docs/design/yosys-synthesis-spike.md` section 3.4 found that Ubuntu
# 24.04's `apt` `yosys` package (`0.33-5build2`) is ~30 releases stale
# relative to the `0.67+post` build the survey's worked example used, and
# explicitly recommended pinning either a prebuilt release or a from-source
# build rather than `apt-get install yosys`. This builds from source, pinned
# to the official `v0.67` release tag (immutable, checksum-verified) -- an
# exact upstream release rather than an arbitrary post-tag commit, so the
# provenance here is a published, citable version.
#
# `-DYOSYS_WITHOUT_SLANG=ON` skips building the SystemVerilog frontend (the
# `slang`/`sv-elab`/`fmt` submodules): `klt synthesize` only ever emits
# `read_verilog` (Yosys survey section 1's script), never a SystemVerilog
# read, and slang is by far the heaviest single component in a full Yosys
# build -- skipping it cuts build time substantially with zero effect on
# the invocation surface this repo actually uses.
#
# System build dependencies (bison >= 3.6, flex, tcl-dev, libffi-dev,
# libreadline-dev, gawk, pkg-config, zlib1g-dev, cmake >= 3.28) are the
# CI workflow's responsibility to install (`.github/workflows/ci.yml`),
# mirroring how that workflow already installs `ngspice` directly rather
# than wrapping a package-manager call in a script -- this script only owns
# the versioned/checksummed fetch-and-build, not system package management.
#
# `libfl-dev` specifically (not just `flex`) matters here: CMake's
# `find_package(FLEX)` needs flex's own `FlexLexer.h` (which `libfl-dev`
# installs to `/usr/include/`) to compile the generated Verilog lexer
# against the *same* interface flex itself generated it for. Without it,
# CMake silently falls back to this source tree's vendored
# `libs/flex/FlexLexer.h` -- an older-style, mismatched interface (see that
# file's own README: "used if flex include directories are not found on
# the system") -- which fails to compile against a modern flex's generated
# `LexerInput`/`LexerOutput` signatures. Verified directly while developing
# this script: reproduced this exact failure on a host with an isolated,
# keg-only flex install (no discoverable `FlexLexer.h`), then confirmed
# pointing CMake at a flex install that *does* ship its own `FlexLexer.h`
# builds clean -- `libfl-dev` is what makes that the default on Ubuntu.
#
# The fetch/checksum/marker/parallelism boilerplate shared with
# install-icarus-verilog.sh and install-verilator.sh lives in
# _install_common.sh (issue #687) -- this file keeps only the pinned
# version and the cmake build step.
#
# Usage: scripts/install-yosys.sh [--force]
#   Installs into $YOSYS_INSTALL_PREFIX (default: ~/.cache/yosys-<version>).
#   Add "$YOSYS_INSTALL_PREFIX/bin" to $PATH after running. Idempotent: a
#   prior successful install for the same pinned version is left in place
#   unless --force is given (matches fetch-pdks.sh's own --force convention).

set -euo pipefail

# shellcheck source=./_install_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_install_common.sh"

# Pinned release -- bump the tag, asset checksum, and CHANGELOG.md together
# in the same change if this is ever refreshed. Fails closed on mismatch.
YOSYS_VERSION="0.67"
YOSYS_TAG="v${YOSYS_VERSION}"
# The exact commit the tag names, recorded for provenance only (the tag
# itself, not this commit sha, is what is actually fetched and verified):
#   https://github.com/YosysHQ/yosys/commit/2d1509d1bcb8df0723f6790057e3b1d21c876683
YOSYS_ASSET_URL="https://github.com/YosysHQ/yosys/releases/download/${YOSYS_TAG}/yosys.tar.gz"
# Computed by downloading the pinned tag's release asset once and hashing it:
#   curl -fL -o /tmp/yosys-0.67.tar.gz "$YOSYS_ASSET_URL"
#   shasum -a 256 /tmp/yosys-0.67.tar.gz
YOSYS_ASSET_SHA256="608d758a6efc73c9f866b0a822aa2f788c2889fcb70dcdcc0e758009465049f6"

PREFIX="${YOSYS_INSTALL_PREFIX:-$HOME/.cache/yosys-${YOSYS_VERSION}}"
MARKER="$PREFIX/.installed-version"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

check_marker "$MARKER" "$YOSYS_TAG" "$FORCE" "yosys"

tmp_tarball="$(mktemp)"
src_dir="$(mktemp -d)"
trap 'rm -f "$tmp_tarball"; rm -rf "$src_dir"' EXIT

fetch_and_verify "$YOSYS_ASSET_URL" "$YOSYS_ASSET_SHA256" "$tmp_tarball"

echo "Extracting into $src_dir ..."
tar -xzf "$tmp_tarball" -C "$src_dir"

echo "Configuring (CMAKE_INSTALL_PREFIX=$PREFIX, YOSYS_WITHOUT_SLANG=ON) ..."
cmake -B "$src_dir/build" -S "$src_dir" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DYOSYS_WITHOUT_SLANG=ON

echo "Building (parallel=$nproc_val) ..."
cmake --build "$src_dir/build" --config Release --parallel "$nproc_val"

echo "Installing into $PREFIX ..."
rm -rf "$PREFIX"
cmake --install "$src_dir/build" --strip

finish_install "$MARKER" "$YOSYS_TAG" "yosys" "$PREFIX"
