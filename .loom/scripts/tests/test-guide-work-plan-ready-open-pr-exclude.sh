#!/usr/bin/env bash
# test-guide-work-plan-ready-open-pr-exclude.sh - Regression tests #7083/#13928/#14785
#
# #7083 excluded merge-pending issues (an open `loom:pr` PR's closes-graph)
# from the Guide WORK_PLAN "Ready" section, and #13928 annotated the closing
# PR. #14785 (operator ruling 2026-11-23, option a — deliberately reversing
# this suite's own prior mid-review inclusion) widens that subtraction to
# the whole PR-link leg of native Builder admission: an issue ANY open
# linked PR points at — closes-graph, #6216 body phrase (Closes/Fixes/
# Resolves, Part of, Contributes to), or an exact feature/issue-N branch —
# is NOT dispatchable (`loom-daemon forge check-claim` answers `OPEN_PR #X`
# for it), so it leaves Ready and its count and is listed, with the PR and
# its stage, under "Issues with Open PRs (Not Dispatchable)" instead.
#
# THE FIX: `render_plan_body()` broadens its one PR fetch to a full open-PR
# inventory (`pr list --state open`, no label filter). `approved_json`
# becomes a jq filter of that inventory (loom:pr label, not frozen — the
# Approved/held renders stay byte-identical), and `open_pr_linkage_rows()`
# derives the linked-issue set with the same union the native OPEN_PR leg
# uses. ONE derivation feeds the Ready filter, the listing section and the
# Backlog Balance count. A failed inventory read renders an unfiltered
# Ready plus a visible note, and `update_work_plan()` refuses to write over
# it (the in-band unreadable note / the "inventory-unreadable" receipt).
#
# Verifies that:
#   1. STRUCTURE: exactly one inventory `pr list --state open` fetch; zero
#      `pr list --label "loom:pr"` left in the render (approved_json is
#      derived, not queried); the linkage is derived from the inventory (no
#      per-Ready-issue probe); verify_plan_provenance() checks the listing
#      through the SAME open_pr_annotation() helper the render uses.
#   2. EXCLUSIONS, executed rather than grepped: issues whose open PR links
#      by approved closes-graph, mid-review `Part of` (#7102 — the reversed
#      assertion), held approved `Part of`, parent-branch feature/issue-N
#      partial, issue-branch PR with no directive at all, and draft
#      `Contributes to` — none appears in Ready or its count, and each is
#      listed with the PR number and its stage.
#   3. CONTROLS: a plain issue, a bare "#N" mention, a `Part of Epic #N`
#      parent reference (b89d38d1e), a foreign-repo qualified reference and
#      a closed PR must NOT suppress Ready work.
#   4. NEGATIVE CONTROL: the pre-fix (unfiltered) ready pipeline DOES
#      include the excluded issues — i.e. this test would have failed before
#      the fix.
#   5. SELF-DESCRIPTION (#13928, updated for #14785): the Ready blurb and
#      the Backlog Balance row name the open-PR admission subtraction;
#      Approved bullets keep their `— closes #N` annotations (unchanged).
#   6. ADMISSION PARITY: for the same synthetic links, the extracted
#      open_pr_linkage_rows() classification equals the documented native
#      OPEN_PR verdicts, the plan's Ready/listing split matches it, and the
#      phrase families + branch prefix are pinned to the Rust source
#      (loom-daemon/src/worktree_ops/{gh,naming}.rs) so the shell mirror
#      cannot silently drift from the native rule.
#   7. INVENTORY LIMITS: a failed read renders an unfiltered Ready plus the
#      visible note and clears WORK_PLAN_OPEN_PR_INVENTORY_OK (the write
#      refusal flag); a 200-PR inventory renders the truncation note.
#
# Hermetic: `gh` is stubbed with fixture JSON; only the real `jq` binary is
# invoked (skipped if unavailable) — no forge/network calls.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
# guide.md is shipped (installed at .claude/commands/loom/guide.md), so
# resolve the installed path first (consumer repos, and Loom's own dogfooded
# checkout), falling back to the defaults/ source-tree path (a bare source
# checkout with no .claude/commands/loom/ copy yet). See issue #6194 / #6241.
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

# Same extraction the #6993/#5930/#6457/#7071 suites use: the function's
# closing `}` is the first line starting at column 0 after the opening line
# (its nested `section()`/`count()` helpers are indented).
extract_fn() { awk -v re="^$1\\\\(\\\\) \\\\{" '$0 ~ re {f=1} f{print} /^\}/{if(f) exit}' "$GUIDE_MD"; }

