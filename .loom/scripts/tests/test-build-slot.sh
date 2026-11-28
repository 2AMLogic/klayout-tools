#!/usr/bin/env bash
# test-build-slot.sh — Tests for the machine-wide build-slot lease (#4512)
#
# Covers defaults/scripts/lib/build-slot.sh, the Bash half of the cross-process
# lock that replaced the CPU-headroom admission term:
#
#   1. acquire/release round-trip — the slot lock dir appears and disappears.
#   2. serialization — a second acquire cannot take the only slot.
#   3. BOUNDED WAIT + DEGRADE OPEN — a contended acquire returns 0 with no slot
#      after the bound instead of blocking forever (the "never a deadlock" AC).
#   4. degrade open when the lock store is unusable (a FILE where the slot dir
#      should be).
#   5. LOOM_BUILD_SLOTS=0 disables serialization entirely.
#   6. multi-slot budget — 2 slots admit 2 holders, refuse the 3rd.
#   7. stale reaping — an abandoned slot older than the threshold is reclaimed.
#   8. re-entrancy — LOOM_BUILD_SLOT_HELD makes a nested acquire a no-op.
#   9. path shape parity with `loom_daemon::build_slot::slot_path` ("$dir/slot-N").
#  10. a slot released between the failed mkdir and the -d test is retried
#      (bounded), not misread as an unusable store (#15157).
#  11. mutual exclusion survives a NON-ATOMIC `mkdir` on PATH (uutils coreutils
#      on Ubuntu 26.04, #15287): a second acquire cannot also take the only slot.
#
# Style follows test-disk-headroom.sh: throwaway mktemp dirs, assert harness,
# no network, no real ~/.loom.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_SLOT_LIB="$SCRIPTS_DIR/lib/build-slot.sh"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

# Local assert_eq kept: takes (actual, expected, msg); shared assert_eq is (expected, actual, msg).
assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$2', got '$1')"; fi
}
assert_true() { if [[ "$1" == "0" ]]; then pass "$2"; else fail "$2 (expected success, got rc=$1)"; fi; }

# NOTE on the `( … ) && CASE_RC=0 || CASE_RC=$?` idiom below: every case runs in
# a subshell so its env exports (LOOM_BUILD_SLOT_DIR/HELD) cannot leak into the
# next one, and the rc is captured explicitly so a FAILING case reports as a test
# failure instead of aborting the whole suite under `set -e`.

# shellcheck source=../lib/build-slot.sh
source "$BUILD_SLOT_LIB"

TMPROOT="$(mktemp -d)"
cleanup() { rm -rf "$TMPROOT"; }
trap cleanup EXIT

# Every test gets a fresh slot dir and a fast bound so the suite stays quick.
new_slot_dir() {
    local d
    d="$(mktemp -d "$TMPROOT/slots.XXXXXX")"
    printf '%s\n' "$d/build-slot"
}

export LOOM_BUILD_SLOT_WAIT_SECS=1
unset LOOM_BUILD_SLOT_HELD LOOM_BUILD_SLOTS LOOM_BUILD_SLOT_STALE_SECS || true
# #11014: a suite-wide default, so a case that forgets its own LOOM_BUILD_SLOT_DIR
# lands in the temp root, never in the host's ~/.loom/locks/build-slot.
export LOOM_BUILD_SLOT_DIR="$TMPROOT/default/build-slot"

echo ""
echo "=== build-slot.sh: acquire / release round-trip ==="

