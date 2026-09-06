#!/usr/bin/env bash
# test-dep-recheck-fingerprint.sh - Unit tests for dep-recheck-fingerprint.sh,
# the deterministic CONCLUSION_HASH builder for Curator's "Re-check
# Idempotency" rule (#1528).
#
# The defect under test: #1523/#1524 made the *decision* deterministic, but
# left the *input* to that decision (CONCLUSION_HASH) hand-rolled, with a
# free-text BLOCK_REASON. Identical dependency state therefore hashed
# differently across passes whenever the wording drifted, a differing hash is
# classified CHANGED, and CHANGED always permits a comment. So the whole
# guard degraded to "comment on every pass" — three distinct hashes for one
# unchanged blocker state on #528 on 2026-09-06 alone.
#
# The two properties that matter are opposite-signed, and both are asserted
# here:
#   STABILITY   two independent invocations over an unchanged blocker state
#               produce a byte-identical hash (scenarios a/e/f)
#   SENSITIVITY the hash still moves when the REAL state moves — a label
#               appears, the blocker closes, the verdict flips (c/d/i)
# A fix that only had stability would be worse than the bug: it would suppress
# genuine state changes.
#
# Black-box: the SUT is a full CLI, driven as a subprocess. `gh` is stubbed on
# PATH (canned JSON per ref, with the SUT's own `--jq` expression applied by
# real jq inside the stub) so the SUT's real forge-parsing code path runs
# against realistic payloads and no network call is ever made. Scenario (b)
# pins the fixture to a value verified against the LIVE forge and quoted
# independently in issue #1528's body, so the fixture cannot silently drift
# into agreeing only with itself.
#
# Usage:
#   ./.loom/scripts/tests/test-dep-recheck-fingerprint.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
SUT="$SCRIPTS_DIR/dep-recheck-fingerprint.sh"

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

assert_ne() {
    local a="$1" b="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$a" != "$b" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Both were: '$a' (expected them to differ)"
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
        echo "    Expected to contain: '$needle'"
        echo "    Actual: '$haystack'"
    fi
}

if [[ ! -x "$SUT" ]]; then
    echo -e "${RED}FATAL${NC}: SUT not found or not executable: $SUT"
    exit 1
fi
command -v jq >/dev/null 2>&1 || { echo -e "${RED}FATAL${NC}: jq required"; exit 1; }

TMP_ROOT="$(mktemp -d)"
# shellcheck disable=SC2329  # invoked indirectly by the EXIT trap below
cleanup() { rm -rf "$TMP_ROOT"; }
trap cleanup EXIT

STUB_DIR="$TMP_ROOT/stub"
FIXTURES="$TMP_ROOT/fixtures"
mkdir -p "$STUB_DIR" "$FIXTURES"

# --- gh stub -------------------------------------------------------------------
# Serves canned JSON per ref and applies the SUT's own --jq expression with
# real jq, so the SUT's real parsing/formatting path is exercised end to end.
# Unknown refs exit non-zero with empty stdout, which is exactly how real `gh`
# behaves for a ref that does not resolve.
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
DIR="$LOOM_FP_STUB_DIR"

kind="${1:-}"; verb="${2:-}"; shift 2 || true

num=""
repo=""
jq_expr=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) repo="$2"; shift 2 ;;
    --json) shift 2 ;;
    --jq) jq_expr="$2"; shift 2 ;;
    *) [[ -z "$num" ]] && num="$1"; shift ;;
  esac
done

key="$num"
[[ -n "$repo" ]] && key="$(printf '%s' "$repo" | tr '/' '_')#$num"

case "$kind:$verb" in
  issue:view) canned="$DIR/issue-$key.json" ;;
  pr:view)    canned="$DIR/pr-$key.json" ;;
  *) echo "stub gh: unhandled $kind $verb" >&2; exit 3 ;;
esac

[[ -f "$canned" ]] || exit 1
if [[ -n "$jq_expr" ]]; then
  jq -r "$jq_expr" < "$canned"
else
  cat "$canned"
fi
STUB
chmod +x "$STUB_DIR/gh"
export LOOM_FP_STUB_DIR="$FIXTURES"
export PATH="$STUB_DIR:$PATH"

# --- fixture helpers -----------------------------------------------------------
labels_json() {
    local out="" l
    for l in "$@"; do
        [[ -n "$out" ]] && out="$out,"
        out="$out{\"name\":\"$l\"}"
    done
    printf '[%s]' "$out"
}

