#!/usr/bin/env bash
# test-resync-installed-user-scope.sh - resync-installed.sh's user-scope steps
# vs. --output staging mode (#15518)
#
# A REAL run ends with two host-level (user-scope, $HOME-relative) side steps:
#   - #10238: (re)provision ~/.local/bin/loom and the ~/.claude/commands/loom
#     + ~/.claude/agents/loom-*.md links from the source checkout
#   - #12121: strip the retired presence-beat.sh wiring from
#     ~/.claude/settings.json
# Neither consulted --output, so a staging run -- the sanctioned "zero risk to
# the live checkout" path for an agent confined to a managed worktree -- still
# ran `install -m 755 <source>/scripts/loom ~/.local/bin/loom`: a host-wide
# path every session on the machine executes, overwritten from whatever
# checkout --source named. --dry-run skips the step, so the preview an agent
# reviews never showed it.
#
#   (1) a real `--output` run (flag and LOOM_RESYNC_OUTPUT form) makes NO
#       user-scope write: a seeded HOME is left exactly as it was
#   (2) control: the same fixture WITHOUT --output still provisions (#10238)
#       and still strips the retired wiring (#12121) -- (1) passes because the
#       steps are skipped, not because the fixture is inert
#   (3) LOOM_RESYNC_SKIP_USER_SCOPE=1 opts a non-staging run out of #10238,
#       and the opt-out is named in --help
#
# Usage:
#   ./.loom/scripts/tests/test-resync-installed-user-scope.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$HELPERS_DIR/resync-installed.sh"
# defaults/scripts/tests -> repo root. scripts/loom and scripts/install/*.sh
# are SOURCE-TREE-ONLY (never shipped into an installed consumer repo), so the
# fixture below copies them when present and this suite SKIPs when not.
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

source "$SCRIPT_DIR/lib/assert.sh"

DISPATCHER_BIN="$REPO_ROOT/scripts/loom"
DISPATCHER_LIB="$REPO_ROOT/scripts/install/provision-dispatcher.sh"
SKILLS_LIB="$REPO_ROOT/scripts/install/provision-skills.sh"
HOOKS_LIB="$REPO_ROOT/scripts/install/provision-hooks.sh"
if [[ ! -f "$DISPATCHER_BIN" || ! -r "$DISPATCHER_LIB" || ! -r "$SKILLS_LIB" || ! -r "$HOOKS_LIB" ]]; then
    echo "SKIP: source-tree-only test, scripts/install/provision-*.sh not found (not shipped into an installed repo)" >&2
    exit 0
fi

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-resync-user-scope.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"
# Each case below must see only the switches it sets itself: an ambient
# LOOM_RESYNC_OUTPUT would silently turn the non-staging control into a
# staging run. LOOM_DAEMON_BIN_DIR is unset too: provision_loom_dispatcher
# prefers it over $HOME/.local/bin, so on a host that exports its real bin dir
# the non-staging control would install the fixture dispatcher there instead of
# into the temporary HOME (and the HOME-relative assertions would miss it).
unset LOOM_RESYNC_OUTPUT LOOM_RESYNC_SKIP_USER_SCOPE LOOM_HOME LOOM_DAEMON_BIN_DIR
# provision-skills.sh refuses a reference tree under a temporary root; these
# fixtures live under mktemp, so no root counts as temporary here (the same
# override test-provision-skills.sh uses).
export LOOM_SKILLS_TEMP_ROOTS=""

# The Loom SOURCE checkout --source names (#14677): a plain git checkout whose
# dispatcher and skill tree DIFFER from the host's, as any --source may.
SRC="$WORKDIR/loom-src"
mkdir -p "$SRC/defaults/hooks" "$SRC/defaults/.claude/commands/loom" \
         "$SRC/defaults/.claude/agents" "$SRC/scripts/install"
git -C "$SRC" init -q -b main
printf 'NEW\n' > "$SRC/defaults/hooks/guard.sh"
chmod +x "$SRC/defaults/hooks/guard.sh"
printf 'SOURCE DISPATCHER\n' > "$SRC/scripts/loom"
cp "$DISPATCHER_LIB" "$SKILLS_LIB" "$SRC/scripts/install/"
printf '0.20.99\n' > "$SRC/VERSION"
printf '# builder skill\n' > "$SRC/defaults/.claude/commands/loom/builder.md"
printf '# loom-builder agent\n' > "$SRC/defaults/.claude/agents/loom-builder.md"
printf '{\n  "version": "9.9.9"\n}\n' > "$SRC/package.json"
git -C "$SRC" add -A >/dev/null 2>&1
git -C "$SRC" commit -qm "feat: source fixture" >/dev/null 2>&1

