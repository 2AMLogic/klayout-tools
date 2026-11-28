#!/usr/bin/env bash
# test-pr-worktree-isolation.sh - Regression test for issue #3358
#
# Verifies that:
#   1. pr-worktree.sh creates a worktree at .loom/worktrees/pr-<N>/ with the
#      .loom-managed sentinel (PR-mode entry).
#   2. doctor.md (defaults/) documents the branch-isolation requirement,
#      including the #9401 occupied-worktree yield (live process, no lease).
#   3. pr-worktree.sh surfaces a clear diagnostic when the PR branch is already
#      checked out elsewhere, and cleans up the partial pr-<N> worktree (#6264,
#      #6594).
#   4. pr-worktree.sh catches that collision up front — before any pr-<N>
#      worktree is created at all — when `gh pr view` resolves the head branch
#      (#6594).
#   5. pr-worktree.sh on a non-colliding PR runs `gh pr checkout` inside the new
#      pr-<N> worktree, never in the orchestrator checkout (#3358): a mock `gh`
#      does a real `git checkout`, so a mis-directed `cd` visibly moves the
#      orchestrator's branch/HEAD. The helper (not the test) writes the sentinel.
#   6. judge.md and `pr-worktree.sh --help` agree on the Judge fallback (#11194).
#   7. When the PR branch is checked out in the PRIMARY clone, pr-worktree.sh
#      creates pr-<N> detached at the PR head sha and exits 0 (#14391), both via
#      the up-front check and via the checkout-failure path.
#   8. pr-worktree.sh runs the primary checkout's .loom/hooks/post-worktree.sh
#      exactly once after each successful create or refresh (#15045), with
#      (worktree path, branch, PR number) from the worktree, and never on a
#      failed refresh, a builder-held branch, or a missing/non-executable hook;
#      a failing hook only warns on stderr.
#   9. builder_holder excludes the PR's own pr-<N> worktree by PHYSICAL path
#      (#15121): re-entering an attached pr-<N> through a symlinked repo path
#      (macOS /tmp -> /private/tmp natively; a matching symlink fixture on
#      Linux) reuses the worktree instead of dying on the builder-wins error,
#      while a genuine builder holder is still refused.
#  10. The pr-<N> reuse gate matches registered worktrees by pwd -P-canonicalized
#      physical path, not substring: re-entry through a symlinked repo path or a
#      symlinked LOOM_WORKTREE_ROOT reuses the registered worktree (printing the
#      logical path on stdout), and an unregistered prefix-colliding directory
#      (pr-97 beside pr-970) is rejected without running gh pr checkout (#15196).
#
# Tests 3-5 run the real pr-worktree.sh against a throwaway git repo with a
# mocked `gh`, since there is no live forge in CI. Branch-shape classification
# is deliberately NOT asserted here: it is no longer shell logic. It lives in
# `issue_number_of` (loom-daemon/src/merge_pr/cleanup_paths.rs, ported out of
# merge-pr.sh in #8191) and the #3358 strict-regex regression cases are covered
# by that module's own unit tests (loom-daemon/src/merge_pr/cleanup_paths/tests.rs).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"

PR_WORKTREE_SH="$SCRIPTS_DIR/pr-worktree.sh"
# doctor.md is shipped (installed at .claude/commands/loom/doctor.md), so
# resolve it the way each layout actually lays it out: the installed path
# first (consumer repos, and Loom's own dogfooded checkout), falling back
# to the defaults/ source-tree path (a bare source checkout with no
# .claude/commands/loom/ copy yet). See issue #6194 / #6241.
if [[ -f "$REPO_ROOT/.claude/commands/loom/doctor.md" ]]; then
    DOCTOR_MD="$REPO_ROOT/.claude/commands/loom/doctor.md"
else
    DOCTOR_MD="$REPO_ROOT/defaults/.claude/commands/loom/doctor.md"
fi

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

if [[ ! -f "$DOCTOR_MD" ]]; then
    echo -e "${RED}FATAL${NC}: doctor.md not found at $DOCTOR_MD"
    exit 1
fi

# --- Test 1: pr-worktree.sh exists and has the sentinel write block ---
echo "Test 1: pr-worktree.sh ships sentinel write logic"
if [[ -x "$PR_WORKTREE_SH" ]]; then
    pass "pr-worktree.sh exists and is executable"
else
    fail "pr-worktree.sh missing or not executable at $PR_WORKTREE_SH"
fi
assert_grep "Loom-managed worktree marker" "$PR_WORKTREE_SH" \
    "pr-worktree.sh writes the sentinel marker"
assert_grep "# PR: " "$PR_WORKTREE_SH" \
    "pr-worktree.sh records PR number in sentinel (mirrors worktree.sh issue convention)"
assert_grep "worktrees/pr-" "$PR_WORKTREE_SH" \
    "pr-worktree.sh uses .loom/worktrees/pr-<N>/ convention"

# --- Test 2: doctor.md documents PR Branch Isolation ---
echo ""
echo "Test 2: doctor.md documents the worktree-isolation rule"
assert_grep "PR Branch Isolation" "$DOCTOR_MD" \
    "doctor.md has a 'PR Branch Isolation' section"
assert_grep "pr-worktree\.sh" "$DOCTOR_MD" \
    "doctor.md references pr-worktree.sh helper"
assert_grep "\^feature/issue-" "$DOCTOR_MD" \
    "doctor.md documents the feature/issue-<N> regex heuristic"

# #9401: a live process in the PR worktree with no PR lease = yield, not reset.
assert_grep "^### Occupied worktree: yield, never reset \(#9401\)" "$DOCTOR_MD" \
    "doctor.md has the 'Occupied worktree' yield section (#9401)"
assert_grep "After \`lease pr claim\` succeeds and \*\*before\*\* \`worktree\.sh\`" "$DOCTOR_MD" \
    "occupied-worktree check runs after the lease claim, before worktree setup"
assert_grep "readlink \"/proc/\\\$p/cwd\".*\|\| lsof -a -p" "$DOCTOR_MD" \
    "occupied-worktree probe reads /proc cwd with an lsof fallback"
assert_grep "\[ \"\\\$q\" = \"\\\$S\" \] \|\|" "$DOCTOR_MD" \
    "occupied-worktree probe excludes the Doctor's own session"
assert_grep "^post \`Doctor yielding: live process in <WT> \(pid <pid>: <command>\) with no PR$" "$DOCTOR_MD" \
    "yield comment names the worktree, pid and command"
assert_grep "lease; worktree left untouched\.\` and stop — no snapshot, reset or checkout\." "$DOCTOR_MD" \
    "yield stops without touching the worktree"
assert_grep "\*\*Occupied → yield\*\*: \`lease pr release\`, drop any \`loom:treating\` you added" "$DOCTOR_MD" \
    "yield releases the lease and the doctor's own loom:treating"
assert_grep "\*\*Cannot probe\*\* \(no \`pgrep\`, or neither \`/proc\` nor \`lsof\`\): treat a dirty" "$DOCTOR_MD" \
    "documents the no-pgrep/no-lsof fallback (dirty = occupied)"
occ_line=$(grep -n "^### Occupied worktree" "$DOCTOR_MD" | head -1 | cut -d: -f1)
pick_line=$(grep -n "^# PR worktree selection" "$DOCTOR_MD" | head -1 | cut -d: -f1)
if [[ -n "$occ_line" && -n "$pick_line" && "$occ_line" -lt "$pick_line" ]]; then
    pass "occupied-worktree check precedes worktree selection/checkout"
else
    fail "occupied-worktree check precedes worktree selection/checkout (occ=$occ_line pick=$pick_line)"
fi

# Execute the documented probe with only a simulated process in a
# disposable directory. The real host's process list is irrelevant.
probe_dir=$(mktemp -d)
awk -v h="### Occupied worktree" 'index($0,h)==1{f=1} f&&/^```bash$/{g=1;next} g&&/^```$/{exit} g' "$DOCTOR_MD" > "$probe_dir/probe.txt"
if [[ -s "$probe_dir/probe.txt" ]]; then
    mkdir -p "$probe_dir/bin" "$probe_dir/.loom/worktrees/pr-1"
    cat > "$probe_dir/bin/pgrep" <<'EOF'
#!/bin/bash
echo 4199
EOF
    cat > "$probe_dir/bin/readlink" <<'EOF'
#!/bin/bash
[[ "$1" = /proc/4199/cwd ]] || exit 1
(cd .loom/worktrees/pr-1 && pwd -P)
EOF
    cat > "$probe_dir/bin/ps" <<'EOF'
#!/bin/bash
case "$*" in
    '-o ppid= -p 4199') echo 1 ;;
    '-o pid=,command= -p 4199') echo '4199 fixture-process' ;;
    *) exit 1 ;;
esac
EOF
    chmod +x "$probe_dir/bin/"*
    run_probe() {
        (cd "$probe_dir" && PATH="$probe_dir/bin:$PATH" \
            WORKTREE_PATH=".loom/worktrees/$1" LOOM_AGENT_SESSION_PID=4400 bash probe.txt)
    }
    assert_eq "" "$(run_probe nonexistent-1)" "occupied-worktree probe prints nothing for a nonexistent worktree"
    assert_eq "4199 fixture-process" "$(run_probe pr-1)" "occupied-worktree probe reports a foreign process in the selected worktree"
else
    fail "could not extract the occupied-worktree probe snippet from doctor.md"
fi
rm -rf "$probe_dir"

# --- Test 3: detached-HEAD collision is diagnosed clearly, not silent (#6264) ---
echo ""
echo "Test 3: pr-worktree.sh surfaces a clear diagnostic on the branch-collision failure (#6264), and cleans up after itself (#6594)"

