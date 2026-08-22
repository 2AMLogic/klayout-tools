#!/usr/bin/env bash
# test-merge-pr-app-permission-fallback.sh - Unit tests for the fresh-token
# retry on merge-pr.sh's merge/auto-merge write paths (#1293).
#
# Incident: during a `/loom:sweep 920` run, PR #1288's terminal Merge step hit
# a confirmed App-installation permission-scope 403
# ("Resource not accessible by integration") on TWO independent write paths:
#   1. `./.loom/scripts/merge-pr.sh 1288 --auto` -> the native `loom-daemon
#      forge auto-merge` call (enablePullRequestAutoMerge GraphQL mutation).
#   2. `./.loom/scripts/merge-pr.sh 1288` (immediate REST merge) -> the
#      synchronous `PUT .../pulls/N/merge` REST call.
# Both failed identically. A THIRD invocation succeeded immediately once a
# force-minted fresh installation token was exported as GH_TOKEN by hand --
# exactly the escalation `forge_gh_perm_safe()` already automates for
# create-pr.sh and the comment/label helpers (#6074), but which neither
# forge_merge_pr() nor forge_auto_merge() (lib/forge-helpers.sh) nor the
# native loom-daemon call site in merge-pr.sh previously used.
#
# This file tests:
#   1. forge_merge_pr() (the synchronous REST merge path): an integration-403
#      on the first attempt recovers via a freshly-minted installation token,
#      routing through forge_gh_perm_safe -- both with and without the
#      optional expected-head-sha precondition.
#   2. forge_auto_merge() (the shell auto-merge path): BOTH its node_id
#      lookup (`gh api .../pulls/N --jq .node_id`) and its GraphQL mutation
#      (`gh api graphql ...`) independently recover from an integration-403
#      the same way.
#   3. Source wiring: forge_merge_pr()/forge_auto_merge() route their `gh`
#      calls through forge_gh_perm_safe (not a bare `gh api` call), and
#      merge-pr.sh's native `loom-daemon forge auto-merge` call site detects
#      an integration-403 via is_app_permission_error() and falls through to
#      the (now-fixed) shell forge_auto_merge path instead of just retrying
#      the native binary call again.
#   4. Regression: a non-permission failure (e.g. "Base branch was modified",
#      a genuine 404) still propagates unretried through both functions --
#      the escalation is additive, not a blanket retry-on-any-error.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-app-permission-fallback.sh

# SC2034: FORGE_TYPE (read by the sourced forge_merge_pr/forge_auto_merge) is
# only consumed by code shellcheck can't see is a reader.
# shellcheck disable=SC2034

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"
FORGE_HELPERS_SRC="$HELPERS_DIR/lib/forge-helpers.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $1"
}

fail() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $1"
}

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    if [[ "$expected" == "$actual" ]]; then
        pass "$msg"
    else
        fail "$msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        pass "$msg"
    else
        fail "$msg"
        echo "    Expected to contain: '$needle'"
        echo "    Actual:              '$haystack'"
    fi
}

# shellcheck source=../lib/forge-helpers.sh
source "$FORGE_HELPERS_SRC"

FORGE_TYPE="github"

STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR"' EXIT

MINT_LOG="$STUB_DIR/mint.log"
MINT_MODE_FILE="$STUB_DIR/mint-mode.txt"
export STUB_DIR MINT_LOG MINT_MODE_FILE

# A `github-app-token.sh` stub speaking the real JSON envelope (mirrors
# test-app-permission-fallback.sh's fixture).
cat > "$STUB_DIR/github-app-token.sh" <<'MINT'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$MINT_LOG"
mode="$(cat "$MINT_MODE_FILE" 2>/dev/null || echo ok)"
if [[ "$mode" == "not-configured" ]]; then
  echo '{"status":"not_configured","message":"github app not configured"}'
  exit 0
fi
echo '{"status":"ok","token":"ghs_fresh","installation_id":"1","app_id":"2","expires_at":"2099-01-01T00:00:00Z"}'
MINT
chmod +x "$STUB_DIR/github-app-token.sh"

