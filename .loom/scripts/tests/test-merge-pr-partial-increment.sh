#!/usr/bin/env bash
# test-merge-pr-partial-increment.sh - Unit tests for the partial-increment
# label-reset logic in merge-pr.sh (#3667).
#
# A merged PR that references a family/epic issue with a NON-closing keyword
# (`Part of #N` / `Contributes to #N`) does not auto-close the issue, so its
# `loom:building` label is left orphaned. merge-pr.sh's
# _reset_partial_increment_labels swaps that still-open, still-loom:building
# issue back to `loom:issue` at the merge choke point (mirroring
# orphan_recovery.py's recover_issue() semantics). Closing keywords
# (`Closes`/`Fixes`/`Resolves`) are untouched (GitHub auto-closes those).
#
# Also covers the #4569 closing-keyword conflict guard: GitHub honors a closing
# keyword ANYWHERE in a PR body (not just a line-leading trailer), so prose like
# "...then close #N" in a follow-up checklist silently overrides the deliberate
# `Contributes to #N` trailer and closes the issue on merge. merge-pr.sh detects
# that pre-merge (_check_partial_increment_close_conflict, warning + two
# published globals) and reverts it post-merge (reopen inside
# _reset_one_partial_issue) — while explicitly NOT reverting a close it cannot
# attribute to this PR (a deliberate close in the same window).
#
# #4595 extends that guard to a third source: the PR's COMMIT MESSAGES. merge-pr.sh
# squash-merges without overriding the commit message, so GitHub composes the
# squash message from the PR's commits and honors a `close #N` there even when the
# PR body only says `Part of #N`. The guard unions a paginated REST read of
# `pulls/<N>/commits` into its close-reference set, names the commit message as
# the source in its warning (the operator remedy is amend/reword, not a body
# edit), and degrades to today's warn-only behavior when that fetch fails.
#
# Strategy: the functions under test (_partial_increment_refs,
# _pr_commit_messages, _check_partial_increment_close_conflict,
# _reset_one_partial_issue, _reset_partial_increment_labels) depend only on
# globals (PR_JSON, REPO_NWO, PR_NUMBER, FORGE_TYPE, GH,
# PARTIAL_CONFLICT_ISSUES, PARTIAL_OPEN_BEFORE_MERGE) and the `gh` CLI. We
# extract just those function definitions from merge-pr.sh and source them, stub
# `gh` on PATH to serve canned issue JSON and record mutating calls, then assert
# on the recorded calls. Extracting from source (rather than replicating) keeps
# the test in lockstep with the script.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-partial-increment.sh

# SC2034: several globals (PR_JSON, REPO_NWO, PR_NUMBER, FORGE_TYPE) are read
# only by the functions extracted+sourced from merge-pr.sh, which shellcheck
# cannot see — every such assignment looks "unused" to the linter.
# shellcheck disable=SC2034

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"

# #8191: the ref extractors now delegate to `loom-daemon merge-pr-refs`. Pin
# the binary they exec and verify it HAS that subcommand — the same harness the
# epic's five other ported suites use. Without the subcommand check, a stale
# binary would make every extractor return empty, and "no references found" is
# the reading that closes an unfinished issue.
# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
# The post-merge reset's decision is `loom-daemon merge-pr partial-reset`, the
# pre-merge conflict guard's is `merge-pr partial-conflict`, and the pass's two
# audit-comment BODIES are `merge-pr partial-comment` (#8191 slices), so the
# same binary must carry all three verbs too.
# #9502 (via #11167): the guard also calls `merge-pr-refs
# loom-issue-trailer-warnings` (#9465), so the pinned binary must carry it.
loom_test_require_daemon_bin "$HELPERS_DIR" merge-pr-refs "merge-pr partial-reset" "merge-pr post-merge-bookkeeping" "merge-pr partial-conflict" "merge-pr partial-comment" "merge-pr-refs loom-issue-trailer-warnings"
# shellcheck source=lib/write-scope-fixture.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/write-scope-fixture.sh"

# Colors
# shellcheck source=lib/assert.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

# --- Minimal logging shims the extracted functions call ---
info()    { echo "INFO: $*"; }
success() { echo "OK: $*"; }
warning() { echo "WARN: $*" >&2; }
# _mp_refs's fail-closed path calls error(), same as merge-pr.sh's own (echo to
# stderr, exit 1) -- #8897's new BT4 case is the first in this suite to reach
# it. Without a real `exit`, bash's command-substitution subshells do NOT
# inherit `-e` by default (no `inherit_errexit`), so an undefined `error`
# would silently no-op ("command not found") instead of aborting the subshell
# the way production's error() does, masking the exact fail-open/fail-closed
# distinction this suite exists to pin.
error()   { echo "ERROR: $*" >&2; exit 1; }

# --- Real forge-helpers.sh (for the #4856 rate-limit-safe mutation wrappers) ---
# merge-pr.sh's mutating call sites no longer invoke `gh issue edit` / `gh issue
# reopen` / `gh issue comment` directly — they route through
# forge_gh_swap_label_rl_safe / forge_gh_reopen_issue_rl_safe /
# forge_gh_comment_rl_safe, which live in lib/forge-helpers.sh and fall back to
# REST when the GraphQL mutation is rate-limited (#4856). Source the REAL
# helpers (rather than shimming them) so this suite keeps exercising the actual
# `gh` invocation shape the wrappers produce on their happy path, which the stub
# below records verbatim. Sourced BEFORE the shared globals are assigned:
# forge-helpers.sh initializes FORGE_TYPE="" at load time, which would otherwise
# clobber the FORGE_TYPE="github" the tests rely on.
# shellcheck source=../lib/forge-helpers.sh
source "$HELPERS_DIR/lib/forge-helpers.sh"