# The host's machine checkout (~/.local/share/loom): OLDER than the source, as
# a provisioned fleet checkout lags the tree --source names.
MACHINE="$WORKDIR/machine"
mkdir -p "$MACHINE/defaults/.claude/commands/loom" "$MACHINE/scripts/install"
printf '0.19.27\n' > "$MACHINE/VERSION"
printf '# builder skill (old)\n' > "$MACHINE/defaults/.claude/commands/loom/builder.md"
cp "$HOOKS_LIB" "$MACHINE/scripts/install/"

# A CONSUMER repo with a stale installed surface, and the linked worktree the
# documented --output command is run from. The install subject matches the
# routine-lineage pattern so the #7864 local-fix guard never gates the update.
CONS="$WORKDIR/consumer"
mkdir -p "$CONS/.loom/hooks"
git -C "$CONS" init -q -b main
printf 'OLD\n' > "$CONS/.loom/hooks/guard.sh"
printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "old",\n  "install_date": "2020-01-01",\n  "installed_files": []\n}\n' \
    > "$CONS/.loom/install-metadata.json"
git -C "$CONS" add -A >/dev/null 2>&1
git -C "$CONS" commit -qm "chore: install Loom v0.0.0" >/dev/null 2>&1
WT="$WORKDIR/wt"
git -C "$CONS" worktree add -q -b feature/x "$WT" >/dev/null 2>&1

# seed_home <dir> -- a HOME as a provisioned host carries it: a dispatcher that
# is NOT the source checkout's, a skills link into the (older) machine
# checkout, and settings.json still carrying the retired #12121 presence
# wiring. Every user-scope step would change something here.
seed_home() {
    local h="$1"
    mkdir -p "$h/.local/bin" "$h/.local/share" "$h/.claude/commands" "$h/.claude/agents"
    printf 'HOST DISPATCHER\n' > "$h/.local/bin/loom"
    touch -t 202001010000 "$h/.local/bin/loom"
    printf 'unrelated tool\n' > "$h/.local/bin/other-tool"
    ln -s "$MACHINE" "$h/.local/share/loom"
    ln -s "$MACHINE/defaults/.claude/commands/loom" "$h/.claude/commands/loom"
    cat > "$h/.claude/settings.json" <<EOF
{
  "env": { "LOOM_OPERATOR_PRESENCE": "1" },
  "hooks": {
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          { "type": "command", "command": "$MACHINE/defaults/hooks/presence-beat.sh" }
        ]
      }
    ]
  }
}
EOF
}

# home_fingerprint <dir> -- every entry under <dir> with its type, link
# target and content checksum, so any user-scope write (a new file, a rewritten
# one, a repointed link) changes the result.
home_fingerprint() {
    ( cd "$1" 2>/dev/null && find . -mindepth 1 | LC_ALL=C sort \
        | while IFS= read -r p; do
            if [[ -L "$p" ]]; then printf 'l %s -> %s\n' "$p" "$(readlink "$p")"
            elif [[ -d "$p" ]]; then printf 'd %s\n' "$p"
            else printf 'f %s %s\n' "$p" "$(cksum < "$p")"; fi
        done )
}

# file_mtime <path> -- epoch mtime: GNU `stat -c %Y` first (an illegal option
# on BSD/macOS, so it fails cleanly there), then BSD `stat -f %m`.
file_mtime() {
    local m
    m="$(stat -c %Y "$1" 2>/dev/null)"
    case "$m" in ''|*[!0-9]*) m="$(stat -f %m "$1" 2>/dev/null)" ;; esac
    printf '%s\n' "$m"
}

# out_tail <run-output> -- a run's last lines, for failure messages (the whole
# log is long and would repeat once per failed assertion).
out_tail() { tail -n 12 <<<"$1"; }

# drop_staging <dir> -- unregister a staging worktree the script intentionally
# left behind (KEEP_STAGING_WORKTREE on a successful run).
drop_staging() {
    git -C "$CONS" worktree remove --force "$1" >/dev/null 2>&1 || rm -rf "$1"
}