# Throwaway repo fixture shared by Tests 3, 4 and 5: a tiny origin + clone so the
# real pr-worktree.sh runs real git commands without touching the real
# workspace.
TMP=$(mktemp -d /tmp/loom-pr-iso-test.XXXXXX)
trap 'rm -rf "$TMP"; cd "$REPO_ROOT" 2>/dev/null || true' EXIT

git init -q -b main "$TMP/origin.git" --bare
git init -q -b main "$TMP/repo"
cd "$TMP/repo"
git config user.email t@t
git config user.name t
git commit --allow-empty -q -m init
git remote add origin "$TMP/origin.git"
git push -q origin main

# Reproduces (with a mocked `gh`, since this test has no live forge) the exact
# mechanism confirmed by hand against a real PR while curating/building #6264:
# `gh pr checkout <N> --force` fails loudly (git refuses the same branch
# checked out in two worktrees at once). The fix under test is NOT changing
# that git-level refusal (impossible — it's a structural git invariant) but
# making pr-worktree.sh's failure message name the colliding worktree
# explicitly (#6264) AND, since this mock's `gh pr view` returns nothing (see
# below), exercising the #6594 fallback cleanup path: when the up-front
# collision check can't resolve the PR's head branch, the collision is only
# discovered via the `gh pr checkout` failure, and the partially-created
# pr-<N> worktree must be removed rather than left behind.
#
# Runs inside the throwaway "$TMP/repo" built above, against its origin remote.
COLLIDE_BRANCH="feature/issue-998"
git update-ref "refs/heads/$COLLIDE_BRANCH" HEAD
# Simulate an existing builder worktree already holding the branch — the
# real-world collision source (a same-host issue-<N> builder worktree).
git worktree add -q ".loom/worktrees/issue-998" "$COLLIDE_BRANCH"

MOCKBIN=$(mktemp -d /tmp/loom-pr-iso-mockbin.XXXXXX)
trap 'rm -rf "$TMP" "$MOCKBIN"; cd "$REPO_ROOT" 2>/dev/null || true' EXIT
cat > "$MOCKBIN/gh" <<'MOCKEOF'
#!/usr/bin/env bash
# Minimal `gh pr checkout --force` stand-in reproducing the exact failure
# `git worktree add`/`checkout` itself produces when the target branch is
# already checked out elsewhere — see the real repro in the #6264 PR
# description. `gh pr view` (used by pr-worktree.sh's #6594 up-front
# collision check) is deliberately left unhandled here (falls through to the
# generic `exit 0` with no stdout) to exercise the fallback path where that
# check can't resolve the head branch and the collision is only caught later,
# via the checkout failure below. Any other subcommand is a no-op success.
if [[ "$1" == "pr" && "$2" == "checkout" ]]; then
    echo "fatal: 'feature/issue-998' is already used by worktree at '$LOOM_TEST_COLLIDING_WT'" >&2
    echo "failed to run git: exit status 128" >&2
    exit 1
fi
exit 0
MOCKEOF
chmod +x "$MOCKBIN/gh"

COLLIDING_WT_ABS="$(cd ".loom/worktrees/issue-998" && pwd)"

set +e
PR_OUT=$(PATH="$MOCKBIN:$PATH" LOOM_DEFAULT_BRANCH=main LOOM_TEST_COLLIDING_WT="$COLLIDING_WT_ABS" \
    "$PR_WORKTREE_SH" 998 2>&1)
PR_RC=$?
set -e
if [[ "$PR_RC" -ne 0 ]]; then
    pass "pr-worktree.sh exits non-zero on the branch-collision failure"
else
    fail "pr-worktree.sh exited 0 despite the checkout failure — got: $PR_OUT"
fi
if grep -q "already checked out in another worktree" <<<"$PR_OUT"; then
    pass "failure message explicitly names the collision (not just the raw git error)"
else
    fail "expected an explicit collision diagnostic; got: $PR_OUT"
fi
if grep -qF "$COLLIDING_WT_ABS" <<<"$PR_OUT"; then
    pass "failure message names the specific colliding worktree path"
else
    fail "expected the colliding worktree path ($COLLIDING_WT_ABS) in the output; got: $PR_OUT"
fi
if grep -qi "do NOT evaluate code in it as-is" <<<"$PR_OUT"; then
    pass "failure message warns against evaluating the detached worktree as-is"
else
    fail "expected an explicit do-not-evaluate warning; got: $PR_OUT"
fi

# #6594: the pr-998 worktree directory is now removed (via `git worktree
# remove`, not left behind on disk on a detached HEAD) before pr-worktree.sh
# exits — this is the behavior change under test in this issue. It replaces
# the pre-#6594 "left behind" assertions that used to live here.
if [[ -d ".loom/worktrees/pr-998" ]]; then
    fail "expected .loom/worktrees/pr-998 to be removed after the failed checkout (#6594 cleanup)"
else
    pass "the pr-998 worktree directory is removed after the failed checkout (#6594 cleanup)"
fi
if git worktree list --porcelain | grep -q "worktrees/pr-998$"; then
    fail "expected no dangling 'git worktree list' entry for pr-998 after cleanup"
else
    pass "no dangling 'git worktree list' entry for pr-998 after cleanup"
fi

# --- Test 4: branch collision is caught up front, before any worktree is
#     created at all, when 'gh pr view' can resolve the PR's head branch (#6594) ---
echo ""
echo "Test 4: pr-worktree.sh catches the collision up front and never creates a pr-<N> worktree (#6594)"

# Still inside "$TMP/repo". Use PR 997 / feature/issue-997 to avoid colliding
# with Test 3's 998 fixture above.
COLLIDE_BRANCH2="feature/issue-997"
git update-ref "refs/heads/$COLLIDE_BRANCH2" HEAD
git worktree add -q ".loom/worktrees/issue-997" "$COLLIDE_BRANCH2"
COLLIDING_WT2_ABS="$(cd ".loom/worktrees/issue-997" && pwd)"

MOCKBIN2=$(mktemp -d /tmp/loom-pr-iso-mockbin2.XXXXXX)
trap 'rm -rf "$TMP" "$MOCKBIN" "$MOCKBIN2"; cd "$REPO_ROOT" 2>/dev/null || true' EXIT
cat > "$MOCKBIN2/gh" <<'MOCKEOF'
#!/usr/bin/env bash
# Stand-in for `gh` supporting exactly the two subcommands pr-worktree.sh's
# up-front collision check (#6594) needs: `gh pr view --json headRefName`
# resolves the PR's head branch to the (already checked-out-elsewhere)
# collision branch. `gh pr checkout` writes a marker file if it is ever
# invoked, so the test can assert the up-front check short-circuited before
# reaching it — reaching checkout here would mean the up-front check failed
# to catch a collision it should have caught.
if [[ "$1" == "pr" && "$2" == "view" ]]; then
    echo "$LOOM_TEST_HEAD_BRANCH"
    exit 0
fi
if [[ "$1" == "pr" && "$2" == "checkout" ]]; then
    touch "$LOOM_TEST_CHECKOUT_MARKER"
    exit 0
fi
exit 0
MOCKEOF
chmod +x "$MOCKBIN2/gh"

CHECKOUT_MARKER="$TMP/checkout-was-called"
set +e
PR_OUT2=$(PATH="$MOCKBIN2:$PATH" LOOM_DEFAULT_BRANCH=main LOOM_TEST_HEAD_BRANCH="$COLLIDE_BRANCH2" \
    LOOM_TEST_CHECKOUT_MARKER="$CHECKOUT_MARKER" "$PR_WORKTREE_SH" 997 2>&1)
PR_RC2=$?
set -e

if [[ "$PR_RC2" -ne 0 ]]; then
    pass "pr-worktree.sh exits non-zero on the up-front collision check"
else
    fail "pr-worktree.sh exited 0 despite the up-front collision — got: $PR_OUT2"
fi
if grep -q "already checked out in another worktree" <<<"$PR_OUT2"; then
    pass "up-front failure message explicitly names the collision"
else
    fail "expected an explicit up-front collision diagnostic; got: $PR_OUT2"
fi
if grep -qF "$COLLIDING_WT2_ABS" <<<"$PR_OUT2"; then
    pass "up-front failure message names the specific colliding worktree path"
else
    fail "expected the colliding worktree path ($COLLIDING_WT2_ABS) in the output; got: $PR_OUT2"
fi
if [[ -d ".loom/worktrees/pr-997" ]]; then
    fail "expected no .loom/worktrees/pr-997 directory to ever be created (up-front detection should prevent it)"
else
    pass "no .loom/worktrees/pr-997 directory created — collision caught before worktree creation"
fi
if [[ -f "$CHECKOUT_MARKER" ]]; then
    fail "expected 'gh pr checkout' to never be called — the up-front check should short-circuit first"
else
    pass "'gh pr checkout' was never called — up-front detection short-circuited before it"
fi

# --- Test 5: success path never moves the orchestrator checkout (#3358) ---
echo ""
echo "Test 5: pr-worktree.sh checks the PR branch out in pr-<N>, leaving the orchestrator checkout untouched (#3358)"

# Still inside "$TMP/repo", on main. PR 999 / fix/foo-bar collides with nothing.
# The PR branch must be a real commit AHEAD of main: with `update-ref ... HEAD`
# a mis-directed checkout would move the branch name but not the SHA, which
# would leave the SHA assertion below unable to fail.
OK_BRANCH="fix/foo-bar"
git checkout -q -b "$OK_BRANCH"
git commit --allow-empty -q -m "pr 999 change"
git checkout -q main
ORCH_BRANCH_BEFORE="$(git rev-parse --abbrev-ref HEAD)"
ORCH_SHA_BEFORE="$(git rev-parse HEAD)"