RPB_BODY="$(extract_fn render_plan_body)"
HELPERS_BODY="$(extract_fn open_pr_linkage_rows; extract_fn open_pr_annotation)"
UWP_BODY="$(extract_fn update_work_plan)"
VPP_BODY="$(extract_fn verify_plan_provenance)"

if [[ -z "$RPB_BODY" ]]; then
    fail "could not extract render_plan_body() body from guide.md"
    echo -e "${RED}FATAL${NC}: cannot continue without the function body"
    exit 1
fi
pass "render_plan_body() body extracted from $GUIDE_MD"

if [[ -z "$HELPERS_BODY" ]]; then
    fail "could not extract open_pr_linkage_rows()/open_pr_annotation() from guide.md"
    echo -e "${RED}FATAL${NC}: cannot continue without the linkage helpers"
    exit 1
fi
pass "open_pr_linkage_rows()/open_pr_annotation() bodies extracted"

# ---------------------------------------------------------------------------
# Test 1: STRUCTURE — one broadened inventory fetch, everything derived.
# ---------------------------------------------------------------------------
echo ""
echo "Test 1: one open-PR inventory fetch; approved/linkage derived from it"

if grep -qF -- '--json number,title,labels,isDraft,headRefName,isCrossRepository,body,closingIssuesReferences' <<<"$RPB_BODY" \
   && grep -qF '"$GH_READ" pr list --state open --limit 200' <<<"$RPB_BODY"; then
    pass "render_plan_body() fetches one broadened open-PR inventory (body/headRefName/closingIssuesReferences included)"
else
    fail "expected the single 'pr list --state open' inventory fetch with the linkage fields"
fi

INVENTORY_QUERY_COUNT="$(grep -cF '"$GH_READ" pr list --state open' <<<"$RPB_BODY" || true)"
if [[ "$INVENTORY_QUERY_COUNT" -eq 1 ]]; then
    pass "exactly one 'pr list --state open' inventory query in render_plan_body()"
else
    fail "expected exactly one 'pr list --state open' query, found $INVENTORY_QUERY_COUNT"
fi

# Count executable queries only (`$GH_READ" pr list …`): the #14785 comment
# block quotes the retired labelled fetch in prose, and prose is not a query.
LABELLED_PR_QUERY_COUNT="$(grep -cF '"$GH_READ" pr list --label "loom:pr"' <<<"$RPB_BODY" || true)"
if [[ "$LABELLED_PR_QUERY_COUNT" -eq 0 ]]; then
    pass "no 'pr list --label \"loom:pr\"' query left in render_plan_body() (approved_json is derived from the inventory)"
else
    fail "expected zero 'pr list --label \"loom:pr\"' queries in the render, found $LABELLED_PR_QUERY_COUNT"
fi

if grep -qF 'approved_json=$(printf '"'"'%s'"'"' "$pr_inventory" | jq -c' <<<"$RPB_BODY"; then
    pass "approved_json is derived from \$pr_inventory via printf | jq (no fresh query)"
else
    fail "expected approved_json= to be derived from \$pr_inventory"
fi

if grep -qF 'openpr_rows_json="$(printf '"'"'%s'"'"' "$pr_inventory" | open_pr_linkage_rows)"' <<<"$RPB_BODY"; then
    pass "the linkage rows are derived from \$pr_inventory via open_pr_linkage_rows() (no per-issue probe)"
else
    fail "expected openpr_rows_json= to be derived from \$pr_inventory via open_pr_linkage_rows()"
fi

if grep -qF 'pr_linked_issues=$(printf '"'"'%s'"'"' "$openpr_rows_json" | jq -c '"'"'map(.issue) | unique'"'"')' <<<"$RPB_BODY"; then
    pass "pr_linked_issues comes from the SAME linkage rows as the listing (single derivation)"
else
    fail "expected pr_linked_issues= to be derived from \$openpr_rows_json"
fi

if grep -qF 'section "Issues with Open PRs (Not Dispatchable)"' <<<"$RPB_BODY"; then
    pass "render_plan_body() emits the 'Issues with Open PRs (Not Dispatchable)' section"
else
    fail "expected the 'Issues with Open PRs (Not Dispatchable)' section call"
fi

if grep -qF 'open_pr_annotation' <<<"$VPP_BODY" && grep -qF "'## Issues with Open PRs (Not Dispatchable)'" <<<"$VPP_BODY"; then
    pass "verify_plan_provenance() gates the listing through the same open_pr_annotation() helper"
