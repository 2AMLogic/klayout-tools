#!/usr/bin/env bash
# Tests for the Builder lease on a GITEA checkout (#14941).
#
# `worktree.sh N` calls `lease ensure`, which runs sweep-lease-publish.sh and
# starts sweep-lease-renew.sh. Both used to reach the forge through `gh`, which
# cannot talk to Gitea: the publish exited 2 and the claim stayed leaseless.
# On a Gitea checkout they now read through `gitea_api`, publish through the
# shared `loom-daemon forge comment` writer (#14940) and renew by PATCHing their
# own comment.
#
# The subject runs unmodified against the REAL loom-daemon built from this
# checkout and a recording loopback Gitea (fixtures/gitea-lease-server.py), from
# a checkout whose `origin` carries an explicit port. `gh` on PATH is a stub that
# records and FAILS, so any lease-related `gh` call fails the suite. The issue's
# comments are inspected straight from the fixture's state, independent of the
# scripts.
#
#   (a) publish writes ONE comment, marker as the literal first line, authored
#       by the credential's user; stdout is `<host> <sweep-id>`
#   (b) repeating with the same identity reuses it (still one comment)
#   (c) a second host while the first lease is fresh: exit 4, nothing written
#   (d) a stale peer does not block; a yielded holder does not block; an
#       untrusted author's marker is prose (no block)
#   (e) a denied POST -> exit 2
#   (f) renewal PATCHes the SAME comment, keeps the marker as first line and
#       advances the forge-assigned updated_at; a failed PATCH -> exit 1
#   (g) the Gitea path never invokes `gh`

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
PUBLISH="$SCRIPTS_DIR/sweep-lease-publish.sh"
RENEW="$SCRIPTS_DIR/sweep-lease-renew.sh"
SERVER="$TEST_DIR/fixtures/gitea-lease-server.py"

# shellcheck source=lib/assert.sh
source "$TEST_DIR/lib/assert.sh"
# shellcheck source=lib/require-daemon-bin.sh
source "$TEST_DIR/lib/require-daemon-bin.sh"
# `forge comment` on Gitea (#14940) and `forge trusted-comments` are the verbs
# the lease chain calls; FATAL without a build of this checkout.
loom_test_require_daemon_bin "$SCRIPTS_DIR" "forge"

command -v python3 > /dev/null 2>&1 || {
    echo "FATAL: python3 is required for the Gitea fixture server" >&2
    exit 2
}

WORK="$(mktemp -d)"
SERVER_PID=""
cleanup() {
    [[ -z "$SERVER_PID" ]] || kill "$SERVER_PID" 2> /dev/null || true
    rm -rf "$WORK" 2> /dev/null || true
}
trap cleanup EXIT

STATE="$WORK/gitea"
mkdir -p "$STATE" "$WORK/bin" "$WORK/checkout"

# --- recording, always-failing gh -------------------------------------------
cat > "$WORK/bin/gh" << 'STUB'
#!/bin/sh
echo "$@" >> "${GH_LOG:?}"
echo "gh must not run on a Gitea checkout" >&2
exit 1
STUB
chmod +x "$WORK/bin/gh"
export GH_LOG="$WORK/gh.log"

# --- Gitea fixture server ---------------------------------------------------
python3 -I "$SERVER" "$STATE" &
SERVER_PID=$!
for _ in $(seq 1 100); do
    [[ -s "$STATE/port" ]] && break
    sleep 0.1
done
PORT="$(cat "$STATE/port" 2> /dev/null || true)"
[[ "$PORT" =~ ^[0-9]+$ ]] || {
    echo "FATAL: the Gitea fixture server did not start" >&2
    exit 2
}
BASE_URL="http://127.0.0.1:$PORT"
NWO="loomtest/loom-consumer"

# --- Gitea checkout ---------------------------------------------------------
(
    cd "$WORK/checkout" || exit 1
    git init -q
    git remote add origin "$BASE_URL/$NWO.git"
    mkdir -p .loom
    jq -n --arg url "$BASE_URL" \
        '{forge: {type: "gitea", gitea: {url: $url, token: "stub-token"}}}' > .loom/config.json
) || exit 2

# Every ambient influence the subject reads is pinned.
for v in GITEA_TOKEN GITEA_URL GITEA_USERNAME FORGE_TOKEN LOOM_FORGE_TYPE LOOM_REPO GH_REPO GH_TOKEN \
    GITHUB_TOKEN LOOM_LEASE_HOST_ID LOOM_LEASE_PUBLISH_NOW LOOM_SWEEP_RUN_ID LOOM_GH_BIN; do
    unset "$v"
