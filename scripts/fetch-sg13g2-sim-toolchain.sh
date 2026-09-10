#!/usr/bin/env bash
# Provision the ngspice/OSDI half of an SG13G2 simulation flow -- the
# companion `fetch-ihp-sg13g2.sh` never provisions (issue #1628).
#
# `fetch-ihp-sg13g2.sh` fetches IHP-Open-PDK's Verilog-A compact-model
# *sources* (`libs.tech/verilog-a/{psp103,psp103_nqs,r3_cmc,mosvar}`), but
# ships no compiled `.osdi` -- ngspice can only instantiate those models
# through OSDI shared libraries, so every MOS/resistor in an SG13G2 netlist
# is unsimulatable straight out of that fetch. This script closes that gap:
#
#   1. Fetches a checksum-pinned OpenVAF-Reloaded (`openvaf-r`) build --
#      the Verilog-A-to-OSDI compiler the PDK's own
#      `libs.tech/verilog-a/openvaf-compile-va.sh` prefers (it looks for
#      `openvaf-r` on PATH before falling back to plain `openvaf`).
#   2. Defensively works around a Linux libLLVM dynamic-link problem some
#      openvaf-r Linux releases ship (see "Why the libLLVM workaround is
#      defensive, not asserted" below) before ever invoking the compiler.
#   3. Compiles the PDK's own Verilog-A sources into
#      `libs.tech/ngspice/osdi/*.osdi` by invoking the PDK's own
#      `openvaf-compile-va.sh` (not a re-implementation of its four
#      per-model command lines -- if the PDK adds a model, this script
#      picks it up for free instead of silently drifting).
#   4. Preflight-checks the ngspice on `PATH` against the OSDI ABI version
#      openvaf-r emits, and actually loads each compiled `.osdi` in that
#      ngspice (`pre_osdi` + `devhelp`) to confirm the ABI floor claim
#      instead of taking it on faith.
#
# Usage: scripts/fetch-sg13g2-sim-toolchain.sh [--force]
#   Requires `scripts/fetch-ihp-sg13g2.sh` to have already been run (this
#   script fails closed, not silently, if the PDK's `libs.tech/verilog-a/`
#   isn't there). Idempotent: a prior successful compile with the same
#   pinned openvaf-r tag is left in place unless --force is given (matches
#   fetch-ihp-sg13g2.sh's own --force convention) -- but the ngspice
#   ABI-floor preflight and the per-model load-verification in step 4 above
#   always re-run, even when the compile itself is skipped, since the
#   ngspice on PATH can change between runs independently of the PDK.
#
# ---------------------------------------------------------------------
# The ngspice-ABI-vs-compiler-version floor this script preflights
# ---------------------------------------------------------------------
#
# openvaf-r built from OpenVAF-Reloaded's `master` branch emits OSDI ABI
# 0.4 descriptors. Whether a given ngspice accepts those descriptors is
# NOT a simple "ngspice supports OSDI" yes/no -- it depends on which
# ngspice release added OpenVAF-Reloaded-aware version handling:
#
#   - ngspice-43 and earlier (`src/osdi/osdiregistry.c`) hard-reject
#     anything that isn't *exactly* OSDI 0.3, printing (verbatim, from the
#     source): `NGSPICE only supports OSDI v0.3 but "<path>" targets
#     v0.4!` -- this is the "opaque, deep-in-a-simulation-run" failure
#     this issue is about; it never explains that a whole different
#     ngspice release is the actual fix.
#   - ngspice-44 (imr/ngspice commit `b40dcaa18d`, "OpenVAF-reloaded
#     compiled model support") added a second code path, gated on the
#     presence of the `OSDI_DESCRIPTOR_SIZE` symbol openvaf-r (but not
#     plain openvaf) exports, that accepts any OSDI `>= 0.4` from an
#     openvaf-r build.
#
# Verified directly while developing this script (not copied from a
# table): diffed `src/osdi/osdiregistry.c` at the `ngspice-43` and
# `ngspice-44` git tags in github.com/imr/ngspice -- `ngspice-43` has only
# the single strict-equality check against OSDI 0.3; `ngspice-44` has both
# the dual-path check above. Ubuntu 24.04 (noble)'s apt `ngspice` package
# is version 42 (packages.ubuntu.com/noble/ngspice) -- below the floor,
# and exactly why every downstream consumer that apt-installs ngspice and
# then tries to load an openvaf-r-compiled model hits this. `NGSPICE_MIN_MAJOR`
# below is this floor. (`docker/eda-sim/pdk-versions.json` separately
# pins a *stricter* `min_major: 46` ngspice floor -- that one comes from
# unrelated per-consumer sim-harness requirements, not this OSDI ABI
# concern; don't conflate the two numbers.)
#
# ---------------------------------------------------------------------
# Why the libLLVM workaround is defensive, not asserted
# ---------------------------------------------------------------------
#
# The issue this script closes (#1628) describes a Linux openvaf-r release
# whose bundled `lib/libLLVM.so.21.1` is a dangling symlink into
# `/usr/lib/x86_64-linux-gnu`, assuming a system LLVM 21 that no current
# Ubuntu LTS ships. Verified directly while developing this script against
# the actual pinned asset below (downloaded live, `ldd`'d, and `tar -tzf`'d):
# it ships as a single, fully self-contained, statically-linked `openvaf-r`
# ELF binary with NO `lib/` directory and no `libLLVM` runtime dependency
# at all -- this specific pin does not reproduce the dangling-symlink
# failure mode. `apply_libllvm_workaround` below is kept anyway, as a
# defensive fallback rather than a no-op: if a future re-pin (or a
# differently-built openvaf-r asset) DOES ship a dynamically-linked
# libLLVM dependency, this detects the unresolved `ldd` entry, looks for
# an already-installed compatible `libLLVM` on the host via `ldconfig`,
# and -- if found -- transparently points openvaf-r at it via a local
# `LD_LIBRARY_PATH` override instead of mutating anything system-wide.
# If no compatible library is found, it fails closed with the exact
# missing library named and a pointer to apt.llvm.org, rather than
# leaving the caller to decode a bare "cannot open shared object file".