else
    fail "expected verify_plan_provenance() to verify the listing via open_pr_annotation()"
fi

if grep -qF 'inventory-unreadable' <<<"$UWP_BODY" && ! grep -qF 'WORK_PLAN_OPEN_PR_INVENTORY_OK' <<<"$UWP_BODY$RPB_BODY"; then
    pass "update_work_plan() carries the inventory-unreadable gate with no cross-subshell flag variable (behavior: Test 8)"
else
    fail "expected update_work_plan() to record inventory-unreadable and no WORK_PLAN_OPEN_PR_INVENTORY_OK global (subshell-lost, #14785)"
fi

if ! command -v jq >/dev/null 2>&1; then
    echo ""
    echo "  SKIP: jq not available — skipping the executable exclusion tests"
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
# honours `--state` (rows without a state field count as open, matching the
# pre-#14785 fixtures), applies `--search "-label:X"` negation the way real
# `gh` combines it (ANDed) with `--label`, then honours `--jq` exactly like
# real `gh ... --json ... --jq EXPR` does. FAIL_INVENTORY=1 fails the
# unlabelled `pr list` (the render's inventory fetch) the way a dead forge
# would.
# ---------------------------------------------------------------------------
TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

GH_STUB="$TMPROOT/gh-stub"
cat > "$GH_STUB" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
kind="${1:-}"; shift || true
label=""; search=""; jqexpr=""; state=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --label)  label="${2:-}"; shift 2 ;;
    --search) search="${2:-}"; shift 2 ;;
    --jq)     jqexpr="${2:-}"; shift 2 ;;
    --state)  state="${2:-}"; shift 2 ;;
    *)        shift ;;
  esac
done
if [[ "${FAIL_INVENTORY:-}" == 1 && "$kind" == pr && -z "$label" ]]; then exit 1; fi
fixture="$LOOM_FIXTURE_DIR/${kind}-${label//:/_}.json"
if [[ -f "$fixture" ]]; then json="$(cat "$fixture")"; else json='[]'; fi
if [[ -n "$state" ]]; then
  json="$(printf '%s' "$json" | jq --arg s "$state" '[.[] | select((.state // "open") == $s)]')"
fi
# Apply every "-label:X" negation term in --search, ANDed with --label,
# exactly like real gh's combined --label + --search filtering.
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

mkdir -p "$TMPROOT/fixtures"
FIX="$TMPROOT/fixtures"

# `ready` fixture (loom:issue query, already past the loom:building/
# holds --search exclusion — this suite only exercises the open-PR
# admission exclusion):
#   #7100 plain ready issue, no linked PR at all.
#   #7101 has a linked PR (#8001) that is OPEN, carries loom:pr and closes
#         it — merge-pending, excluded since #7083.
#   #7102 has a linked PR (#8002, body "Part of #7102") that is still
#         mid-review (no loom:pr) — excluded by #14785's ruling: the native
#         OPEN_PR leg refuses it regardless of review stage.
#   #7103 held approved partial PR (#8005, loom:pr + loom:operator).
#   #7104 parent-branch partial PR (#8006 on feature/issue-7104).
#   #7105 issue-branch PR with no closing directive (#8007, branch-only).
#   #7106 draft partial PR (#8008, "Contributes to #7106").
#   #7107 bare-mention control (#8009 mentions it without a phrase).
#   #7108 epic-parent control (#8010 says "Part of Epic #7108" — a parent
#         reference, b89d38d1e, never a link).
#   #7109 foreign-reference control (#8011: "Contributes to example-org/tool-repo#7109").
#   #7110 closed-PR control (#8012 is state closed, "Closes #7110").
cat > "$FIX/issue-loom_issue.json" <<'JSON'
[
  {"number":7100,"title":"Plain ready issue, no linked PR","labels":[{"name":"loom:issue"}]},
  {"number":7101,"title":"Issue with an open loom:pr-labeled linked PR","labels":[{"name":"loom:issue"}]},
  {"number":7102,"title":"Issue with a linked PR still under review","labels":[{"name":"loom:issue"}]},
  {"number":7103,"title":"Issue with a held approved partial PR","labels":[{"name":"loom:issue"}]},
  {"number":7104,"title":"Issue with a parent-branch partial PR","labels":[{"name":"loom:issue"}]},
  {"number":7105,"title":"Issue with an issue-branch PR and no directive","labels":[{"name":"loom:issue"}]},
  {"number":7106,"title":"Issue with a draft partial PR","labels":[{"name":"loom:issue"}]},
  {"number":7107,"title":"Bare-mention control","labels":[{"name":"loom:issue"}]},
  {"number":7108,"title":"Epic-parent control","labels":[{"name":"loom:issue"}]},
  {"number":7109,"title":"Foreign-reference control","labels":[{"name":"loom:issue"}]},
  {"number":7110,"title":"Closed-PR control","labels":[{"name":"loom:issue"}]}
]
JSON

