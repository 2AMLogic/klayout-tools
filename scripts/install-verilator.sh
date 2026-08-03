#!/usr/bin/env bash
# Build and install a pinned, reproducible Verilator for CI -- issue #423,
# Phase 3 of Epic #391.
#
# `docs/design/cocotb-verification-spike.md` was run against a locally
# Homebrew-installed `Verilator 5.050 2026-07-01` -- not yet
# pinned/provisioned for CI (the spike's own "CI provisioning" open
# question). Ubuntu 24.04's `apt` `verilator` package resolves `5.020-1`
# (verified against packages.ubuntu.com/noble/verilator) -- 30 releases
# behind the exact release the spike's worked example (`test_gcd.py`,
# section 6) was captured against, same "apt is stale" problem
# `scripts/install-yosys.sh`'s header comment documents for Yosys.
# Verilator publishes no prebuilt release assets (its GitHub project ships
# tags only, no Releases), so this builds from source, pinned to the
# official `v5.050` tag (immutable, checksum-verified) -- the tag's own
# auto-generated source archive, an exact upstream release.
#
# System build dependencies (git, autoconf, help2man, perl, python3, make,
# g++, flex, bison) are the CI workflow's responsibility to install
# (`.github/workflows/ci.yml`), same split `install-yosys.sh`'s header
# comment documents: this script only owns the versioned/checksummed
# fetch-and-build, not system package management. `autoconf` in particular
# is required here (unlike install-yosys.sh's CMake-only build): Verilator's
# source tarball ships `configure.ac` but not a pre-generated `configure`
# script, so this script runs `autoconf` itself before `./configure` --
# verified live while developing this script (skipping it leaves no
# `configure` executable to run).
#
# Verified end-to-end on a real Ubuntu 24.04 container while developing this
# script: `autoconf && ./configure && make && make install` (this script's
# exact recipe) produces a working `verilator` reporting the identical
# version string the spike itself captured -- `Verilator 5.050 2026-07-01`
# -- no drift. `--coverage`-instrumented builds and `verilator_coverage`
# (docs/design/cocotb-verification-spike.md section 5) are installed
# alongside `verilator` itself, no separate package.
#
# Usage: scripts/install-verilator.sh [--force]
#   Installs into $VERILATOR_INSTALL_PREFIX (default:
#   ~/.cache/verilator-<version>). Add "$VERILATOR_INSTALL_PREFIX/bin" to
#   $PATH after running. Idempotent: a prior successful install for the same
#   pinned version is left in place unless --force is given (matches
#   install-yosys.sh's own --force convention).

set -euo pipefail

# Pinned release -- bump the tag, asset checksum, and CHANGELOG.md together
# in the same change if this is ever refreshed. Fails closed on mismatch.
VERILATOR_VERSION="5.050"
VERILATOR_TAG="v${VERILATOR_VERSION}"
VERILATOR_ASSET_URL="https://github.com/verilator/verilator/archive/refs/tags/${VERILATOR_TAG}.tar.gz"
# Computed by downloading the pinned tag's source archive once and hashing
# it:
#   curl -fL -o /tmp/verilator-v5.050.tar.gz "$VERILATOR_ASSET_URL"
#   shasum -a 256 /tmp/verilator-v5.050.tar.gz
VERILATOR_ASSET_SHA256="ec6723f30c1798b1fbbbed97364f09c431fb4875577c314f37240e99b60a4a04"

PREFIX="${VERILATOR_INSTALL_PREFIX:-$HOME/.cache/verilator-${VERILATOR_VERSION}}"
MARKER="$PREFIX/.installed-version"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

if [[ -f "$MARKER" && $FORCE -eq 0 ]]; then
    have="$(cat "$MARKER")"
    if [[ "$have" == "$VERILATOR_TAG" ]]; then
        echo "verilator $VERILATOR_TAG already installed at $PREFIX (use --force to rebuild)"
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

echo "Fetching $VERILATOR_ASSET_URL ..."
if ! curl -fsL --retry 3 -o "$tmp_tarball" "$VERILATOR_ASSET_URL"; then
    echo "error: failed to fetch $VERILATOR_ASSET_URL" >&2
    exit 1
fi

actual_sha256="$(sha256_of "$tmp_tarball")"
if [[ "$actual_sha256" != "$VERILATOR_ASSET_SHA256" ]]; then
    echo "error: checksum mismatch for $VERILATOR_ASSET_URL" >&2
    echo "  expected: $VERILATOR_ASSET_SHA256" >&2
    echo "  actual:   $actual_sha256" >&2
    exit 1
fi

echo "Extracting into $src_dir ..."
tar -xzf "$tmp_tarball" -C "$src_dir" --strip-components=1

echo "Running autoconf (generates ./configure from configure.ac) ..."
(cd "$src_dir" && autoconf)

echo "Configuring (prefix=$PREFIX) ..."
(cd "$src_dir" && ./configure --prefix="$PREFIX")

nproc_val="$( (command -v nproc &>/dev/null && nproc) || sysctl -n hw.ncpu 2>/dev/null || echo 2)"
echo "Building (parallel=$nproc_val) ..."
(cd "$src_dir" && make -j"$nproc_val")

echo "Installing into $PREFIX ..."
rm -rf "$PREFIX"
(cd "$src_dir" && make install)

echo "$VERILATOR_TAG" >"$MARKER"
echo "Installed verilator $VERILATOR_TAG into $PREFIX"
echo "  Add to PATH: $PREFIX/bin"
