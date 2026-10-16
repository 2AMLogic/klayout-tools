#!/usr/bin/env bash
# Fetch the ihp-sg13cmos5l/ subtree of a pinned commit of
# IHP-GmbH/IHP-Open-PDK (Apache-2.0, `dev` branch) into pdks/
# (gitignored -- see pdks/README.md) so `klt pdk find`/`list`/`env` can
# resolve a real ihp-sg13cmos5l install (issue #1929), the same way
# `scripts/fetch-ihp-sg13g2.sh` does for ihp-sg13g2 (issue #522). `klt`
# already supports `ihp-sg13cmos5l` as a technology (#1398/#1399) and ships
# a curated starter deck targeting it (#1400/#1408); this script is the
# checksum-pinned, CI-consumable way to actually provision that PDK.
#
# SG13CMOS5L now lives inside IHP-Open-PDK (tree `ihp-sg13cmos5l/`, next to
# `ihp-sg13g2/` and `ihp-common/`). The standalone repo
# IHP-GmbH/ihp-sg13cmos5l was archived on 2026-09-23 (issue #2720) and
# will receive no further fixes. No IHP-Open-PDK release includes
# SG13CMOS5L yet (latest, v0.3.0, predates the merge), so the pin is a
# `dev` branch commit SHA -- revisit when a release carries it. Unlike `fetch-ihp-sg13g2.sh`'s
# `IHP_OPEN_PDK_VERSION` tag, this script pins an exact **commit SHA** plus
# the sha256 of the codeload tarball for that SHA. When bumping
# IHP_SG13CMOS5L_COMMIT (any dev commit at/after e8fba183, which removed the
# prebuilt x86-64 cap_cmom{i,f}.osdi -- IHP-Open-PDK#1239/#1260), bump IHP_SG13CMOS5L_SHA256 in the same change using
# the same recipe (with the new SHA substituted) -- the script fails closed
# on mismatch, so a stale value here blocks every fetch:
#
#   curl -fL -o /tmp/ihp-open-pdk-<sha>.tar.gz \
#     https://codeload.github.com/IHP-GmbH/IHP-Open-PDK/tar.gz/<sha>
#   shasum -a 256 /tmp/ihp-open-pdk-<sha>.tar.gz
#
# NOTE: the whole-repo tarball is ~400 MB (all of ihp-sg13g2, ihp-common and
# ihp-sg13cmos5l); only the ihp-sg13cmos5l/ subtree (~96 MB) is extracted.
#
# CRITICAL layout constraint (issue #1406 dangling-symlink regression
# guard): the ihp-sg13cmos5l/ tree still holds *relative* symlinks up and
# back down into the **sibling** ihp-sg13g2/ directory of the same
# IHP-Open-PDK tree, e.g. (verified against the real pinned-commit tarball):
#
#   libs.tech/verilog-a/psp103
#     -> ../../../ihp-sg13g2/libs.tech/verilog-a/psp103
#   libs.tech/verilog-a/{r3_cmc,mosvar}, libs.tech/gnucap/gnucap.mk,
#   libs.ref/sg13cmos5l_sram -> ../../ihp-sg13g2/libs.ref/sg13g2_sram, ...
#
# Because only the ihp-sg13cmos5l/ subtree is extracted, these resolve only
# when ihp-sg13g2 lives
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
# KNOWN-INCOMPLETE SIBLING (issue #3133): the pinned `dev` subtree expects an
# ihp-sg13g2 sibling at (or near) the same `dev` commit, but
# `fetch-ihp-sg13g2.sh` installs release v0.3.0, which predates the merge of
# SG13CMOS5L into `dev`. Some symlink targets (e.g. moscap models, pycell
# helpers, LVS helpers) may therefore be absent and the links dangle -- 262 of
# 530 as of commit 159527a5, 2026-10 (re-measure with this script's summary).
# After extraction, and on the cached re-run path, this script counts dangling
# symlinks under the install, prints a sample and the sibling's version marker,
# and warns loudly. It still exits 0 (the models-only use case works without
# the missing targets) unless `--strict` is given, which exits 3 when any
# link dangles. Repinning the sibling is a separate, human decision: it
# changes a checksummed pin used by CI. `klt pdk check` also reports these.
#
# Usage: scripts/fetch-ihp-sg13cmos5l.sh [--force] [--strict]

set -euo pipefail

