#!/usr/bin/env bash
# Mirror-resilient `apt-get update && apt-get install` for CI (issue #1219).
#
# Usage: scripts/ci-apt-install.sh <package> [<package> ...]
#
# Why this exists
# ---------------
# GitHub's Ubuntu 24.04 runners ship an Azure-local apt mirror
# (`azure.archive.ubuntu.com`) alongside the public `archive.ubuntu.com`.
# On 2026-08-19 that mirror degraded: connections blackholed at the TCP
# level rather than being refused, so apt hung for minutes with no output
# instead of failing over. Two distinct signatures were observed live
# (issue #1219):
#
#   1. `azure.archive.ubuntu.com` unreachable -- `Ign:` on every suite,
#      fallback to `archive.ubuntu.com`, then a stall part-way through the
#      index fetch.
#   2. `azure.archive.ubuntu.com` reachable -- every `InRelease` line `Hit:`
#      within ~12s, then a stall on the package/`.deb` fetch that follows.
#
# Signature 2 is why retrying `apt-get update` alone is not enough: `update`
# succeeded and the subsequent `install` is what stalled. So the retry here
# wraps the whole `update && install` pair.
#
# The per-step `timeout-minutes: 5` guard in .github/workflows/ci.yml
# (issue #1204 / PR #1210) stays as an outer backstop -- it converts a hang
# into a visible failure, but on its own it converts a hang into a *red
# check* rather than a green build. This script is the layer that actually
# gets past the stall:
#
#   - Rewrites `azure.archive.ubuntu.com` out of apt's sources (both the
#     classic `sources.list` and Ubuntu 24.04's deb822 `.sources` layout)
#     so the job only ever talks to the mirror that stayed healthy.
#   - Passes short `Acquire::*::Timeout` / bounded `Acquire::Retries`
#     options so a stalled connection errors in seconds instead of hanging
#     near the whole step budget.
#   - Retries the `update && install` pair, with a hard per-command
#     `timeout(1)` cap and an overall deadline sized to fit *inside* the
#     workflow step's own 5-minute budget, so a retry can still land.
#
# A genuine packaging failure (typo'd/nonexistent package) is NOT retried
# and NOT masked: it is detected and fails immediately, non-zero.
#
# Knobs (all optional; defaults are tuned for `timeout-minutes: 5`):
#   CI_APT_DEADLINE          total wall-clock budget, seconds (default 250)
#   CI_APT_PER_CMD_TIMEOUT   per apt-get invocation cap, seconds (default 90)
#   CI_APT_MAX_ATTEMPTS      attempts at the update+install pair (default 4)
#   CI_APT_BACKOFF           seconds slept between attempts (default 5)
#   CI_APT_SOURCE_FILES      space-separated apt source files to sanitize
#                            (default: the standard system locations)
#   CI_APT_NO_SUDO           set to any value to invoke apt-get directly
#                            (already root, or under test)

set -euo pipefail

DEADLINE="${CI_APT_DEADLINE:-250}"
PER_CMD_TIMEOUT="${CI_APT_PER_CMD_TIMEOUT:-90}"
MAX_ATTEMPTS="${CI_APT_MAX_ATTEMPTS:-4}"
BACKOFF="${CI_APT_BACKOFF:-5}"

# The runner-local mirror observed stalling, and the public mirror that kept
# serving in the same window (issue #1219's log evidence).
FLAKY_MIRROR="azure.archive.ubuntu.com"
GOOD_MIRROR="archive.ubuntu.com"

# Short acquire timeouts + a bounded retry count mean a blackholed mirror
# errors within seconds instead of hanging until the step budget expires.
APT_OPTS=(
    -o Acquire::Retries=2
    -o Acquire::Timeout=15
    -o Acquire::http::Timeout=15
    -o Acquire::https::Timeout=15
)

