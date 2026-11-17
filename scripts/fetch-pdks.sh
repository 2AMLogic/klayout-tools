#!/usr/bin/env bash
# Fetch open PDK data into pdks/ (gitignored — see pdks/README.md).
#
# Downloads a pinned release of lambdapdk (Apache-2.0), the
# siliconcompiler project's library of open PDK packages: sky130,
# gf180, asap7, freepdk45, ihp130, gt2n, interposer.
#
# Usage: scripts/fetch-pdks.sh [--force]

set -euo pipefail

# shellcheck source=scripts/_install_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_install_common.sh"

LAMBDAPDK_VERSION="0.2.17"
LAMBDAPDK_URL="https://github.com/siliconcompiler/lambdapdk/archive/refs/tags/v${LAMBDAPDK_VERSION}.tar.gz"
# Computed by downloading the pinned tag archive once and hashing it:
#   curl -fL -o /tmp/lambdapdk-v0.2.17.tar.gz \
#     https://github.com/siliconcompiler/lambdapdk/archive/refs/tags/v0.2.17.tar.gz
#   shasum -a 256 /tmp/lambdapdk-v0.2.17.tar.gz
# When bumping LAMBDAPDK_VERSION, bump this checksum in the same change
# using the same recipe (with the new version substituted) — the script
# fails closed on mismatch, so a stale value here blocks every fetch.
LAMBDAPDK_SHA256="ac7e950c4d0394a829658b6e219fea4a4b0c61a998183d5520f8b9e38c6efff2"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PDKS_DIR="$REPO_ROOT/pdks"
DEST="$PDKS_DIR/lambdapdk"
MARKER="$DEST/.fetched-version"
COMMIT_MARKER="$DEST/.fetched-commit"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

if [[ -f "$MARKER" && $FORCE -eq 0 ]]; then
    have="$(cat "$MARKER")"
    if [[ "$have" == "$LAMBDAPDK_VERSION" ]]; then
        echo "lambdapdk v$LAMBDAPDK_VERSION already present at $DEST (use --force to refetch)"
        exit 0
    fi
    echo "lambdapdk v$have present; updating to v$LAMBDAPDK_VERSION"
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

fetch_and_verify "$LAMBDAPDK_URL" "$LAMBDAPDK_SHA256" "$tmp/lambdapdk.tar.gz" progress

commit="$(tarball_commit "$tmp/lambdapdk.tar.gz")"

echo "Extracting ..."
mkdir -p "$tmp/extract"
tar -xzf "$tmp/lambdapdk.tar.gz" -C "$tmp/extract" --strip-components=1

rm -rf "$DEST"
mkdir -p "$PDKS_DIR"
mv "$tmp/extract" "$DEST"
echo "$LAMBDAPDK_VERSION" > "$MARKER"
if [[ -n "$commit" ]]; then
    printf '%s\n' "$commit" >"$COMMIT_MARKER"
fi

echo "Done: $DEST"
du -sh "$DEST"
