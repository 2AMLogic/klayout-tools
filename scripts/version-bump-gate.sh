#!/usr/bin/env bash
# version-bump-gate.sh — whether release.yml bumps the version now (issue #5005).
#
# Release cadence, on Loom's model (.github/workflows/version-bump-on-merge.yml,
# rjwalters/loom #11174): at most one `chore: bump version to X.Y.Z` per
# RELEASE_MIN_INTERVAL (repo variable `vars.RELEASE_MIN_INTERVAL`, seconds,
# default 86400 = 24 h). Merges inside the window accumulate, unreleased; the
# next eligible run (a push, the hourly schedule, or a forced dispatch) ships
# them all in one +1 patch. This script is the pure decision: it reads local
# git history only (no forge calls; the bump path holds only an OIDC token).
#
# "Last bump" = the newest FIRST-PARENT commit on --ref whose subject names a
# release bump, timed by its COMMITTER time (%ct). Three spellings exist in
# this repo's history: the fleet publisher's `chore: bump version to X.Y.Z`
# (what this workflow's commits look like), the hand-cut release PRs'
# `chore(release): bump version to X.Y.Z`, and the merge commits that landed
# them (`Merge pull request #N from <owner>/release/vX.Y.Z`). Tags and Release
# dates are never read: tags skip versions by design (the tag job releases a
# green commit, not "the newest version").
#
# Usage:
#   version-bump-gate.sh --event push|schedule|workflow_dispatch
#                        [--force true|false] [--reason <text>] [--actor <login>]
#                        [--now <epoch>] [--interval <seconds>] [--ref <rev>]
#
#   --interval defaults to $RELEASE_MIN_INTERVAL, then 86400 when that is unset
#   or empty. A non-integer or negative value is an error, never "always bump".
#   0 is accepted and means "no rate limit".
#   --force (workflow_dispatch only) skips the interval check, never the
#   "is there anything unreleased" check; it needs a --reason.
#
# "Unreleased" = a change since the last bump under any RELEASE_PATHS entry:
# src/ (the shipped package, decks included) and pyproject.toml (a dependency
# change is release-worthy). CHANGELOG.md is deliberately NOT in the set: the
# bump commit itself rewrites it, so counting it would make every interval-
# elapsed run think there is something new. The workflow's push `paths:`
# filter is deliberately NARROWER (src/** only, so the publisher's bump commit
# -- which rewrites pyproject.toml -- can never re-trigger the workflow);
# a pyproject-only change is picked up by the hourly schedule instead.
#
# Output (stdout, key=value): decision=bump|deferred|nothing|error,
#   last_bump_sha=, last_bump_at=, eligible_at=, forced=, reason=
#
# Exit: 0 decision made (bump, deferred or nothing); 2 bad input or history
# that cannot be read (fails closed: a gate wrong in the "defer" direction
# silently stops releases, so it must be red, not quiet). Shell, not a
# package module, because it runs in a hosted job before any venv exists.
set -euo pipefail

event="" force="false" reason="" actor="" now="" ref="origin/main"
interval="${RELEASE_MIN_INTERVAL:-}"
interval_given=0
last_sha="" last_ct="" eligible_ct=""

utc() { # epoch -> ISO-8601 UTC (GNU date, then BSD date)
  date -u -d "@$1" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -r "$1" '+%Y-%m-%dT%H:%M:%SZ'
}

emit() { # decision reason
  local at="" el=""
  [[ -n "$last_ct" ]] && at="$(utc "$last_ct")"
  [[ -n "$eligible_ct" ]] && el="$(utc "$eligible_ct")"
  printf 'decision=%s\nlast_bump_sha=%s\nlast_bump_at=%s\neligible_at=%s\nforced=%s\nreason=%s\n' \
    "$1" "$last_sha" "$at" "$el" "$force" "$2"
}

die() { emit error "$1"; exit 2; }

while [[ $# -gt 0 ]]; do
  [[ $# -ge 2 ]] || die "argument '$1' needs a value"
  case "$1" in
    --event) event="${2-}"; shift 2 ;;
    --force) force="${2-}"; shift 2 ;;
    --reason) reason="${2-}"; shift 2 ;;
    --actor) actor="${2-}"; shift 2 ;;
    --now) now="${2-}"; shift 2 ;;
    --interval) interval="${2-}"; interval_given=1; shift 2 ;;
    --ref) ref="${2-}"; shift 2 ;;
    *) die "unknown argument '$1'" ;;
  esac
done

case "$event" in
  push|schedule|workflow_dispatch) ;;
  *) die "--event '${event}' is not push, schedule or workflow_dispatch" ;;
