#!/usr/bin/env bash
# Install a pinned, reproducible SymbiYosys (sby) plus a pinned SAT/SMT
# backend (Bitwuzla) for CI -- issue #1312, Phase 2 of Epic #707.
#
# `docs/design/sequential-equivalence-survey.md` section 4.1 (Priority 1,
# "prerequisite for everything below") identifies `sby` -- the orchestration
# tool that drives Yosys's `equiv_make`/`equiv_induct`/`mode equiv`/
# `mode prove` commands -- as the first thing Phase 2's register-
# correspondence sequential-equivalence engine needs and does not yet have:
# nothing in that survey's own section 4.2-4.4 can be built, let alone
# oracle-validated, without a real `sby` to compare against. This script
# closes that gap the same way `scripts/install-yosys.sh` closed it for
# Yosys itself: pin an exact upstream release, fetch it, verify its
# checksum, install it reproducibly, fail closed on mismatch.
#
# Pinned to sby's `v0.67` tag specifically (not just "the latest sby") --
# YosysHQ/sby tags each release against the exact Yosys release it was built
# and tested with (this tag's own annotation reads literally "SBY with
# Yosys 0.67"), and `scripts/install-yosys.sh` already pins this repo's CI
# to Yosys `v0.67`. Pinning both to the same upstream pairing avoids a
# version-skew combination YosysHQ itself never tested together.
#
# `sby` itself needs no compiler: unlike Yosys/Icarus Verilog/Verilator,
# upstream's own `make install` only copies `sbysrc/sby_*.py` into
# `$PREFIX/share/yosys/python3/` and generates a `$PREFIX/bin/sby` launcher
# script (a `sed`-templated copy of `sbysrc/sby.py`) -- no cmake/configure
# step, matching this script's own installer to that shape instead of the
# fetch-and-build cmake recipe `install-yosys.sh` uses. At runtime, `sby`
# needs Yosys (built by `scripts/install-yosys.sh`) and `yosys-smtbmc`
# (installed as part of that same Yosys build) on `$PATH` -- neither
# vendored nor duplicated here.
#
# The SAT/SMT backend -- Bitwuzla, pinned to release `0.9.1` -- is what
# `yosys-smtbmc`'s `smtbmc bitwuzla` engine actually invokes for the
# BMC/k-induction solving `mode equiv`/`mode prove` jobs need (the survey's
# section 4.1 flags this as a required, explicitly-pinned choice, not
# "whatever happens to be on $PATH"). Unlike the four `install-*.sh`
# scripts above, this fetches a prebuilt static release asset rather than
# building from source: Bitwuzla's upstream project itself publishes
# checksummed, statically-linked (against its own C++ dependencies; still
# dynamically linked against system libgmp10/libmpfr6/libstdc++6 --
# `.github/workflows/ci.yml`'s job installs those the same "CI owns system
# packages" way it already does for the four scripts above) binaries per
# release, so building from source here would only add build time for zero
# reproducibility benefit over verifying the upstream binary's own
# checksum.
#
# Verified end-to-end while developing this script: `sby --version` reports
# `SBY v0.67`; a `mode bmc` job using `smtbmc bitwuzla` against a trivial
# clocked design (a 4-bit free-running counter with a `count <= 15`
# assertion) reaches `DONE (PASS, rc=0)` when run against a *current*
# `yosys`/`yosys-smtbmc` pair. Reproducing this against Ubuntu 24.04's
# stale `apt` Yosys (`0.33-5build2` -- the same staleness
# `install-yosys.sh`'s header comment documents) instead fails with
# "Unexpected response from solver: [error] invalid option '--smt2'": that
# old `smtio.py` unconditionally passes Bitwuzla the pre-0.3 `--smt2` flag
# instead of probing `bitwuzla --help` for the `--lang` flag Bitwuzla 0.9.1
# actually expects. This is exactly why this script -- like the Yosys/
# Icarus/Verilator installers before it -- exists instead of an
# `apt-get install` one-liner, and why the CI wiring below must run this
# step (and put its `sby`/`yosys-smtbmc` on `$PATH`) *after* the pinned
# Yosys build, never before/instead of it.
#
# Bitwuzla ships prebuilt static binaries for Linux x86_64/arm64 and macOS
# arm64 (no macOS x86_64 asset upstream); this script supports exactly
# those three, matching `uname -s`/`uname -m` to the corresponding pinned,
# checksummed release asset. `ubuntu-latest` GitHub Actions runners are
# Linux x86_64, the only platform this repo's CI actually exercises.
#
# The fetch/checksum/marker boilerplate shared with install-icarus-
# verilog.sh/install-verilator.sh/install-yosys.sh lives in
# _install_common.sh (issue #687) -- this file reuses `fetch_and_verify`/
# `check_marker`/`finish_install`/`sha256_of` unchanged; it does not need
# `nproc_val` since nothing here is compiled.
#
# Usage: scripts/install-symbiyosys.sh [--force]
#   Installs into $SYMBIYOSYS_INSTALL_PREFIX (default:
#   ~/.cache/symbiyosys-<version>). Add "$SYMBIYOSYS_INSTALL_PREFIX/bin" to
#   $PATH after running (this is where both `sby` and `bitwuzla` land).
#   Idempotent: a prior successful install for the same pinned sby+Bitwuzla
#   pair is left in place unless --force is given (matches
#   install-yosys.sh's own --force convention). Requires `make` and
#   `unzip` on $PATH (both ship on ubuntu-latest by default).

