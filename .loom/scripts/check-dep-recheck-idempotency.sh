#!/usr/bin/env bash
# check-dep-recheck-idempotency.sh — regression guard + decision helper for
# Curator's "Re-check Idempotency" rule (`.claude/commands/loom/curator.md`
# § "Re-check Idempotency: never re-post an unchanged conclusion (#4986)").
#
# Why this exists (#1523): that rule's decision table says a same-hash
# `curator:dep-recheck` comment inside the staleness window (default 24h,
# `LOOM_DEP_RECHECK_HEARTBEAT_HOURS`) must be skipped silently. In production
# it was violated on four independent issues (#528, #527, #56, #55) on
# 2026-09-06: each received a comment whose `CONCLUSION_HASH` was IDENTICAL to
# its immediately-preceding dep-recheck comment, only ~4.5h apart — well
# inside the window. The comment text also cited a stale multi-day-old
# baseline instead of the true immediately-preceding comment, consistent with
# the decision having been narrated from memory of a prior pass's prose
# rather than recomputed from a fresh, live read of the comment history
# (`.claude/commands/loom/curator.md`'s own hand-rolled jq/date snippet is
# easy to reproduce unfaithfully by hand on every single pass). This script
# is the single deterministic implementation of that snippet's logic —
# mirroring `check-verified-corrections-preserved.sh`'s role for the Verified
# Corrections rule — so Curator's workflow can call ONE command instead of
# re-deriving PRIOR_HASH/PRIOR_AT by hand every pass, and so CI/tests can
# assert the invariant never regresses.
#
# Two marker families are recognized (#1780): `<!-- curator:dep-recheck:<v>:<hash> -->`
# (posted when the dependency is a closing PR — `dep-recheck-fingerprint.sh
# dep-recheck`) and `<!-- curator:named-dependency:<hash> -->` (posted when the
# dependency is a body `## Dependencies` checklist reference, not a closing
# PR — `dep-recheck-fingerprint.sh named-dependency`; this family has no
# `FORMULA_VERSION`, so its marker never carries a version segment). The two
# families hash DIFFERENT, non-comparable inputs, so they are never allowed to
# match against each other — see "--assume-new-family" below.
#
# Two modes, one algorithm (sorted marker history across BOTH families, then
# walk CHRONOLOGICALLY ADJACENT pairs — never all pairs; two same-hash
# comments separated by a differently-hashed comment are NOT a violation,
# since the conclusion legitimately changed and reverted):
#
#   AUDIT mode (no --assume-new-hash): scans the full existing comment history
#   for any already-committed violation — an adjacent same-hash pair less
#   than the staleness window apart. This is the literal regression guard the
#   issue's acceptance criteria ask for.
#
#   DECISION mode (--assume-new-hash <hash> given): appends a synthetic
#   candidate entry (the hash a fresh re-check pass just computed, timestamped
#   --assume-new-at or "now") to the real history and classifies ONLY that
#   candidate against its true immediately-preceding marker — i.e. "may I
#   post this comment right now?" Existing violations earlier in the history
#   do not affect this classification (they are an AUDIT concern).
#
# Usage:
#   check-dep-recheck-idempotency.sh --issue <n> [--hours <n>]
#   check-dep-recheck-idempotency.sh --file <comments.json> [--hours <n>]
#   check-dep-recheck-idempotency.sh (--issue <n> | --file <f>) \
#       --assume-new-hash <hash> [--assume-new-version <v>] \
#       [--assume-new-at <iso8601>] [--hours <n>]
#
#   --issue <n>            Fetch comments live via REST with --paginate (never
#                           GraphQL's `gh issue view --json comments`, whose
#                           default page size is a suspected contributor —
#                           see "Suspected Cause" #2 in issue #1523).
#   --file <f>              Read comments from a JSON array instead of the
#                           network (for tests/fixtures). Each element must
#                           have a body field and either created_at or
#                           createdAt.
#   --hours <n>             Staleness window in hours. Defaults to
#                           $LOOM_DEP_RECHECK_HEARTBEAT_HOURS, else 24 — same
#                           env var and default as curator.md's rule.
#   --assume-new-hash <h>   Switch to DECISION mode for candidate hash <h>.
#   --assume-new-family <f>  Which marker family <h> was computed under:
#                           `dep-recheck` (default, preserves pre-#1780
#                           behavior for callers that don't pass this flag) or
#                           `named-dependency`. Callers computing a hash via
#                           `dep-recheck-fingerprint.sh named-dependency` MUST
#                           pass `--assume-new-family named-dependency` so this
#                           script never compares that hash against a
#                           different-family prior marker — the two families
#                           hash non-comparable inputs (see "#1780 marker
#                           family guard" below).
#   --assume-new-version <v>  The `FORMULA_VERSION` the candidate hash <h> was
#                           computed under (see dep-recheck-fingerprint.sh's
#                           `FORMULA_VERSION` output field). Defaults to `v0`
#                           when omitted — matching the "unversioned" sentinel
#                           used for markers with no version tag at all, so
#                           callers that do not yet pass this flag keep the
#                           pre-#1544 hash-only comparison behavior unchanged.
#                           A marker with no version segment (every
#                           `curator:dep-recheck` comment posted before #1544
#                           shipped, and every `curator:named-dependency`
#                           comment — that family has no `FORMULA_VERSION` at
#                           all) is parsed as `v0` for this same reason.
#                           A version MISMATCH between this and the prior
#                           marker's version always yields DECISION=NONE,
#                           regardless of whether the hashes happen to match —
#                           see "#1544 version guard" below. A FAMILY mismatch
#                           is treated identically — see "#1780 marker family
#                           guard" below.
#   --assume-new-at <t>     Timestamp (ISO-8601 UTC, e.g. 2026-09-06T12:48:22Z)
#                           for the candidate entry. Defaults to now (UTC).
#
# #1544 version guard: a CLI-changing resync of dep-recheck-fingerprint.sh can
# reshape what CONCLUSION_HASH means without any real dependency-state change
# (the exact fleet-wide false-CHANGED incident on #528/#527/#56/#55,
# 2026-09-06). `FORMULA_VERSION` lets this script tell that apart from a
# genuine state change: when the candidate's version differs from the most
# recent marker's version, this is "the first check under this formula
# version" — always DECISION=NONE (comment once, unconditionally), never
# CHANGED — so a version bump costs exactly one comment per affected issue,
# attributed correctly to "recipe changed" rather than misread as new
# dependency state. Only when the versions match does the ordinary
# hash-comparison logic (CHANGED/SKIP/STALE) run.
#
# #1780 marker family guard: `curator:dep-recheck:` and `curator:named-dependency:`
# markers hash fundamentally different inputs (a closing-PR fingerprint vs. a
# body `## Dependencies` checklist fingerprint) — a shared hash VALUE between
# the two families would be pure coincidence, never a meaningful match. PRIOR
# selection always picks the single most-recent marker on the issue REGARDLESS
# of family (an issue can carry either shape over its lifetime, e.g. a
# body-only dependency later gaining a closing PR — the more recent marker
# always wins, never a fixed family preference). But the two are compared only
# when the candidate's declared `--assume-new-family` (default `dep-recheck`)
# matches that prior marker's family — a family mismatch is folded into the
# SAME "first check under this formula" NONE branch the version guard above
# uses, since it is exactly the same shape of problem (two conclusions that
# are not comparable must never yield CHANGED/SKIP/STALE against each other).
#
# Output (stdout — one KEY=VALUE per line, machine-parseable):
#   AUDIT mode:   MODE=AUDIT, DECISION=OK|VIOLATION, VIOLATIONS=<n>, then one
#                 "VIOLATION: hash=<h> family=<f> at=<t1> and at=<t2> gap_hours=<g> window_hours=<w>"
#                 line per violation (sorted oldest-first).
#   DECISION mode: MODE=DECISION, DECISION=NONE|CHANGED|SKIP|STALE,
#                 PRIOR_HASH=<hash or empty>, PRIOR_AT=<timestamp or empty>,
#                 PRIOR_VERSION=<version or empty>, PRIOR_FAMILY=<family or empty>,
#                 AGE_HOURS=<n or empty>
#
# Exit codes:
#   AUDIT mode:    0 = OK (no violation, including empty/no-marker history)
#                  1 = VIOLATION found
#                  2 = usage or environment error
#   DECISION mode: 0 = NONE|CHANGED|STALE (safe to post the candidate comment)
#                 20 = SKIP (must NOT post — same hash as PRIOR, still fresh)
#                  2 = usage or environment error
#
# CALLERS MUST NOT SWALLOW THE EXIT CODE — DECISION mode's 0 vs 20 split is
# the actionable signal ("comment" vs "skip silently"), matching every other
# classify-then-caller-acts script in this directory.

