#!/usr/bin/env bash
#
# check-release-lag.sh — report how far `main` has drifted ahead of the
# latest tagged release (issue #1020).
#
# RELEASING.md's "Release cadence" section already defines a commit-count
# backstop: a release is due once `main` is more than N commits ahead of the
# latest tag. Before this script, that rule was memory-dependent — nothing
# computed it, so the gap grew silently (240 commits ahead of v0.2.0 by
# 2026-08-15, ~10x the backstop, which is what surfaced issue #1020).
#
# This script makes the rule self-reporting. It is strictly READ-ONLY and
# ADVISORY: it never tags, pushes, or triggers .github/workflows/publish.yml.
# Cutting a release is .github/workflows/release.yml's job (see RELEASING.md).
#
# The threshold is *parsed from RELEASING.md* rather than hardcoded here, so
# the two can never drift apart silently — editing the policy edits this
# check. The `git rev-list --count` invocation below deliberately mirrors the
# one RELEASING.md documents:
#
#   git rev-list --count "$(git tag --sort=-creatordate | head -1)"..origin/main
#
# Usage:
#   scripts/check-release-lag.sh [options]
#
#   --repo DIR         Repository to inspect (default: this script's repo
#                      root). Also where RELEASING.md is read from.
#   --ref REF          Ref to measure the tag against (default: origin/main;
#                      falls back to main, then HEAD, when absent — e.g. in a
#                      clone with no remote).
#   --threshold N      Override the backstop instead of parsing RELEASING.md.
#   --format FORMAT    text (default) | json | markdown.
#   --annotate         Also emit GitHub Actions workflow-command annotations
#                      (::warning:: / ::notice::) on stdout.
#   --help             Show this help and exit.
#
# Environment:
#   GITHUB_STEP_SUMMARY  When set to a writable path, the markdown report is
#                        appended to it (so a CI run surfaces the signal in
#                        the job summary, not only in the log).
#
#   GH_TOKEN/GITHUB_TOKEN  When either is set (and `gh` is on PATH, and the
#                        repository has a GitHub slug -- $GITHUB_REPOSITORY or
#                        an origin remote on github.com), the report also
#                        names the most recent failed `release.yml` run,
#                        even after a later successful run (issue #6911). The
#                        lookup is strictly best-effort -- no token, no `gh`,
#                        no slug, or any API error just omits the line, prints
#                        nothing to stdout (JSON lives there), and leaves the
#                        exit codes alone.
#
# Exit codes:
#   0  Within the backstop, or no tags exist yet (nothing to compare against).
#   1  Over the backstop — a release is due.
#   2  The check itself could not run (bad usage, not a git repo, RELEASING.md
#      threshold unparseable). A failed Actions lookup is NEVER a 2.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"

REPO="$DEFAULT_REPO"
REF=""
THRESHOLD=""
FORMAT="text"
ANNOTATE=0

usage() {
    # Print this file's leading comment block (everything after the shebang up
    # to the first non-comment line), so --help can never drift from the header.
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' \
        "${BASH_SOURCE[0]}"
}

die() {
    echo "check-release-lag.sh: $*" >&2
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --repo)
            [ $# -ge 2 ] || die "--repo requires a directory"
            REPO="$2"
            shift 2
            ;;
        --ref)
            [ $# -ge 2 ] || die "--ref requires a ref name"
            REF="$2"
            shift 2
            ;;
        --threshold)
            [ $# -ge 2 ] || die "--threshold requires a number"
            THRESHOLD="$2"
            shift 2
            ;;
        --format)
            [ $# -ge 2 ] || die "--format requires text|json|markdown"
            FORMAT="$2"
            shift 2
            ;;
        --annotate)
            ANNOTATE=1
            shift
            ;;
        --help | -h)
            usage
            exit 0
            ;;
        *)
            die "unknown argument: $1 (try --help)"
            ;;
    esac
done

case "$FORMAT" in
    text | json | markdown) ;;
    *) die "unknown --format: $FORMAT (expected text, json, or markdown)" ;;
esac

[ -d "$REPO" ] || die "not a directory: $REPO"
REPO="$(cd "$REPO" && pwd)"
git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || die "not a git repository: $REPO"