# --- Extract the functions under test from merge-pr.sh and source them ---
# One span, bounded by an anchor line that is NOT part of the span:
#   1. `_mp_refs() {` .. `_check_partial_increment_close_conflict || true` —
#      the #4569 pre-merge guard and the loom-daemon-backed ref extractor it
#      depends on.
#      Stopping at the INVOCATION line keeps the top-level call out of the
#      sourced file (we drive the guard explicitly from the tests).
#   (The post-merge reset/reopen pass was a second span until #14905 retired it:
#   it is the daemon's merge-observation pass now.)
# The span contains only function definitions plus comments (harmless when
# sourced).
#
# Two lines are pulled in from OUTSIDE both spans: the one-line
# `_mp_daemon_roll_hint` (which `_mp_refs`'s refusal path calls, and which is
# defined above span 1 because the verdict-label guard needs it earlier) and
# every `# requires-daemon:` marker comment (which that function reads back out
# of ${BASH_SOURCE[0]} — i.e. out of THIS extracted file, so the markers have to
# travel with it). #8285's own suite,
# test-merge-pr-daemon-version-floor.sh, is what asserts on that message; here
# they exist only so the refusal path is not a dangling call.
FUNCS_FILE="$(mktemp)"
trap 'rm -rf "$FUNCS_FILE" "$STUB_DIR" 2>/dev/null || true' EXIT
awk '
  /^# requires-daemon:/                                  { print; next }
  /^# daemon-verb-hint:/                                 { print; next }
  /^_mp_daemon_roll_hint\(\) \{/                         { print; next }
  /^_mp_refs\(\) \{/                                     { capture=1 }
  /^_check_partial_increment_close_conflict \|\| true/   { capture=0 }
  capture { print }
' "$MERGE_PR_SRC" > "$FUNCS_FILE"

for _fn in _mp_refs _partial_increment_refs \
           _pr_commit_messages _check_partial_increment_close_conflict; do
    if ! grep -q "^${_fn}() {" "$FUNCS_FILE"; then
        echo -e "${RED}FATAL${NC}: could not extract $_fn from $MERGE_PR_SRC" >&2
        exit 2
    fi
done
# shellcheck disable=SC1090
source "$FUNCS_FILE"

# --- Stub gh on PATH ---
STUB_DIR="$(mktemp -d)"
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
# Stub gh for test-merge-pr-partial-increment.sh.
#   gh api repos/OWNER/REPO/issues/N        -> cat $STUB_DIR/issue-N.json (or {})
#   gh api repos/OWNER/REPO/pulls/N/commits -> cat $STUB_DIR/pr-commits.json (or [])
#        (exits 1 when LOOM_TEST_COMMITS_FAIL=1, simulating a fetch failure)
#   gh issue edit N ...                     -> record to $STUB_DIR/gh-calls.log
#   gh issue comment N ...                  -> record to $STUB_DIR/gh-calls.log
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
LOG="$STUB_DIR_FROM_ENV/gh-calls.log"

if [[ "$1" == "api" ]]; then
  # Scan argv for the api path rather than assuming it is the last argument:
  # flags such as --paginate / --jq '<program>' may follow it (#4595).
  shift
  path=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      # Flags that take a value: drop the flag AND its argument.
      --jq|-q|--field|-f|--raw-field|-F|--header|-H|--method|-X|--input)
        shift
        [[ $# -gt 0 ]] && shift
        continue
        ;;
      -*) shift; continue ;;
      *) path="$1"; break ;;
    esac
  done

  case "$path" in
    */pulls/*/commits)
      if [[ "${LOOM_TEST_COMMITS_FAIL:-}" == "1" ]]; then
        echo "stub gh: simulated commits fetch failure" >&2
        exit 1
      fi
      canned="$STUB_DIR_FROM_ENV/pr-commits.json"
      if [[ -f "$canned" ]]; then cat "$canned"; else echo '[]'; fi
      exit 0
      ;;
    */comments)
      # #9774/#9865: a POST to the comments endpoint is the daemon chokepoint's
      # shape (forge comment -> gh api --input FILE), and it is the ONLY comment
      # path now. Record it in the `issue comment` shape the assertions below
      # read, so a comment that never lands still fails them.
      if [[ "${LOOM_TEST_COMMENT_FAIL:-}" == "1" ]]; then
        echo "stub gh: simulated comment post failure" >&2
        exit 1
      fi
      _rest="${path#repos/}"; _repo="${_rest%%/issues/*}"
      _num="${_rest#*/issues/}"; _num="${_num%%/*}"
      echo "issue comment $_num --repo $_repo --body $(jq -r .body "$3" 2>/dev/null)" >> "$LOG"
      exit 0
      ;;
  esac

  num="${path##*/}"
  canned="$STUB_DIR_FROM_ENV/issue-$num.json"
  if [[ -f "$canned" ]]; then cat "$canned"; else echo '{}'; fi
  exit 0
fi

if [[ "$1" == "issue" ]]; then
  # Fault switch (#8191 slice 2): the reopen write fails. The orchestrator now
  # reaches the real forge_gh_reopen_issue_rl_safe in a child bash, so a shell
  # function override can no longer inject this; the forge call itself fails.
  if [[ "$2" == "reopen" && "${LOOM_TEST_REOPEN_FAIL:-}" == "1" ]]; then
    echo "stub gh: simulated reopen failure" >&2
    exit 1
  fi
  # Record the full argv (issue edit/comment) for assertions.
  echo "$*" >> "$LOG"
  exit 0
fi

echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"
export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"
# #9548: merge-pr.sh vets its write target through the write scope before it
# writes. It runs from a checkout registered as owner/repo (origin, .loom/, push
# reported to the permission probe), so the real decision admits it.
write_scope_register "$STUB_DIR/checkout" owner/repo
cd "$STUB_DIR/checkout"

# --- Shared globals the functions read (consumed indirectly by the sourced
# functions; see the file-level SC2034 disable at the top). ---
REPO_NWO="owner/repo"
PR_NUMBER="999"
FORGE_TYPE="github"
GH="gh"
PARTIAL_OPEN_BEFORE_MERGE=""
PARTIAL_CONFLICT_ISSUES=""

# Shim for the GraphQL-backed close-target helper merge-pr.sh gets from
# forge-helpers.sh. Driven by $FORGE_CLOSE_TARGETS so tests can simulate both
# "GraphQL answered" and "GraphQL quota exhausted -> empty" (the condition the
# #4569 incident actually occurred under).
FORGE_CLOSE_TARGETS=""
forge_pr_close_targets() { printf '%s' "$FORGE_CLOSE_TARGETS"; }

# Canned issue fixtures.
cat > "$STUB_DIR/issue-123.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:building"},{"name":"loom:epic"}]}
EOF
cat > "$STUB_DIR/issue-456.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:building"}]}
EOF
cat > "$STUB_DIR/issue-777.json" <<'EOF'
{"state":"closed","labels":[{"name":"loom:building"}]}
EOF
cat > "$STUB_DIR/issue-888.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:issue"}]}
EOF
cat > "$STUB_DIR/issue-321.json" <<'EOF'
{"state":"open","pull_request":{"url":"x"},"labels":[{"name":"loom:building"}]}
EOF
cat > "$STUB_DIR/issue-4574.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:building"}]}
EOF
# PA6 fixtures: a numbered-list declaration `3. Part of #789` whose marker
# ordinal (3) collides with a genuine `Closes #3` elsewhere in the same body.
cat > "$STUB_DIR/issue-3.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:building"}]}
EOF
cat > "$STUB_DIR/issue-789.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:building"}]}
EOF

# Canned `pulls/<N>/commits` payload: one JSON commit object per message given.
# Written in the GitHub REST shape the script reads (.[].commit.message).
set_commits() {
  local first=1 m
  {
    printf '['
    for m in "$@"; do
      [[ $first -eq 1 ]] || printf ','
      first=0
      printf '{"sha":"deadbeef","commit":{"message":%s}}' "$(printf '%s' "$m" | jq -Rs .)"
    done
    printf ']'
  } > "$STUB_DIR/pr-commits.json"
}

# Clearing the two #4569 globals here keeps every pre-existing case below running
# against the same "no conflict recorded" state it was written for; resetting the
# commits fixture to empty does the same for the #4595 commit-message signal.
reset_log() {
  : > "$STUB_DIR/gh-calls.log"
  PARTIAL_OPEN_BEFORE_MERGE=""
  PARTIAL_CONFLICT_ISSUES=""
  FORGE_CLOSE_TARGETS=""
  set_commits
  unset LOOM_TEST_COMMITS_FAIL
}
read_log()  { cat "$STUB_DIR/gh-calls.log" 2>/dev/null || true; }

# Run one of the functions under test IN THE CURRENT SHELL (never a command
# substitution — the #4569 guard publishes its results via globals, which a
# subshell would discard) while capturing its stderr for assertions.
run_capturing_stderr() {
    "$@" 2>"$STUB_DIR/stderr.log" || true
}
read_stderr() { cat "$STUB_DIR/stderr.log" 2>/dev/null || true; }

