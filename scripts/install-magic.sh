#!/usr/bin/env bash
# Build and install a pinned, reproducible magic for the cross-validation
# oracle -- issue #2014 (sub-issue of #2007's oracle tracking issue).
#
# `magic` (RTimothyEdwards/magic) is the *oracle* half of
# `tests/test_drc_magic_oracle.py` / `tests/test_extract_magic_oracle.py`:
# an independently implemented geometry engine used to cross-check
# `klt drc`'s violations and `klt extract`'s devices/nets. It is never a
# runtime dependency of `klt` itself -- see `docs/design/magic-oracle.md`
# and `docs/design/lvs-extraction-spike.md` §1 ("oracle, not runtime").
#
# Why a from-source pin rather than `apt-get install magic`, mirroring the
# same call install-yosys.sh made for Yosys: Ubuntu 24.04's `magic` package
# is `8.3.105`, and open_pdks' own sky130/gf180mcu magic technology files
# both declare `requires magic-8.3.411` in their `version` stanza -- i.e.
# the distro build is ~300 revisions too old to even load the decks this
# oracle must run. Verified directly while writing this script: the apt
# build refuses the generated `sky130A.tech`, the pinned build below loads
# it and runs DRC + `ext2spice` headlessly.
#
# `--without-x` builds the non-graphic magic: this repo is headless-always
# (see CLAUDE.md), the oracle only ever drives magic as
# `magic -dnull -noconsole -T <tech> <script.tcl>`, and skipping X11/OpenGL/
# Cairo drops the whole graphics dependency stack from CI. Tcl is *not*
# optional -- magic's batch scripting interface is the Tcl interpreter.
#
# System build dependencies (tcl-dev, tk-dev, m4, csh) are the caller's /
# CI workflow's responsibility to install, mirroring install-yosys.sh's
# same split (`.github/workflows/magic-oracle.yml` installs them via
# scripts/ci-apt-install.sh); this script owns only the versioned,
# checksummed fetch-and-build.
#
# Usage: scripts/install-magic.sh [--force]
#   Installs into $MAGIC_INSTALL_PREFIX (default: ~/.cache/magic-<version>).
#   Add "$MAGIC_INSTALL_PREFIX/bin" to $PATH after running. Idempotent: a
#   prior successful install for the same pinned version is left in place
#   unless --force is given (matches install-yosys.sh's own convention).

set -euo pipefail

# shellcheck source=scripts/_install_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_install_common.sh"

# Pinned release -- bump the tag, asset checksum, and CHANGELOG.md together
# in the same change if this is ever refreshed. Fails closed on mismatch.
MAGIC_VERSION="8.3.683"
# The exact commit the tag names, recorded for provenance only (the tag
# archive, not this commit sha, is what is fetched and verified):
#   https://github.com/RTimothyEdwards/magic/commit/4481c509dae88e96c3af51f45bc3545ec6af7f60
#
# magic publishes only AppImage binaries as release *assets* (EL7/8/9/10
# builds), not a source tarball, so this pins the tag's source archive the
# way scripts/fetch-pdks.sh already pins lambdapdk's -- by URL + sha256 --
# rather than install-yosys.sh's release-asset URL.
MAGIC_ASSET_URL="https://github.com/RTimothyEdwards/magic/archive/refs/tags/${MAGIC_VERSION}.tar.gz"
# Computed by downloading the pinned tag archive once and hashing it:
#   curl -fL -o /tmp/magic-8.3.683.tar.gz "$MAGIC_ASSET_URL"
#   shasum -a 256 /tmp/magic-8.3.683.tar.gz
MAGIC_ASSET_SHA256="e54b74b00aa3eb5e0536b80c3e5f99953ace9307023e7f319891f12cf60c7658"

PREFIX="${MAGIC_INSTALL_PREFIX:-$HOME/.cache/magic-${MAGIC_VERSION}}"
MARKER="$PREFIX/.installed-version"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

check_marker "$MARKER" "$MAGIC_VERSION" "$FORCE" "magic"

tmp_tarball="$(mktemp)"
src_dir="$(mktemp -d)"
trap 'rm -f "$tmp_tarball"; rm -rf "$src_dir"' EXIT

fetch_and_verify "$MAGIC_ASSET_URL" "$MAGIC_ASSET_SHA256" "$tmp_tarball"

echo "Extracting into $src_dir ..."
tar -xzf "$tmp_tarball" -C "$src_dir" --strip-components=1

echo "Configuring (prefix=$PREFIX, --without-x) ..."
(
    cd "$src_dir"
    ./configure --prefix="$PREFIX" --without-x
)

echo "Building (parallel=$nproc_val) ..."
make -C "$src_dir" -j"$nproc_val"

echo "Installing into $PREFIX ..."
rm -rf "$PREFIX"
make -C "$src_dir" install

finish_install "$MARKER" "$MAGIC_VERSION" "magic" "$PREFIX"