# IHP-Open-PDK `dev` branch commit (a branch SHA, not a release tag).
IHP_SG13CMOS5L_COMMIT="f7bca32694fe3d58be0cc7d9df4b9f5db7161a14"
IHP_SG13CMOS5L_URL="https://codeload.github.com/IHP-GmbH/IHP-Open-PDK/tar.gz/${IHP_SG13CMOS5L_COMMIT}"
# Computed by downloading the pinned commit's codeload tarball once and
# hashing it (recipe above, with IHP_SG13CMOS5L_COMMIT substituted):
IHP_SG13CMOS5L_SHA256="5eb6f8a9b0b2ecc4fa08cc30627668406f85b1a97834f278036c43f0c0dbc70b"

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
PDKS_DIR="${IHP_PDKS_DIR:-$REPO_ROOT/pdks}"
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
    echo "  ihp-sg13cmos5l's shared Verilog-A models, SRAM, gnucap and xschem" >&2
    echo "  test files are relative symlinks into the sibling ihp-sg13g2" >&2
    echo "  directory (../../../ihp-sg13g2/libs.tech/...)." >&2
    echo "  Fetching ihp-sg13cmos5l without it produces an install whose" >&2
    echo "  directory listing looks complete but whose device library cannot" >&2
    echo "  be opened -- the dangling-symlink failure mode from issue #1406." >&2
    echo "  Run scripts/fetch-ihp-sg13g2.sh first, then re-run this script." >&2
    exit 1
fi

FORCE=0
STRICT=0
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        --strict) STRICT=1 ;;
        *) echo "error: unknown argument: $arg" >&2
           echo "usage: scripts/fetch-ihp-sg13cmos5l.sh [--force] [--strict]" >&2
           exit 2 ;;
    esac
done

# Count dangling symlinks under $DEST (`-L` and not `-e`, the same definition
# as pdk.py's _find_broken_symlinks), print a sample and the sibling's version
# marker. Sets DANGLING_COUNT; returns 0 if clean, 1 if any dangle.
report_dangling() {
    local list sibling_ver
    list="$(find "$DEST" -type l ! -exec test -e {} \; -print 2>/dev/null | sort)"
    DANGLING_COUNT=0
    [[ -n "$list" ]] && DANGLING_COUNT="$(printf '%s\n' "$list" | wc -l | tr -d ' ')"
    sibling_ver="$(cat "$SG13G2_DIR/.fetched-version" 2>/dev/null || echo unknown)"
    if [[ "$DANGLING_COUNT" -eq 0 ]]; then
        echo "Symlink check: no dangling symlinks under $DEST (ihp-sg13g2 sibling @ $sibling_ver)"
        return 0
    fi
    echo "WARNING: $DANGLING_COUNT dangling symlink(s) under $DEST" >&2
    echo "  ihp-sg13g2 sibling version: $sibling_ver (pinned subtree expects a matching dev commit;" >&2
    echo "  release v0.3.0 is known-incomplete -- see this script's header and pdks/README.md)" >&2
    echo "  first 10 (run 'klt pdk check' for the full report):" >&2
    printf '%s\n' "$list" | head -n 10 | sed "s|^$DEST/|    |" >&2
    return 1
}

finish_dangling() {
    if ! report_dangling && [[ $STRICT -eq 1 ]]; then
        echo "error: --strict: install is incomplete ($DANGLING_COUNT dangling symlinks)" >&2
        exit 3
    fi
    return 0
}

if [[ -f "$MARKER" && $FORCE -eq 0 ]]; then
    have="$(cat "$MARKER")"
    if [[ "$have" == "$IHP_SG13CMOS5L_COMMIT" ]]; then
        echo "ihp-sg13cmos5l @ $IHP_SG13CMOS5L_COMMIT already present at $DEST (use --force to refetch)"
        finish_dangling
        exit 0
    fi
    echo "ihp-sg13cmos5l @ $have present; updating to $IHP_SG13CMOS5L_COMMIT"
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "Downloading ihp-sg13cmos5l @ $IHP_SG13CMOS5L_COMMIT (~400 MB whole-repo tarball) ..."
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
# Extract only the ihp-sg13cmos5l/ subtree (codeload names the top-level
# directory IHP-Open-PDK-<sha>); strip that and the subtree dir itself.
tar -xzf "$tmp/ihp-sg13cmos5l.tar.gz" -C "$tmp/extract" --strip-components=2 \
    "IHP-Open-PDK-${IHP_SG13CMOS5L_COMMIT}/ihp-sg13cmos5l"
if [[ ! -d "$tmp/extract/libs.tech" || ! -d "$tmp/extract/libs.ref" ]]; then
    echo "error: ihp-sg13cmos5l/ subtree missing libs.tech/libs.ref after extraction" >&2
    exit 1
fi

rm -rf "$DEST"
mkdir -p "$SIBLING_ROOT"
mv "$tmp/extract" "$DEST"
echo "$IHP_SG13CMOS5L_COMMIT" >"$MARKER"

if report_dangling; then
    echo "Done: $DEST"
else
    echo "Extracted (INCOMPLETE -- $DANGLING_COUNT dangling symlinks): $DEST"
fi
du -sh "$DEST"
echo "  PDK_ROOT=$SIBLING_ROOT PDK=ihp-sg13cmos5l"
if [[ $DANGLING_COUNT -gt 0 && $STRICT -eq 1 ]]; then
    echo "error: --strict: install is incomplete ($DANGLING_COUNT dangling symlinks)" >&2
    exit 3
fi