# ---------- threshold (parsed from RELEASING.md unless overridden) ----------

THRESHOLD_SOURCE="--threshold"
if [ -z "$THRESHOLD" ]; then
    RELEASING_DOC="$REPO/RELEASING.md"
    [ -f "$RELEASING_DOC" ] ||
        die "no RELEASING.md in $REPO to read the backstop from; pass --threshold N"
    # Matches RELEASING.md's "Release cadence" wording:
    #   "`main` is more than **25 commits** ahead of the latest tag"
    THRESHOLD="$(grep -Eo 'more than \*\*[0-9]+ commits\*\*' "$RELEASING_DOC" |
        head -1 | grep -Eo '[0-9]+' || true)"
    [ -n "$THRESHOLD" ] ||
        die "could not parse the commit backstop from $RELEASING_DOC (expected a
    \"more than **N commits**\" phrase in its \"Release cadence\" section). If that
    policy wording changed, update this parser so the two stay in sync; or pass
    --threshold N to bypass it."
    THRESHOLD_SOURCE="RELEASING.md"
fi
case "$THRESHOLD" in
    '' | *[!0-9]*) die "threshold must be a non-negative integer, got: $THRESHOLD" ;;
esac

# ---------- ref to measure against ----------

ref_exists() {
    git -C "$REPO" rev-parse --verify --quiet "$1^{commit}" >/dev/null 2>&1
}

if [ -n "$REF" ]; then
    ref_exists "$REF" || die "ref not found in $REPO: $REF"
else
    # Prefer origin/main (what RELEASING.md's invocation uses); degrade
    # gracefully in a clone with no remote, mirroring the missing-ref handling
    # in .loom/scripts/check-main-freshness.sh.
    for candidate in origin/main main HEAD; do
        if ref_exists "$candidate"; then
            REF="$candidate"
            break
        fi
    done
    [ -n "$REF" ] || die "no origin/main, main, or HEAD commit in $REPO"
fi

# ---------- latest tag + commits ahead ----------

# Same ordering RELEASING.md documents: newest tag by creation date.
LATEST_TAG="$(git -C "$REPO" tag --sort=-creatordate | head -1)"

CHANGELOG_HINT="See changelog.d/ and CHANGELOG.md's \"## Unreleased\" section for changes pending release."

if [ -z "$LATEST_TAG" ]; then
    STATUS="no_tags"
    COMMITS_AHEAD="null"
    MESSAGE="No tags in $REPO — no release has been cut yet, so there is no lag to measure. $CHANGELOG_HINT"
else
    COMMITS_AHEAD="$(git -C "$REPO" rev-list --count "${LATEST_TAG}..${REF}" 2>/dev/null || echo 0)"
    case "$COMMITS_AHEAD" in
        '' | *[!0-9]*) COMMITS_AHEAD=0 ;;
    esac
    if [ "$COMMITS_AHEAD" -gt "$THRESHOLD" ]; then
        STATUS="release_due"
        MESSAGE="$REF is $COMMITS_AHEAD commits ahead of $LATEST_TAG, over the $THRESHOLD-commit backstop in RELEASING.md's \"Release cadence\" section — a release is due. $CHANGELOG_HINT"
    else
        STATUS="ok"
        MESSAGE="$REF is $COMMITS_AHEAD commits ahead of $LATEST_TAG, within the $THRESHOLD-commit backstop in RELEASING.md's \"Release cadence\" section."
    fi
fi

# ---------- last failed Release run (best-effort, issue #6911) ----------

