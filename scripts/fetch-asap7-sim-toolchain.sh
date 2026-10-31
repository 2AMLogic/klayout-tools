#!/usr/bin/env bash
# Provision the ngspice/OSDI half of an ASAP7 FinFET simulation flow (issue
# #2814, part of #2761).
#
# ASAP7's device models (lambdapdk `asap7/base/spice/hspice/7nm_*_160803.pm`)
# are BSIM-CMG `level = 72` HSPICE cards. ngspice has no built-in BSIM-CMG
# (its `Only MOS device levels 1-6,8-10,14,49,54-58,60,68,73 are supported`
# error is the symptom); it can only run BSIM-CMG as an OSDI module compiled
# from the Verilog-A source. This script:
#
#   1. Fetches the CMC BSIM-CMG Verilog-A source from a commit-pinned,
#      checksum-verified tarball of dwarning/VA-Models (the same upstream
#      collection the SG13G2 flow's PSP103 sources come from), into the
#      ignored `pdks/asap7-sim/` store. Nothing fetched or generated is ever
#      tracked.
#   2. Reuses the checksum-pinned OpenVAF-Reloaded compiler pin of
#      `fetch-sg13g2-sim-toolchain.sh` (kept in step by hand: bump both
#      together) and compiles `bsimcmg.va` to `pdks/asap7-sim/osdi/bsimcmg.osdi`.
#   3. Preflights the ngspice on PATH against the OSDI ABI the compiler emits
#      and *actually loads* the module (`pre_osdi` + `devhelp`), failing
#      closed with a named missing capability -- never treating file
#      existence as proof of compatibility.
#
# Usage: scripts/fetch-asap7-sim-toolchain.sh [--force]
#
# The pinned openvaf-r asset is a Linux x86-64 binary. On any other platform
# set OPENVAF_R_BIN to a compatible openvaf-r (or run on a Linux x86-64 host
# or container); otherwise this script fails closed naming the platform.
# `KLT_ASAP7_OSDI_ONLY_PREFLIGHT=1` runs only the ngspice capability preflight.
#
# The model is BSIM-CMG 111.0.0 (`module bsimcmg_va`); ASAP7's cards declare
# `version = 107`. `klayout_tools.asap7_bsimcmg` documents and applies the
# card adaptation (see docs/cli/sim.md, "ASAP7 FinFET (BSIM-CMG) simulation").

set -euo pipefail

# shellcheck source=scripts/_install_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_install_common.sh"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STORE="${KLT_ASAP7_SIM_STORE:-$REPO_ROOT/pdks/asap7-sim}"
VA_DIR="$STORE/bsimcmg-va"
OSDI_DIR="$STORE/osdi"
OSDI_FILE="$OSDI_DIR/bsimcmg.osdi"
MARKER="$OSDI_DIR/.compiled-version"

# BSIM-CMG Verilog-A source: dwarning/VA-Models @ commit, directory
# code/bsimcmg/vacode111 (BSIM-CMG 111.0.0, ECL-2.0 licensed; see the
# LICENSE.txt/NOTICE.txt shipped beside the source).
VA_MODELS_COMMIT="6099b4e12e6f66ff530f59b8d01fb909094be85a"
VA_MODELS_URL="https://codeload.github.com/dwarning/VA-Models/tar.gz/${VA_MODELS_COMMIT}"
VA_MODELS_SHA256="357d69569a6fbfd7632887f28a1b3f26102605e1e64ccfcfb2de85b2399af0ab"
VA_SUBDIR="code/bsimcmg/vacode111"

# Same OpenVAF-Reloaded pin as fetch-sg13g2-sim-toolchain.sh.
OPENVAF_R_TAG="osdi_0.4-153-g2e066436"
OPENVAF_R_URL="https://fides.fe.uni-lj.si/openvaf/download/openvaf-reloaded-${OPENVAF_R_TAG}-linux_x64.tar.gz"
OPENVAF_R_SHA256="9c570bf8f3e29fed5bac66298bf0b631a40a9075c5fe1ce3f6754cadd106df04"
OPENVAF_R_OSDI_ABI="0.4"
NGSPICE_MIN_MAJOR=44

COMPILER_PREFIX="${OPENVAF_R_INSTALL_PREFIX:-$HOME/.cache/openvaf-r-${OPENVAF_R_TAG}}"
COMPILER_BIN="${OPENVAF_R_BIN:-$COMPILER_PREFIX/bin/openvaf-r}"
COMPILER_MARKER="$COMPILER_PREFIX/.installed-version"
STAMP="${VA_MODELS_COMMIT}+${OPENVAF_R_TAG}"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

preflight_ngspice() {
    if ! command -v ngspice &>/dev/null; then
        echo "error: ngspice not found on PATH -- BSIM-CMG needs ngspice >= $NGSPICE_MIN_MAJOR built with OSDI support (missing capability: ngspice)" >&2
        return 1
    fi
    local banner major
    banner="$(ngspice --version 2>&1)"
    major="$(echo "$banner" | grep -oE 'ngspice-[0-9]+' | head -1 | grep -oE '[0-9]+' || true)"
    if [[ -n "$major" ]] && ((major < NGSPICE_MIN_MAJOR)); then
        echo "error: ngspice-$major is below ngspice-$NGSPICE_MIN_MAJOR (missing capability: OSDI ABI $OPENVAF_R_OSDI_ABI support, see fetch-sg13g2-sim-toolchain.sh)" >&2
        return 1
    fi
    echo "ngspice-${major:-?} on PATH clears the OSDI ABI $OPENVAF_R_OSDI_ABI floor."
}

