#!/usr/bin/env bash
# Repository-owned Python runtime bootstrap for a Linux audit host (#2829).
#
# Provisions everything an Auditor needs to run the `klt` CLI from a clean
# Linux checkout that has a POSIX shell, `curl`, `tar` and a SHA-256 tool but
# neither Python nor `uv`:
#
#   1. downloads a pinned `uv` release artifact for this architecture and
#      verifies it against the SHA-256 pinned below (fails closed);
#   2. uses that `uv` to install a uv-managed CPython satisfying
#      pyproject.toml's `requires-python` floor, into the same prefix;
#   3. creates/synchronizes the checkout-local `.venv` from the committed
#      `uv.lock` (`uv sync --locked`: uv.lock is consumed, never rewritten);
#   4. runs the startup smoke -- `klt --help`, `klt version --format json`,
#      `klt kb validate --format json` -- from that environment;
#   5. prints the exact uv version, Python version, environment path and
#      source revision as KEY=value lines on stdout.
#
# Non-root and repository-scoped: everything lands in the caller-selected
# prefix (default `${XDG_CACHE_HOME:-$HOME/.cache}/klayout-tools-auditor`) and
# the checkout's own `.venv`. No sudo, no system package manager, no
# credentials, no Rust toolchain. It never touches a `uv` or Python already
# on PATH -- the uv it runs is always the pinned one in the prefix, and the
# interpreter is always uv-managed inside the prefix
# (`UV_PYTHON_PREFERENCE=only-managed`).
#
# An existing `.venv` is reused only if its interpreter resolves inside this
# bootstrap's prefix. Anything else -- a dangling symlink (e.g. a macOS
# `/Users/.../cpython-...-macos-aarch64-none/bin/python3.12` link copied onto
# a Linux host), an interpreter elsewhere on this host, a Windows-layout or
# non-venv directory -- is refused WITHOUT executing it. `--replace-venv`
# moves such an environment aside to `.venv.foreign-<UTC timestamp>` (never
# deletes it) and builds a fresh one.
#
# Idempotent: a re-run with the same prefix reuses the verified uv (no
# download), lets `uv python install` / `uv sync --locked` no-op, and prints
# the same summary.
#
# Usage:
#   scripts/bootstrap-auditor-python.sh [--prefix DIR] [--python X.Y]
#                                       [--replace-venv] [--no-smoke]
#
# Exit codes (each failure class is distinct so a caller can branch on it):
#   0  success
#   2  usage error
#   10 unsupported OS or CPU architecture
#   11 required host tool missing (curl, tar, sha256sum/shasum)
#   12 prefix not creatable/writable
#   13 uv download failed (network error, HTTP error, interrupted transfer)
#   14 uv artifact checksum mismatch (or unpacked binary is not the pin)
#   15 Python interpreter install failed, or does not satisfy requires-python
#   16 existing .venv provenance mismatch (dangling/foreign interpreter)
#   17 `uv sync --locked` failed (stale lock, resolver/network error)
#   18 a klt smoke command failed
#   19 checkout is not usable (missing pyproject.toml/uv.lock, bad floor)
#
# Re-pinning uv: pick the release, then copy the per-arch digests from the
# official checksum files, e.g.
#   curl -fsSL https://github.com/astral-sh/uv/releases/download/<ver>/uv-x86_64-unknown-linux-gnu.tar.gz.sha256
#   curl -fsSL https://github.com/astral-sh/uv/releases/download/<ver>/uv-aarch64-unknown-linux-gnu.tar.gz.sha256
# and update UV_VERSION and the two UV_SHA256_* constants together.

set -euo pipefail

# --- pins -----------------------------------------------------------------
UV_VERSION="0.12.23"
# From astral-sh/uv release 0.12.23's own `<artifact>.sha256` files.
UV_SHA256_X86_64="9167d72b3319674b6303c4cbe071854bba13ebdf3d76b1a7cbdc175471fb66d6"
UV_SHA256_AARCH64="6524bd338177ed50d035d39354e12545e993bbeba2ecbddf0480c5b3a81d313f"
UV_RELEASE_BASE_URL="https://github.com/astral-sh/uv/releases/download"
# Default interpreter: any X.Y at or above pyproject.toml's floor works;
# 3.12 is inside CI's 3.10-3.14 matrix.
DEFAULT_PYTHON="3.12"

