#!/usr/bin/env bash
# check-pr-body-closing-keywords.sh - Flag closing-keyword text
# (`Closes #N` / `Fixes #N` / `Resolves #N`, and conjugations) that appears
# INSIDE a markdown inline-code span or fenced code block in a PR body or
# commit message.
#
# #1674: GitHub's issue auto-close keyword scanner matches these keywords on
# raw text - it is NOT restricted to a genuine closing directive, and does
# NOT respect markdown code-span/fence delimiters. PR #1671's body quoted an
# illustrative example, inside a single-backtick inline-code span, of a
# *different* issue's checklist wording:
#
#   `- [ ] PR #1607 (Closes #1600) merged - ...` did not match, so the
#
# GitHub matched the `(Closes #1600)` inside that code span anyway and
# falsely auto-closed issue #1600 the instant #1671 merged, even though
# #1600's actual implementation PR (#1607) was still open. A Curator pass
# caught this only by accident (it was re-checking a dependent issue whose
# blocker had unexpectedly "resolved"); nothing else in the pipeline would
# have surfaced the false close.
#
# This script is the guard: it scans a PR body (or commit message) and
# reports every closing-keyword occurrence found inside an inline-code span
# (single backticks) or a fenced code block (``` or ~~~), which is exactly
# the shape GitHub cannot tell apart from a real directive. A closing
# keyword in plain prose - including the PR's own genuine `Closes #N` line -
# is never flagged; that is the intended, working case this guard must stay
# silent on.
#
# Usage:
#   ./.loom/scripts/check-pr-body-closing-keywords.sh --body-file <path>
#   ./.loom/scripts/check-pr-body-closing-keywords.sh --pr <N>
#   echo "$BODY" | ./.loom/scripts/check-pr-body-closing-keywords.sh
#   ./.loom/scripts/check-pr-body-closing-keywords.sh --self-test
#   ./.loom/scripts/check-pr-body-closing-keywords.sh --help
#
# Exit codes:
#   0 - no closing-keyword text found inside a code span or fence.
#   1 - usage error (bad arguments, --pr lookup failed, missing gh, etc).
#   2 - one or more closing-keyword occurrences found inside a code span or
#       fence - every offender is printed with its line content.
#
# Notes:
#   - Detection is a per-LINE heuristic: a line is split on the literal `
#     character, alternating prose/code segments starting with prose (so an
#     even count of backticks per line is assumed - the common case, and the
#     exact shape of the #1671 incident). A closing-keyword span that itself
#     crosses a line break is not detected; that is a known limitation, not
#     the failure mode this guard targets.
#   - Fenced code blocks (```...``` or ~~~...~~~) are tracked across the
#     whole body: every line between an opening and closing fence counts as
#     code, including the fence delimiter lines themselves are excluded from
#     scanning (they are markdown syntax, not content).
#   - The keyword set mirrors GitHub's own auto-close vocabulary as this
#     repo's builder-pr.md documents it: close/closes/closed/closing,
#     fix/fixes/fixed/fixing, resolve/resolves/resolved/resolving, each
#     immediately followed by optional whitespace and `#<digits>`.
#   - This is advisory tooling (run it by hand, or wire it into Judge/Doctor
#     PR-body review) - it cannot stop GitHub's own scanner. The fix for a
#     flagged occurrence is to break the keyword/`#N` adjacency in the
#     quoted example (see .loom/docs/pr-body-closing-keywords.md).

set -uo pipefail

EXIT_OK=0
EXIT_USAGE=1
EXIT_RISK_FOUND=2

# GitHub's closing-keyword vocabulary (see builder-pr.md), immediately
# followed by optional whitespace and a `#<digits>` reference.
KEYWORD_ERE='\b([Cc]lose[sd]?|[Cc]losing|[Ff]ix(e[sd])?|[Ff]ixing|[Rr]esolve[sd]?|[Rr]esolving)[[:space:]]*#[0-9]+'

BODY_FILE=""
PR_NUMBER=""
SELF_TEST=0
QUIET=0