MOCKBIN3=$(mktemp -d /tmp/loom-pr-iso-mockbin3.XXXXXX)
trap 'rm -rf "$TMP" "$MOCKBIN" "$MOCKBIN2" "$MOCKBIN3"; cd "$REPO_ROOT" 2>/dev/null || true' EXIT
cat > "$MOCKBIN3/gh" <<'MOCKEOF'
#!/usr/bin/env bash
# `gh pr view` resolves the head branch; `gh pr checkout` performs a REAL
# `git checkout` in its cwd, so wherever pr-worktree.sh runs it is where HEAD
# moves. (Unlike the Test 3 mock, which only prints a canned failure.)
if [[ "$1" == "pr" && "$2" == "view" ]]; then
    echo "$LOOM_TEST_HEAD_BRANCH"
    exit 0
fi
if [[ "$1" == "pr" && "$2" == "checkout" ]]; then
    git checkout -q "$LOOM_TEST_HEAD_BRANCH"
    exit $?
fi
exit 0
MOCKEOF
chmod +x "$MOCKBIN3/gh"

set +e
PR_OUT3=$(PATH="$MOCKBIN3:$PATH" LOOM_DEFAULT_BRANCH=main LOOM_TEST_HEAD_BRANCH="$OK_BRANCH" \
    "$PR_WORKTREE_SH" 999 2>&1)
PR_RC3=$?
set -e

if [[ "$PR_RC3" -eq 0 ]]; then
    pass "pr-worktree.sh exits 0 for a non-colliding PR"
else
    fail "pr-worktree.sh failed for a non-colliding PR (rc=$PR_RC3): $PR_OUT3"
fi
ORCH_BRANCH_AFTER="$(git rev-parse --abbrev-ref HEAD)"
ORCH_SHA_AFTER="$(git rev-parse HEAD)"
if [[ "$ORCH_BRANCH_AFTER" == "$ORCH_BRANCH_BEFORE" ]]; then
    pass "orchestrator checkout stays on '$ORCH_BRANCH_BEFORE' (#3358)"
else
    fail "orchestrator checkout moved from '$ORCH_BRANCH_BEFORE' to '$ORCH_BRANCH_AFTER' (#3358 regression)"
fi
if [[ "$ORCH_SHA_AFTER" == "$ORCH_SHA_BEFORE" ]]; then
    pass "orchestrator HEAD SHA is unchanged"
else
    fail "orchestrator HEAD moved from $ORCH_SHA_BEFORE to $ORCH_SHA_AFTER (#3358 regression)"
fi
PR999_BRANCH="$(git -C ".loom/worktrees/pr-999" rev-parse --abbrev-ref HEAD 2>/dev/null || echo MISSING)"
if [[ "$PR999_BRANCH" == "$OK_BRANCH" ]]; then
    pass "pr-999 worktree is on the PR branch ($OK_BRANCH)"
else
    fail "pr-999 worktree is on '$PR999_BRANCH', expected '$OK_BRANCH'"
fi
if grep -qx "# PR: 999" ".loom/worktrees/pr-999/.loom-managed" 2>/dev/null; then
    pass "pr-999 worktree has the .loom-managed sentinel written by the helper"
else
    fail "pr-999 worktree is missing the helper-written .loom-managed sentinel with '# PR: 999'"
fi

# --- Test 7: branch held by the primary clone -> detached pr-<N> (#14391) ---
echo ""
echo "Test 7: pr-worktree.sh creates a detached pr-<N> when the primary clone holds the PR branch (#14391)"

cd "$TMP/repo"
PRIMARY_ABS="$(pwd -P)"

MOCKBIN4=$(mktemp -d /tmp/loom-pr-iso-mockbin4.XXXXXX)
trap 'rm -rf "$TMP" "$MOCKBIN" "$MOCKBIN2" "$MOCKBIN3" "$MOCKBIN4"; cd "$REPO_ROOT" 2>/dev/null || true' EXIT
cat > "$MOCKBIN4/gh" <<'MOCKEOF'
#!/usr/bin/env bash
# LOOM_TEST_VIEW=none makes `gh pr view` resolve nothing (exercises the
# checkout-failure path); otherwise it returns the head branch or, for
# --json headRefOid, the head sha. `gh pr checkout` fails the way git does
# when the primary clone holds the branch.
if [[ "$1" == "pr" && "$2" == "view" ]]; then
    [[ "${LOOM_TEST_VIEW:-}" == "none" ]] && exit 1
    if [[ "$*" == *headRefOid* ]]; then echo "$LOOM_TEST_HEAD_SHA"; else echo "$LOOM_TEST_HEAD_BRANCH"; fi
    exit 0
fi
if [[ "$1" == "pr" && "$2" == "checkout" ]]; then
    echo "fatal: '$LOOM_TEST_HEAD_BRANCH' is already used by worktree at '$LOOM_TEST_PRIMARY'" >&2
    exit 1
fi
exit 0
MOCKEOF
chmod +x "$MOCKBIN4/gh"

# 7a: up-front path (gh pr view resolves the branch and the sha).
P_BRANCH="feature/issue-996"
git checkout -q -b "$P_BRANCH" main
git commit --allow-empty -q -m "pr 996 change"
P_SHA="$(git rev-parse HEAD)"   # primary clone stays on $P_BRANCH
set +e
PR_OUT7=$(PATH="$MOCKBIN4:$PATH" LOOM_DEFAULT_BRANCH=main LOOM_TEST_HEAD_BRANCH="$P_BRANCH" \
    LOOM_TEST_HEAD_SHA="$P_SHA" LOOM_TEST_PRIMARY="$PRIMARY_ABS" "$PR_WORKTREE_SH" 996 2>&1)
PR_RC7=$?
set -e
if [[ "$PR_RC7" -eq 0 ]]; then
    pass "primary-clone holder: pr-worktree.sh exits 0"
else
    fail "primary-clone holder: expected exit 0, got $PR_RC7: $PR_OUT7"
fi
if grep -q "Detached at $P_SHA" <<<"$PR_OUT7" && grep -q "primary clone" <<<"$PR_OUT7"; then
    pass "primary-clone holder: output says it detached, at which sha, and why"
else
    fail "primary-clone holder: missing detach explanation; got: $PR_OUT7"
fi
if [[ -z "$(git -C .loom/worktrees/pr-996 symbolic-ref -q HEAD || true)" ]] \
    && [[ "$(git -C .loom/worktrees/pr-996 rev-parse HEAD)" == "$P_SHA" ]]; then
    pass "primary-clone holder: pr-996 is detached at the PR head sha"
else
    fail "primary-clone holder: pr-996 is not detached at $P_SHA"
fi
if grep -qx "# PR: 996" ".loom/worktrees/pr-996/.loom-managed" 2>/dev/null; then
    pass "primary-clone holder: .loom-managed sentinel written"
else
    fail "primary-clone holder: .loom-managed sentinel missing"
fi
if [[ "$(git rev-parse --abbrev-ref HEAD)" == "$P_BRANCH" ]]; then
    pass "primary-clone holder: primary clone checkout is untouched"
else
    fail "primary-clone holder: primary clone moved off $P_BRANCH"
fi

# 7b: re-run reuses the existing pr-996 and follows a new PR head sha.
git commit --allow-empty -q -m "pr 996 follow-up"
P_SHA2="$(git rev-parse HEAD)"
set +e
PR_OUT7B=$(PATH="$MOCKBIN4:$PATH" LOOM_DEFAULT_BRANCH=main LOOM_TEST_HEAD_BRANCH="$P_BRANCH" \
    LOOM_TEST_HEAD_SHA="$P_SHA2" LOOM_TEST_PRIMARY="$PRIMARY_ABS" "$PR_WORKTREE_SH" 996 2>&1)
PR_RC7B=$?
set -e
if [[ "$PR_RC7B" -eq 0 ]] && grep -q "reusing" <<<"$PR_OUT7B" \
    && [[ "$(git -C .loom/worktrees/pr-996 rev-parse HEAD)" == "$P_SHA2" ]]; then
    pass "re-run reuses the detached pr-996 and moves it to the new head sha"
else
    fail "re-run did not reuse/refresh pr-996 (rc=$PR_RC7B): $PR_OUT7B"
fi

# 7c: checkout-failure path (gh pr view unresolved); head sha comes from the
#     pull/<N>/head ref fetched from origin.
Q_BRANCH="feature/issue-995"
git checkout -q -b "$Q_BRANCH" main
git commit --allow-empty -q -m "pr 995 change"
Q_SHA="$(git rev-parse HEAD)"
git push -q origin "$Q_SHA:refs/pull/995/head"
set +e
PR_OUT7C=$(PATH="$MOCKBIN4:$PATH" LOOM_DEFAULT_BRANCH=main LOOM_TEST_VIEW=none LOOM_TEST_HEAD_BRANCH="$Q_BRANCH" \
    LOOM_TEST_PRIMARY="$PRIMARY_ABS" "$PR_WORKTREE_SH" 995 2>&1)
PR_RC7C=$?
set -e
if [[ "$PR_RC7C" -eq 0 ]] && [[ "$(git -C .loom/worktrees/pr-995 rev-parse HEAD 2>/dev/null)" == "$Q_SHA" ]] \
    && grep -q "Detached at $Q_SHA" <<<"$PR_OUT7C"; then
    pass "checkout-failure path: primary-clone holder also yields a detached pr-995"