esac
case "$force" in
  true|false) ;;
  *) die "--force '${force}' is not true or false" ;;
esac
if [[ "$force" == "true" ]]; then
  [[ "$event" == "workflow_dispatch" ]] || die "force is only accepted from workflow_dispatch, not ${event}"
  [[ "$reason" =~ [^[:space:]] ]] || die "force=true needs a non-empty reason (it is recorded in the bump commit)"
  [[ "$actor" != *"[bot]" ]] || die "force=true from a bot actor ('${actor}') is refused: forcing is an operator path"
fi

if [[ -z "$interval" && "$interval_given" -eq 0 ]]; then
  interval=86400
fi
[[ "$interval" =~ ^[0-9]{1,10}$ ]] || die "RELEASE_MIN_INTERVAL '${interval}' is not a non-negative integer number of seconds"
interval=$((10#$interval))

if [[ -z "$now" ]]; then
  now="$(date +%s)"
fi
[[ "$now" =~ ^[0-9]{1,12}$ ]] || die "--now '${now}' is not an epoch in seconds"

git rev-parse --verify --quiet "${ref}^{commit}" >/dev/null || die "ref '${ref}' does not resolve to a commit (is the history fetched?)"
# A shallow clone's boundary looks like the oldest commit there is, which would
# make the last-bump lookup silently wrong. Refuse instead.
if [[ "$(git rev-parse --is-shallow-repository)" != "false" ]]; then
  die "shallow clone: the last bump cannot be found reliably (check out with fetch-depth: 0)"
fi

# The last bump: newest first-parent commit whose subject names one. The three
# spellings are documented in the header; plain release-PR subjects,
# publisher subjects, and the merge commits that landed release PRs.
#
# Two portability/robustness rules for this pipeline (both bit on the real
# 4,000+-commit history, where the match sits far from the tip):
#   * the regexes use POSIX classes (`[^[:space:]]`), never `\S`: mawk, the
#     default awk on Debian/Ubuntu runners, does not understand `\S`;
#   * awk must read the WHOLE stream and print only the first match. An early
#     `exit` closes the pipe while `git log` is still writing, git dies of
#     SIGPIPE (141) and `pipefail` turns that into a spurious failure.
line="$(git log --first-parent --format='%H%x09%ct%x09%s' "$ref" \
  | awk -F'\t' '!found && ($3 ~ /^chore: bump version to [0-9]+\.[0-9]+\.[0-9]+$/ \
                 || $3 ~ /^chore\(release\): bump version to [0-9]+\.[0-9]+\.[0-9]+$/ \
                 || $3 ~ /^Merge pull request #[0-9]+ from [^[:space:]]+\/release\/v[0-9]+\.[0-9]+\.[0-9]+/) { print; found = 1 }')" \
  || die "git log over '${ref}' failed"
if [[ -z "$line" ]]; then
  die "no commit on ${ref}'s first-parent history names a version bump: cannot find the last bump (shallow clone?), refusing to guess"
fi
last_sha="$(cut -f1 <<<"$line")"
last_ct="$(cut -f2 <<<"$line")"
subject="$(cut -f3 <<<"$line")"
if [[ "$subject" != "chore: bump version to "* ]]; then
  echo "warning: last bump ${last_sha:0:12} is a legacy spelling: '${subject}'" >&2
fi
eligible_ct=$((last_ct + interval))

# Anything unreleased? src/ and pyproject.toml (see the header for why
# CHANGELOG.md is excluded and why this set is wider than the push trigger's
# paths filter; the workflow identity test pins both). diff-tree compares tree
# ids only, so a blob:none partial clone answers it without fetching blobs.
RELEASE_PATHS=(src/ pyproject.toml)
rc=0
git diff-tree --quiet -r "$last_sha" "$ref" -- "${RELEASE_PATHS[@]}" || rc=$?
case "$rc" in
  0)
    eligible_ct=""
    emit nothing "nothing to release: no change under ${RELEASE_PATHS[*]} on ${ref} since the last bump ${last_sha:0:12}"
    exit 0
    ;;
  1) ;;
  *) die "git diff-tree ${last_sha:0:12} ${ref} -- ${RELEASE_PATHS[*]} failed (rc=${rc})" ;;
esac

if [[ "$force" == "true" ]]; then
  emit bump "forced by ${actor:-<unknown>}: ${reason}"
  exit 0
fi

if ((now - last_ct >= interval)); then
  emit bump "${interval}s elapsed since the last bump ${last_sha:0:12} and a release path changed since it"
  exit 0
fi

emit deferred "deferred: last bump ${last_sha:0:12} at $(utc "$last_ct"); next bump eligible at $(utc "$eligible_ct")"
