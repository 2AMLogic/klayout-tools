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
# The fetch/checksum/marker/parallelism boilerplate shared with
# install-verilator.sh and install-yosys.sh lives in _install_common.sh
# (issue #687) -- this file keeps only the pinned version and the
# configure/make build step.
#
# Usage: scripts/install-icarus-verilog.sh [--force]
#   Installs into $ICARUS_INSTALL_PREFIX (default: ~/.cache/icarus-<version>).
#   Add "$ICARUS_INSTALL_PREFIX/bin" to $PATH after running. Idempotent: a
#   prior successful install for the same pinned version is left in place
#   unless --force is given (matches install-yosys.sh's own --force
#   convention).

set -euo pipefail

# shellcheck source=./_install_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_install_common.sh"

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

check_marker "$MARKER" "$ICARUS_TAG" "$FORCE" "iverilog"

tmp_tarball="$(mktemp)"
src_dir="$(mktemp -d)"
trap 'rm -f "$tmp_tarball"; rm -rf "$src_dir"' EXIT

fetch_and_verify "$ICARUS_ASSET_URL" "$ICARUS_ASSET_SHA256" "$tmp_tarball"

echo "Extracting into $src_dir ..."
tar -xzf "$tmp_tarball" -C "$src_dir" --strip-components=1

echo "Configuring (prefix=$PREFIX) ..."
(cd "$src_dir" && ./configure --prefix="$PREFIX")

echo "Building (parallel=$nproc_val) ..."
(cd "$src_dir" && make -j"$nproc_val")

echo "Installing into $PREFIX ..."
rm -rf "$PREFIX"
(cd "$src_dir" && make install)

finish_install "$MARKER" "$ICARUS_TAG" "iverilog" "$PREFIX"
