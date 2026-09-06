#!/usr/bin/env bash
# test-check-dep-recheck-idempotency.sh - Unit tests for
# check-dep-recheck-idempotency.sh, the regression guard + decision helper
# for Curator's "Re-check Idempotency" rule (#1523).
#
# This is a black-box test: the script is a full CLI (no functions to
# source), invoked as a subprocess via `--file` fixtures so no `gh`/network
# call is ever made. Real `jq` is used unstubbed. Mirrors the fixture-driven
# pattern in test-check-verified-corrections-preserved.sh (files on disk, not
# a live forge) rather than the `gh`-stubbing pattern in
# test-check-promotion-landed.sh, since this SUT's live-fetch path is a single
# well-isolated `gh api ... --paginate` call with no branching logic worth
# separately exercising here.
#
# Scenario (a)-(b) reconstruct the actual #1523 incident: known-bad fixture
# built from #528's real `curator:dep-recheck` marker timestamps/hashes
# (2026-08-31 through 2026-09-06), and a known-good fixture using the
# legitimate ~24h heartbeat cadence visible earlier in that same history, so
# the guard is proven to flag the real violation without false-positiving on
# the real non-violating cadence right next to it.
#
# Usage:
#   ./.loom/scripts/tests/test-check-dep-recheck-idempotency.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
SUT="$SCRIPTS_DIR/check-dep-recheck-idempotency.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" == "$actual" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if printf '%s' "$haystack" | grep -qF -- "$needle"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

if [[ ! -x "$SUT" ]]; then
    echo -e "${RED}FAIL${NC}: check-dep-recheck-idempotency.sh missing or not executable at $SUT"
    exit 1
fi
command -v jq >/dev/null 2>&1 || { echo -e "${RED}FAIL${NC}: jq not found on PATH"; exit 1; }

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

run_sut() {
    OUT="$("$SUT" "$@" 2>/tmp/dri-test-stderr.$$)"
    RC=$?
    ERR="$(cat /tmp/dri-test-stderr.$$ 2>/dev/null)"
    rm -f "/tmp/dri-test-stderr.$$"
}

get_field() {
    local out="$1" key="$2"
    printf '%s\n' "$out" | sed -n "s/^${key}=//p" | head -n1
}

echo "=== check-dep-recheck-idempotency.sh ==="
echo ""

# --- Fixture: #528's real marker history, 2026-08-31 through 2026-09-06 ----
# (verbatim timestamps/hashes reported in issue #1523; bodies trimmed to just
# the marker since the SUT only reads the marker + timestamp).
cat > "$WORK_DIR/528-full.json" <<'EOF'
[
  {"created_at":"2026-08-31T11:30:45Z","body":"still blocked <!-- curator:dep-recheck:bb3b15b9f3db0761 -->"},
  {"created_at":"2026-09-01T12:19:46Z","body":"still blocked <!-- curator:dep-recheck:bb3b15b9f3db0761 -->"},
  {"created_at":"2026-09-02T12:47:06Z","body":"still blocked <!-- curator:dep-recheck:bb3b15b9f3db0761 -->"},
  {"created_at":"2026-09-03T12:49:17Z","body":"still blocked <!-- curator:dep-recheck:bb3b15b9f3db0761 -->"},
  {"created_at":"2026-09-04T13:34:20Z","body":"still blocked <!-- curator:dep-recheck:bb3b15b9f3db0761 -->"},
  {"created_at":"2026-09-05T13:42:50Z","body":"still blocked <!-- curator:dep-recheck:bb3b15b9f3db0761 -->"},
  {"created_at":"2026-09-06T01:21:21Z","body":"still blocked <!-- curator:dep-recheck:5342934786687480 -->"},
  {"created_at":"2026-09-06T08:23:52Z","body":"still blocked <!-- curator:dep-recheck:bb3b15b9f3db0761 -->"},
  {"created_at":"2026-09-06T12:48:22Z","body":"still blocked <!-- curator:dep-recheck:bb3b15b9f3db0761 -->"}
]
EOF

# Known-good prefix: only the legitimate ~24h cadence, before the incident.
cat > "$WORK_DIR/528-good-prefix.json" <<'EOF'
[
  {"created_at":"2026-08-31T11:30:45Z","body":"still blocked <!-- curator:dep-recheck:bb3b15b9f3db0761 -->"},
  {"created_at":"2026-09-01T12:19:46Z","body":"still blocked <!-- curator:dep-recheck:bb3b15b9f3db0761 -->"},
  {"created_at":"2026-09-02T12:47:06Z","body":"still blocked <!-- curator:dep-recheck:bb3b15b9f3db0761 -->"},
  {"created_at":"2026-09-03T12:49:17Z","body":"still blocked <!-- curator:dep-recheck:bb3b15b9f3db0761 -->"},
  {"created_at":"2026-09-04T13:34:20Z","body":"still blocked <!-- curator:dep-recheck:bb3b15b9f3db0761 -->"},
  {"created_at":"2026-09-05T13:42:50Z","body":"still blocked <!-- curator:dep-recheck:bb3b15b9f3db0761 -->"}
]
EOF

