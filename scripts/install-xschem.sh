#!/usr/bin/env bash
# Install the fleet's pinned xschem build, credential-free -- issue #6219
# (fleet rule: 2AMLogic/2am#7275).
#
# Downloads the PUBLIC release asset published by 2AMLogic's xschem fork
# (https://github.com/<release_repo>/releases/download/<release_tag>/<asset>,
# all three read from `tools.xschem` in docker/eda-sim/pdk-versions.json),
# verifies its sha256 against `tools.xschem.sha256` BEFORE anything is
# unpacked, then unpacks it. No token, no AWS credentials.
#
# SELF-CONTAINED ON PURPOSE: chip-repo CI copies this one file, so it does not
# source scripts/_install_common.sh. Needs bash, curl, jq, tar, sha256sum or
# shasum. Linux x86_64 (Ubuntu 24.04 "noble") only; the asset is not built for
# other platforms.
#
# Usage: install-xschem.sh [--force]
#   Installs into $XSCHEM_INSTALL_PREFIX (default: ~/.cache/xschem-<tag>) and
#   prints the directory to add to PATH. Idempotent: a prior install of the same
#   tag + sha256 is left alone unless --force is given.
#
# Where the pin comes from (first that applies):
#   $XSCHEM_MANIFEST            a path or http(s) URL of pdk-versions.json
#   ../docker/eda-sim/pdk-versions.json next to this script (inside this repo)
#   the manifest on this repo's main branch (a copied script, e.g. chip CI)
#   -- this repo hosts the pin; the release asset lives in the fork named by
#   the manifest's `release_repo`
# A copied script therefore tracks the fleet pin; to freeze a chip repo on a
# specific pin, point XSCHEM_MANIFEST at a vendored manifest or a commit-pinned
# raw URL.
#
# Relocation: the asset is built with --prefix=/opt/eda/xschem, so xschem's
# compiled-in data directory is wrong anywhere else. The installed
# `bin/xschem` is therefore a two-line wrapper that exports XSCHEM_SHAREDIR for
# the real binary (`bin/xschem.real`) -- the same mechanism the fleet's other
# relocated xschem installs use.
#
# Exit codes: 0 installed/already installed; 1 any failure (fails closed: a
# missing sha256 in the manifest is an error, never a skipped check).

set -euo pipefail

MANIFEST_REPO="2AMLogic/klayout-tools"
DEFAULT_MANIFEST_URL="https://raw.githubusercontent.com/${MANIFEST_REPO}/main/docker/eda-sim/pdk-versions.json"

die() { echo "error: $*" >&2; exit 1; }

FORCE=0
case "${1:-}" in
    "") ;;
    --force) FORCE=1 ;;
    *) die "usage: install-xschem.sh [--force]" ;;
esac

[[ "$(uname -s)" == "Linux" && "$(uname -m)" == "x86_64" ]] \
    || die "the pinned xschem asset is Linux x86_64 only (this host: $(uname -s) $(uname -m))"
for tool in curl jq tar; do
    command -v "$tool" > /dev/null || die "$tool is required"
done

sha256_of() {
    if command -v sha256sum > /dev/null; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum > /dev/null; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        die "no sha256 command found (need sha256sum or shasum)"
    fi
}

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# -- resolve the manifest ----------------------------------------------------
manifest="${XSCHEM_MANIFEST:-}"
if [[ -z "$manifest" ]]; then
    local_manifest="$(dirname "${BASH_SOURCE[0]}")/../docker/eda-sim/pdk-versions.json"
    if [[ -f "$local_manifest" ]]; then
        manifest="$local_manifest"
    else
        manifest="$DEFAULT_MANIFEST_URL"
    fi
fi
if [[ "$manifest" =~ ^https?:// ]]; then
    curl -fsSL --connect-timeout 15 --retry 3 --retry-all-errors -o "$work/manifest.json" "$manifest" \
        || die "failed to fetch manifest $manifest"
    manifest="$work/manifest.json"
fi
[[ -f "$manifest" ]] || die "manifest not found: $manifest"

tag="$(jq -er '.tools.xschem.tag' "$manifest")" || die "manifest has no tools.xschem.tag"
asset="$(jq -er '.tools.xschem.asset' "$manifest")" || die "manifest has no tools.xschem.asset"
release_repo="$(jq -er '.tools.xschem.release_repo' "$manifest")" || die "manifest has no tools.xschem.release_repo"
release_tag="$(jq -er '.tools.xschem.release_tag' "$manifest")" || die "manifest has no tools.xschem.release_tag"
expected_sha256="$(jq -er '.tools.xschem.sha256' "$manifest")" \
    || die "manifest has no tools.xschem.sha256 yet -- the asset has not been published/recorded (issue #6219)"
[[ "$expected_sha256" =~ ^[0-9a-f]{64}$ ]] || die "tools.xschem.sha256 is not a 64-hex digest: $expected_sha256"

prefix="${XSCHEM_INSTALL_PREFIX:-$HOME/.cache/xschem-${tag}}"
marker="$prefix/.installed-version"
want="${tag} ${expected_sha256}"

if [[ -f "$marker" && "$FORCE" -eq 0 && "$(cat "$marker")" == "$want" ]]; then
    echo "xschem $tag already installed at $prefix (use --force to reinstall)"
    echo "  Add to PATH: $prefix/bin"
    exit 0
fi

# -- download, verify, THEN unpack -------------------------------------------
url="https://github.com/${release_repo}/releases/download/${release_tag}/${asset}"
echo "Fetching $url ..."
curl -fL --connect-timeout 15 --retry 3 --retry-all-errors -o "$work/$asset" "$url" \
    || die "failed to fetch $url"

actual_sha256="$(sha256_of "$work/$asset")"
if [[ "$actual_sha256" != "$expected_sha256" ]]; then
    echo "error: checksum mismatch for $url" >&2
    echo "  expected: $expected_sha256" >&2
    echo "  actual:   $actual_sha256" >&2
    exit 1
fi

echo "Installing into $prefix ..."
rm -rf "$prefix"
mkdir -p "$prefix"
tar -C "$prefix" -xzf "$work/$asset"

[[ -x "$prefix/bin/xschem" ]] || die "asset did not contain bin/xschem"
mv "$prefix/bin/xschem" "$prefix/bin/xschem.real"
cat > "$prefix/bin/xschem" <<WRAPPER
#!/bin/sh
XSCHEM_SHAREDIR="$prefix/share/xschem"
export XSCHEM_SHAREDIR
exec "$prefix/bin/xschem.real" "\$@"
WRAPPER
chmod 0755 "$prefix/bin/xschem"

echo "$want" > "$marker"
echo "Installed xschem $tag into $prefix"
echo "  Add to PATH: $prefix/bin"