else
    fail "checkout-failure path: expected detached pr-995 at $Q_SHA (rc=$PR_RC7C): $PR_OUT7C"
fi
if [[ "$(git worktree list --porcelain | grep -c 'worktrees/pr-995$')" -eq 1 ]]; then
    pass "checkout-failure path: exactly one pr-995 worktree registered"
else
    fail "checkout-failure path: unexpected pr-995 worktree registrations"
fi
git checkout -q main

# 7d: primary clone AND the builder's issue worktree both hold the branch ->
#     the builder wins: exit 1, builder path named, no detached pr-<N> created.
B_BRANCH="feature/issue-994"
git checkout -q -b "$B_BRANCH" main
git commit --allow-empty -q -m "pr 994 change"
B_SHA="$(git rev-parse HEAD)"
git worktree add -q --force .loom/worktrees/issue-994 "$B_BRANCH"
set +e
PR_OUT7D=$(PATH="$MOCKBIN4:$PATH" LOOM_DEFAULT_BRANCH=main LOOM_TEST_HEAD_BRANCH="$B_BRANCH" \
    LOOM_TEST_HEAD_SHA="$B_SHA" LOOM_TEST_PRIMARY="$PRIMARY_ABS" "$PR_WORKTREE_SH" 994 2>&1)
PR_RC7D=$?
set -e
if [[ "$PR_RC7D" -eq 1 ]] && grep -q "worktrees/issue-994" <<<"$PR_OUT7D" \
    && [[ ! -e .loom/worktrees/pr-994 ]]; then
    pass "builder + primary both hold the branch: exit 1, builder path named, no pr-994 created"
else
    fail "builder + primary: expected exit 1 naming issue-994 and no pr-994 (rc=$PR_RC7D): $PR_OUT7D"
fi
git worktree remove --force .loom/worktrees/issue-994
git checkout -q main

# 7e: existing detached pr-996 is NOT reused once the builder holds the branch.
git worktree add -q .loom/worktrees/issue-996 "$P_BRANCH"
set +e
PR_OUT7E=$(PATH="$MOCKBIN4:$PATH" LOOM_DEFAULT_BRANCH=main LOOM_TEST_HEAD_BRANCH="$P_BRANCH" \
    LOOM_TEST_HEAD_SHA="$P_SHA2" LOOM_TEST_PRIMARY="$PRIMARY_ABS" "$PR_WORKTREE_SH" 996 2>&1)
PR_RC7E=$?
set -e
if [[ "$PR_RC7E" -eq 1 ]] && grep -q "worktrees/issue-996" <<<"$PR_OUT7E"; then
    pass "existing detached pr-996 + builder holder: exit 1, builder path named"
else
    fail "reuse path: expected exit 1 naming issue-996 (rc=$PR_RC7E): $PR_OUT7E"
fi
git worktree remove --force .loom/worktrees/issue-996

# 7f: a failed refresh of the detached pr-996 returns nonzero and keeps the local edit.
git checkout -q -b tmp-14391-b "$P_SHA2"
echo base > t14391.txt; git add t14391.txt; git commit -q -m "base t"
T_B="$(git rev-parse HEAD)"
echo new > t14391.txt; git commit -q -am "new t"
T_C="$(git rev-parse HEAD)"
git checkout -q main
git -C .loom/worktrees/pr-996 checkout -q --detach "$T_B"
echo local > .loom/worktrees/pr-996/t14391.txt
set +e
PR_OUT7F=$(PATH="$MOCKBIN4:$PATH" LOOM_DEFAULT_BRANCH=main LOOM_TEST_HEAD_BRANCH="$P_BRANCH" \
    LOOM_TEST_HEAD_SHA="$T_C" LOOM_TEST_PRIMARY="$PRIMARY_ABS" "$PR_WORKTREE_SH" 996 2>&1)
PR_RC7F=$?
set -e
if [[ "$PR_RC7F" -ne 0 ]] && [[ "$(cat .loom/worktrees/pr-996/t14391.txt)" == "local" ]]; then
    pass "failed detached refresh: nonzero exit and local edit preserved"
else
    fail "failed refresh: expected nonzero + preserved edit (rc=$PR_RC7F): $PR_OUT7F"
fi

# --- Test 8: provisioning hook runs on successful checkouts only (#15045) ---
echo ""
echo "Test 8: pr-worktree.sh provisions checkouts via .loom/hooks/post-worktree.sh (#15045)"

# A recording hook in the PRIMARY checkout (the lookup path worktree.sh uses,
# which is what pr-worktree.sh must mirror — never the PR's own copy): logs
# its cwd, the three hook arguments and the checkout sha, prints a marker on
# stdout (to prove hook stdout is routed to stderr), and can be made to fail
# or vanish per case below.
HOOK_LOG="$TMP/hook.log"
: > "$HOOK_LOG"
mkdir -p .loom/hooks
cat > .loom/hooks/post-worktree.sh <<'HOOKEOF'
#!/usr/bin/env bash
echo "HOOK-STDOUT-MARKER"
printf '%s|%s|%s|%s|%s\n' "$(pwd)" "$1" "$2" "$3" "$(git rev-parse HEAD)" >> "${LOOM_TEST_HOOK_LOG:?}"
exit "${LOOM_TEST_HOOK_RC:-0}"
HOOKEOF
chmod +x .loom/hooks/post-worktree.sh
hook_count() { wc -l < "$HOOK_LOG" | tr -d ' '; }

MOCKBIN5=$(mktemp -d /tmp/loom-pr-iso-mockbin5.XXXXXX)
trap 'rm -rf "$TMP" "$MOCKBIN" "$MOCKBIN2" "$MOCKBIN3" "$MOCKBIN4" "$MOCKBIN5" "${SYM:-}"; cd "$REPO_ROOT" 2>/dev/null || true' EXIT
cat > "$MOCKBIN5/gh" <<'MOCKEOF'
#!/usr/bin/env bash
# Hook-test `gh` stand-in. `pr view` resolves head branch / head sha
# (LOOM_TEST_VIEW=none makes it fail); `pr checkout` either performs a REAL
# git checkout (LOOM_TEST_CHECKOUT_MODE=real), fails silently (=fail), or
# reports the primary-held-branch collision git itself produces (default).
if [[ "$1" == "pr" && "$2" == "view" ]]; then
    [[ "${LOOM_TEST_VIEW:-}" == "none" ]] && exit 1
    if [[ "$*" == *headRefOid* ]]; then echo "$LOOM_TEST_HEAD_SHA"; else echo "$LOOM_TEST_HEAD_BRANCH"; fi
    exit 0
fi
if [[ "$1" == "pr" && "$2" == "checkout" ]]; then
    case "${LOOM_TEST_CHECKOUT_MODE:-}" in
        real) git checkout -q "$LOOM_TEST_HEAD_BRANCH"; exit $? ;;
        fail) exit 1 ;;
    esac
    echo "fatal: '$LOOM_TEST_HEAD_BRANCH' is already used by worktree at '$LOOM_TEST_PRIMARY'" >&2
    exit 1
fi
exit 0
MOCKEOF
chmod +x "$MOCKBIN5/gh"

# 8.0: --help documents the provisioning hook.
HELP_OUT8=$("$PR_WORKTREE_SH" --help 2>&1)
if grep -q "post-worktree\.sh" <<<"$HELP_OUT8"; then
    pass "pr-worktree.sh --help documents the post-worktree hook"
else
    fail "pr-worktree.sh --help does not mention the post-worktree hook"
fi

# 8a: fresh attached create runs the hook exactly once, from the worktree,
#     with <worktree path> <PR head branch> <PR number>; hook stdout is routed
#     to stderr so the worktree path stays the last stdout line.
git checkout -q main
git checkout -q -b fix/hook-fresh
git commit --allow-empty -q -m "pr 983 change"
SHA983="$(git rev-parse HEAD)"
git checkout -q main
set +e
PR_OUT8A=$(PATH="$MOCKBIN5:$PATH" LOOM_DEFAULT_BRANCH=main LOOM_TEST_HOOK_LOG="$HOOK_LOG" \
    LOOM_TEST_HEAD_BRANCH=fix/hook-fresh LOOM_TEST_CHECKOUT_MODE=real "$PR_WORKTREE_SH" 983 2>"$TMP/err-8a")
RC8A=$?
set -e
WT983="$(cd .loom/worktrees/pr-983 && pwd)"
if [[ "$RC8A" -eq 0 ]] && [[ "$(hook_count)" -eq 1 ]]; then
    pass "fresh attached create runs the hook exactly once and exits 0"
else
    fail "fresh attached create: expected rc=0 and exactly one hook record (rc=$RC8A, records=$(hook_count)): $PR_OUT8A"
fi
if grep -qF "${WT983}|${WT983}|fix/hook-fresh|983|${SHA983}" "$HOOK_LOG"; then
    pass "hook runs from the worktree with (path, branch, PR number) and sees the PR head"
else
    fail "hook record missing or wrong (want ${WT983}|${WT983}|fix/hook-fresh|983|${SHA983}): $(cat "$HOOK_LOG")"
fi
if [[ "$(tail -n1 <<<"$PR_OUT8A")" == "$WT983" ]]; then
    pass "worktree path remains the last stdout line"
else
    fail "last stdout line is not the worktree path: $(tail -n1 <<<"$PR_OUT8A")"
fi
if grep -q "HOOK-STDOUT-MARKER" "$TMP/err-8a" && ! grep -q "HOOK-STDOUT-MARKER" <<<"$PR_OUT8A"; then
    pass "hook stdout is routed to stderr, not stdout"