usage() {
    awk 'NR > 1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$0"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            exit "$EXIT_OK"
            ;;
        --body-file)
            if [[ $# -lt 2 || -z "$2" ]]; then
                echo "check-pr-body-closing-keywords.sh: --body-file requires a path argument" >&2
                exit "$EXIT_USAGE"
            fi
            BODY_FILE="$2"
            shift 2
            ;;
        --pr)
            if [[ $# -lt 2 || -z "$2" ]]; then
                echo "check-pr-body-closing-keywords.sh: --pr requires a PR number argument" >&2
                exit "$EXIT_USAGE"
            fi
            PR_NUMBER="$2"
            shift 2
            ;;
        --self-test)
            SELF_TEST=1
            shift
            ;;
        --quiet|-q)
            QUIET=1
            shift
            ;;
        *)
            echo "check-pr-body-closing-keywords.sh: unknown argument: $1" >&2
            echo "Run with --help for usage." >&2
            exit "$EXIT_USAGE"
            ;;
    esac
done

# ---- Core scan (used by both normal operation and --self-test) ------------
#
# Reads body text on stdin, prints "CODE:<line>" for every line-fragment that
# is inside a code span/fence, "PROSE:<line>" for every fragment that is not.
# Kept as one small awk pass so both callers exercise identical logic.
split_prose_and_code() {
    awk '
        BEGIN { in_fence = 0 }
        /^(```|~~~)/ {
            in_fence = !in_fence
            next
        }
        in_fence {
            print "CODE:" $0
            next
        }
        {
            n = split($0, parts, "`")
            for (i = 1; i <= n; i++) {
                if (i % 2 == 1) {
                    print "PROSE:" parts[i]
                } else {
                    print "CODE:" parts[i]
                }
            }
        }
    '
}

# Runs the scan against the body text passed as $1. Appends to the global
# RISK_LINES / GENUINE_LINES arrays (reset by the caller before invoking).
# Deliberately takes the body as an ARGUMENT rather than via a pipe into this
# function: `some_producer | scan_body` would run scan_body in a subshell,
# and any array appends inside a pipeline subshell are invisible to the
# caller once the pipe exits - the process-substitution redirect below keeps
# scan_body itself in the current shell.
scan_body() {
    local body="$1"
    local line
    while IFS= read -r line; do
        case "$line" in
            CODE:*)
                local content="${line#CODE:}"
                if [[ "$content" =~ $KEYWORD_ERE ]]; then
                    RISK_LINES+=("$content")
                fi
                ;;
            PROSE:*)
                local content="${line#PROSE:}"
                if [[ "$content" =~ $KEYWORD_ERE ]]; then
                    GENUINE_LINES+=("$content")
                fi
                ;;
        esac
    done < <(split_prose_and_code <<< "$body")
}

# ---- Self-test --------------------------------------------------------------

if [[ "$SELF_TEST" -eq 1 ]]; then
    st_fail=0

    st_assert() {
        local label="$1" expected="$2" actual="$3"
        if [[ "$expected" == "$actual" ]]; then
            echo "  ok: $label"
        else
            echo "  FAIL: $label (expected exit $expected, got $actual)" >&2
            st_fail=1
        fi
    }

    # 1. The #1671/#1600 incident shape: an inline-code-quoted example
    #    containing "(Closes #1600)" describing unrelated checklist syntax,
    #    plus the PR's own genuine closing line on its own paragraph.
    incident_body='## Summary

`_extract_named_deps` required a checklist item ref immediately after the
checkbox. A prose-prefixed form like
`- [ ] PR #1607 (Closes #1600) merged - ...` did not match, so the
extraction silently returned zero dependencies.

## Test plan

- [x] tests pass

Closes #1671'
    out=$(printf '%s\n' "$incident_body" | "$0" 2>&1); rc=$?
    st_assert "#1671/#1600 incident shape is flagged" "$EXIT_RISK_FOUND" "$rc"
    if [[ "$out" == *"1600"* ]]; then
        echo "  ok: offending #1600 text is reported"
    else
        echo "  FAIL: expected report to mention 1600, got: $out" >&2
        st_fail=1
    fi
    if [[ "$out" == *"Closes #1671"* ]]; then
        echo "  FAIL: genuine own-line Closes #1671 must not be reported as risk" >&2
        st_fail=1
    else
        echo "  ok: genuine own-line Closes #1671 is not reported as risk"
    fi

    # 2. A clean PR body: only a genuine closing line, no code spans
    #    containing keyword+#N at all.
    clean_body='## Summary

Implements the feature.

## Test plan

- [x] tests pass

Closes #123'
    printf '%s\n' "$clean_body" | "$0" --quiet >/dev/null 2>&1
    st_assert "clean body (no code-span keyword) passes" "$EXIT_OK" "$?"

    # 3. Closing-keyword text inside a FENCED code block is flagged too.
    fenced_body='## Summary

Example transcript:

```
- [ ] PR #999 (Fixes #888) merged
```