# The open-PR inventory fixture (the unlabelled `pr list --state open`
# fetch). #8004 additionally carries "Part of #7101" in its body so one
# issue (#7101) is linked by TWO PRs — the multi-PR annotation case.
cat > "$FIX/pr-.json" <<'JSON'
[
  {"number":8001,"title":"Fix for #7101","state":"open","labels":[{"name":"loom:pr"}],"closingIssuesReferences":[{"number":7101}],"body":"Closes #7101"},
  {"number":8002,"title":"Mid-review partial PR for #7102","state":"open","labels":[{"name":"loom:review-requested"}],"closingIssuesReferences":[],"body":"Part of #7102"},
  {"number":8003,"title":"Chore PR closing nothing","state":"open","labels":[{"name":"loom:pr"}],"closingIssuesReferences":[],"body":""},
  {"number":8004,"title":"Multi-close PR","state":"open","labels":[{"name":"loom:pr"}],"closingIssuesReferences":[{"number":42},{"number":7}],"body":"Closes #42 and fixes #7. Part of #7101"},
  {"number":8005,"title":"Held approved partial PR for #7103","state":"open","labels":[{"name":"loom:pr"},{"name":"loom:operator"}],"closingIssuesReferences":[],"body":"Part of #7103"},
  {"number":8006,"title":"Parent-branch partial PR for #7104","state":"open","labels":[],"closingIssuesReferences":[],"isCrossRepository":false,"headRefName":"feature/issue-7104","body":"Part of #7104"},
  {"number":8007,"title":"Issue-branch PR with no directive","state":"open","labels":[],"closingIssuesReferences":[],"isCrossRepository":false,"headRefName":"feature/issue-7105","body":""},
  {"number":8008,"title":"Draft partial PR for #7106","state":"open","labels":[],"isDraft":true,"closingIssuesReferences":[],"body":"Contributes to #7106"},
  {"number":8009,"title":"Bare-mention PR","state":"open","labels":[],"closingIssuesReferences":[],"body":"Also see #7107 for context"},
  {"number":8010,"title":"Epic-phase PR","state":"open","labels":[],"closingIssuesReferences":[],"body":"Part of Epic #7108. Depends on #4"},
  {"number":8011,"title":"Foreign-repo reference PR","state":"open","labels":[],"closingIssuesReferences":[],"body":"Contributes to example-org/tool-repo#7109"},
  {"number":8012,"title":"Closed PR that once closed #7110","state":"closed","labels":[],"closingIssuesReferences":[],"body":"Closes #7110"}
]
JSON

RPB_RUN='eval "$HELPERS_SRC"; eval "$RPB_SRC"; render_plan_body'
RENDER="$(LOOM_FIXTURE_DIR="$FIX" GH_READ="$GH_STUB" HELPERS_SRC="$HELPERS_BODY" RPB_SRC="$RPB_BODY" bash -c "$RPB_RUN")"

if [[ -z "$RENDER" ]]; then
    fail "render_plan_body() produced no output against the fixtures (harness broken)"
else
    pass "render_plan_body() executed against the stubbed \$GH_READ"
fi

READY_BODY="$(awk '/^## Ready/{flag=1; next} /^## /{flag=0} flag' <<<"$RENDER")"
OPENPR_BODY="$(awk '/^## Issues with Open PRs/{flag=1; next} /^## /{flag=0} flag' <<<"$RENDER")"
APPROVED_BODY="$(awk '/^## Approved/{flag=1; next} /^## /{flag=0} flag' <<<"$RENDER")"

# ---------------------------------------------------------------------------
# Test 2 (AC): the excluded six leave Ready and its count.
# ---------------------------------------------------------------------------
echo ""
echo "Test 2: every open-linked-PR issue leaves the Ready section and count"

for n in 7101 7102 7103 7104 7105 7106; do
    if grep -q "#$n" <<<"$READY_BODY"; then
        fail "#$n (open linked PR) must NOT appear in the Ready section"
    else
        pass "#$n is excluded from the Ready section"
    fi