else
    fail "hook stdout leaked to stdout or did not reach stderr"
fi

# 8b: attached reuse with a successful refresh runs the hook again.
set +e
PR_OUT8B=$(PATH="$MOCKBIN5:$PATH" LOOM_DEFAULT_BRANCH=main LOOM_TEST_HOOK_LOG="$HOOK_LOG" \
    LOOM_TEST_HEAD_BRANCH=fix/hook-fresh LOOM_TEST_CHECKOUT_MODE=real "$PR_WORKTREE_SH" 983 2>/dev/null)
RC8B=$?
set -e
if [[ "$RC8B" -eq 0 ]] && [[ "$(hook_count)" -eq 2 ]] \
    && grep -q "Refreshed PR branch" <<<"$PR_OUT8B"; then
    pass "successful attached refresh runs the hook exactly once more"
else
    fail "attached refresh: expected rc=0 and a second hook record (rc=$RC8B, records=$(hook_count)): $PR_OUT8B"
fi

# 8c: attached reuse whose refresh FAILS (the warning fallback) runs no hook.
BEFORE8C="$(hook_count)"
set +e
PR_OUT8C=$(PATH="$MOCKBIN5:$PATH" LOOM_DEFAULT_BRANCH=main LOOM_TEST_HOOK_LOG="$HOOK_LOG" \
    LOOM_TEST_HEAD_BRANCH=fix/hook-fresh LOOM_TEST_CHECKOUT_MODE=fail "$PR_WORKTREE_SH" 983 2>/dev/null)
RC8C=$?
set -e
if [[ "$RC8C" -eq 0 ]] && [[ "$(hook_count)" -eq "$BEFORE8C" ]] \
    && grep -q "continuing with existing checkout" <<<"$PR_OUT8C" \
    && [[ "$(tail -n1 <<<"$PR_OUT8C")" == "$WT983" ]]; then
    pass "failed attached refresh: no hook run, exit 0, path still returned"
else
    fail "failed attached refresh: expected no new hook record, rc=0 and the path (rc=$RC8C, records $(hook_count)): $PR_OUT8C"
fi

# 8d: detached create (primary holds the branch) runs the hook with the head
#     branch, which IS known here via `gh pr view`.
git checkout -q -b fix/hook-detached
git commit --allow-empty -q -m "pr 982 change"
SHA982="$(git rev-parse HEAD)"
BEFORE8D="$(hook_count)"
set +e
PR_OUT8D=$(PATH="$MOCKBIN5:$PATH" LOOM_DEFAULT_BRANCH=main LOOM_TEST_HOOK_LOG="$HOOK_LOG" \
    LOOM_TEST_HEAD_BRANCH=fix/hook-detached LOOM_TEST_HEAD_SHA="$SHA982" \
    LOOM_TEST_PRIMARY="$PRIMARY_ABS" "$PR_WORKTREE_SH" 982 2>/dev/null)
RC8D=$?
set -e
WT982="$(cd .loom/worktrees/pr-982 && pwd)"
if [[ "$RC8D" -eq 0 ]] && [[ "$(hook_count)" -eq $((BEFORE8D + 1)) ]] \
    && grep -qF "${WT982}|${WT982}|fix/hook-detached|982|${SHA982}" "$HOOK_LOG"; then
    pass "detached create runs the hook once with (path, head branch, PR number, sha)"
else
    fail "detached create: hook record wrong or missing (rc=$RC8D, want ${WT982}|${WT982}|fix/hook-detached|982|${SHA982}): $(tail -1 "$HOOK_LOG") / $PR_OUT8D"
fi

# 8e: detached reuse refresh to a new head sha runs the hook again.
git commit --allow-empty -q -m "pr 982 follow-up"
SHA982B="$(git rev-parse HEAD)"
BEFORE8E="$(hook_count)"
set +e
PR_OUT8E=$(PATH="$MOCKBIN5:$PATH" LOOM_DEFAULT_BRANCH=main LOOM_TEST_HOOK_LOG="$HOOK_LOG" \
    LOOM_TEST_HEAD_BRANCH=fix/hook-detached LOOM_TEST_HEAD_SHA="$SHA982B" \
    LOOM_TEST_PRIMARY="$PRIMARY_ABS" "$PR_WORKTREE_SH" 982 2>/dev/null)
RC8E=$?
set -e
if [[ "$RC8E" -eq 0 ]] && [[ "$(hook_count)" -eq $((BEFORE8E + 1)) ]] \
    && grep -qF "${WT982}|${WT982}|fix/hook-detached|982|${SHA982B}" "$HOOK_LOG"; then
    pass "successful detached refresh runs the hook once at the new sha"
else
    fail "detached refresh: expected one new hook record at $SHA982B (rc=$RC8E, records=$(hook_count)): $PR_OUT8E"
fi

# 8f: detached create with branch metadata UNAVAILABLE (checkout-failure
#     path, `gh pr view` fails) derives the branch argument from the checkout:
#     "HEAD" when detached.
git checkout -q -b fix/hook-detached2
git commit --allow-empty -q -m "pr 981 change"
SHA981="$(git rev-parse HEAD)"
git push -q origin "$SHA981:refs/pull/981/head"
BEFORE8F="$(hook_count)"
set +e
PR_OUT8F=$(PATH="$MOCKBIN5:$PATH" LOOM_DEFAULT_BRANCH=main LOOM_TEST_HOOK_LOG="$HOOK_LOG" \
    LOOM_TEST_VIEW=none LOOM_TEST_PRIMARY="$PRIMARY_ABS" "$PR_WORKTREE_SH" 981 2>/dev/null)
RC8F=$?
set -e
WT981="$(cd .loom/worktrees/pr-981 && pwd)"
if [[ "$RC8F" -eq 0 ]] && [[ "$(hook_count)" -eq $((BEFORE8F + 1)) ]] \
    && grep -qF "${WT981}|${WT981}|HEAD|981|${SHA981}" "$HOOK_LOG"; then
    pass "detached create without branch metadata passes branch arg 'HEAD'"
else
    fail "detached create (view=none): expected a hook record with branch 'HEAD' at $SHA981 (rc=$RC8F, records=$(hook_count)): $PR_OUT8F"
fi

# 8g: FAILED detached refresh (local edit blocks the move) runs no hook.
git checkout -q -b tmp-hook-b main
echo base > thook.txt; git add thook.txt; git commit -q -m "hook base"
TH_B="$(git rev-parse HEAD)"
echo new > thook.txt; git commit -q -am "hook new"
TH_C="$(git rev-parse HEAD)"
git checkout -q fix/hook-detached
git -C .loom/worktrees/pr-982 checkout -q --detach "$TH_B"
echo local > .loom/worktrees/pr-982/thook.txt
BEFORE8G="$(hook_count)"
set +e
PR_OUT8G=$(PATH="$MOCKBIN5:$PATH" LOOM_DEFAULT_BRANCH=main LOOM_TEST_HOOK_LOG="$HOOK_LOG" \
    LOOM_TEST_HEAD_BRANCH=fix/hook-detached LOOM_TEST_HEAD_SHA="$TH_C" \
    LOOM_TEST_PRIMARY="$PRIMARY_ABS" "$PR_WORKTREE_SH" 982 2>/dev/null)
RC8G=$?
set -e
if [[ "$RC8G" -ne 0 ]] && [[ "$(hook_count)" -eq "$BEFORE8G" ]]; then
    pass "failed detached refresh: nonzero exit and no hook run"
else
    fail "failed detached refresh: expected rc!=0 and no new hook record (rc=$RC8G, records=$(hook_count)): $PR_OUT8G"
fi

# 8h: a FAILING hook only warns on stderr; checkout success is preserved.
git checkout -q main
git checkout -q -b fix/hook-failing
git commit --allow-empty -q -m "pr 980 change"
git checkout -q main
BEFORE8H="$(hook_count)"
set +e
PR_OUT8H=$(PATH="$MOCKBIN5:$PATH" LOOM_DEFAULT_BRANCH=main LOOM_TEST_HOOK_LOG="$HOOK_LOG" \
    LOOM_TEST_HOOK_RC=1 LOOM_TEST_HEAD_BRANCH=fix/hook-failing \
    LOOM_TEST_CHECKOUT_MODE=real "$PR_WORKTREE_SH" 980 2>"$TMP/err-8h")
RC8H=$?
set -e
WT980="$(cd .loom/worktrees/pr-980 && pwd)"
if [[ "$RC8H" -eq 0 ]] && [[ "$(hook_count)" -eq $((BEFORE8H + 1)) ]] \
    && grep -q "hook failed" "$TMP/err-8h" \
    && [[ "$(tail -n1 <<<"$PR_OUT8H")" == "$WT980" ]]; then
    pass "failing hook: warns on stderr, exit 0, worktree path still the last stdout line"
else
    fail "failing hook: expected rc=0 + stderr warning + path on stdout (rc=$RC8H): $PR_OUT8H / $(cat "$TMP/err-8h")"
fi

# 8i: a MISSING hook is skipped silently (no warning, still success).
mv .loom/hooks/post-worktree.sh .loom/hooks/post-worktree.sh.hidden
git checkout -q -b fix/hook-missing
git commit --allow-empty -q -m "pr 979 change"
git checkout -q main
BEFORE8I="$(hook_count)"
set +e
PR_OUT8I=$(PATH="$MOCKBIN5:$PATH" LOOM_DEFAULT_BRANCH=main LOOM_TEST_HOOK_LOG="$HOOK_LOG" \
    LOOM_TEST_HEAD_BRANCH=fix/hook-missing LOOM_TEST_CHECKOUT_MODE=real "$PR_WORKTREE_SH" 979 2>"$TMP/err-8i")