echo "Testing the post-merge partial-increment reset (RETIRED by #14905)..."
# merge-pr.sh no longer runs the reset (#14905, cutover of #14900/#14902): the
# daemon's merge-observation pass does, for every observed merge. T1-T10 drove
# the shell wrapper against a stub gh; the same decisions are pinned below.
retired "T1/T5/T6: Part of / Contributes to #N, open + loom:building -> swap to loom:issue + one comment (repo-scoped; case-insensitive; several refs)" \
    "a still-open partial-increment issue returns to the ready queue with an audit comment" \
    "the wrapper that ran the reset is gone from merge-pr.sh; the stub-gh transcript cannot be produced by a script that no longer makes the calls" \
    "partial_reset::tests::t1_open_and_building_swaps and post_merge_bookkeeping::tests::partial_reset_swaps_and_comments (decision + order), merge_observation_tests::an_open_partial_increment_is_swapped_and_commented_once (the daemon tick, one comment), and tests/merge_pr_partial_reset_differential.rs (Rust plan == frozen shell on the whole corpus)"
retired "T2/T2b/T10: Closes/Fixes/Resolves #N is not a partial ref; a mixed body resets only the partial ref" \
    "the auto-close path is untouched and only declared partial increments are reset" \
    "same: no script-side call remains" \
    "merge_pr::refs tests (closing vs partial vocabulary, tests/merge_pr_refs_differential.rs) feed partial_reset::plan; merge_observation_tests::a_preventive_reopens_only_what_this_merge_closed covers the closing-reference side"
retired "T3/T4/T7/T9: closed issue, issue without loom:building, a PR target, an empty/null body -> no mutation" \
    "every no-op case stays a no-op (idempotent re-run)" \
    "same: no script-side call remains" \
    "partial_reset::tests::t3_t20_closed_and_untracked_is_a_logged_skip, t4_open_without_building_is_an_idempotent_skip, t7_a_pull_request_is_skipped_silently; post_merge_bookkeeping::tests::partial_reset_empty_body_does_nothing and partial_reset_never_touches_a_pull_request"
retired "T8: FORGE_TYPE != github -> no-op" \
    "the reset is GitHub-only" \
    "the FORGE_TYPE guard lived in the removed wrapper; the daemon's merge-observation pass is the GitHub reconciliation tick and reads only GitHub" \
    "no Gitea path exists on the daemon side, so there is nothing to assert; the shell never acted on Gitea either"

# ---------------------------------------------------------------------------
# #4569: closing-keyword conflict detection + premature-close revert.
# ---------------------------------------------------------------------------

echo ""
echo "Testing _partial_increment_refs prose/code-span guarding (#5234)..."

# PA1: the exact #5234 incident shape — a mid-sentence, backticked, conditional
# mention of `Part of #4574` inside a paragraph addressed to the Judge, plus
# the real, declared `Closes #4574` elsewhere in the body. The backticked
# mention is prose describing a hypothetical ("if you would rather... I will
# switch the reference to"), not a declaration, and must NOT be read as one.
incident_body='Summary of the fix.

Judge: if you would rather this stay open until #4580 lands, say so and I will switch the reference to `Part of #4574`.

More detail about the change.

Closes #4574'
assert_eq "" "$(_partial_increment_refs "$incident_body")" \
  "#5234 incident: backticked, mid-sentence 'Part of #4574' is NOT read as a declaration"

reset_log
PR_JSON="$(jq -n --arg body "$incident_body" '{body: $body}')"
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "" "$PARTIAL_CONFLICT_ISSUES" \
  "#5234 incident: no conflict recorded — the real 'Closes #4574' stands unopposed"
assert_eq "" "$PARTIAL_OPEN_BEFORE_MERGE" \
  "#5234 incident: #4574 is not tracked as a partial-increment ref at all (no declaration found)"

# PA2: companion — a genuine, line-leading `Part of #4574` trailer (its own
# line, no backticks) still triggers the existing partial-increment path, and
# a conflicting closing reference elsewhere in the body is still detected. Also
# verifies AC #4: the warning quotes the matched declaration text.
genuine_body='Summary of the fix.

Part of #4574

Closes #4574'
assert_eq "4574" "$(_partial_increment_refs "$genuine_body")" \
  "Companion: genuine line-leading 'Part of #4574' IS read as a declaration"

reset_log
PR_JSON="$(jq -n --arg body "$genuine_body" '{body: $body}')"
run_capturing_stderr _check_partial_increment_close_conflict
genuine_err="$(read_stderr)"
assert_eq "4574" "$PARTIAL_CONFLICT_ISSUES" \
  "Companion: genuine 'Part of #4574' + 'Closes #4574' -> conflict recorded (existing path still fires)"
assert_contains "$genuine_err" '("Part of #4574")' \
  "Companion: warning quotes the matched declaration text with context (AC #4)"
assert_contains "$genuine_err" 'Closes #4574' \
  "Companion: warning also quotes the offending closing-keyword text"

# PA3: `Part of #123` shown inside a FENCED code block (not just inline
# backticks) is also excluded — a documentation example, not a live
# declaration.
fenced_body='Example usage of the convention:

```
Part of #123
```

Closes #123'
assert_eq "" "$(_partial_increment_refs "$fenced_body")" \
  "Fenced code block: 'Part of #123' inside a triple-backtick fence is NOT a declaration"

# PA4: mid-sentence prose (no backticks at all) referencing the pattern is
# still excluded by the line-leading anchor alone.
prose_body='This work is part of #123, a larger initiative that will continue after this PR.'
assert_eq "" "$(_partial_increment_refs "$prose_body")" \
  "Mid-sentence prose 'part of #123' (no backticks) is NOT a declaration (line-leading anchor)"

# PA5: a list-marker-prefixed declaration still counts (AC: "optionally after
# list-marker/whitespace").
list_body='Changes in this increment:

- Part of #123'
assert_eq "123" "$(_partial_increment_refs "$list_body")" \
  "List-marker prefix: '- Part of #123' still counts as a declaration"

# PA6: a NUMBERED list marker carries its own digits, which the second-stage
# extraction must not mistake for an issue number. PA5 only covers a dash
# marker (no digits to leak), so this case is what caught the regression: with
# a naive `grep -oE '[0-9]+'` over the whole matched span, `3. Part of #789`
# yielded BOTH `3` and `789`. Paired with a genuine `Closes #3` elsewhere in
# the body, that spuriously registers #3 as a declared partial increment AND a
# closing reference — the exact false-positive-reopen shape #5234 exists to
# eliminate, reintroduced through the list-marker support itself.
numbered_body='Changes in this increment:

3. Part of #789

Closes #3'
assert_eq "789" "$(_partial_increment_refs "$numbered_body")" \
  "Numbered list marker: '3. Part of #789' yields ONLY 789 — the marker ordinal 3 does not leak"

reset_log
PR_JSON="$(jq -n --arg body "$numbered_body" '{body: $body}')"
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "" "$PARTIAL_CONFLICT_ISSUES" \
  "Numbered list marker: no conflict — 'Closes #3' stands, #3 is not a partial-increment ref"
assert_eq "789" "$PARTIAL_OPEN_BEFORE_MERGE" \
  "Numbered list marker: only the real declaration target (#789) is tracked as open before the merge"

# PA7: blockquote marker (`>`), the other marker branch the regex claims to
# support and that no test previously exercised.
quoted_body='Context from the epic:

> Part of #456'
assert_eq "456" "$(_partial_increment_refs "$quoted_body")" \
  "Blockquote marker: '> Part of #456' still counts as a declaration"

echo ""
echo "Testing backticked-trailer warning (#5690, ported to Rust #8831)..."