# assert_home_untouched <home> <fingerprint-before> <mtime-before> <label> <out>
# -- the three user-scope paths the issue names, then the whole tree.
assert_home_untouched() {
    local h="$1" before="$2" mtime_before="$3" label="$4" out="$5" now
    # A failed assertion prints the run's last lines, not its whole log.
    out="$(out_tail "$out")"
    if [[ "$(cat "$h/.local/bin/loom" 2>/dev/null)" == "HOST DISPATCHER" ]]; then
        pass "$label leaves the host's ~/.local/bin/loom content alone"
    else
        fail "$label overwrote ~/.local/bin/loom with the source checkout's dispatcher; out=$out"
    fi
    now="$(file_mtime "$h/.local/bin/loom")"
    if [[ -n "$mtime_before" && "$now" == "$mtime_before" ]]; then
        pass "$label leaves the ~/.local/bin/loom mtime alone"
    else
        fail "$label re-installed ~/.local/bin/loom (mtime '$mtime_before' -> '$now'); out=$out"
    fi
    if [[ "$(readlink "$h/.claude/commands/loom" 2>/dev/null)" == "$MACHINE/defaults/.claude/commands/loom" ]]; then
        pass "$label leaves the ~/.claude/commands/loom link pointing where it did"
    else
        fail "$label repointed ~/.claude/commands/loom (now '$(readlink "$h/.claude/commands/loom" 2>/dev/null)'); out=$out"
    fi
    if [[ "$(home_fingerprint "$h")" == "$before" ]]; then
        pass "$label leaves the whole HOME byte-identical (no new file, no rewrite, incl. ~/.claude/settings.json)"
    else
        fail "$label wrote something under HOME; out=$out"
    fi
    if ! grep -q '\[loom-dispatcher\]\|\[loom-skills\]' <<<"$out"; then
        pass "$label prints no [loom-dispatcher] / [loom-skills] provisioning lines"
    else
        fail "$label provisioned user-scope state; out=$out"
    fi
}

# --- precondition: the mtime the assertions rely on is readable here --------
PROBE="$WORKDIR/probe"
seed_home "$PROBE"
if [[ -n "$(file_mtime "$PROBE/.local/bin/loom")" ]]; then
    pass "fixture: the seeded dispatcher's mtime is readable on this platform"
else
    fail "fixture: the seeded dispatcher's mtime is not readable, so a re-install could go unnoticed"
fi
rm -rf "$PROBE"

echo ""
echo "Test group 1: a real --output run makes no user-scope write (#15518)"
HR="$WORKDIR/home1"
seed_home "$HR"
BEFORE="$(home_fingerprint "$HR")"
MTIME_BEFORE="$(file_mtime "$HR/.local/bin/loom")"
STAGING1="$WORKDIR/staging1"
OUT1="$(cd "$WT" && HOME="$HR" bash "$SCRIPT" --output "$STAGING1" --source "$SRC" 2>&1)"
RC1=$?
if [[ $RC1 -eq 0 && "$(cat "$STAGING1/.loom/hooks/guard.sh" 2>/dev/null)" == "NEW" ]]; then
    pass "(#15518) the staged resync itself completed (precondition for the assertions below)"
else
    fail "(#15518) the staged resync did not complete (rc=$RC1); last lines of the run:$(out_tail "$OUT1")"
fi
assert_home_untouched "$HR" "$BEFORE" "$MTIME_BEFORE" "(#15518) --output" "$OUT1"
drop_staging "$STAGING1"

echo ""
echo "Test group 2: the LOOM_RESYNC_OUTPUT env form makes no user-scope write either (#15518)"
HR2="$WORKDIR/home2"
seed_home "$HR2"
BEFORE2="$(home_fingerprint "$HR2")"
MTIME_BEFORE2="$(file_mtime "$HR2/.local/bin/loom")"
STAGING2="$WORKDIR/staging2"
OUT2="$(cd "$WT" && HOME="$HR2" LOOM_RESYNC_OUTPUT="$STAGING2" bash "$SCRIPT" --source "$SRC" 2>&1)"
RC2=$?
if [[ $RC2 -eq 0 && "$(cat "$STAGING2/.loom/hooks/guard.sh" 2>/dev/null)" == "NEW" ]]; then
    pass "(#15518) the env-form staging run completes and stages the resync"
else
    fail "(#15518) the env-form staging run did not complete (rc=$RC2); last lines of the run:$(out_tail "$OUT2")"
fi
assert_home_untouched "$HR2" "$BEFORE2" "$MTIME_BEFORE2" "(#15518) LOOM_RESYNC_OUTPUT" "$OUT2"
drop_staging "$STAGING2"

echo ""
echo "Test group 3: control -- the same run WITHOUT --output still does both user-scope steps (#10238, #12121)"
HR3="$WORKDIR/home3"
seed_home "$HR3"
BEFORE3="$(home_fingerprint "$HR3")"
OUT3="$(cd "$CONS" && HOME="$HR3" bash "$SCRIPT" --source "$SRC" 2>&1)"
RC3=$?
if [[ $RC3 -eq 0 ]]; then
    pass "(#10238) the non-staging run succeeds"