set -uo pipefail

ISSUE=""
FILE=""
HOURS="${LOOM_DEP_RECHECK_HEARTBEAT_HOURS:-24}"
NEW_HASH=""
NEW_VERSION=""
NEW_FAMILY=""
NEW_AT=""

usage() {
  echo "Usage: $0 (--issue <n> | --file <comments.json>) [--hours <n>] [--assume-new-hash <hash> [--assume-new-family dep-recheck|named-dependency] [--assume-new-version <v>] [--assume-new-at <iso8601>]]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --issue) ISSUE="${2:-}"; shift 2 ;;
    --file) FILE="${2:-}"; shift 2 ;;
    --hours) HOURS="${2:-}"; shift 2 ;;
    --assume-new-hash) NEW_HASH="${2:-}"; shift 2 ;;
    --assume-new-family) NEW_FAMILY="${2:-}"; shift 2 ;;
    --assume-new-version) NEW_VERSION="${2:-}"; shift 2 ;;
    --assume-new-at) NEW_AT="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$ISSUE" && -z "$FILE" ]]; then
  echo "ERROR: one of --issue <n> or --file <f> is required" >&2
  usage
  exit 2
fi
if [[ -n "$ISSUE" && -n "$FILE" ]]; then
  echo "ERROR: --issue and --file are mutually exclusive" >&2
  usage
  exit 2
