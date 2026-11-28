#!/usr/bin/env bash
# test-filing-lock.sh — Tests for the machine-wide issue-filing lock (#6714)
#
# Covers defaults/scripts/lib/filing-lock.sh, the Bash half of the cross-process
# lock that replaced #3707's documentation-only "do not run concurrent
# Architects" mitigation after it regressed on 2026-08-08 and silently corrupted
# five issue bodies across two repos for 13 days:
#
#   1. acquire/release round-trip — the holder dir + owner.json appear/disappear.
#   2. THE #6714 REGRESSION — two concurrent issue-creating agents in DIFFERENT
#      repos, asserting each filed body matches the request that produced it.
#   3. BOUNDED + FAIL-SAFE — a contended acquire returns 75 (DEFER) rather than
#      filing unserialized. This is the inverse of build-slot.sh's degrade-open.
#   4. crashed holder — a dead owner PID on this host is reaped immediately.
#   5. crashed holder — an aged holder with no owner record is reaped by mtime.
#   6. a foreign host's PID is never used for liveness.
#   7. degrade OPEN when the lock store is unusable (a FILE in the way).
#   8. LOOM_FILING_LOCK=0 disables serialization entirely.
#   9. re-entrancy — LOOM_FILING_LOCK_HELD makes a nested acquire a no-op.
#  10. create-issue.sh actually takes the lock (the wiring, not just the lib).
#
# Style follows test-build-slot.sh: throwaway mktemp dirs, assert harness, no
# network, no real ~/.loom. The captain tier (#13918) is pinned off for the
# whole suite (see the LOOM_DAEMON_BIN pin below): a host that has a real
# loom-daemon must not let live fleet state decide this suite's outcome.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FILING_LOCK_LIB="$SCRIPTS_DIR/lib/filing-lock.sh"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

# Local assert_eq kept: takes (actual, expected, msg); shared assert_eq is (expected, actual, msg).
assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$2', got '$1')"; fi
}
assert_true() { if [[ "$1" == "0" ]]; then pass "$2"; else fail "$2 (expected success, got rc=$1)"; fi; }

# NOTE on the `( … ) && CASE_RC=0 || CASE_RC=$?` idiom: every case runs in a
# subshell so its env exports cannot leak into the next one, and the rc is
# captured explicitly so a FAILING case reports as a test failure instead of
# aborting the whole suite under `set -e`.

# shellcheck source=../lib/filing-lock.sh
source "$FILING_LOCK_LIB"

TMPROOT="$(mktemp -d)"
cleanup() { rm -rf "$TMPROOT"; }
trap cleanup EXIT

new_store() {
    local d
    d="$(mktemp -d "$TMPROOT/store.XXXXXX")"
    printf '%s\n' "$d/issue-filing"
}

# Fast bounds so the suite stays quick.
export LOOM_FILING_LOCK_WAIT_SECS=1
# Never reach the real safehouse socket from a test.
unset SAFEHOUSED_SOCKET LOOM_SAFEHOUSE_SOCKET SAFEHOUSE_PERSONA || true
unset LOOM_FILING_LOCK_HELD || true
# Isolate the captain tier (#13918, the nightly #14663 flake): unpinned, a
# host with a loom-daemon on PATH or in $LOOM_DAEMON_BIN takes every acquire
# below into the REAL fleet filing slot — a slot held by another filer answers
# rc 75 and the concurrent filers exit 75 (case rc 1), so live fleet state
# would decide this suite's outcome. Pointing the bin at a nonexistent path
# makes `command -v` fail in loom_filing_lock_host and
# _loom_filing_lock_captain_acquire alike — the lib header's "no binary"
# contract: file under the host lock alone, exactly like a CI runner. The
# sections that DO test daemon interplay (host-id, captain stubs) override
# this pin locally with their own LOOM_DAEMON_BIN.
export LOOM_DAEMON_BIN="$TMPROOT/no-loom-daemon"

