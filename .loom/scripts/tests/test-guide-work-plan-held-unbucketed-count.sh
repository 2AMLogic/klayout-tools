#!/usr/bin/env bash
# test-guide-work-plan-held-unbucketed-count.sh - Regression test for issue #15201
#
# guide.md's `render_plan_body()` (WORK_PLAN.md's machine-generated region)
# consulted the registry hold set in only two places: `ready_json` excludes
# every hold from `## Ready` (#7008/#7071), and `held` folds merge-risk-held
# PRs into "Operator Attention". No issue query listed held issues and no
# Backlog Balance row counted them. #13908 keeps a held issue visible only
# when it ALSO carries a priority, proposal or epic label, because its
# ` *(held)*` suffix can only annotate a bullet that already renders. An
# issue whose only stage label is a hold — exactly what parking produces
# (park record + `loom:blocked`, stage label removed) — therefore had no
# bullet in any section and no count in the table: it vanished from the
# roadmap.
#
# THE FIX: one Backlog Balance row, `| Held, no other bucket | N |`. N is the
# number of open issues that carry a registry hold other than
# `loom:frozen`, carry none of the six labels that render a bullet
# (`loom:very-important`, `loom:important`, `loom:curated`,
# `loom:architect`, `loom:hermit`, `loom:epic`), and are not already listed
# under In Progress (which renders `loom:building` minus the operator-gate
# labels, so `loom:blocked`+`loom:building` is listed there while
# `loom:operator`+`loom:building` is not). The listings go through
# `$GH_READ`, one cached query per registry hold label (gh ANDs `--label`,
# so no single query expresses "any hold"), unioned by issue number. No
# `## Blocked` section and no per-issue bullets: #13908's decision stands;
# the row is a count, a pointer that parked work exists.
#
# Verifies that:
#   1. STRUCTURE: the row exists in the Backlog Balance table; the listing
#      block derives from the registry sets (`$hold_q`, `$op_q`), names no
#      hold label besides `loom:frozen`, goes through `$GH_READ` with
#      `sort_by(.number)`, and adds no section.
#   2. The reproduction's five synthetic items render
#      `| Held, no other bucket | 1 |`: only #9101 counts, and it still has
#      no bullet anywhere — with an open PR linked to it included (#14785's
#      "Issues with Open PRs" section lists Ready candidates only).
#   3. A `loom:frozen`-only issue is not counted; `loom:blocked`+
#      `loom:building` (listed under In Progress) is not counted;
#      `loom:operator`+`loom:building` (not listed there) counts.
#   4. With no held issues the row renders 0, and the rest of the body is
#      byte-identical to the pre-fix render (the #15201 block and the row
#      stripped from the function) — update_work_plan() compares the region
#      byte-for-byte.
#   5. #15228: a listing read that puts non-JSON on stdout breaks the loop's
#      jq -s 'add // []' slurp (exit 5, no output), leaving $held_unbucketed
#      empty — the row's ${held_unbucketed:-0} guard renders 0, never an
#      empty cell (the same empty-input guard count() gives sibling rows).
#   6. Registry-driven: an unmigrated retired hold name is counted, an issue
#      carrying two hold labels is counted once, and the daemon-failure
#      fallback list still counts a `loom:blocked` issue.
#   7. NEGATIVE CONTROL: the pre-fix body renders no such row and the parked
#      issue nowhere — i.e. this test would have failed before the fix.
#
# Hermetic: `gh` and `loom-daemon` are stubbed with fixture JSON; only the
# real `jq` binary is invoked (skipped if unavailable) — no forge/network
# calls.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
# guide.md is shipped (installed at .claude/commands/loom/guide.md), so
# resolve it the way each layout actually lays it out: the installed path
# first, falling back to the defaults/ source-tree path (#6194 / #6241).
if [[ -f "$REPO_ROOT/.claude/commands/loom/guide.md" ]]; then
    GUIDE_MD="$REPO_ROOT/.claude/commands/loom/guide.md"