echo "--- AUDIT mode ---"

# (a) Known-bad: #528's real history flags exactly one violation -- the
#     08:23/12:48 pair, ~4.4h apart, hash bb3b15b9f3db0761.
run_sut --file "$WORK_DIR/528-full.json"
assert_eq "1" "$RC" "(a) #528 real history -> VIOLATION exit code 1"
assert_eq "VIOLATION" "$(get_field "$OUT" DECISION)" "(a) DECISION=VIOLATION"
assert_eq "1" "$(get_field "$OUT" VIOLATIONS)" "(a) exactly one violation found (not the whole daily cadence)"
assert_contains "$OUT" "at=2026-09-06T08:23:52Z and at=2026-09-06T12:48:22Z" "(a) violation names the true offending pair"

# (b) Known-good: the legitimate ~24h cadence alone -> no violation. This is
#     the Test Plan's explicit false-positive guard: the daily heartbeat
#     recurrence of the same hash is INTENDED behavior, not a violation.
run_sut --file "$WORK_DIR/528-good-prefix.json"
assert_eq "0" "$RC" "(b) legitimate ~24h cadence -> exit 0"
assert_eq "OK" "$(get_field "$OUT" DECISION)" "(b) DECISION=OK"
assert_eq "0" "$(get_field "$OUT" VIOLATIONS)" "(b) zero violations on the legit cadence"

# (c) Synthetic well-spaced sequence (different hashes, arbitrary gaps): no
#     violation, since no two ADJACENT entries share a hash inside the window.
cat > "$WORK_DIR/synthetic-good.json" <<'EOF'
[
  {"createdAt":"2026-01-01T00:00:00Z","body":"<!-- curator:dep-recheck:1111111111111111 -->"},
  {"createdAt":"2026-01-01T01:00:00Z","body":"<!-- curator:dep-recheck:2222222222222222 -->"},
  {"createdAt":"2026-01-02T02:00:00Z","body":"<!-- curator:dep-recheck:2222222222222222 -->"},
  {"createdAt":"2026-01-03T03:00:00Z","body":"<!-- curator:dep-recheck:3333333333333333 -->"}
]
EOF
run_sut --file "$WORK_DIR/synthetic-good.json"
assert_eq "0" "$RC" "(c) synthetic well-spaced sequence -> exit 0"
assert_eq "OK" "$(get_field "$OUT" DECISION)" "(c) DECISION=OK"

# (d) Empty history -> OK, not an error.
echo "[]" > "$WORK_DIR/empty.json"
run_sut --file "$WORK_DIR/empty.json"
assert_eq "0" "$RC" "(d) empty comment history -> exit 0"
assert_eq "OK" "$(get_field "$OUT" DECISION)" "(d) DECISION=OK on empty history"

# (e) A same-hash pair separated by a DIFFERENT-hash comment is never a
#     violation, even if the two same-hash comments are close together in
#     real time -- only chronologically ADJACENT pairs matter (the
#     conclusion legitimately changed and reverted).
cat > "$WORK_DIR/changed-reverted.json" <<'EOF'
[
  {"created_at":"2026-09-05T13:42:50Z","body":"<!-- curator:dep-recheck:bb3b15b9f3db0761 -->"},
  {"created_at":"2026-09-06T01:21:21Z","body":"<!-- curator:dep-recheck:5342934786687480 -->"},
  {"created_at":"2026-09-06T08:23:52Z","body":"<!-- curator:dep-recheck:bb3b15b9f3db0761 -->"}
]
EOF
run_sut --file "$WORK_DIR/changed-reverted.json"
assert_eq "0" "$RC" "(e) same hash reappearing after an intervening different hash -> exit 0"
assert_eq "OK" "$(get_field "$OUT" DECISION)" "(e) DECISION=OK, not a violation"

# (f) Exact-boundary gap (exactly --hours apart) is NOT a violation (< window,
#     not <=).
cat > "$WORK_DIR/exact-boundary.json" <<'EOF'
[
  {"created_at":"2026-09-01T00:00:00Z","body":"<!-- curator:dep-recheck:aaaaaaaaaaaaaaaa -->"},
  {"created_at":"2026-09-02T00:00:00Z","body":"<!-- curator:dep-recheck:aaaaaaaaaaaaaaaa -->"}
]
EOF
run_sut --file "$WORK_DIR/exact-boundary.json"
assert_eq "0" "$RC" "(f) exactly 24h apart -> not a violation"

