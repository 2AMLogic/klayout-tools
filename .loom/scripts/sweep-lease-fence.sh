#!/usr/bin/env bash
# sweep-lease-fence.sh - Sweep-side lease fencing check, run immediately
# before push / PR-open (Issue #6309, Phase 3 of Epic #6165: "give the forge
# claim a liveness dimension").
#
# ## Why this exists
#
# Epic #6165's Phase 1 (#6179/#6180) writes and renews a
# `<!-- loom:lease host=<host> sweep=<sweep-id> -->` marker comment for the
# life of a sweep (`defaults/docs/lease-record.md`,
# `defaults/docs/lease-renewal.md`). Phase 2 (#6286/#6287/#6288) made the
# DAEMON's reclamation path consult that lease's forge-assigned `updated_at`
# before reclaiming a peer's claim (`loom-daemon/src/claim_reconciliation.rs`
# — `lease_is_fresh` / `fetch_freshest_lease_updated_at`).
#
# Phase 3 (this script) is the sweep's OWN, symmetric check: fencing, not
# reclamation. A sweep about to do the one irreversible, externally-visible
# action -- push a branch / open a PR -- first confirms, against the forge's
# own clock, that its lease is still the one in force for the issue it is
# about to act on. This does not prevent an acquisition race (#4028) -- it
# stops an overlap from *costing* anything by capping waste at one build
# rather than N.
#
# **The sweep checks its own lease -- never the daemon.** Same rationale as
# Phase 1's renewal: role agents run as transient scopes that routinely
# outlive the daemon that spawned them (#6129), so only the sweep itself, at
# the moment of action, knows whether it is still the intended owner.
#
# ## What this checks
#
# Reads the FRESHEST comment on the target issue whose body starts with the
# lease marker (mirroring `loom-daemon`'s
# `claim_reconciliation::forge::fetch_freshest_lease_updated_at` /
# `sweep_registry::guards::read_lease_comments` read path -- REST comments
# endpoint, `--paginate`, NDJSON output to survive Issue #4637's per-page
# `--jq` re-invocation), then confirms BOTH:
#
#   1. FRESH: `now - updated_at <= ttl_minutes` (default 15, the same TTL
#      Phase 2 uses -- `LEASE_TTL_MINUTES_ENV` in claim_reconciliation.rs --
#      overridable here via `--ttl-minutes` or `LOOM_LEASE_TTL_MINUTES`).
#   2. OWNED: the freshest lease's `host=` field equals this sweep's own
#      host identity (no other host's lease has superseded this one via a
#      reclaim or a race) -- and, when `--sweep-id` is supplied, that its
#      `sweep=` field names this dispatch's own sweep run too (Issue #2237,
#      see "Ownership is a (host, sweep) pair" below).
#
# On failure of EITHER condition, `check` exits non-zero (distinct codes for
# each failure reason, see below) and logs which condition failed plus the
# observed lease state, so a post-incident read can tell "expired" apart
# from "superseded by another host". It does NOT attempt to clean up or
# contest the peer's claim -- the label/claim is left alone; that is out of
# scope for this phase.
#
# ## Yielded leases are excluded from "freshest" (Issue #6485)
#
# A lease comment whose own `(host, sweep)` pair has a LATER
# `<!-- loom:lease-yield host=... sweep=... earliest_host=... -->` comment on
# the same issue (Issue #6287's claim-then-verify-order tie-break standdown)
# is never eligible to be picked as "the freshest lease" -- it is dropped
# from the candidate pool entirely before the freshest-by-`updated_at`
# selection runs. Without this, a stood-down dispatcher whose renewal loop
# keeps PATCHing its own (abandoned) lease record -- or, per #6485's own
# incident, ANOTHER host's renewal loop that mistakenly targets it under
# "newest wins" (see `sweep-lease-renew.sh`) -- can look artificially
# fresher than the tie-break WINNER's own, correctly-owned, but less
# frequently renewed lease, fencing the winner out of its own push/PR-open.
# This is matched by exact `(host, sweep)` pair, not by host alone, so a
# host that legitimately re-claims the SAME issue later (a brand new lease
# comment, a different `sweep=`) is never excluded by an unrelated, older
# yield from a past claim episode on that same host. A genuinely abandoned
# lease that was never yielded (no `loom:lease-yield` record at all) is
# NEVER excluded by this rule -- it still ages out purely via the ordinary
# TTL-based path below (EXPIRED aborts only when it is THIS sweep's own
# host's lease; an expired lease owned by a different host now PASSes as
# stale, no-longer-live evidence instead, Issue #6783), so this does not
# weaken TTL-based reclamation.
#
# On success (or when there is simply no evidence to fence against -- see
# "Fail-open cases" below), `check` exits 0 and the caller proceeds exactly
# as it would have before this script existed.
#
# ## Where this is wired in
#
# `defaults/.claude/commands/loom/sweep.md`'s Builder phase, immediately
# before `git push` + `./.loom/scripts/create-pr.sh` (see "Creating the PR"
# in `defaults/roles/builder-pr.md`). Meaningful for BOTH dispatch paths
# since #6320: a daemon-dispatched sweep's lease is written at dispatch
# (Step 1a's self-claim signal, #6179), and an in-session sweep (manual
# `/loom:sweep`, GH Actions cron, `--no-daemon`) publishes its own at
# pre-flight Step 1b (`sweep-lease-publish.sh`). Only a run that predates
# those writers -- or one whose lease write failed -- has nothing to fence
# against, and this check fails open for it (see "Fail-open cases").
#
# ## Fail-open cases (never block on unverifiable evidence)
#
# Per `defaults/docs/lease-record.md`'s own reader contract, the ABSENCE of a
# lease record is "no evidence", never "not fresh" -- and every other forge
# probe in this lease subsystem (`fetch_freshest_lease_updated_at`,
# `read_lease_comments`) fails open on a `gh`/network error rather than
# blocking. This script mirrors that posture exactly:
#
#   - No lease comment found at all on the issue -> PASS (exit 0).
#   - The freshest lease comment fails to parse (malformed marker) -> PASS.
#   - The `gh api` call itself fails (network, rate limit, auth) -> PASS.
#   - The freshest lease comment is EXPIRED (older than ttl-minutes) AND
#     belongs to a host OTHER THAN this sweep's own -> PASS (Issue #6783,
#     see below).
#   - `--sweep-id` was supplied and the freshest lease belongs to a
#     DIFFERENT sweep run -- even on this very same host -> PASS, whether it
#     is expired or fresh (Issue #2237, see below). This dispatch simply has
#     no lease record of its own on this issue, which is "no evidence", not
#     "not fresh".
#
# Only a lease that is either (a) expired AND owned by THIS sweep's own host
# (and, with --sweep-id, its own sweep run), or (b) fresh but held by a
# DIFFERENT host, causes an abort. This is deliberate: a false abort
# (blocking a legitimately-owned sweep on a transient `gh` hiccup, or on a
# dead peer's abandoned record) is a strictly worse failure mode here than an
# occasional missed fence -- the fence is a cost-bounding backstop for a rare
# race, not a correctness-critical lock.
#
# ### Expired lease, different host (Issue #6783)
#
# Prior to #6783, ANY expired freshest lease -- regardless of which host
# wrote it -- aborted with exit `3` (EXPIRED). But EXPIRED is an abort, not a
# pass: if the lease's own sweep died without ever renewing OR yielding it,
# the record never stops being the freshest lease comment on the issue (it is
# never superseded unless a successor sweep writes a lease of its own), so it
# permanently fenced out every future dispatch -- observed on issue #6694 /
# PR #6773, where a dead sweep's lease from a different, dead host blocked a
# legitimate successor with zero live competitors.
#
# An expired lease is, by definition, not evidence of a LIVE peer -- it is
# exactly the state `loom-daemon`'s own `claim_reconciliation.rs` treats as
# reclaimable, and closer to "no evidence" (per this doc's own reader
# contract, `defaults/docs/lease-record.md`) than to "a peer owns this". So:
#
#   - Expired AND owned by THIS sweep's own host -> still ABORT (exit `3`).
#     A sweep whose own renewal loop died should not trust its own claim --
#     it may have been legitimately reclaimed out from under it.
#   - Expired AND owned by a DIFFERENT host -> now PASS (exit `0`). Nobody
#     live holds the claim; the abandoned record is no longer treated as
#     fencing evidence.
#
# Exit `4` (SUPERSEDED -- a different, FRESH lease from a live peer) is
# unaffected by this change: that case still means a live peer genuinely
# holds the claim, and aborting there remains unambiguously correct.
#
# ### Ownership is a (host, sweep) pair, not a host (Issue #2237)
#
# #6783 above fixed the abandoned-lease fence-out for a lease written by a
# DIFFERENT host. The identical failure on the SAME host was simply not
# reachable then, because `check` had no way to know its own sweep id: its
# ownership tests compared only `host=`, so two independent dispatches on one
# box -- two sequential Builder runs on the same machine, the ordinary shape
# for a single-host fleet -- were indistinguishable to it. An abandoned lease
# left behind by a dead predecessor on that host therefore aborted every
# successor with exit `3` (EXPIRED), forever, on the false premise "my own
# renewal loop died". Observed live on issue #2226 (2026-09-21): dispatch
# `sweep-issue-2226-1789988240`, whose own renewal loop was alive and
# watching the correct pid the whole time, was fenced out by the expired
# lease of `sweep-issue-2226-1789985384` on the same host -- a dispatch that
# had never published a lease of its own at all, so the record it aborted on
# could not possibly have been "its own".
#
# `--sweep-id ID` closes that hole. When supplied, "this dispatch's own
# lease" means `host=` AND `sweep=` both match -- the same (host, sweep)
# identity `sweep-lease-publish.sh publish --sweep-id` writes, and the same
# pair the #6485 yield-exclusion rule already matches on. The consequences
# are deliberately asymmetric -- it only ever REMOVES aborts, never adds one:
#
#   - Expired, same host, DIFFERENT sweep -> PASS (exit `0`), where it used
#     to ABORT `3`. This is the #2237 fix: an abandoned predecessor's record
#     is not evidence about THIS dispatch's renewal loop.
#   - Fresh, same host, DIFFERENT sweep -> still PASS (exit `0`), exactly as
#     before, but reported as the fail-open "no lease of my own" case rather
#     than as a verified own-lease match, so the log never claims an
#     ownership check succeeded when it did not. Deliberately NOT promoted to
#     an abort: this script's stated posture (see "Fail-open cases" above) is
#     that a false abort is strictly worse than a missed fence, and turning a
#     case that passes today into a new exit `4` would fence out same-host
#     dispatches that currently succeed -- the very failure #2237 exists to
#     end, reintroduced from the other side.
#   - Expired or fresh, DIFFERENT host -> unchanged (#6783 PASS / exit `4`
#     SUPERSEDED respectively). A different host is not this dispatch
#     regardless of sweep id, so the sweep comparison adds nothing there.
#
# When `--sweep-id` is OMITTED the script cannot know its own run identity,
# so it falls back to the pre-#2237 host-only comparison, byte for byte. Any
# caller that has not been updated behaves exactly as it did before.
#
# ## Commands
#
#   sweep-lease-fence.sh check <issue> [--host HOST] [--sweep-id ID]
#                                      [--ttl-minutes N]
#     Perform the fencing check for <issue>. --host defaults to the PUBLISHED
#     form of this host's own identity (Issue #6322): the opaque id
#     (`opaque_host_id`, mirroring `sweep_registry::opaque_host_id` byte for
#     byte) of the raw identity `sweep_registry::host_identity()` resolves
#     (`LOOM_HOST_ID` env > `$HOSTNAME` > the `hostname` binary >
#     `unknown-host`) -- unless `LOOM_LEASE_PUBLISH_HOSTNAME` opts into raw
#     publishing, in which case the raw identity is used directly, matching
#     `write_lease_comment`'s own opt-in. An explicit --host is used verbatim
#     (no transform applied) -- it is the caller's job to pass whatever value
#     was actually published. --sweep-id is THIS dispatch's own sweep run id
#     (Issue #2237) -- the same value the caller passed to
#     `sweep-lease-publish.sh publish --sweep-id`, i.e. `/loom:sweep`'s own
#     `$RUN_ID` (`sweep-run-registry.sh new`) or `$LOOM_SWEEP_RUN_ID`. When
#     given, the ownership comparisons require the freshest lease's `sweep=`
#     to match it as well as `host=`; when omitted (or empty) the comparison
#     is host-only, exactly as it was before #2237. --ttl-minutes defaults to
#     `LOOM_LEASE_TTL_MINUTES` or 15 (Phase 2's default,
#     `DEFAULT_LEASE_TTL_MINUTES` in claim_reconciliation.rs).
#
#     Exit codes:
#       0  PASS -- proceed with push / PR-open. Covers: fresh lease owned by
#          this host (and, with --sweep-id, this sweep); no lease comment
#          found; a lease comment that failed to parse; a `gh` fetch failure;
#          an EXPIRED lease owned by a DIFFERENT host (fail-open, Issue
#          #6783); or -- with --sweep-id -- any lease belonging to a
#          DIFFERENT sweep run, including one on this very host (fail-open,
#          Issue #2237).
#       1  Usage error (bad issue number, unknown flag, non-numeric
#          --ttl-minutes).
#       3  ABORT: EXPIRED -- the freshest lease comment is older than
#          ttl-minutes AND belongs to THIS sweep's own host (Issue #6783: an
#          expired lease owned by a different host now PASSes instead, see
#          above) AND, when --sweep-id is given, to THIS sweep's own run
#          (Issue #2237: an expired lease from a different run on this same
#          host now PASSes too).
#       4  ABORT: SUPERSEDED -- the freshest lease comment is still FRESH but
#          its host= differs from this sweep's own host.
#
# Usage:
#   .loom/scripts/sweep-lease-fence.sh check 6309
#   .loom/scripts/sweep-lease-fence.sh check 6309 --host studio-host --ttl-minutes 15
#   .loom/scripts/sweep-lease-fence.sh check 6309 --sweep-id "$RUN_ID"