else
    fail "(#10238) the non-staging run failed (rc=$RC3); last lines of the run:$(out_tail "$OUT3")"
fi
if [[ "$(cat "$HR3/.local/bin/loom" 2>/dev/null)" == "SOURCE DISPATCHER" ]]; then
    pass "(#10238) a non-staging run re-provisions ~/.local/bin/loom from the source checkout"
else
    fail "(#10238) a non-staging run no longer provisions the dispatcher; last lines of the run:$(out_tail "$OUT3")"
fi
if [[ "$(readlink "$HR3/.claude/commands/loom" 2>/dev/null)" == "$SRC/defaults/.claude/commands/loom" ]]; then
    pass "(#10238) a non-staging run moves the skills link onto the newer source checkout"
else
    fail "(#10238) a non-staging run no longer re-links ~/.claude/commands/loom (now '$(readlink "$HR3/.claude/commands/loom" 2>/dev/null)'); last lines of the run:$(out_tail "$OUT3")"
fi
if [[ "$(readlink "$HR3/.claude/agents/loom-builder.md" 2>/dev/null)" == "$SRC/defaults/.claude/agents/loom-builder.md" ]]; then
    pass "(#10238) a non-staging run links the source checkout's agents"
else
    fail "(#10238) a non-staging run no longer links the agents; last lines of the run:$(out_tail "$OUT3")"
fi
if ! command -v jq >/dev/null 2>&1; then
    echo "  SKIP: jq unavailable, the #12121 strip cannot run here (not asserted)"
elif ! grep -q 'presence-beat.sh\|LOOM_OPERATOR_PRESENCE' "$HR3/.claude/settings.json" 2>/dev/null; then
    pass "(#12121) a non-staging run still strips the retired presence wiring from ~/.claude/settings.json"
else
    fail "(#12121) a non-staging run no longer strips the retired presence wiring; last lines of the run:$(out_tail "$OUT3")"
fi
if [[ "$(home_fingerprint "$HR3")" != "$BEFORE3" ]]; then
    pass "(#10238) the control HOME changed, so groups 1-2 proved a skip rather than an inert fixture"
else
    fail "(#10238) the control HOME is unchanged: the user-scope steps never acted on this fixture"
fi

echo ""
echo "Test group 4: LOOM_RESYNC_SKIP_USER_SCOPE=1 opts a non-staging run out (#10238)"
HR4="$WORKDIR/home4"
seed_home "$HR4"
OUT4="$(cd "$CONS" && HOME="$HR4" LOOM_RESYNC_SKIP_USER_SCOPE=1 bash "$SCRIPT" --source "$SRC" 2>&1)"
RC4=$?
if [[ $RC4 -eq 0 ]]; then
    pass "(#10238) the opt-out run succeeds"
else
    fail "(#10238) the opt-out run failed (rc=$RC4); last lines of the run:$(out_tail "$OUT4")"
fi
if [[ "$(cat "$HR4/.local/bin/loom" 2>/dev/null)" == "HOST DISPATCHER" \
      && "$(readlink "$HR4/.claude/commands/loom" 2>/dev/null)" == "$MACHINE/defaults/.claude/commands/loom" ]]; then
    pass "(#10238) LOOM_RESYNC_SKIP_USER_SCOPE=1 leaves the dispatcher and the skills link alone"
else
    fail "(#10238) LOOM_RESYNC_SKIP_USER_SCOPE=1 was ignored; last lines of the run:$(out_tail "$OUT4")"
fi
if ! grep -q '\[loom-dispatcher\]\|\[loom-skills\]' <<<"$OUT4"; then
    pass "(#10238) the opt-out run prints no provisioning lines"
else
    fail "(#10238) the opt-out run provisioned user-scope state; last lines of the run:$(out_tail "$OUT4")"
fi

echo ""
echo "Test group 5: --help names the opt-out"
HELP_OUT="$(bash "$SCRIPT" --help 2>&1)"
if grep -q 'LOOM_RESYNC_SKIP_USER_SCOPE' <<<"$HELP_OUT"; then
    pass "(#15518) --help (the header) documents LOOM_RESYNC_SKIP_USER_SCOPE"
else
    fail "(#15518) --help does not mention LOOM_RESYNC_SKIP_USER_SCOPE"
fi

# --- summary -----------------------------------------------------------------
echo ""
echo "========================================"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
echo "========================================"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "${RED}$TESTS_FAILED test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
exit 0
