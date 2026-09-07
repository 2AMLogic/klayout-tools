#!/usr/bin/env bash
# test-curator-dep-recheck-recipe.sh - Regression guard for the exact bash
# recipe curator.md's "Re-check Idempotency" section (#4986) documents for
# computing CONCLUSION_HASH via dep-recheck-fingerprint.sh (#1541).
#
# WHY THIS EXISTS (#1541)
#
#   curator.md's FINGERPRINT snippet is prose embedded in a role prompt —
#   nothing previously executed it. A same-day upstream resync (5cba0c2,
#   2026-09-06) restructured dep-recheck-fingerprint.sh's CLI into
#   subcommands (`dep-recheck`/`operator-premise` + `--number`, replacing the
#   flat `--verdict`/`--issue`/`--blocker`/`--reason-key` interface the doc
#   still described) with nothing to flag the coupling breaking. The
#   documented recipe started exiting 2 on every real invocation, and at
#   least one live Curator pass responded by hand-typing a CONCLUSION_HASH
#   while narrating as if the script had produced it — producing THREE
#   different "canonical" hashes for one unchanged blocker state in issue
#   #527's comment history (see #1541).
#
#   This suite extracts curator.md's ACTUAL documented bash block VERBATIM
#   (not a hand-mirrored copy of it) and executes it against the real,
#   deployed dep-recheck-fingerprint.sh / check-dep-recheck-idempotency.sh
#   scripts (only `gh` is stubbed, for hermeticity — everything else,
#   including the extraction itself, is the live doc + live scripts). A
#   future resync that changes either script's CLI in a way that breaks the
#   documented invocation shape fails THIS test instead of drifting silently
#   into another round of hand-typed hashes.
#
# SCOPE
#
#   Covers the three invocation shapes the recipe documents:
#     (A) mechanical/primary path   — BLOCKERS non-empty (a linked PR exists),
#                                     --verdict always passed but is a no-op
#                                     per dep-recheck-fingerprint.sh's own
#                                     contract in this case.
#     (B) secondary heuristic, bare in-repo ref (e.g. `528`) — BLOCKERS
#                                     empty, BLOCK_REASON resolved live via
#                                     `gh issue view` into the canonical
#                                     "<ref>:<STATE>:<sorted loom:-labels>"
#                                     token and passed via --block-reason.
#     (C) secondary heuristic, cross-repo ref (e.g. `rjwalters/loom#5325`) —
#                                     same shape, resolved via
#                                     `gh issue view --repo`.
#     (D) --verdict clear            — the "clear" half of `--verdict
#                                     blocked|clear`, not just "blocked".
#
# klayout-tools-local suite — not upstream Loom's own harness, so it is not,
# and should not be, added to `.loom/scripts/tests/ci-wired.txt` (that file
# is vendored wholesale from upstream on every resync and is never consulted
# by this repo's own CI — see `.loom/docs/local-test-suite-wiring.md`, which
# lists this suite as one of the ones run manually). Run it manually:
#   ./.loom/scripts/tests/test-curator-dep-recheck-recipe.sh
#
# Usage:
#   ./.loom/scripts/tests/test-curator-dep-recheck-recipe.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"
CURATOR_MD="$REPO_ROOT/.claude/commands/loom/curator.md"

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
        echo "    Both sides were: '$a'"
    fi
}

[[ -f "$CURATOR_MD" ]] || {
    echo -e "${RED}FATAL${NC}: $CURATOR_MD not found"
    exit 2
}
[[ -x "$SCRIPTS_DIR/dep-recheck-fingerprint.sh" ]] || {
    echo -e "${RED}FATAL${NC}: dep-recheck-fingerprint.sh missing or not executable"
    exit 2
}
[[ -x "$SCRIPTS_DIR/check-dep-recheck-idempotency.sh" ]] || {
    echo -e "${RED}FATAL${NC}: check-dep-recheck-idempotency.sh missing or not executable"
    exit 2
}
command -v jq >/dev/null 2>&1 || {
    echo -e "${RED}FATAL${NC}: jq required"
    exit 2
}

echo "Testing curator.md's documented dep-recheck FINGERPRINT recipe..."
echo ""

# --- Extract the documented recipe VERBATIM ---------------------------------
# Anchor on the prose sentence that immediately precedes the fenced block, so
# this stays robust to line-number churn elsewhere in curator.md.
RECIPE_RAW="$(awk '
  found && /^```bash/ { infence = 1; next }
  found && infence && /^```/ { exit }
  found && infence { print }
  /Embed the fingerprint as a marker in every re-check comment you post/ { found = 1 }
