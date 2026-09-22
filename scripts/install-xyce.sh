#!/usr/bin/env bash
# Install the pinned Xyce 7.10 cross-validation oracle binary -- issue #2016
# (pairing #5 of #2007's oracle tracking issue).
#
# Xyce (Sandia National Laboratories) is a from-scratch SPICE implementation
# rather than a SPICE 3f5 derivative, which makes it a genuinely independent
# oracle for `klt sim`'s ngspice results: it is the `engine: "xyce"` side of
# tests/test_sim_xyce_oracle.py. It is never a runtime dependency of `klt`
# itself -- nothing in `src/klayout_tools/` requires it unless a caller
# explicitly asks for that engine; see docs/design/xyce-oracle.md.
#
# Which distribution, and why this one:
#
#   Sandia publishes official, versioned installers per platform at
#   xyce.sandia.gov/downloads/executables/. The macOS artifact is the
#   "XyceNF" (NORAD) build: it carries additional proprietary device models
#   Sandia can distribute as binaries but not as source. For oracle purposes
#   this is fine and is recorded as such in the methodology doc: the SPICE
#   frontend, netlist parser, and the R/C/D/V device models the cross-
#   validation fixtures use are the same open-source Xyce 7.10 code either
#   way. The open-source build itself requires building Trilinos first --
#   hours of build on the box class this repo targets -- which is exactly
#   the setup cost #2007 flagged when it ranked this pairing last.
#
#   Platform support here:
#   - macOS arm64 (the Darwin .pkg): fully automated below. The .pkg targets
#     /usr/local, which would need sudo, so this script instead unpacks the
#     payload into $XYCE_INSTALL_PREFIX and adds an rpath entry so the
#     binary finds its bundled libxyce.dylib from there (build-only fixup,
#     ad-hoc re-signed; the original download is checksum-verified first).
#   - Linux: the official artifact is an RHEL8 RPM (no Ubuntu/Debian build);
#     on rpm-based systems pass --rpm <file-or-url> after downloading it,
#     or `yum localinstall` it yourself. A from-source build recipe (Trilinos
#     pin + Xyce cmake) is documented in docs/design/xyce-oracle.md for the
#     repo's Linux fleet box, where it can run in the background.
#
# Usage: scripts/install-xyce.sh [--force] [--rpm <file-or-url>]
#   Installs into $XYCE_INSTALL_PREFIX (default: ~/.cache/xyce-7.10).
#   Add "$XYCE_INSTALL_PREFIX/bin" to $PATH after running. Idempotent: a
#   prior successful install for the same pinned version is left in place
#   unless --force is given (matches install-fastcap.sh's convention).

set -euo pipefail

# shellcheck source=scripts/_install_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_install_common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The pinned release. Bump the version, the URL, the checksum, and
# CHANGELOG.md together in the same change. The checksum pins the *zip
# wrapper* Sandia serves (the .pkg lives inside it); recompute with:
#   curl -fsL -o /tmp/xyce.zip "$XYCE_MACOS_URL" && shasum -a 256 /tmp/xyce.zip
XYCE_VERSION="7.10.0"
XYCE_MACOS_URL="https://xyce.sandia.gov/download/1952/"
# Verified 2026-09-22 while writing this script (macOS arm64 serial build,
# XyceNF-7.10.0-Darwin.pkg inside the zip).
XYCE_MACOS_SHA256="06cd438e0a3d4dc86948c1d0f6684fa125db3dfa1776ff1d9339ebb77865754b"

PREFIX="${XYCE_INSTALL_PREFIX:-$HOME/.cache/xyce-${XYCE_VERSION}}"
MARKER="$PREFIX/.installed-version"

FORCE=0
RPM_SOURCE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --force) FORCE=1 ;;
        --rpm)
            RPM_SOURCE="${2:?--rpm needs a file path or URL}"
            shift
            ;;
        *)
            echo "usage: $0 [--force] [--rpm <file-or-url>]" >&2
            exit 2
            ;;
    esac
    shift
done

check_marker "$MARKER" "$XYCE_VERSION" "$FORCE" "xyce"