DIR="$(new_slot_dir)"
(
    export LOOM_BUILD_SLOT_DIR="$DIR"
    rc=0
    loom_build_slot_acquire "unit" >/dev/null 2>&1 || rc=$?
    [[ "$rc" == "0" ]] || exit 1
    [[ -n "$LOOM_BUILD_SLOT_PATH" ]] || exit 2
    [[ -d "$LOOM_BUILD_SLOT_PATH" ]] || exit 3
    [[ "$LOOM_BUILD_SLOT_HELD" == "1" ]] || exit 4
    loom_build_slot_release >/dev/null 2>&1
    [[ -d "$DIR/slot-0" ]] && exit 5
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "acquire creates slot-0 + exports LOOM_BUILD_SLOT_HELD; release removes it"

echo ""
echo "=== build-slot.sh: serialization + bounded wait (never a deadlock) ==="

DIR="$(new_slot_dir)"
mkdir -p "$DIR"
# Simulate a live holder by taking slot-0 directly (fresh mtime ⇒ not stale).
mkdir "$DIR/slot-0"
START="$(date +%s)"
(
    export LOOM_BUILD_SLOT_DIR="$DIR"
    rc=0
    OUT="$(loom_build_slot_acquire "contended" 2>&1)" || rc=$?
    # Must SUCCEED (degrade open), hold no slot, and say why.
    [[ "$rc" == "0" ]] || exit 1
    [[ -z "$LOOM_BUILD_SLOT_PATH" ]] || exit 2
    grep -q "waiting up to" <<<"$OUT" || exit 3
    grep -q "degrading open" <<<"$OUT" || exit 4
    # It must NOT have claimed to cover children (that would let a nested
    # acquire skip a slot it never took).
    [[ -z "${LOOM_BUILD_SLOT_HELD:-}" ]] || exit 5
    exit 0
) && RC=0 || RC=$?
ELAPSED=$(( $(date +%s) - START ))
assert_true "$RC" "a contended acquire waits the bound, then degrades open (rc=0, no slot, telemetry)"
if (( ELAPSED >= 1 && ELAPSED < 15 )); then
    pass "the wait is bounded (${ELAPSED}s, bound 1s) — no deadlock"
else
    fail "the wait was not bounded as expected (${ELAPSED}s)"
fi
rmdir "$DIR/slot-0"

echo ""
echo "=== build-slot.sh: degrades open when the lock store is unusable ==="

DIR="$(new_slot_dir)"
# A FILE where the slot directory should be ⇒ mkdir -p fails.
printf 'x' > "$DIR"
(
    export LOOM_BUILD_SLOT_DIR="$DIR"
    rc=0
    OUT="$(loom_build_slot_acquire "broken-store" 2>&1)" || rc=$?
    [[ "$rc" == "0" ]] || exit 1
    [[ -z "$LOOM_BUILD_SLOT_PATH" ]] || exit 2
    grep -q "unusable" <<<"$OUT" || exit 3
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "an unusable slot dir degrades open (never a failure, never a spin)"

echo ""
echo "=== build-slot.sh: a slot released mid-race is retried, not 'unusable' (#15157) ==="

# Deterministic interleaving: the FIRST `mkdir <slot>` loses to a "peer" whose
# release then lands before the follow-up `-d` test — i.e. mkdir reports failure
# and NO slot dir is on disk. Shadow mkdir with a function (counter in a file,
# because the round runs in a command substitution subshell).
DIR="$(new_slot_dir)"
mkdir -p "$DIR"
MKDIR_COUNT_FILE="$TMPROOT/mkdir-count.vanish"
: > "$MKDIR_COUNT_FILE"
(
    export LOOM_BUILD_SLOT_DIR="$DIR"
    mkdir() {
        if [[ "${1:-}" == "$DIR/slot-0" ]]; then
            printf 'x\n' >> "$MKDIR_COUNT_FILE"
            if [[ "$(wc -l < "$MKDIR_COUNT_FILE")" -le 1 ]]; then
                return 1   # lost the race; the peer's rmdir already ran
            fi
        fi
        command mkdir "$@"
    }
    rc=0
    # Not `$(...)`: that subshell would swallow LOOM_BUILD_SLOT_PATH.
    loom_build_slot_acquire "vanished" 2>"$TMPROOT/vanished.err" >/dev/null || rc=$?
    OUT="$(cat "$TMPROOT/vanished.err")"
    [[ "$rc" == "0" ]] || exit 1
    [[ "$LOOM_BUILD_SLOT_PATH" == "$DIR/slot-0" ]] || exit 2
    grep -q "degrading open" <<<"$OUT" && exit 3
    grep -q "WITHOUT a slot" <<<"$OUT" && exit 4
    [[ -d "$DIR/slot-0" ]] || exit 5
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "a slot that vanished between mkdir and -d is retried and taken (no degrade-open)"

# A mkdir that ALWAYS fails with the slot absent (a permission-denied store the
# shell cannot create in) must exhaust the bounded retry and still degrade open.
DIR="$(new_slot_dir)"
mkdir -p "$DIR"
MKDIR_COUNT_FILE="$TMPROOT/mkdir-count.denied"
: > "$MKDIR_COUNT_FILE"
(
    export LOOM_BUILD_SLOT_DIR="$DIR"
    mkdir() {
        if [[ "${1:-}" == "$DIR/slot-0" ]]; then
            printf 'x\n' >> "$MKDIR_COUNT_FILE"
            return 1
        fi
        command mkdir "$@"
    }
    rc=0
    OUT="$(loom_build_slot_acquire "denied" 2>&1)" || rc=$?
    [[ "$rc" == "0" ]] || exit 1
    [[ -z "$LOOM_BUILD_SLOT_PATH" ]] || exit 2
    grep -q "unusable" <<<"$OUT" || exit 3
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "a persistently failing mkdir (slot absent) still degrades open"
ATTEMPTS="$(wc -l < "$MKDIR_COUNT_FILE" | tr -d ' ')"
if (( ATTEMPTS >= 2 && ATTEMPTS <= 8 )); then
    pass "the vanished-slot retry is bounded (${ATTEMPTS} mkdir attempts, no spin)"
else
    fail "the vanished-slot retry was not bounded as expected (${ATTEMPTS} mkdir attempts)"
fi

# A real read-only store (skipped as root, which ignores mode bits).
if [[ "$(id -u)" != "0" ]]; then
    DIR="$(new_slot_dir)"
    mkdir -p "$DIR"
    chmod 555 "$DIR"
    (
        export LOOM_BUILD_SLOT_DIR="$DIR"
        rc=0
        OUT="$(loom_build_slot_acquire "readonly" 2>&1)" || rc=$?
        [[ "$rc" == "0" ]] || exit 1
        [[ -z "$LOOM_BUILD_SLOT_PATH" ]] || exit 2
        grep -q "unusable" <<<"$OUT" || exit 3
        exit 0
    ) && CASE_RC=0 || CASE_RC=$?
    chmod 755 "$DIR"
    assert_true "$CASE_RC" "a permission-denied slot dir degrades open (bounded, no spin)"
fi

echo ""
echo "=== build-slot.sh: LOOM_BUILD_SLOTS=0 disables serialization ==="

DIR="$(new_slot_dir)"
(
    export LOOM_BUILD_SLOT_DIR="$DIR" LOOM_BUILD_SLOTS=0
    rc=0
    OUT="$(loom_build_slot_acquire "opted-out" 2>&1)" || rc=$?
    [[ "$rc" == "0" ]] || exit 1
    [[ -z "$LOOM_BUILD_SLOT_PATH" ]] || exit 2
    grep -q "disabled" <<<"$OUT" || exit 3
    [[ -e "$DIR" ]] && exit 4   # must not even create the dir
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "LOOM_BUILD_SLOTS=0 opts out cleanly (no dir, no slot, rc=0)"

echo ""
echo "=== build-slot.sh: multi-slot budget ==="

DIR="$(new_slot_dir)"
mkdir -p "$DIR"
mkdir "$DIR/slot-0"    # holder A
(
    export LOOM_BUILD_SLOT_DIR="$DIR" LOOM_BUILD_SLOTS=2
    rc=0
    loom_build_slot_acquire "second-of-two" >/dev/null 2>&1 || rc=$?
    [[ "$rc" == "0" ]] || exit 1
    [[ "$LOOM_BUILD_SLOT_PATH" == "$DIR/slot-1" ]] || exit 2
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "with 2 slots, a second holder takes slot-1"

# The subshell above exited WITHOUT releasing (no trap), exactly like a crashed
# holder, so slot-1 is still held here — that is the state the third holder must
# face. Both slots are now occupied.
[[ -d "$DIR/slot-1" ]] || fail "slot-1 should still be held by the previous (unreleased) holder"
(
    export LOOM_BUILD_SLOT_DIR="$DIR" LOOM_BUILD_SLOTS=2
    rc=0
    loom_build_slot_acquire "third-of-two" >/dev/null 2>&1 || rc=$?
    [[ "$rc" == "0" ]] || exit 1
    [[ -z "$LOOM_BUILD_SLOT_PATH" ]] || exit 2
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "with 2 slots, a third holder gets none (degrades open after the bound)"
rmdir "$DIR/slot-0" "$DIR/slot-1"

echo ""
echo "=== build-slot.sh: stale slot from a crashed holder is reaped ==="

DIR="$(new_slot_dir)"
mkdir -p "$DIR"
mkdir "$DIR/slot-0"
sleep 1   # let its mtime age past the 0-second-ish threshold below
(
    export LOOM_BUILD_SLOT_DIR="$DIR" LOOM_BUILD_SLOT_STALE_SECS=1
    rc=0
    loom_build_slot_acquire "after-crash" >/dev/null 2>&1 || rc=$?
    [[ "$rc" == "0" ]] || exit 1
    [[ "$LOOM_BUILD_SLOT_PATH" == "$DIR/slot-0" ]] || exit 2
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "a slot older than LOOM_BUILD_SLOT_STALE_SECS is reclaimed"

echo ""
echo "=== build-slot.sh: re-entrancy ==="

DIR="$(new_slot_dir)"
(
    export LOOM_BUILD_SLOT_DIR="$DIR" LOOM_BUILD_SLOT_HELD=1
    rc=0
    OUT="$(loom_build_slot_acquire "nested" 2>&1)" || rc=$?
    [[ "$rc" == "0" ]] || exit 1
    [[ -z "$LOOM_BUILD_SLOT_PATH" ]] || exit 2
    grep -q "re-entrant" <<<"$OUT" || exit 3
    [[ -e "$DIR/slot-0" ]] && exit 4
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "LOOM_BUILD_SLOT_HELD=1 makes a nested acquire a no-op (no second slot taken)"

echo ""
echo "=== build-slot.sh: cross-language path shape parity ==="

DIR="$(new_slot_dir)"
(
    export LOOM_BUILD_SLOT_DIR="$DIR"
    loom_build_slot_acquire "shape" >/dev/null 2>&1
    # `loom_daemon::build_slot::slot_path` composes exactly "$dir/slot-$i"; a
    # divergence here silently stops the daemon gate and the sweep-side gate
    # from serializing against each other.
    [[ "$LOOM_BUILD_SLOT_PATH" == "$DIR/slot-0" ]] || exit 1
    loom_build_slot_release >/dev/null 2>&1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "slot path is \$dir/slot-N, matching build_slot.rs"

assert_eq "$(LOOM_BUILD_SLOT_DIR="/tmp/x" loom_build_slot_dir)" "/tmp/x" \
    "LOOM_BUILD_SLOT_DIR overrides the default location"
assert_eq "$(LOOM_BUILD_SLOT_DIR="   " HOME=/h loom_build_slot_dir)" "/h/.loom/locks/build-slot" \
    "a blank override falls back to the machine-wide ~/.loom/locks/build-slot"

echo ""
echo "=== build-slot.sh: a non-atomic (uutils) mkdir on PATH cannot double-grant a slot (#15287) ==="

# Ubuntu 26.04's /usr/bin/mkdir is uutils coreutils 0.10.0, which exits 0 when the
# target directory already exists (21/100 two-racer double-wins measured). The
# stub reproduces exactly that, deterministically: a mkdir of an EXISTING dir
# succeeds. The slot must still be exclusive, because build-slot goes through
# lib/atomic-mkdir.sh instead of the PATH mkdir.
REAL_BASH="$(command -v bash)"
REAL_MKDIR="$(command -v gnumkdir 2>/dev/null || command -v mkdir)"
STUB_ROOT="$TMPROOT/stubs"
mkdir -p "$STUB_ROOT"
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

# Take the only slot, then a second (non-nested) acquire must NOT get it.
# $1 = slot dir; PATH is set by the caller.
assert_slot_exclusive_under_stub() {
    local dir="$1" out rc=0
    export LOOM_BUILD_SLOT_DIR="$dir" LOOM_BUILD_SLOTS=1
    loom_build_slot_acquire "first" >/dev/null 2>&1 || return 1
    [[ "$LOOM_BUILD_SLOT_PATH" == "$dir/slot-0" ]] || return 2
    out="$(
        unset LOOM_BUILD_SLOT_HELD
        LOOM_BUILD_SLOT_PATH=""
        loom_build_slot_acquire "second" 2>&1
        [[ -z "$LOOM_BUILD_SLOT_PATH" ]] || echo "SECOND-GOT-SLOT"
    )" || rc=$?
    loom_build_slot_release >/dev/null 2>&1 || true
    [[ "$rc" == "0" ]] || return 3
    if grep -q "SECOND-GOT-SLOT" <<<"$out"; then return 4; fi
    grep -q "degrading open" <<<"$out" || return 5
    return 0
}

# Control: the stub really is non-atomic (otherwise the cases below prove nothing).
(
    FAKE_BIN="$STUB_ROOT/control"
    mkdir -p "$FAKE_BIN"
    make_uutils_stub "$FAKE_BIN" "$FAKE_BIN/called.log"
    T="$STUB_ROOT/control-target"
    "$FAKE_BIN/mkdir" "$T" && "$FAKE_BIN/mkdir" "$T"
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "control: the stub mkdir double-wins on an existing dir (the uutils bug)"

# Case 1: gnumkdir present -> used; the broken mkdir is never called.
(
    FAKE_BIN="$STUB_ROOT/gnu"
    mkdir -p "$FAKE_BIN"
    make_uutils_stub "$FAKE_BIN" "$FAKE_BIN/uutils-called.log"
    printf '#!%s\nexec "%s" "$@"\n' "$REAL_BASH" "$REAL_MKDIR" > "$FAKE_BIN/gnumkdir"
    chmod +x "$FAKE_BIN/gnumkdir"
    SLOTS="$STUB_ROOT/gnu-slots"
    mkdir -p "$SLOTS"
    export PATH="$FAKE_BIN:$PATH"
    assert_slot_exclusive_under_stub "$SLOTS/build-slot" || exit $?
    [[ ! -e "$FAKE_BIN/uutils-called.log" ]] || exit 6
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "uutils mkdir + gnumkdir: the slot stays exclusive and the broken mkdir is bypassed"

# Case 2: no gnumkdir -> direct mkdir(2) via python3/perl.
(
    FAKE_BIN="$STUB_ROOT/fallback"
    mkdir -p "$FAKE_BIN"
    make_uutils_stub "$FAKE_BIN" "$FAKE_BIN/uutils-called.log"
    for tool in cat date stat sed rm rmdir tr hostname sleep python3 perl wc head dirname grep; do
        tpath="$(command -v "$tool" 2>/dev/null || true)"
        [[ -n "$tpath" ]] && ln -s "$tpath" "$FAKE_BIN/$tool"
    done
    SLOTS="$STUB_ROOT/fallback-slots"
    mkdir -p "$SLOTS"
    export PATH="$FAKE_BIN"
    assert_slot_exclusive_under_stub "$SLOTS/build-slot" || exit $?
    [[ ! -e "$FAKE_BIN/uutils-called.log" ]] || exit 6
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "uutils mkdir without gnumkdir: the slot stays exclusive via direct mkdir(2)"

echo ""
echo "=== build-gate.sh sources the helper ==="
if grep -q "lib/build-slot.sh" "$SCRIPTS_DIR/build-gate.sh"; then
    pass "build-gate.sh sources lib/build-slot.sh (the designated high-CPU stage)"
else
    fail "build-gate.sh no longer takes a build slot — the #4512 serialization point is gone"
fi

echo ""
echo "==============================================="
echo "  Tests run:    $TESTS_RUN"
echo -e "  ${GREEN}Passed:${NC}       $TESTS_PASSED"
echo -e "  ${RED}Failed:${NC}       $TESTS_FAILED"
echo "==============================================="

[[ "$TESTS_FAILED" -eq 0 ]]
