#!/usr/bin/env bash
# test-install-package-json-stub.sh - the installer's package.json stub must
# fail loud, not green (issue #15018, operator ruling q81 2026-11-23).
#
# defaults/package.json is copied verbatim into any install target that lacks
# its own (loom-daemon/src/init/scaffolding.rs fs::copy; the shell installer
# reaches the same copy via `loom-daemon init --defaults`). Its four suite
# entry points (`check:ci`, `check:all`, `test`, `lint`) are the documented
# pnpm/npm commands roles and docs name (#13898) — a stub that echoes "not
# configured" and exits 0 is a false green: `npm test` passes on a repo whose
# real checks never ran.
#
# This suite pins three things (the issue's acceptance criteria):
#   1. Fresh install is honest: EVERY script in the stub (not just the four
#      known ones — a fifth script added later is covered too) exits
#      non-zero and prints a wire-in message naming package.json.
#   2. No dangling script: the script set is exactly the four entry points,
#      each a pure `echo ... && exit <nonzero>` — no reference to any path
#      the install does not provide (`worktree:return` was retired by
#      #13118/#13160 and must stay absent).
#   3. A consumer's own package.json stays theirs: manifest.sh's skip branch
#      keeps package.json out of the Loom-owned manifest when the target has
#      one (so uninstall never deletes it), and lists it when it doesn't
#      (so a fresh stub IS owned). The copy-vs-preserve behavior itself is
#      additionally pinned Rust-side by scaffolding tests
#      test_package_json_copied_when_missing / test_package_json_preserved_when_exists.
#
# Source-tree-only by design (#6194): the subjects are defaults/package.json
# and scripts/install/manifest.sh, neither of which ships into an installed
# consumer repo as itself, so the suite SKIPs (exit 0) outside Loom's own
# checkout.
#
# Usage:
#   bash defaults/scripts/tests/test-install-package-json-stub.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
STUB="$REPO_ROOT/defaults/package.json"
MANIFEST_SH="$REPO_ROOT/scripts/install/manifest.sh"

source "$SCRIPT_DIR/lib/assert.sh"

assert_nonzero_exit() {
    local actual="$1" msg="$2"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$actual" -ne 0 ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg (exit=$actual)"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (expected non-zero, got $actual)"
    fi
}

if [[ ! -f "$STUB" ]]; then
    echo "SKIP: source-tree-only test, $STUB not found (not shipped into an installed repo)" >&2
    exit 0
fi
if [[ ! -f "$MANIFEST_SH" ]]; then
    echo "SKIP: source-tree-only test, $MANIFEST_SH not found (not shipped into an installed repo)" >&2
    exit 0
fi
if ! command -v node &> /dev/null; then
    # node is a hard prerequisite of the installer itself (install.sh reads
    # LOOM_VERSION through it), so a runner without node cannot install the
    # stub this suite guards. Skip loudly rather than fail spuriously.
    echo "SKIP: node not on PATH — cannot evaluate the stub's scripts" >&2
    exit 0
fi

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-install-pkg-stub.XXXXXX")"
# shellcheck disable=SC2317  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

echo "================================================================"
echo "Group 1: fresh-install stub is honest (no script exits 0)"
echo "================================================================"
echo ""

# Simulate the fresh-install copy exactly as the installer performs it: a
# verbatim copy of defaults/package.json into a target repo lacking one.
TARGET="$WORKDIR/repo"
mkdir -p "$TARGET"
cp "$STUB" "$TARGET/package.json"

# Walk EVERY script the stub declares (not a hardcoded list) so a script
# added later can never reintroduce a silent exit 0.
while IFS= read -r script_name; do
    [[ -n "$script_name" ]] || continue
    cmd="$(node -e 'console.log(require(process.argv[1]).scripts[process.argv[2]])' \
        "$TARGET/package.json" "$script_name")"
    out="$(bash -c "$cmd" 2>&1)"
    rc=$?
    assert_nonzero_exit "$rc" "script '$script_name' exits non-zero (not a false green)"
    assert_contains "$out" "not configured" \
        "script '$script_name' prints a not-configured message"
    assert_contains "$out" "edit package.json" \
        "script '$script_name' says where to wire the real command"
done < <(node -e 'console.log(Object.keys(require(process.argv[1]).scripts || {}).join("\n"))' \
        "$TARGET/package.json")

echo ""
echo "================================================================"
echo "Group 2: no dangling script references"
echo "================================================================"
echo ""

# The script set is exactly the four documented entry points — `worktree:return`
# (retired in #13118/#13160) must stay absent, and nothing else may appear.
actual_keys="$(node -e 'console.log(Object.keys(require(process.argv[1]).scripts || {}).sort().join(" "))' \
    "$STUB")"
assert_eq "check:all check:ci lint test" "$actual_keys" \
    "stub declares exactly the four documented entry points (worktree:return stays absent)"

# Each script must be a pure echo-then-fail stub: no command, variable, or
# path reference at all. That shape structurally guarantees "no script
# references a path the install does not provide" — a stub that cannot name
# a path cannot dangle one.
pure_stub_re='^echo .* && exit [1-9][0-9]*$'
while IFS= read -r script_name; do
    [[ -n "$script_name" ]] || continue
    cmd="$(node -e 'console.log(require(process.argv[1]).scripts[process.argv[2]])' \
        "$STUB" "$script_name")"
    if [[ "$cmd" =~ $pure_stub_re ]]; then
        pass "script '$script_name' is a pure echo+exit stub (no path references)"
    else
        fail "script '$script_name' must match 'echo <msg> && exit <nonzero>'; got: $cmd"
    fi
done < <(node -e 'console.log(Object.keys(require(process.argv[1]).scripts || {}).join("\n"))' \
        "$STUB")

echo ""
echo "================================================================"
echo "Group 3: consumer package.json is not Loom-owned (manifest skip branch)"
echo "================================================================"
echo ""

# manifest.sh only defines helpers when sourced; _emit_installed_files_manifest
# walks defaults/ read-only. TARGET_PATH selects the package.json carve-out.
# shellcheck disable=SC1090
source "$MANIFEST_SH"

CONSUMER="$WORKDIR/consumer"
mkdir -p "$CONSUMER"
printf '{\n  "name": "consumer-app"\n}\n' > "$CONSUMER/package.json"
manifest="$(LOOM_ROOT="$REPO_ROOT" TARGET_PATH="$CONSUMER" _emit_installed_files_manifest)"
assert_not_contains "$manifest" '"package.json"' \
    "target WITH its own package.json: not in the Loom-owned manifest (uninstall must not delete it)"

FRESH="$WORKDIR/fresh"
mkdir -p "$FRESH"
manifest_fresh="$(LOOM_ROOT="$REPO_ROOT" TARGET_PATH="$FRESH" _emit_installed_files_manifest)"
assert_contains "$manifest_fresh" '"package.json"' \
    "target WITHOUT a package.json: stub IS in the manifest (Loom owns what it installed)"

echo ""
if [[ "$TESTS_FAILED" -eq 0 ]]; then
    echo "All $TESTS_RUN tests passed"
    exit 0
else
    echo "$TESTS_FAILED of $TESTS_RUN tests failed"
    exit 1
fi
