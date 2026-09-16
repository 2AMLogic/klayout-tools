#!/usr/bin/env bash
# Fetch a pinned commit of IHP-GmbH/ihp-sg13cmos5l (Apache-2.0) into pdks/
# (gitignored -- see pdks/README.md) so `klt pdk find`/`list`/`env` can
# resolve a real ihp-sg13cmos5l install (issue #1929), the same way
# `scripts/fetch-ihp-sg13g2.sh` does for ihp-sg13g2 (issue #522). `klt`
# already supports `ihp-sg13cmos5l` as a technology (#1398/#1399) and ships
# a curated starter deck targeting it (#1400/#1408); this script is the
# checksum-pinned, CI-consumable way to actually provision that PDK.
#
# `ihp-sg13cmos5l` is a **separate upstream repository** from
# `IHP-Open-PDK`/`ihp-sg13g2`
# (https://github.com/IHP-GmbH/ihp-sg13cmos5l.git), and as of this writing
# publishes no release tags -- so unlike `fetch-ihp-sg13g2.sh`'s
# `IHP_OPEN_PDK_VERSION` tag, this script pins an exact **commit SHA** plus
# the sha256 of the codeload tarball for that SHA. When bumping
# IHP_SG13CMOS5L_COMMIT, bump IHP_SG13CMOS5L_SHA256 in the same change using
# the same recipe (with the new SHA substituted) -- the script fails closed
# on mismatch, so a stale value here blocks every fetch:
#
#   curl -fL -o /tmp/ihp-sg13cmos5l-<sha>.tar.gz \
#     https://codeload.github.com/IHP-GmbH/ihp-sg13cmos5l/tar.gz/<sha>
#   shasum -a 256 /tmp/ihp-sg13cmos5l-<sha>.tar.gz
#
# CRITICAL layout constraint (issue #1406 dangling-symlink regression
# guard): ihp-sg13cmos5l's own device symbols and HV model cards
# (`libs.tech/xschem/sg13cmos5l_pr/*.sym`) are *relative* symlinks four
# levels up and back down into a **sibling** ihp-sg13g2 checkout, e.g.:
#
#   libs.tech/xschem/sg13cmos5l_pr/sg13_hv_nmos.sym
#     -> ../../../../ihp-sg13g2/libs.tech/xschem/sg13g2_pr/sg13_hv_nmos.sym
#
# (verified against the real pinned-commit tarball extracted at this
# script's IHP_SG13CMOS5L_COMMIT). That resolves only when ihp-sg13g2 lives
# immediately alongside ihp-sg13cmos5l under the same directory -- exactly
# the layout `scripts/fetch-ihp-sg13g2.sh` already produces
# (`pdks/ihp-open-pdk/ihp-sg13g2/`), so this script fetches
# ihp-sg13cmos5l as `pdks/ihp-open-pdk/ihp-sg13cmos5l/`, a sibling of that
# existing ihp-sg13g2 checkout, and refuses to run (loudly, not silently)
# if that sibling isn't already present -- run
# `scripts/fetch-ihp-sg13g2.sh` first. IHP's own upstream README documents
# this exact "clone ihp-sg13cmos5l inside your IHP-Open-PDK checkout"
# layout.
#
# Layout produced (verified against the real pinned-commit tarball):
#
#   pdks/ihp-open-pdk/ihp-sg13g2/libs.tech/...      # fetched by fetch-ihp-sg13g2.sh
#   pdks/ihp-open-pdk/ihp-sg13g2/libs.ref/...        # (must already exist)
#   pdks/ihp-open-pdk/ihp-sg13cmos5l/libs.tech/...   # fetched by this script
#   pdks/ihp-open-pdk/ihp-sg13cmos5l/libs.ref/...
#
# -- both are variants of the same open_pdks-shaped root, already
# resolvable by `klt pdk find` with no new resolver code (see
# src/klayout_tools/pdk.py's module docstring: "a root may hold more than
# one" variant):
#
#   PDK_ROOT=pdks/ihp-open-pdk PDK=ihp-sg13cmos5l klt pdk find
#
# GitHub's codeload tarball for a commit does not include any git
# submodules; verified against the real pinned commit that ihp-sg13cmos5l
# has none (no `.gitmodules`), so this is a non-issue here (unlike
# `fetch-ihp-sg13g2.sh`'s note about IHP-Open-PDK's submodules).
#
# This fetch alone does not make the PDK simulatable any more than
# `fetch-ihp-sg13g2.sh`'s does -- see that script's header and
# `scripts/fetch-sg13g2-sim-toolchain.sh` (issue #1628) for the OSDI
# compile step, which the ihp-sg13g2 sibling this script requires must
# already have gone through if simulation (not just layout/DRC/LVS) is the
# goal.
#
# Usage: scripts/fetch-ihp-sg13cmos5l.sh [--force]