set -euo pipefail

# shellcheck source=scripts/_install_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_install_common.sh"

if ! command -v unzip &>/dev/null; then
    echo "error: 'unzip' not found on PATH -- required to extract the pinned Bitwuzla release asset" >&2
    exit 1
fi

# --- SymbiYosys (sby) -- pinned release ------------------------------------
# Bump the tag, asset checksum, and this header comment together in the
# same change if this is ever refreshed; keep it paired with whatever
# Yosys tag install-yosys.sh pins. Fails closed on mismatch.
SBY_VERSION="0.67"
SBY_TAG="v${SBY_VERSION}"
# The exact commit the tag names, recorded for provenance only (the tag
# itself, not this commit sha, is what is actually fetched and verified):
#   https://github.com/YosysHQ/sby/commit/8f8833c6176be263907dea5b50da7759632aaff6
SBY_ASSET_URL="https://github.com/YosysHQ/sby/archive/refs/tags/${SBY_TAG}.tar.gz"
# Computed by downloading the pinned tag's source archive once and hashing
# it:
#   curl -fL -o /tmp/sby-v0.67.tar.gz "$SBY_ASSET_URL"
#   shasum -a 256 /tmp/sby-v0.67.tar.gz
SBY_ASSET_SHA256="ee7b050c967ac3efe0120743322b2b2e2be682cc799c847ed843c8e7dad76be6"

# --- Bitwuzla SAT/SMT backend -- pinned release -----------------------------
# Bump the version and all three asset checksums together in the same
# change if this is ever refreshed. Fails closed on mismatch.
BITWUZLA_VERSION="0.9.1"
BITWUZLA_RELEASE_BASE_URL="https://github.com/bitwuzla/bitwuzla/releases/download/${BITWUZLA_VERSION}"
# Computed by downloading each pinned release asset once and hashing it:
#   curl -fL -o /tmp/bw-linux-x86_64.zip "$BITWUZLA_RELEASE_BASE_URL/Bitwuzla-Linux-x86_64-static.zip"
#   shasum -a 256 /tmp/bw-linux-x86_64.zip
#   (repeat for the arm64/macOS assets below)
BITWUZLA_LINUX_X86_64_ASSET="Bitwuzla-Linux-x86_64-static.zip"
BITWUZLA_LINUX_X86_64_SHA256="057f1546ae2068df57beb178f3eeab1678f0e5f0c378787a05b7bb294617c9c6"
BITWUZLA_LINUX_ARM64_ASSET="Bitwuzla-Linux-arm64-static.zip"
BITWUZLA_LINUX_ARM64_SHA256="f2e9f77b5f5c5d6a7bbb2c0fbea096952f61f9fd5d387f0a06fd235f2ec0d3a1"
BITWUZLA_MACOS_ARM64_ASSET="Bitwuzla-macOS-arm64-static.zip"
BITWUZLA_MACOS_ARM64_SHA256="86a6fb1af2b7cdaf3f7807662ab679088113bbf3e55d243597f98d826bcb7511"