# #8831: the detection logic (_backticked_partial_increment_trailer_refs /
# _backticked_partial_increment_trailer_snippets) and the warning-message
# assembly moved to loom-daemon (cli/merge_pr_refs.rs's
# `backticks-partial-increment-warnings`, loom_daemon::merge_pr::refs) so the
# logic is not duplicated in portable shell. BT2-BT7 exercised
# the detector's regex shape directly and are retired in favor of the byte-
# identical port's own unit tests
# (loom-daemon/src/merge_pr/refs/tests.rs::bt2_.. through bt7_..). BT1 and BT8
# stay HERE as call-site wiring pins: they prove merge-pr.sh's own guard still
# invokes the daemon correctly and still surfaces the warning end to end —
# they do not re-litigate the regex shape.
retired "BT2: plain-text trailer does not warn" \
  "a real 'Part of #N' trailer must not trigger the backticked-trailer warning" \
  "detection logic moved to loom-daemon (#8831); shell no longer defines the detector to call directly" \
  "loom-daemon/src/merge_pr/refs/tests.rs::bt2_a_plain_text_trailer_does_not_warn"
retired "BT3: mid-sentence backticked mention does not warn" \
  "prose describing a hypothetical must stay silent" \
  "detection logic moved to loom-daemon (#8831)" \
  "loom-daemon/src/merge_pr/refs/tests.rs::bt3_a_mid_sentence_backticked_mention_does_not_warn"
retired "BT4: a line listing backticked trailers as examples does not warn" \
  "documentation prose is not an attempted declaration" \
  "detection logic moved to loom-daemon (#8831)" \
  "loom-daemon/src/merge_pr/refs/tests.rs::bt4_a_line_listing_backticked_trailers_as_examples_does_not_warn"
retired "BT5: a backticked trailer inside a fenced block does not warn" \
  "fenced code blocks are stripped before matching" \
  "detection logic moved to loom-daemon (#8831)" \
  "loom-daemon/src/merge_pr/refs/tests.rs::bt5_a_backticked_trailer_inside_a_fenced_block_does_not_warn"
retired "BT6: an issue named by both shapes is not warned about" \
  "the plain-text trailer's declaration wins; the reset fires regardless" \
  "detection logic moved to loom-daemon (#8831)" \
  "loom-daemon/src/merge_pr/refs/tests.rs::bt6_an_issue_named_by_both_shapes_is_not_warned_about"
retired "BT7: marker-prefixed and numbered-list forms; the PA6 ordinal trap" \
  "a numbered-list marker's own ordinal must not leak in as an issue number" \
  "detection logic moved to loom-daemon (#8831)" \
  "loom-daemon/src/merge_pr/refs/tests.rs::bt7_marker_prefixed_and_numbered_list_forms"

# BT1 (wiring pin): the exact incident shape — PR #5686's body wrote the
# trailer as `Part of #5240` (whole line, wrapped in an inline code span)
# instead of the plain `Part of #5240`. #5234's code-span exclusion correctly
# reads that as a non-declaration, so the #3667 reset silently no-opped and
# #5240 was stranded at loom:building with no log line anywhere. The parser's
# answer must NOT change; the guard's warning is what makes the silence
# visible — and this proves merge-pr.sh's call site still wires it up after
# the port.
backticked_body='## Summary

Implements the first slice.

`Part of #5240`'
assert_eq "" "$(_partial_increment_refs "$backticked_body")" \
  "#5690 incident: backticked '\`Part of #5240\`' is still NOT a declaration (#5234 unchanged)"

reset_log
PR_JSON="$(jq -n --arg body "$backticked_body" '{body: $body}')"
run_capturing_stderr _check_partial_increment_close_conflict
bt_err="$(read_stderr)"
assert_contains "$bt_err" "Backticked partial-increment trailer (#5690)" \
  "#5690 incident: pre-merge guard emits the backticked-trailer warning"
assert_contains "$bt_err" '"`Part of #5240`"' \
  "#5690 incident: warning quotes the offending line verbatim"
assert_contains "$bt_err" '#5240' \
  "#5690 incident: warning names the affected issue"
assert_eq "" "$PARTIAL_CONFLICT_ISSUES" \
  "#5690 incident: warning is advisory only — no conflict recorded"
assert_eq "" "$PARTIAL_OPEN_BEFORE_MERGE" \
  "#5690 incident: warning does not add the issue to the partial-increment tracking sets"
assert_eq "" "$(read_log)" \
  "#5690 incident: warning is non-blocking and mutates nothing"

# BT2 (wiring pin, renumbered from BT8): --dry-run prefixes the warning,
# matching the conflict warnings' contract — proves the guard forwards
# DRY_RUN to the daemon subcommand correctly.
reset_log
PR_JSON="$(jq -n --arg body "$backticked_body" '{body: $body}')"
# Set/unset explicitly rather than `DRY_RUN=true run_capturing_stderr …`:
# in bash, a variable assignment preceding a SHELL FUNCTION call leaks into the
# calling shell afterwards, which would silently flip every later case to
# dry-run mode.
DRY_RUN=true
run_capturing_stderr _check_partial_increment_close_conflict
unset DRY_RUN
assert_contains "$(read_stderr)" "[dry-run] Backticked partial-increment trailer (#5690)" \
  "Dry run: warning carries the [dry-run] prefix"

# BT3 (wiring pin): a plain-text-only body must not warn — proves the guard
# does not fire the daemon subcommand's finding when there is nothing to
# report (the common, non-#5690 path).
reset_log
PR_JSON='{"body":"## Summary\n\nImplements the first slice.\n\nPart of #123"}'
run_capturing_stderr _check_partial_increment_close_conflict
assert_not_contains "$(read_stderr)" "Backticked partial-increment trailer (#5690)" \
  "Plain-text 'Part of #123' trailer: no warning (declaration parses, reset will fire)"

# BT4 (#8897): a daemon that answers `merge-pr-refs closing-refs` /
# `partial-increment-refs` but REJECTS `backticks-partial-increment-warnings`
# (the daemon predates that mode, #8808) must not print _mp_refs's
# hardcoded, fail-closed "Refusing..." wording -- that text is hardcoded to
# the closing-reference case, so on THIS mode it names the wrong PR (#8191)
# and cites a roll-hint version this host already satisfies, while the merge
# proceeds regardless (the `local var=$(...)` exit-status swallow, unrelated
# to this fix, that makes the call fail-open in practice already). The call
# site must instead report a quiet, advisory skip.
fake_daemon="$STUB_DIR/fake-loom-daemon-no-bt-mode"
cat > "$fake_daemon" <<'FAKEDAEMON'
#!/usr/bin/env bash
# Simulates a loom-daemon predating #8808: answers merge-pr-refs for every
# mode EXCEPT backticks-partial-increment-warnings, which it rejects the way
# clap rejects an unknown subcommand (nonzero exit, no stdout).
if [[ "$1" == "merge-pr-refs" && "$2" == "backticks-partial-increment-warnings" ]]; then
  echo "error: unrecognized subcommand 'backticks-partial-increment-warnings'" >&2
  exit 2
fi
exit 0
FAKEDAEMON
chmod +x "$fake_daemon"

reset_log
PR_JSON='{"body":"Implements a slice.\n\nPart of #123"}'
saved_self_bin="${LOOM_DAEMON_SELF_BIN:-}"
export LOOM_DAEMON_SELF_BIN="$fake_daemon"
run_capturing_stderr _check_partial_increment_close_conflict
no_bt_mode_err="$(read_stderr)"
export LOOM_DAEMON_SELF_BIN="$saved_self_bin"
assert_not_contains "$no_bt_mode_err" "Refusing" \
  "#8897: daemon rejecting backticks-partial-increment-warnings -> no 'Refusing' wording printed"
