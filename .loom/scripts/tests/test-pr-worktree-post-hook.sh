#!/usr/bin/env bash
# test-pr-worktree-post-hook.sh - Regression test for issue #2020
#
# Problem: worktree.sh already re-runs a project-specific post-worktree hook
# (.loom/hooks/post-worktree.sh, invoked automatically after every issue
# worktree is created) but pr-worktree.sh never did — so a PR review worktree
# created via pr-worktree.sh could silently `import` the MAIN CHECKOUT's
# src/klayout_tools (via a stale editable install recorded elsewhere) instead
# of the worktree's own copy. That is a false negative on a PR that fixes a
# bug (old code still runs) and a more dangerous false positive on a PR that
# introduces one (the broken code is never actually exercised).
#
# This is klayout-tools-local (this repo's `.loom/hooks/post-worktree.sh`
# runs `uv sync --extra dev`; upstream Loom's own pr-worktree.sh template has
# no Python-specific setup step at all) — per
# .loom/docs/local-test-suite-wiring.md this suite has no automated CI runner
# in this repo and must be run manually:
#   ./.loom/scripts/tests/test-pr-worktree-post-hook.sh
#
# This test verifies:
#   1. pr-worktree.sh's run_post_worktree_hook helper is defined and called
#      from BOTH the "reuse an existing PR worktree" path and the "freshly
#      created PR worktree" path.
#   2. .loom/hooks/post-worktree.sh exists, is executable, and runs `uv sync`.
#   3. The hook itself, run standalone against a scratch directory with a
#      stubbed `uv`, invokes `uv sync --extra dev` and exits 0; skips
#      cleanly (exit 0, no `uv` call) when there's no pyproject.toml; and
#      fails loudly but non-crashingly (exit 1) when `uv` is missing.
#   4. A full pr-worktree.sh run against a scratch repo (mocked `gh pr
#      checkout`, mocked `uv`) actually invokes the hook and the hook
#      actually invokes `uv sync`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"

PR_WORKTREE_SH="$SCRIPTS_DIR/pr-worktree.sh"
POST_WORKTREE_HOOK="$REPO_ROOT/.loom/hooks/post-worktree.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_grep() {
    local pattern="$1" file="$2" msg="$3"
    if grep -qE "$pattern" "$file"; then pass "$msg"; else fail "$msg (pattern: $pattern)"; fi
}

if [[ ! -x "$PR_WORKTREE_SH" ]]; then
    echo -e "${RED}FATAL${NC}: pr-worktree.sh not found or not executable at $PR_WORKTREE_SH"
    exit 1
fi

# --- Test 1: pr-worktree.sh defines and calls the post-worktree hook helper ---
echo "Test 1: pr-worktree.sh wires up run_post_worktree_hook"
assert_grep "run_post_worktree_hook\(\)" "$PR_WORKTREE_SH" \
    "pr-worktree.sh defines a run_post_worktree_hook helper"
assert_grep '\.loom/hooks/post-worktree\.sh' "$PR_WORKTREE_SH" \
    "pr-worktree.sh references .loom/hooks/post-worktree.sh"
HOOK_CALL_COUNT="$(grep -c 'run_post_worktree_hook "\$WORKTREE_PATH"' "$PR_WORKTREE_SH" || true)"
if [[ "$HOOK_CALL_COUNT" -ge 2 ]]; then
    pass "run_post_worktree_hook is called at least twice (reuse path + fresh-creation path)"
else
    fail "expected run_post_worktree_hook to be called at least twice, found $HOOK_CALL_COUNT"
fi

# --- Test 2: .loom/hooks/post-worktree.sh exists and syncs uv ---
echo ""
echo "Test 2: .loom/hooks/post-worktree.sh exists and runs uv sync"
if [[ -x "$POST_WORKTREE_HOOK" ]]; then
    pass "post-worktree.sh exists and is executable"
else
    fail "post-worktree.sh missing or not executable at $POST_WORKTREE_HOOK"
fi
assert_grep 'uv sync --extra dev' "$POST_WORKTREE_HOOK" \
    "post-worktree.sh runs 'uv sync --extra dev'"

# --- Test 3: standalone hook behavior against a scratch directory ---
echo ""
echo "Test 3: post-worktree.sh standalone behavior"

HOOK_TMP=$(mktemp -d /tmp/loom-post-hook-test.XXXXXX)
MOCKBIN=$(mktemp -d /tmp/loom-post-hook-mockbin.XXXXXX)
cleanup_hook_test() { rm -rf "$HOOK_TMP" "$MOCKBIN"; }
trap cleanup_hook_test EXIT

# 3a: pyproject.toml present, uv present -> uv sync invoked, exit 0
mkdir -p "$HOOK_TMP/with-pyproject"
echo '[project]' > "$HOOK_TMP/with-pyproject/pyproject.toml"
UV_LOG="$HOOK_TMP/uv-invocations.log"
cat > "$MOCKBIN/uv" <<MOCKEOF
#!/usr/bin/env bash
echo "\$@" >> "$UV_LOG"
exit 0
MOCKEOF
chmod +x "$MOCKBIN/uv"

set +e
HOOK_OUT_A=$(PATH="$MOCKBIN:$PATH" "$POST_WORKTREE_HOOK" "$HOOK_TMP/with-pyproject" "feature/issue-2020" "2020" 2>&1)
HOOK_RC_A=$?
set -e

if [[ "$HOOK_RC_A" -eq 0 ]]; then
    pass "hook exits 0 when pyproject.toml present and uv succeeds"