else
    GUIDE_MD="$REPO_ROOT/defaults/.claude/commands/loom/guide.md"
fi

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

if [[ ! -f "$GUIDE_MD" ]]; then
    echo -e "${RED}FATAL${NC}: guide.md not found at $GUIDE_MD"
    exit 1
fi

# Same extraction the #6993/#5930/#6457/#7008 suites use: the function's
# closing `}` is the first line starting at column 0 after the opening line
# (its nested `section()`/`count()` helpers are indented).
RPB_BODY="$(awk '/^render_plan_body\(\) \{/{flag=1} flag{print} /^\}/{if(flag){exit}}' "$GUIDE_MD")"
HELPERS_BODY="$(awk -f "$SCRIPT_DIR/lib/guide-plan-helpers.awk" "$GUIDE_MD")" || exit 1

if [[ -z "$RPB_BODY" ]]; then
    fail "could not extract render_plan_body() body from guide.md"
    echo -e "${RED}FATAL${NC}: cannot continue without the function body"
    exit 1
fi
pass "render_plan_body() body extracted from $GUIDE_MD"

ROW='| Held, no other bucket |'

# The pre-fix function: the #15201 block (its comment through the line
# before the first section call) and the table row removed. The region
# update_work_plan() compares byte-for-byte must differ from it by exactly
# that one row.
OLD_BODY="$(awk '/^  # #15201:/ {s = 1} s && /^  section "Operator Attention/ {s = 0} !s' <<<"$RPB_BODY" \
    | grep -vF -- "$ROW \${held_unbucketed:-0} |")"
# The #15201 block alone, for the structural assertions.
HELD_BLOCK="$(awk '/^  # #15201:/ {s = 1} s && /^  section "Operator Attention/ {s = 0} s' <<<"$RPB_BODY")"

# ---------------------------------------------------------------------------
# Test 1: STRUCTURE — the row, and the listing block that feeds it
# ---------------------------------------------------------------------------
echo ""
echo "Test 1: the row exists in Backlog Balance and derives from the registry sets"

if grep -qF -- "$ROW \${held_unbucketed:-0} |" <<<"$RPB_BODY"; then
    pass "render_plan_body() renders a 'Held, no other bucket' Backlog Balance row"
else
    fail "expected a '| Held, no other bucket | \${held_unbucketed:-0} |' row in render_plan_body()"
fi

if [[ -n "$HELD_BLOCK" ]]; then
    pass "the '# #15201:' listing block is present in render_plan_body()"
else
    fail "expected the '# #15201:' listing block in render_plan_body()"
fi

if [[ "$OLD_BODY" != "$RPB_BODY" ]] \
   && ! grep -qE 'held_unbucketed|#15201|Held, no other bucket' <<<"$OLD_BODY"; then
    pass "the pre-fix body is the function minus the #15201 block and the row"
else
    fail "could not derive the pre-fix body (the strip removed nothing, or left a trace)"
fi

if grep -qF '"$hold_q"' <<<"$HELD_BLOCK" && grep -qF '"$op_q"' <<<"$HELD_BLOCK"; then
    pass "the block lists the registry hold set (\$hold_q) and excludes In Progress by the operator-gate set (\$op_q)"
else
    fail "expected the block to derive from \$hold_q and \$op_q (registry sets), never a hardcoded label list"
fi

if grep -qE '"\$GH_READ" issue list --label "\$[a-z_]+" --state open --limit 200 --json number,labels' <<<"$HELD_BLOCK" \
   && grep -qF -- "--jq 'sort_by(.number)'" <<<"$HELD_BLOCK"; then
    pass "each hold-label listing goes through \$GH_READ and carries sort_by(.number) (#6993)"
else
    fail "expected a \$GH_READ issue list --label <hold> ... --json number,labels --jq 'sort_by(.number)' listing in the block"
fi

if grep -qE '"loom:(blocked|operator)[a-z-]*"' <<<"$HELD_BLOCK"; then
    fail "the block hardcodes a hold label (the set must come from \$hold_q; only loom:frozen is named)"