# (g) One second under the boundary IS a violation.
cat > "$WORK_DIR/under-boundary.json" <<'EOF'
[
  {"created_at":"2026-09-01T00:00:00Z","body":"<!-- curator:dep-recheck:aaaaaaaaaaaaaaaa -->"},
  {"created_at":"2026-09-01T23:59:59Z","body":"<!-- curator:dep-recheck:aaaaaaaaaaaaaaaa -->"}
]
EOF
run_sut --file "$WORK_DIR/under-boundary.json"
assert_eq "1" "$RC" "(g) one second under 24h -> violation"

# (h) Custom --hours narrows the window: the #528 4.4h-apart pair is no
#     longer a violation once the window is set below the actual gap.
run_sut --file "$WORK_DIR/528-full.json" --hours 4
assert_eq "0" "$RC" "(h) --hours 4 (< the real 4.4h gap) -> no violation"

echo ""
echo "--- DECISION mode ---"

# (i) No prior marker at all -> NONE, exit 0 (always comment on a first pass).
run_sut --file "$WORK_DIR/empty.json" --assume-new-hash abc123 --assume-new-at 2026-09-06T12:48:22Z
assert_eq "0" "$RC" "(i) first-ever check -> exit 0"
assert_eq "NONE" "$(get_field "$OUT" DECISION)" "(i) DECISION=NONE"

# (j) Reproduce the actual bad post: candidate hash matches the true
#     immediately-preceding comment (08:23:52), only ~4.4h later -> SKIP,
#     exit 20. This is the DECISION-mode analogue of scenario (a): had the
#     12:48 pass called this script, it would have been told not to post.
run_sut --file "$WORK_DIR/528-full.json" --assume-new-hash bb3b15b9f3db0761 --assume-new-at 2026-09-06T12:48:22Z
assert_eq "20" "$RC" "(j) same hash as the true PRIOR, 4.4h later -> SKIP, exit 20"
assert_eq "SKIP" "$(get_field "$OUT" DECISION)" "(j) DECISION=SKIP"
assert_eq "2026-09-06T08:23:52Z" "$(get_field "$OUT" PRIOR_AT)" "(j) PRIOR_AT is the TRUE immediately-preceding comment, not the stale 2026-09-02 baseline the real incident cited"

# (k) Same hash, past the staleness window -> STALE (heartbeat allowed),
#     exit 0.
run_sut --file "$WORK_DIR/528-good-prefix.json" --assume-new-hash bb3b15b9f3db0761 --assume-new-at 2026-09-06T14:00:00Z
assert_eq "0" "$RC" "(k) same hash, past the 24h window -> exit 0"
assert_eq "STALE" "$(get_field "$OUT" DECISION)" "(k) DECISION=STALE"

# (l) Different hash from PRIOR -> CHANGED, comment allowed regardless of
#     window.
run_sut --file "$WORK_DIR/528-good-prefix.json" --assume-new-hash deadbeefdeadbeef --assume-new-at 2026-09-05T14:00:00Z
assert_eq "0" "$RC" "(l) different hash -> exit 0"
assert_eq "CHANGED" "$(get_field "$OUT" DECISION)" "(l) DECISION=CHANGED"

echo ""
echo "--- Usage / environment errors ---"

# (m) Neither --issue nor --file -> usage error, exit 2.
run_sut
assert_eq "2" "$RC" "(m) missing --issue/--file -> exit 2"
assert_contains "$ERR" "required" "(m) stderr explains the usage error"

# (n) Both --issue and --file -> usage error, exit 2.
run_sut --issue 1 --file "$WORK_DIR/empty.json"
assert_eq "2" "$RC" "(n) --issue and --file together -> exit 2"

# (o) Nonexistent --file -> usage error, exit 2.
run_sut --file "$WORK_DIR/does-not-exist.json"
assert_eq "2" "$RC" "(o) missing fixture file -> exit 2"

# (p) `{"comments": [...]}` wrapper shape (gh issue view --json comments) is
#     also accepted, not just a bare top-level array.
cat > "$WORK_DIR/wrapped.json" <<'EOF'
{"comments": [
  {"createdAt":"2026-09-01T00:00:00Z","body":"<!-- curator:dep-recheck:aaaaaaaaaaaaaaaa -->"},
  {"createdAt":"2026-09-01T23:59:59Z","body":"<!-- curator:dep-recheck:aaaaaaaaaaaaaaaa -->"}
]}
EOF
run_sut --file "$WORK_DIR/wrapped.json"
assert_eq "1" "$RC" "(p) wrapped {comments:[...]} shape parses and still flags the violation"

echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