done
export PATH="$WORK/bin:$PATH"
export LOOM_CONFIG_DEFAULTS_FILE=""
export LOOM_WORKSPACES_PATH="$WORK/workspaces.json"
export LOOM_HOST_ID="studio-host"
cd "$WORK/checkout" || exit 2

# `LOOM_HOST_ID=studio-host` publishes as its opaque id (Issue #6322).
OPAQUE_HOST="host-471642b3"

# --- helpers ----------------------------------------------------------------
iso_ago() { date -u -d "@$(($(date -u +%s) - $1))" +%Y-%m-%dT%H:%M:%SZ; }

reset_state() {
    # Keep every gh call of the whole run for assertion (g).
    [[ ! -f "$GH_LOG" ]] || cat "$GH_LOG" >> "$WORK/gh-all.log"
    rm -f "$STATE/comments.json" "$STATE/post-fail" "$STATE/patch-fail" "$STATE/requests.log" "$GH_LOG"
    echo "loomtest" > "$STATE/user"
    echo "open" > "$STATE/state"
}

comment_count() { jq 'length' "$STATE/comments.json" 2> /dev/null || echo 0; }

# seed <id> <login> <body> <updated_at>
seed() {
    local tmp
    tmp="$(mktemp)"
    jq --argjson id "$1" --arg login "$2" --arg body "$3" --arg ts "$4" \
        '. + [{id: $id, body: $body, user: {login: $login}, created_at: $ts, updated_at: $ts}]' \
        "$STATE/comments.json" > "$tmp" 2> /dev/null || jq -n --argjson id "$1" --arg login "$2" --arg body "$3" --arg ts "$4" \
        '[{id: $id, body: $body, user: {login: $login}, created_at: $ts, updated_at: $ts}]' > "$tmp"
    mv "$tmp" "$STATE/comments.json"
}

run_publish() {
    OUT="$("$PUBLISH" publish 14941 "$@" 2> "$WORK/stderr.log")"
    RC=$?
    ERR="$(cat "$WORK/stderr.log")"
}

# Lease-related gh invocations (anything naming issues/comments/user) since
# the last reset_state. The real daemon's own self-update commit probe also
# runs `gh api repos/.../commits/main` on a fresh start; that is not lease
# traffic, so it is not counted.
gh_lease_calls() {
    [[ ! -f "$GH_LOG" ]] || cat "$GH_LOG" >> "$WORK/gh-all.log"
    if [[ -f "$WORK/gh-all.log" ]]; then grep -cE 'issues|comments|user' "$WORK/gh-all.log" || true; else echo 0; fi
}

echo "=== Builder lease on a Gitea checkout (#14941) ==="

# (a) publish
reset_state
run_publish --sweep-id sweep-a
assert_eq "0" "$RC" "(a) publish exits 0 on a Gitea checkout (stderr: $ERR)"
assert_eq "$OPAQUE_HOST sweep-a" "$OUT" "(a) stdout is '<host> <sweep-id>'"
assert_eq "1" "$(comment_count)" "(a) exactly one lease comment was written"
assert_eq "<!-- loom:lease host=$OPAQUE_HOST sweep=sweep-a -->" \
    "$(jq -r '.[0].body | split("\n")[0]' "$STATE/comments.json")" "(a) the marker is the literal first line"
assert_eq "loomtest" "$(jq -r '.[0].user.login' "$STATE/comments.json")" "(a) authored by the credential's user"
assert_eq "1" "$(grep -c "^POST /api/v1/repos/$NWO/issues/14941/comments\$" "$STATE/requests.log")" \
    "(a) the one POST went to the Gitea issue-comments endpoint"
LEASE_ID="$(jq -r '.[0].id' "$STATE/comments.json")"

# (b) same identity again: reuse
run_publish --sweep-id sweep-a
assert_eq "0" "$RC" "(b) repeating the publish exits 0"
assert_eq "$OPAQUE_HOST sweep-a" "$OUT" "(b) it reports the same identity"
assert_eq "1" "$(comment_count)" "(b) the existing comment is reused (still one comment)"

# (c) a second host while the first lease is fresh
run_publish --host other-host --sweep-id sweep-b
assert_eq "4" "$RC" "(c) a second host is refused while the first lease is fresh (stderr: $ERR)"
assert_eq "1" "$(comment_count)" "(c) no competing lease was written"
assert_contains "$ERR" "FRESH lease" "(c) the existing peer refusal is reported"