assert_not_contains "$no_bt_mode_err" "#8191" \
  "#8897: daemon rejecting backticks-partial-increment-warnings -> does not blame the closing-ref analysis (#8191)"
assert_contains "$no_bt_mode_err" "Skipped backticked-trailer advisory warning check" \
  "#8897: daemon rejecting backticks-partial-increment-warnings -> quiet skip note printed instead"
assert_eq "" "$(read_log)" \
  "#8897: the skip is advisory only -- no forge mutation results from it"

echo ""
echo "Testing Loom-Issue trailer warning wiring (#9502, via #11167)..."

# The detector itself (backticked / slug-less `Loom-Issue:` trailers, #9465)
# is loom-daemon's `merge-pr-refs loom-issue-trailer-warnings`, unit-tested in
# loom-daemon/src/merge_pr/refs. These are CALL-SITE pins, mirroring BT1-BT4
# above: merge-pr.sh's pre-merge guard must invoke it, forward --dry-run,
# surface its finding, stay silent when there is none, and degrade to an
# advisory skip on a daemon that predates the verb.

# LT1: whole-line backticked trailer under --dry-run -> warning, [dry-run]-prefixed.
lt_backticked_body='## Summary

Implements the first slice.

`Loom-Issue: 2AMLogic/loom#9465`'
reset_log
PR_JSON="$(jq -n --arg body "$lt_backticked_body" '{body: $body}')"
DRY_RUN=true
run_capturing_stderr _check_partial_increment_close_conflict
unset DRY_RUN
lt_err="$(read_stderr)"
assert_contains "$lt_err" "[dry-run] Unparseable Loom-Issue trailer (#9465)" \
  "LT1: backticked Loom-Issue trailer under --dry-run -> warning carries the [dry-run] prefix"
assert_contains "$lt_err" '"`Loom-Issue: 2AMLogic/loom#9465`"' \
  "LT1: warning quotes the offending line verbatim"
assert_eq "" "$(read_log)" \
  "LT1: the warning is advisory only -- no forge mutation"

# LT1b: same body without --dry-run -> warning, no prefix.
reset_log
PR_JSON="$(jq -n --arg body "$lt_backticked_body" '{body: $body}')"
run_capturing_stderr _check_partial_increment_close_conflict
lt_err="$(read_stderr)"
assert_contains "$lt_err" "Unparseable Loom-Issue trailer (#9465)" \
  "LT1b: backticked Loom-Issue trailer (real run) -> warning emitted"
assert_not_contains "$lt_err" "[dry-run]" \
  "LT1b: a real run's warning carries no [dry-run] prefix"

# LT2: the same trailer as PLAIN TEXT -> parses, so nothing to warn about.
reset_log
PR_JSON='{"body":"Implements the first slice.\n\nLoom-Issue: 2AMLogic/loom#9465"}'
run_capturing_stderr _check_partial_increment_close_conflict
assert_not_contains "$(read_stderr)" "Loom-Issue trailer" \
  "LT2: plain-text 'Loom-Issue: owner/repo#N' trailer -> no warning (no false positive)"

# LT3: BOTH shapes for the same issue -> the plain one already carries the link.
reset_log
PR_JSON='{"body":"Implements the first slice.\n\n`Loom-Issue: 2AMLogic/loom#9465`\nLoom-Issue: 2AMLogic/loom#9465"}'
run_capturing_stderr _check_partial_increment_close_conflict
assert_not_contains "$(read_stderr)" "Loom-Issue trailer" \
  "LT3: backticked + plain trailer for the same issue -> no warning"

# LT4: slug-less `Loom-Issue: #123` -> warns, naming the missing owner/repo.
reset_log
PR_JSON='{"body":"Implements the first slice.\n\nLoom-Issue: #123"}'
run_capturing_stderr _check_partial_increment_close_conflict
lt_err="$(read_stderr)"
assert_contains "$lt_err" "Unparseable Loom-Issue trailer (#9465)" \
  "LT4: slug-less 'Loom-Issue: #123' -> warning emitted"
assert_contains "$lt_err" '"Loom-Issue: #123"' \
  "LT4: warning quotes the slug-less line"
assert_contains "$lt_err" "owner/repo" \
  "LT4: warning names the missing owner/repo slug"

# LT5: a daemon that REJECTS loom-issue-trailer-warnings (predates #9465) ->
# an advisory "skipped" warning that names the too-old daemon and the verb's
# own landing version (#9377 daemon-verb-hint), never a refusal; the guard
# returns 0 and the rest of the pre-merge pass still runs.
fake_daemon_lt="$STUB_DIR/fake-loom-daemon-no-lt-mode"
cat > "$fake_daemon_lt" <<'FAKEDAEMON'
#!/usr/bin/env bash
# Simulates a loom-daemon predating #9465: answers every merge-pr-refs mode
# EXCEPT loom-issue-trailer-warnings, which it rejects the way clap rejects an
# unknown subcommand. Every other mode delegates to the real pinned binary so
# the rest of the guard behaves normally.
if [[ "$1" == "--version" ]]; then echo "loom-daemon 0.19.500 (fake)"; exit 0; fi
if [[ "$1" == "merge-pr-refs" && "$2" == "loom-issue-trailer-warnings" ]]; then
  echo "error: unrecognized subcommand 'loom-issue-trailer-warnings'" >&2
  exit 2
fi
exec "$LOOM_TEST_REAL_DAEMON_BIN" "$@"
FAKEDAEMON
chmod +x "$fake_daemon_lt"

reset_log
PR_JSON='{"body":"Implements a slice.\n\nPart of #123\n\n`Loom-Issue: 2AMLogic/loom#9465`"}'
saved_self_bin="${LOOM_DAEMON_SELF_BIN:-}"
export LOOM_TEST_REAL_DAEMON_BIN="${LOOM_DAEMON_SELF_BIN:-${LOOM_DAEMON_BIN:-$(command -v loom-daemon)}}"
export LOOM_DAEMON_SELF_BIN="$fake_daemon_lt"
lt5_rc=0
# Called directly, not via run_capturing_stderr (which swallows the status).
_check_partial_increment_close_conflict 2>"$STUB_DIR/stderr.log" || lt5_rc=$?
no_lt_mode_err="$(read_stderr)"
export LOOM_DAEMON_SELF_BIN="$saved_self_bin"
assert_eq "0" "$lt5_rc" \
  "LT5: daemon rejecting loom-issue-trailer-warnings -> guard still returns 0 (merge proceeds)"
assert_contains "$no_lt_mode_err" "Skipped Loom-Issue trailer advisory warning check" \
  "LT5: daemon rejecting loom-issue-trailer-warnings -> skipped-advisory warning printed (never silent)"
assert_contains "$no_lt_mode_err" "advisory-only" \
  "LT5: the skip says it is advisory-only"
assert_not_contains "$no_lt_mode_err" "Refusing" \
  "LT5: no 'Refusing' wording -- a missing advisory verb never refuses the merge"
assert_contains "$no_lt_mode_err" "loom-daemon >= 0.19.512 for 'merge-pr-refs loom-issue-trailer-warnings'" \
  "LT5: roll hint names the verb's own landing version (daemon-verb-hint), not the merge-pr-refs family floor"
assert_contains "$no_lt_mode_err" "0.19.500 (fake)" \
  "LT5: roll hint reports what the resolved (too-old) binary actually is"
assert_eq "123" "$PARTIAL_OPEN_BEFORE_MERGE" \
  "LT5: the rest of the pre-merge pass still ran past the skipped check (#123 tracked)"

echo ""
echo "Testing _check_partial_increment_close_conflict (pre-merge guard)..."

