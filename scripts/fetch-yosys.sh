#!/usr/bin/env bash
# Install a pinned, checksum-verified Yosys build (issue #417).
#
# Not `apt-get install yosys`: docs/design/yosys-synthesis-spike.md §3.4
# found Ubuntu 24.04's apt package (0.33-5build2) is ~30 releases behind the
# 0.67 build the spike's worked example used -- old enough to be missing
# passes/flags a real synthesis run needs (`stat -json`'s field set and
# `dfflibmap`'s legalization behavior have both changed across releases).
#
# Installs yowasp-yosys (https://github.com/YoWASP/yosys, ISC -- same
# license as upstream Yosys itself, see the spike's §3.1), YoWASP's
# WASM-compiled redistribution of an official, tagged Yosys release, as a
# prebuilt, platform-independent PyPI wheel. This is the "prebuilt release
# binary" path the spike's §3.4/§5 recommended over a from-source CI build:
# no build toolchain (bison/flex/tcl/libreadline/...) needed, and PyPI
# releases are immutable, so an exact version pin is already reproducible.
# The sha256 check below is defense-in-depth against a compromised/mirrored
# index -- the same "pin + verify, fail closed" discipline as this repo's
# other pinned fetches (scripts/fetch-pdks.sh, scripts/fetch-cell-netlists.sh).
#
# Caveat for a future re-pin: the YoWASP/yosys GitHub repo is archived as of
# this pin (still actively publishing PyPI releases as of this writing, but
# no longer accepting GitHub-side changes) -- if it stops publishing new
# Yosys versions entirely, Phase 2's next version bump should re-evaluate
# against a from-source build instead (the spike's §3.4 alternative (b)).
#
# After install, `yowasp-yosys` (and `yowasp-yosys-smtbmc`/`-witness`) are on
# PATH via pip's console-script entry points -- this script does not alias
# it to a plain `yosys` name; callers that need that (this repo's CI, or a
# future `klt synthesize`, issue #416) create that symlink themselves.
#
# Usage: scripts/fetch-yosys.sh

set -euo pipefail

# Pinned to the Yosys 0.67 release (upstream git tag v0.67) -- the same
# Yosys version docs/design/yosys-synthesis-spike.md §3.3/§4's worked
# example used. Bump alongside the sha256 below in the same change:
#   pip download --no-deps -d /tmp yowasp-yosys==<new-version>
#   shasum -a 256 /tmp/yowasp_yosys-<new-version>-py3-none-any.whl
YOWASP_YOSYS_VERSION="0.67.0.0.post1190"
YOWASP_YOSYS_SHA256="b3f48c4d24d42daf5146c9932356df223485086868be8171855f79721d56464a"

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

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "Downloading yowasp-yosys==$YOWASP_YOSYS_VERSION ..."
pip download --no-deps -d "$tmp" "yowasp-yosys==$YOWASP_YOSYS_VERSION"

wheel="$(find "$tmp" -name '*.whl' -print -quit)"
if [[ -z "$wheel" ]]; then
    echo "error: no wheel downloaded for yowasp-yosys==$YOWASP_YOSYS_VERSION" >&2
    exit 1
fi

echo "Verifying checksum ..."
actual_sha256="$(sha256_of "$wheel")"
if [[ "$actual_sha256" != "$YOWASP_YOSYS_SHA256" ]]; then
    echo "error: checksum mismatch for $(basename "$wheel")" >&2
    echo "  expected: $YOWASP_YOSYS_SHA256" >&2
    echo "  actual:   $actual_sha256" >&2
    exit 1
fi

echo "Installing $(basename "$wheel") ..."
pip install "$wheel"

echo "Done: $(command -v yowasp-yosys)"
yowasp-yosys --version
