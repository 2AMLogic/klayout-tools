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
#   - Rewrites `azure.archive.ubuntu.com` out of apt's sources (the classic
#     `sources.list`, Ubuntu 24.04's deb822 `.sources` layout, and the
#     `mirror+file:` mirror-list file GitHub's real runner images actually
#     use -- see the "Update" note below) so the job only ever talks to the
#     mirror that stayed healthy.
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
# Update (issue #1224, post-#1226): the rewrite above was a silent no-op on
# real GitHub-hosted `ubuntu-24.04` runners -- confirmed by pulling raw job
# logs (runs 32291907647 / 32292729034) and finding the "rewriting ..." log
# line never printed, with every apt line still hitting
# `azure.archive.ubuntu.com`. Root cause: those runner images do not put the
# mirror hostname directly in `/etc/apt/sources.list.d/*.sources` -- the
# `URIs:` line there is `mirror+file:/etc/apt/apt-mirrors.txt` (apt's
# "mirror" method), and the *actual* candidate mirror URL(s), including
# `azure.archive.ubuntu.com`, live in that separate plain-text mirror-list
# file instead (visible in CI logs as `Get:1 file:/etc/apt/apt-mirrors.txt
# Mirrorlist [144 B]` on every `apt-get update`). `grep`-ing only the
# `sources.list(.d)` files for the mirror hostname string can never match in
# that layout. `sanitize_sources` below now also rewrites
# `/etc/apt/apt-mirrors.txt` (configurable via `CI_APT_MIRROR_LIST_FILES`),
# and logs an explicit warning -- not just silence -- when nothing it
# checked referenced the flaky mirror, so a future runner-image layout
# change is visible in the CI log instead of silently doing nothing again.
#
# Separately (same investigation): even a retry against the *same* degraded
# host is not always enough for a large package. `cmake` (11.2 MB, part of
# the Yosys build-deps step) was repeatedly killed mid-download by the fixed
# per-command timeout while smaller packages (e.g. `ngspice`, 4.4 MB)
# completed under the same budget -- see `CI_APT_DEADLINE` /
# `CI_APT_PER_CMD_TIMEOUT` overrides on that step in
# `.github/workflows/ci.yml`.
#
# Knobs (all optional; defaults are tuned for `timeout-minutes: 5`):
#   CI_APT_DEADLINE          total wall-clock budget, seconds (default 250)
#   CI_APT_PER_CMD_TIMEOUT   per apt-get invocation cap, seconds (default 90)
#   CI_APT_MAX_ATTEMPTS      attempts at the update+install pair (default 4)
#   CI_APT_BACKOFF           seconds slept between attempts (default 5)
#   CI_APT_SOURCE_FILES      space-separated apt source files to sanitize
#                            (default: the standard system locations)
#   CI_APT_MIRROR_LIST_FILES space-separated apt "mirror+file:" mirror-list
#                            files to sanitize (default:
#                            /etc/apt/apt-mirrors.txt -- see the Update note
#                            above)
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
#
# GitHub's real `ubuntu-24.04` runner images do not reference the mirror
# hostname in *.sources/*.list at all: their `URIs:` line is
# `mirror+file:/etc/apt/apt-mirrors.txt`, and that separate plain-text file
# is where `azure.archive.ubuntu.com` actually appears (issue #1224). It is
# just a list of candidate mirror URLs, so the same substring rewrite
# applies to it unmodified -- it is checked alongside the classic/deb822
# source files below, not instead of them, since a non-GitHub or future
# runner layout may still reference the mirror directly.
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

    local mirror_files=()
    if [[ -n "${CI_APT_MIRROR_LIST_FILES:-}" ]]; then
        read -r -a mirror_files <<<"${CI_APT_MIRROR_LIST_FILES}"
    else
        mirror_files=(/etc/apt/apt-mirrors.txt)
    fi

    local file matched=0
    for file in "${files[@]}" "${mirror_files[@]}"; do
        # Unmatched globs stay literal; -f skips them.
        [[ -f "$file" ]] || continue
        grep -q "${FLAKY_MIRROR}" "$file" 2>/dev/null || continue
        matched=1
        log "rewriting ${FLAKY_MIRROR} -> ${GOOD_MIRROR} in $file"
        as_root sed -i "s#${FLAKY_MIRROR//./\\.}#${GOOD_MIRROR}#g" "$file"
    done

    if ((matched == 0)); then
        log "WARNING: no candidate apt source/mirror-list file referenced" \
            "${FLAKY_MIRROR} verbatim -- the mirror rewrite was a no-op this" \
            "run (checked: ${files[*]} ${mirror_files[*]}); if" \
            "${FLAKY_MIRROR} still shows up in apt-get output below, the" \
            "runner's real file/format has drifted from these paths and" \
            "CI_APT_SOURCE_FILES/CI_APT_MIRROR_LIST_FILES needs updating"
    fi
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
