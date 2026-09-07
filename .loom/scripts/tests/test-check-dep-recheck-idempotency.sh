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
echo "--- CONCLUSION_HASH stability (#1528) ---"
#
# This SUT is a pure function of the hash history it is handed, so it can only
# be as good as the hashes its callers compute. #1528 reported that the
# duplicate heartbeats survived #1523/#1524 because the CALLER's
# CONCLUSION_HASH was unstable: curator.md built BLOCK_REASON from free-text
# prose, so two passes describing the IDENTICAL blocker state in different
# words produced different hashes -- and a different hash is CHANGED, which
# always permits a comment regardless of the staleness window.
#
# (q*) are the negative fixtures: they reproduce that failure mode against
# this SUT, proving the duplicate is permitted with no bug in this script.
# (r*)/(s*) are the positive fixtures: the same scenarios re-run with hashes
# produced by dep-recheck-fingerprint.sh, which builds every component from
# machine-checkable state.

FP="$SCRIPTS_DIR/dep-recheck-fingerprint.sh"

# The legacy, now-removed caller formula, kept ONLY as a fixture generator so
# the failure mode stays reproducible after the real code path is gone.
legacy_hash() {
    # legacy_hash <verdict> <blockers> <block_reason-as-prose>
    printf '%s\n%s\n%s' "$1" "$2" "$3" | {
        if command -v shasum >/dev/null 2>&1; then shasum -a 256
        else sha256sum; fi
    } | awk '{print substr($1, 1, 16)}'
}

# (q0) The live-reported drift, straight from #528's real history: the
#      2026-09-05T13:42:50Z heartbeat (bb3b15b9f3db0761) is followed 11.6h
#      later -- well inside the 24h window -- by a DIFFERENT hash for the same
#      unchanged blocker state. The SUT correctly says CHANGED; that is the
#      point. The duplicate was licensed upstream of here.
cat > "$WORK_DIR/1528-drift-prior.json" <<'EOF'
[
  {"created_at":"2026-09-05T13:42:50Z","body":"still blocked <!-- curator:dep-recheck:bb3b15b9f3db0761 -->"}
]
EOF
run_sut --file "$WORK_DIR/1528-drift-prior.json" \
    --assume-new-hash "5342934786687480" --assume-new-at "2026-09-06T01:21:21Z"
assert_eq "0" "$RC" "(q0) drifted hash 11.6h later -> exit 0 (comment permitted)"
assert_eq "CHANGED" "$(get_field "$OUT" DECISION)" "(q0) DECISION=CHANGED — the window never gets consulted"

# (q1) Same thing, mechanically: two prose wordings of ONE unchanged state
#      (#378 still open, still loom:operator-only, no linked PR) hash
#      differently under the removed free-text formula.
PROSE_A="$(legacy_hash blocked "" "blocked on #378 (design epic still unfiled, operator-only)")"
PROSE_B="$(legacy_hash blocked "" "still blocked: #378 remains open and operator-only, no design epic yet")"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$PROSE_A" != "$PROSE_B" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: (q1) free-text BLOCK_REASON: two wordings of one state hash differently"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: (q1) expected the prose wordings to hash differently"
fi

printf '[{"created_at":"2026-09-06T08:00:00Z","body":"still blocked <!-- curator:dep-recheck:%s -->"}]\n' \
    "$PROSE_A" > "$WORK_DIR/1528-prose-prior.json"
run_sut --file "$WORK_DIR/1528-prose-prior.json" \
    --assume-new-hash "$PROSE_B" --assume-new-at "2026-09-06T12:00:00Z"
assert_eq "0" "$RC" "(q2) prose-drifted rerun 4h later -> exit 0 (duplicate permitted)"
assert_eq "CHANGED" "$(get_field "$OUT" DECISION)" "(q2) DECISION=CHANGED on an unchanged state — the #1528 defect"