else
    fail "hook expected exit 0, got $HOOK_RC_A — output: $HOOK_OUT_A"
fi
if [[ -f "$UV_LOG" ]] && grep -qE 'sync --extra dev --quiet' "$UV_LOG"; then
    pass "hook invoked 'uv sync --extra dev --quiet'"
else
    fail "expected 'uv sync --extra dev --quiet' in $UV_LOG"
fi

# 3b: no pyproject.toml -> skip cleanly, uv never invoked, exit 0
mkdir -p "$HOOK_TMP/no-pyproject"
rm -f "$UV_LOG"
set +e
HOOK_OUT_B=$(PATH="$MOCKBIN:$PATH" "$POST_WORKTREE_HOOK" "$HOOK_TMP/no-pyproject" "some-branch" "" 2>&1)
HOOK_RC_B=$?
set -e

if [[ "$HOOK_RC_B" -eq 0 ]]; then
    pass "hook exits 0 (skip) when no pyproject.toml is present"
else
    fail "hook expected exit 0 with no pyproject.toml, got $HOOK_RC_B — output: $HOOK_OUT_B"
fi
if [[ ! -f "$UV_LOG" ]]; then
    pass "hook never invoked uv when there was no pyproject.toml"
else
    fail "hook invoked uv even though there was no pyproject.toml: $(cat "$UV_LOG")"
fi

# 3c: pyproject.toml present, uv NOT on PATH -> fails loudly but exits cleanly (1)
STRIPPED_PATH="$(printf '%s\n' "$PATH" | tr ':' '\n' | grep -v "^$MOCKBIN\$" | paste -sd: -)"
set +e
HOOK_OUT_C=$(PATH="$STRIPPED_PATH" "$POST_WORKTREE_HOOK" "$HOOK_TMP/with-pyproject" "some-branch" "" 2>&1)
HOOK_RC_C=$?
set -e

if [[ "$HOOK_RC_C" -eq 1 ]]; then
    pass "hook exits 1 (non-crashing failure) when 'uv' is not on PATH"
else
    fail "hook expected exit 1 when uv missing, got $HOOK_RC_C — output: $HOOK_OUT_C"
fi
if echo "$HOOK_OUT_C" | grep -q "2020"; then
    pass "hook's 'uv missing' message cites issue #2020"
else
    fail "expected the 'uv missing' message to cite issue #2020 — got: $HOOK_OUT_C"
fi

cleanup_hook_test
trap - EXIT

# --- Test 4: full pr-worktree.sh run actually invokes the hook end-to-end ---
echo ""
echo "Test 4: pr-worktree.sh's happy path invokes the post-worktree hook"

TMP=$(mktemp -d /tmp/loom-pr-posthook-test.XXXXXX)
MOCKBIN2=$(mktemp -d /tmp/loom-pr-posthook-mockbin.XXXXXX)
trap 'rm -rf "$TMP" "$MOCKBIN2"; cd "$REPO_ROOT" 2>/dev/null || true' EXIT

git init -q -b main "$TMP/origin.git" --bare
git init -q -b main "$TMP/repo"
cd "$TMP/repo"
git config user.email t@t
git config user.name t
git commit --allow-empty -q -m init
git remote add origin "$TMP/origin.git"
git push -q origin main

# Scratch repo gets its own copy of the real hook, so the full pr-worktree.sh
# run resolves $REPO_ROOT/.loom/hooks/post-worktree.sh to THIS file, not the
# real repo's.
mkdir -p ".loom/hooks"
cp "$POST_WORKTREE_HOOK" ".loom/hooks/post-worktree.sh"
chmod +x ".loom/hooks/post-worktree.sh"
echo '[project]' > pyproject.toml
git add pyproject.toml .loom/hooks/post-worktree.sh
git commit -q -m "scratch fixture"
git push -q origin main

UV_LOG2="$TMP/uv-invocations.log"
cat > "$MOCKBIN2/uv" <<MOCKEOF
#!/usr/bin/env bash
echo "\$@" >> "$UV_LOG2"
exit 0
MOCKEOF
chmod +x "$MOCKBIN2/uv"

cat > "$MOCKBIN2/gh" <<'MOCKEOF'
#!/usr/bin/env bash
# Minimal `gh pr checkout --force` / `gh pr view` stand-in: always succeeds,
# no live forge involved.
if [[ "$1" == "pr" && "$2" == "checkout" ]]; then
    exit 0
fi
exit 0
MOCKEOF
chmod +x "$MOCKBIN2/gh"

set +e
PR_OUT4=$(PATH="$MOCKBIN2:$PATH" LOOM_DEFAULT_BRANCH=main "$PR_WORKTREE_SH" 2020 2>&1)
PR_RC4=$?
set -e

if [[ "$PR_RC4" -eq 0 ]]; then
    pass "pr-worktree.sh exits 0 on the mocked happy path"
else
    fail "pr-worktree.sh expected exit 0, got $PR_RC4 — output: $PR_OUT4"
fi
if echo "$PR_OUT4" | grep -q "Post-worktree hook completed"; then
    pass "pr-worktree.sh reports the post-worktree hook completed"
else
    fail "expected 'Post-worktree hook completed' in output — got: $PR_OUT4"
fi
if [[ -f "$UV_LOG2" ]] && grep -qE 'sync --extra dev --quiet' "$UV_LOG2"; then
    pass "the real pr-worktree.sh run caused 'uv sync --extra dev --quiet' to be invoked inside the PR worktree"
else
    fail "expected 'uv sync --extra dev --quiet' to have been invoked via $UV_LOG2"
fi

cd "$REPO_ROOT"

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