set -euo pipefail

LEASE_MARKER_PREFIX="<!-- loom:lease host="
YIELD_MARKER_PREFIX="<!-- loom:lease-yield host="
DEFAULT_TTL_MINUTES="${LOOM_LEASE_TTL_MINUTES:-15}"

# --- Opaque host id (Issue #6322) -------------------------------------------
# `write_lease_comment` (`loom-daemon/src/sweep_registry/guards.rs`) publishes
# an OPAQUE id for `host=`, not the raw hostname `resolve_host` below
# resolves, so a public forge comment never carries a machine name (which
# commonly embeds a person's name). `opaque_host_id` here is a byte-for-byte
# bash port of `sweep_registry::opaque_host_id` — same salt, same "host-" +
# first 8 lowercase hex chars of sha256(salt+host) shape — so this script's
# own default `--host` resolution matches whatever the daemon actually
# published, and `check`'s OWNED comparison keeps working unmodified.
LEASE_HOST_SALT="loom-lease-host-id-v1:"

# --- sha256 hex digest of stdin, tolerating either common tool -------------
sha256_hex_stdin() {
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 | awk '{print $1}'
    elif command -v sha256sum >/dev/null 2>&1; then
        sha256sum | awk '{print $1}'
    else
        return 1
    fi
}

# opaque_host_id <host> -- prints "host-" + the first 8 hex chars of
# sha256(LEASE_HOST_SALT + host), mirroring
# `sweep_registry::opaque_host_id` exactly. Prints nothing and returns
# non-zero if neither `shasum` nor `sha256sum` is available (see
# `lease_publish_raw_hostname`'s caller for the fail-open handling of that
# case -- an extremely rare environment, not expected in practice).
opaque_host_id() {
    local host="$1" hash
    hash="$(printf '%s%s' "$LEASE_HOST_SALT" "$host" | sha256_hex_stdin)" || return 1
    [[ -n "$hash" ]] || return 1
    printf 'host-%s' "${hash:0:8}"
}

