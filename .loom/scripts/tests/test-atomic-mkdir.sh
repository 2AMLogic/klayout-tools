#!/usr/bin/env bash
# test-atomic-mkdir.sh — Tests for lib/atomic-mkdir.sh and its non-filing-lock
# consumer in worktree.sh (#15287).
#
# Ubuntu 26.04's /usr/bin/mkdir is uutils coreutils 0.10.0, which exits 0 when
# the target directory already exists, so a bare `mkdir <lock>` is not mutual
# exclusion (measured 21/100 two-racer double-wins). Covers:
#
#   1. backend resolution order: shell-function mkdir > gnumkdir > direct
#      mkdir(2) (python3/perl) when mkdir is uutils > plain mkdir.
#   2. loom_atomic_mkdir is exclusive under a non-atomic mkdir on PATH.
#   3. worktree.sh's no-daemon mkdir FALLBACK lock (acquire_worktree_lock with
#      $_WT_DAEMON_BIN unset) stays exclusive under the same stub — a second
#      acquirer times out instead of also "winning" the repo-global lock.
#
# build-slot.sh and filing-lock.sh have their own cases in test-build-slot.sh /
# test-filing-lock.sh. No daemon binary is needed: the worktree lock functions
# are cut out of worktree.sh the same way test-worktree-concurrency.sh does.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LIB="$SCRIPTS_DIR/lib/atomic-mkdir.sh"
WORKTREE_SH="$SCRIPTS_DIR/worktree.sh"

source "$SCRIPT_DIR/lib/assert.sh"
assert_true() { if [[ "$1" == "0" ]]; then pass "$2"; else fail "$2 (expected success, got rc=$1)"; fi; }

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

REAL_BASH="$(command -v bash)"
REAL_MKDIR="$(command -v gnumkdir 2>/dev/null || command -v mkdir)"

# A mkdir that behaves like the broken uutils one: --version says so, and a
# mkdir of an EXISTING directory exits 0. `mkdir -p` goes to the real binary.
make_uutils_stub() { # <bindir> <marker>
    local stub="$1/mkdir"
    cat > "$stub" <<EOF
#!$REAL_BASH
if [[ "\${1:-}" == "--version" ]]; then echo "mkdir (uutils coreutils) 0.10.0"; exit 0; fi
if [[ "\${1:-}" == "-p" ]]; then exec "$REAL_MKDIR" "\$@"; fi
echo "called:\$*" >> "$2"
"$REAL_MKDIR" "\$@" 2>/dev/null || [[ -d "\${!#}" ]]
EOF
    chmod +x "$stub"
}

# A PATH dir holding the stub, optionally gnumkdir, plus the tools the cases use.
# $1 = name, $2 = "gnu" | "nognu". Echoes the dir.
make_fake_bin() {
    local bin="$TMPROOT/bin-$1" tool tpath
    mkdir -p "$bin"
    make_uutils_stub "$bin" "$bin/uutils-called.log"
    if [[ "$2" == "gnu" ]]; then
        printf '#!%s\nexec "%s" "$@"\n' "$REAL_BASH" "$REAL_MKDIR" > "$bin/gnumkdir"
        chmod +x "$bin/gnumkdir"
    fi
    for tool in awk cat date dirname git grep head kill python3 perl rm rmdir sed sleep stat tr wc; do
        tpath="$(command -v "$tool" 2>/dev/null || true)"
        if [[ -n "$tpath" ]]; then ln -s "$tpath" "$bin/$tool"; fi
    done
    echo "$bin"
}

echo ""
echo "=== atomic-mkdir.sh: backend resolution ==="

# shellcheck source=../lib/atomic-mkdir.sh
source "$LIB"