done

READY_COUNT="$(grep -c '^- ' <<<"$READY_BODY" || true)"
if [[ "$READY_COUNT" -eq 5 ]]; then
    pass "Ready count is 5 (the five controls), not ${READY_COUNT}X"
else
    fail "expected Ready to list exactly the 5 control issues, got $READY_COUNT lines"
fi

BALANCE_ROW="$(grep -F '| Ready (`loom:issue`, net of holds/building/open-linked-PRs) |' <<<"$RENDER")"
if [[ -n "$BALANCE_ROW" ]] && grep -qF '| 5 |' <<<"$BALANCE_ROW"; then
    pass "Backlog Balance Ready row uses the SAME filtered count (5) with the open-linked-PRs label"
else
    fail "expected 'Ready (`loom:issue`, net of holds/building/open-linked-PRs) | 5 |', got: $BALANCE_ROW"
fi

# ---------------------------------------------------------------------------
# Test 3 (AC): each excluded issue is listed with its PR and that PR's stage.
# ---------------------------------------------------------------------------
echo ""
echo "Test 3: the listing shows each excluded issue with its PR and stage"

expect_bullet() { # issue expected-suffix-regex description
    if grep -qF -- "- **#$1**: $2" <<<"$OPENPR_BODY"; then
        pass "$3"
    else
        fail "$3 — expected '- **#$1**: $2', section body: $(tr '\n' '|' <<<"$OPENPR_BODY")"
    fi
}

expect_bullet 7101 "Issue with an open loom:pr-labeled linked PR — open PRs #8001 (approved), #8004 (approved)" \
    "#7101 lists BOTH linking PRs, PR-number-sorted (multi-PR annotation)"
expect_bullet 7102 "Issue with a linked PR still under review — open PR #8002 (in review)" \
    "#7102 (the reversed mid-review case) lists PR #8002 with stage 'in review'"
expect_bullet 7103 "Issue with a held approved partial PR — open PR #8005 (held)" \
    "#7103 lists the held approved partial PR with stage 'held'"
expect_bullet 7104 "Issue with a parent-branch partial PR — open PR #8006 (unlabeled)" \
    "#7104 lists the parent-branch partial PR"
expect_bullet 7105 "Issue with an issue-branch PR and no directive — open PR #8007 (unlabeled)" \
    "#7105 lists the branch-only PR with no closing directive"
expect_bullet 7106 "Issue with a draft partial PR — open PR #8008 (draft)" \
    "#7106 lists the draft PR with stage 'draft'"

for n in 7100 7107 7108 7109 7110; do
    if grep -q "#$n" <<<"$OPENPR_BODY"; then
        fail "#$n (a control) must NOT appear in the open-PR listing"
    else
        pass "#$n stays out of the open-PR listing"
    fi
done

# ---------------------------------------------------------------------------
# Test 4: CONTROLS — no over-exclusion.
# ---------------------------------------------------------------------------
echo ""
echo "Test 4: controls still render in Ready"

for n in 7100 7107 7108 7109 7110; do
    if grep -q "#$n" <<<"$READY_BODY"; then
        pass "#$n still appears in the Ready section"
    else
        fail "#$n should appear in Ready — its link does not satisfy the native rule"
    fi
done

# ---------------------------------------------------------------------------
# Test 5: NEGATIVE CONTROL — the pre-fix (unfiltered) ready query DOES
# include the excluded issues (bug reproduced).
# ---------------------------------------------------------------------------
echo ""
echo "Test 5: negative control — the pre-fix query would have included the excluded six"

PRE_READY="$(LOOM_FIXTURE_DIR="$FIX" "$GH_STUB" issue list --label "loom:issue" --state open --limit 200 \
    --json number,title --jq '.[] | "- **#\(.number)**: \(.title)"')"

for n in 7101 7102 7103 7104 7105 7106; do
    if grep -q "#$n" <<<"$PRE_READY"; then
        pass "without the exclusion, #$n WOULD have rendered into Ready (bug reproduced)"
    else
        fail "expected the negative-control (unfiltered) query to include #$n"
    fi
done

# ---------------------------------------------------------------------------
# Test 6: SELF-DESCRIPTION (#13928, updated for #14785) — the Ready blurb
# names the open-PR subtraction; Approved keeps its closes annotations.
# ---------------------------------------------------------------------------
echo ""
echo "Test 6: Ready blurb and Backlog row name the exclusions (#13928/#14785)"