' "$CURATOR_MD")"

if [[ -z "$RECIPE_RAW" ]]; then
    echo -e "${RED}FATAL${NC}: could not locate curator.md's FINGERPRINT recipe block — the anchor sentence may have moved; update this test's awk anchor to match" >&2
    exit 2
fi
if ! grep -q 'dep-recheck-fingerprint.sh' <<<"$RECIPE_RAW"; then
    echo -e "${RED}FATAL${NC}: extracted block does not mention dep-recheck-fingerprint.sh — the extraction anchor found the wrong block" >&2
    exit 2
fi

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR" 2>/dev/null || true' EXIT

# --- Stub `gh` ---------------------------------------------------------------
STUB_DIR="$WORK_DIR/bin"
mkdir -p "$STUB_DIR"
FIXTURES_DIR="$WORK_DIR/fixtures"
mkdir -p "$FIXTURES_DIR"

cat >"$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
D="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"

case "${1:-}" in
  issue)
    shift
    sub="$1"; shift
    num=""
    jqexpr=""
    repo=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --json) shift 2 ;;
        --jq) jqexpr="${2:-}"; shift 2 ;;
        --repo) repo="${2:-}"; shift 2 ;;
        *) [[ -z "$num" ]] && num="$1"; shift ;;
      esac
    done
    if [[ "$sub" == "view" ]]; then
      if [[ -n "$repo" ]]; then
        safe_repo="${repo//\//_}"
        f="$D/issue-repo-${safe_repo}-$num.json"
      else
        f="$D/issue-$num.json"
      fi
      [[ -f "$f" ]] || { echo "stub gh: missing $f" >&2; exit 1; }
      if [[ -n "$jqexpr" ]]; then jq -r "$jqexpr" "$f"; else cat "$f"; fi
    else
      echo "stub gh: unhandled issue sub '$sub'" >&2; exit 3
    fi
    ;;
  pr)
    shift
    sub="$1"; shift
    num=""
    jqexpr=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --json) shift 2 ;;
        --jq) jqexpr="${2:-}"; shift 2 ;;
        --repo) shift 2 ;;
        *) [[ -z "$num" ]] && num="$1"; shift ;;
      esac
    done
    if [[ "$sub" == "view" ]]; then
      f="$D/pr-$num.json"
      [[ -f "$f" ]] || { echo "stub gh: missing $f" >&2; exit 1; }
      if [[ -n "$jqexpr" ]]; then jq -r "$jqexpr" "$f"; else cat "$f"; fi
    else
      echo "stub gh: unhandled pr sub '$sub'" >&2; exit 3
    fi
    ;;
  api)
    # check-dep-recheck-idempotency.sh's live path:
    #   gh api repos/{owner}/{repo}/issues/<N>/comments --paginate
    # No prior curator:dep-recheck marker on any of this suite's synthetic
    # issue numbers -> an empty comment history, i.e. DECISION=NONE.
    echo "[]"
    ;;
  *) echo "stub gh: unhandled args: $*" >&2; exit 3 ;;
esac
STUB
chmod +x "$STUB_DIR/gh"
export LOOM_TEST_STUB_DIR="$FIXTURES_DIR"

# run_recipe <issue_number> <blocker_ref> [verdict]
#
# Binds ISSUE_NUMBER/BLOCKER_REF/VERDICT in curator.md's verbatim extracted
# recipe and executes it from the repo root (so its relative
# `./.loom/scripts/...` invocations resolve), with the stub `gh` shadowing
# the real one. Prints the recipe's key variables as KEY=VALUE lines on
# stdout for the caller to inspect; the recipe's own exit code is preserved.
run_recipe() {
    local issue="$1" blocker_ref="$2" verdict="${3:-blocked}"
    local body
    body="$(printf '%s\n' "$RECIPE_RAW" | sed \
        -e "s|^ISSUE_NUMBER=<number>|ISSUE_NUMBER=$issue|" \
        -e "s|^VERDICT=blocked|VERDICT=$verdict|" \
        -e "s|^BLOCKER_REF=.*|BLOCKER_REF=$blocker_ref|")"
    body+=$'\n'
    body+='printf "VERDICT=%s\nBLOCK_REASON=%s\nCONCLUSION_HASH=%s\nDECISION=%s\nDECISION_RC=%s\n" "$VERDICT" "$BLOCK_REASON" "$CONCLUSION_HASH" "$DECISION" "$DECISION_RC"'
    ( cd "$REPO_ROOT" && PATH="$STUB_DIR:$PATH" bash -c "$body" )
}

