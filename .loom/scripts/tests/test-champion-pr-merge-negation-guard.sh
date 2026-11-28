#!/usr/bin/env bash
# Champion's read-only post-merge check (#1057, #14905). The daemon tests
# negated_only_references_reopen_what_this_merge_closed and
# an_open_negated_only_issue_stays_open cover the reopen and its #8942 gate.
set -uo pipefail
TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$TEST_DIR/../../.." && pwd)"
DOC="$REPO_ROOT/.claude/commands/loom/champion-pr-merge.md"
[[ -f "$DOC" ]] || DOC="$REPO_ROOT/defaults/.claude/commands/loom/champion-pr-merge.md"
source "$TEST_DIR/lib/assert.sh"

fixture=$(mktemp -d)
trap 'rm -rf "$fixture"' EXIT
cd "$fixture" || exit 1
git init -q || exit 1
mkdir -p .loom/scripts/lib
awk '/^### Step 4:/{section=1} section && /^```bash$/{code=1;next}
     code && /^```$/{exit} code{print}' "$DOC" >step4
awk '/^    if .*BLOCKER_REPO.*LINKED_ISSUES/{code=1} code{print} code && /^    fi$/{exit}' "$DOC" >linked_blocker_check

# Fixture the IO boundary. Every target looks CLOSED before the daemon tick.
cat >.loom/scripts/lib/forge-helpers.sh <<'STUB'
forge_detect() { FORGE_TYPE="$fixture_forge"; }
forge_pr_close_targets() { printf '%s\n' 3 33 44 55; }
forge_get_repo_nwo() { echo owner/repo; }
forge_get_pr() { echo '{"head":{"sha":"abc1234"},"body":"Closes #33","merged":true,"merge_commit_sha":"def5678"}'; }
forge_get_issue_state() { echo CLOSED; }
STUB

gitea_api() {
    [[ "$*" = 'GET repos/owner/repo/git/commits/def5678' ]] || return 1
    echo '{"commit":{"message":"does not fix #3"}}'
}
gh() {
    if [[ "$*" = 'api repos/owner/repo/commits/def5678' ]]; then
        [[ "$commit_readable" = yes ]] || return 1
        echo '{"commit":{"message":"does not fix #3"}}'
    else
        echo "$*" >>unexpected-writes
        return 1
    fi
}
loom-daemon() {
    case "$*" in
        'config get champion.outOfBandAcGate --json') echo '{"found":true,"value":false}' ;;
        'merge-pr-refs has-unnegated-closing-ref --issue '*)
            [[ "$(cat)" = $'Closes #33\ndoes not fix #3' ]] || return 2
            case "$4" in
                3) return 1 ;;   # negated in the squash commit
                33) return 0 ;;  # explicit in the PR body
                44) return 3 ;;  # sidebar-only link
                *) return 2 ;;   # unavailable predicate
            esac ;;
        *) echo "$*" >>unexpected-writes; return 1 ;;
    esac
}

run_step4() (
    source ./step4 77
    echo "ready=${READY_TO_UNBLOCK:?expected ready issues}"
)
export fixture_forge=github
commit_readable=yes
assert_eq 'ready= 33 44' "$(run_step4)" "Step 4 admits explicit and sidebar links and skips negated or indeterminate targets"
commit_readable=no
if run_step4 >/dev/null; then fail "an unreadable merge message authorized unblocking"
else pass "an unreadable merge message defers unblocking"; fi
fixture_forge=gitea
assert_eq 'ready= 33 44' "$(run_step4)" "Gitea classifies the merge message from its own forge"
[[ ! -e unexpected-writes ]] && pass "Champion only reads" || fail "unexpected forge or daemon action"

# A scan triggered by #33 still checks its co-blockers, including repository.
export LINKED_ISSUES=$'3\n33\n44\n55'
READY_TO_UNBLOCK=" 33 44"
export THIS_REPO=owner/repo
check_blocker() {
    # The sourced Champion guard reads BLOCKER_REPO and BLOCKER_NUM.
    # shellcheck disable=SC2034
    local BLOCKER_REPO="$1" BLOCKER_NUM ALL_RESOLVED=true
    # shellcheck disable=SC2034
    for BLOCKER_NUM in "${@:2}"; do source ./linked_blocker_check; done
    echo "$ALL_RESOLVED"
}
assert_eq false "$(check_blocker OWNER/repo 33 3)" "a disclaimed co-blocker keeps the dependent blocked"
assert_eq true "$(check_blocker owner/repo 33 44)" "two ready targets pass the linked-issue check"
assert_eq true "$(check_blocker other/repo 3)" "a foreign issue with the same number uses its own state"

echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]]