set -euo pipefail

IHP_SG13CMOS5L_COMMIT="607e18d4bd9214a52575c194b4181ef449f9252f"
IHP_SG13CMOS5L_URL="https://codeload.github.com/IHP-GmbH/ihp-sg13cmos5l/tar.gz/${IHP_SG13CMOS5L_COMMIT}"
# Computed by downloading the pinned commit's codeload tarball once and
# hashing it (recipe above, with IHP_SG13CMOS5L_COMMIT substituted):
IHP_SG13CMOS5L_SHA256="a9f30df3b7c9e6eb12bd2fa5213651fa299841e9a39ed0c8f797ef99e0e8658e"

# Detect a portable sha256 command (macOS ships shasum, Linux sha256sum).
# Mirrors scripts/fetch-ihp-sg13g2.sh's / scripts/fetch-pdks.sh's /
# .loom/scripts/verify-install.sh's compute_sha256 helper.
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
SIBLING_ROOT="$PDKS_DIR/ihp-open-pdk"
SG13G2_DIR="$SIBLING_ROOT/ihp-sg13g2"
DEST="$SIBLING_ROOT/ihp-sg13cmos5l"
MARKER="$DEST/.fetched-version"

# Fail loudly (not silently) if the sibling ihp-sg13g2 checkout the
# symlinks above depend on isn't already present -- the #1406 regression
# this script exists to prevent. Checked on every run (cheap, local-only),
# not just the first fetch, since the sibling could be removed later.
if [[ ! -d "$SG13G2_DIR/libs.tech" || ! -d "$SG13G2_DIR/libs.ref" ]]; then
    echo "error: no ihp-sg13g2 checkout found at $SG13G2_DIR" >&2
    echo "  ihp-sg13cmos5l's device symbols and HV model cards" >&2
    echo "  (libs.tech/xschem/sg13cmos5l_pr/*.sym) are relative symlinks into" >&2
    echo "  a sibling ihp-sg13g2 checkout (../../../../ihp-sg13g2/libs.tech/...)." >&2
    echo "  Fetching ihp-sg13cmos5l without it produces an install whose" >&2
    echo "  directory listing looks complete but whose device library cannot" >&2
    echo "  be opened -- the dangling-symlink failure mode from issue #1406." >&2
    echo "  Run scripts/fetch-ihp-sg13g2.sh first, then re-run this script." >&2
    exit 1
fi

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

if [[ -f "$MARKER" && $FORCE -eq 0 ]]; then
    have="$(cat "$MARKER")"
    if [[ "$have" == "$IHP_SG13CMOS5L_COMMIT" ]]; then
        echo "ihp-sg13cmos5l @ $IHP_SG13CMOS5L_COMMIT already present at $DEST (use --force to refetch)"
        exit 0
    fi
    echo "ihp-sg13cmos5l @ $have present; updating to $IHP_SG13CMOS5L_COMMIT"
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "Downloading ihp-sg13cmos5l @ $IHP_SG13CMOS5L_COMMIT (~25 MB) ..."
curl -fL --progress-bar -o "$tmp/ihp-sg13cmos5l.tar.gz" "$IHP_SG13CMOS5L_URL"

echo "Verifying checksum ..."
actual_sha256="$(sha256_of "$tmp/ihp-sg13cmos5l.tar.gz")"
if [[ "$actual_sha256" != "$IHP_SG13CMOS5L_SHA256" ]]; then
    echo "error: checksum mismatch for ihp-sg13cmos5l @ $IHP_SG13CMOS5L_COMMIT" >&2
    echo "  expected: $IHP_SG13CMOS5L_SHA256" >&2
    echo "  actual:   $actual_sha256" >&2
    exit 1
fi

echo "Extracting ..."
mkdir -p "$tmp/extract"
tar -xzf "$tmp/ihp-sg13cmos5l.tar.gz" -C "$tmp/extract" --strip-components=1

rm -rf "$DEST"
mkdir -p "$SIBLING_ROOT"
mv "$tmp/extract" "$DEST"
echo "$IHP_SG13CMOS5L_COMMIT" >"$MARKER"

echo "Done: $DEST"
du -sh "$DEST"
echo "  PDK_ROOT=$SIBLING_ROOT PDK=ihp-sg13cmos5l"