BLURB_LINE="$(awk '/^## Ready/{flag=1; next} /^## /{flag=0} flag' <<<"$RENDER" | grep -m1 'Human-approved' || true)"
if grep -qF 'excluding held (`loom:blocked`/`loom:operator`/`loom:frozen`), `loom:building`, and issues with an open linked PR (native admission `OPEN_PR`; listed under Issues with Open PRs)' <<<"$BLURB_LINE"; then
    pass "Ready blurb names the open-PR admission subtraction"
else
    fail "Ready blurb does not name the open-PR admission subtraction: '$BLURB_LINE'"
fi

if grep -q -- '- \*\*#8001\*\*: Fix for #7101 — closes #7101' <<<"$APPROVED_BODY"; then
    pass "PR #8001 bullet keeps its '— closes #7101' annotation (unchanged #13928 behavior)"
else
    fail "expected '— closes #7101' on the #8001 bullet, got: $(grep '#8001' <<<"$APPROVED_BODY" || echo MISSING)"
fi

if grep -q -- '- \*\*#8004\*\*: Multi-close PR — closes #7, #42' <<<"$APPROVED_BODY"; then
    pass "PR #8004 closing two issues annotates them sorted (unchanged)"
else
    fail "expected a sorted multi-issue annotation on #8004, got: $(grep '#8004' <<<"$APPROVED_BODY" || echo MISSING)"
fi

CLOSES_8003="$(grep '#8003' <<<"$APPROVED_BODY" || true)"
if [[ -n "$CLOSES_8003" ]] && ! grep -q 'closes' <<<"$CLOSES_8003"; then
    pass "PR #8003 (empty closingIssuesReferences) renders unchanged — no annotation"
else
    fail "PR #8003 (no closing refs) should render without an annotation, got: '$CLOSES_8003'"
fi

# ---------------------------------------------------------------------------
# Test 7: ADMISSION PARITY — the extracted linkage equals the documented
# native OPEN_PR verdicts, the plan split matches it, and the shell mirror
# is pinned to the Rust source in lockstep.
# ---------------------------------------------------------------------------
echo ""
echo "Test 7: admission parity between the plan and the native OPEN_PR rule"

# 7a. The extracted open_pr_linkage_rows() over the SAME --state-filtered
# inventory must classify exactly the documented blocked set — this is the
# control the issue requires: the same synthetic links, the native rule's
# verdicts.
BLOCKED_EXPECTED='7101 7102 7103 7104 7105 7106'
INVENTORY_JSON="$(LOOM_FIXTURE_DIR="$FIX" "$GH_STUB" pr list --state open --limit 200 \
    --json number,title,labels,isDraft,headRefName,isCrossRepository,body,closingIssuesReferences)"
ROWS_JSON="$(printf '%s' "$INVENTORY_JSON" | HELPERS_SRC="$HELPERS_BODY" bash -c 'eval "$HELPERS_SRC"; open_pr_linkage_rows')"
# The linkage legitimately also reaches non-candidate issues (#8004 closes
# #7 and #42, which carry no `loom:issue` label); the parity contract is
# over the Ready candidates the plan actually classifies.
BLOCKED_GOT="$(printf '%s' "$ROWS_JSON" | jq -r --argjson cand \
    "$(LOOM_FIXTURE_DIR="$FIX" "$GH_STUB" issue list --label loom:issue --state open --limit 200 --json number | jq -c '.')" \
    'map(.issue) | unique | .[] | select(. as $n | $cand | map(.number) | index($n))' | tr '\n' ' ' | sed 's/ $//')"
if [[ "$BLOCKED_GOT" == "$BLOCKED_EXPECTED" ]]; then
    pass "open_pr_linkage_rows() classifies exactly the native-blocked set ($BLOCKED_EXPECTED)"
else
    fail "linkage classified '$BLOCKED_GOT', expected '$BLOCKED_EXPECTED'"
fi

# 7b. The plan's split must match that classification: blocked issues are
# listed (not Ready), allowed issues are Ready (not listed).
parity_ok=1
for n in $BLOCKED_EXPECTED; do
    grep -q "#$n" <<<"$OPENPR_BODY" || { parity_ok=0; fail "#$n: native verdict OPEN_PR but the plan does not list it"; }
done
for n in 7100 7107 7108 7109 7110; do
    grep -q "#$n" <<<"$READY_BODY" || { parity_ok=0; fail "#$n: native verdict allowed but the plan keeps it out of Ready"; }