case "$(uname -s)" in
    Darwin)
        if [[ "$(uname -m)" != "arm64" ]]; then
            echo "error: the pinned macOS installer is arm64-only; on Intel Macs build from source (see docs/design/xyce-oracle.md)" >&2
            exit 1
        fi
        for required in curl tar unzip install_name_tool; do
            if ! command -v "$required" &>/dev/null; then
                echo "error: '$required' is required to install xyce but is not on \$PATH" >&2
                exit 1
            fi
        done

        tmp_zip="$(mktemp)"
        tmp_dir="$(mktemp -d)"
        trap 'rm -f "$tmp_zip"; rm -rf "$tmp_dir"' EXIT

        fetch_and_verify "$XYCE_MACOS_URL" "$XYCE_MACOS_SHA256" "$tmp_zip"

        echo "Unpacking ..."
        unzip -o -q "$tmp_zip" -d "$tmp_dir"
        pkg_path="$(find "$tmp_dir" -maxdepth 1 -name '*.pkg' | head -1)"
        if [[ -z "$pkg_path" ]]; then
            echo "error: no .pkg inside the downloaded zip -- is the pin still valid?" >&2
            exit 1
        fi
        # Expand rather than `sudo installer`: the payload lands in the
        # user-owned prefix instead of /usr/local, and the binary is made
        # self-locating with an rpath (build-only fixup below).
        pkgutil --expand-full "$pkg_path" "$tmp_dir/expanded"

        echo "Installing into $PREFIX ..."
        rm -rf "$PREFIX"
        mkdir -p "$PREFIX"
        # The Xyce executable ships in the "Unspecified" sub-package; the
        # Core package carries libxyce.dylib. Overlay both into one prefix.
        cp -R "$tmp_dir"/expanded/*.pkg/Payload/usr/local/XyceNF_*/ "$PREFIX/"

        binary="$PREFIX/bin/Xyce"
        if [[ ! -f "$binary" ]]; then
            echo "error: $binary missing after install -- unexpected package layout" >&2
            exit 1
        fi
        # The binary references @rpath/libxyce.dylib; point the rpath at
        # this prefix (idempotent: skip when one already resolves).
        if ! otool -l "$binary" | grep -q "path $PREFIX/lib"; then
            install_name_tool -add_rpath "$PREFIX/lib" "$binary"
        fi

        # Smoke test before declaring success (fail-closed, matching
        # install-fastcap.sh): solve one RC transient and require the
        # .measure value in the log. A build that cannot parse or solve is
        # caught here rather than surfacing later as a skipped oracle.
        echo "Smoke-testing the installed binary ..."
        smoke_dir="$(mktemp -d)"
        trap 'rm -f "$tmp_zip"; rm -rf "$tmp_dir" "$smoke_dir"' EXIT
        cat >"$smoke_dir/smoke.cir" <<'CIR'
* klt xyce install smoke test: RC step, 63.2% at t = RC = 1us
V1 in 0 PULSE(0 1 0 1p 1p 10 20)
R1 in out 1k
C1 out 0 1n
.tran 10u 5m
.meas tran v1rc FIND v(out) AT=1u
.end
CIR
        if ! "$binary" -l "$smoke_dir/smoke.log" "$smoke_dir/smoke.cir" >/dev/null 2>&1; then
            echo "error: the installed Xyce failed its smoke netlist" >&2
            exit 1
        fi
        # V(1ms) = 1 - e^-1 = 0.6321; require the measure line, value only
        # sanity-checked (the oracle tests own the tight numbers).
        if ! grep -q "V1RC = 6.3" "$smoke_dir/smoke.log"; then
            echo "error: Xyce's smoke .measure did not produce the expected operating point" >&2
            exit 1
        fi
        ;;
    Linux)
        if [[ -z "$RPM_SOURCE" ]]; then
            cat >&2 <<'EOF'
error: no Linux installation path was selected.

The official Linux artifact is an RHEL8 RPM (no Debian/Ubuntu build):
  https://xyce.sandia.gov/downloads/executables/

Either pass the downloaded RPM to this script:
  scripts/install-xyce.sh --rpm ./xyce-7.10.rpm

or build from source on a Linux box with the recorded recipe (Trilinos
pin + Xyce cmake, hours of build): docs/design/xyce-oracle.md
EOF
            exit 1
        fi
        if ! command -v yum &>/dev/null && ! command -v dnf &>/dev/null; then
            echo "error: the official Linux artifact is an RPM; this host has no yum/dnf (see the source-build recipe in docs/design/xyce-oracle.md)" >&2
            exit 1
        fi
        if [[ "$RPM_SOURCE" == http* ]]; then
            tmp_rpm="$(mktemp)"
            trap 'rm -f "$tmp_rpm"' EXIT
            fetch_and_verify "$RPM_SOURCE" "${XYCE_LINUX_SHA256:?set XYCE_LINUX_SHA256 to the sha256 of the RPM}" "$tmp_rpm"
            RPM_SOURCE="$tmp_rpm"
        fi
        echo "Installing RPM ($RPM_SOURCE) -- may prompt for sudo ..."
        if command -v dnf &>/dev/null; then
            sudo dnf install -y "$RPM_SOURCE"
        else
            sudo yum localinstall -y "$RPM_SOURCE"
        fi
        PREFIX="/usr/local/Xyce-Release-7.10-NORAD"
        ;;
    *)
        echo "error: unsupported platform $(uname -s) -- see docs/design/xyce-oracle.md for the source-build recipe" >&2
        exit 1
        ;;
esac

finish_install "$MARKER" "$XYCE_VERSION" "xyce" "$PREFIX"
