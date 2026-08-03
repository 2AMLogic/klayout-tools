#!/usr/bin/env bash
# Build and install a pinned, reproducible Icarus Verilog for CI -- issue
# #423, Phase 3 of Epic #391.
#
# `docs/design/cocotb-verification-spike.md` was run against a locally
# Homebrew-installed `Icarus Verilog version 13.0 (stable) (v13_0)` -- not
# yet pinned/provisioned for CI (the spike's own "CI provisioning" open
# question). Ubuntu 24.04's `apt` `iverilog` package resolves `12.0-2build2`
# (verified against packages.ubuntu.com/noble/iverilog) -- a full major
# version behind the exact release this repo's own worked example
# (`test_gcd.py`, spike section 6) was captured against, same "apt is stale"
# problem `scripts/install-yosys.sh`'s header comment documents for Yosys.
# This builds from source, pinned to the official `v13_0` release tag
# (immutable, checksum-verified) -- an exact upstream release, matching that
# script's own provenance discipline.
#
# System build dependencies (bison, flex, gperf, g++, gcc) are the CI
# workflow's responsibility to install (`.github/workflows/ci.yml`), same
# split `install-yosys.sh`'s header comment documents: this script only owns
# the versioned/checksummed fetch-and-build, not system package management.
#
# Verified end-to-end on a real Ubuntu 24.04 container while developing this
# script: `./configure && make && make install` (this script's exact
# recipe) produces a working `iverilog` reporting the identical version
# string the spike itself captured -- `Icarus Verilog version 13.0 (stable)
# (v13_0)` -- no drift.
#
# Usage: scripts/install-icarus-verilog.sh [--force]
#   Installs into $ICARUS_INSTALL_PREFIX (default: ~/.cache/icarus-<version>).
#   Add "$ICARUS_INSTALL_PREFIX/bin" to $PATH after running. Idempotent: a
#   prior successful install for the same pinned version is left in place
#   unless --force is given (matches install-yosys.sh's own --force
#   convention).

set -euo pipefail

# Pinned release -- bump the tag, asset checksum, and CHANGELOG.md together
# in the same change if this is ever refreshed. Fails closed on mismatch.
ICARUS_VERSION="13.0"
ICARUS_TAG="v13_0"
ICARUS_ASSET_URL="https://github.com/steveicarus/iverilog/archive/refs/tags/${ICARUS_TAG}.tar.gz"
# Computed by downloading the pinned tag's source archive once and hashing
# it:
#   curl -fL -o /tmp/iverilog-v13_0.tar.gz "$ICARUS_ASSET_URL"
#   shasum -a 256 /tmp/iverilog-v13_0.tar.gz
ICARUS_ASSET_SHA256="c897bbfa9848688982c6d5c30529fc29d68df0b9ff22ffa73bad89db73a7ce49"

PREFIX="${ICARUS_INSTALL_PREFIX:-$HOME/.cache/icarus-${ICARUS_VERSION}}"
MARKER="$PREFIX/.installed-version"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

if [[ -f "$MARKER" && $FORCE -eq 0 ]]; then
    have="$(cat "$MARKER")"
    if [[ "$have" == "$ICARUS_TAG" ]]; then
        echo "iverilog $ICARUS_TAG already installed at $PREFIX (use --force to rebuild)"
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

tmp_tarball="$(mktemp)"
src_dir="$(mktemp -d)"
trap 'rm -f "$tmp_tarball"; rm -rf "$src_dir"' EXIT

echo "Fetching $ICARUS_ASSET_URL ..."
if ! curl -fsL --retry 3 -o "$tmp_tarball" "$ICARUS_ASSET_URL"; then
    echo "error: failed to fetch $ICARUS_ASSET_URL" >&2
    exit 1
fi

actual_sha256="$(sha256_of "$tmp_tarball")"
if [[ "$actual_sha256" != "$ICARUS_ASSET_SHA256" ]]; then
    echo "error: checksum mismatch for $ICARUS_ASSET_URL" >&2
    echo "  expected: $ICARUS_ASSET_SHA256" >&2
    echo "  actual:   $actual_sha256" >&2
    exit 1
fi

echo "Extracting into $src_dir ..."
tar -xzf "$tmp_tarball" -C "$src_dir" --strip-components=1

echo "Configuring (prefix=$PREFIX) ..."
(cd "$src_dir" && ./configure --prefix="$PREFIX")

nproc_val="$( (command -v nproc &>/dev/null && nproc) || sysctl -n hw.ncpu 2>/dev/null || echo 2)"
echo "Building (parallel=$nproc_val) ..."
(cd "$src_dir" && make -j"$nproc_val")

echo "Installing into $PREFIX ..."
rm -rf "$PREFIX"
(cd "$src_dir" && make install)

echo "$ICARUS_TAG" >"$MARKER"
echo "Installed iverilog $ICARUS_TAG into $PREFIX"
echo "  Add to PATH: $PREFIX/bin"