# A git repo with an origin remote, so _forge_nwo_from_remote resolves
# without any API call.
FAKE_REPO="$STUB_DIR/repo"
mkdir -p "$FAKE_REPO"
git -C "$FAKE_REPO" init -q
git -C "$FAKE_REPO" remote add origin "https://github.com/owner/repo.git"
git -C "$FAKE_REPO" -c user.name=t -c user.email=t@t commit -q --allow-empty -m init

# --- gh stub: routes by call SHAPE (merge / node_id lookup / graphql
# mutation) so forge_merge_pr's and forge_auto_merge's TWO separate gh calls
# can be independently driven through their own mode files. ---
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
cred="ambient"
[[ -n "${GH_TOKEN:-}" ]] && cred="token:${GH_TOKEN}"
[[ -z "${GH_TOKEN:-}" && -z "${GH_CONFIG_DIR:-}" ]] && cred="personal-ambient"

kind="other"
if [[ "$1" == "api" && "$2" == "graphql" ]]; then
  kind="graphql"
elif [[ "$1" == "api" && "$*" == *"/merge"* ]]; then
  kind="merge"
elif [[ "$1" == "api" && "$*" == *"--jq"* ]]; then
  kind="node_id"
fi

mode_file="$STUB_DIR/mode-$kind.txt"
mode="$(cat "$mode_file" 2>/dev/null || echo ok)"
log_file="$STUB_DIR/attempts-$kind.log"
printf '%s | %s\n' "$cred" "$*" >> "$log_file"
attempts=$(wc -l < "$log_file" | tr -d ' ')

_succeed() {
  case "$kind" in
    node_id)  echo "PR_NODE_ID_XYZ" ;;
    graphql)  echo '{"data":{"enablePullRequestAutoMerge":{"pullRequest":{"number":1}}}}' ;;
    merge)    echo '{"merged":true,"number":1}' ;;
    *)        echo '{}' ;;
  esac
}

case "$mode" in
  ok)
    _succeed
    exit 0
    ;;
  perm403-once)
    if [[ "$attempts" == "1" ]]; then
      echo "HTTP 403: Resource not accessible by integration" >&2
      exit 1
    fi
    _succeed
    exit 0
    ;;
  perm403)
    echo "HTTP 403: Resource not accessible by integration" >&2
    exit 1
    ;;
  other-error)
    echo "HTTP 404: Not Found" >&2
    exit 1
    ;;
  base-modified)
    echo "gh: Base branch was modified. Review and try the merge again." >&2
    exit 1
    ;;
esac
STUB
chmod +x "$STUB_DIR/gh"

_reset_stubs() {
    : > "$MINT_LOG"
    echo "ok" > "$MINT_MODE_FILE"
    rm -f "$STUB_DIR"/mode-*.txt "$STUB_DIR"/attempts-*.log
}

_set_mode() {
    local kind="$1" mode="$2"
    echo "$mode" > "$STUB_DIR/mode-$kind.txt"
}

_attempts() {
    local kind="$1"
    wc -l < "$STUB_DIR/attempts-$kind.log" 2>/dev/null | tr -d ' ' || echo 0
}

_run_in_fake_repo() {
    (
        cd "$FAKE_REPO"
        PATH="$STUB_DIR:$PATH" \
        LOOM_GITHUB_APP_SCRIPT="$STUB_DIR/github-app-token.sh" \
            "$@"
    )
}

# ============================================================================
# 1. forge_merge_pr(): synchronous REST merge path
# ============================================================================
echo "Testing forge_merge_pr() App-permission-window 403 retry..."

_reset_stubs
_set_mode merge perm403-once
rc=0
out="$(_run_in_fake_repo forge_merge_pr "owner/repo" "1288" "deadbeef" 2>/dev/null)" || rc=$?
assert_eq "0" "$rc" \
    "forge_merge_pr: an integration-403 on the merge PUT recovers via a fresh installation token"
assert_contains "$out" '"merged":true' \
    "forge_merge_pr: the escalated attempt's response is returned"
assert_contains "$(cat "$MINT_LOG")" "get-token --force" \
    "forge_merge_pr: the recovery force-mints a fresh installation token (bypasses the ~1h cache)"
