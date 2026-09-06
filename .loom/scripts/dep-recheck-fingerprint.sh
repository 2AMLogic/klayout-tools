#!/usr/bin/env bash
# dep-recheck-fingerprint.sh — deterministic CONCLUSION_HASH computation for
# Curator's "Re-check Idempotency" rule (`.claude/commands/loom/curator.md`
# § "Re-check Idempotency: never re-post an unchanged conclusion (#4986)").
#
# Why this exists (#1528): #1523/#1524 replaced the *decision* half of that
# rule (find PRIOR, compare hashes, check the staleness window) with
# check-dep-recheck-idempotency.sh, because a hand-rolled jq/date snippet was
# being reproduced unfaithfully on every pass. The exact same failure then
# recurred one layer up, in the *input* to that decision: `CONCLUSION_HASH`
# was still built by hand, and its `BLOCK_REASON` component was specified as
# free-text prose ("fold the cited justification in"). Identical underlying
# dependency state therefore hashed differently from pass to pass purely
# because the wording differed — and a differing hash is classified CHANGED,
# which always permits a comment. That is the "guard can only be as good as
# the hash it is fed" defect.
#
# Live evidence (#528, `curator:dep-recheck` marker history, verified
# 2026-09-06): ten consecutive days 2026-08-27 -> 2026-09-05 produced exactly
# one heartbeat per day, every one carrying the SAME hash
# `bb3b15b9f3db0761`, despite ~20 Curator dispatches per day. Idempotency
# held. Then on 2026-09-06 the same unchanged blocker state produced THREE
# different hashes — `5342934786687480` (01:21Z), `bb3b15b9f3db0761`
# (08:23Z / 12:48Z / 16:24Z) and `28b66af6ee975a56` (16:33Z). Each flip reset
# PRIOR, so the following pass saw CHANGED and posted again. High dispatch
# frequency alone had been survivable for ten days; hash instability is what
# turned it into a comment storm.
#
# This script is the single deterministic implementation of the fingerprint.
# It builds every component from machine-checkable forge state — never prose —
# so two independent passes over an unchanged dependency state always produce
# byte-identical output, and any *real* change (a blocker closing, a
# `loom:` label appearing or clearing, a verdict flip) still changes it.
#
# Canonical component shapes (both are `<ref>:<STATE>:<sorted loom:-labels>`,
# the shape curator.md's linked-PR BLOCKERS fingerprint already used — it was
# the stable half all along, precisely because it is derived from
# `gh pr view --json number,state,labels` output rather than from prose):
#
#   BLOCKERS      one line per PR that would close this issue (primary check)
#   BLOCK_REASON  one line per blocker cited by the secondary heuristic, used
#                 ONLY when there is no linked PR
#
# Usage:
#   dep-recheck-fingerprint.sh --verdict <blocked|clear>
#       [--issue <n> | --blockers-file <f> | --no-linked-prs]
#       [--blocker <ref>]... [--reason-key <token>]... [--hash-only]
#
#   --verdict <v>        Required. `blocked` or `clear`. Prefixed to the hash
#                        input so a blocked->clear flip can never collide with
#                        clear->blocked.
#   --issue <n>          Issue being re-checked. Its linked PRs (the primary
#                        superseding-block check) become BLOCKERS, resolved
#                        live via `gh`.
#   --blockers-file <f>  Offline/test source for BLOCKERS: one pre-resolved
#                        `<ref>:<STATE>:<labels>` line per blocker. Mutually
#                        exclusive with --issue.
#   --no-linked-prs      Assert there are no linked PRs (BLOCKERS empty)
#                        without making a forge call. Mutually exclusive with
#                        --issue and --blockers-file.
#   --blocker <ref>      Repeatable. A blocker cited by the secondary
#                        heuristic: a bare same-repo number (`378`) or a
#                        cross-repo ref (`owner/repo#5325`). Resolved live via
#                        `gh` to `<ref>:<STATE>:<sorted loom:-labels>`.
#   --reason-key <tok>   Repeatable. Canonical token for a blocker that is not
#                        a forge issue/PR at all (e.g. an unreleased upstream
#                        tag: `release:rjwalters/loom:>v0.18.0`). Validated
#                        against ^[A-Za-z0-9._:/+@#=<>,-]+$ — whitespace is
#                        REJECTED, which is what structurally prevents prose
#                        from re-entering the fingerprint.
#   --hash-only          Print only the 16-hex CONCLUSION_HASH.
#
# Output (stdout — one KEY=VALUE per line, machine-parseable; multi-valued
# components are emitted as repeated single-line keys rather than embedded
# newlines, so a caller can parse with `sed -n 's/^KEY=//p'`):
#   VERDICT=<blocked|clear>
#   BLOCKER=<line>            (zero or more, sorted)
#   REASON=<line>             (zero or more, sorted)
#   CONCLUSION_HASH=<16 hex>
#   RECHECK_MARKER=<!-- curator:dep-recheck:<hash> -->
#
# Exit codes:
#   0 = fingerprint computed
#   2 = usage or environment error (including a --reason-key containing
#       whitespace, and the mutually-exclusive-source violations below)
#
# INVARIANT ENFORCED HERE, NOT LEFT TO THE CALLER: BLOCK_REASON exists only
# for the secondary heuristic. Passing --blocker/--reason-key while BLOCKERS
# is non-empty is a usage error (exit 2), mirroring curator.md's "Leave empty
# when the primary check supplied the blockers."