RC8I=$?
set -e
mv .loom/hooks/post-worktree.sh.hidden .loom/hooks/post-worktree.sh
if [[ "$RC8I" -eq 0 ]] && [[ "$(hook_count)" -eq "$BEFORE8I" ]] \
    && ! grep -qi "hook" "$TMP/err-8i"; then
    pass "missing hook: skipped silently, exit 0"
else
    fail "missing hook: expected silent skip and rc=0 (rc=$RC8I): $PR_OUT8I / $(cat "$TMP/err-8i")"
fi

# 8j: a NON-EXECUTABLE hook is skipped silently.
git checkout -q -b fix/hook-noexec
git commit --allow-empty -q -m "pr 978 change"
git checkout -q main
chmod -x .loom/hooks/post-worktree.sh
BEFORE8J="$(hook_count)"
set +e
PR_OUT8J=$(PATH="$MOCKBIN5:$PATH" LOOM_DEFAULT_BRANCH=main LOOM_TEST_HOOK_LOG="$HOOK_LOG" \
    LOOM_TEST_HEAD_BRANCH=fix/hook-noexec LOOM_TEST_CHECKOUT_MODE=real "$PR_WORKTREE_SH" 978 2>"$TMP/err-8j")
RC8J=$?
set -e
chmod +x .loom/hooks/post-worktree.sh
if [[ "$RC8J" -eq 0 ]] && [[ "$(hook_count)" -eq "$BEFORE8J" ]] \
    && ! grep -qi "hook" "$TMP/err-8j"; then
    pass "non-executable hook: skipped silently, exit 0"
else
    fail "non-executable hook: expected silent skip and rc=0 (rc=$RC8J): $PR_OUT8J / $(cat "$TMP/err-8j")"
fi

# 8k: builder-held branch (exit 1, no checkout created) runs no hook.
git checkout -q -b feature/issue-977
git commit --allow-empty -q -m "pr 977 change"
git checkout -q main
git worktree add -q .loom/worktrees/issue-977 feature/issue-977
BEFORE8K="$(hook_count)"
set +e
PR_OUT8K=$(PATH="$MOCKBIN5:$PATH" LOOM_DEFAULT_BRANCH=main LOOM_TEST_HOOK_LOG="$HOOK_LOG" \
    LOOM_TEST_HEAD_BRANCH=feature/issue-977 "$PR_WORKTREE_SH" 977 2>/dev/null)
RC8K=$?
set -e
git worktree remove --force .loom/worktrees/issue-977
if [[ "$RC8K" -eq 1 ]] && [[ "$(hook_count)" -eq "$BEFORE8K" ]]; then
    pass "builder-held branch: exit 1 and no hook run"
else
    fail "builder-held branch: expected rc=1 and no new hook record (rc=$RC8K, records=$(hook_count)): $PR_OUT8K"
fi

# --- Test 9: builder_holder compares physical paths (#15121) ---
echo ""
echo "Test 9: pr-worktree.sh re-enters its own attached pr-<N> through a symlinked repo path (#15121)"

# `git worktree list --porcelain` prints physical spellings while
# pr-worktree.sh's WORKTREE_PATH keeps the logical spelling the repo was
# reached by; a string compare then reports the PR's own pr-<N> as a foreign
# builder holder and re-entry dies with the builder-wins error (CI Daily run
# 45250283620, macOS /tmp -> /private/tmp). On Linux the same spelling split
# is built by giving the repo a real location whose absolute path literally
# ends with the symlink's own path string — the /tmp-into-/private/tmp shape —
# so the reuse gate's unanchored grep still matches the physical spelling
# while the porcelain compare splits, exactly as on macOS.
SYM=$(mktemp -d /tmp/loom-pr-iso-sym.XXXXXX)
SYM_BASE=$(basename "$SYM")
SYM_PHYS="$SYM/priv/tmp/$SYM_BASE/repo"
mkdir -p "$SYM_PHYS"
# git prints the physical spelling in porcelain output; on macOS /tmp is a
# symlink to /private/tmp, so the 9a expectation must be canonicalised too.
SYM_PHYS_REAL="$(cd "$SYM_PHYS" && pwd -P)"
git init -q -b main "$SYM/origin.git" --bare
git -C "$SYM_PHYS" init -q -b main
git -C "$SYM_PHYS" config user.email t@t
git -C "$SYM_PHYS" config user.name t
git -C "$SYM_PHYS" commit --allow-empty -q -m init
git -C "$SYM_PHYS" remote add origin "$SYM/origin.git"
git -C "$SYM_PHYS" push -q origin main
ln -s "$SYM_PHYS" "$SYM/repo"
mkdir -p "$SYM/repo/.loom/hooks"
cat > "$SYM/repo/.loom/hooks/post-worktree.sh" <<'HOOKEOF'
#!/usr/bin/env bash
printf '%s|%s|%s|%s|%s\n' "$(pwd)" "$1" "$2" "$3" "$(git rev-parse HEAD)" >> "${LOOM_TEST_HOOK_LOG:?}"
exit 0
HOOKEOF
chmod +x "$SYM/repo/.loom/hooks/post-worktree.sh"

# 9a: create an attached pr-991 through the symlinked path (sets up 9b).
git -C "$SYM_PHYS" checkout -q -b fix/symlink-reentry
git -C "$SYM_PHYS" commit --allow-empty -q -m "pr 991 change"
git -C "$SYM_PHYS" checkout -q main
set +e
PR_OUT9A=$(cd "$SYM/repo" && PATH="$MOCKBIN5:$PATH" LOOM_DEFAULT_BRANCH=main \
    LOOM_TEST_HOOK_LOG="$HOOK_LOG" LOOM_TEST_HEAD_BRANCH=fix/symlink-reentry \
    LOOM_TEST_CHECKOUT_MODE=real "$PR_WORKTREE_SH" 991 2>/dev/null)
RC9A=$?
set -e
WT991="$SYM/repo/.loom/worktrees/pr-991"
if [[ "$RC9A" -eq 0 ]] && [[ -d "$WT991" ]] \
    && git -C "$SYM_PHYS" worktree list --porcelain | grep -q "worktree $SYM_PHYS_REAL/.loom/worktrees/pr-991$"; then
    pass "symlinked repo path: attached pr-<N> created (git registers the physical spelling)"
else
    fail "symlinked repo path: attached create failed (rc=$RC9A): $PR_OUT9A"
fi

# 9b: RE-ENTER the PR's own attached pr-991 through the same symlinked path —
#     the macOS Test 8b/8c failure. Must reuse + refresh (rc=0, one more hook
#     record), not die on the builder-wins error.
BEFORE9B="$(hook_count)"
set +e
PR_OUT9B=$(cd "$SYM/repo" && PATH="$MOCKBIN5:$PATH" LOOM_DEFAULT_BRANCH=main \
    LOOM_TEST_HOOK_LOG="$HOOK_LOG" LOOM_TEST_HEAD_BRANCH=fix/symlink-reentry \
    LOOM_TEST_CHECKOUT_MODE=real "$PR_WORKTREE_SH" 991 2>/dev/null)
RC9B=$?
set -e
if [[ "$RC9B" -eq 0 ]] && [[ "$(hook_count)" -eq $((BEFORE9B + 1)) ]] \
    && grep -q "already exists at $WT991 (reusing)" <<<"$PR_OUT9B" \
    && ! grep -q "Reuse the existing worktree directly" <<<"$PR_OUT9B" \
    && [[ "$(tail -n1 <<<"$PR_OUT9B")" == "$WT991" ]]; then
    pass "symlinked repo path: re-entering the PR's own attached pr-<N> reuses it (rc=0)"
else
    fail "symlinked re-entry: expected rc=0, reuse message and one hook record (rc=$RC9B, records=$(hook_count)): $PR_OUT9B"
fi

# 9c: a GENUINE builder holder is still refused through the symlinked path.
git -C "$SYM_PHYS" checkout -q -b feature/issue-992
git -C "$SYM_PHYS" commit --allow-empty -q -m "pr 992 change"
git -C "$SYM_PHYS" checkout -q main
git -C "$SYM_PHYS" worktree add -q "$SYM_PHYS/.loom/worktrees/issue-992" feature/issue-992
BEFORE9C="$(hook_count)"
set +e
PR_OUT9C=$(cd "$SYM/repo" && PATH="$MOCKBIN5:$PATH" LOOM_DEFAULT_BRANCH=main \
    LOOM_TEST_HOOK_LOG="$HOOK_LOG" LOOM_TEST_HEAD_BRANCH=feature/issue-992 \
    "$PR_WORKTREE_SH" 992 2>/dev/null)
RC9C=$?
set -e
git -C "$SYM_PHYS" worktree remove --force "$SYM_PHYS/.loom/worktrees/issue-992"
if [[ "$RC9C" -eq 1 ]] && [[ "$(hook_count)" -eq "$BEFORE9C" ]] \
    && grep -q "Reuse the existing worktree directly" <<<"$PR_OUT9C"; then
    pass "symlinked repo path: genuine builder holder still refused (rc=1)"
else
    fail "symlinked genuine holder: expected rc=1 and no hook record (rc=$RC9C, records=$(hook_count)): $PR_OUT9C"
fi

# --- Test 10: the reuse gate matches registered worktrees by canonicalized
#     physical path, not substring (#15196) ---
echo ""
echo "Test 10: the pr-<N> reuse gate matches canonicalized physical paths, not substrings (#15196)"