if [[ -x "$FP" ]]; then
    # (r) The fix: the same unchanged state, fingerprinted canonically by two
    #     independent invocations, is now SKIPped inside the window.
    #
    # canon_hash invokes the CURRENT `dep-recheck --stdin` subcommand shape
    # (the flat `--verdict/--no-linked-prs/--reason-key/--hash-only` interface
    # this suite used to call was replaced by #7281's subcommand restructuring
    # — see test-curator-dep-recheck-recipe.sh's header for the incident this
    # caused when a doc/recipe kept the old shape after a resync).
    canon_hash() { # canon_hash <verdict> <block_reason>
        echo '{"prs":[]}' | "$FP" dep-recheck --stdin --verdict "$1" --block-reason "$2" \
            | sed -n 's/^CONCLUSION_HASH=//p'
    }
    CANON_STATE='378:OPEN:loom:architect,loom:epic-phase,loom:operator-only'
    CANON_A="$(canon_hash blocked "$CANON_STATE")"
    CANON_B="$(canon_hash blocked "$CANON_STATE")"
    assert_eq "$CANON_A" "$CANON_B" "(r1) two independent canonical passes -> identical CONCLUSION_HASH"
    assert_eq "934b6f057745738b" "$CANON_A" "(r1) pinned CONCLUSION_HASH for this state under the current dep-recheck-fingerprint.sh formula"

    printf '[{"created_at":"2026-09-06T08:00:00Z","body":"still blocked <!-- curator:dep-recheck:%s -->"}]\n' \
        "$CANON_A" > "$WORK_DIR/1528-canonical-prior.json"
    run_sut --file "$WORK_DIR/1528-canonical-prior.json" \
        --assume-new-hash "$CANON_B" --assume-new-at "2026-09-06T12:00:00Z"
    assert_eq "20" "$RC" "(r2) canonical rerun 4h later -> exit 20 (SKIP), the duplicate is suppressed"
    assert_eq "SKIP" "$(get_field "$OUT" DECISION)" "(r2) DECISION=SKIP"
    assert_eq "2026-09-06T08:00:00Z" "$(get_field "$OUT" PRIOR_AT)" "(r2) PRIOR_AT is the true immediately-preceding comment"

    # (r3) ...and the 9-minute regap from the incident report is suppressed too.
    run_sut --file "$WORK_DIR/1528-canonical-prior.json" \
        --assume-new-hash "$CANON_B" --assume-new-at "2026-09-06T08:09:00Z"
    assert_eq "20" "$RC" "(r3) canonical rerun 9 minutes later -> SKIP"

    # (r4) Past the window, the heartbeat still fires — suppression is not silence.
    run_sut --file "$WORK_DIR/1528-canonical-prior.json" \
        --assume-new-hash "$CANON_B" --assume-new-at "2026-09-07T09:00:00Z"
    assert_eq "0" "$RC" "(r4) canonical rerun past 24h -> exit 0"
    assert_eq "STALE" "$(get_field "$OUT" DECISION)" "(r4) DECISION=STALE (heartbeat still posts)"

    # (s) Sensitivity: the canonicalization must NOT swallow a real change.
    CANON_CHANGED="$(canon_hash blocked "378:OPEN:loom:architect,loom:epic-phase,loom:operator-only,loom:urgent")"
    run_sut --file "$WORK_DIR/1528-canonical-prior.json" \
        --assume-new-hash "$CANON_CHANGED" --assume-new-at "2026-09-06T12:00:00Z"
    assert_eq "0" "$RC" "(s1) a real label change 4h later -> exit 0"
    assert_eq "CHANGED" "$(get_field "$OUT" DECISION)" "(s1) DECISION=CHANGED — genuine changes are still reported"

    CANON_CLOSED="$(canon_hash blocked "378:CLOSED:loom:architect,loom:epic-phase,loom:operator-only")"
    run_sut --file "$WORK_DIR/1528-canonical-prior.json" \
        --assume-new-hash "$CANON_CLOSED" --assume-new-at "2026-09-06T09:00:00Z"
    assert_eq "CHANGED" "$(get_field "$OUT" DECISION)" "(s2) blocker closing 1h later is still CHANGED"

    CANON_CLEAR="$(canon_hash clear "")"
    run_sut --file "$WORK_DIR/1528-canonical-prior.json" \
        --assume-new-hash "$CANON_CLEAR" --assume-new-at "2026-09-06T09:00:00Z"
    assert_eq "CHANGED" "$(get_field "$OUT" DECISION)" "(s3) blocked -> clear 1h later is still CHANGED"
else
    echo "  SKIP: dep-recheck-fingerprint.sh not executable — canonical fixtures skipped"
fi

echo ""
echo "--- Version guard (#1544) ---"

