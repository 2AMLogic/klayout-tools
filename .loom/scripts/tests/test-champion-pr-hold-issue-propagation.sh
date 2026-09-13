#!/usr/bin/env bash
# test-champion-pr-hold-issue-propagation.sh - Regression tests for the
# PR->issue derived loom:blocked propagation (#1749).
#
# Champion's criterion #2 (merge-risk hold) and criterion #3 (critical-file
# hold) both apply `loom:operator` to a PR when it cannot be auto-merged —
# but that label lives on the PR, not on the issue(s) the PR would close.
# `loom-daemon`'s work-finder has no signal from that alone, so it kept
# re-dispatching a no-op sweep on the linked issue every cycle, even though
# the sweep could only ever re-derive "sole open PR is held, skip" — a real,
# confirmed recurrence on issue #1651 / PR #1659 (three separate no-op
# sweeps, 2026-09-12/13) that this issue was filed to end.
#
# The fix (`.claude/commands/loom/champion-pr-merge.md`, "Propagating a hold
# to linked issues", a klayout-tools-local customization pinned via
# `.loom/resync-ignore`): when either hold applies `loom:operator` to a PR,
# mirror it onto the issue(s) named by the PR's `closingIssuesReferences` as
# `loom:blocked` (a label `loom-daemon`'s `PARK_LABELS` already hard-skips,
# so this needs zero daemon-side change) with a machine-parseable
# `Blocked by #<PR>` comment. When the hold releases, the propagation is
# reversed — but ONLY for an issue this exact mechanism itself blocked; an
# issue already `loom:blocked` for an unrelated reason is never touched in
# either direction.
#
# Champion's hold-propagation logic is prose an LLM instance reads and
# executes, not a standalone script (same situation as
# test-champion-critical-file-check.sh and test-dependency-parse.sh) — so
# this file mirrors the documented `propagate_pr_hold()` / `release_pr_hold()`
# functions in local shell functions (using in-memory associative arrays as
# a forge stand-in) and pins the shipped markdown's exact commands with
# `assert_doc_contains`, catching drift between the two.
#
# This file asserts:
#   1. A fresh hold propagates loom:blocked + a Blocked by #<PR> comment to
#      every issue the PR's closingIssuesReferences names.
#   2. A repeated tick while the hold still stands does not duplicate the
#      label add or the marker comment (idempotency).
#   3. Releasing the hold removes loom:blocked and posts a one-time reversal
#      comment; a repeated release tick is a no-op.
#   4. An issue already loom:blocked for an unrelated reason is left
#      completely untouched on both the propagate and release paths —
#      no label churn, no comment, no misattribution.
#   5. A PR with no closingIssuesReferences is a silent no-op on both paths.
#   6. The shipped markdown defines the two functions, wires them into both
#      hold-apply sites and both hold-release sites, uses the exact
#      `Blocked by #<PR>` phrasing, and both `champion-pr-merge.md` and
#      `label-state-machine.md` are pinned in `.loom/resync-ignore`.
#
# Usage:
#   ./.loom/scripts/tests/test-champion-pr-hold-issue-propagation.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"

# Two `..` reaches repo-root/.claude/commands/loom for an INSTALLED copy
# (SCRIPTS_DIR is .loom/scripts there); one `..` reaches defaults/.claude/
# commands/loom when running inside this source repo (SCRIPTS_DIR is
# defaults/scripts) -- the two layouts differ in depth, so probe both rather
# than hard-coding one (#6725, same probe as test-champion-critical-file-check.sh).
if [[ -d "$SCRIPTS_DIR/../../.claude/commands/loom" ]]; then
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../../.claude/commands/loom" && pwd)"
    REPO_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"
else
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../.claude/commands/loom" && pwd)"
    REPO_ROOT="$(cd "$SCRIPTS_DIR/.." && pwd)"
fi
CHAMPION_MD="$PROMPT_DIR/champion-pr-merge.md"
LABEL_STATE_MD="$REPO_ROOT/.loom/docs/label-state-machine.md"
RESYNC_IGNORE="$REPO_ROOT/.loom/resync-ignore"

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

