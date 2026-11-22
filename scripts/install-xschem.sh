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
# shasum, plus sudo when the prefix is not writable. Linux x86_64 (Ubuntu 24.04
# "noble") only; the asset is not built for other platforms.
#
# Usage: install-xschem.sh [--force]
#   Installs into $XSCHEM_INSTALL_PREFIX (default: /opt/eda/xschem) and prints
#   the directory to add to PATH. Idempotent: a prior install of the same
#   tag + sha256 is left alone unless --force is given. --force also replaces a
#   non-empty prefix that this script did not create (see "Install location").
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
# Install location: the asset is built with --prefix=/opt/eda/xschem and the
# tree is NOT relocatable. Outside its source tree xschem takes
# XSCHEM_LIBRARY_PATH from the compile-time prefix, so a copy unpacked anywhere
# else cannot find its symbol library (res.sym, lab_pin.sym) -- and an
# XSCHEM_SHAREDIR wrapper only moves the Tcl files, not that library. The script
# therefore unpacks at /opt/eda/xschem and leaves bin/xschem as shipped.
# /opt/eda is root-owned: when the prefix is not writable the write steps go
# through `sudo -n` (passwordless on GitHub-hosted runners; it never prompts).
# XSCHEM_INSTALL_PREFIX exists for tests and for inspecting an unpack; xschem
# installed at any other prefix will not find its symbols, and the script warns.
#
# Exit codes: 0 installed/already installed; 1 any failure (fails closed: a
# missing sha256 in the manifest is an error, never a skipped check).

set -euo pipefail

DEFAULT_PREFIX="/opt/eda/xschem"   # the asset's compile-time --prefix
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
    || die "manifest has no tools.xschem.sha256 -- refusing to install an unverified asset (issue #6219)"
[[ "$expected_sha256" =~ ^[0-9a-f]{64}$ ]] || die "tools.xschem.sha256 is not a 64-hex digest: $expected_sha256"

prefix="${XSCHEM_INSTALL_PREFIX:-$DEFAULT_PREFIX}"
[[ "$prefix" == /* ]] || die "XSCHEM_INSTALL_PREFIX must be an absolute path: $prefix"
marker="$prefix/.installed-version"
want="${tag} ${expected_sha256}"

if [[ "$prefix" != "$DEFAULT_PREFIX" ]]; then
    echo "warning: this xschem build is compiled for $DEFAULT_PREFIX; installed at" \
        "$prefix it cannot find its symbol library (res.sym, lab_pin.sym)." >&2
fi

# Write steps run through `sudo -n` when the nearest existing ancestor of the
# prefix is not writable (the default prefix is under root-owned /opt).
as_root=()
parent="$(dirname "$prefix")"
probe="$parent"
while [[ ! -e "$probe" ]]; do probe="$(dirname "$probe")"; done
if [[ ! -w "$probe" ]]; then
    command -v sudo > /dev/null \
        || die "$prefix is not writable by $(id -un) and sudo is not available"
    as_root=(sudo -n)
fi
priv() { ${as_root[@]+"${as_root[@]}"} "$@"; }

if [[ -f "$marker" && "$FORCE" -eq 0 && "$(cat "$marker")" == "$want" ]]; then
    echo "xschem $tag already installed at $prefix (use --force to reinstall)"
    echo "  Add to PATH: $prefix/bin"
    exit 0
fi

# Reserve recursive removal for a managed install or an explicit --force.
remove_prefix=()
if [[ "$FORCE" -eq 1 || ( ! -L "$prefix" && -f "$marker" ) ]]; then
    remove_prefix=(rm -rf)
elif [[ -e "$prefix" || -L "$prefix" ]]; then
    [[ -d "$prefix" && ! -L "$prefix" ]] || die "$prefix is not a plain directory; refusing to replace it (use --force)"
    # -q keeps a name made entirely of newlines visible in command substitution.
    entries="$(ls -Aq -- "$prefix")" || die "cannot inspect $prefix; refusing to replace it"
    [[ -z "$entries" ]] || die "$prefix exists and was not installed by this script; refusing to replace it (use --force)"
    remove_prefix=(rmdir)
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
# rmdir fails if an inspected directory gained a file during the download.
# Exclusive creation also preserves a destination that appeared in the meantime.
if (( ${#remove_prefix[@]} )); then
    priv "${remove_prefix[@]}" -- "$prefix" || die "could not clear $prefix"
fi
priv mkdir -p -- "$parent" || die "could not create $parent"
priv mkdir -- "$prefix" || die "could not create $prefix"
# --no-same-owner: under sudo the files belong to root, not to whatever uid the
# build runner had when it packed the tarball.
priv tar -C "$prefix" --no-same-owner -xzf "$work/$asset" || die "could not unpack into $prefix"

[[ -x "$prefix/bin/xschem" ]] || die "asset did not contain bin/xschem"

printf '%s\n' "$want" | priv tee "$marker" > /dev/null || die "could not write $marker"
echo "Installed xschem $tag into $prefix"
echo "  Add to PATH: $prefix/bin"