# T11: reproduces the censusapi#5 -> censusapi#2 incident shape — a deliberate
# `Contributes to #123` trailer plus an "Operator follow-up" step saying
# "...then close #123", with GraphQL unavailable (quota exhausted).
reset_log
PR_JSON='{"body":"## Summary\n\nBuilder scope only; publish steps stay with the operator.\n\n## Operator follow-up (after merge)\n\n1. npm publish\n2. Verify, then close #123.\n\nContributes to #123\n"}'
run_capturing_stderr _check_partial_increment_close_conflict
guard_err="$(read_stderr)"
assert_eq "123" "$PARTIAL_CONFLICT_ISSUES" \
  "Incident shape: #123 recorded as a closing-keyword conflict (body regex, no GraphQL)"
assert_eq "123" "$PARTIAL_OPEN_BEFORE_MERGE" \
  "Incident shape: #123 recorded as open before the merge"
assert_contains "$guard_err" "Partial-increment conflict (#4569)" \
  "Incident shape: emits the actionable pre-merge warning"
assert_contains "$guard_err" "close #123" \
  "Incident shape: warning quotes the offending phrase"

# T12: a clean partial-increment body -> tracked as open-before-merge, no conflict.
reset_log
PR_JSON='{"body":"Implements a slice.\n\nContributes to #123"}'
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "" "$PARTIAL_CONFLICT_ISSUES" \
  "Clean 'Contributes to #123' body -> no conflict recorded"
assert_eq "123" "$PARTIAL_OPEN_BEFORE_MERGE" \
  "Clean body -> #123 still recorded as open before the merge"

# T13: closing keyword aimed at a DIFFERENT issue -> not a conflict.
reset_log
PR_JSON='{"body":"Closes #888\n\nPart of #123"}'
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "" "$PARTIAL_CONFLICT_ISSUES" \
  "Closes #888 + Part of #123 -> no conflict (different issues)"

# T14: partial ref already closed BEFORE the merge -> tracked in neither set,
# so nothing this merge does can be blamed on it.
reset_log
PR_JSON='{"body":"Part of #777"}'
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "" "$PARTIAL_CONFLICT_ISSUES" "Pre-closed #777 -> no conflict recorded"
assert_eq "" "$PARTIAL_OPEN_BEFORE_MERGE" "Pre-closed #777 -> not recorded as open before merge"

# T15: GraphQL-only closing link (e.g. a Development-sidebar link no body text
# reveals) is also caught, via the forge_pr_close_targets union.
reset_log
FORGE_CLOSE_TARGETS="123"
PR_JSON='{"body":"Contributes to #123"}'
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "123" "$PARTIAL_CONFLICT_ISSUES" \
  "closingIssuesReferences-only link (no body keyword) -> conflict recorded"

# T16: the reference target is a PR -> never tracked, never mutated.
reset_log
PR_JSON='{"body":"Part of #321"}'
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "" "$PARTIAL_OPEN_BEFORE_MERGE" "Partial ref that is a PR (#321) -> not tracked"

# T17: gitea -> guard is a no-op (matches the GitHub-only reset path).
reset_log
FORGE_TYPE="gitea"
PR_JSON='{"body":"Contributes to #123\n\nthen close #123"}'
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "" "$PARTIAL_CONFLICT_ISSUES" "FORGE_TYPE=gitea -> conflict guard no-op"
FORGE_TYPE="github"

# T17b: --dry-run still DETECTS and reports the conflict, but must not phrase it
# as an accomplished merge (same dry-run contract as
# _check_no_open_stacked_children: report the would-be outcome, prefixed).
reset_log
DRY_RUN=true
PR_JSON='{"body":"Verify, then close #123.\n\nContributes to #123"}'
run_capturing_stderr _check_partial_increment_close_conflict
dryrun_err="$(read_stderr)"
assert_eq "123" "$PARTIAL_CONFLICT_ISSUES" \
  "--dry-run still detects the closing-keyword conflict"
assert_contains "$dryrun_err" "[dry-run] Partial-increment conflict (#4569)" \
  "--dry-run prefixes the conflict warning"
assert_contains "$dryrun_err" "would reopen #123" \
  "--dry-run phrases the reopen as conditional, not as an accomplished action"
assert_not_contains "$dryrun_err" "will reopen #123" \
  "--dry-run does not claim a reopen will happen"
DRY_RUN=false

echo ""
echo "Testing the commit-message closing-keyword signal (#4595)..."

# T21: the #4595 shape — PR body is CLEAN (`Contributes to #123` only), but a
# commit message carries `close #123`. This repo squash-merges without overriding
# the commit message, so GitHub composes the squash message from the commits and
# honors that keyword: it must be recorded as a conflict.
reset_log
set_commits "feat: implement the slice

close #123"
PR_JSON='{"body":"Implements a slice.\n\nContributes to #123"}'
run_capturing_stderr _check_partial_increment_close_conflict
commit_err="$(read_stderr)"
assert_eq "123" "$PARTIAL_CONFLICT_ISSUES" \
  "Clean body + commit message 'close #123' -> conflict recorded (commit-message signal)"
assert_eq "123" "$PARTIAL_OPEN_BEFORE_MERGE" \
  "Commit-message conflict -> #123 also recorded as open before the merge"
assert_contains "$commit_err" "closing keyword in a commit message of this PR" \
  "Commit-message conflict -> warning names the commit message as the source"
assert_contains "$commit_err" "close #123" \
  "Commit-message conflict -> warning quotes the offending commit-message phrase"
assert_contains "$commit_err" "reword the offending commit message" \
  "Commit-message conflict -> remedy is amend/reword, not editing the PR body"
assert_not_contains "$commit_err" "its body ALSO carries" \
  "Commit-message conflict -> does NOT blame the (clean) PR body"

# T22: commits fetch FAILS (e.g. REST error) -> empty signal, conservative
# behavior: still tracked as open-before-merge, but no conflict, so the
# post-merge pass warns instead of reopening. Never blocks the merge.
reset_log
export LOOM_TEST_COMMITS_FAIL=1
PR_JSON='{"body":"Implements a slice.\n\nContributes to #123"}'
run_capturing_stderr _check_partial_increment_close_conflict
assert_eq "" "$PARTIAL_CONFLICT_ISSUES" \
  "Commits fetch failure -> degrades to an empty signal (no conflict recorded)"
assert_eq "123" "$PARTIAL_OPEN_BEFORE_MERGE" \
  "Commits fetch failure -> #123 still tracked as open before the merge"
unset LOOM_TEST_COMMITS_FAIL

# T22b: fetch failure is survivable — the guard still returns 0 (best-effort,
# never fails or blocks the merge).
reset_log
export LOOM_TEST_COMMITS_FAIL=1
PR_JSON='{"body":"Contributes to #123"}'
guard_rc=0
_check_partial_increment_close_conflict 2>/dev/null || guard_rc=$?
assert_eq "0" "$guard_rc" "Commits fetch failure -> guard still exits 0 (never blocks the merge)"
unset LOOM_TEST_COMMITS_FAIL

# T23: clean body AND clean commits -> no conflict at all.
reset_log
set_commits "feat: implement the slice" "docs: note the follow-up work"
PR_JSON='{"body":"Implements a slice.\n\nContributes to #123"}'
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "" "$PARTIAL_CONFLICT_ISSUES" \
  "Clean body + clean commits -> no conflict recorded"
assert_eq "123" "$PARTIAL_OPEN_BEFORE_MERGE" \
  "Clean body + clean commits -> #123 recorded as open before the merge"

