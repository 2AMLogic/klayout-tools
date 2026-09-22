#!/usr/bin/env bash
# Build and install a pinned, reproducible FastCap 2.0 for the capacitance
# cross-validation oracle -- issue #2015 (pairing #4 of #2007's oracle
# tracking issue).
#
# FastCap (Nabors & White, "FastCap: A Multipole Accelerated 3-D Capacitance
# Extraction Program", IEEE TCAD 1991) is the *oracle* half of
# `tests/test_mom_capacitance_oracle.py`: an independently implemented
# constant-panel Method-of-Moments capacitance solver used to cross-check
# `klt mom`'s Maxwell capacitance matrix. It is never a runtime dependency of
# `klt` itself -- nothing in `src/klayout_tools/` invokes it; see
# `docs/design/fastcap-oracle.md`.
#
# Which source, and why this one:
#
#   ediloren/FastCap2 `master` is the original M.I.T. FastCap 2.0
#   distribution, carrying M.I.T.'s 2003 relicensing ("License to use, copy,
#   modify, sell and/or distribute this software ... for any purpose is
#   hereby granted without royalty"). The repository's `WRCad` branch has
#   equivalent modern-toolchain fixes already applied, but is distributed
#   under the original 1990 "internal, noncommercial purposes" license --
#   the wrong license to point a public MIT repo's contributors at. So this
#   pins `master` and applies scripts/patches/fastcap-2.0-modern-toolchain.patch,
#   two build-only hunks (see that file's header). Nothing is vendored: the
#   source is fetched at install time, checksum-verified, and built.
#
# Why not `apt-get install fastcap`: there is no such package on any distro
# this repo targets. FastCap has had no upstream release since 1992.
#
# Compiler flags, and why they are pinned here rather than left to the
# upstream Makefile:
#   -std=gnu89      FastCap is K&R C (old-style definitions, implicit int).
#                   C99 onward rejects implicit int, and C23 (GCC 15's
#                   default) removes K&R definitions outright.
#   -fcommon        Its globals are tentative definitions declared in
#                   headers; GCC 10 flipped the default to -fno-common.
#   -fno-strict-aliasing  1992 C, written long before the aliasing rules
#                   were something optimisers exploited.
#   -w              Suppresses the (very large) legacy warning stream; this
#                   is a pinned third-party build, not code this repo edits.
# On macOS only, two further build-time aids are added (issue #2306): an
# -I pointing at a generated <malloc.h> shim, and
# -Wno-error=return-mismatch. See the `uname -s` branch before the build
# step for why each is needed and why it is Darwin-only.
#
# Usage: scripts/install-fastcap.sh [--force]
#   Installs into $FASTCAP_INSTALL_PREFIX (default: ~/.cache/fastcap-<version>).
#   Add "$FASTCAP_INSTALL_PREFIX/bin" to $PATH after running. Idempotent: a
#   prior successful install for the same pinned version is left in place
#   unless --force is given (matches install-magic.sh's own convention).

set -euo pipefail

# shellcheck source=scripts/_install_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_install_common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# FastCap's own version string (what the binary prints: "Running fastcap 2.0
# (18Sep92)"), suffixed with the short pinned commit so the install prefix
# and the marker change if this pin is ever moved. Bump the version, the
# commit, the checksum, and CHANGELOG.md together in the same change.
FASTCAP_VERSION="2.0-ec3479e"

# ediloren/FastCap2 `master` has exactly one commit -- it is a 2015 snapshot
# of the M.I.T. distribution, not an active fork -- so this pins that commit's
# source archive by URL + sha256, the way scripts/fetch-pdks.sh pins
# lambdapdk's. Verified stable across repeated fetches while writing this.
FASTCAP_COMMIT="ec3479ebdbfe0e6dbc6eda33ca170d525a7423ee"
FASTCAP_ASSET_URL="https://github.com/ediloren/FastCap2/archive/${FASTCAP_COMMIT}.tar.gz"
# Computed by downloading the pinned archive once and hashing it:
#   curl -fL -o /tmp/fastcap.tar.gz "$FASTCAP_ASSET_URL"
#   shasum -a 256 /tmp/fastcap.tar.gz
FASTCAP_ASSET_SHA256="6d4433558b22496102ab60d431fbb3ba2e8d9eb2ebd676b8b66ccdd457eddc8c"

PATCH_FILE="$SCRIPT_DIR/patches/fastcap-2.0-modern-toolchain.patch"

PREFIX="${FASTCAP_INSTALL_PREFIX:-$HOME/.cache/fastcap-${FASTCAP_VERSION}}"
MARKER="$PREFIX/.installed-version"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

check_marker "$MARKER" "$FASTCAP_VERSION" "$FORCE" "fastcap"

if [[ ! -f "$PATCH_FILE" ]]; then
    echo "error: missing $PATCH_FILE" >&2
    exit 1
fi