Closes #123'
    out=$(printf '%s\n' "$fenced_body" | "$0" 2>&1); rc=$?
    st_assert "fenced-code-block keyword text is flagged" "$EXIT_RISK_FOUND" "$rc"
    if [[ "$out" == *"888"* ]]; then
        echo "  ok: offending #888 text (inside fence) is reported"
    else
        echo "  FAIL: expected report to mention 888, got: $out" >&2
        st_fail=1
    fi

    # 4. A genuine closing keyword NOT on its own line (mid-sentence prose,
    #    not in a code span) must NOT be flagged - only code-span/fence
    #    occurrences are this guard's business.
    midsentence_body='## Summary

This PR closes #123 as part of a larger cleanup effort described below.'
    printf '%s\n' "$midsentence_body" | "$0" --quiet >/dev/null 2>&1
    st_assert "mid-sentence prose keyword (not in code) passes" "$EXIT_OK" "$?"

    # 5. --body-file mode.
    st_tmp=$(mktemp)
    printf '%s\n' "$incident_body" > "$st_tmp"
    "$0" --body-file "$st_tmp" --quiet >/dev/null 2>&1
    st_assert "--body-file mode reproduces the flagged result" "$EXIT_RISK_FOUND" "$?"
    rm -f "$st_tmp"

    if [[ "$st_fail" -ne 0 ]]; then
        echo "check-pr-body-closing-keywords.sh: --self-test FAILED" >&2
        exit "$EXIT_RISK_FOUND"
    fi
    echo "check-pr-body-closing-keywords.sh: --self-test passed"
    exit "$EXIT_OK"
fi

# ---- Normal operation -------------------------------------------------------

if [[ -n "$BODY_FILE" && -n "$PR_NUMBER" ]]; then
    echo "check-pr-body-closing-keywords.sh: --body-file and --pr are mutually exclusive" >&2
    exit "$EXIT_USAGE"
fi

BODY=""
if [[ -n "$PR_NUMBER" ]]; then
    if ! command -v gh >/dev/null 2>&1; then
        echo "check-pr-body-closing-keywords.sh: --pr requires the gh CLI, which is not on PATH" >&2
        exit "$EXIT_USAGE"
    fi
    if ! BODY=$(gh pr view "$PR_NUMBER" --json body -q .body 2>&1); then
        echo "check-pr-body-closing-keywords.sh: failed to fetch PR #$PR_NUMBER body: $BODY" >&2
        exit "$EXIT_USAGE"
    fi
elif [[ -n "$BODY_FILE" ]]; then
    if [[ ! -f "$BODY_FILE" ]]; then
        echo "check-pr-body-closing-keywords.sh: --body-file path does not exist: $BODY_FILE" >&2
        exit "$EXIT_USAGE"
    fi
    BODY=$(cat "$BODY_FILE")
else
    BODY=$(cat)
fi

RISK_LINES=()
GENUINE_LINES=()
scan_body "$BODY"

if [[ "${#RISK_LINES[@]}" -gt 0 ]]; then
    echo "ERROR: check-pr-body-closing-keywords.sh: closing-keyword text found inside a code span or fence:" >&2
    for l in "${RISK_LINES[@]}"; do
        echo "  $l" >&2
    done
    echo "" >&2
    echo "  GitHub's auto-close scanner matches these keywords on raw text and does" >&2
    echo "  NOT respect markdown code-span/fence delimiters (#1674) - an illustrative" >&2
    echo "  example quoting 'Closes #N'-shaped text can falsely auto-close an" >&2
    echo "  unrelated issue when this PR merges, exactly as happened to issue #1600" >&2
    echo "  via PR #1671's quoted example." >&2
    echo "  Fix: break the keyword/#N adjacency in the quoted text (e.g. 'Closes" >&2
    echo "  #&#8203;N', 'Closes # N', or rephrase to avoid the literal adjacency)." >&2
    echo "  See .loom/docs/pr-body-closing-keywords.md." >&2
    exit "$EXIT_RISK_FOUND"
fi

if [[ "$QUIET" -eq 0 ]]; then
    if [[ "${#GENUINE_LINES[@]}" -gt 0 ]]; then
        echo "check-pr-body-closing-keywords.sh: no code-span/fence closing-keyword risk found. Genuine closing reference(s):"
        for l in "${GENUINE_LINES[@]}"; do
            echo "  $l"
        done
    else
        echo "check-pr-body-closing-keywords.sh: no closing-keyword text found (in code spans or prose)."
    fi
fi
exit "$EXIT_OK"