# lease_publish_raw_hostname -- true (exit 0) when `LOOM_LEASE_PUBLISH_HOSTNAME`
# opts into publishing the raw hostname, mirroring
# `SweepRegistry::lease_publish_raw_hostname`'s exact truthy-token set and
# env-only precedence (no per-repo config key -- see that function's Rust
# doc comment for why: this script has no access to `loom-daemon`'s own
# config resolution, so env is the only source both sides can agree on).
lease_publish_raw_hostname() {
    case "$(printf '%s' "${LOOM_LEASE_PUBLISH_HOSTNAME:-}" | tr '[:upper:]' '[:lower:]' | xargs)" in
        1 | true | yes | on) return 0 ;;
        *) return 1 ;;
    esac
}

# resolve_published_host -- the host identity this check compares against by
# default (no explicit --host): the opaque id of resolve_host()'s raw value,
# or the raw value itself when lease_publish_raw_hostname opts in. Falls back
# to the raw value if the opaque transform is unavailable (no sha256 tool) --
# a degraded-but-non-blocking outcome consistent with this script's fail-open
# posture (see the fail-open discussion below `resolve_host`).
resolve_published_host() {
    local raw
    raw="$(resolve_host)"
    if lease_publish_raw_hostname; then
        printf '%s' "$raw"
        return 0
    fi
    opaque_host_id "$raw" || printf '%s' "$raw"
}