assert_eq "2" "$(_attempts merge)" \
    "forge_merge_pr: exactly one retry after the initial 403 (ambient, then fresh token)"

# Without an expected_head_sha (backward compatible call shape).
_reset_stubs
_set_mode merge perm403-once
rc=0
_run_in_fake_repo forge_merge_pr "owner/repo" "1288" >/dev/null 2>&1 || rc=$?
assert_eq "0" "$rc" \
    "forge_merge_pr: the retry also works when no expected_head_sha is supplied"

# A non-permission failure must NOT trigger a mint/retry.
_reset_stubs
_set_mode merge other-error
rc=0
_run_in_fake_repo forge_merge_pr "owner/repo" "1288" "deadbeef" >/dev/null 2>&1 || rc=$?
assert_eq "1" "$rc" "forge_merge_pr: a non-permission failure (404) propagates unretried"
assert_eq "1" "$(_attempts merge)" \
    "forge_merge_pr: a non-permission failure makes exactly one attempt"
assert_eq "0" "$(wc -c < "$MINT_LOG" | tr -d ' ')" \
    "forge_merge_pr: a non-permission failure never mints a token"

# The pre-existing "Base branch was modified" stale-branch signature (handled
# by merge-pr.sh's own retry loop, NOT forge_gh_perm_safe) must also still
# propagate as a single, unretried attempt -- proving the two retry
# mechanisms stay disjoint.
_reset_stubs
_set_mode merge base-modified
rc=0
out="$(_run_in_fake_repo forge_merge_pr "owner/repo" "1288" "deadbeef" 2>&1)" || rc=$?
assert_eq "1" "$rc" "forge_merge_pr: 'Base branch was modified' propagates unretried (disjoint from the 403 ladder)"
assert_contains "$out" "Base branch was modified" \
    "forge_merge_pr: the stale-branch error text reaches the caller for merge-pr.sh's own retry classifier"
assert_eq "1" "$(_attempts merge)" \
    "forge_merge_pr: 'Base branch was modified' makes exactly one attempt (no mint escalation)"

# ============================================================================
# 2. forge_auto_merge(): shell auto-merge path (node_id lookup + mutation)
# ============================================================================
echo ""
echo "Testing forge_auto_merge() App-permission-window 403 retry..."

# --- node_id lookup 403s once, then recovers ---
_reset_stubs
_set_mode node_id perm403-once
_set_mode graphql ok
rc=0
out="$(_run_in_fake_repo forge_auto_merge "owner/repo" "1288" "deadbeef" 2>/dev/null)" || rc=$?
assert_eq "0" "$rc" \
    "forge_auto_merge: an integration-403 on the node_id lookup recovers via a fresh installation token"
assert_contains "$out" "enablePullRequestAutoMerge" \
    "forge_auto_merge: the mutation still runs (with the recovered node_id) after the node_id lookup's fresh-token retry"
assert_contains "$(cat "$MINT_LOG")" "get-token --force" \
    "forge_auto_merge: the node_id-lookup recovery force-mints a fresh installation token"
assert_eq "2" "$(_attempts node_id)" \
    "forge_auto_merge: node_id lookup makes exactly one retry after the initial 403"

# --- graphql mutation 403s once, then recovers ---
_reset_stubs
_set_mode node_id ok
_set_mode graphql perm403-once
rc=0
out="$(_run_in_fake_repo forge_auto_merge "owner/repo" "1288" "deadbeef" 2>/dev/null)" || rc=$?
assert_eq "0" "$rc" \
    "forge_auto_merge: an integration-403 on the GraphQL enablePullRequestAutoMerge mutation recovers via a fresh installation token"
assert_contains "$out" "enablePullRequestAutoMerge" \
    "forge_auto_merge: the escalated mutation attempt's response is returned"
assert_contains "$(cat "$MINT_LOG")" "get-token --force" \
    "forge_auto_merge: the mutation-403 recovery force-mints a fresh installation token"
assert_eq "2" "$(_attempts graphql)" \
    "forge_auto_merge: the mutation makes exactly one retry after the initial 403"