done
[[ "$parity_ok" -eq 1 ]] && pass "the plan's Ready/listing split matches the native verdicts for all 11 synthetic issues"

# 7c. Rust lockstep — the phrase families, gap class and branch prefix in
# guide.md must be the Oniguruma rendering of the native constants. Skipped
# in consumer repos that do not ship the loom-daemon source tree.
GH_RS="$REPO_ROOT/loom-daemon/src/worktree_ops/gh.rs"
NAMING_RS="$REPO_ROOT/loom-daemon/src/worktree_ops/naming.rs"
if [[ -f "$GH_RS" && -f "$NAMING_RS" ]]; then
    RUST_PHRASE="$(grep -oF '(clos(?:e|es|ed)|fix(?:es|ed)?|resolv(?:e|es|ed))|(part\s+of|contributes\s+to))' "$GH_RS" | head -1 || true)"
    if [[ -n "$RUST_PHRASE" ]]; then
        GUIDE_PHRASE="$(printf '%s' "$RUST_PHRASE" | sed 's/\\s/[[:space:]]/g')"
        if grep -qF "$GUIDE_PHRASE" <<<"$HELPERS_BODY"; then
            pass "guide's linkage phrases are the [[:space:]] rendering of the Rust LINKAGE_PHRASE constant"
        else
            fail "guide's linkage phrase alternation drifted from the Rust LINKAGE_PHRASE constant"
        fi
    else
        fail "could not read the Rust LINKAGE_PHRASE constant from $GH_RS (lockstep broken)"
    fi

    if grep -qF '[*_:\s]*#{issue}' "$GH_RS" && grep -qF '[*_:[:space:]]*#([0-9]+)' <<<"$HELPERS_BODY"; then
        pass "guide's phrase-gap class mirrors the Rust [*_:\s]* gap"
    else
        fail "guide's phrase-gap class no longer mirrors the Rust [*_:\s]* gap"
    fi

    if grep -qF 'BRANCH_PREFIX: &str = "feature/issue-"' "$NAMING_RS" && grep -qF '"^feature/issue-[0-9]+$"' <<<"$HELPERS_BODY"; then
        pass "guide's exact-branch match mirrors worktree_ops::naming::BRANCH_PREFIX"
    else
        fail "guide's branch match drifted from BRANCH_PREFIX (feature/issue-)"
    fi
else
    echo "  SKIP: loom-daemon Rust source not present — skipping the Rust lockstep pins"
fi

# ---------------------------------------------------------------------------
# Test 8: INVENTORY LIMITS — a failed read degrades visibly and fails the
# write flag; a limit-length inventory reports possible truncation.
# ---------------------------------------------------------------------------
echo ""
echo "Test 8: inventory read failure and truncation are visible"

FAIL_RENDER="$(FAIL_INVENTORY=1 LOOM_FIXTURE_DIR="$FIX" GH_READ="$GH_STUB" HELPERS_SRC="$HELPERS_BODY" RPB_SRC="$RPB_BODY" \
    bash -c "$RPB_RUN" 2>/dev/null || true)"
if grep -qF 'Open-PR inventory unreadable — Ready is NOT net of open-PR admission this tick' <<<"$FAIL_RENDER"; then
    pass "a failed inventory read renders the unreadable note visibly"
else
    fail "expected the 'Open-PR inventory unreadable' note on a failed read"
fi
# The gate must work ACROSS the subshell boundary production uses
# (`new_body="$(render_plan_body)"` in update_work_plan, then the check): run the
# real gate text extracted from update_work_plan() against both renders.
GATE_SRC="$(awk '/new_body="\$\(render_plan_body\)"/{f=1} f{print} f&&/^  fi$/&&seen{exit} f&&/inventory-unreadable/{seen=1}' <<<"$UWP_BODY")"
GATE_RUN='eval "$HELPERS_SRC"; eval "$RPB_SRC"; record_work_plan_receipt() { echo "RECEIPT=$1"; }
  gate() { '"$GATE_SRC"'
    echo PASSED_GATE; return 0; }
  gate; echo "RC=$?"'
if [[ -z "$GATE_SRC" ]]; then
    fail "could not extract the inventory gate from update_work_plan() (harness broken)"
