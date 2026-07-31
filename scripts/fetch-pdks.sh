#!/usr/bin/env bash
# Fetch open PDK data into pdks/ (gitignored — see pdks/README.md).
#
# Downloads a pinned release of lambdapdk (Apache-2.0), the
# siliconcompiler project's library of open PDK packages: sky130,
# gf180, asap7, freepdk45, ihp130, gt2n, interposer.
#
# Usage: scripts/fetch-pdks.sh [--force]

set -euo pipefail

LAMBDAPDK_VERSION="0.2.17"
LAMBDAPDK_URL="https://github.com/siliconcompiler/lambdapdk/archive/refs/tags/v${LAMBDAPDK_VERSION}.tar.gz"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PDKS_DIR="$REPO_ROOT/pdks"
DEST="$PDKS_DIR/lambdapdk"
MARKER="$DEST/.fetched-version"

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

echo "Downloading lambdapdk v$LAMBDAPDK_VERSION ..."
curl -fL --progress-bar -o "$tmp/lambdapdk.tar.gz" "$LAMBDAPDK_URL"

echo "Extracting ..."
mkdir -p "$tmp/extract"
tar -xzf "$tmp/lambdapdk.tar.gz" -C "$tmp/extract" --strip-components=1

rm -rf "$DEST"
mkdir -p "$PDKS_DIR"
mv "$tmp/extract" "$DEST"
echo "$LAMBDAPDK_VERSION" > "$MARKER"

echo "Done: $DEST"
du -sh "$DEST"