set -uo pipefail

VERDICT=""
ISSUE=""
BLOCKERS_FILE=""
NO_LINKED_PRS=0
HASH_ONLY=0
BLOCKER_REFS=()
REASON_KEYS=()

usage() {
  echo "Usage: $0 --verdict <blocked|clear> (--issue <n> | --blockers-file <f> | --no-linked-prs) [--blocker <ref>]... [--reason-key <token>]... [--hash-only]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --verdict) VERDICT="${2:-}"; shift 2 ;;
    --issue) ISSUE="${2:-}"; shift 2 ;;
    --blockers-file) BLOCKERS_FILE="${2:-}"; shift 2 ;;
    --no-linked-prs) NO_LINKED_PRS=1; shift ;;
    --blocker) BLOCKER_REFS+=("${2:-}"); shift 2 ;;
    --reason-key) REASON_KEYS+=("${2:-}"); shift 2 ;;
    --hash-only) HASH_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ "$VERDICT" != "blocked" && "$VERDICT" != "clear" ]]; then
  echo "ERROR: --verdict must be 'blocked' or 'clear', got: '$VERDICT'" >&2
  usage
  exit 2
fi

SOURCE_COUNT=0
[[ -n "$ISSUE" ]] && SOURCE_COUNT=$((SOURCE_COUNT + 1))
[[ -n "$BLOCKERS_FILE" ]] && SOURCE_COUNT=$((SOURCE_COUNT + 1))
[[ "$NO_LINKED_PRS" -eq 1 ]] && SOURCE_COUNT=$((SOURCE_COUNT + 1))
if [[ "$SOURCE_COUNT" -ne 1 ]]; then
  echo "ERROR: exactly one of --issue, --blockers-file, --no-linked-prs is required" >&2
  usage
  exit 2
fi
if [[ -n "$ISSUE" && ! "$ISSUE" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --issue must be numeric" >&2
  exit 2
fi
if [[ -n "$BLOCKERS_FILE" && ! -f "$BLOCKERS_FILE" ]]; then
  echo "ERROR: --blockers-file not found: $BLOCKERS_FILE" >&2
  exit 2
fi

# --- the anti-prose guard ------------------------------------------------------
# A --reason-key is a canonical token, not a sentence. Rejecting whitespace is
# what makes "fold in the cited justification" un-writable: there is no way to
# smuggle "still blocked pending the design epic" through this argument.
REASON_KEY_RE='^[A-Za-z0-9._:/+@#=<>,-]+$'
for KEY in ${REASON_KEYS+"${REASON_KEYS[@]}"}; do
  if [[ ! "$KEY" =~ $REASON_KEY_RE ]]; then
    echo "ERROR: --reason-key must be a canonical token matching $REASON_KEY_RE (no whitespace/prose), got: '$KEY'" >&2
    echo "HINT: describe the blocker as machine-checkable state, e.g. 'release:rjwalters/loom:>v0.18.0', not as a sentence." >&2
    exit 2
  fi
done

sha256_hex_stdin() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum | awk '{print $1}'
  else
    return 1
  fi
}

need_gh() {
  command -v gh >/dev/null 2>&1 || { echo "ERROR: 'gh' not found on PATH" >&2; exit 2; }
}

# ref_state_labels <ref> -- print "<ref>:<STATE>:<sorted loom:-labels>" for a
# forge issue or PR. <ref> is a bare number (this repo) or `owner/repo#N`.
# Tries the issue endpoint first, then the PR endpoint, since a cited blocker
# may legitimately be either.
#
# Returns 1 (never `exit`s) on an unresolvable ref: callers invoke this in a
# command substitution, where an `exit` would only kill the subshell and be
# silently absorbed into an empty — but still hashable — fingerprint. A
# fingerprint computed from a blocker the script could not actually read is
# exactly the class of silent wrong answer this whole script exists to
# eliminate, so the failure has to be visible to the caller.
ref_state_labels() {
  local ref="$1"
  local num="$ref"
  local out=""
  local repo_args=()
  if [[ "$ref" == *"#"* ]]; then
    repo_args=(--repo "${ref%%#*}")
    num="${ref##*#}"
  fi
  if [[ ! "$num" =~ ^[0-9]+$ ]]; then
    echo "ERROR: --blocker must be <number> or <owner>/<repo>#<number>, got: '$ref'" >&2
    return 1
  fi
  local jq_expr='"\(.state):\([.labels[].name | select(startswith("loom:"))] | sort | join(","))"'
  out="$(gh issue view "$num" ${repo_args+"${repo_args[@]}"} --json state,labels --jq "$jq_expr" 2>/dev/null)"
  if [[ -z "$out" ]]; then
    out="$(gh pr view "$num" ${repo_args+"${repo_args[@]}"} --json state,labels --jq "$jq_expr" 2>/dev/null)"
  fi
  if [[ -z "$out" ]]; then
    echo "ERROR: could not resolve blocker ref '$ref' as an issue or PR" >&2
    return 1
  fi
  printf '%s:%s\n' "$ref" "$out"
}

# --- BLOCKERS (primary check: PRs that would close this issue) -----------------
BLOCKERS=""
if [[ -n "$BLOCKERS_FILE" ]]; then
  BLOCKERS="$(grep -v '^[[:space:]]*$' "$BLOCKERS_FILE" | sort)"
elif [[ -n "$ISSUE" ]]; then
  need_gh
  PR_NUMBERS="$(gh issue view "$ISSUE" --json closedByPullRequestsReferences \
    --jq '.closedByPullRequestsReferences[].number' 2>/dev/null)"
  if [[ -n "$PR_NUMBERS" ]]; then
    BLOCKERS="$(while IFS= read -r PR; do
      [[ -n "$PR" ]] || continue
      gh pr view "$PR" --json number,state,labels --jq \
        '"\(.number):\(.state):\([.labels[].name | select(startswith("loom:"))] | sort | join(","))"'
    done <<<"$PR_NUMBERS" | sort)"
  fi