# (d1) stale peer does not block
reset_state
seed 7 loomtest "<!-- loom:lease host=peer-host sweep=old -->
prose" "$(iso_ago 7200)"
run_publish --sweep-id sweep-d1
assert_eq "0" "$RC" "(d1) a stale peer lease does not block publication"
assert_eq "2" "$(comment_count)" "(d1) the new lease was written next to the stale one"

# (d2) yielded holder does not block
reset_state
seed 7 loomtest "<!-- loom:lease host=peer-host sweep=lost -->
prose" "$(iso_ago 30)"
seed 8 loomtest "<!-- loom:lease-yield host=peer-host sweep=lost earliest_host=x earliest_sweep=y -->
prose" "$(iso_ago 20)"
run_publish --sweep-id sweep-d2
assert_eq "0" "$RC" "(d2) a yielded holder does not block publication (stderr: $ERR)"
assert_eq "3" "$(comment_count)" "(d2) the new lease was written"

# (d3) an untrusted author's marker is prose
reset_state
seed 7 mallory "<!-- loom:lease host=peer-host sweep=fake -->
prose" "$(iso_ago 10)"
run_publish --sweep-id sweep-d3
assert_eq "0" "$RC" "(d3) an untrusted author's fresh marker does not block (stderr: $ERR)"
assert_eq "2" "$(comment_count)" "(d3) the new lease was written"

# (e) denied POST
reset_state
touch "$STATE/post-fail"
run_publish --sweep-id sweep-e
assert_eq "2" "$RC" "(e) a denied POST exits 2 (caller proceeds without a lease)"
assert_eq "0" "$(comment_count)" "(e) nothing was written"
assert_contains "$ERR" "failed to publish lease comment" "(e) the failure is reported"

# (f) renewal
reset_state
run_publish --sweep-id sweep-f
assert_eq "0" "$RC" "(f) publish for the renewal case"
LEASE_ID="$(jq -r '.[0].id' "$STATE/comments.json")"
# Age the record so a renewal's forge-assigned updated_at is observably later.
jq --arg ts "$(iso_ago 120)" '.[0].updated_at = $ts | .[0].created_at = $ts' "$STATE/comments.json" > "$STATE/c.tmp" \
    && mv "$STATE/c.tmp" "$STATE/comments.json"
BEFORE_TS="$(jq -r '.[0].updated_at' "$STATE/comments.json")"
"$RENEW" renew-once 14941 --host "$OPAQUE_HOST" --sweep-id sweep-f 2> "$WORK/renew.log"
RENEW_RC=$?
assert_eq "0" "$RENEW_RC" "(f) renew-once exits 0 (log: $(tr '\n' ' ' < "$WORK/renew.log"))"
assert_eq "1" "$(comment_count)" "(f) renewal edited in place (no new comment)"
assert_eq "$LEASE_ID" "$(jq -r '.[0].id' "$STATE/comments.json")" "(f) the same comment id"
assert_eq "<!-- loom:lease host=$OPAQUE_HOST sweep=sweep-f -->" \
    "$(jq -r '.[0].body | split("\n")[0]' "$STATE/comments.json")" "(f) the marker is still the literal first line"
AFTER_TS="$(jq -r '.[0].updated_at' "$STATE/comments.json")"
assert_eq "true" "$([[ "$AFTER_TS" > "$BEFORE_TS" ]] && echo true || echo false)" \
    "(f) the forge-assigned updated_at advanced ($BEFORE_TS -> $AFTER_TS)"
assert_eq "1" "$(grep -c "^PATCH /api/v1/repos/$NWO/issues/comments/$LEASE_ID\$" "$STATE/requests.log")" \
    "(f) one PATCH to the Gitea comment endpoint"

# (f2) failed PATCH
touch "$STATE/patch-fail"
"$RENEW" renew-once 14941 --host "$OPAQUE_HOST" --sweep-id sweep-f 2> "$WORK/renew.log"
assert_eq "1" "$?" "(f2) a failed PATCH reports failure (exit 1)"
assert_contains "$(cat "$WORK/renew.log")" "failed" "(f2) the failure is reported"

# (g) gh never ran
assert_eq "0" "$(gh_lease_calls)" "(g) no lease-related gh call was made on the Gitea path (publish and renew)"

echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if ((TESTS_FAILED > 0)); then
    echo -e "${RED}FAILED${NC}: $TESTS_FAILED test(s) failed"
    exit 1
fi
echo -e "${GREEN}ALL PASSED${NC}"
exit 0
