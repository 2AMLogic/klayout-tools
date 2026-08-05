#!/usr/bin/env bash
# Fetch a pinned release of IHP-Open-PDK (Apache-2.0) into pdks/ (gitignored
# -- see pdks/README.md) so `klt pdk find`/`list`/`env` can resolve a real
# SG13G2 install (issue #522).
#
# This is a **different upstream project** from `scripts/fetch-pdks.sh`'s
# lambdapdk bundle. lambdapdk ships its own `ihp130` process tree in a
# wholly different layout and with different (not SG13G2) data -- it is
# never a substitute for this fetch. See pdks/README.md for the explicit
# ihp130-vs-SG13G2 distinction.
#
# Layout produced (verified against the real v0.3.0 release tarball):
#
#   pdks/ihp-open-pdk/ihp-sg13g2/libs.tech/...
#   pdks/ihp-open-pdk/ihp-sg13g2/libs.ref/...
#
# -- an ordinary open_pdks-shaped install (root=pdks/ihp-open-pdk,
# variant=ihp-sg13g2), already resolvable by `klt pdk find` with no new
# resolver code (see src/klayout_tools/pdk.py's module docstring for the
# *other*, flat-layout case this issue's resolver change actually adds:
# `$PDK_ROOT` pointed directly at an `ihp-sg13g2/`-shaped directory, which is
# how some IHP-ecosystem tooling documents `$PDK_ROOT` too):
#
#   PDK_ROOT=pdks/ihp-open-pdk PDK=ihp-sg13g2 klt pdk find
#
# GitHub's tag/release tarball does **not** include IHP-Open-PDK's git
# submodules (`.gitmodules`: digital-cell, KLayout PyCell, OpenEMS, and
# Palace helper repos) -- none of those are needed to resolve the PDK or run
# its shipped KLayout/ngspice/netgen views, only optional adjunct tooling.
#
# Usage: scripts/fetch-ihp-sg13g2.sh [--force]

set -euo pipefail

IHP_OPEN_PDK_VERSION="0.3.0"
IHP_OPEN_PDK_URL="https://github.com/IHP-GmbH/IHP-Open-PDK/archive/refs/tags/v${IHP_OPEN_PDK_VERSION}.tar.gz"
# Computed by downloading the pinned tag archive once and hashing it:
#   curl -fL -o /tmp/ihp-open-pdk-v0.3.0.tar.gz \
#     https://github.com/IHP-GmbH/IHP-Open-PDK/archive/refs/tags/v0.3.0.tar.gz
#   shasum -a 256 /tmp/ihp-open-pdk-v0.3.0.tar.gz
# When bumping IHP_OPEN_PDK_VERSION, bump this checksum in the same change
# using the same recipe (with the new version substituted) -- the script
# fails closed on mismatch, so a stale value here blocks every fetch.
IHP_OPEN_PDK_SHA256="f6afb7a8d2adf3e40acd3fd2c7892fc93510e6f760565409a8df2b5b242254fc"

# Detect a portable sha256 command (macOS ships shasum, Linux sha256sum).
# Mirrors scripts/fetch-pdks.sh's / .loom/scripts/verify-install.sh's
# compute_sha256 helper.
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

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PDKS_DIR="$REPO_ROOT/pdks"
DEST="$PDKS_DIR/ihp-open-pdk"
MARKER="$DEST/.fetched-version"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

if [[ -f "$MARKER" && $FORCE -eq 0 ]]; then
    have="$(cat "$MARKER")"
    if [[ "$have" == "$IHP_OPEN_PDK_VERSION" ]]; then
        echo "IHP-Open-PDK v$IHP_OPEN_PDK_VERSION already present at $DEST (use --force to refetch)"
        exit 0
    fi
    echo "IHP-Open-PDK v$have present; updating to v$IHP_OPEN_PDK_VERSION"
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "Downloading IHP-Open-PDK v$IHP_OPEN_PDK_VERSION (~350 MB) ..."
curl -fL --progress-bar -o "$tmp/ihp-open-pdk.tar.gz" "$IHP_OPEN_PDK_URL"

echo "Verifying checksum ..."
actual_sha256="$(sha256_of "$tmp/ihp-open-pdk.tar.gz")"
if [[ "$actual_sha256" != "$IHP_OPEN_PDK_SHA256" ]]; then
    echo "error: checksum mismatch for IHP-Open-PDK v$IHP_OPEN_PDK_VERSION" >&2
    echo "  expected: $IHP_OPEN_PDK_SHA256" >&2
    echo "  actual:   $actual_sha256" >&2
    exit 1
fi

echo "Extracting ..."
mkdir -p "$tmp/extract"
tar -xzf "$tmp/ihp-open-pdk.tar.gz" -C "$tmp/extract" --strip-components=1

rm -rf "$DEST"
mkdir -p "$PDKS_DIR"
mv "$tmp/extract" "$DEST"
echo "$IHP_OPEN_PDK_VERSION" >"$MARKER"

echo "Done: $DEST"
du -sh "$DEST"
echo "  PDK_ROOT=$DEST PDK=ihp-sg13g2"