# `gh` stand-in for Test 10: `pr view` resolves the head branch / head sha;
# `pr checkout` records that it ran (the marker lets the prefix-collision case
# assert the gate rejected an unregistered directory BEFORE any checkout) and
# fails the way git does when the primary clone holds the branch — steering
# re-entry of the registered DETACHED worktrees below through the #14391
# refresh path, which does not trip builder_holder (fixed separately in #15121).
MOCKBIN6=$(mktemp -d /tmp/loom-pr-iso-mockbin6.XXXXXX)
trap 'rm -rf "$TMP" "$MOCKBIN" "$MOCKBIN2" "$MOCKBIN3" "$MOCKBIN4" "$MOCKBIN5" "$MOCKBIN6"; cd "$REPO_ROOT" 2>/dev/null || true' EXIT
cat > "$MOCKBIN6/gh" <<'MOCKEOF'
#!/usr/bin/env bash
if [[ "$1" == "pr" && "$2" == "view" ]]; then
    if [[ "$*" == *headRefOid* ]]; then echo "$LOOM_TEST_HEAD_SHA"; else echo "$LOOM_TEST_HEAD_BRANCH"; fi
    exit 0
fi
if [[ "$1" == "pr" && "$2" == "checkout" ]]; then
    touch "$LOOM_TEST_CHECKOUT_MARKER"
    echo "fatal: '$LOOM_TEST_HEAD_BRANCH' is already used by worktree at '$LOOM_TEST_PRIMARY'" >&2
    exit 1
fi
exit 0
MOCKEOF
chmod +x "$MOCKBIN6/gh"
HOOK_LOG10="$TMP/hook9.log"
: > "$HOOK_LOG10"

# 10a: re-entry of a registered detached pr-971 through a symlinked repo path
#     whose physical location is not a suffix of the logical spelling (the
#     space in the symlink dir also exercises path quoting): `git worktree
#     add` registers the PHYSICAL path while REPO_ROOT/WORKTREE_PATH stay
#     logical, so the pre-fix unanchored grep missed it (#15196 false
#     negative) and advised rm -rf on a live worktree.
git checkout -q -b fix/sym-reentry
git commit --allow-empty -q -m "pr 971 change"
SHA971="$(git rev-parse HEAD)"
git checkout -q main
mkdir -p "$TMP/sp ace"
ln -s "$TMP/repo" "$TMP/sp ace/repo"
LOGICAL971="$TMP/sp ace/repo/.loom/worktrees/pr-971"
git worktree add -q --detach "$LOGICAL971" "$SHA971"
MARKER10A="$TMP/checkout-10a"
rm -f "$MARKER10A"
set +e
PR_OUT10A=$(cd "$TMP/sp ace/repo" && PATH="$MOCKBIN6:$PATH" LOOM_DEFAULT_BRANCH=main \
    LOOM_TEST_HEAD_BRANCH=fix/sym-reentry LOOM_TEST_HEAD_SHA="$SHA971" \
    LOOM_TEST_PRIMARY="$TMP/repo" LOOM_TEST_CHECKOUT_MARKER="$MARKER10A" \
    LOOM_TEST_HOOK_LOG="$HOOK_LOG10" "$PR_WORKTREE_SH" 971 2>"$TMP/err-10a")
RC10A=$?
set -e
ERR10A="$(cat "$TMP/err-10a")"
if [[ "$RC10A" -eq 0 ]] && grep -q "reusing" <<<"$PR_OUT10A" \
    && ! grep -q "not a registered worktree" <<<"$PR_OUT10A$ERR10A" \
    && ! grep -q "rm -rf" <<<"$PR_OUT10A$ERR10A"; then
    pass "symlinked re-entry reuses the registered pr-971 (no refusal, no rm -rf hint)"
else
    fail "symlinked re-entry: expected rc=0 reuse without refusal (rc=$RC10A): $PR_OUT10A / $ERR10A"
fi
if [[ "$(tail -n1 <<<"$PR_OUT10A")" == "$LOGICAL971" ]]; then
    pass "symlinked re-entry prints the logical worktree path as the last stdout line"
else
    fail "last stdout line is not the logical path ($LOGICAL971): $(tail -n1 <<<"$PR_OUT10A")"
fi
if [[ "$(git -C "$TMP/repo/.loom/worktrees/pr-971" rev-parse HEAD 2>/dev/null)" == "$SHA971" ]] \
    && [[ "$(git -C "$TMP/repo" worktree list --porcelain | grep -c 'worktrees/pr-971$')" -eq 1 ]]; then
    pass "symlinked re-entry leaves pr-971 registered and refreshed at the PR head sha"
else
    fail "pr-971 not registered/refreshed at $SHA971 after symlinked re-entry"
fi

# 10b: same when only LOOM_WORKTREE_ROOT traverses a symlink; a stale sibling
#     entry (registered but missing on disk, no `git worktree prune`) must not
#     abort the scan.
git checkout -q -b fix/sym-root
git commit --allow-empty -q -m "pr 970 change"
SHA970="$(git rev-parse HEAD)"
git checkout -q main
mkdir -p "$TMP/realwt"
ln -s "$TMP/realwt" "$TMP/linkwt"
git worktree add -q --detach "$TMP/linkwt/repo/pr-970" "$SHA970"
git worktree add -q --detach "$TMP/linkwt/repo/pr-969" "$SHA970"
rm -rf "$TMP/realwt/repo/pr-969"
MARKER10B="$TMP/checkout-10b"
rm -f "$MARKER10B"
set +e
PR_OUT10B=$(cd "$TMP/repo" && PATH="$MOCKBIN6:$PATH" LOOM_DEFAULT_BRANCH=main \
    LOOM_WORKTREE_ROOT="$TMP/linkwt" \
    LOOM_TEST_HEAD_BRANCH=fix/sym-root LOOM_TEST_HEAD_SHA="$SHA970" \
    LOOM_TEST_PRIMARY="$TMP/repo" LOOM_TEST_CHECKOUT_MARKER="$MARKER10B" \
    LOOM_TEST_HOOK_LOG="$HOOK_LOG10" "$PR_WORKTREE_SH" 970 2>"$TMP/err-10b")
RC10B=$?
set -e
ERR10B="$(cat "$TMP/err-10b")"
if [[ "$RC10B" -eq 0 ]] && grep -q "reusing" <<<"$PR_OUT10B" \
    && ! grep -q "not a registered worktree" <<<"$PR_OUT10B$ERR10B"; then
    pass "symlinked LOOM_WORKTREE_ROOT re-entry reuses the registered pr-970"
else
    fail "symlinked LOOM_WORKTREE_ROOT: expected rc=0 reuse (rc=$RC10B): $PR_OUT10B / $ERR10B"
fi
if [[ "$(git -C "$TMP/realwt/repo/pr-970" rev-parse HEAD 2>/dev/null)" == "$SHA970" ]]; then
    pass "symlinked-root re-entry refreshes pr-970 at the PR head sha (stale sibling skipped)"
else
    fail "pr-970 not refreshed at $SHA970 through the symlinked root"
fi

# 10c: an unregistered directory whose path prefixes a REGISTERED worktree
#     (unregistered pr-97 beside registered pr-970, both spelled physically so
#     the listing line really contains the pattern) must be rejected: the
#     pre-fix unanchored grep matched pr-970's line and "reused" the debris,
#     running gh pr checkout inside it (#15196 false positive).
mkdir -p "$TMP/realwt/repo/pr-97"
MARKER10C="$TMP/checkout-10c"
rm -f "$MARKER10C"
set +e
PR_OUT10C=$(cd "$TMP/repo" && PATH="$MOCKBIN6:$PATH" LOOM_DEFAULT_BRANCH=main \
    LOOM_WORKTREE_ROOT="$TMP/realwt" \
    LOOM_TEST_HEAD_BRANCH=fix/sym-root LOOM_TEST_HEAD_SHA="$SHA970" \
    LOOM_TEST_PRIMARY="$TMP/repo" LOOM_TEST_CHECKOUT_MARKER="$MARKER10C" \
    LOOM_TEST_HOOK_LOG="$HOOK_LOG10" "$PR_WORKTREE_SH" 97 2>"$TMP/err-10c")
RC10C=$?
set -e
ERR10C="$(cat "$TMP/err-10c")"
if [[ "$RC10C" -eq 1 ]] && grep -q "not a registered worktree" <<<"$PR_OUT10C$ERR10C"; then
    pass "unregistered prefix-colliding pr-97 is rejected as unregistered"
else
    fail "prefix collision: expected rc=1 'not a registered worktree' (rc=$RC10C): $PR_OUT10C / $ERR10C"
fi
if [[ ! -f "$MARKER10C" ]] && [[ -d "$TMP/realwt/repo/pr-97" ]] \
    && [[ "$(git -C "$TMP/realwt/repo/pr-970" rev-parse HEAD 2>/dev/null)" == "$SHA970" ]]; then
    pass "prefix collision: no gh pr checkout ran, git state and the debris dir untouched"
else
    fail "prefix collision: checkout ran or git state changed (marker present: $([[ -f "$MARKER10C" ]] && echo yes || echo no))"
fi

cd "$REPO_ROOT"

# --- Test 6: Judge guidance and helper --help agree on the fallback (#11194) ---
echo ""
echo "Test 6: judge.md and pr-worktree.sh --help agree (no fabricated judge-* checkout)"
JUDGE_MD="$REPO_ROOT/defaults/.claude/commands/loom/judge.md"
[[ -f "$REPO_ROOT/.claude/commands/loom/judge.md" ]] && JUDGE_MD="$REPO_ROOT/.claude/commands/loom/judge.md"
HELP_OUT=$("$PR_WORKTREE_SH" --help 2>&1)
assert_grep "pr-worktree\.sh <PR_NUMBER>" "$JUDGE_MD" \
    "judge.md routes the no-builder-checkout case through pr-worktree.sh"