# Without expected_head_sha (the no-oid mutation shape) also recovers.
_reset_stubs
_set_mode node_id ok
_set_mode graphql perm403-once
rc=0
_run_in_fake_repo forge_auto_merge "owner/repo" "1288" >/dev/null 2>&1 || rc=$?
assert_eq "0" "$rc" \
    "forge_auto_merge: the mutation-403 recovery also works when no expected_head_sha is supplied"

# A node_id-lookup failure that is NOT a permission fault must not mint, and
# must abort forge_auto_merge entirely (the mutation is never attempted --
# there is no node_id to pass it).
_reset_stubs
_set_mode node_id other-error
_set_mode graphql ok
rc=0
_run_in_fake_repo forge_auto_merge "owner/repo" "1288" "deadbeef" >/dev/null 2>&1 || rc=$?
assert_eq "1" "$rc" "forge_auto_merge: a non-permission node_id-lookup failure propagates unretried"
assert_eq "1" "$(_attempts node_id)" \
    "forge_auto_merge: a non-permission node_id-lookup failure makes exactly one attempt"
assert_eq "0" "$(wc -c < "$MINT_LOG" | tr -d ' ')" \
    "forge_auto_merge: a non-permission node_id-lookup failure never mints a token"
if [[ -f "$STUB_DIR/attempts-graphql.log" ]]; then
    fail "forge_auto_merge: the mutation must never be attempted when node_id resolution fails"
else
    pass "forge_auto_merge: the mutation is never attempted when node_id resolution fails"
fi

# A GraphQL mutation failure that is NOT a permission fault (e.g. the
# pre-existing CLEAN/UNSTABLE and Base-branch-modified strings, handled by
# merge-pr.sh's own classifiers) must still propagate unretried.
_reset_stubs
_set_mode node_id ok
_set_mode graphql other-error
rc=0
out="$(_run_in_fake_repo forge_auto_merge "owner/repo" "1288" "deadbeef" 2>&1)" || rc=$?
assert_eq "1" "$rc" "forge_auto_merge: a non-permission mutation failure propagates unretried"
assert_eq "1" "$(_attempts graphql)" \
    "forge_auto_merge: a non-permission mutation failure makes exactly one attempt"
assert_eq "0" "$(wc -c < "$MINT_LOG" | tr -d ' ')" \
    "forge_auto_merge: a non-permission mutation failure never mints a token"

# ============================================================================
# 3. Source wiring
# ============================================================================
echo ""
echo "Testing lib/forge-helpers.sh source wiring (#1293)..."

_fmp_block="$(awk '/^forge_merge_pr\(\)/{f=1} f; /^}/{if(f){exit}}' "$FORGE_HELPERS_SRC")"
if echo "$_fmp_block" | grep -q 'forge_gh_perm_safe api'; then
    pass "forge_merge_pr() routes its GitHub merge call through forge_gh_perm_safe"
else
    fail "forge_merge_pr() must route its GitHub merge call through forge_gh_perm_safe"
fi
if echo "$_fmp_block" | grep -Eq '^\s*gh api'; then
    fail "forge_merge_pr() still has a bare 'gh api' call bypassing forge_gh_perm_safe"
else
    pass "forge_merge_pr() has no bare 'gh api' call left on the GitHub branch"
fi

_fam_block="$(awk '/^forge_auto_merge\(\)/{f=1} f; /^}/{if(f){exit}}' "$FORGE_HELPERS_SRC")"
if echo "$_fam_block" | grep -q 'forge_gh_perm_safe api "repos/\$nwo/pulls/\$pr_number" --jq'; then
    pass "forge_auto_merge() routes its node_id lookup through forge_gh_perm_safe"
else
    fail "forge_auto_merge() must route its node_id lookup through forge_gh_perm_safe"
fi
if echo "$_fam_block" | grep -q 'forge_gh_perm_safe api graphql'; then
    pass "forge_auto_merge() routes its GraphQL mutation through forge_gh_perm_safe"
else
    fail "forge_auto_merge() must route its GraphQL mutation through forge_gh_perm_safe"
fi
if echo "$_fam_block" | grep -Eq '^\s*gh api'; then
    fail "forge_auto_merge() still has a bare 'gh api' call bypassing forge_gh_perm_safe"