echo ""
echo "=== filing-lock.sh: acquire / release round-trip ==="

DIR="$(new_store)"
(
    export LOOM_FILING_LOCK_DIR="$DIR"
    loom_filing_lock_acquire "architect" >/dev/null 2>&1
    [[ -d "$DIR/holder" ]] || exit 1
    [[ -f "$DIR/holder/owner.json" ]] || exit 1
    grep -q '"label":"architect"' "$DIR/holder/owner.json" || exit 1
    [[ "$LOOM_FILING_LOCK_PATH" == "$DIR/holder" ]] || exit 1
    loom_filing_lock_release >/dev/null 2>&1
    [[ ! -e "$DIR/holder" ]] || exit 1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "acquire creates \$store/holder + owner.json; release removes both"

echo ""
echo "=== THE #6714 REGRESSION: concurrent filers in DIFFERENT repos ==="

# Two issue-creating agents, each filing a 4-issue burst into a *different*
# repo, sharing one scratch body path — the only way two processes can swap each
# other's body text, and exactly the 2026-08-08 shape (gf180-sram's bodies
# overwritten with sky130-modexp's). Each filer writes its body, pauses, reads
# it back and "files" it. Without the lock the read-back is the other repo's
# body; with it, every filed body matches the request that produced it.
DIR="$(new_store)"
(
    export LOOM_FILING_LOCK_DIR="$DIR"
    export LOOM_FILING_LOCK_WAIT_SECS=30
    # #15265: record the mkdir rc + holder stat of every attempt (and any reap)
    # in the filer logs, so a hosted-runner failure names its branch.
    export LOOM_FILING_LOCK_DIAG=1
    SCRATCH="$DIR/scratch-body.md"
    FILED="$DIR/filed.txt"
    TAB=$'\t'
    mkdir -p "$DIR"
    : > "$FILED"

    filer() { # <repo> <body> <log>
        local repo="$1" body="$2" log="$3" i rc
        for i in 1 2 3 4; do
            # Acquire output goes to the filer's own log — the caller dumps it
            # on failure (#14663: a blind >/dev/null 2>&1 here left the nightly
            # flake with nothing but "rc=1" to go on). Never discarded blind.
            rc=0
            loom_filing_lock_acquire "$repo" >>"$log" 2>&1 || rc=$?
            if [[ "$rc" -ne 0 ]]; then
                echo "filer ${repo}: acquire FAILED rc=${rc} (iteration ${i})" >>"$log"
                exit "$rc"
            fi
            printf '%s #%s' "$body" "$i" > "$SCRATCH"
            sleep 0.05
            printf '%s%s%s\n' "$repo" "$TAB" "$(cat "$SCRATCH")" >> "$FILED"
            loom_filing_lock_release >>"$log" 2>&1
        done
    }

    ( filer "gf180-sram" "SRAM macro: 512x8 bitcell array" "$DIR/filer-gf180-sram.log" ) &
    P1=$!
    ( filer "sky130-modexp" "RTL synthesis: rtl/modexp.v timing closure" "$DIR/filer-sky130-modexp.log" ) &
    P2=$!
    R1=0; wait "$P1" || R1=$?
    R2=0; wait "$P2" || R2=$?
    if [[ "$R1" -ne 0 || "$R2" -ne 0 ]]; then
        echo "filer exited nonzero: gf180-sram rc=${R1}, sky130-modexp rc=${R2}; filer logs follow" >&2
        sed 's/^/  [gf180-sram] /' "$DIR/filer-gf180-sram.log" >&2 || true
        sed 's/^/  [sky130-modexp] /' "$DIR/filer-sky130-modexp.log" >&2 || true
        exit 1
    fi

    # A writable store must NEVER degrade open (#15122): a vanished holder is a
    # retry, not "unusable". Any such warning means a filer ran unserialized.
    if grep -qE 'degrading open|WITHOUT serialization' "$DIR"/filer-*.log; then
        echo "a filer degraded OPEN on a writable store (ran unserialized); see filer logs below" >&2
        exit 1
    fi

    if [[ "$(wc -l < "$FILED")" -ne 8 ]]; then
        echo "FILED has $(wc -l < "$FILED") rows, want 8; contents follow" >&2
        cat "$FILED" >&2
        exit 1
    fi
    # Every filed body must carry ITS OWN repo's text: all 8 rows must match
    # the correct (repo, body-prefix) pairing, so any single crossed body fails.
    GF="$(grep -c "^gf180-sram${TAB}SRAM macro" "$FILED" || true)"
    SK="$(grep -c "^sky130-modexp${TAB}RTL synthesis" "$FILED" || true)"
    if [[ "$GF" -ne 4 || "$SK" -ne 4 ]]; then
        echo "cross-contaminated FILED (gf180-sram rows=${GF}, sky130-modexp rows=${SK}, want 4/4); contents follow" >&2
        cat "$FILED" >&2
        exit 1
    fi
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" \
    "two concurrent filers in DIFFERENT repos each file their own body (the #6714 regression)"
# Re-emit the diagnostics AFTER the assertion line (#15122). The case body above
# already prints them, but ci-suite-excerpt.sh keeps only 3 TRAILING context
# lines after a FAIL line (LOOM_CI_FAIL_CONTEXT_LINES), so a dump printed before
# it never reached the CI excerpt. Hence exactly one collapsed line per file
# (gf180-sram log, sky130-modexp log, FILED rows) — it must fit in that window.
if [[ "$CASE_RC" -ne 0 ]]; then
    for f in "$DIR/filer-gf180-sram.log" "$DIR/filer-sky130-modexp.log" "$DIR/filed.txt"; do
        printf '  diag %s: %s\n' "${f##*/}" \
            "$( [[ -f "$f" ]] && tr '\n\t' '|~' < "$f" || echo '(missing)' )" >&2
    done
fi

# #15122: a waiter whose mkdir lost to a peer whose release ran before the
# follow-up `-d` check must RETRY, not degrade open. Forced deterministically by
# shadowing mkdir: the first `mkdir <holder>` reports failure with no holder dir
# on disk — exactly what the waiter observes when the peer has already released.
DIR="$(new_store)"
(
    export LOOM_FILING_LOCK_DIR="$DIR"
    LOG="$DIR.log"
    MKDIR_CALLS=0
    mkdir() {
        if [[ "${!#}" == "$DIR/holder" && "$MKDIR_CALLS" -eq 0 ]]; then
            MKDIR_CALLS=1
            return 1
        fi
        command mkdir "$@"
    }
    RC=0
    loom_filing_lock_acquire "waiter" >"$LOG" 2>&1 || RC=$?
    [[ "$RC" -eq 0 ]] || exit 1
    [[ "$LOOM_FILING_LOCK_PATH" == "$DIR/holder" ]] || exit 1   # holds the lock
    [[ -d "$DIR/holder" && -f "$DIR/holder/owner.json" ]] || exit 1
    ! grep -qE 'degrading open|WITHOUT serialization' "$LOG" || exit 1
    loom_filing_lock_release >/dev/null 2>&1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "a holder that vanishes between mkdir and -d is retried, not degraded open (#15122)"

# The retry is bounded: a mkdir that always fails with no holder on a store that
# is a directory (e.g. permission denied) must still degrade open, not spin.
DIR="$(new_store)"
(
    export LOOM_FILING_LOCK_DIR="$DIR"
    mkdir() {
        if [[ "${!#}" == "$DIR/holder" ]]; then return 1; fi
        command mkdir "$@"
    }
    RC=0
    loom_filing_lock_acquire "waiter" >/dev/null 2>&1 || RC=$?
    [[ "$RC" -eq 0 ]] || exit 1
    [[ -z "${LOOM_FILING_LOCK_PATH:-}" ]] || exit 1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "a persistently failing holder mkdir degrades open after a bounded retry (no spin)"

# The unserialized control, forced deterministically: without the lock, filer A
# reads back filer B's body. Proves the case above is exercising the mechanism.
DIR="$(new_store)"
(
    mkdir -p "$DIR"
    SCRATCH="$DIR/scratch-body.md"
    printf 'SRAM macro: 512x8 bitcell array' > "$SCRATCH"   # A writes
    printf 'RTL synthesis: rtl/modexp.v' > "$SCRATCH"       # B overwrites
    A_FILED="$(cat "$SCRATCH")"                             # A reads back
    [[ "$A_FILED" == RTL* ]] || exit 1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" \
    "control: unserialized, the same interleaving DOES cross-contaminate"

echo ""
echo "=== bounded wait is FAIL-SAFE (defer), not fail-open ==="

DIR="$(new_store)"
(
    export LOOM_FILING_LOCK_DIR="$DIR"
    export LOOM_FILING_LOCK_WAIT_SECS=1
    # A live holder owned by THIS shell's pid, so no reap leg can fire.
    mkdir -p "$DIR/holder"
    printf '{"host":"%s","pid":%s,"label":"burst","acquired_at":%s}\n' \
        "$(loom_filing_lock_host)" "$$" "$(date +%s)" > "$DIR/holder/owner.json"
    RC=0
    loom_filing_lock_acquire "second-filer" >/dev/null 2>&1 || RC=$?
    [[ "$RC" -eq 75 ]] || exit 1
    # And nothing was taken.
    [[ -z "${LOOM_FILING_LOCK_PATH:-}" ]] || exit 1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "a contended acquire returns 75 (DEFER), never 0-without-a-lock"

echo ""
echo "=== a crashed holder cannot wedge issue creation ==="

DIR="$(new_store)"
(
    export LOOM_FILING_LOCK_DIR="$DIR"
    mkdir -p "$DIR/holder"
    # A killed holder: the lock survives, the process does not. Spawn a real
    # child and reap it so its PID is genuinely dead (never a recycled guess).
    ( exit 0 ) &
    DEAD_PID=$!
    wait "$DEAD_PID" 2>/dev/null || true
    printf '{"host":"%s","pid":%s,"label":"crashed-architect","acquired_at":%s}\n' \
        "$(loom_filing_lock_host)" "$DEAD_PID" "$(date +%s)" > "$DIR/holder/owner.json"
    loom_filing_lock_acquire "next-architect" >/dev/null 2>&1 || exit 1
    grep -q '"label":"next-architect"' "$DIR/holder/owner.json" || exit 1
    loom_filing_lock_release >/dev/null 2>&1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "a dead owner PID on THIS host is reaped immediately"

DIR="$(new_store)"
(
    export LOOM_FILING_LOCK_DIR="$DIR"
    export LOOM_FILING_LOCK_STALE_SECS=1
    mkdir -p "$DIR/holder"   # no owner.json — only the mtime leg can free it
    sleep 1.1
    loom_filing_lock_acquire "next" >/dev/null 2>&1 || exit 1
    loom_filing_lock_release >/dev/null 2>&1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "an aged holder with NO owner record is reaped by mtime"

# Regression (#15265 review): with LOOM_FILING_LOCK_DIAG unset, reaping a holder
# that has no owner.json must not leak a failed-redirect error onto stderr.
DIR="$(new_store)"
(
    export LOOM_FILING_LOCK_DIR="$DIR"
    export LOOM_FILING_LOCK_STALE_SECS=1
    unset LOOM_FILING_LOCK_DIAG
    mkdir -p "$DIR/holder"
    sleep 1.1
    ERR="$(loom_filing_lock_acquire "next" 2>&1 >/dev/null)" || exit 1
    loom_filing_lock_release >/dev/null 2>&1
    [[ "$ERR" != *"No such file or directory"* ]] || { echo "$ERR" >&2; exit 1; }
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "reaping an owner-less holder is silent about owner.json when DIAG is off"

DIR="$(new_store)"
(
    export LOOM_FILING_LOCK_DIR="$DIR"
    export LOOM_FILING_LOCK_WAIT_SECS=1
    mkdir -p "$DIR/holder"
    ( exit 0 ) &
    DEAD_PID=$!
    wait "$DEAD_PID" 2>/dev/null || true
    # Same dead PID, but recorded against ANOTHER host: a PID number from
    # another machine says nothing about a process here, so the dead-PID leg
    # must NOT fire and the acquire must defer.
    printf '{"host":"some-other-host","pid":%s,"label":"remote","acquired_at":%s}\n' \
        "$DEAD_PID" "$(date +%s)" > "$DIR/holder/owner.json"
    RC=0
    loom_filing_lock_acquire "local" >/dev/null 2>&1 || RC=$?
    [[ "$RC" -eq 75 ]] || exit 1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "a foreign host's PID is never used for liveness"

echo ""
echo "=== degrade open only when there is no lock to take ==="

DIR="$(new_store)"
(
    mkdir -p "$(dirname "$DIR")"
    printf 'x' > "$DIR"           # a FILE where the store must be
    export LOOM_FILING_LOCK_DIR="$DIR"
    loom_filing_lock_acquire "architect" >/dev/null 2>&1 || exit 1
    [[ -z "${LOOM_FILING_LOCK_PATH:-}" ]] || exit 1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "an unusable store degrades OPEN (rc 0, no lock), never defers"

DIR="$(new_store)"
(
    export LOOM_FILING_LOCK_DIR="$DIR"
    export LOOM_FILING_LOCK=0
    loom_filing_lock_acquire "architect" >/dev/null 2>&1 || exit 1
    [[ ! -e "$DIR/holder" ]] || exit 1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "LOOM_FILING_LOCK=0 disables serialization entirely"

DIR="$(new_store)"
(
    export LOOM_FILING_LOCK_DIR="$DIR"
    export LOOM_FILING_LOCK_HELD=1
    loom_filing_lock_acquire "nested" >/dev/null 2>&1 || exit 1
    [[ ! -e "$DIR/holder" ]] || exit 1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "LOOM_FILING_LOCK_HELD makes a nested acquire a re-entrant no-op"

echo ""
echo "=== store resolution ==="

assert_eq "$(LOOM_FILING_LOCK_DIR="/tmp/x" loom_filing_lock_store)" "/tmp/x" \
    "LOOM_FILING_LOCK_DIR overrides the default location"
assert_eq "$(LOOM_FILING_LOCK_DIR="   " HOME=/h loom_filing_lock_store)" "/h/.loom/locks/issue-filing" \
    "a blank override falls back to ~/.loom/locks/issue-filing"

echo ""
echo "=== create-issue.sh is wired to the lock ==="

if grep -q "lib/filing-lock.sh" "$SCRIPTS_DIR/create-issue.sh"; then
    pass "create-issue.sh sources lib/filing-lock.sh"
else
    fail "create-issue.sh no longer takes the filing lock — the #6714 serialization point is gone"
fi
if grep -q "loom_filing_lock_acquire" "$SCRIPTS_DIR/create-issue.sh" \
    && grep -q "LOOM_FILING_LOCK_DEFER_RC" "$SCRIPTS_DIR/create-issue.sh"; then
    pass "create-issue.sh acquires the lock and propagates the DEFER exit code"
else
    fail "create-issue.sh does not acquire the lock / does not propagate the DEFER exit code"
fi

echo ""
echo "=== host identity comes from loom-daemon host-id (#10023) ==="

HID_DIR="$(mktemp -d)"
printf '#!/usr/bin/env bash\n[[ "${1:-}" == "host-id" ]] && { echo persisted-host-abc; exit 0; }\nexit 0\n' > "$HID_DIR/fake-daemon"
printf '#!/usr/bin/env bash\nexit 1\n' > "$HID_DIR/failing-daemon"
chmod +x "$HID_DIR/fake-daemon" "$HID_DIR/failing-daemon"
GOT="$(env -u LOOM_HOST_ID HOSTNAME=some-distinct-hostname LOOM_DAEMON_BIN="$HID_DIR/fake-daemon" \
    bash -c 'source "$1"; loom_filing_lock_host' _ "$FILING_LOCK_LIB")"
[[ "$GOT" == "persisted-host-abc" ]] && pass "owner host equals loom-daemon host-id, not \$HOSTNAME" \
    || fail "owner host was '$GOT', expected persisted-host-abc"
GOT="$(env -u LOOM_HOST_ID HOSTNAME=some-distinct-hostname LOOM_DAEMON_BIN="$HID_DIR/failing-daemon" \
    bash -c 'source "$1"; loom_filing_lock_host' _ "$FILING_LOCK_LIB")"
[[ "$GOT" == "unknown-host" ]] && pass "a failing host-id yields the unknown-host literal" \
    || fail "failing host-id produced '$GOT', expected unknown-host"
rm -rf "$HID_DIR"

echo ""
echo "=== captain acquire: subcommand stderr reaches the caller (#14422) ==="

CAP_DIR="$(mktemp -d "$TMPROOT/captain.XXXXXX")"
# Grants: warn line on stderr, holder id on stdout, rc 0.
printf '#!/usr/bin/env bash\necho "WARN stub-fail-open-line" >&2\necho holder-xyz\nexit 0\n' > "$CAP_DIR/warn-grant"
# Deferred: the subcommand's EXIT_DEFERRED (75) with its reason on stderr.
printf '#!/usr/bin/env bash\necho "filing-lock: deferred: stub-held" >&2\nexit 75\n' > "$CAP_DIR/defer"
# An older binary / crash: an unrelated rc with a message on stderr.
printf '#!/usr/bin/env bash\necho "error: unrecognized subcommand" >&2\nexit 2\n' > "$CAP_DIR/older"
chmod +x "$CAP_DIR"/warn-grant "$CAP_DIR"/defer "$CAP_DIR"/older

# Run in a subshell (the helper sets a global) and report "<rc>|<holder>"; the
# helper's own stderr lands in $2.
captain_case() {
    local stub="$1" errfile="$2"
    (
        export LOOM_DAEMON_BIN="$stub"
        rc=0
        _loom_filing_lock_captain_acquire "t" 5 2>"$errfile" || rc=$?
        printf '%s|%s' "$rc" "$LOOM_FILING_LOCK_CAPTAIN_HOLDER"
    )
}

GOT="$(captain_case "$CAP_DIR/warn-grant" "$CAP_DIR/err.grant")"
assert_eq "$GOT" "0|holder-xyz" "a grant still captures stdout as the holder id"
if grep -q "WARN stub-fail-open-line" "$CAP_DIR/err.grant"; then
    pass "the subcommand's stderr warn line reaches the caller's stderr"
else
    fail "the subcommand's stderr warn line was swallowed (got: $(cat "$CAP_DIR/err.grant"))"
fi
if grep -q "holder-xyz" "$CAP_DIR/err.grant"; then
    fail "the holder id leaked onto stderr (stdout/stderr were mixed)"
else
    pass "the holder id stays on stdout, off stderr"
fi

GOT="$(captain_case "$CAP_DIR/defer" "$CAP_DIR/err.defer")"
assert_eq "$GOT" "$LOOM_FILING_LOCK_DEFER_RC|" "an rc-75 answer still returns LOOM_FILING_LOCK_DEFER_RC with no holder"
if grep -q "deferred: stub-held" "$CAP_DIR/err.defer"; then
    pass "the defer reason reaches the caller's stderr"
else
    fail "the defer reason was swallowed (got: $(cat "$CAP_DIR/err.defer"))"
fi

GOT="$(captain_case "$CAP_DIR/older" "$CAP_DIR/err.older")"
assert_eq "$GOT" "0|" "any other failure still fails open (rc 0, no holder)"

# Release stays quiet (out of scope for #14422): best effort, its output is discarded.
GOT="$(
    LOOM_DAEMON_BIN="$CAP_DIR/warn-grant" LOOM_FILING_LOCK_CAPTAIN_HOLDER=holder-xyz \
        _loom_filing_lock_captain_release 2>&1
)"
assert_eq "$GOT" "" "the captain release stays silent"
rm -rf "$CAP_DIR"

echo ""
echo "=== atomic mkdir backend avoids uutils coreutils TOCTOU (#15265) ==="

# Ubuntu 26.04 ships uutils coreutils 0.10.0 as /usr/bin/mkdir, which checks
# path.exists() then returns Ok(()) on EEXIST when path.is_dir() even without
# -p (breaking mkdir locking under concurrency). Simulate that exact broken
# uutils mkdir on PATH and verify filing-lock bypasses it: first via gnumkdir
# when available, and via python3/perl direct mkdir(2) when gnumkdir is absent.
REAL_MKDIR="$(command -v gnumkdir 2>/dev/null || command -v mkdir)"

# Helper: build a fake PATH bin dir with a simulated broken uutils mkdir that
# exits 0 even when the target directory already exists (the non-atomic bug),
# while forwarding `mkdir -p` to the real binary for store setup.
REAL_BASH="$(command -v bash)"
make_uutils_stub() {
    local bindir="$1" marker="$2"
    cat > "$bindir/mkdir" <<EOF
#!$REAL_BASH
if [[ "\${1:-}" == "--version" ]]; then
    echo "mkdir (uutils coreutils) 0.10.0"
    exit 0
fi
if [[ "\${1:-}" == "-p" ]]; then
    exec "$REAL_MKDIR" "\$@"
fi
echo "called:\$*" >> "$marker"
# Buggy uutils behavior: ignore EEXIST on an existing directory and exit 0.
"$REAL_MKDIR" "\$@" 2>/dev/null || [[ -d "\${!#}" ]]
EOF
    chmod +x "$bindir/mkdir"
}

# Case 1: uutils mkdir on PATH + gnumkdir present -> uses gnumkdir, never the
# broken uutils mkdir, and rejects a contended acquire with rc 75 (DEFER).
DIR="$(new_store)"
(
    FAKE_BIN="$(mktemp -d "$TMPROOT/fake-gnu.XXXXXX")"
    UUTILS_LOG="$FAKE_BIN/uutils-called.log"
    GNU_LOG="$FAKE_BIN/gnu-called.log"
    make_uutils_stub "$FAKE_BIN" "$UUTILS_LOG"
    cat > "$FAKE_BIN/gnumkdir" <<EOF
#!/usr/bin/env bash
echo "called:\$*" >> "$GNU_LOG"
exec "$REAL_MKDIR" "\$@"
EOF
    chmod +x "$FAKE_BIN/gnumkdir"

    export PATH="$FAKE_BIN:$PATH"
    export LOOM_FILING_LOCK_DIR="$DIR"
    export LOOM_FILING_LOCK_WAIT_SECS=1

    loom_filing_lock_acquire "first" >/dev/null 2>&1 || exit 1
    [[ ! -e "$UUTILS_LOG" ]] || { echo "broken uutils mkdir was called: $(cat "$UUTILS_LOG")" >&2; exit 1; }
    [[ -s "$GNU_LOG" ]] || { echo "gnumkdir was NOT called" >&2; exit 1; }

    # A second contender (without LOOM_FILING_LOCK_HELD) must see EEXIST and defer (75).
    RC2=0
    (
        unset LOOM_FILING_LOCK_HELD
        loom_filing_lock_acquire "second" >/dev/null 2>&1
    ) || RC2=$?
    loom_filing_lock_release >/dev/null 2>&1
    [[ "$RC2" -eq "$LOOM_FILING_LOCK_DEFER_RC" ]] || { echo "contender rc=$RC2, want 75" >&2; exit 1; }
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "uutils mkdir on PATH is bypassed in favor of gnumkdir (#15265)"

# Case 2: uutils mkdir on PATH + NO gnumkdir -> falls back to direct mkdir(2)
# via python3 or perl and still rejects a contended acquire with rc 75 (DEFER).
DIR="$(new_store)"
(
    FAKE_BIN="$(mktemp -d "$TMPROOT/fake-fallback.XXXXXX")"
    UUTILS_LOG="$FAKE_BIN/uutils-called.log"
    make_uutils_stub "$FAKE_BIN" "$UUTILS_LOG"
    # Populate a clean PATH without gnumkdir (in case the host has /usr/bin/gnumkdir,
    # e.g. Ubuntu 26.04), symlinking only the standard utilities filing-lock needs.
    for tool in cat date stat sed rm rmdir tr hostname sleep python3 perl; do
        tpath="$(command -v "$tool" 2>/dev/null || true)"
        [[ -n "$tpath" ]] && ln -s "$tpath" "$FAKE_BIN/$tool"
    done

    export PATH="$FAKE_BIN"
    export LOOM_FILING_LOCK_DIR="$DIR"
    export LOOM_FILING_LOCK_WAIT_SECS=1

    loom_filing_lock_acquire "first" >/dev/null 2>&1 || exit 1
    [[ ! -e "$UUTILS_LOG" ]] || { echo "broken uutils mkdir was called: $(cat "$UUTILS_LOG")" >&2; exit 1; }

    RC2=0
    (
        unset LOOM_FILING_LOCK_HELD
        loom_filing_lock_acquire "second" >/dev/null 2>&1
    ) || RC2=$?
    loom_filing_lock_release >/dev/null 2>&1
    [[ "$RC2" -eq "$LOOM_FILING_LOCK_DEFER_RC" ]] || { echo "contender rc=$RC2, want 75" >&2; exit 1; }
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "uutils mkdir without gnumkdir falls back to direct mkdir(2) syscall (#15265)"

# Case 3: shell-function mkdir() override (test seam from #15122) still wins
# even when gnumkdir is on PATH.
DIR="$(new_store)"
(
    FAKE_BIN="$(mktemp -d "$TMPROOT/fake-fn-seam.XXXXXX")"
    GNU_LOG="$FAKE_BIN/gnu-called.log"
    cat > "$FAKE_BIN/gnumkdir" <<EOF
#!/usr/bin/env bash
echo "called:\$*" >> "$GNU_LOG"
exec "$REAL_MKDIR" "\$@"
EOF
    chmod +x "$FAKE_BIN/gnumkdir"
    export PATH="$FAKE_BIN:$PATH"
    export LOOM_FILING_LOCK_DIR="$DIR"

    FN_CALLED=0
    mkdir() {
        if [[ "${!#}" == "$DIR/holder" ]]; then
            FN_CALLED=1
        fi
        "$REAL_MKDIR" "$@"
    }
    loom_filing_lock_acquire "seam" >/dev/null 2>&1 || exit 1
    loom_filing_lock_release >/dev/null 2>&1
    [[ "$FN_CALLED" -eq 1 ]] || { echo "shell-function mkdir was bypassed" >&2; exit 1; }
    [[ ! -e "$GNU_LOG" ]] || { echo "gnumkdir ran instead of shell-function mkdir" >&2; exit 1; }
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "shell-function mkdir override retains precedence over gnumkdir (#15265)"

echo ""
echo "==============================================="
echo "  Tests run:    $TESTS_RUN"
echo -e "  ${GREEN}Passed:${NC}       $TESTS_PASSED"
echo -e "  ${RED}Failed:${NC}       $TESTS_FAILED"
echo "==============================================="

[[ "$TESTS_FAILED" -eq 0 ]]