# T24: multi-commit PR where only a MIDDLE commit carries the keyword — all
# commit messages must be aggregated, not just the first or last.
reset_log
set_commits "feat: part one" "fixup: resolves #123 while here" "chore: part three"
PR_JSON='{"body":"Part of #123"}'
run_capturing_stderr _check_partial_increment_close_conflict
mid_err="$(read_stderr)"
assert_eq "123" "$PARTIAL_CONFLICT_ISSUES" \
  "Middle commit's 'resolves #123' -> conflict recorded (all commit messages aggregated)"
assert_contains "$mid_err" "resolves #123" \
  "Middle-commit conflict -> warning quotes the offending phrase"

# T25: 'close issue #123' in a commit message is NOT a closing reference (same
# word-adjacency rule the body regex enforces).
reset_log
set_commits "chore: follow-up will close issue #123 later"
PR_JSON='{"body":"Part of #123"}'
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "" "$PARTIAL_CONFLICT_ISSUES" \
  "Commit 'close issue #123' -> not a closing reference, no conflict"

# T26: a commit closing a DIFFERENT issue than the partial ref -> ignored.
reset_log
set_commits "fix: closes #888"
PR_JSON='{"body":"Part of #123"}'
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "" "$PARTIAL_CONFLICT_ISSUES" \
  "Commit 'closes #888' + Part of #123 -> no conflict (different issues)"

# T27: end-to-end — the commit-message conflict recorded pre-merge drives the
# post-merge reopen. #456 is open when the guard runs and closed when the reset
# pass runs (the fixture is swapped mid-test to simulate the merge's auto-close).
reset_log
set_commits "feat: slice

close #456"
PR_JSON='{"body":"Implements a slice.\n\nContributes to #456"}'
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "456" "$PARTIAL_CONFLICT_ISSUES" \
  "End-to-end: commit-message conflict on #456 recorded pre-merge"
: > "$STUB_DIR/gh-calls.log"
cat > "$STUB_DIR/issue-456.json" <<'EOF'
{"state":"closed","labels":[{"name":"loom:building"}]}
EOF
retired "T27 (post-merge half): the commit-message conflict recorded pre-merge drives the post-merge reopen + swap of #456" \
    "an issue a stray commit-message \`close #N\` closed against a declared partial increment is reopened and returned to the queue" \
    "the script no longer runs a post-merge pass, and the daemon cannot read this script's globals; it re-derives the pre-merge facts from the issue's close events and the PR's closing references" \
    "merge_observation_tests::a_preventive_reopens_only_what_this_merge_closed and missing_attribution_skips_the_dependent_reopen (re-derived facts, fail direction), partial_reset::tests::t18_t27_conflicted_and_closed_reopens_then_swaps (the decision)"
# Restore the shared fixture for any later case.
cat > "$STUB_DIR/issue-456.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:building"}]}
EOF

# T28: the commit fetch must not happen at all when the PR has no partial-
# increment reference (zero extra API calls on the common path). A failing
# commits endpoint would be reached only if the guard fetched it.
reset_log
export LOOM_TEST_COMMITS_FAIL=1
PR_JSON='{"body":"All done.\n\nCloses #123"}'
run_capturing_stderr _check_partial_increment_close_conflict
nofetch_err="$(read_stderr)"
assert_eq "" "$PARTIAL_OPEN_BEFORE_MERGE" \
  "No partial-increment ref -> guard returns before any commit fetch"
assert_not_contains "$nofetch_err" "simulated commits fetch failure" \
  "No partial-increment ref -> commits endpoint is never called"
unset LOOM_TEST_COMMITS_FAIL

# T31 (#8191): the conflict decision moved to `loom-daemon merge-pr
# partial-conflict`. A daemon that cannot answer must REFUSE the merge -- an
# unread plan is not an empty one, and read as empty it would record nothing
# for the post-merge pass to revert -- while --dry-run only reports it.
reset_log
fake_no_conflict="$STUB_DIR/fake-loom-daemon-no-partial-conflict"
printf '#!/usr/bin/env bash\necho "error: unrecognized subcommand" >&2\nexit 2\n' > "$fake_no_conflict"
chmod +x "$fake_no_conflict"
saved_bin="${LOOM_DAEMON_BIN:-}"
export LOOM_DAEMON_BIN="$fake_no_conflict"
PR_JSON='{"body":"Verify, then close #123.\n\nContributes to #123"}'
nc_rc=0
( _check_partial_increment_close_conflict ) 2>"$STUB_DIR/stderr.log" || nc_rc=$?
assert_eq "1" "$nc_rc" "#8191: daemon without 'merge-pr partial-conflict' -> the merge is refused"
assert_contains "$(read_stderr)" "partial-increment close-conflict guard (#4569) could not run" \
  "#8191: the refusal names the guard that could not run"
DRY_RUN=true
nc_rc=0
( _check_partial_increment_close_conflict ) 2>"$STUB_DIR/stderr.log" || nc_rc=$?
DRY_RUN=false
export LOOM_DAEMON_BIN="$saved_bin"
assert_eq "0" "$nc_rc" "#8191: --dry-run with no answerable daemon -> reported, not exited"
assert_contains "$(read_stderr)" "[dry-run] Would BLOCK merge of PR #999" \
  "#8191: --dry-run phrases the refusal as conditional"

echo ""
echo "Testing the post-merge premature-close revert (RETIRED by #14905)..."
retired "T18/T19/T20: conflicted + closed after the merge -> reopen, comment, then swap; open-before but unattributed -> warn only; closed before the merge -> untouched" \
    "only an issue THIS merge closed against its declared intent is reopened; a deliberate human close inside the window is never reverted" \
    "the wrapper is gone; the daemon re-derives 'closed by THIS merge' from close events instead of reading script globals" \
    "partial_reset::tests::t18_t27_conflicted_and_closed_reopens_then_swaps, t19_open_before_but_unattributed_warns_and_does_not_reopen, t3_t20_closed_and_untracked_is_a_logged_skip; merge_observation_tests::a_preventive_reopens_only_what_this_merge_closed (closed_by_merge verdicts)"
retired "T29/T30/T31(audit bodies)/T31b/T32: daemon-verb absent or off-protocol -> nothing mutated + manual remedy; a failed reopen stops the pass; comment bodies rendered by partial-comment; a held remainder swaps to loom:operator" \
    "the pass is fail-closed, stops after a failed reopen, posts the byte-frozen audit bodies, and honors loom:remainder-hold" \
    "the 'daemon without the verb' wrapper warning is gone with the wrapper; the old-daemon case is now the advisory _warn_if_daemon_lacks_merge_observation (asserted in test-merge-pr-daemon-version-floor.sh)" \
    "post_merge_bookkeeping::tests::partial_reset_stops_when_the_reopen_fails, partial_reset_failed_swap_warns_and_posts_no_comment, partial_reset_comment_failure_is_a_warning_after_the_swap; partial_reset::tests::a_marker_swaps_to_operator_and_quotes_the_reason, a_conflicted_close_with_a_marker_reopens_then_holds; merge_observation_tests::a_held_remainder_swaps_to_the_operator_label; tests/merge_pr_partial_comment_differential.rs (bodies byte-for-byte)"

echo ""
echo "Testing merge-pr.sh source guards..."
src="$(cat "$MERGE_PR_SRC")"
assert_not_contains "$src" "_reset_partial_increment_labels ||" \
  "merge-pr.sh no longer calls the retired post-merge partial-increment reset (#14905)"