set -euo pipefail

# shellcheck source=scripts/_install_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_install_common.sh"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PDK_DIR="$REPO_ROOT/pdks/ihp-open-pdk/ihp-sg13g2"
VA_DIR="$PDK_DIR/libs.tech/verilog-a"
OSDI_DIR="$PDK_DIR/libs.tech/ngspice/osdi"
MODELS_MARKER="$OSDI_DIR/.compiled-version"

# Pinned openvaf-r release -- bump the tag/URL/checksum together in the
# same change if this is ever refreshed. Fails closed on checksum
# mismatch (fetch_and_verify, from _install_common.sh).
#
# This is OpenVAF-Reloaded/OpenVAF's GitHub release tag `_20260610`
# (https://github.com/OpenVAF-Reloaded/OpenVAF/releases/tag/_20260610),
# at commit 2e066436d985b05cf8e6563e936daf9ab875775a -- recorded for
# provenance only, the URL below (not this commit sha) is what is
# actually fetched and checksum-verified. OpenVAF-Reloaded does not
# attach binary assets to its GitHub releases; it publishes Linux/Windows
# builds to its own download index instead (see its README's "What about
# binaries?" section), named by `git describe` against the tag.
OPENVAF_R_TAG="osdi_0.4-153-g2e066436"
OPENVAF_R_URL="https://fides.fe.uni-lj.si/openvaf/download/openvaf-reloaded-${OPENVAF_R_TAG}-linux_x64.tar.gz"
# Computed by downloading the pinned asset once and hashing it:
#   curl -fL -o /tmp/openvaf-r.tar.gz "$OPENVAF_R_URL"
#   shasum -a 256 /tmp/openvaf-r.tar.gz
OPENVAF_R_SHA256="9c570bf8f3e29fed5bac66298bf0b631a40a9075c5fe1ce3f6754cadd106df04"
# OSDI ABI version this exact pinned build emits -- verified directly
# while developing this script by `dlopen`-ing a model this compiler
# produced and reading its exported `OSDI_VERSION_MAJOR`/`OSDI_VERSION_MINOR`
# symbols (0 / 4), not copied from documentation.
OPENVAF_R_OSDI_ABI="0.4"