# Prefer the measured checkout's origin. Actions can run this script against
# another checkout, in which case GITHUB_REPOSITORY names the caller.
github_slug() {
    local url slug
    url="$(git -C "$REPO" remote get-url origin 2>/dev/null)" || url=""
    case "$url" in
        https://github.com/* | http://github.com/* | ssh://git@github.com/*)
            slug="${url#*github.com/}" ;;
        git@github.com:*) slug="${url#git@github.com:}" ;;
        "") [ "$REPO" -ef "${GITHUB_WORKSPACE:-$DEFAULT_REPO}" ] || return 1
            slug="${GITHUB_REPOSITORY:-}" ;;
        *) return 1 ;;
    esac
    slug="${slug%.git}"
    case "$slug" in
        */*/* | /* | */) return 1 ;;
        */*) printf '%s\n' "$slug" ;;
        *) return 1 ;;
    esac
}

# Echo the html_url of the most recent failed release.yml run.
# Silent on every unavailability; prints nothing else, so callers can treat
# empty output as "no link".
last_failed_release_run() {
    command -v gh >/dev/null 2>&1 || return 0
    [ -n "${GH_TOKEN:-}${GITHUB_TOKEN:-}" ] || return 0
    local slug runs
    slug="$(github_slug)" || return 0
    runs="$(gh api "repos/$slug/actions/workflows/release.yml/runs?status=failure&per_page=100" \
        2>/dev/null)" || return 0
    printf '%s' "$runs" | python3 -c '
import json, sys

try:
    runs = json.load(sys.stdin).get("workflow_runs") or []
except ValueError:
    sys.exit(1)
failed = [run for run in runs if run.get("conclusion") == "failure"]
if failed:
    last = max(failed, key=lambda r: (r.get("created_at") or "", r.get("id") or 0))
    print(last.get("html_url") or "")
' 2>/dev/null
}

LAST_FAILED_RUN="$(last_failed_release_run || true)"
if [ -n "$LAST_FAILED_RUN" ]; then
    MESSAGE="$MESSAGE Most recent failed Release workflow run: $LAST_FAILED_RUN"
fi

# ---------- reporting ----------

json_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

emit_text() {
    echo "Release lag: $MESSAGE"
}

emit_json() {
    local tag_field="null" run_field="null"
    [ -n "$LATEST_TAG" ] && tag_field="\"$(json_escape "$LATEST_TAG")\""
    [ -n "$LAST_FAILED_RUN" ] && run_field="\"$(json_escape "$LAST_FAILED_RUN")\""
    cat <<EOF
{
  "status": "$STATUS",
  "latest_tag": $tag_field,
  "ref": "$(json_escape "$REF")",
  "commits_ahead": $COMMITS_AHEAD,
  "threshold": $THRESHOLD,
  "threshold_source": "$(json_escape "$THRESHOLD_SOURCE")",
  "last_failed_run": $run_field,
  "message": "$(json_escape "$MESSAGE")"
}
EOF
}

emit_markdown() {
    local icon tag_cell ahead_cell
    case "$STATUS" in
        release_due) icon="⚠️" ;;
        no_tags) icon="ℹ️" ;;
        *) icon="✅" ;;
    esac
    tag_cell="—"
    [ -n "$LATEST_TAG" ] && tag_cell="\`$LATEST_TAG\`"
    ahead_cell="—"
    [ "$COMMITS_AHEAD" != "null" ] && ahead_cell="$COMMITS_AHEAD"
    cat <<EOF
### $icon Release lag

| Latest tag | Ref | Commits ahead | Backstop |
| --- | --- | --- | --- |
| $tag_cell | \`$REF\` | $ahead_cell | $THRESHOLD |

$MESSAGE

Advisory only — this check never cuts a release. See
[\`RELEASING.md\`](RELEASING.md)'s "Release cadence" section for the trigger and
the release sequence.
EOF
}

case "$FORMAT" in
    text) emit_text ;;
    json) emit_json ;;
    markdown) emit_markdown ;;
esac

# The job summary is how a CI run surfaces this without anyone reading logs.
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    emit_markdown >>"$GITHUB_STEP_SUMMARY" 2>/dev/null ||
        echo "check-release-lag.sh: could not append to GITHUB_STEP_SUMMARY" >&2
fi

if [ "$ANNOTATE" -eq 1 ]; then
    if [ "$STATUS" = "release_due" ]; then
        echo "::warning file=RELEASING.md,title=Release due::$MESSAGE"
    else
        echo "::notice file=RELEASING.md,title=Release lag::$MESSAGE"
    fi
fi

[ "$STATUS" = "release_due" ] && exit 1
exit 0
