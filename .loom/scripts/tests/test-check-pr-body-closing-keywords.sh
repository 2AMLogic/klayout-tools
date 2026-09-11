#!/usr/bin/env bash
# test-check-pr-body-closing-keywords.sh - Smoke tests for
# check-pr-body-closing-keywords.sh (#1674)
#
# Reproduces the #1671/#1600 incident: PR #1671's body quoted an
# illustrative example, inside a single-backtick inline-code span,
# containing "(Closes #1600)" - unrelated checklist wording describing a
# *different* issue. GitHub's auto-close scanner matched it anyway and
# falsely closed issue #1600 the instant #1671 merged, even though #1600's
# real implementation PR was still open.
#
# Verified behavior:
#   - a closing-keyword occurrence inside a single-backtick inline-code span
#     is flagged (exit 2), reproducing the #1671/#1600 shape exactly
#   - a closing-keyword occurrence inside a fenced code block is flagged too
#   - the PR's own genuine closing line (`Closes #N` on its own line, not in
#     a code span) is NEVER flagged, including in a body that ALSO contains
#     a flagged code-span occurrence elsewhere
#   - a closing keyword in ordinary prose (not on its own line, not in a
#     code span) is NOT flagged - only code-span/fence occurrences are this
#     guard's business
#   - --body-file, --pr (mocked gh), and stdin input modes all agree
#   - --quiet suppresses the success summary but still reports failures
#   - --self-test exits 0
#   - --help exits 0 and prints usage
#   - unknown argument exits 1
#   - --body-file with a nonexistent path exits 1
#   - --body-file and --pr together exits 1 (mutually exclusive)
#
# Usage:
#   ./.loom/scripts/tests/test-check-pr-body-closing-keywords.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$HELPERS_DIR/check-pr-body-closing-keywords.sh"

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

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-pr-body-closing-keywords.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

# -------- Test 1: script exists and is executable --------
echo "Test 1: script exists and is executable"
if [[ -x "$SCRIPT" ]]; then
    pass "check-pr-body-closing-keywords.sh is executable"
else
    fail "check-pr-body-closing-keywords.sh is missing or not executable: $SCRIPT"
    echo "FAILED: $TESTS_FAILED/$TESTS_RUN"
    exit 1
fi

# -------- Test 2: --help exits 0 and prints usage --------
echo "Test 2: --help"
out=$("$SCRIPT" --help 2>&1); RC=$?
if [[ "$RC" -eq 0 && "$out" == *"check-pr-body-closing-keywords.sh"* ]]; then
    pass "--help exits 0 and prints usage"
else
    fail "expected exit 0 with usage text, got rc=$RC out=$out"
fi

# -------- Test 3: unknown argument exits 1 --------
echo "Test 3: unknown argument"
"$SCRIPT" --bogus >/dev/null 2>&1; RC=$?
if [[ "$RC" -eq 1 ]]; then pass "unknown arg exits 1"; else fail "expected 1, got $RC"; fi

# -------- Test 4: --self-test exits 0 --------
echo "Test 4: --self-test"
out=$("$SCRIPT" --self-test 2>&1); RC=$?
if [[ "$RC" -eq 0 && "$out" == *"self-test passed"* ]]; then
    pass "--self-test exits 0"
else
    fail "expected exit 0 from --self-test, got rc=$RC out=$out"
fi

# -------- Test 5: the exact #1671/#1600 incident shape is flagged --------
echo "Test 5: #1671/#1600 incident reproduction"
incident_body='## Summary

`_extract_named_deps` in `.loom/scripts/dep-recheck-fingerprint.sh` required
a checklist item'"'"'s `#N` reference to appear immediately after the
checkbox. A prose-prefixed form like
`- [ ] PR #1607 (Closes #1600) merged - ...` did not match, so the
extraction silently returned zero dependencies.

## Test plan

- [x] 62/62 tests pass

Closes #1669'
out=$(printf '%s\n' "$incident_body" | "$SCRIPT" 2>&1); RC=$?
if [[ "$RC" -eq 2 && "$out" == *"1600"* ]]; then
    pass "inline-code-quoted 'Closes #1600' is flagged, naming the offending text"
else
    fail "expected exit 2 naming 1600, got rc=$RC out=$out"
fi
if [[ "$out" == *"Closes #1669"* ]]; then
    fail "the PR's own genuine 'Closes #1669' line must not appear in the risk report"
else
    pass "the PR's own genuine 'Closes #1669' line is not reported as risk"
fi

# -------- Test 6: a clean body (genuine closing line, no code-span keywords) passes --------
echo "Test 6: clean body passes"
clean_body='## Summary

Implements the feature.

## Test plan

- [x] tests pass

Closes #123'
out=$(printf '%s\n' "$clean_body" | "$SCRIPT" --quiet 2>&1); RC=$?
if [[ "$RC" -eq 0 && -z "$out" ]]; then
    pass "clean body exits 0 with --quiet producing no output"
else
    fail "expected silent exit 0, got rc=$RC out=$out"
fi

# -------- Test 7: fenced code block keyword text is flagged --------
echo "Test 7: fenced code block"
fenced_body='## Summary