# Pin a literal snippet as present verbatim in a doc file — catches drift
# between this test's mirrored functions and the shipped markdown.
assert_doc_contains() {
    local file="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -qF -- "$needle" "$file"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (missing literal in $file: $needle)"
    fi
}

assert_doc_count_at_least() {
    local file="$1" needle="$2" min="$3" msg="$4"
    local n
    n=$(grep -cF -- "$needle" "$file" || true)
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$n" -ge "$min" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg (found $n, need >= $min)"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (found $n, need >= $min, in $file: $needle)"
    fi
}

# =====================================================================
# Mirrored forge stand-in: an issue's labels and comment-marker history,
# keyed by issue number. Mirrors just enough of `gh issue view --json
# labels,comments` / `gh issue edit --add-label/--remove-label` / `gh issue
# comment` for propagate_pr_hold()/release_pr_hold() to run against.
# =====================================================================
declare -A ISSUE_LABELS
declare -A ISSUE_MARKERS   # newline-separated marker history, chronological

mock_reset_issue() {
    local issue="$1"
    ISSUE_LABELS["$issue"]=""
    ISSUE_MARKERS["$issue"]=""
}

mock_has_label() {
    local issue="$1" label="$2"
    [[ " ${ISSUE_LABELS[$issue]:-} " == *" $label "* ]]
}

mock_add_label() {
    local issue="$1" label="$2"
    mock_has_label "$issue" "$label" && return 0
    ISSUE_LABELS["$issue"]="${ISSUE_LABELS[$issue]:-} $label"
}

mock_remove_label() {
    local issue="$1" label="$2"
    ISSUE_LABELS["$issue"]=" ${ISSUE_LABELS[$issue]:-} "
    ISSUE_LABELS["$issue"]="${ISSUE_LABELS[$issue]// $label /}"
    ISSUE_LABELS["$issue"]="${ISSUE_LABELS[$issue]# }"
    ISSUE_LABELS["$issue"]="${ISSUE_LABELS[$issue]% }"
}

mock_has_marker() {
    local issue="$1" marker="$2"
    printf '%s\n' "${ISSUE_MARKERS[$issue]:-}" | grep -qF -- "$marker"
}

mock_post_marker() {
    local issue="$1" marker="$2"
    ISSUE_MARKERS["$issue"]="${ISSUE_MARKERS[$issue]:-}"$'\n'"$marker"
}

# Last-marker-wins lookup, mirroring the doc's
# `[.comments[] | select(startswith(block) or startswith(unblock))] | last`.
mock_last_marker() {
    local issue="$1" block="$2" unblock="$3"
    printf '%s\n' "${ISSUE_MARKERS[$issue]:-}" \
        | grep -E -- "^($(printf '%s' "$block" | sed 's/[.[\*^$()+?{|]/\\&/g')|$(printf '%s' "$unblock" | sed 's/[.[\*^$()+?{|]/\\&/g'))" \
        | tail -1
}

# =====================================================================
# Mirrored propagate_pr_hold() / release_pr_hold(), verbatim shape from
# champion-pr-merge.md's "Propagating a hold to linked issues" — the
# `closingIssuesReferences` resolution (forge_pr_close_targets) is replaced
# by a plain issue-number list argument so this can run without a live PR.
# =====================================================================
propagate_pr_hold_mirror() {
    local pr_number="$1"; shift
    local issue marker
    for issue in "$@"; do
        marker="champion:pr-hold-block:$pr_number"
        if mock_has_marker "$issue" "$marker"; then
            echo "SKIP:already-propagated:$issue"
            continue
        fi
        if mock_has_label "$issue" "loom:blocked"; then
            echo "SKIP:unrelated-block:$issue"
            continue
        fi
        mock_add_label "$issue" "loom:blocked"
        mock_post_marker "$issue" "$marker"
        echo "BLOCKED:$issue"
    done
}

release_pr_hold_mirror() {
    local pr_number="$1"; shift
    local issue block_marker unblock_marker last
    for issue in "$@"; do
        block_marker="champion:pr-hold-block:$pr_number"
        unblock_marker="champion:pr-hold-unblock:$pr_number"
        last="$(mock_last_marker "$issue" "$block_marker" "$unblock_marker")"
        if [[ "$last" != "$block_marker" ]]; then
            continue
        fi
        mock_remove_label "$issue" "loom:blocked"
        mock_post_marker "$issue" "$unblock_marker"
        echo "UNBLOCKED:$issue"
    done
}