EXIT_USAGE=2
EXIT_PLATFORM=10
EXIT_PREREQ=11
EXIT_PREFIX=12
EXIT_DOWNLOAD=13
EXIT_CHECKSUM=14
EXIT_PYTHON=15
EXIT_PROVENANCE=16
EXIT_SYNC=17
EXIT_SMOKE=18
EXIT_CHECKOUT=19

log() { echo "bootstrap: $*" >&2; }
die() {
    local code="$1"
    shift
    echo "bootstrap: error: $*" >&2
    exit "$code"
}

usage() {
    sed -n '/^# Usage:/,/^#   0  success/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
}

# --- arguments ------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PREFIX="${KLT_AUDITOR_PREFIX:-${XDG_CACHE_HOME:-$HOME/.cache}/klayout-tools-auditor}"
PYTHON_REQUEST="$DEFAULT_PYTHON"
REPLACE_VENV=0
SMOKE=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix)
            [[ $# -ge 2 && -n "$2" ]] || die "$EXIT_USAGE" "--prefix needs a directory"
            PREFIX="$2"
            shift 2
            ;;
        --python)
            [[ $# -ge 2 && -n "$2" ]] || die "$EXIT_USAGE" "--python needs a version like 3.12"
            PYTHON_REQUEST="$2"
            shift 2
            ;;
        --replace-venv)
            REPLACE_VENV=1
            shift
            ;;
        --no-smoke)
            SMOKE=0
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            usage
            die "$EXIT_USAGE" "unknown argument: $1"
            ;;
    esac
done

# --- platform -------------------------------------------------------------
detect_target() {
    local os arch
    os="$(uname -s)"
    arch="$(uname -m)"
    if [[ "$os" != "Linux" ]]; then
        die "$EXIT_PLATFORM" "unsupported OS '$os': this bootstrap targets Linux audit hosts only"
    fi
    case "$arch" in
        x86_64 | amd64)
            UV_TARGET="x86_64-unknown-linux-gnu"
            UV_SHA256="$UV_SHA256_X86_64"
            ;;
        aarch64 | arm64)
            UV_TARGET="aarch64-unknown-linux-gnu"
            UV_SHA256="$UV_SHA256_AARCH64"
            ;;
        *)
            die "$EXIT_PLATFORM" "unsupported CPU architecture '$arch' (supported: x86_64, aarch64)"
            ;;
    esac
    UV_ARTIFACT="uv-${UV_TARGET}.tar.gz"
    UV_URL="${UV_RELEASE_BASE_URL}/${UV_VERSION}/${UV_ARTIFACT}"
}

check_prereqs() {
    local tool
    for tool in curl tar uname; do
        command -v "$tool" >/dev/null 2>&1 \
            || die "$EXIT_PREREQ" "required tool '$tool' not found on PATH"
    done
    if command -v sha256sum >/dev/null 2>&1; then
        SHA256_TOOL="sha256sum"
    elif command -v shasum >/dev/null 2>&1; then
        SHA256_TOOL="shasum"
    else
        die "$EXIT_PREREQ" "no SHA-256 tool found on PATH (need sha256sum or shasum)"
    fi
}