fi

# --- BLOCK_REASON (secondary heuristic only) -----------------------------------
REASON_LINES=()
for REF in ${BLOCKER_REFS+"${BLOCKER_REFS[@]}"}; do
  need_gh
  RESOLVED="$(ref_state_labels "$REF")" || exit 2
  [[ -n "$RESOLVED" ]] || { echo "ERROR: empty resolution for blocker ref '$REF'" >&2; exit 2; }
  REASON_LINES+=("$RESOLVED")
done
for KEY in ${REASON_KEYS+"${REASON_KEYS[@]}"}; do
  REASON_LINES+=("$KEY")
done

BLOCK_REASON=""
if [[ ${#REASON_LINES[@]} -gt 0 ]]; then
  if [[ -n "$BLOCKERS" ]]; then
    echo "ERROR: --blocker/--reason-key are for the SECONDARY heuristic only; the primary check already supplied BLOCKERS." >&2
    echo "HINT: curator.md — 'Leave empty when the primary check supplied the blockers.'" >&2
    exit 2
  fi
  BLOCK_REASON="$(printf '%s\n' "${REASON_LINES[@]}" | sort)"
fi

if [[ "$VERDICT" == "clear" && ( -n "$BLOCKERS" || -n "$BLOCK_REASON" ) ]]; then
  echo "ERROR: --verdict clear is inconsistent with a non-empty blocker set" >&2
  exit 2
fi

# --- the hash ------------------------------------------------------------------
# Byte-identical to curator.md's long-standing formula:
#   printf '%s\n%s\n%s' "$VERDICT" "$BLOCKERS" "$BLOCK_REASON" | sha256 | cut 16
# Verified against the value #1528 reported for #528's canonical state:
#   blocked / (empty) / 378:OPEN:loom:architect,loom:epic-phase,loom:operator-only
#   -> 7004d3c3258ab254
CONCLUSION_HASH="$(printf '%s\n%s\n%s' "$VERDICT" "$BLOCKERS" "$BLOCK_REASON" \
  | sha256_hex_stdin)" || {
  echo "ERROR: neither 'shasum' nor 'sha256sum' found on PATH" >&2
  exit 2
}
CONCLUSION_HASH="${CONCLUSION_HASH:0:16}"

if [[ "$HASH_ONLY" -eq 1 ]]; then
  printf '%s\n' "$CONCLUSION_HASH"
  exit 0
fi

echo "VERDICT=$VERDICT"
if [[ -n "$BLOCKERS" ]]; then
  while IFS= read -r LINE; do echo "BLOCKER=$LINE"; done <<<"$BLOCKERS"
fi
if [[ -n "$BLOCK_REASON" ]]; then
  while IFS= read -r LINE; do echo "REASON=$LINE"; done <<<"$BLOCK_REASON"
fi
echo "CONCLUSION_HASH=$CONCLUSION_HASH"
echo "RECHECK_MARKER=<!-- curator:dep-recheck:$CONCLUSION_HASH -->"
exit 0