fi
if [[ -n "$ISSUE" && ! "$ISSUE" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --issue must be numeric" >&2
  usage
  exit 2
fi
if [[ -n "$FILE" && ! -f "$FILE" ]]; then
  echo "ERROR: --file not found: $FILE" >&2
  exit 2
fi
if [[ ! "$HOURS" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --hours must be a non-negative integer, got: '$HOURS'" >&2
  usage
  exit 2
fi
if [[ -n "$NEW_FAMILY" && "$NEW_FAMILY" != "dep-recheck" && "$NEW_FAMILY" != "named-dependency" ]]; then
  echo "ERROR: --assume-new-family must be 'dep-recheck' or 'named-dependency', got: '$NEW_FAMILY'" >&2
  usage
  exit 2
fi

command -v jq >/dev/null 2>&1 || { echo "ERROR: 'jq' not found on PATH" >&2; exit 2; }

RAW_JSON=""
if [[ -n "$ISSUE" ]]; then
  command -v gh >/dev/null 2>&1 || { echo "ERROR: 'gh' not found on PATH" >&2; exit 2; }
  GH_STDERR="$(mktemp)"
  trap 'rm -f "$GH_STDERR" 2>/dev/null || true' EXIT
  # REST + --paginate, not `gh issue view --json comments` (GraphQL): the
  # GraphQL path's default page size is one of the suspected root causes
  # (#1523 "Suspected Cause" #2) — a long-lived issue's true most-recent
  # comment can sit past a silent page-size truncation. REST --paginate
  # walks every page unconditionally.
  RAW_JSON="$(gh api "repos/{owner}/{repo}/issues/$ISSUE/comments" --paginate 2>"$GH_STDERR")" || {
    echo "ERROR: 'gh api .../issues/$ISSUE/comments --paginate' failed: $(cat "$GH_STDERR" 2>/dev/null)" >&2
    exit 2
  }
else
  RAW_JSON="$(cat "$FILE")"
fi

# Normalize to a sorted array of {at, hash, version, family}, tolerating both
# REST (created_at) and GraphQL (createdAt) field spellings, and both a bare
# top-level array (REST / --file) and a `{"comments": [...]}` wrapper
# (`gh issue view --json comments`-shaped fixtures), so callers can hand this
# script either shape without pre-massaging it.
#
# Two marker families are recognized (#1780 — see header comment):
#   - `curator:dep-recheck:` — the `(?:v(?<v>[0-9]+):)?` group is OPTIONAL, so
#     both the versioned form (`<!-- curator:dep-recheck:v1:<hash> -->`,
#     #1544) and every legacy unversioned marker posted before #1544 shipped
#     (`<!-- curator:dep-recheck:<hash> -->`) match this same regex — a legacy
#     marker's `.v` capture is `null`, normalized to the `v0` sentinel below.
#   - `curator:named-dependency:` — never has a version segment (that family
#     has no `FORMULA_VERSION`); every match is normalized to `v0`.
# Each entry's `family` field keeps the two apart for the guard below — never
# assume a `version` of `v0` alone means "comparable"; a `dep-recheck` marker
# and a `named-dependency` marker can both be `v0` while hashing unrelated
# inputs.
ENTRIES_JSON="$(jq -c '
  ( if type == "object" and has("comments") then .comments else . end )
  | [ .[]
      | select(.body != null and (.body | test("<!--[ \t]*curator:(dep-recheck|named-dependency):")))
      | . as $c
      # capture() is a generator (zero outputs on no match, not `null`), so
      # each candidate regex is wrapped in `[...] | .[0]` to turn "no match"
      # into an explicit `null` the `if`/`elif` below can branch on.
      | ($c.body | [capture("<!--[ \t]*curator:dep-recheck:(?:v(?<v>[0-9]+):)?(?<h>[0-9a-fA-F]+)[ \t]*-->")] | .[0]) as $dr
      | ($c.body | [capture("<!--[ \t]*curator:named-dependency:(?<h>[0-9a-fA-F]+)[ \t]*-->")] | .[0]) as $nd
      | if $dr != null then
          {
            at: ($c.created_at // $c.createdAt),
            hash: $dr.h,
            version: (if $dr.v == null then "v0" else "v" + $dr.v end),
            family: "dep-recheck"
          }
        elif $nd != null then
          {
            at: ($c.created_at // $c.createdAt),
            hash: $nd.h,
            version: "v0",
            family: "named-dependency"
          }
        else empty
        end
    ]
  | sort_by(.at)
' <<<"$RAW_JSON" 2>&1)" || {
  echo "ERROR: failed to parse comments JSON: $ENTRIES_JSON" >&2
  exit 2
}

# Portable ISO-8601 -> epoch-seconds: GNU `date -d` first, BSD/macOS `date -j
# -f` fallback (mirrors check-evaluating-staleness.sh's iso_to_epoch).
iso_to_epoch() {
  date -u -d "$1" +%s 2>/dev/null || date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$1" +%s 2>/dev/null || echo ""
}

if [[ -n "$NEW_HASH" ]]; then
  # --- DECISION mode ---------------------------------------------------------
  if [[ -z "$NEW_AT" ]]; then
    NEW_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  fi
  # See "#1544 version guard" above: omitting --assume-new-version preserves
  # the pre-#1544 hash-only comparison for callers that have not yet been
  # updated to pass it (defaults both sides of the comparison to the same
  # `v0` sentinel legacy markers parse to, above).
  [[ -n "$NEW_VERSION" ]] || NEW_VERSION="v0"
  # See "#1780 marker family guard" above: omitting --assume-new-family
  # preserves the pre-#1780 behavior (every caller before this flag existed
  # was, and every caller that still doesn't pass it is, computing a
  # `dep-recheck` hash).
  [[ -n "$NEW_FAMILY" ]] || NEW_FAMILY="dep-recheck"

  # Most recent PRIOR entry, of ANY hash AND ANY family, strictly before the
  # candidate — an issue can carry either marker family over its lifetime, so
  # PRIOR selection never filters by family; only the comparison below does.
  PRIOR="$(jq -c --arg at "$NEW_AT" '
    [ .[] | select(.at < $at) ] | sort_by(.at) | last // empty
  ' <<<"$ENTRIES_JSON")"

  echo "MODE=DECISION"

  if [[ -z "$PRIOR" || "$PRIOR" == "null" ]]; then
    echo "DECISION=NONE"
    echo "PRIOR_HASH="
    echo "PRIOR_AT="
    echo "PRIOR_VERSION="
    echo "PRIOR_FAMILY="
    echo "AGE_HOURS="
    exit 0
  fi

  PRIOR_HASH="$(jq -r '.hash' <<<"$PRIOR")"
  PRIOR_AT="$(jq -r '.at' <<<"$PRIOR")"
  PRIOR_VERSION="$(jq -r '.version' <<<"$PRIOR")"
  PRIOR_FAMILY="$(jq -r '.family' <<<"$PRIOR")"

  # #1544 version guard + #1780 marker family guard: a version OR family
  # mismatch is ALWAYS "first-ever check under this formula" -- never
  # CHANGED, regardless of whether the hashes happen to match or differ. This
  # must run before the hash comparison below so a formula-version bump (or a
  # comparison across the two non-comparable marker families) never gets
  # misread as a genuine dependency-state change (or, worse, a coincidental
  # hash collision gets misread as SKIP/STALE). Unlike the "no prior marker at
  # all" NONE case above, PRIOR_* is still populated here (a prior marker DOES
  # exist, just under a different formula version or family) -- useful for
  # logging/debugging even though the caller's action for DECISION=NONE is
  # identical either way.
  if [[ "$PRIOR_FAMILY" != "$NEW_FAMILY" || "$PRIOR_VERSION" != "$NEW_VERSION" ]]; then
    echo "DECISION=NONE"
    echo "PRIOR_HASH=$PRIOR_HASH"
    echo "PRIOR_AT=$PRIOR_AT"
    echo "PRIOR_VERSION=$PRIOR_VERSION"
    echo "PRIOR_FAMILY=$PRIOR_FAMILY"
    echo "AGE_HOURS="
    exit 0
  fi

  PRIOR_EPOCH="$(iso_to_epoch "$PRIOR_AT")"
  NEW_EPOCH="$(iso_to_epoch "$NEW_AT")"
  if [[ -z "$PRIOR_EPOCH" || -z "$NEW_EPOCH" ]]; then
    echo "ERROR: could not parse timestamp(s): PRIOR_AT='$PRIOR_AT' NEW_AT='$NEW_AT'" >&2
    exit 2
  fi
  GAP_SECONDS=$(( NEW_EPOCH - PRIOR_EPOCH ))
  WINDOW_SECONDS=$(( HOURS * 3600 ))
  # Integer hours for display only — the SKIP/STALE decision itself compares
  # exact seconds, never truncated hours, to avoid an off-by-one near the
  # window boundary.
  AGE_HOURS=$(( GAP_SECONDS / 3600 ))

  if [[ "$PRIOR_HASH" != "$NEW_HASH" ]]; then
    echo "DECISION=CHANGED"
    echo "PRIOR_HASH=$PRIOR_HASH"
    echo "PRIOR_AT=$PRIOR_AT"
    echo "PRIOR_VERSION=$PRIOR_VERSION"
    echo "PRIOR_FAMILY=$PRIOR_FAMILY"
    echo "AGE_HOURS=$AGE_HOURS"
    exit 0
  fi

  if [[ "$GAP_SECONDS" -lt "$WINDOW_SECONDS" ]]; then
    echo "DECISION=SKIP"
    echo "PRIOR_HASH=$PRIOR_HASH"
    echo "PRIOR_AT=$PRIOR_AT"
    echo "PRIOR_VERSION=$PRIOR_VERSION"
    echo "PRIOR_FAMILY=$PRIOR_FAMILY"
    echo "AGE_HOURS=$AGE_HOURS"
    exit 20
  fi

  echo "DECISION=STALE"
  echo "PRIOR_HASH=$PRIOR_HASH"
  echo "PRIOR_AT=$PRIOR_AT"
  echo "PRIOR_VERSION=$PRIOR_VERSION"
  echo "PRIOR_FAMILY=$PRIOR_FAMILY"
  echo "AGE_HOURS=$AGE_HOURS"
  exit 0
fi

# --- AUDIT mode ---------------------------------------------------------------
echo "MODE=AUDIT"

COUNT="$(jq 'length' <<<"$ENTRIES_JSON")"
if [[ "$COUNT" -lt 2 ]]; then
  echo "DECISION=OK"
  echo "VIOLATIONS=0"
  exit 0
fi

# Adjacency is scanned across the FULL, combined-family timeline (sorted by
# time, per the header comment's "auto" PRIOR-selection policy) but a pair
# only counts as a violation when BOTH the hash AND the family match — a
# `dep-recheck` marker immediately followed by a `named-dependency` marker
# (or vice versa) is never comparable, even on the rare chance the two hash
# identically (#1780).
VIOLATIONS=0
PREV_AT=""
PREV_HASH=""
PREV_FAMILY=""
FIRST=1
while IFS=$'\t' read -r AT HASH FAMILY; do
  if [[ "$FIRST" -eq 0 ]]; then
    if [[ "$HASH" == "$PREV_HASH" && "$FAMILY" == "$PREV_FAMILY" ]]; then
      PREV_EPOCH="$(iso_to_epoch "$PREV_AT")"
      CURR_EPOCH="$(iso_to_epoch "$AT")"
      if [[ -n "$PREV_EPOCH" && -n "$CURR_EPOCH" ]]; then
        GAP_SECONDS=$(( CURR_EPOCH - PREV_EPOCH ))
        WINDOW_SECONDS=$(( HOURS * 3600 ))
        if [[ "$GAP_SECONDS" -lt "$WINDOW_SECONDS" ]]; then
          GAP_HOURS_DISPLAY=$(( GAP_SECONDS / 3600 ))
          VIOLATIONS=$((VIOLATIONS + 1))
          echo "VIOLATION: hash=$HASH family=$FAMILY at=$PREV_AT and at=$AT gap_hours=$GAP_HOURS_DISPLAY window_hours=$HOURS"
        fi
      fi
    fi
  fi
  PREV_AT="$AT"
  PREV_HASH="$HASH"
  PREV_FAMILY="$FAMILY"
  FIRST=0
done < <(jq -r '.[] | [.at, .hash, .family] | @tsv' <<<"$ENTRIES_JSON")

if [[ "$VIOLATIONS" -gt 0 ]]; then
  echo "DECISION=VIOLATION"
  echo "VIOLATIONS=$VIOLATIONS"
  exit 1
fi

echo "DECISION=OK"
echo "VIOLATIONS=0"
exit 0