assert_grep "Never.*invent a .judge-\*" "$JUDGE_MD" \
    "judge.md forbids inventing a judge-* checkout after a helper failure"
if grep -qE "Judge's supported review checkout" <<<"$HELP_OUT" \
    && grep -qE "names the holder" <<<"$HELP_OUT" \
    && grep -qE "invent a judge-\* directory" <<<"$HELP_OUT"; then
    pass "pr-worktree.sh --help states the Judge fallback and its collision failure behavior"
else
    fail "pr-worktree.sh --help does not match the Judge fallback guidance"
fi
if grep -qE "use worktree\.sh instead" <<<"$HELP_OUT"; then
    fail "pr-worktree.sh --help still redirects issue PRs to worktree.sh"
else
    pass "pr-worktree.sh --help no longer contradicts judge.md"
fi

# --- Test 8: run the documented selection against disposable checkouts ---
echo ""
echo "Test 8: Judge and Doctor check head repository metadata before selection"
SELECT="$TMP/selection"
mkdir -p "$SELECT/bin"
cat > "$SELECT/bin/gh" <<'EOF'
#!/usr/bin/env bash
case "$*" in
    "pr view 71 --repo fixture/loom --json isCrossRepository,headRefName,headRepository")
        [[ "$FIXTURE_JSON" != unreadable ]] || exit 1
        printf '%s\n' "$FIXTURE_JSON"
        ;;
    "pr view 71 --json headRefName --jq .headRefName")
        jq -r .headRefName <<<"$FIXTURE_JSON"
        ;;
    "pr checkout 71 --repo fixture/loom")
        printf 'checkout %s\n' "${PWD##*/}" >>"$FIXTURE_CALLS"
        ;;
    *)
        printf 'unexpected forge command\n' >>"$FIXTURE_CALLS"
        exit 1
        ;;
esac
EOF
chmod +x "$SELECT/bin/gh"

for role in judge doctor; do
    prompt="$REPO_ROOT/defaults/.claude/commands/loom/$role.md"
    [[ -f "$prompt" ]] || prompt="$REPO_ROOT/.claude/commands/loom/$role.md"
    awk '/^# PR worktree selection$/{capture=1;next}
         /^# End PR worktree selection$/{exit}
         capture && /^```$/{fence=1;next}
         capture && /^```bash$/{fence=0;next}
         capture && !fence' "$prompt" >"$SELECT/$role.sh"
    cat >>"$SELECT/$role.sh" <<'EOF'
printf 'entered %s\n' "${PWD##*/}" >>"$FIXTURE_CALLS"
EOF
done

run_selection() {
    local role="$1" name="$2" json="$3" holder="$4" alternate="$5" expected="$6"
    local repo="$SELECT/$role-$name" calls rc=0 main
    mkdir -p "$repo/.loom/scripts" "$repo/.loom/worktrees"
    git init -q -b main "$repo"
    git -C "$repo" -c user.name=Fixture -c user.email=fixture@example.invalid -c commit.gpgsign=false \
        commit -q --allow-empty -m fixture
    git -C "$repo" remote add origin "$TMP/origin.git"
    git -C "$repo" branch feature/issue-71
    main="$(git -C "$repo" rev-parse HEAD)"
    ln -s "$SCRIPTS_DIR/lib" "$repo/.loom/scripts/lib"
    if [[ "$holder" = present ]]; then
        git -C "$repo" worktree add -q "$repo/.loom/worktrees/issue-71" feature/issue-71
    fi
    cat >"$repo/.loom/scripts/worktree.sh" <<'EOF'
#!/usr/bin/env bash
set -e
printf 'issue helper\n' >>"$FIXTURE_CALLS"
if [[ ! -d .loom/worktrees/issue-71 ]]; then
    git worktree add -q .loom/worktrees/issue-71 feature/issue-71
fi
EOF
    cat >"$repo/.loom/scripts/pr-worktree.sh" <<'EOF'
#!/usr/bin/env bash
set -e
printf 'PR helper\n' >>"$FIXTURE_CALLS"
case "$FIXTURE_ALT" in
    real) exec "$FIXTURE_PR_HELPER" "$@" ;;
    failed) exit 1 ;;
    detached) git worktree add -q --detach .loom/worktrees/pr-71 HEAD ;;
    *) git worktree add -q -b fixture-pr .loom/worktrees/pr-71 HEAD ;;
esac
EOF
    chmod +x "$repo/.loom/scripts/"*.sh
    calls="$repo/calls"
    (cd "$repo" && PATH="$SELECT/bin:$PATH" PR_NUMBER=71 PR_REPO=fixture/loom \
        LOOM_WORKTREE_ROOT='' LOOM_DEFAULT_BRANCH=main \
        FIXTURE_JSON="$json" FIXTURE_CALLS="$calls" FIXTURE_ALT="$alternate" \
        FIXTURE_PR_HELPER="$PR_WORKTREE_SH" bash "$SELECT/$role.sh") >"$repo/trace" 2>&1 || rc=$?
    assert_eq "$expected" "$(cat "$calls" 2>/dev/null || true)" "$role $name records only the expected actions"
    assert_eq "$main" "$(git -C "$repo" rev-parse HEAD)" "$role $name preserves the primary HEAD"
    if [[ "$expected" = *"entered "* ]]; then
        assert_eq 0 "$rc" "$role $name succeeds"
    else
        assert_eq 1 "$rc" "$role $name stops before proceeding"
    fi
    if [[ "$holder" = present ]]; then
        assert_eq "$main" "$(git -C "$repo/.loom/worktrees/issue-71" rev-parse HEAD)" \
            "$role $name preserves the existing issue HEAD"
    fi
}
same='{"headRefName":"feature/issue-71","isCrossRepository":false,"headRepository":{"nameWithOwner":"Fixture/Loom"}}'
fork='{"headRefName":"feature/issue-71","isCrossRepository":true,"headRepository":{"nameWithOwner":"another/loom"}}'
ad_hoc='{"headRefName":"fix/ad-hoc","isCrossRepository":false,"headRepository":{"nameWithOwner":"fixture/loom"}}'
# Match the shared Forge slug validator's per-segment bounds and dot rules.
long_segment=$(printf '%101s' '' | tr ' ' 'a')
max_segment=${long_segment%a}
fork_named() {
    jq -cn --arg slug "$1" '{headRefName:"feature/issue-71",isCrossRepository:true,headRepository:{nameWithOwner:$slug}}'
}
for role in judge doctor; do
    run_selection "$role" fork-collision "$fork" present real 'PR helper'
    run_selection "$role" valid-dotted-repo "$(fork_named 'another/.github')" absent ready $'PR helper\nentered pr-71'
    run_selection "$role" max-length-slug "$(fork_named "$max_segment/$max_segment")" absent ready $'PR helper\nentered pr-71'
    run_selection "$role" invalid-owner-dot "$(fork_named '.team/loom')" present ready ''
    run_selection "$role" invalid-owner-hyphen "$(fork_named '-team/loom')" present ready ''
    run_selection "$role" invalid-repo-hyphen "$(fork_named 'another/-loom')" present ready ''
    run_selection "$role" invalid-repo-single-dot "$(fork_named 'another/.')" present ready ''
    run_selection "$role" invalid-repo-double-dot "$(fork_named 'another/..')" present ready ''
    run_selection "$role" overlong-owner "$(fork_named "$long_segment/loom")" present ready ''
    run_selection "$role" overlong-repo "$(fork_named "another/$long_segment")" present ready ''
    run_selection "$role" fork-without-holder "$fork" absent ready $'PR helper\nentered pr-71'
    run_selection "$role" ad-hoc "$ad_hoc" absent ready $'PR helper\nentered pr-71'
    run_selection "$role" helper-failure "$fork" absent failed 'PR helper'
    run_selection "$role" unreadable unreadable absent ready ''
    run_selection "$role" malformed-json '{' absent ready ''
    run_selection "$role" absent-branch '{"isCrossRepository":false,"headRepository":{"nameWithOwner":"fixture/loom"}}' present ready ''
    run_selection "$role" absent-flag '{"headRefName":"feature/issue-71","headRepository":{"nameWithOwner":"fixture/loom"}}' present ready ''
    run_selection "$role" absent-repository '{"headRefName":"feature/issue-71","isCrossRepository":false}' present ready ''
    run_selection "$role" malformed-repository "${same/Fixture\/Loom/Fixture}" present ready ''
    run_selection "$role" inconsistent-same "${fork/true/false}" present ready ''
    run_selection "$role" inconsistent-cross "${same/false/true}" present ready ''
    run_selection "$role" malformed-boolean '{"headRefName":"feature/issue-71","isCrossRepository":"false","headRepository":{"nameWithOwner":"fixture/loom"}}' present ready ''
    run_selection "$role" invalid-ref "${same/issue-71/issue-71..}" present ready ''
done
run_selection judge existing-managed "$same" present ready 'entered issue-71'
run_selection judge missing-managed "$same" absent ready $'PR helper\nentered pr-71'
run_selection doctor managed "$same" absent ready $'issue helper\ncheckout issue-71\nentered issue-71'
run_selection judge detached "$ad_hoc" absent detached $'PR helper\nentered pr-71'
run_selection doctor detached "$ad_hoc" absent detached 'PR helper'

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