Example transcript:

```
- [ ] PR #999 (Fixes #888) merged
```

Closes #123'
out=$(printf '%s\n' "$fenced_body" | "$SCRIPT" 2>&1); RC=$?
if [[ "$RC" -eq 2 && "$out" == *"888"* ]]; then
    pass "keyword text inside a fenced code block is flagged"
else
    fail "expected exit 2 naming 888, got rc=$RC out=$out"
fi
if [[ "$out" == *"Closes #123"* ]]; then
    fail "the genuine own-line 'Closes #123' must not appear in the risk report"
else
    pass "the genuine own-line 'Closes #123' is not reported as risk (fenced-body case)"
fi

# -------- Test 8: mid-sentence prose keyword (not in code) is NOT flagged --------
echo "Test 8: mid-sentence prose keyword is not a code-span occurrence"
midsentence_body='## Summary

This PR closes #123 as part of a larger cleanup effort described below.'
out=$(printf '%s\n' "$midsentence_body" | "$SCRIPT" --quiet 2>&1); RC=$?
if [[ "$RC" -eq 0 ]]; then
    pass "mid-sentence prose keyword (outside any code span) is not flagged"
else
    fail "expected exit 0, got rc=$RC out=$out"
fi

# -------- Test 9: --body-file mode agrees with stdin mode --------
echo "Test 9: --body-file mode"
printf '%s\n' "$incident_body" > "$WORKDIR/incident-body.txt"
out=$("$SCRIPT" --body-file "$WORKDIR/incident-body.txt" 2>&1); RC=$?
if [[ "$RC" -eq 2 && "$out" == *"1600"* ]]; then
    pass "--body-file mode reproduces the flagged result"
else
    fail "expected exit 2 naming 1600 via --body-file, got rc=$RC out=$out"
fi

# -------- Test 10: --body-file with a nonexistent path exits 1 --------
echo "Test 10: --body-file nonexistent path"
"$SCRIPT" --body-file "$WORKDIR/does-not-exist.txt" >/dev/null 2>&1; RC=$?
if [[ "$RC" -eq 1 ]]; then
    pass "--body-file nonexistent path exits 1"
else
    fail "expected 1, got $RC"
fi

# -------- Test 11: --body-file and --pr together exits 1 --------
echo "Test 11: --body-file and --pr are mutually exclusive"
"$SCRIPT" --body-file "$WORKDIR/incident-body.txt" --pr 123 >/dev/null 2>&1; RC=$?
if [[ "$RC" -eq 1 ]]; then
    pass "--body-file + --pr together exits 1"
else
    fail "expected 1, got $RC"
fi

# -------- Test 12: --pr mode via a stubbed gh on PATH --------
echo "Test 12: --pr mode (stubbed gh)"
STUB_BIN="$WORKDIR/bin"
mkdir -p "$STUB_BIN"
cat > "$STUB_BIN/gh" <<STUB
#!/usr/bin/env bash
if [[ "\$1" == "pr" && "\$2" == "view" ]]; then
    cat "$WORKDIR/incident-body.txt"
    exit 0
fi
exit 1
STUB
chmod +x "$STUB_BIN/gh"
out=$(PATH="$STUB_BIN:$PATH" "$SCRIPT" --pr 1671 2>&1); RC=$?
if [[ "$RC" -eq 2 && "$out" == *"1600"* ]]; then
    pass "--pr mode fetches via gh and reproduces the flagged result"
else
    fail "expected exit 2 naming 1600 via --pr, got rc=$RC out=$out"
fi

# -------- Test 13: --pr mode without gh on PATH exits 1 --------
echo "Test 13: --pr mode without gh"
# A PATH containing ONLY a bash symlink (no gh) - the script's own
# `#!/usr/bin/env bash` shebang still needs a resolvable bash, but
# `command -v gh` inside it must fail.
BASH_ONLY_BIN="$WORKDIR/bash-only-bin"
mkdir -p "$BASH_ONLY_BIN"
ln -sf "$(command -v bash)" "$BASH_ONLY_BIN/bash"
out=$(PATH="$BASH_ONLY_BIN" "$SCRIPT" --pr 1671 2>&1); RC=$?
if [[ "$RC" -eq 1 ]]; then
    pass "--pr mode without gh on PATH exits 1"
else
    fail "expected 1, got rc=$RC out=$out"
fi

# -------- Test 14: --quiet still reports failures --------
echo "Test 14: --quiet reports failures"
out=$(printf '%s\n' "$incident_body" | "$SCRIPT" --quiet 2>&1); RC=$?
if [[ "$RC" -eq 2 && "$out" == *"1600"* ]]; then
    pass "--quiet still reports a flagged occurrence"
else
    fail "expected exit 2 naming 1600 even with --quiet, got rc=$RC out=$out"
fi

# -------- Summary --------
echo ""
if [[ "$TESTS_FAILED" -eq 0 ]]; then
    echo -e "${GREEN}All $TESTS_PASSED/$TESTS_RUN tests passed${NC}"
    exit 0
else
    echo -e "${RED}FAILED: $TESTS_FAILED/$TESTS_RUN tests failed${NC}"
    exit 1
fi