else
    pass "forge_auto_merge() has no bare 'gh api' call left on the GitHub branch"
fi

echo ""
echo "Testing merge-pr.sh native loom-daemon path wiring (#1293)..."

# The native path's failure branch must check is_app_permission_error and, on
# a match, set _AM_DECLINED=true so the (fixed) shell forge_auto_merge runs
# instead of just retrying the native binary call again.
_native_block="$(awk '/AUTO_MERGE_OUTPUT=\$\(loom-daemon forge auto-merge/{f=1} f; /^[[:space:]]*if \[\[ "\$_AM_DECLINED" == true \]\]; then$/{if(f){exit}}' "$MERGE_PR_SRC")"

if echo "$_native_block" | grep -q 'is_app_permission_error "\$AUTO_MERGE_OUTPUT"'; then
    pass "merge-pr.sh's native-path failure branch checks is_app_permission_error on AUTO_MERGE_OUTPUT"
else
    fail "merge-pr.sh's native-path failure branch must check is_app_permission_error (#1293)"
fi

if echo "$_native_block" | grep -Eq 'is_app_permission_error.*\n.*_AM_DECLINED=true' \
   || { echo "$_native_block" | grep -q 'is_app_permission_error' && echo "$_native_block" | grep -q '_AM_DECLINED=true'; }; then
    pass "merge-pr.sh's native-path App-permission branch sets _AM_DECLINED=true (falls through to the shell path)"
else
    fail "merge-pr.sh's native-path App-permission branch must set _AM_DECLINED=true"
fi

# Ordering: the is_app_permission_error check must be INSIDE the
# "$_AM_RC -ne 3" (native attempted and failed) branch, i.e. after
# _AM_DECLINED=false is set for that branch -- never short-circuiting the
# existing head-mismatch (exit 4) or Gitea-decline (exit 3) handling above it.
_decline_false_line=$(grep -n '_AM_DECLINED=false' "$MERGE_PR_SRC" | head -1 | cut -d: -f1)
_perm_check_line=$(grep -n 'is_app_permission_error "\$AUTO_MERGE_OUTPUT"' "$MERGE_PR_SRC" | head -1 | cut -d: -f1)
_am_rc4_line=$(grep -n '_AM_RC -eq 4' "$MERGE_PR_SRC" | head -1 | cut -d: -f1)
if [[ -n "$_decline_false_line" ]] && [[ -n "$_perm_check_line" ]] && [[ -n "$_am_rc4_line" ]] \
   && [[ "$_am_rc4_line" -lt "$_decline_false_line" ]] && [[ "$_decline_false_line" -lt "$_perm_check_line" ]]; then
    pass "the App-permission check is placed after the head-mismatch (exit 4) branch and inside the generic-failure branch"
else
    fail "the App-permission check ordering is wrong (rc4=$_am_rc4_line declined_false=$_decline_false_line perm_check=$_perm_check_line)"
fi

# The comment must name #1293 so a future reader can find this incident.
if grep -q '#1293' "$MERGE_PR_SRC"; then
    pass "merge-pr.sh references #1293 near the native-path fallback"
else
    fail "merge-pr.sh should reference #1293 near the native-path fallback for traceability"
fi

# ============================================================================
# 4. Regression: other classifiers/blocks untouched
# ============================================================================
echo ""
echo "Testing no regression to other error-shape classifiers (#1293)..."

for anchor in \
    'grep -q "Merge already in progress"' \
    'grep -q "Base branch was modified"' \
    '_is_head_mismatch_response "\$MERGE_RESPONSE"' \
    '_is_head_mismatch_response "\$AUTO_MERGE_OUTPUT"' \
    'grep -q "is in clean status"' \
    'grep -q "is in unstable status"' \
    'grep -Eq "API rate limit\|rate limit exceeded\|RATE_LIMITED\|was submitted too quickly\|could not resolve repository NWO"'
do
    if grep -q -- "$anchor" "$MERGE_PR_SRC"; then
        pass "unchanged: $anchor"
    else
        fail "regression: missing classifier anchor: $anchor"
    fi
done

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