# (t) A prior marker with NO version tag (v0 sentinel) and a candidate hash
#     computed under the current formula (v1) always overrides via the
#     versioned marker form when posted -- but the DECISION here must be
#     NONE regardless of whether the hash also happens to differ, since the
#     comparison is not even reached once versions mismatch.
cat > "$WORK_DIR/legacy-unversioned.json" <<'EOF'
[
  {"created_at":"2026-09-06T08:00:00Z","body":"still blocked <!-- curator:dep-recheck:934b6f057745738b -->"}
]
EOF
run_sut --file "$WORK_DIR/legacy-unversioned.json" \
    --assume-new-hash "934b6f057745738b" --assume-new-version "v1" --assume-new-at "2026-09-06T09:00:00Z"
assert_eq "0" "$RC" "(t1) v0 prior vs v1 candidate, SAME hash, 1h later -> exit 0 (never SKIP across a version bump)"
assert_eq "NONE" "$(get_field "$OUT" DECISION)" "(t1) DECISION=NONE, not SKIP — a version mismatch is 'first check under this formula', even with an identical hash"
assert_eq "v0" "$(get_field "$OUT" PRIOR_VERSION)" "(t1) PRIOR_VERSION reports the legacy v0 sentinel"

# (t2) Same scenario but with a DIFFERENT hash too -- must still be NONE
#      (not CHANGED): the version mismatch alone determines the decision.
run_sut --file "$WORK_DIR/legacy-unversioned.json" \
    --assume-new-hash "deadbeefdeadbeef" --assume-new-version "v1" --assume-new-at "2026-09-06T09:00:00Z"
assert_eq "0" "$RC" "(t2) v0 prior vs v1 candidate, DIFFERENT hash -> exit 0"
assert_eq "NONE" "$(get_field "$OUT" DECISION)" "(t2) DECISION=NONE, not CHANGED — version mismatch is checked before the hash comparison"

# (t3) Two versioned (v1) markers, same version, same hash, inside the
#      window -> ordinary SKIP still applies once versions agree.
cat > "$WORK_DIR/versioned-prior.json" <<'EOF'
[
  {"created_at":"2026-09-06T08:00:00Z","body":"still blocked <!-- curator:dep-recheck:v1:934b6f057745738b -->"}
]
EOF
run_sut --file "$WORK_DIR/versioned-prior.json" \
    --assume-new-hash "934b6f057745738b" --assume-new-version "v1" --assume-new-at "2026-09-06T09:00:00Z"
assert_eq "20" "$RC" "(t3) matching v1 versions, same hash, 1h later -> SKIP as before"
assert_eq "SKIP" "$(get_field "$OUT" DECISION)" "(t3) DECISION=SKIP once versions agree"
assert_eq "v1" "$(get_field "$OUT" PRIOR_VERSION)" "(t3) PRIOR_VERSION echoes the parsed v1 marker"

# (t4) A v1 prior vs a v2 candidate (the NEXT future formula bump) is also
#      NONE -- the rule is symmetric, not just "v0 vs v1".
run_sut --file "$WORK_DIR/versioned-prior.json" \
    --assume-new-hash "934b6f057745738b" --assume-new-version "v2" --assume-new-at "2026-09-06T09:00:00Z"
assert_eq "0" "$RC" "(t4) v1 prior vs v2 candidate -> exit 0"
assert_eq "NONE" "$(get_field "$OUT" DECISION)" "(t4) DECISION=NONE — the version guard is symmetric across any two differing versions, not v0-specific"

# (t5) Backward compatibility: omitting --assume-new-version entirely
#      defaults the candidate to v0, matching the legacy prior's v0 --
#      preserving the exact pre-#1544 hash-only comparison for callers that
#      have not yet been updated to pass the new flag.
run_sut --file "$WORK_DIR/legacy-unversioned.json" \
    --assume-new-hash "934b6f057745738b" --assume-new-at "2026-09-06T09:00:00Z"
assert_eq "20" "$RC" "(t5) no --assume-new-version given, legacy v0 prior, same hash, 1h later -> SKIP (unchanged pre-#1544 behavior)"
assert_eq "SKIP" "$(get_field "$OUT" DECISION)" "(t5) DECISION=SKIP — omitting the flag does not spuriously trigger the version guard"

echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