case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)
        BITWUZLA_ASSET="$BITWUZLA_LINUX_X86_64_ASSET"
        BITWUZLA_ASSET_SHA256="$BITWUZLA_LINUX_X86_64_SHA256"
        ;;
    Linux-aarch64 | Linux-arm64)
        BITWUZLA_ASSET="$BITWUZLA_LINUX_ARM64_ASSET"
        BITWUZLA_ASSET_SHA256="$BITWUZLA_LINUX_ARM64_SHA256"
        ;;
    Darwin-arm64)
        BITWUZLA_ASSET="$BITWUZLA_MACOS_ARM64_ASSET"
        BITWUZLA_ASSET_SHA256="$BITWUZLA_MACOS_ARM64_SHA256"
        ;;
    *)
        echo "error: no pinned Bitwuzla $BITWUZLA_VERSION asset for $(uname -s)-$(uname -m)" >&2
        echo "  supported: Linux x86_64, Linux arm64, macOS arm64" >&2
        exit 1
        ;;
esac
BITWUZLA_ASSET_URL="${BITWUZLA_RELEASE_BASE_URL}/${BITWUZLA_ASSET}"

# Single marker tag covering both pinned tools: bumping either version
# invalidates the install-marker (and, in CI, the actions/cache entry
# keyed off this script's own hash) automatically.
SYMBIYOSYS_TAG="sby-${SBY_TAG}+bitwuzla-${BITWUZLA_VERSION}"

PREFIX="${SYMBIYOSYS_INSTALL_PREFIX:-$HOME/.cache/symbiyosys-${SBY_VERSION}}"
MARKER="$PREFIX/.installed-version"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

check_marker "$MARKER" "$SYMBIYOSYS_TAG" "$FORCE" "symbiyosys"

sby_tarball="$(mktemp)"
sby_src_dir="$(mktemp -d)"
bitwuzla_zip="$(mktemp)"
bitwuzla_dir="$(mktemp -d)"
trap 'rm -f "$sby_tarball" "$bitwuzla_zip"; rm -rf "$sby_src_dir" "$bitwuzla_dir"' EXIT

fetch_and_verify "$SBY_ASSET_URL" "$SBY_ASSET_SHA256" "$sby_tarball"

echo "Extracting sby into $sby_src_dir ..."
tar -xzf "$sby_tarball" -C "$sby_src_dir" --strip-components=1

fetch_and_verify "$BITWUZLA_ASSET_URL" "$BITWUZLA_ASSET_SHA256" "$bitwuzla_zip"

echo "Extracting Bitwuzla into $bitwuzla_dir ..."
unzip -q "$bitwuzla_zip" -d "$bitwuzla_dir"

echo "Installing into $PREFIX ..."
rm -rf "$PREFIX"
mkdir -p "$PREFIX/bin"

# sby's own Makefile creates $PREFIX/bin and $PREFIX/share/yosys/python3
# itself and generates a $PREFIX/bin/sby launcher whose sys.path is
# computed relative to its own location -- no absolute PREFIX baked in, so
# this install is fully relocatable.
make -C "$sby_src_dir" install "PREFIX=$PREFIX"

bitwuzla_bin="$bitwuzla_dir/${BITWUZLA_ASSET%.zip}/bin/bitwuzla"
if [[ ! -f "$bitwuzla_bin" ]]; then
    echo "error: expected Bitwuzla binary not found at $bitwuzla_bin after extraction" >&2
    exit 1
fi
install -m 0755 "$bitwuzla_bin" "$PREFIX/bin/bitwuzla"

finish_install "$MARKER" "$SYMBIYOSYS_TAG" "symbiyosys (sby ${SBY_TAG} + bitwuzla ${BITWUZLA_VERSION})" "$PREFIX"
