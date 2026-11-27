#!/usr/bin/env bash
# test-guide-awaiting-revision-park.sh - Regression test for issue #10936
#
# Guide converts a loom:blocked issue parked only as "awaiting (Architect)
# revision" / "awaiting a design epic" (no named blocker) onto the revise path.
# awaiting_revision_park is extracted VERBATIM from each shipped copy and run
# against fixture park records.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
TESTS_RUN=0; TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN + 1)); echo "  PASS: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo "  FAIL: $1"; }
check() { # name expected_rc body [blocked_at]
    awaiting_revision_park "$3" "${4:-}"; local rc=$?
    if [[ "$rc" == "$2" ]]; then pass "$1"; else fail "$1 (rc=$rc, expected $2)"; fi
}

for copy in "defaults/.claude/commands/loom/guide.md" "defaults/.agents/skills/loom-guide/SKILL.md"; do
    f="$REPO_ROOT/$copy"; echo "Copy: $copy"
    [[ -f "$f" ]] || { fail "$copy exists"; continue; }
    FN="$(sed -n '/^awaiting_revision_park() {/,/^}$/p' "$f")"
    [[ -n "$FN" ]] || { fail "extract awaiting_revision_park from $copy"; continue; }
    eval "$FN"

    P='<!-- loom:park Blocked by: (unstated) by=builder at=2026-11-01T10:00:00Z reason="%s" -->'
    check "awaiting Architect revision => convertible" 0 "$(printf "$P" 'awaiting Architect revision')"
    check "fresh design epic request => convertible" 0 \
        "$(printf "$P" 'awaiting a design epic')" "2026-11-01T10:00:30Z"
    check "case-insensitive reason" 0 "$(printf "$P" 'Awaiting architect REVISION of scope')"
    check "standalone revision request => convertible" 0 \
        "$(printf "$P" 'Awaiting a revision.')" "2026-11-01T10:00:30Z"
    check "explicit proposal revision request => convertible" 0 \
        "$(printf "$P" 'Awaiting proposal revision of the scope.')" "2026-11-01T10:00:30Z"
    check "plural architect revisions => convertible (#15050)" 0 \
        "$(printf "$P" 'awaiting architect revisions')" "2026-11-01T10:00:30Z"
    check "revision of the proposal => convertible (#15050)" 0 \
        "$(printf "$P" 'Awaiting revision of the proposal.')" "2026-11-01T10:00:30Z"
    check "plural design epics => convertible (#15050)" 0 \
        "$(printf "$P" 'awaiting design epics')" "2026-11-01T10:00:30Z"
    check "unrelated reason => left alone" 1 "$(printf "$P" 'needs an architecture ruling')"
    check "no park record => left alone" 1 "just a body"
    check "revision reason + open blocker named => left alone" 1 \
        '<!-- loom:park Blocked by: #123 by=builder at=2026-11-01T10:00:00Z reason="awaiting Architect revision" -->'
    check "cross-repo blocker named => left alone" 1 \
        '<!-- loom:park Blocked by: owner/repo#9 by=builder at=2026-11-01T10:00:00Z reason="awaiting a design epic" -->'
    check "park fresh vs latest loom:blocked label => convertible" 0 \
        "$(printf "$P" 'awaiting Architect revision')" "2026-11-01T10:00:30Z"
    check "fresh installed-revision evidence wait (#14788) => left alone" 1 \
        "$(printf "$P" 'Awaiting an authorized same-attempt record of a natural targeted Champion refusal that identifies the operation, deciding hook, installed revision and refusal category.')" "2026-11-01T10:00:30Z"
    check "fresh deployed-revision evidence wait (#14603) => left alone" 1 \
        "$(printf "$P" 'May already be fixed by #14555. Awaiting SigNoz verification on a deployed revision containing d89fb26c2 and comparison with a sanitized real Codex step.')" "2026-11-01T10:00:30Z"
    check "fresh wait for a revision to deploy => left alone" 1 \
        "$(printf "$P" 'Awaiting revision d89fb26c2 to be deployed for SigNoz verification.')" "2026-11-01T10:00:30Z"
    check "fresh wait for revision evidence => left alone" 1 \
        "$(printf "$P" 'Awaiting revision evidence from the deployed worker.')" "2026-11-01T10:00:30Z"
    check "revision of a non-park noun => left alone (#15050)" 1 \
        "$(printf "$P" 'Awaiting revision of the issue tracker config')" "2026-11-01T10:00:30Z"
    check "block re-applied >5min after record => stale, left alone" 1 \
        "$(printf "$P" 'awaiting Architect revision')" "2026-11-03T10:00:00Z"
done
echo "Tests run: $TESTS_RUN"
[[ $TESTS_FAILED -eq 0 ]] && echo "All tests passed" || { echo "Tests failed: $TESTS_FAILED"; exit 1; }