else
    GATE_BAD="$(FAIL_INVENTORY=1 LOOM_FIXTURE_DIR="$FIX" GH_READ="$GH_STUB" HELPERS_SRC="$HELPERS_BODY" RPB_SRC="$RPB_BODY" GATE_SRC="$GATE_SRC" \
        bash -c "$GATE_RUN" 2>/dev/null || true)"
    GATE_OK="$(LOOM_FIXTURE_DIR="$FIX" GH_READ="$GH_STUB" HELPERS_SRC="$HELPERS_BODY" RPB_SRC="$RPB_BODY" GATE_SRC="$GATE_SRC" \
        bash -c "$GATE_RUN" 2>/dev/null || true)"
    if grep -qF 'RECEIPT=inventory-unreadable' <<<"$GATE_BAD" && grep -qF 'RC=1' <<<"$GATE_BAD" && ! grep -qF PASSED_GATE <<<"$GATE_BAD"; then
        pass "a failed inventory read is REFUSED by the write gate across the render subshell (inventory-unreadable receipt)"
    else
        fail "expected the gate to refuse a failed read; got: $GATE_BAD"
    fi
    if grep -qF 'PASSED_GATE' <<<"$GATE_OK" && ! grep -qF 'RECEIPT=' <<<"$GATE_OK"; then
        pass "a healthy inventory read passes the write gate (no refusal, no receipt)"
    else
        fail "expected the gate to pass a healthy read; got: $GATE_OK"
    fi
    # Third direction (#14785 review): forge-supplied titles are untrusted text
    # that lands in the rendered region. A HEALTHY read whose ready issue AND
    # open PR are titled with the note's phrase must still pass the gate — the
    # signal is a render-controlled marker, not prose a title can carry.
    TITLE_DIR="$TMPROOT/titlefix"
    mkdir -p "$TITLE_DIR"
    jq '. + [{"number":7190,"title":"Open-PR inventory unreadable on the Gitea path","labels":[{"name":"loom:issue"}]}]' \
        "$FIX/issue-loom_issue.json" > "$TITLE_DIR/issue-loom_issue.json"
    jq '. + [{"number":8190,"title":"Open-PR inventory unreadable — Ready is NOT net of open-PR admission this tick","state":"open","labels":[{"name":"loom:pr"}],"body":"","closingIssuesReferences":[]}]' \
        "$FIX/pr-.json" > "$TITLE_DIR/pr-.json"
    GATE_TITLE="$(LOOM_FIXTURE_DIR="$TITLE_DIR" GH_READ="$GH_STUB" HELPERS_SRC="$HELPERS_BODY" RPB_SRC="$RPB_BODY" GATE_SRC="$GATE_SRC" \
        bash -c "$GATE_RUN" 2>/dev/null || true)"
    if grep -qF 'PASSED_GATE' <<<"$GATE_TITLE" && ! grep -qF 'RECEIPT=' <<<"$GATE_TITLE"; then
        pass "a healthy read passes the gate even when an issue/PR title carries the unreadable-note phrase"
    else
        fail "expected a sentinel-phrase title not to trip the gate; got: $GATE_TITLE"
    fi
fi
FAIL_READY="$(awk '/^## Ready/{flag=1; next} /^## /{flag=0} flag' <<<"$FAIL_RENDER")"
if grep -q '#7102' <<<"$FAIL_READY" && grep -q '#7106' <<<"$FAIL_READY"; then
    pass "a failed read renders Ready UNFILTERED (fail-open, matching the native ProbeFailed contract)"
else
    fail "expected the failed-read render to leave Ready unfiltered"
fi

jq -n '[range(1;201) | {number:., title:"dummy", state:"open", labels:[], body:"", closingIssuesReferences:[]}]' \
    > "$FIX/pr-.json.trunc" 2>/dev/null || true
TRUNC_DIR="$TMPROOT/truncfix"
mkdir -p "$TRUNC_DIR"
cp "$FIX/issue-loom_issue.json" "$TRUNC_DIR/"
cp "$FIX/pr-.json.trunc" "$TRUNC_DIR/pr-.json"
TRUNC_RENDER="$(LOOM_FIXTURE_DIR="$TRUNC_DIR" GH_READ="$GH_STUB" HELPERS_SRC="$HELPERS_BODY" RPB_SRC="$RPB_BODY" bash -c "$RPB_RUN")"
if grep -qF 'Open-PR inventory hit its 200-PR read limit — open-PR linkage below may be incomplete' <<<"$TRUNC_RENDER"; then
    pass "a 200-PR inventory renders the truncation note visibly"
else
    fail "expected the 200-PR read-limit note on a limit-length inventory"
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