# Fail with a name, not a mid-build error. `patch` in particular is easy to
# be missing on a slim container image, and its absence would otherwise
# surface as a compile error in unpatched 1992 C.
for required in curl tar patch make cc install; do
    if ! command -v "$required" &>/dev/null; then
        echo "error: '$required' is required to build fastcap but is not on \$PATH" >&2
        exit 1
    fi
done

tmp_tarball="$(mktemp)"
src_dir="$(mktemp -d)"
trap 'rm -f "$tmp_tarball"; rm -rf "$src_dir"' EXIT

fetch_and_verify "$FASTCAP_ASSET_URL" "$FASTCAP_ASSET_SHA256" "$tmp_tarball"

echo "Extracting into $src_dir ..."
tar -xzf "$tmp_tarball" -C "$src_dir" --strip-components=1

echo "Applying $(basename "$PATCH_FILE") ..."
# No --forward/--fuzz: this must apply exactly against the checksum-verified
# archive above, and fail loudly if it ever does not.
patch -p1 --directory="$src_dir" --input="$PATCH_FILE"

# macOS-only build aids -- issue #2306.
#
# These are deliberately NOT folded into $PATCH_FILE: `patch` hunks apply
# unconditionally, and both fixes are inherently platform-conditional. They
# stay in shell so `extra_cflags` is the empty string everywhere except
# Darwin, leaving the Linux/CI `make` invocation byte-identical to what it
# was before this block existed. Both are build-only, like the patch
# itself: no discretisation, kernel, or solve source is touched, and the
# checksum-gated fetch above is untouched.
extra_cflags=""
if [[ "$(uname -s)" == "Darwin" ]]; then
    # 1. <malloc.h> shim. src/mulGlobal.h:46 includes <malloc.h>, which is
    #    a glibc-only header; BSD/macOS declare malloc()/calloc() in
    #    <stdlib.h> and ship no <malloc.h> at all, so every translation
    #    unit fails with "fatal error: 'malloc.h' file not found". The
    #    shim is written into the scratch source tree (never a tracked
    #    path) and reached only via the -I below, so it cannot leak into
    #    any other build.
    shim_include_dir="$src_dir/klt-macos-shim-include"
    mkdir -p "$shim_include_dir"
    cat >"$shim_include_dir/malloc.h" <<'MALLOC_SHIM'
#pragma once
#include <stdlib.h>
MALLOC_SHIM

    # 2. -Wno-error=return-mismatch. With the shim in place the next
    #    failure is src/mulSetup.c:744, `if(depth == 0) return;` inside
    #    the int-returning, unprototyped K&R `getnbrs` -- the same class of
    #    1992-C problem $PATCH_FILE already fixes for `static` linkage.
    #    Apple Clang promotes -Wreturn-mismatch to an error by default and
    #    the -w above does not demote it; -Wno-error=return-mismatch does,
    #    leaving it a warning. Not added on Linux, where the CI toolchain
    #    does not need it and adding it would silently widen what is
    #    normally a hard error on the path CI actually gates.
    extra_cflags=" -I$shim_include_dir -Wno-error=return-mismatch"
fi

echo "Building (parallel=$nproc_val) ..."
# `bin/` is where the upstream Makefile links the executable, and the
# archive does not ship it (git does not track empty directories), so the
# link step would fail with "cannot open output file".
mkdir -p "$src_dir/bin"
make -C "$src_dir/src" -j"$nproc_val" fastcap \
    CFLAGS="-O2 -DOTHER -std=gnu89 -fcommon -fno-strict-aliasing -w${extra_cflags}"

echo "Installing into $PREFIX ..."
rm -rf "$PREFIX"
mkdir -p "$PREFIX/bin"
install -m 0755 "$src_dir/bin/fastcap" "$PREFIX/bin/fastcap"

# Smoke test before declaring success: FastCap exits 0 on several of its own
# failure paths (see tests/helpers/fastcap_oracle.py), so "the binary ran" is
# not evidence -- solve a two-panel problem and require a capacitance matrix
# in the output. A build that segfaults (which is exactly what an unpatched
# build does, on its first allocation) is caught here rather than surfacing
# later as a skipped test.
echo "Smoke-testing the built binary ..."
smoke_qui="$src_dir/klt-smoke.qui"
cat >"$smoke_qui" <<'QUI'
0 klt fastcap install smoke test: two 1um square plates, 1um apart
Q 1 0 0 0 1e-6 0 0 1e-6 1e-6 0 0 1e-6 0
Q 2 0 0 1e-6 1e-6 0 1e-6 1e-6 1e-6 1e-6 0 1e-6 1e-6
N 1 top
N 2 bottom
QUI
if ! "$PREFIX/bin/fastcap" "$smoke_qui" | grep -q "CAPACITANCE MATRIX"; then
    echo "error: the built fastcap did not produce a capacitance matrix" >&2
    exit 1
fi

finish_install "$MARKER" "$FASTCAP_VERSION" "fastcap" "$PREFIX"