retired "merge-pr.sh's source contains the (Part of|Contributes to) alternation" \
    "the non-closing partial-increment vocabulary must not be silently narrowed or dropped by a refactor -- a dropped keyword means a declared partial increment is read as a plain close" \
    "the alternation is no longer in this file; it is a Rust pattern in loom-daemon/src/merge_pr/refs.rs, so no grep of merge-pr.sh can pass" \
    "merge_pr::refs::tests::a_line_leading_declaration_is_still_read pins all nine accepted spellings, and tests/merge_pr_refs_differential.rs proves the port agrees with the RETIRED shell byte-for-byte on a 29-entry corpus (frozen fixture). A scan checks the text is present; the differential checks the behaviour is identical, which subsumes it."
# The label swap / reopen / comment mutations route through the #4856
# rate-limit-safe wrappers in lib/forge-helpers.sh, so the source guards assert
# on the WRAPPER call sites (with their label/issue arguments) rather than the
# pre-#4856 raw `gh issue edit --remove-label ... --add-label ...` / `gh issue
# reopen ...` literals, which no longer appear in merge-pr.sh. The intent is
# unchanged: fail loudly if a refactor drops the loom:building -> loom:issue
# swap, the premature-auto-close revert, or the REPO_NWO scoping.
retired "merge-pr.sh's source swaps loom:building -> loom:issue via the rate-limit-safe wrapper (#4856)" \
    "the partial-increment reset must go through the #4856 wrapper (REST fallback) rather than a raw gh issue edit" \
    "the call sites moved into loom-daemon/src/merge_pr/post_merge_bookkeeping.rs, which reaches the wrapper through a child bash; merge-pr.sh no longer contains the call text, so the grep cannot pass" \
    "the behavioural cases above drive the real wrapper against the stub gh and assert the recorded call: T1/T2 ('issue edit 123 --repo owner/repo --remove-label loom:building --add-label loom:issue', repo-scoped), the #4569 reopen cases ('issue reopen' then the swap), the comment cases ('issue comment 123'), and post_merge_bookkeeping::tests::partial_reset_* assert the helper name and arguments the orchestrator invokes."
retired "merge-pr.sh's source restores loom:issue as the swap's add-label argument" \
    "an un-held partial slice returns to loom:issue, not to any other label" \
    "the call sites moved into loom-daemon/src/merge_pr/post_merge_bookkeeping.rs, which reaches the wrapper through a child bash; merge-pr.sh no longer contains the call text, so the grep cannot pass" \
    "the behavioural cases above drive the real wrapper against the stub gh and assert the recorded call: T1/T2 ('issue edit 123 --repo owner/repo --remove-label loom:building --add-label loom:issue', repo-scoped), the #4569 reopen cases ('issue reopen' then the swap), the comment cases ('issue comment 123'), and post_merge_bookkeeping::tests::partial_reset_* assert the helper name and arguments the orchestrator invokes."
assert_contains "$src" "--repo \"\$REPO_NWO\"" \
  "merge-pr.sh scopes the partial-increment mutations to REPO_NWO"
retired "merge-pr.sh's source passes REPO_NWO to the label-swap wrapper" \
    "the swap is scoped to the PR's repository" \
    "the call sites moved into loom-daemon/src/merge_pr/post_merge_bookkeeping.rs, which reaches the wrapper through a child bash; merge-pr.sh no longer contains the call text, so the grep cannot pass" \
    "the behavioural cases above drive the real wrapper against the stub gh and assert the recorded call: T1/T2 ('issue edit 123 --repo owner/repo --remove-label loom:building --add-label loom:issue', repo-scoped), the #4569 reopen cases ('issue reopen' then the swap), the comment cases ('issue comment 123'), and post_merge_bookkeeping::tests::partial_reset_* assert the helper name and arguments the orchestrator invokes."
assert_contains "$src" "_check_partial_increment_close_conflict || true" \
  "merge-pr.sh invokes the #4569 conflict guard BEFORE either merge path"
retired "merge-pr.sh's source reverts a premature auto-close via the rate-limit-safe reopen wrapper (#4856)" \
    "the #4569 revert goes through the #4856 reopen wrapper" \
    "the call sites moved into loom-daemon/src/merge_pr/post_merge_bookkeeping.rs, which reaches the wrapper through a child bash; merge-pr.sh no longer contains the call text, so the grep cannot pass" \
    "the behavioural cases above drive the real wrapper against the stub gh and assert the recorded call: T1/T2 ('issue edit 123 --repo owner/repo --remove-label loom:building --add-label loom:issue', repo-scoped), the #4569 reopen cases ('issue reopen' then the swap), the comment cases ('issue comment 123'), and post_merge_bookkeeping::tests::partial_reset_* assert the helper name and arguments the orchestrator invokes."
# The BODY argument is no longer a `$comment` local: both bodies are
# `loom-daemon merge-pr partial-comment` since #8191's slice, so the third
# argument is now the daemon's output with its sentinel stripped. The assertion
# is narrowed to the part that is still this file's responsibility — that the
# post goes through the #4856 wrapper, repo-scoped and issue-scoped — and the
# body itself is asserted behaviourally by T31/T32 below and byte-for-byte by
# loom-daemon/tests/merge_pr_partial_comment_differential.rs. This is a
# narrowing of an existing assertion, not a retirement: nothing it used to
# forbid is now allowed.
retired "merge-pr.sh's source posts the partial-increment / premature-close comments via the rate-limit-safe wrapper (#4856)" \
    "the audit comments go through the #4856 comment chokepoint, repo- and issue-scoped" \
    "the call sites moved into loom-daemon/src/merge_pr/post_merge_bookkeeping.rs, which reaches the wrapper through a child bash; merge-pr.sh no longer contains the call text, so the grep cannot pass" \
    "the behavioural cases above drive the real wrapper against the stub gh and assert the recorded call: T1/T2 ('issue edit 123 --repo owner/repo --remove-label loom:building --add-label loom:issue', repo-scoped), the #4569 reopen cases ('issue reopen' then the swap), the comment cases ('issue comment 123'), and post_merge_bookkeeping::tests::partial_reset_* assert the helper name and arguments the orchestrator invokes."
retired "merge-pr.sh's source contains the closing-keyword alternation" \
    "the GitHub closing-keyword set (and its \\b guard, which is what stops 'Discloses #N' matching) must not drift from the one forge_pr_close_targets uses" \
    "the alternation is now a Rust pattern; the grep cannot pass against this file" \
    "merge_pr::refs::tests::every_documented_closing_keyword_form_is_recognised pins all ten forms and a_keyword_substring_does_not_match pins the boundary; the differential confirms agreement with the retired shell."
assert_contains "$src" 'repos/$REPO_NWO/pulls/$PR_NUMBER/commits' \
  "merge-pr.sh reads the PR's commit messages for closing keywords (#4595)"
assert_contains "$src" '--paginate' \
  "merge-pr.sh paginates the commits fetch (>30-commit PRs are not truncated)"
retired "merge-pr.sh's source contains the inline-code-span strip" \
    "inline code spans are blanked before the line-leading anchor runs, so the anchor sees the text a reader sees rather than the raw markup. (The #5234 incident itself is caught by the ANCHOR, not by this strip: its mention is mid-sentence. On a single line the strip only ever ADDS matches -- measured, 0 of 29 corpus entries change without it.)" \
    "the strip is now blank_inline_code() in Rust; this file no longer runs sed" \
    "merge_pr::refs::tests::the_inline_code_strip_changes_the_answer_and_this_pins_which_way, which asserts the one single-line shape where the strip is load-bearing (\"\`x\` Part of #5\" -> [5]) and would fail if the strip were removed -- the property the original successor did NOT pin."

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