if [[ $# -eq 0 ]]; then
    echo "usage: $(basename "$0") <package> [<package> ...]" >&2
    exit 2
fi
PACKAGES=("$@")

log() {
    echo "ci-apt-install: $*"
}

as_root() {
    if [[ -n "${CI_APT_NO_SUDO:-}" || "$(id -u)" -eq 0 ]]; then
        "$@"
    else
        sudo "$@"
    fi
}

remaining() {
    echo $((DEADLINE - SECONDS))
}

# Rewrite the flaky runner-local mirror to the public one, in place, in
# whichever source files actually reference it. Ubuntu 24.04 uses the deb822
# format (/etc/apt/sources.list.d/*.sources) by default, but the runner image
# layout has varied, so both are handled. Rewriting the host (rather than
# deleting the entry) keeps every suite/component the stanza declares --
# apt's "configured multiple times" warning on a resulting duplicate is
# harmless and does not fail the update.
sanitize_sources() {
    local files=()
    if [[ -n "${CI_APT_SOURCE_FILES:-}" ]]; then
        read -r -a files <<<"${CI_APT_SOURCE_FILES}"
    else
        files=(
            /etc/apt/sources.list
            /etc/apt/sources.list.d/*.list
            /etc/apt/sources.list.d/*.sources
        )
    fi

    local file
    for file in "${files[@]}"; do
        # Unmatched globs stay literal; -f skips them.
        [[ -f "$file" ]] || continue
        grep -q "${FLAKY_MIRROR}" "$file" 2>/dev/null || continue
        log "rewriting ${FLAKY_MIRROR} -> ${GOOD_MIRROR} in $file"
        as_root sed -i "s#${FLAKY_MIRROR//./\\.}#${GOOD_MIRROR}#g" "$file"
    done
}

# Run one apt-get invocation under a hard cap that never exceeds the script's
# remaining budget, streaming output to the CI log and appending it to $LOG
# for the fatal-error scan below.
run_apt() {
    local rem cap
    rem="$(remaining)"
    if ((rem <= 5)); then
        log "budget exhausted before running: apt-get $*"
        return 1
    fi
    cap="$PER_CMD_TIMEOUT"
    ((cap > rem)) && cap="$rem"

    log "apt-get $* (cap ${cap}s, ${rem}s left in budget)"
    as_root timeout "$cap" apt-get "${APT_OPTS[@]}" "$@" 2>&1 | tee -a "$LOG"
}

# A packaging error is deterministic -- retrying it just burns the budget and
# ends in the same failure, so surface it immediately. This is what keeps the
# retry loop from masking a real missing-package failure.
fatal_apt_error() {
    grep -Eq \
        "Unable to locate package|has no installation candidate|Unable to correct problems|Version '[^']*' for '[^']*' was not found" \
        "$LOG"
}

LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT

sanitize_sources

attempt=0
while ((attempt < MAX_ATTEMPTS)); do
    attempt=$((attempt + 1))
    : >"$LOG"
    log "attempt ${attempt}/${MAX_ATTEMPTS}: update + install ${PACKAGES[*]}"

    if run_apt update && run_apt install -y "${PACKAGES[@]}"; then
        log "installed: ${PACKAGES[*]}"
        exit 0
    fi

    if fatal_apt_error; then
        log "apt reported a packaging error, not a transient mirror failure -- not retrying"
        exit 1
    fi

    # A timeout(1) kill can land mid-dpkg; clear that state so the next
    # attempt is not rejected outright. Best-effort: never masks the retry.
    as_root dpkg --configure -a >/dev/null 2>&1 || true

    if ((attempt >= MAX_ATTEMPTS)); then
        break
    fi
    if (($(remaining) <= BACKOFF + 5)); then
        log "budget exhausted after attempt ${attempt}"
        break
    fi
    log "transient failure; retrying in ${BACKOFF}s"
    sleep "$BACKOFF"
done

log "failed to install ${PACKAGES[*]} after ${attempt} attempt(s) in ${SECONDS}s" >&2
exit 1