fetch_va_source() {
    if [[ $FORCE -eq 0 && -f "$VA_DIR/.commit" && "$(cat "$VA_DIR/.commit")" == "$VA_MODELS_COMMIT" ]]; then
        echo "BSIM-CMG Verilog-A source $VA_MODELS_COMMIT already at $VA_DIR"
        return 0
    fi
    local tarball extract
    tarball="$(mktemp)"
    extract="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -f '$tarball'; rm -rf '$extract'" RETURN
    fetch_and_verify "$VA_MODELS_URL" "$VA_MODELS_SHA256" "$tarball"
    tar -xzf "$tarball" -C "$extract"
    rm -rf "$VA_DIR"
    mkdir -p "$VA_DIR"
    cp -R "$extract/VA-Models-${VA_MODELS_COMMIT}/${VA_SUBDIR}/." "$VA_DIR/"
    echo "$VA_MODELS_COMMIT" >"$VA_DIR/.commit"
}

fetch_compiler() {
    if [[ -n "${OPENVAF_R_BIN:-}" ]]; then
        [[ -x "$OPENVAF_R_BIN" ]] || { echo "error: OPENVAF_R_BIN=$OPENVAF_R_BIN is not executable" >&2; exit 1; }
        return 0
    fi
    if [[ "$(uname -s)-$(uname -m)" != "Linux-x86_64" ]]; then
        cat >&2 <<MSG
error: the pinned openvaf-r $OPENVAF_R_TAG asset is Linux x86-64 only; this host is $(uname -s)-$(uname -m)
  (missing capability: OpenVAF compiler for this platform)
  Run this script on a Linux x86-64 host/container, or point OPENVAF_R_BIN at
  a compatible openvaf-r build.
MSG
        exit 1
    fi
    if [[ $FORCE -eq 0 && -f "$COMPILER_MARKER" && -x "$COMPILER_BIN" && "$(cat "$COMPILER_MARKER")" == "$OPENVAF_R_TAG" ]]; then
        return 0
    fi
    local tarball extract
    tarball="$(mktemp)"
    extract="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -f '$tarball'; rm -rf '$extract'" RETURN
    fetch_and_verify "$OPENVAF_R_URL" "$OPENVAF_R_SHA256" "$tarball"
    tar -xzf "$tarball" -C "$extract"
    mkdir -p "$COMPILER_PREFIX/bin"
    install -m 0755 "$extract/openvaf-r" "$COMPILER_BIN"
    finish_install "$COMPILER_MARKER" "$OPENVAF_R_TAG" "openvaf-r" "$COMPILER_PREFIX"
}

# Load-verify through ngspice itself; a file that exists is not a model that
# loads. `devhelp` lists the module (`bsimcmg_va`) only on a successful load.
verify_osdi_load() {
    local deck out status=0
    deck="$(mktemp "${TMPDIR:-/tmp}/asap7-osdi-verify.XXXXXX")"
    cat >"$deck" <<DECK
* OSDI load verification -- scripts/fetch-asap7-sim-toolchain.sh
.control
pre_osdi $OSDI_FILE
devhelp
quit
.endc
.end
DECK
    out="$(ngspice -b "$deck" 2>&1)" || status=$?
    rm -f "$deck"
    if [[ $status -ne 0 ]] || echo "$out" | grep -qiE "only supports OSDI|couldn't be loaded|undefined symbol"; then
        echo "error: ngspice failed to load $OSDI_FILE (missing capability: BSIM-CMG OSDI module)" >&2
        echo "$out" | grep -iE "only supports OSDI|couldn't be loaded|undefined symbol" >&2 || true
        return 1
    fi
    if ! echo "$out" | grep -qi "bsimcmg_va"; then
        echo "error: ngspice loaded $OSDI_FILE but devhelp does not list bsimcmg_va (missing capability: BSIM-CMG OSDI module)" >&2
        return 1
    fi
    echo "  OK: $OSDI_FILE loads in $(command -v ngspice) ($(echo "$out" | grep -oE 'ngspice-[0-9]+' | head -1))"
}

preflight_ngspice || exit 1
if [[ "${KLT_ASAP7_OSDI_ONLY_PREFLIGHT:-0}" == "1" ]]; then
    exit 0
fi

if [[ $FORCE -eq 1 || ! -f "$OSDI_FILE" || "$(cat "$MARKER" 2>/dev/null)" != "$STAMP" ]]; then
    fetch_va_source
    fetch_compiler
    mkdir -p "$OSDI_DIR"
    echo "Compiling BSIM-CMG ($VA_SUBDIR) with $COMPILER_BIN ..."
    "$COMPILER_BIN" -o "$OSDI_FILE" "$VA_DIR/bsimcmg.va"
    echo "$STAMP" >"$MARKER"
else
    echo "bsimcmg.osdi already compiled for $STAMP (use --force to rebuild)"
fi

verify_osdi_load
echo "sha256 $(sha256_of "$OSDI_FILE")  $OSDI_FILE"
echo "Done: $OSDI_FILE"