# Captures a mutating mirror function's stdout WITHOUT forking a subshell —
# plain `out=$(fn ...)` command substitution runs `fn` in a subshell, so any
# mutation it makes to the global ISSUE_LABELS/ISSUE_MARKERS associative
# arrays is invisible back in this shell once the subshell exits. Redirecting
# to a temp file instead keeps the function call in the current shell.
capture() {
    local __outvar="$1"; shift
    local __tmp
    __tmp="$(mktemp)"
    "$@" >"$__tmp"
    # shellcheck disable=SC2034  # assigned by nameref-style printf -v below
    printf -v "$__outvar" '%s' "$(cat "$__tmp")"
    rm -f "$__tmp"
}

echo "--- propagate_pr_hold_mirror: fresh hold blocks a clean linked issue ---"

mock_reset_issue 200
capture out propagate_pr_hold_mirror 100 200
assert_eq "BLOCKED:200" "$out" "a fresh hold on PR #100 blocks its linked issue #200"
assert_eq "true" "$(mock_has_label 200 loom:blocked && echo true || echo false)" \
    "issue #200 carries loom:blocked after propagation"
assert_eq "1" "$(printf '%s\n' "${ISSUE_MARKERS[200]}" | grep -cF 'champion:pr-hold-block:100')" \
    "exactly one champion:pr-hold-block:100 marker recorded on issue #200"

echo
echo "--- propagate_pr_hold_mirror: repeated tick while hold stands is idempotent ---"

capture out2 propagate_pr_hold_mirror 100 200
assert_eq "SKIP:already-propagated:200" "$out2" \
    "a repeated tick with an existing marker does not re-block or re-comment"
assert_eq "1" "$(printf '%s\n' "${ISSUE_MARKERS[200]}" | grep -cF 'champion:pr-hold-block:100')" \
    "still exactly one marker after the repeated tick (no duplicate comment)"

echo
echo "--- release_pr_hold_mirror: release removes loom:blocked and posts one reversal ---"

capture out3 release_pr_hold_mirror 100 200
assert_eq "UNBLOCKED:200" "$out3" "releasing PR #100's hold unblocks issue #200"
assert_eq "false" "$(mock_has_label 200 loom:blocked && echo true || echo false)" \
    "issue #200 no longer carries loom:blocked after release"
assert_eq "1" "$(printf '%s\n' "${ISSUE_MARKERS[200]}" | grep -cF 'champion:pr-hold-unblock:100')" \
    "exactly one champion:pr-hold-unblock:100 marker recorded on issue #200"

echo
echo "--- release_pr_hold_mirror: repeated release tick is a no-op ---"

capture out4 release_pr_hold_mirror 100 200
assert_eq "" "$out4" "a repeated release tick (already released) produces no output/side effects"
assert_eq "1" "$(printf '%s\n' "${ISSUE_MARKERS[200]}" | grep -cF 'champion:pr-hold-unblock:100')" \
    "still exactly one unblock marker after the repeated release tick"

echo
echo "--- Pre-existing unrelated loom:blocked is never claimed or clobbered ---"

mock_reset_issue 300
mock_add_label 300 "loom:blocked"   # e.g. an unrelated Dependencies-section block
capture out5 propagate_pr_hold_mirror 101 300
assert_eq "SKIP:unrelated-block:300" "$out5" \
    "propagation does not claim an issue already loom:blocked for an unrelated reason"
assert_eq "true" "$(mock_has_label 300 loom:blocked && echo true || echo false)" \
    "issue #300 still carries loom:blocked (untouched, not removed)"
assert_eq "" "${ISSUE_MARKERS[300]:-}" \
    "no champion:pr-hold-block marker was posted on issue #300 (ownership not claimed)"

capture out6 release_pr_hold_mirror 101 300
assert_eq "" "$out6" \
    "release for PR #101 does not touch issue #300 either (never claimed ownership)"