usage() {
    awk 'NR < 3 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
    exit 1
}

# --- Repo-relative `gh` targeting (mirrors sweep-lease-renew.sh) -----------
gh_repo_args() {
    if [[ -n "${LOOM_REPO:-}" ]]; then
        printf -- '-R\n%s\n' "$LOOM_REPO"
    fi
}

# --- Host identity, mirroring sweep_registry::host_identity()'s precedence -
resolve_host() {
    if [[ -n "${LOOM_HOST_ID:-}" ]]; then
        printf '%s' "$LOOM_HOST_ID"
        return 0
    fi
    if [[ -n "${HOSTNAME:-}" ]]; then
        printf '%s' "$HOSTNAME"
        return 0
    fi
    local h
    h="$(hostname 2>/dev/null || true)"
    if [[ -n "$h" ]]; then
        printf '%s' "$h"
        return 0
    fi
    printf 'unknown-host'
}

# --- ISO-8601 -> epoch (portable across GNU date and BSD/macOS date, mirrors
# urgent-flip-guard.sh's `_iso_to_epoch`) ------------------------------------
iso_to_epoch() {
    local ts="$1" out
    out="$(date -u -d "$ts" +%s 2>/dev/null)" && [[ "$out" =~ ^[0-9]+$ ]] && {
        echo "$out"
        return 0
    }
    out="$(date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$ts" +%s 2>/dev/null)" && [[ "$out" =~ ^[0-9]+$ ]] && {
        echo "$out"
        return 0
    }
    return 1
}