else
    pass "the block hardcodes no hold label besides loom:frozen"
fi

if grep -qE '^[[:space:]]*section "' <<<"$HELD_BLOCK"; then
    fail "the block adds a section — #13908 rejected a '## Blocked' section; the fix is one table row"
else
    pass "the block adds no section (one table row only; the #13908 decision stands)"
fi

if ! command -v jq >/dev/null 2>&1; then
    echo ""
    echo "  SKIP: jq not available — skipping the executable held-unbucketed tests"
    echo ""
    echo "================================"
    echo "Tests run:    $TESTS_RUN"
    echo -e "Tests passed: ${GREEN}${TESTS_PASSED}${NC}"
    [[ $TESTS_FAILED -gt 0 ]] && { echo -e "Tests failed: ${RED}${TESTS_FAILED}${NC}"; exit 1; }
    echo "All tests passed"
    exit 0
fi

# ---------------------------------------------------------------------------
# Harness: a `gh` stub that serves per-label JSON fixtures from a directory,
# applies `--search "-label:X"` negation the way real `gh` combines it with
# `--label`, and honours `--jq` exactly like real `gh ... --json ... --jq
# EXPR` does; plus a `loom-daemon` stub answering the two registry queries.
# ---------------------------------------------------------------------------
TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

GH_STUB="$TMPROOT/gh-stub"
cat > "$GH_STUB" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
kind="${1:-}"; shift || true
label=""; search=""; jqexpr=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --label)  label="${2:-}"; shift 2 ;;
    --search) search="${2:-}"; shift 2 ;;
    --jq)     jqexpr="${2:-}"; shift 2 ;;
    *)        shift ;;
  esac
done
fixture="$LOOM_FIXTURE_DIR/${kind}-${label//:/_}.json"
if [[ -f "$fixture" ]]; then json="$(cat "$fixture")"; else json='[]'; fi
for tok in $search; do
  case "$tok" in
    -label:*)
      excl="${tok#-label:}"
      json="$(printf '%s' "$json" | jq --arg l "$excl" '[.[] | select(((.labels // []) | map(.name) | index($l)) | not)]')"
      ;;
  esac
done
if [[ -n "$jqexpr" ]]; then printf '%s' "$json" | jq -r "$jqexpr"; else printf '%s' "$json"; fi
STUB
chmod +x "$GH_STUB"

# `loom-daemon labels list --property P --with-retired` stub: the registry's
# sets, retired names included (#11795) — the real daemon's answer when one
# is built, else those sets spelled out.
mkdir -p "$TMPROOT/bin" "$TMPROOT/bin-fail"
REAL_DAEMON="${LOOM_TEST_REAL_DAEMON:-${LOOM_DAEMON_BIN:-}}"
for prop in hold operator_gate; do
    if [[ -n "$REAL_DAEMON" && -x "$REAL_DAEMON" ]]; then
        "$REAL_DAEMON" labels list --property "$prop" --with-retired >"$TMPROOT/registry-$prop"
    elif [[ "$prop" == "hold" ]]; then
        printf '%s\n' loom:blocked loom:operator loom:frozen loom:operator-only loom:operator-decision \
            loom:operator-mechanical loom:operator-blocked loom:needs-capability >"$TMPROOT/registry-$prop"
    else
        printf '%s\n' loom:operator loom:operator-only loom:operator-decision loom:operator-mechanical \
            loom:operator-blocked >"$TMPROOT/registry-$prop"
    fi
done
printf '#!/usr/bin/env bash\n[ "$1 $2 $3 ${5:-}" = "labels list --property --with-retired" ] || exit 2\ncat "%s/registry-$4"\n' \
    "$TMPROOT" >"$TMPROOT/bin/loom-daemon"
chmod +x "$TMPROOT/bin/loom-daemon"
# A daemon that fails outright: render_plan_body() must fall back to its
# literal three-hold list.
printf '#!/usr/bin/env bash\nexit 2\n' >"$TMPROOT/bin-fail/loom-daemon"
chmod +x "$TMPROOT/bin-fail/loom-daemon"