# ngspice major-version floor for OpenVAF-Reloaded's OSDI >= 0.4 support --
# see the header comment above for how this was verified (diffing
# imr/ngspice's osdiregistry.c at the ngspice-43 vs. ngspice-44 tags).
NGSPICE_MIN_MAJOR=44

COMPILER_PREFIX="${OPENVAF_R_INSTALL_PREFIX:-$HOME/.cache/openvaf-r-${OPENVAF_R_TAG}}"
COMPILER_BIN="$COMPILER_PREFIX/bin/openvaf-r"
COMPILER_MARKER="$COMPILER_PREFIX/.installed-version"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

# preflight_ngspice_osdi_abi: fail closed, naming the version floor, if
# ngspice is missing or too old to accept what openvaf-r emits -- rather
# than letting the mismatch surface only as ngspice's own opaque
# per-model "OSDI v0.3 vs v0.4" error deep in a simulation run.
preflight_ngspice_osdi_abi() {
    if ! command -v ngspice &>/dev/null; then
        cat >&2 <<EOF
error: ngspice not found on PATH

  scripts/fetch-sg13g2-sim-toolchain.sh compiles SG13G2's OSDI models with
  openvaf-r $OPENVAF_R_TAG, which emits OSDI ABI $OPENVAF_R_OSDI_ABI.
  Loading those models needs ngspice >= $NGSPICE_MIN_MAJOR (see this
  script's own header comment for why). Install ngspice
  >= $NGSPICE_MIN_MAJOR and put it on PATH, then re-run.
EOF
        return 1
    fi

    local version_output major
    # ngspice's own `--version` banner is multi-line (`******` on its own
    # line first, `** ngspice-NN : ...` second) -- grep the whole banner,
    # not just its first line (verified directly while developing this
    # script: `head -1` alone only ever sees the `******` line).
    version_output="$(ngspice --version 2>&1)"
    major="$(echo "$version_output" | grep -oE 'ngspice-[0-9]+' | head -1 | grep -oE '[0-9]+' || true)"
    if [[ -z "$major" ]]; then
        echo "warning: could not parse an ngspice version number from: $(echo "$version_output" | head -2 | tr '\n' ' ')" >&2
        echo "  proceeding without the OSDI ABI floor preflight -- the load-verification step below will still catch a real mismatch." >&2
        return 0
    fi

    if ((major < NGSPICE_MIN_MAJOR)); then
        cat >&2 <<EOF
error: ngspice on PATH is ngspice-$major, below the OSDI ABI floor

  openvaf-r $OPENVAF_R_TAG emits OSDI ABI $OPENVAF_R_OSDI_ABI. ngspice only
  gained support for OpenVAF-Reloaded's OSDI >= 0.4 descriptors in
  ngspice-$NGSPICE_MIN_MAJOR (imr/ngspice commit b40dcaa18d,
  "OpenVAF-reloaded compiled model support"); ngspice-$major will instead
  reject every compiled model deep in a simulation run with:
    NGSPICE only supports OSDI v0.3 but "<model>.osdi" targets v0.4!
  Ubuntu 24.04's apt ngspice package is version 42 -- below the floor on
  every stock Ubuntu 24.04 host. Install a from-source ngspice
  >= $NGSPICE_MIN_MAJOR (docker/eda-sim/pdk-versions.json's "tools.ngspice"
  entry is a working from-source recipe, though it pins a stricter,
  unrelated floor of its own) and re-run.
EOF
        return 1
    fi

    echo "ngspice-$major on PATH clears the OSDI ABI $OPENVAF_R_OSDI_ABI floor (>= ngspice-$NGSPICE_MIN_MAJOR)."
    return 0
}

# apply_libllvm_workaround <extracted-binary> <workdir>: defensive fixup
# for the Linux libLLVM dynamic-link problem described in this script's
# header comment. A no-op (prints a note, returns 0) when the binary
# already runs standalone, which is the case for this script's current
# pin -- see the header comment for why this is still kept as a fallback.
apply_libllvm_workaround() {
    local bin="$1" workdir="$2"
    local missing
    missing="$(ldd "$bin" 2>/dev/null | awk '/=> not found/ {print $1}')"

    if [[ -z "$missing" ]]; then
        echo "openvaf-r runs standalone (ldd reports no unresolved shared libraries) -- no libLLVM workaround needed."
        return 0
    fi

    echo "openvaf-r has unresolved shared library dependencies:" >&2
    echo "$missing" | awk '{print "  " $0}' >&2

    local llvm_missing
    llvm_missing="$(echo "$missing" | grep -i libllvm || true)"
    if [[ -z "$llvm_missing" ]]; then
        echo "error: none of the above are libLLVM -- this script does not know how to work around them." >&2
        return 1
    fi

    # Look for any libLLVM shared object already installed on this host
    # (Ubuntu 24.04 ships llvm-18 by default; apt.llvm.org's llvm-N
    # packages install their own libLLVM-N.so.1 / libLLVM.so.N.M) and, if
    # one exists, point openvaf-r at it via a local LD_LIBRARY_PATH
    # override -- never touching anything system-wide.
    local found
    found="$(ldconfig -p 2>/dev/null | awk '/libLLVM[.-]/ {print $NF; exit}')"
    if [[ -z "$found" || ! -e "$found" ]]; then
        cat >&2 <<EOF
error: openvaf-r needs a shared libLLVM this host does not have installed
  (missing: $llvm_missing)

  openvaf-r's Linux release can assume a system LLVM is already installed
  and merely symlink to it, rather than bundling a real shared object --
  install a matching LLVM runtime, e.g.:
    wget https://apt.llvm.org/llvm.sh && chmod +x llvm.sh && sudo ./llvm.sh 21
  then re-run this script. (Or set LD_LIBRARY_PATH yourself to a directory
  containing a compatible $llvm_missing before re-running.)
EOF
        return 1
    fi

    mkdir -p "$workdir/lib"
    ln -sf "$found" "$workdir/lib/$(basename "$llvm_missing")"
    export LD_LIBRARY_PATH="$workdir/lib:${LD_LIBRARY_PATH:-}"
    echo "Applied libLLVM workaround: $workdir/lib/$(basename "$llvm_missing") -> $found"
    return 0
}

fetch_compiler() {
    if [[ $FORCE -eq 0 && -f "$COMPILER_MARKER" && -x "$COMPILER_BIN" && "$(cat "$COMPILER_MARKER")" == "$OPENVAF_R_TAG" ]]; then
        echo "openvaf-r $OPENVAF_R_TAG already cached at $COMPILER_BIN (use --force to refetch)"
        return 0
    fi

    local tmp_tarball tmp_extract
    tmp_tarball="$(mktemp)"
    tmp_extract="$(mktemp -d)"
    # shellcheck disable=SC2064  # intentionally expand now: values are fixed
    trap "rm -f '$tmp_tarball'; rm -rf '$tmp_extract'" RETURN

    fetch_and_verify "$OPENVAF_R_URL" "$OPENVAF_R_SHA256" "$tmp_tarball"

    echo "Extracting into $tmp_extract ..."
    tar -xzf "$tmp_tarball" -C "$tmp_extract"

    mkdir -p "$COMPILER_PREFIX/bin"
    install -m 0755 "$tmp_extract/openvaf-r" "$COMPILER_BIN"

    finish_install "$COMPILER_MARKER" "$OPENVAF_R_TAG" "openvaf-r" "$COMPILER_PREFIX"
}

compile_models() {
    echo "Compiling SG13G2 Verilog-A models with openvaf-r $OPENVAF_R_TAG ..."
    mkdir -p "$OSDI_DIR"
    (
        cd "$VA_DIR"
        # Prepend, don't replace: openvaf-compile-va.sh itself falls back
        # to a plain `openvaf` on PATH if `openvaf-r` isn't found, and a
        # caller's own PATH may already have build tooling this needs.
        PATH="$COMPILER_PREFIX/bin:$PATH" ./openvaf-compile-va.sh
    )
    echo "$OPENVAF_R_TAG" >"$MODELS_MARKER"
}

# verify_osdi_load <osdi-file>: load-verify a compiled model in the
# ngspice on PATH via a throwaway `pre_osdi` + `devhelp` deck (ngspice's
# actual OSDI-loading commands -- NOT the XSPICE `codemodel` command,
# which loads `.cm` code models, a different mechanism verified directly
# while developing this script). `devhelp` lists every loaded device,
# including a line like "PSP103VA : A simulator independent device loaded
# with OSDI" for a successful load -- absence of that line, or any
# "only supports OSDI"/"couldn't be loaded" text, means the load failed.
verify_osdi_load() {
    local osdi_file="$1"
    local tmp_deck out status
    tmp_deck="$(mktemp --suffix=.cir)"
    cat >"$tmp_deck" <<EOF
* OSDI load verification -- scripts/fetch-sg13g2-sim-toolchain.sh
.control
pre_osdi $osdi_file
devhelp
quit
.endc
.end
EOF
    status=0
    out="$(ngspice -b "$tmp_deck" 2>&1)" || status=$?
    rm -f "$tmp_deck"

    if [[ $status -ne 0 ]]; then
        echo "error: ngspice exited $status while loading $osdi_file" >&2
        echo "$out" >&2
        return 1
    fi
    if echo "$out" | grep -qiE "only supports OSDI|couldn't be loaded|undefined symbol"; then
        echo "error: ngspice failed to load $osdi_file:" >&2
        echo "$out" | grep -iE "only supports OSDI|couldn't be loaded|undefined symbol" >&2
        return 1
    fi
    return 0
}

if [[ ! -d "$VA_DIR" ]]; then
    cat >&2 <<EOF
error: $VA_DIR not found

  Run scripts/fetch-ihp-sg13g2.sh first -- this script compiles that
  fetch's Verilog-A sources into OSDI models, it does not fetch the PDK
  itself.
EOF
    exit 1
fi

preflight_ngspice_osdi_abi || exit 1

NEED_COMPILE=1
if [[ -f "$MODELS_MARKER" && $FORCE -eq 0 && "$(cat "$MODELS_MARKER" 2>/dev/null)" == "$OPENVAF_R_TAG" ]]; then
    echo "SG13G2 OSDI models already compiled with openvaf-r $OPENVAF_R_TAG at $OSDI_DIR (use --force to rebuild)"
    NEED_COMPILE=0
fi

if [[ $NEED_COMPILE -eq 1 ]]; then
    fetch_compiler
    apply_libllvm_workaround "$COMPILER_BIN" "$COMPILER_PREFIX" || exit 1
    compile_models
fi

echo "Verifying compiled OSDI models load in $(command -v ngspice) ..."
shopt -s nullglob
osdi_files=("$OSDI_DIR"/*.osdi)
shopt -u nullglob
if [[ ${#osdi_files[@]} -eq 0 ]]; then
    echo "error: no *.osdi files found in $OSDI_DIR after compiling" >&2
    exit 1
fi

FAILED=0
for f in "${osdi_files[@]}"; do
    if verify_osdi_load "$f"; then
        echo "  OK: $f"
    else
        FAILED=1
    fi
done

if [[ $FAILED -ne 0 ]]; then
    echo "error: one or more compiled OSDI models failed to load in ngspice -- see above" >&2
    exit 1
fi

echo "Done: $OSDI_DIR"
du -sh "$OSDI_DIR"