# --- Parse `host=`/`sweep=` out of a lease marker's literal first line
# (mirrors SweepRegistry::parse_lease_marker_line in guards.rs). Prints
# "host<TAB>sweep" on success, nothing on a malformed marker. ---------------
parse_lease_marker_line() {
    local first_line="$1" rest host sweep_id
    rest="${first_line#"$LEASE_MARKER_PREFIX"}"
    [[ "$rest" == "$first_line" ]] && return 1 # prefix did not match
    [[ "$rest" == *" -->" ]] || return 1
    rest="${rest% -->}"
    case "$rest" in
        *" sweep="*)
            host="${rest%% sweep=*}"
            sweep_id="${rest#* sweep=}"
            ;;
        *)
            return 1
            ;;
    esac
    [[ -n "$host" && -n "$sweep_id" ]] || return 1
    printf '%s\t%s' "$host" "$sweep_id"
}

# --- Parse `host=`/`sweep=` out of a lease-YIELD marker's literal first line
# (Issue #6485): "host=<H> sweep=<S> earliest_host=<EH> earliest_sweep=<ES>
# -->". The earliest_host/earliest_sweep fields identify who WON the
# tie-break, not who is yielding, so they are parsed off and discarded here.
# Prints "host<TAB>sweep" on success, nothing on a malformed marker.
parse_lease_yield_marker_line() {
    local first_line="$1" rest host sweep_id
    rest="${first_line#"$YIELD_MARKER_PREFIX"}"
    [[ "$rest" == "$first_line" ]] && return 1 # prefix did not match
    case "$rest" in
        *" sweep="*)
            host="${rest%% sweep=*}"
            sweep_id="${rest#* sweep=}"
            sweep_id="${sweep_id%% earliest_host=*}"
            sweep_id="${sweep_id% }"
            ;;
        *)
            return 1
            ;;
    esac
    [[ -n "$host" && -n "$sweep_id" ]] || return 1
    printf '%s\t%s' "$host" "$sweep_id"
}