# render_with <fixture-dir> <function-body> [<path-prepend>] (default: the registry stub dir, so a host with no loom-daemon, as on CI, still answers): eval the shared
# helpers and the body, then run the real render_plan_body() against the stubs.
# LOOM_TEST_GH_READ overrides the gh stub (poisoned-read variants, #15228).
render_with() {
    local fix="$1" body="$2" prepend="${3:-$TMPROOT/bin}"
    PATH="$prepend:$PATH" LOOM_FIXTURE_DIR="$fix" GH_READ="${LOOM_TEST_GH_READ:-$GH_STUB}" HELPERS_SRC="$HELPERS_BODY" RPB_SRC="$body" \
        bash -c 'eval "$HELPERS_SRC"; eval "$RPB_SRC"; render_plan_body'
}
# A copy of fixture dir $1 holding ONLY issue $2 (every other item removed).
mk_variant() {
    local src="$1" dst="$2" n="$3" f
    mkdir -p "$dst"
    for f in "$src"/*.json; do
        jq --argjson n "$n" '[.[] | select(.number == $n)]' "$f" >"$dst/$(basename "$f")"
    done
}
section_body() { awk -v h="$1" '$0 == h {flag=1; next} /^## /{flag=0} flag' <<<"$2"; }
row_of() { grep -F -- "$ROW" <<<"$1" || true; }
strip_row() { grep -vF -- "$ROW" <<<"$1" || true; }

# ---------------------------------------------------------------------------
# Test 2 (AC): the reproduction's five synthetic items
# ---------------------------------------------------------------------------
echo ""
echo "Test 2: only the issue whose sole stage label is a hold is counted"

# The issue's reproduction, as per-label fixtures: #9101 `loom:blocked` and
# nothing else; #9102 also curated; #9103 ready; #9104 being built; #9105
# also important.
FIX="$TMPROOT/ac1"
mkdir -p "$FIX"
cat > "$FIX/issue-loom_blocked.json" <<'JSON'
[
  {"number":9101,"title":"parked, no other stage label","labels":[{"name":"loom:blocked"}]},
  {"number":9102,"title":"parked and curated","labels":[{"name":"loom:blocked"},{"name":"loom:curated"}]},
  {"number":9105,"title":"parked and important","labels":[{"name":"loom:blocked"},{"name":"loom:important"}]}
]
JSON
cat > "$FIX/issue-loom_curated.json" <<'JSON'
[{"number":9102,"title":"parked and curated","labels":[{"name":"loom:blocked"},{"name":"loom:curated"}]}]
JSON
cat > "$FIX/issue-loom_important.json" <<'JSON'
[{"number":9105,"title":"parked and important","labels":[{"name":"loom:blocked"},{"name":"loom:important"}]}]
JSON
cat > "$FIX/issue-loom_issue.json" <<'JSON'
[{"number":9103,"title":"ready","labels":[{"name":"loom:issue"}]}]
JSON
cat > "$FIX/issue-loom_building.json" <<'JSON'
[{"number":9104,"title":"being built","labels":[{"name":"loom:building"}]}]
JSON

RENDER="$(render_with "$FIX" "$RPB_BODY")"
if [[ -z "$RENDER" ]]; then
    fail "render_plan_body() produced no output against the fixtures (harness broken)"
    echo -e "${RED}FATAL${NC}: cannot continue without a render"
    exit 1
else
    pass "render_plan_body() executed against the stubbed \$GH_READ and loom-daemon"
fi

assert_eq "| Held, no other bucket | 1 |" "$(row_of "$RENDER")" \
    "the five reproduction items render '| Held, no other bucket | 1 |' (only #9101)"
if grep -qxF -- "$ROW 1 |" <<<"$(section_body '## Backlog Balance' "$RENDER")"; then
    pass "the row sits in the Backlog Balance table"
else
    fail "expected the row inside the '## Backlog Balance' table"
fi
if grep -q '#9101' <<<"$(strip_row "$RENDER")"; then
    fail "#9101 must gain no bullet — the row is a count, not a listing (#13908)"
else
    pass "#9101 still has no bullet in any section; the row is its only trace"
fi

# The other four keep the bullets they already had, so a count of 1 means
# "9101 only", not "one of five".
expect_in() {
    # expect_in <section-heading> <exact-line> <msg>: the exact bullet line
    # must appear in that section of the current $RENDER.
    if grep -qxF -- "$2" <<<"$(section_body "$1" "$RENDER")"; then
        pass "$3"
    else
        fail "$3 (expected exact line '$2' under '$1')"
    fi
}
expect_in '## Important' '- **#9105**: parked and important *(held)*' \
    "#9105 (held + loom:important) renders under Important, not in the row"
expect_in '## Ready' '- **#9103**: ready' \
    "#9103 (loom:issue, unheld) renders under Ready, not in the row"
expect_in '## In Progress' '- **#9104**: being built' \
    "#9104 (loom:building, unheld) renders under In Progress, not in the row"
expect_in '## Proposed' '- **#9102**: parked and curated *(curated)* *(held)*' \
    "#9102 (held + loom:curated) renders under Proposed, not in the row"

for n in 9102 9103 9104 9105; do
    mk_variant "$FIX" "$TMPROOT/only-$n" "$n"
    assert_eq "| Held, no other bucket | 0 |" "$(row_of "$(render_with "$TMPROOT/only-$n" "$RPB_BODY")")" \
        "#$n alone contributes 0 to the row"
done

# #14785: an open PR linked to the held issue (here by its Builder branch)
# does not give it a bucket — "Issues with Open PRs" lists Ready candidates
# only, and a held issue is never one — so the row still counts it.
mkdir -p "$TMPROOT/ac1-pr"
cp "$FIX"/*.json "$TMPROOT/ac1-pr/"
cat > "$TMPROOT/ac1-pr/pr-.json" <<'JSON'
[{"number":8001,"title":"Open PR on the held issue's branch","state":"open","labels":[],"closingIssuesReferences":[],"body":"","headRefName":"feature/issue-9101","isCrossRepository":false}]
JSON
RPR="$(render_with "$TMPROOT/ac1-pr" "$RPB_BODY")"
assert_eq "| Held, no other bucket | 1 |" "$(row_of "$RPR")" \
    "an open PR linked to the held issue leaves the count at 1"
OPENPR_BODY="$(section_body '## Issues with Open PRs (Not Dispatchable)' "$RPR")"
if grep -qxF '_None._' <<<"$OPENPR_BODY" && ! grep -q '#9101' <<<"$OPENPR_BODY"; then
    pass "a held issue is not listed under 'Issues with Open PRs' (Ready candidates only, #14785)"
else
    fail "expected the held issue absent from '## Issues with Open PRs (Not Dispatchable)', got: $OPENPR_BODY"
fi

# ---------------------------------------------------------------------------
# Test 3 (AC): frozen, and the two loom:building shapes
# ---------------------------------------------------------------------------
echo ""
echo "Test 3: frozen is not counted; blocked+building is In Progress; operator+building counts"

FIX3="$TMPROOT/ac2"
mkdir -p "$FIX3"
cat > "$FIX3/issue-loom_frozen.json" <<'JSON'
[{"number":9201,"title":"frozen, nothing else","labels":[{"name":"loom:frozen"}]}]
JSON
cat > "$FIX3/issue-loom_blocked.json" <<'JSON'
[{"number":9202,"title":"blocked while building","labels":[{"name":"loom:blocked"},{"name":"loom:building"}]}]
JSON
cat > "$FIX3/issue-loom_operator.json" <<'JSON'
[{"number":9203,"title":"operator-held while building","labels":[{"name":"loom:operator"},{"name":"loom:building"}]}]
JSON
cat > "$FIX3/issue-loom_building.json" <<'JSON'
[
  {"number":9202,"title":"blocked while building","labels":[{"name":"loom:blocked"},{"name":"loom:building"}]},
  {"number":9203,"title":"operator-held while building","labels":[{"name":"loom:operator"},{"name":"loom:building"}]}
]
JSON

mk_variant "$FIX3" "$TMPROOT/only-9201" 9201
R9201="$(render_with "$TMPROOT/only-9201" "$RPB_BODY")"
assert_eq "| Held, no other bucket | 0 |" "$(row_of "$R9201")" \
    "a loom:frozen-only issue does not count (every section drops frozen on purpose)"

mk_variant "$FIX3" "$TMPROOT/only-9202" 9202
R9202="$(render_with "$TMPROOT/only-9202" "$RPB_BODY")"
assert_eq "| Held, no other bucket | 0 |" "$(row_of "$R9202")" \
    "a loom:blocked + loom:building issue does not count"
if grep -qxF -- '- **#9202**: blocked while building' <<<"$(section_body '## In Progress' "$R9202")"; then
    pass "#9202 is listed under In Progress, which is why it has a bucket"
else
    fail "expected #9202 under '## In Progress' (loom:building, no operator gate)"
fi

mk_variant "$FIX3" "$TMPROOT/only-9203" 9203
R9203="$(render_with "$TMPROOT/only-9203" "$RPB_BODY")"
assert_eq "| Held, no other bucket | 1 |" "$(row_of "$R9203")" \
    "a loom:operator + loom:building issue counts"
if grep -q '#9203' <<<"$(section_body '## In Progress' "$R9203")"; then
    fail "#9203 carries an operator gate, so it must NOT be listed under In Progress"
else
    pass "#9203 is not listed under In Progress (operator gate), so the row is its only bucket"
fi

# ---------------------------------------------------------------------------
# Test 4 (AC): no held issues renders 0, and nothing else changes
# ---------------------------------------------------------------------------
echo ""
echo "Test 4: with no held issues the row renders 0 and the body is otherwise byte-identical"

mkdir -p "$TMPROOT/empty"
RENDER_EMPTY="$(render_with "$TMPROOT/empty" "$RPB_BODY")"
assert_eq "| Held, no other bucket | 0 |" "$(row_of "$RENDER_EMPTY")" \
    "with no held issues the row renders 0"

RENDER_EMPTY_OLD="$(render_with "$TMPROOT/empty" "$OLD_BODY")"
assert_eq "$RENDER_EMPTY_OLD" "$(strip_row "$RENDER_EMPTY")" \
    "no-held render minus the row is byte-identical to the pre-fix render"

RENDER_AC1_OLD="$(render_with "$FIX" "$OLD_BODY")"
assert_eq "$RENDER_AC1_OLD" "$(strip_row "$RENDER")" \
    "the reproduction render minus the row is byte-identical to the pre-fix render"
assert_eq "$(grep -c '^## ' <<<"$RENDER_AC1_OLD")" "$(grep -c '^## ' <<<"$RENDER")" \
    "the fix adds no section"

# ---------------------------------------------------------------------------
# Test 5 (AC, #15228): a poisoned listing read renders 0, not an empty cell
# ---------------------------------------------------------------------------
echo ""
echo "Test 5: a read that puts non-JSON on stdout renders 0, not an empty cell"

# Every sibling row reads its count through count(), which guards empty input
# ('[ -z "$1" ] && printf 0'), but the held row read $held_unbucketed raw: a
# listing that puts non-JSON on stdout makes the loop's 'jq -s add // []'
# slurp fail (exit 5, no output), and jq on the then-empty stream prints
# nothing (exit 0) — so the row rendered '| Held, no other bucket |  |'.
# gh writes errors to stderr, so only a stdout pollutant (e.g. a stale cache
# read behind $GH_READ) produces this shape. Variant stub: one hold-label
# issue listing answers non-JSON on stdout; everything else delegates to the
# fixture stub.
GH_STUB_GARBAGE="$TMPROOT/gh-stub-garbage"
cat > "$GH_STUB_GARBAGE" <<STUB
#!/usr/bin/env bash
label=""; prev=""
for a in "\$@"; do
  [[ "\$prev" = "--label" ]] && label="\$a"
  prev="\$a"
done
if [[ "\${1:-}" = issue && "\$label" = loom:blocked ]]; then
  printf 'stale cache entry: non-JSON on stdout\n'
  exit 0
fi
exec "$GH_STUB" "\$@"
STUB
chmod +x "$GH_STUB_GARBAGE"

RENDER_GARBAGE="$(LOOM_TEST_GH_READ="$GH_STUB_GARBAGE" render_with "$FIX" "$RPB_BODY")"
assert_eq "| Held, no other bucket | 0 |" "$(row_of "$RENDER_GARBAGE")" \
    "a read that emits non-JSON on stdout renders '| Held, no other bucket | 0 |', never an empty cell (#15228)"

# ---------------------------------------------------------------------------
# Test 6: registry-driven — retired hold name, two holds, fallback list
# ---------------------------------------------------------------------------
echo ""
echo "Test 6: the count follows the registry hold set"

FIX5="$TMPROOT/ac5"
mkdir -p "$FIX5"
cat > "$FIX5/issue-loom_operator-only.json" <<'JSON'
[{"number":9501,"title":"unmigrated retired hold, nothing else","labels":[{"name":"loom:operator-only"}]}]
JSON
cat > "$FIX5/issue-loom_blocked.json" <<'JSON'
[{"number":9502,"title":"two holds, no other label","labels":[{"name":"loom:blocked"},{"name":"loom:operator"}]}]
JSON
cp "$FIX5/issue-loom_blocked.json" "$FIX5/issue-loom_operator.json"

mk_variant "$FIX5" "$TMPROOT/only-9501" 9501
mk_variant "$FIX5" "$TMPROOT/only-9502" 9502
assert_eq "| Held, no other bucket | 1 |" "$(row_of "$(render_with "$TMPROOT/only-9501" "$RPB_BODY")")" \
    "an unmigrated retired hold name (registry set, not hardcoded) is counted"
assert_eq "| Held, no other bucket | 1 |" "$(row_of "$(render_with "$TMPROOT/only-9502" "$RPB_BODY")")" \
    "an issue carrying two hold labels is counted once, not once per query"

assert_eq "| Held, no other bucket | 1 |" "$(row_of "$(render_with "$TMPROOT/only-9502" "$RPB_BODY" "$TMPROOT/bin-fail")")" \
    "fallback list still counts a loom:blocked issue when the daemon call fails"
assert_eq "| Held, no other bucket | 0 |" "$(row_of "$(render_with "$TMPROOT/only-9501" "$RPB_BODY" "$TMPROOT/bin-fail")")" \
    "fallback list cannot count the retired name (it only knows the three holds)"

# ---------------------------------------------------------------------------
# Test 7: NEGATIVE CONTROL — the pre-fix body renders no row and no #9101
# ---------------------------------------------------------------------------
echo ""
echo "Test 7: negative control — the pre-fix render has no row (bug reproduced)"

if [[ -z "$(row_of "$RENDER_AC1_OLD")" ]]; then
    pass "the pre-fix render has no 'Held, no other bucket' row"
else
    fail "expected no such row in the pre-fix render (got '$(row_of "$RENDER_AC1_OLD")')"
fi
if grep -q '#9101' <<<"$RENDER_AC1_OLD"; then
    fail "expected #9101 to appear nowhere in the pre-fix render"
else
    pass "pre-fix, the parked hold-only issue appears nowhere in the region (the reported bug)"
fi

# ---------------------------------------------------------------------------
echo ""
echo "================================"
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}${TESTS_PASSED}${NC}"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "Tests failed: ${RED}${TESTS_FAILED}${NC}"
    exit 1
fi
echo "All tests passed"
exit 0