sha256_of() {
    if [[ "$SHA256_TOOL" == "sha256sum" ]]; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

# --- checkout -------------------------------------------------------------
# Reads the `requires-python = ">=X.Y"` floor; anything fancier is refused
# rather than guessed at, so a future floor change cannot silently pass.
read_python_floor() {
    [[ -f "$REPO_ROOT/pyproject.toml" ]] \
        || die "$EXIT_CHECKOUT" "no pyproject.toml in $REPO_ROOT"
    [[ -f "$REPO_ROOT/uv.lock" ]] \
        || die "$EXIT_CHECKOUT" "no uv.lock in $REPO_ROOT (the bootstrap only syncs from a committed lockfile)"
    local spec
    spec="$(sed -n 's/^requires-python *= *"\(.*\)"[[:space:]]*$/\1/p' "$REPO_ROOT/pyproject.toml" | head -n 1)"
    if [[ ! "$spec" =~ ^\>=([0-9]+)\.([0-9]+)$ ]]; then
        die "$EXIT_CHECKOUT" "cannot interpret requires-python '$spec' in pyproject.toml (expected '>=X.Y')"
    fi
    FLOOR_MAJOR="${BASH_REMATCH[1]}"
    FLOOR_MINOR="${BASH_REMATCH[2]}"
}

# version_meets_floor <X.Y[.Z]> -> 0 if >= floor
version_meets_floor() {
    [[ "$1" =~ ^([0-9]+)\.([0-9]+)(\.[0-9]+)?$ ]] || return 1
    local major="${BASH_REMATCH[1]}" minor="${BASH_REMATCH[2]}"
    ((major > FLOOR_MAJOR || (major == FLOOR_MAJOR && minor >= FLOOR_MINOR)))
}

source_revision() {
    local rev=""
    if command -v git >/dev/null 2>&1; then
        rev="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)"
    fi
    if [[ -z "$rev" ]]; then
        echo "unknown"
        return
    fi
    if [[ -n "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=no 2>/dev/null || true)" ]]; then
        rev="${rev}-dirty"
    fi
    echo "$rev"
}

# --- prefix ---------------------------------------------------------------
prepare_prefix() {
    if ! mkdir -p "$PREFIX" 2>/dev/null; then
        die "$EXIT_PREFIX" "cannot create prefix '$PREFIX' (choose a user-writable --prefix)"
    fi
    local probe="$PREFIX/.write-probe.$$"
    if ! (: >"$probe") 2>/dev/null; then
        die "$EXIT_PREFIX" "prefix '$PREFIX' is not writable (choose a user-writable --prefix)"
    fi
    rm -f "$probe"
    PREFIX="$(cd "$PREFIX" && pwd -P)"
    UV_HOME="$PREFIX/uv-$UV_VERSION-$UV_TARGET"
    UV_BIN="$UV_HOME/uv"
    PYTHON_DIR="$PREFIX/python"
    mkdir -p "$PYTHON_DIR" "$PREFIX/cache" \
        || die "$EXIT_PREFIX" "cannot create directories under '$PREFIX'"
}

# --- uv -------------------------------------------------------------------
uv_is_installed() {
    [[ -x "$UV_BIN" && -f "$UV_HOME/.verified" ]] || return 1
    [[ "$(cat "$UV_HOME/.verified")" == "$UV_VERSION $UV_TARGET $UV_SHA256" ]] || return 1
    [[ "$("$UV_BIN" --version 2>/dev/null)" == "uv $UV_VERSION"* ]]
}

install_uv() {
    if uv_is_installed; then
        log "reusing verified uv $UV_VERSION at $UV_BIN (no download)"
        UV_ACTION="reused"
        return
    fi
    local work
    work="$(mktemp -d "$PREFIX/.uv-download.XXXXXX")" \
        || die "$EXIT_PREFIX" "cannot create a staging directory under '$PREFIX'"
    # shellcheck disable=SC2064  # expand $work now: it is local to this function
    trap "rm -rf '$work'" EXIT

    log "downloading $UV_URL"
    if ! curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "$work/$UV_ARTIFACT" "$UV_URL"; then
        die "$EXIT_DOWNLOAD" "download of $UV_URL failed (network/HTTP error or interrupted transfer); nothing was installed -- check connectivity and re-run"
    fi

    local actual
    actual="$(sha256_of "$work/$UV_ARTIFACT")"
    if [[ "$actual" != "$UV_SHA256" ]]; then
        die "$EXIT_CHECKSUM" "checksum mismatch for $UV_ARTIFACT
  url:      $UV_URL
  expected: $UV_SHA256
  actual:   $actual
nothing was installed; a truncated download or a tampered/mirrored artifact both land here"
    fi

    mkdir -p "$work/unpack"
    tar -xzf "$work/$UV_ARTIFACT" -C "$work/unpack" \
        || die "$EXIT_CHECKSUM" "verified artifact $UV_ARTIFACT did not unpack"
    local unpacked="$work/unpack/uv-$UV_TARGET"
    [[ -x "$unpacked/uv" ]] \
        || die "$EXIT_CHECKSUM" "verified artifact $UV_ARTIFACT has no uv-$UV_TARGET/uv binary"
    local reported
    reported="$("$unpacked/uv" --version 2>/dev/null || true)"
    [[ "$reported" == "uv $UV_VERSION"* ]] \
        || die "$EXIT_CHECKSUM" "unpacked uv reports '$reported', expected 'uv $UV_VERSION'"

    echo "$UV_VERSION $UV_TARGET $UV_SHA256" >"$unpacked/.verified"
    rm -rf "$UV_HOME"
    mv "$unpacked" "$UV_HOME" || die "$EXIT_PREFIX" "cannot move uv into '$UV_HOME'"
    rm -rf "$work"
    trap - EXIT
    log "installed uv $UV_VERSION ($UV_TARGET, sha256 $UV_SHA256) at $UV_BIN"
    UV_ACTION="downloaded"
}

# Every uv call runs with an environment that pins it to the prefix and the
# checkout, regardless of what the caller's shell exported.
run_uv() {
    env -u VIRTUAL_ENV -u UV_PYTHON -u UV_SYSTEM_PYTHON -u UV_NO_SYNC \
        UV_CACHE_DIR="$PREFIX/cache" \
        UV_PYTHON_INSTALL_DIR="$PYTHON_DIR" \
        UV_PYTHON_BIN_DIR="$PREFIX/python-bin" \
        UV_PYTHON_PREFERENCE=only-managed \
        UV_PYTHON_DOWNLOADS=automatic \
        UV_PROJECT_ENVIRONMENT="$VENV" \
        UV_NO_PROGRESS=1 \
        "$UV_BIN" "$@"
}

# --- python ---------------------------------------------------------------
install_python() {
    version_meets_floor "$PYTHON_REQUEST" \
        || die "$EXIT_PYTHON" "--python '$PYTHON_REQUEST' does not satisfy requires-python >=$FLOOR_MAJOR.$FLOOR_MINOR"
    log "ensuring uv-managed CPython $PYTHON_REQUEST under $PYTHON_DIR"
    run_uv python install --no-bin "$PYTHON_REQUEST" >&2 \
        || die "$EXIT_PYTHON" "uv python install $PYTHON_REQUEST failed (see uv output above)"
    PYTHON_BIN="$(run_uv python find "$PYTHON_REQUEST" 2>/dev/null)" \
        || die "$EXIT_PYTHON" "uv cannot find the CPython $PYTHON_REQUEST it just installed"
    is_inside_prefix "$PYTHON_BIN" \
        || die "$EXIT_PYTHON" "uv resolved CPython to '$PYTHON_BIN', outside the bootstrap prefix '$PREFIX'"
}

# is_inside_prefix <path> -> 0 if <path> fully resolves to an existing file
# under the prefix's managed-Python directory.
is_inside_prefix() {
    local resolved
    resolved="$(readlink -f -- "$1" 2>/dev/null)" || return 1
    [[ -e "$resolved" && "$resolved" == "$PYTHON_DIR/"* ]]
}

# --- environment provenance -----------------------------------------------
# Prints a reason (and returns 1) if $VENV must not be reused. Never executes
# anything inside $VENV.
venv_problem() {
    local py="$VENV/bin/python"
    if [[ ! -d "$VENV" ]]; then
        echo "'$VENV' exists but is not a directory"
    elif [[ ! -f "$VENV/pyvenv.cfg" ]]; then
        echo "'$VENV' has no pyvenv.cfg (not a virtual environment, or not one this bootstrap manages)"
    elif [[ ! -L "$py" && ! -e "$py" ]]; then
        echo "'$VENV' has no bin/python (a non-POSIX or cross-platform environment)"
    elif [[ ! -e "$py" ]]; then
        echo "'$py' is a dangling symlink to '$(readlink -- "$py")' (an interpreter from another host)"
    elif ! is_inside_prefix "$py"; then
        echo "'$py' resolves to '$(readlink -f -- "$py")', outside this bootstrap's prefix '$PYTHON_DIR'"
    else
        return 0
    fi
    return 1
}

check_venv() {
    VENV_ACTION="synced"
    if [[ ! -e "$VENV" && ! -L "$VENV" ]]; then
        VENV_ACTION="created"
        return
    fi
    local problem
    if problem="$(venv_problem)"; then
        return
    fi
    if [[ "$REPLACE_VENV" -ne 1 ]]; then
        die "$EXIT_PROVENANCE" "refusing to reuse $VENV: $problem
  it was not executed. Re-run with --replace-venv to move it aside to
  $VENV.foreign-<timestamp> and build a host-local environment."
    fi
    local aside
    aside="$VENV.foreign-$(date -u +%Y%m%dT%H%M%SZ)"
    while [[ -e "$aside" || -L "$aside" ]]; do
        aside="${aside}x"
    done
    mv -- "$VENV" "$aside" || die "$EXIT_PROVENANCE" "cannot move $VENV aside to $aside"
    log "--replace-venv: moved foreign environment to $aside ($problem)"
    VENV_ACTION="replaced"
}

sync_venv() {
    log "synchronizing $VENV from uv.lock (uv sync --locked --extra dev)"
    run_uv sync --project "$REPO_ROOT" --locked --extra dev --python "$PYTHON_BIN" >&2 \
        || die "$EXIT_SYNC" "uv sync --locked failed; if uv reports the lockfile needs updating, uv.lock is stale relative to pyproject.toml -- fix that in the repository, the bootstrap never rewrites it"
    local problem
    if ! problem="$(venv_problem)"; then
        die "$EXIT_PROVENANCE" "after sync, $problem"
    fi
    VENV_PYTHON_VERSION="$("$VENV/bin/python" --version 2>&1 | awk '{print $2}')"
    version_meets_floor "$VENV_PYTHON_VERSION" \
        || die "$EXIT_PYTHON" "environment Python '$VENV_PYTHON_VERSION' does not satisfy requires-python >=$FLOOR_MAJOR.$FLOOR_MINOR"
}

# --- smoke ----------------------------------------------------------------
smoke() {
    local klt="$VENV/bin/klt"
    [[ -x "$klt" ]] || die "$EXIT_SMOKE" "no klt entry point at $klt after sync"
    local -a cmds=("--help" "version --format json" "kb validate --format json")
    local args
    for args in "${cmds[@]}"; do
        log "smoke [$SOURCE_REVISION]: klt $args"
        # shellcheck disable=SC2086  # word-split the fixed argument strings
        (cd "$REPO_ROOT" && "$klt" $args) >&2 \
            || die "$EXIT_SMOKE" "smoke command 'klt $args' failed (exit $?) at source revision $SOURCE_REVISION"
    done
}

# --- main -----------------------------------------------------------------
main() {
    check_prereqs
    detect_target
    read_python_floor
    SOURCE_REVISION="$(source_revision)"
    VENV="$REPO_ROOT/.venv"
    prepare_prefix
    # Provenance first: a foreign .venv is refused before anything is
    # downloaded.
    check_venv
    install_uv
    install_python
    sync_venv
    if [[ "$SMOKE" -eq 1 ]]; then
        smoke
        SMOKE_RESULT="passed"
    else
        SMOKE_RESULT="skipped"
    fi
    cat <<EOF
uv_version=$UV_VERSION
uv_target=$UV_TARGET
uv_sha256=$UV_SHA256
uv_path=$UV_BIN
uv_action=$UV_ACTION
python_version=$VENV_PYTHON_VERSION
python_path=$(readlink -f -- "$VENV/bin/python")
environment=$VENV
environment_action=$VENV_ACTION
source_revision=$SOURCE_REVISION
smoke=$SMOKE_RESULT
EOF
}

main