cmd_check() {
    local issue="${1:-}"
    shift || true
    [[ "$issue" =~ ^[0-9]+$ ]] || {
        echo "ERROR: check requires a positive integer issue number (got: '${issue:-}')" >&2
        exit 1
    }

    # `sweep_id` (Issue #2237) is deliberately OPTIONAL and defaults to the
    # empty string -- NOT to `$LOOM_SWEEP_RUN_ID` or any generated id the way
    # `sweep-lease-publish.sh` defaults its own --sweep-id. A publisher may
    # invent an identity for the record it is about to write; a READER may
    # not, because guessing wrong here would compare this dispatch's real
    # lease against a fabricated id and manufacture the very "not mine"
    # verdict this flag exists to prevent. Empty means "caller did not tell
    # me my run id", which selects the pre-#2237 host-only comparison.
    local host="" sweep_id="" ttl_minutes="$DEFAULT_TTL_MINUTES"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --host)
                host="${2:-}"
                shift 2
                ;;
            --sweep-id)
                sweep_id="${2:-}"
                shift 2
                ;;
            --ttl-minutes)
                ttl_minutes="${2:-}"
                shift 2
                ;;
            *)
                echo "ERROR: check: unknown flag '$1'" >&2
                exit 1
                ;;
        esac
    done

    if [[ -z "$host" ]]; then
        # Issue #6322: default resolution must match what was actually
        # PUBLISHED (opaque by default), not the raw hostname -- an explicit
        # --host is a caller-supplied literal and is compared verbatim,
        # unmodified by this transform.
        host="$(resolve_published_host)"
    fi
    if ! [[ "$ttl_minutes" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
        echo "ERROR: check: --ttl-minutes must be a non-negative number (got: '$ttl_minutes')" >&2
        exit 1
    fi

    local -a repo_args=()
    while IFS= read -r line; do
        [[ -n "$line" ]] && repo_args+=("$line")
    done < <(gh_repo_args)

    # NDJSON, one lease-marker comment per line (id, updated_at, body) --
    # deliberately NOT a `[...]`-wrapped array: `gh api --paginate --jq`
    # re-invokes the `--jq` filter once per response page (#4637), so an
    # array-literal filter would emit `[...][...]` across a multi-page
    # result -- not valid JSON. NDJSON has no wrapper to corrupt.
    # NDJSON of BOTH marker shapes (lease AND lease-yield, Issue #6485) --
    # fetched together in ONE round trip so the yield-exclusion filter below
    # never needs a second `gh api` call.
    local comments_ndjson
    if ! comments_ndjson="$(gh api "${repo_args[@]+"${repo_args[@]}"}" "repos/{owner}/{repo}/issues/${issue}/comments" \
        --paginate --jq \
        ".[] | select(.body != null and ((.body | startswith(\"${LEASE_MARKER_PREFIX}\")) or (.body | startswith(\"${YIELD_MARKER_PREFIX}\")))) | {updated_at: .updated_at, body: .body}" \
        2>&1)"; then
        echo "PASS: could not fetch comments for issue #${issue} (${comments_ndjson}) -- unverifiable, failing open (proceeding with push/PR-open)" >&2
        exit 0
    fi

    if [[ -z "$(printf '%s' "$comments_ndjson" | tr -d '[:space:]')" ]]; then
        echo "PASS: no lease comment found on issue #${issue} (predates the lease feature, a manually-launched sweep, or a lease write that failed) -- no evidence to fence against; proceeding with push/PR-open" >&2
        exit 0
    fi

    # Split into lease-marker and lease-yield-marker candidates. Neither jq
    # call goes through `gh --paginate --jq` (that already happened above),
    # so the #4637 per-page re-invocation hazard does not apply here -- each
    # call sees the full, already-concatenated NDJSON as one input.
    local lease_ndjson yield_ndjson
    lease_ndjson="$(jq -c --arg p "$LEASE_MARKER_PREFIX" 'select(.body | startswith($p))' <<< "$comments_ndjson" 2> /dev/null || true)"
    yield_ndjson="$(jq -c --arg p "$YIELD_MARKER_PREFIX" 'select(.body | startswith($p))' <<< "$comments_ndjson" 2> /dev/null || true)"

    if [[ -z "$(printf '%s' "$lease_ndjson" | tr -d '[:space:]')" ]]; then
        echo "PASS: no lease comment found on issue #${issue} (predates the lease feature, a manually-launched sweep, or a lease write that failed) -- no evidence to fence against; proceeding with push/PR-open" >&2
        exit 0
    fi

    # Yield-exclusion (Issue #6485): drop any lease comment whose OWN
    # (host, sweep) -- parsed from its own first line -- has a matching
    # `loom:lease-yield` record on this issue. See the "Yielded leases are
    # excluded" section in this script's header comment for why.
    local yield_first_lines=""
    if [[ -n "$(printf '%s' "$yield_ndjson" | tr -d '[:space:]')" ]]; then
        yield_first_lines="$(jq -r '.body | split("\n")[0]' <<< "$yield_ndjson" 2> /dev/null || true)"
    fi

    local filtered_ndjson=""
    while IFS= read -r lease_line; do
        [[ -z "$lease_line" ]] && continue
        local lbody lfirst lparsed lhost lsweep lyielded="0"
        lbody="$(jq -r '.body // empty' <<< "$lease_line" 2> /dev/null || true)"
        [[ -z "$lbody" ]] && continue
        lfirst="${lbody%%$'\n'*}"
        lparsed="$(parse_lease_marker_line "$lfirst" || true)"
        if [[ -n "$lparsed" && -n "$yield_first_lines" ]]; then
            lhost="${lparsed%%$'\t'*}"
            lsweep="${lparsed#*$'\t'}"
            while IFS= read -r yield_first_line; do
                [[ -z "$yield_first_line" ]] && continue
                local yparsed
                yparsed="$(parse_lease_yield_marker_line "$yield_first_line" || true)"
                [[ -z "$yparsed" ]] && continue
                if [[ "${yparsed%%$'\t'*}" == "$lhost" && "${yparsed#*$'\t'}" == "$lsweep" ]]; then
                    lyielded="1"
                    break
                fi
            done <<< "$yield_first_lines"
        fi
        if [[ "$lyielded" == "0" ]]; then
            filtered_ndjson+="${lease_line}"$'\n'
        fi
    done <<< "$lease_ndjson"

    if [[ -z "$(printf '%s' "$filtered_ndjson" | tr -d '[:space:]')" ]]; then
        echo "PASS: every lease comment found on issue #${issue} belongs to a (host, sweep) that has since posted its own loom:lease-yield standdown record for this issue (#6485) -- no non-yielded evidence to fence against; proceeding with push/PR-open" >&2
        exit 0
    fi

    # Pick the freshest by `updated_at` among the non-yielded candidates.
    local freshest_json
    freshest_json="$(jq -s -c 'sort_by(.updated_at) | last' <<< "$filtered_ndjson" 2> /dev/null || true)"
    if [[ -z "$freshest_json" || "$freshest_json" == "null" ]]; then
        echo "PASS: lease comments on issue #${issue} failed to parse -- no evidence to fence against; proceeding with push/PR-open" >&2
        exit 0
    fi

    local updated_at body first_line
    updated_at="$(jq -r '.updated_at // empty' <<< "$freshest_json" 2>/dev/null || true)"
    body="$(jq -r '.body // empty' <<< "$freshest_json" 2>/dev/null || true)"
    if [[ -z "$updated_at" || -z "$body" ]]; then
        echo "PASS: freshest lease comment on issue #${issue} is missing updated_at/body -- no evidence to fence against; proceeding with push/PR-open" >&2
        exit 0
    fi
    first_line="${body%%$'\n'*}"

    local parsed lease_host lease_sweep
    if ! parsed="$(parse_lease_marker_line "$first_line")"; then
        echo "PASS: freshest lease comment on issue #${issue} has a malformed marker ('${first_line}') -- no evidence to fence against; proceeding with push/PR-open" >&2
        exit 0
    fi
    lease_host="${parsed%%$'\t'*}"
    lease_sweep="${parsed#*$'\t'}"

    # Issue #2237: "this dispatch's own lease" is a (host, sweep) PAIR when
    # the caller told us its sweep id, and a host alone when it did not. Both
    # the EXPIRED and the SUPERSEDED decisions below key off this one
    # predicate so they can never disagree about what "own" means.
    #
    # `lease_sweep_differs` is the narrower signal used to explain a PASS:
    # true only when a sweep id WAS supplied and the freshest lease names a
    # different run. With no sweep id supplied it is always false, which is
    # what collapses every branch below back to the exact pre-#2237 behavior.
    local lease_sweep_differs="0" lease_is_own="0"
    if [[ -n "$sweep_id" && "$lease_sweep" != "$sweep_id" ]]; then
        lease_sweep_differs="1"
    fi
    if [[ "$lease_host" == "$host" && "$lease_sweep_differs" == "0" ]]; then
        lease_is_own="1"
    fi

    local updated_epoch now_epoch
    if ! updated_epoch="$(iso_to_epoch "$updated_at")"; then
        echo "PASS: freshest lease comment on issue #${issue} has an unparseable updated_at ('${updated_at}') -- no evidence to fence against; proceeding with push/PR-open" >&2
        exit 0
    fi
    now_epoch="${LOOM_LEASE_FENCE_NOW:-$(date -u +%s)}"

    local age_seconds age_minutes ttl_seconds
    age_seconds=$((now_epoch - updated_epoch))
    ((age_seconds < 0)) && age_seconds=0
    age_minutes="$(awk -v s="$age_seconds" 'BEGIN { printf "%.2f", s / 60 }')"
    ttl_seconds="$(awk -v m="$ttl_minutes" 'BEGIN { printf "%d", m * 60 }')"

    if ((age_seconds > ttl_seconds)); then
        if [[ "$lease_is_own" == "1" ]]; then
            echo "ABORT: EXPIRED -- lease fence failed for issue #${issue}. Freshest lease comment (host=${lease_host} sweep=${lease_sweep}) was last renewed at ${updated_at}, age ${age_minutes} min > ttl ${ttl_minutes} min. This sweep (host=${host}${sweep_id:+ sweep=$sweep_id}) is aborting BEFORE push/PR-open (Epic #6165 Phase 3, #6309) rather than proceed on a stale claim it can no longer trust as its own. Not contesting or cleaning up the peer/lease -- the loom:building label and claim are left alone." >&2
            exit 3
        fi
        # Issue #2237: an expired lease on THIS host that belongs to a
        # DIFFERENT sweep run is an abandoned predecessor's record, not this
        # dispatch's own. It says nothing about whether THIS dispatch's
        # renewal loop is alive -- which is the entire premise of the EXPIRED
        # abort above -- so aborting on it is a false positive that
        # permanently fences out every successor dispatch on this issue (the
        # #2226 incident). Same fail-open reasoning as #6783 immediately
        # below, one identity component further in.
        if [[ "$lease_sweep_differs" == "1" && "$lease_host" == "$host" ]]; then
            echo "PASS: freshest lease comment on issue #${issue} (host=${lease_host} sweep=${lease_sweep}) is EXPIRED (last renewed at ${updated_at}, age ${age_minutes} min > ttl ${ttl_minutes} min) and belongs to a DIFFERENT sweep than this dispatch (sweep=${sweep_id}) even though it is on this same host (${host}) -- an abandoned predecessor's lease is not evidence about THIS dispatch's own renewal loop (#2237); proceeding with push/PR-open" >&2
            exit 0
        fi
        # Issue #6783: an expired lease owned by a DIFFERENT host is an
        # abandoned record, not a live peer -- it is exactly the state
        # `loom-daemon`'s own claim_reconciliation.rs treats as reclaimable,
        # and closer to "no evidence" than to "a peer owns this" (see this
        # script's header doc, "Fail-open cases"). Treating it as a fencing
        # ABORT would permanently fence out every future dispatch on this
        # issue that does not itself write a lease record, since a dead
        # sweep's never-renewed, never-yielded record can never stop being
        # the freshest lease comment on its own (the #6694/PR #6773
        # incident). PASS instead -- this is fail-open, not fail-safe: it
        # does not contest or clean up the abandoned record.
        echo "PASS: freshest lease comment on issue #${issue} (host=${lease_host} sweep=${lease_sweep}) is EXPIRED (last renewed at ${updated_at}, age ${age_minutes} min > ttl ${ttl_minutes} min) and belongs to a DIFFERENT host than this sweep (${host}) -- an abandoned peer lease is no longer live evidence of a peer's claim; proceeding with push/PR-open" >&2
        exit 0
    fi

    if [[ "$lease_host" != "$host" ]]; then
        echo "ABORT: SUPERSEDED -- lease fence failed for issue #${issue}. The freshest lease comment (updated_at=${updated_at}, age ${age_minutes} min <= ttl ${ttl_minutes} min) is held by host=${lease_host} sweep=${lease_sweep}, not this sweep's own host=${host}. Another host's dispatch or reclaim has superseded this sweep's claim. Aborting BEFORE push/PR-open (Epic #6165 Phase 3, #6309). Not contesting or cleaning up the peer's claim -- the loom:building label is left alone." >&2
        exit 4
    fi

    # Issue #2237: same host, FRESH, but a different sweep run. This stays a
    # PASS -- exactly what it was before #2237, when the sweep id was invisible
    # to this script -- but it is reported as the fail-open "no lease of my
    # own to fence against" case rather than as a verified ownership match, so
    # the log never claims an ownership check succeeded when it did not.
    # Deliberately NOT promoted to an exit 4: per this script's own fail-open
    # posture, a false abort is strictly worse than a missed fence, and a new
    # abort here would fence out same-host dispatches that succeed today --
    # reintroducing #2237's failure from the opposite side.
    if [[ "$lease_is_own" != "1" ]]; then
        echo "PASS: freshest lease comment on issue #${issue} (host=${lease_host} sweep=${lease_sweep}) is FRESH (last renewed at ${updated_at}, age ${age_minutes} min <= ttl ${ttl_minutes} min) and is on this sweep's own host (${host}), but belongs to a DIFFERENT sweep than this dispatch (sweep=${sweep_id}) -- this dispatch has no lease record of its own on this issue, which is 'no evidence', not 'not fresh' (#2237); proceeding with push/PR-open" >&2
        exit 0
    fi

    echo "PASS: lease fence OK for issue #${issue} -- freshest lease (host=${lease_host} sweep=${lease_sweep}) updated_at=${updated_at}, age ${age_minutes} min <= ttl ${ttl_minutes} min, host matches this sweep (${host})${sweep_id:+ and sweep id matches (${sweep_id})}. Proceeding with push/PR-open." >&2
    exit 0
}

main() {
    local cmd="${1:-}"
    shift || true
    case "$cmd" in
        check) cmd_check "$@" ;;
        -h | --help | "") usage ;;
        *)
            echo "ERROR: unknown command '$cmd'" >&2
            usage
            ;;
    esac
}

main "$@"