write_issue() {
    # write_issue <key> <state> [label ...]
    local key="$1" state="$2"; shift 2
    printf '{"number":%s,"state":"%s","labels":%s,"closedByPullRequestsReferences":[]}\n' \
        "${key##*#}" "$state" "$(labels_json "$@")" > "$FIXTURES/issue-$key.json"
}

write_issue_with_prs() {
    # write_issue_with_prs <key> <state> <pr#>[,<pr#>...]
    local key="$1" state="$2" prs="$3" refs="" p
    for p in ${prs//,/ }; do
        [[ -n "$refs" ]] && refs="$refs,"
        refs="$refs{\"number\":$p}"
    done
    printf '{"number":%s,"state":"%s","labels":[],"closedByPullRequestsReferences":[%s]}\n' \
        "${key##*#}" "$state" "$refs" > "$FIXTURES/issue-$key.json"
}

write_pr() {
    # write_pr <key> <state> [label ...]
    local key="$1" state="$2"; shift 2
    printf '{"number":%s,"state":"%s","labels":%s}\n' \
        "${key##*#}" "$state" "$(labels_json "$@")" > "$FIXTURES/pr-$key.json"
}

hash_of() { printf '%s\n' "$1" | sed -n 's/^CONCLUSION_HASH=//p'; }

echo "Testing dep-recheck-fingerprint.sh"
echo "=================================="
echo

# --- (a) determinism across two independent invocations ------------------------
echo "(a) Two independent passes, unchanged blocker state -> identical hash"
write_issue 378 OPEN loom:architect loom:epic-phase loom:operator-only
write_issue 528 OPEN

OUT1="$("$SUT" --verdict blocked --issue 528 --blocker 378)"
RC1=$?
OUT2="$("$SUT" --verdict blocked --issue 528 --blocker 378)"
assert_eq "0" "$RC1" "(a) exit 0"
assert_eq "$OUT1" "$OUT2" "(a) full output byte-identical across two invocations"
H_CANONICAL="$(hash_of "$OUT1")"
assert_contains "$OUT1" "REASON=378:OPEN:loom:architect,loom:epic-phase,loom:operator-only" \
    "(a) BLOCK_REASON is the canonical <ref>:<STATE>:<sorted loom:-labels> triple"

# --- (b) the canonical hash matches the independently-verified live value ------
echo
echo "(b) Canonical hash equals the value verified against the live forge"
# #1528 quotes 7004d3c3258ab254 for #528's canonical state, computed by hand
# from the real forge BEFORE this script existed. Pinning it here means the
# fixture agrees with reality, not merely with itself.
assert_eq "7004d3c3258ab254" "$H_CANONICAL" \
    "(b) matches #1528's independently hand-computed 7004d3c3258ab254"

# --- (c) sensitivity: a loom: label appears on the blocker ---------------------
echo
echo "(c) Genuine state change (label added to blocker) -> hash MUST change"
write_issue 378 OPEN loom:architect loom:epic-phase loom:operator-only loom:urgent
H_LABEL_ADDED="$(hash_of "$("$SUT" --verdict blocked --issue 528 --blocker 378)")"
assert_ne "$H_CANONICAL" "$H_LABEL_ADDED" "(c) added loom:urgent changes the hash"

# --- (d) sensitivity: the blocker closes ---------------------------------------
echo
echo "(d) Genuine state change (blocker closes) -> hash MUST change"
write_issue 378 CLOSED loom:architect loom:epic-phase loom:operator-only
H_CLOSED="$(hash_of "$("$SUT" --verdict blocked --issue 528 --blocker 378)")"
assert_ne "$H_CANONICAL" "$H_CLOSED" "(d) OPEN -> CLOSED changes the hash"

# --- (e) stability: label ORDER churn from the API is not a change -------------
echo
echo "(e) API label-order churn -> hash MUST NOT change"
write_issue 378 OPEN loom:operator-only loom:architect loom:epic-phase
H_REORDERED="$(hash_of "$("$SUT" --verdict blocked --issue 528 --blocker 378)")"
assert_eq "$H_CANONICAL" "$H_REORDERED" "(e) labels are sorted before hashing"

# --- (f) stability: blocker-ref argument order is not a change -----------------
echo
echo "(f) --blocker argument order -> hash MUST NOT change"
write_issue 111 OPEN loom:blocked
write_issue 222 OPEN loom:operator-only
H_AB="$(hash_of "$("$SUT" --verdict blocked --no-linked-prs --blocker 111 --blocker 222)")"
H_BA="$(hash_of "$("$SUT" --verdict blocked --no-linked-prs --blocker 222 --blocker 111)")"
assert_eq "$H_AB" "$H_BA" "(f) reason lines are sorted before hashing"

# --- (g) the anti-prose guard --------------------------------------------------
echo
echo "(g) A prose --reason-key is REJECTED (this is the #1528 defect, structurally)"
ERR="$("$SUT" --verdict blocked --no-linked-prs \
    --reason-key 'still blocked pending the design epic' 2>&1 >/dev/null)"
RC=$?
assert_eq "2" "$RC" "(g) prose reason-key -> exit 2"
assert_contains "$ERR" "no whitespace/prose" "(g) stderr names the reason"

echo
echo "(g2) A canonical token reason-key is accepted"
OUT="$("$SUT" --verdict blocked --no-linked-prs --reason-key 'release:rjwalters/loom:>v0.18.0')"
assert_eq "0" "$?" "(g2) canonical token -> exit 0"
assert_contains "$OUT" "REASON=release:rjwalters/loom:>v0.18.0" "(g2) token passes through verbatim"

# --- (h) BLOCK_REASON is secondary-heuristic-only ------------------------------
echo
echo "(h) --blocker alongside a primary-check blocker -> usage error"
write_issue_with_prs 900 OPEN 901
write_pr 901 OPEN loom:changes-requested
ERR="$("$SUT" --verdict blocked --issue 900 --blocker 378 2>&1 >/dev/null)"
RC=$?
assert_eq "2" "$RC" "(h) exit 2 when the primary check already supplied BLOCKERS"
assert_contains "$ERR" "SECONDARY heuristic only" "(h) stderr explains the invariant"

echo
echo "(h2) Primary-check blockers alone produce the linked-PR fingerprint"
OUT="$("$SUT" --verdict blocked --issue 900)"
assert_eq "0" "$?" "(h2) exit 0"
assert_contains "$OUT" "BLOCKER=901:OPEN:loom:changes-requested" "(h2) canonical linked-PR line"

echo
echo "(h3) A label change on the linked PR moves the hash"
H_PR_BEFORE="$(hash_of "$OUT")"
write_pr 901 OPEN loom:blocked loom:changes-requested
H_PR_AFTER="$(hash_of "$("$SUT" --verdict blocked --issue 900)")"
assert_ne "$H_PR_BEFORE" "$H_PR_AFTER" "(h3) linked-PR labels are part of the fingerprint"

# --- (i) verdict flip ----------------------------------------------------------
echo
echo "(i) blocked -> clear is always a changed conclusion"
H_BLOCKED_EMPTY="$(hash_of "$("$SUT" --verdict blocked --no-linked-prs)")"
H_CLEAR_EMPTY="$(hash_of "$("$SUT" --verdict clear --no-linked-prs)")"
assert_ne "$H_BLOCKED_EMPTY" "$H_CLEAR_EMPTY" "(i) the verdict prefix prevents collision"

# --- (j) verdict/blocker-set consistency ---------------------------------------
echo
echo "(j) --verdict clear with a non-empty blocker set -> usage error"
write_issue 378 OPEN loom:architect
ERR="$("$SUT" --verdict clear --no-linked-prs --blocker 378 2>&1 >/dev/null)"
RC=$?
assert_eq "2" "$RC" "(j) exit 2"
assert_contains "$ERR" "inconsistent" "(j) stderr explains the inconsistency"

# --- (k) an unresolvable blocker must NOT silently yield a hash ----------------
echo
echo "(k) Unresolvable blocker ref -> exit 2, no fingerprint emitted"
OUT="$("$SUT" --verdict blocked --no-linked-prs --blocker 424242 2>/dev/null)"
RC=$?
assert_eq "2" "$RC" "(k) exit 2 rather than hashing an empty reason"
assert_eq "" "$(hash_of "$OUT")" "(k) no CONCLUSION_HASH on the failure path"

echo
echo "(k2) A malformed blocker ref -> exit 2"
ERR="$("$SUT" --verdict blocked --no-linked-prs --blocker 'not-a-ref' 2>&1 >/dev/null)"
RC=$?
assert_eq "2" "$RC" "(k2) exit 2"
assert_contains "$ERR" "<owner>/<repo>#<number>" "(k2) stderr shows the accepted shapes"

# --- (l) usage errors ----------------------------------------------------------
echo
echo "(l) Usage validation"
"$SUT" --no-linked-prs >/dev/null 2>&1
assert_eq "2" "$?" "(l) missing --verdict -> exit 2"
"$SUT" --verdict blocked >/dev/null 2>&1
assert_eq "2" "$?" "(l) no blocker source -> exit 2"
"$SUT" --verdict blocked --issue 528 --no-linked-prs >/dev/null 2>&1
assert_eq "2" "$?" "(l) two blocker sources -> exit 2"
"$SUT" --verdict maybe --no-linked-prs >/dev/null 2>&1
assert_eq "2" "$?" "(l) invalid verdict -> exit 2"
"$SUT" --verdict blocked --issue abc >/dev/null 2>&1
assert_eq "2" "$?" "(l) non-numeric --issue -> exit 2"
"$SUT" --verdict blocked --bogus >/dev/null 2>&1
assert_eq "2" "$?" "(l) unknown flag -> exit 2"

# --- (m) --hash-only / --blockers-file -----------------------------------------
echo
echo "(m) --hash-only and --blockers-file"
write_issue 378 OPEN loom:architect loom:epic-phase loom:operator-only
HASH_ONLY="$("$SUT" --verdict blocked --issue 528 --blocker 378 --hash-only)"
assert_eq "7004d3c3258ab254" "$HASH_ONLY" "(m) --hash-only prints just the hash"

printf '901:OPEN:loom:changes-requested\n' > "$TMP_ROOT/blockers.txt"
OUT="$("$SUT" --verdict blocked --blockers-file "$TMP_ROOT/blockers.txt")"
assert_eq "0" "$?" "(m) --blockers-file exit 0"
assert_contains "$OUT" "BLOCKER=901:OPEN:loom:changes-requested" "(m) --blockers-file line preserved"

printf '222:OPEN:loom:b\n111:OPEN:loom:a\n' > "$TMP_ROOT/unsorted.txt"
printf '111:OPEN:loom:a\n222:OPEN:loom:b\n' > "$TMP_ROOT/sorted.txt"
H_U="$("$SUT" --verdict blocked --blockers-file "$TMP_ROOT/unsorted.txt" --hash-only)"
H_S="$("$SUT" --verdict blocked --blockers-file "$TMP_ROOT/sorted.txt" --hash-only)"
assert_eq "$H_S" "$H_U" "(m) BLOCKERS file order churn does not change the hash"

"$SUT" --verdict blocked --blockers-file "$TMP_ROOT/nope.txt" >/dev/null 2>&1
assert_eq "2" "$?" "(m) missing --blockers-file -> exit 2"

# --- (n) the fingerprint feeds the idempotency guard unchanged -----------------
echo
echo "(n) RECHECK_MARKER is the exact shape check-dep-recheck-idempotency.sh parses"
OUT="$("$SUT" --verdict blocked --issue 528 --blocker 378)"
MARKER="$(printf '%s\n' "$OUT" | sed -n 's/^RECHECK_MARKER=//p')"
assert_eq "<!-- curator:dep-recheck:7004d3c3258ab254 -->" "$MARKER" "(n) marker shape"
GUARD="$SCRIPTS_DIR/check-dep-recheck-idempotency.sh"
if [[ -x "$GUARD" ]]; then
    printf '[{"created_at":"2026-09-06T00:00:00Z","body":"still blocked %s"}]\n' "$MARKER" \
        > "$TMP_ROOT/history.json"
    GOUT="$("$GUARD" --file "$TMP_ROOT/history.json" \
        --assume-new-hash "7004d3c3258ab254" --assume-new-at "2026-09-06T04:00:00Z")"
    GRC=$?
    assert_eq "20" "$GRC" "(n) guard reads the marker back and says SKIP"
    assert_contains "$GOUT" "DECISION=SKIP" "(n) DECISION=SKIP"
else
    echo "  SKIP: check-dep-recheck-idempotency.sh not executable"
fi

echo
echo "=================================="
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}$TESTS_PASSED${NC}"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "Tests failed: ${RED}$TESTS_FAILED${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
exit 0