field() { # <output> <KEY>
    grep -E "^$2=" <<<"$1" | head -n 1 | cut -d= -f2-
}

# --- (A) Mechanical/primary path: BLOCKERS non-empty ------------------------
jq -n '{closedByPullRequestsReferences: [{number: 4743}]}' >"$FIXTURES_DIR/issue-6335.json"
jq -n '{number: 4743, state: "OPEN", labels: [{name: "loom:changes-requested"}], mergeable: "MERGEABLE", mergeStateStatus: "CLEAN"}' \
    >"$FIXTURES_DIR/pr-4743.json"

rc=0
out="$(run_recipe 6335 "")" || rc=$?
assert_eq "0" "$rc" "(A) mechanical/primary path: documented recipe exits 0 against a real linked-PR block"
assert_ne "" "$(field "$out" CONCLUSION_HASH)" "(A) CONCLUSION_HASH is non-empty"
assert_eq "NONE" "$(field "$out" DECISION)" "(A) first-ever check on this synthetic issue -> DECISION=NONE"
assert_eq "0" "$(field "$out" DECISION_RC)" "(A) DECISION mode also exits 0 (safe to comment)"
HASH_A="$(field "$out" CONCLUSION_HASH)"

# --- (B) Secondary heuristic: bare in-repo ref ------------------------------
jq -n '{closedByPullRequestsReferences: []}' >"$FIXTURES_DIR/issue-6336.json"
jq -n '{state: "OPEN", labels: [{name: "loom:blocked"}]}' >"$FIXTURES_DIR/issue-528.json"

rc=0
out="$(run_recipe 6336 528)" || rc=$?
assert_eq "0" "$rc" "(B) secondary heuristic (bare ref #528): documented recipe exits 0"
assert_eq "528:OPEN:loom:blocked" "$(field "$out" BLOCK_REASON)" \
    "(B) BLOCK_REASON is the canonical, whitespace-free live-derived token — never prose"
assert_ne "" "$(field "$out" CONCLUSION_HASH)" "(B) CONCLUSION_HASH is non-empty"
HASH_B="$(field "$out" CONCLUSION_HASH)"

# --- (C) Secondary heuristic: cross-repo ref --------------------------------
jq -n '{closedByPullRequestsReferences: []}' >"$FIXTURES_DIR/issue-6337.json"
jq -n '{state: "OPEN"}' >"$FIXTURES_DIR/issue-repo-rjwalters_loom-5325.json"

rc=0
out="$(run_recipe 6337 "rjwalters/loom#5325")" || rc=$?
assert_eq "0" "$rc" "(C) secondary heuristic (cross-repo ref): documented recipe exits 0"
assert_eq "rjwalters/loom#5325:OPEN:" "$(field "$out" BLOCK_REASON)" \
    "(C) cross-repo BLOCK_REASON is a canonical whitespace-free token (state only)"
HASH_C="$(field "$out" CONCLUSION_HASH)"
assert_ne "$HASH_B" "$HASH_C" "(C) an in-repo ref and a same-numbered cross-repo ref hash distinctly, not collide"

# --- (D) --verdict clear (the other half of blocked|clear) ------------------
jq -n '{closedByPullRequestsReferences: []}' >"$FIXTURES_DIR/issue-6338.json"

rc=0
out="$(run_recipe 6338 "" clear)" || rc=$?
assert_eq "0" "$rc" "(D) --verdict clear: documented recipe exits 0"
assert_eq "clear" "$(field "$out" VERDICT)" "(D) VERDICT propagates through to the script's output"
HASH_D="$(field "$out" CONCLUSION_HASH)"
assert_ne "$HASH_A" "$HASH_D" "(D) blocked vs clear on distinct issues never collide"

# --- Stability: identical inputs -> identical hash across two invocations ---
rc=0
out_repeat="$(run_recipe 6335 "")" || rc=$?
assert_eq "0" "$rc" "(E) repeat invocation of (A) also exits 0"
assert_eq "$HASH_A" "$(field "$out_repeat" CONCLUSION_HASH)" \
    "(E) two independent invocations of the documented recipe against an unchanged state produce a byte-identical hash"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