assert_eq "true" "$(mock_has_label 300 loom:blocked && echo true || echo false)" \
    "issue #300's unrelated loom:blocked survives the release pass — not clobbered"

echo
echo "--- No closingIssuesReferences is a silent no-op on both paths ---"

capture out7 propagate_pr_hold_mirror 102
assert_eq "" "$out7" "propagate_pr_hold with an empty issue list produces no output (no error)"
capture out8 release_pr_hold_mirror 102
assert_eq "" "$out8" "release_pr_hold with an empty issue list produces no output (no error)"

echo
echo "--- Multiple linked issues: each tracked independently ---"

mock_reset_issue 400
mock_reset_issue 401
capture out9 propagate_pr_hold_mirror 103 400 401
assert_eq "$(printf 'BLOCKED:400\nBLOCKED:401')" "$out9" \
    "a PR closing two issues propagates the hold to both independently"
capture out10 release_pr_hold_mirror 103 400 401
assert_eq "$(printf 'UNBLOCKED:400\nUNBLOCKED:401')" "$out10" \
    "releasing propagates the release to both issues independently"

echo
echo "--- Doc pins: shipped markdown ships the hold-propagation functions (#1749) ---"

assert_doc_contains "$CHAMPION_MD" \
    'propagate_pr_hold() {' \
    "champion-pr-merge.md defines propagate_pr_hold()"

assert_doc_contains "$CHAMPION_MD" \
    'release_pr_hold() {' \
    "champion-pr-merge.md defines release_pr_hold()"

assert_doc_contains "$CHAMPION_MD" \
    'marker="<!-- champion:pr-hold-block:$pr_number -->"' \
    "propagate_pr_hold() uses the champion:pr-hold-block:<PR> marker"

assert_doc_contains "$CHAMPION_MD" \
    'unblock_marker="<!-- champion:pr-hold-unblock:$pr_number -->"' \
    "release_pr_hold() uses the champion:pr-hold-unblock:<PR> marker"

assert_doc_contains "$CHAMPION_MD" \
    "Blocked by #\$pr_number" \
    "the propagation comment uses the machine-parseable Blocked by #<PR> phrasing"

assert_doc_contains "$CHAMPION_MD" \
    'forge_pr_close_targets "$pr_number"' \
    "propagate_pr_hold()/release_pr_hold() resolve linked issues via forge_pr_close_targets (GitHub's own closingIssuesReferences parse, not a regex over the PR body)"

assert_doc_contains "$CHAMPION_MD" \
    "#1749" \
    "champion-pr-merge.md documents the #1749 hold-propagation fix"

echo
echo "--- Doc pins: both hold-apply sites and both hold-release sites call the functions ---"

assert_doc_count_at_least "$CHAMPION_MD" 'propagate_pr_hold "$PR_NUMBER"' 2 \
    "propagate_pr_hold is called from both criterion #2's and criterion #3's hold-apply branches"

assert_doc_count_at_least "$CHAMPION_MD" 'release_pr_hold "$PR_NUMBER"' 2 \
    "release_pr_hold is called from both criterion #2's (Step 2) and criterion #3's release branches"

echo
echo "--- Doc pins: resync-ignore pins the customized files (#1749) ---"

assert_doc_contains "$RESYNC_IGNORE" \
    "commands/loom/champion-pr-merge.md" \
    "champion-pr-merge.md is pinned in .loom/resync-ignore"

assert_doc_contains "$RESYNC_IGNORE" \
    "docs/label-state-machine.md" \
    "label-state-machine.md is pinned in .loom/resync-ignore"

assert_doc_contains "$RESYNC_IGNORE" \
    "#1749" \
    ".loom/resync-ignore documents the #1749 rationale for these pins"

echo
echo "--- Doc pins: label-state-machine.md documents the PR->issue derived use ---"

assert_doc_contains "$LABEL_STATE_MD" \
    "PR→issue derived propagation" \
    "label-state-machine.md documents the new PR->issue derived loom:operator/loom:blocked use"

assert_doc_contains "$LABEL_STATE_MD" \
    "#1749" \
    "label-state-machine.md cites #1749"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