(
    mkdir() { command mkdir "$@"; }
    [[ "$(loom_atomic_mkdir_backend)" == "mkdir" ]] || exit 1
    # A function mkdir wins even when gnumkdir exists (test seam, #15122/#15157).
    FAKE="$(make_fake_bin fn gnu)"
    PATH="$FAKE:$PATH"
    [[ "$(loom_atomic_mkdir_backend)" == "mkdir" ]] || exit 2
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "a shell-function mkdir keeps precedence over gnumkdir"

(
    PATH="$(make_fake_bin gnu gnu):$PATH"
    [[ "$(loom_atomic_mkdir_backend)" == "gnumkdir" ]] || exit 1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "uutils mkdir + gnumkdir on PATH resolves to gnumkdir"

(
    PATH="$(make_fake_bin nognu nognu)"
    B="$(loom_atomic_mkdir_backend)"
    [[ "$B" == "python3" || "$B" == "perl" ]] || { echo "backend=$B" >&2; exit 1; }
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "uutils mkdir without gnumkdir resolves to direct mkdir(2) (python3/perl)"

(
    # No stub: this host's own mkdir (GNU coreutils here) resolves to itself or gnumkdir, and works.
    T="$TMPROOT/plain-target"
    loom_atomic_mkdir "$T" || exit 1
    [[ -d "$T" ]] || exit 2
    if loom_atomic_mkdir "$T"; then exit 3; fi
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "loom_atomic_mkdir creates once and refuses an existing directory"

echo ""
echo "=== atomic-mkdir.sh: exclusive under a non-atomic mkdir on PATH ==="

for variant in gnu nognu; do
    (
        FAKE="$(make_fake_bin "excl-$variant" "$variant")"
        T="$TMPROOT/excl-$variant-target"
        # Control: the stub itself double-wins.
        "$FAKE/mkdir" "$T-ctl" && "$FAKE/mkdir" "$T-ctl" || exit 1
        rm -f "$FAKE/uutils-called.log"   # the control's own calls must not count below
        if [[ "$variant" == "gnu" ]]; then PATH="$FAKE:$PATH"; else PATH="$FAKE"; fi
        loom_atomic_mkdir "$T" || exit 2
        if loom_atomic_mkdir "$T"; then exit 3; fi
        [[ ! -e "$FAKE/uutils-called.log" ]] || exit 4
        exit 0
    ) && CASE_RC=0 || CASE_RC=$?
    assert_true "$CASE_RC" "second loom_atomic_mkdir is refused under the stub ($variant); broken mkdir never called"
done

echo ""
echo "=== worktree.sh: the no-daemon mkdir fallback lock stays exclusive (#15287) ==="

# Cut the lock primitives out of worktree.sh exactly like
# test-worktree-concurrency.sh's extract_lock_functions, plus the helper they use.
extract_worktree_lock_functions() { # <outfile> <repo>
    local out="$1" repo="$2" start end
    start=$(awk '/^_worktree_locks_dir\(\) \{/{print NR; exit}' "$WORKTREE_SH")
    end=$(awk '/^release_worktree_lock\(\) \{/{f=1} f && /^}/{print NR; exit}' "$WORKTREE_SH")
    printf 'source %q\n' "$LIB" > "$out"
    sed -n "${start},${end}p" "$WORKTREE_SH" >> "$out"
    : "$repo"
}

for variant in gnu nognu; do
    REPO="$TMPROOT/repo-$variant"
    git init -q -b main "$REPO"
    FAKE="$(make_fake_bin "wt-$variant" "$variant")"
    FUNCS="$TMPROOT/wt-funcs-$variant.sh"
    extract_worktree_lock_functions "$FUNCS" "$REPO"
    (
        cd "$REPO"
        if [[ "$variant" == "gnu" ]]; then PATH="$FAKE:$PATH"; else PATH="$FAKE"; fi
        # shellcheck disable=SC2034
        LOOM_WORKTREE_LOCK_TIMEOUT=1
        # shellcheck disable=SC2034
        LOOM_WORKTREE_LOCK_POLL_INTERVAL=1
        # shellcheck disable=SC2034
        JSON_OUTPUT=""
        print_warning() { :; }
        _WT_DAEMON_BIN=""
        # shellcheck disable=SC1090
        source "$FUNCS"

        acquire_worktree_lock 1 || exit 1
        TOKEN="$WORKTREE_LOCK_TOKEN"
        [[ -n "$TOKEN" ]] || exit 2
        # A second contender (the lock is repo-global; the owner pid is this live
        # shell) must time out. A non-atomic mkdir would hand it the lock too.
        RC2=0
        ( WORKTREE_LOCK_TOKEN=""; acquire_worktree_lock 2 ) || RC2=$?
        release_worktree_lock 1 "$TOKEN"
        [[ "$RC2" -ne 0 ]] || exit 3
        [[ ! -e "$FAKE/uutils-called.log" ]] || exit 4
        exit 0
    ) && CASE_RC=0 || CASE_RC=$?
    assert_true "$CASE_RC" "a second acquire_worktree_lock fallback caller is refused under the stub ($variant)"
done

echo ""
echo "==============================================="
echo "  Tests run:    $TESTS_RUN"
echo -e "  ${GREEN}Passed:${NC}       $TESTS_PASSED"
echo -e "  ${RED}Failed:${NC}       $TESTS_FAILED"
echo "==============================================="

[[ "$TESTS_FAILED" -eq 0 ]]
