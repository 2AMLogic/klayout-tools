#!/usr/bin/env bash
# test-guide-work-plan-held-approved-reason.sh - Regression test for issue #15325
#
# guide.md's `render_plan_body()` listed every open `loom:pr` PR under
# "Approved (Awaiting Merge)" as "queued for Champion auto-merge", and listed
# the held ones (`loom:blocked` / `loom:operator`) under "Operator Attention"
# as missing "only a human merge decision". Both were false for a PR whose
# park record names a prerequisite (tool-repo #7859: the prerequisite release PR
# must land and the release tag must exist), and the next refresh dropped a
# prerequisite a reviewer had restored by hand.
#
# THE FIX: one shared helper, `approved_hold_suffix()`, prints the row suffix
# for ONE PR object (labels + body):
#   - no hold label            -> nothing (row byte-identical to before; a park
#                                 record on an unheld PR is stale and ignored)
#   - hold, no usable record   -> ` *(held)*`
#   - hold + live park record  -> ` *(held: <reason>)*` (the LAST record's
#                                 reason; else `blocked by [repo]#N`)
# The reason is PR-body free text, so it is flattened to one line, stripped of
# Markdown/HTML metacharacters and capped at 160 characters. Both
# `render_plan_body()` (Approved AND Operator Attention rows) and
# `verify_plan_provenance()` call it, so the gate cannot drift from the render.
#
# Verifies that:
#   1. STRUCTURE: the helper exists, render and verify both call it, and it
#      reads records through `park-record parse --json` (never a hand-grep).
#   2. DETERMINISM: two consecutive renders over identical input are
#      byte-identical.
#   3. SCENARIOS (executed against the REAL extracted functions, stubbed
#      `$GH_READ` + `loom-daemon`): ordinary approved PR unchanged; held
#      approval with a release prerequisite shows the reason under BOTH
#      sections; held PR with no record / a record with no reason / a
#      blocker-only record shows a visible marker with no invented reason; a
#      stale record on an unheld PR is ignored; the LAST record wins; a parse
#      failure degrades to bare `*(held)*`.
#   4. HOSTILE REASON: backticks, `|`, `*`, `_`, `<>`, `[]`, newlines and a
#      300-character string render as ONE bounded line the gate still accepts.
#   5. GATE: `verify_plan_provenance()` accepts the render and rejects a
#      dropped, tampered or invented hold suffix in either section.
#   6. UNCHANGED: closes annotations, the held review row, and the Ready /
#      open-PR classification keep working; the blurbs no longer overclaim.
#
# Hermetic: `gh` and `loom-daemon` are stubbed; only the real `jq` runs.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
# guide.md is shipped (installed at .claude/commands/loom/guide.md), so
# resolve the installed path first, falling back to the defaults/ source tree
# (issue #6194 / #6241).
if [[ -f "$REPO_ROOT/.claude/commands/loom/guide.md" ]]; then
    GUIDE_MD="$REPO_ROOT/.claude/commands/loom/guide.md"
else
    GUIDE_MD="$REPO_ROOT/defaults/.claude/commands/loom/guide.md"
fi

source "$SCRIPT_DIR/lib/assert.sh"

if [[ ! -f "$GUIDE_MD" ]]; then
    echo -e "${RED}FATAL${NC}: guide.md not found at $GUIDE_MD"
    exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
    echo "SKIP: jq not available"
    exit 0
fi

extract_fn() { awk -v re="^$1\\\\(\\\\) \\\\{" '$0 ~ re {f=1} f{print} /^\}/{if(f) exit}' "$GUIDE_MD"; }

RPB_BODY="$(extract_fn render_plan_body)"
HSFX_BODY="$(extract_fn approved_hold_suffix)"
VPP_BODY="$(extract_fn verify_plan_provenance)"
HELPERS_BODY="$(awk -f "$SCRIPT_DIR/lib/guide-plan-helpers.awk" "$GUIDE_MD")" || exit 1

for pair in "render_plan_body:$RPB_BODY" "approved_hold_suffix:$HSFX_BODY" \
            "verify_plan_provenance:$VPP_BODY"; do
    if [[ -z "${pair#*:}" ]]; then
        fail "could not extract ${pair%%:*}() from guide.md"
        echo -e "${RED}FATAL${NC}: cannot continue"
        exit 1
    fi
done
pass "render_plan_body()/approved_hold_suffix()/verify_plan_provenance() extracted from $GUIDE_MD"

# ---------------------------------------------------------------------------
echo ""
echo "Test 1: structure — one shared helper, records via the daemon parser"

if grep -qF 'approved_hold_suffix' <<<"$RPB_BODY" && grep -qF 'approved_hold_suffix' <<<"$VPP_BODY"; then
    pass "render_plan_body() and verify_plan_provenance() both call approved_hold_suffix()"
else
    fail "expected both render and verify to call approved_hold_suffix()"
fi
if grep -qF 'loom-daemon park-record parse --json' <<<"$HSFX_BODY"; then
    pass "the helper reads records through 'park-record parse --json'"
else
    fail "expected the helper to use 'loom-daemon park-record parse --json'"
fi
if grep -v '^ *#' <<<"$HSFX_BODY" | grep -qE 'loom:park|grep|\.title|comments'; then
    fail "the helper must not hand-grep the marker or read titles/comments"
else
    pass "the helper never hand-greps the marker nor reads titles/comments"
fi
if grep -qE 'date |\$\(date|EPOCH|at=' <<<"$(grep -v '^ *#' <<<"$HSFX_BODY")"; then
    fail "the helper must not depend on the clock or on the record's age"
else
    pass "the helper has no clock/age dependency"
fi

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT
FIX="$TMPROOT/fixtures"; mkdir -p "$FIX" "$TMPROOT/bin"

# gh stub: per-label fixtures (`<kind>-<label>.json`), `--state`, `--search
# -label:X` negation, then `--jq` like real gh. `gh api repos/.../issues/N
# --jq .title` serves titles out of the inventory fixture.
cat > "$TMPROOT/gh-stub" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
kind="${1:-}"; shift || true
if [[ "$kind" == api ]]; then
  n="${1##*/}"
  jq -rs --argjson n "$n" 'add | first(.[] | select(.number == $n)).title // empty' \
    "$LOOM_FIXTURE_DIR/pr-.json" "$LOOM_FIXTURE_DIR/issue-loom_issue.json"
  exit 0
fi
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
fixture="$LOOM_FIXTURE_DIR/${kind}-${label//:/_}.json"
if [[ -f "$fixture" ]]; then json="$(cat "$fixture")"; else json='[]'; fi
if [[ -n "$state" ]]; then
  json="$(printf '%s' "$json" | jq --arg s "$state" '[.[] | select((.state // "open") == $s)]')"
fi
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
chmod +x "$TMPROOT/gh-stub"

# loom-daemon stub: `park-record parse --json` answers from a canned record
# chosen by a PARK:* token in the body on stdin (exit 1 + no records when none,
# like the real verb). Label-registry calls fail, so the render takes its
# documented literal fallback.
cat > "$TMPROOT/bin/loom-daemon" <<'STUB'
#!/usr/bin/env bash
if [[ "${1:-}" == park-record && "${2:-}" == parse ]]; then
  body="$(cat)"
  rec() { printf '{"records":[%s],"declared":true}\n' "$1"; }
  case "$body" in
    *PARK:RELEASE*) rec '{"blocker":null,"blocker_repo":null,"at":"2026-11-25T10:00:00Z","reason":"bridge release PR must land and the v0.10.0 tag must exist"}'; exit 0 ;;
    *PARK:BLOCKER*) rec '{"blocker":8322,"blocker_repo":"example-org/tool-repo","at":"2026-11-25T10:00:00Z","reason":null}'; exit 0 ;;
    *PARK:BARE*)    rec '{"blocker":null,"blocker_repo":null,"at":"2026-11-25T10:00:00Z","reason":null}'; exit 0 ;;
    *PARK:MULTI*)   rec '{"blocker":null,"at":"2026-11-24T10:00:00Z","reason":"first reason"},{"blocker":null,"at":"2026-11-25T10:00:00Z","reason":"second reason wins"}'; exit 0 ;;
    *PARK:HOSTILE*) jq -nc '{records:[{blocker:null,at:"2026-11-25T10:00:00Z",
        reason:("needs `v0.10.0` | tag *now* <b>x</b> [link](http://x) __bold__ back\\slash\nsecond\tline " + ("long " * 60))}],declared:true}'; exit 0 ;;
    *PARK:FAIL*)    echo 'garbage that is not json'; exit 2 ;;
    *)              echo '{"records":[],"blockers":[],"withdrawn":[],"declared":false}'; exit 1 ;;
  esac
fi
exit 1
STUB
chmod +x "$TMPROOT/bin/loom-daemon"

# Inventory (the unlabelled `pr list --state open`).
#   9001 ordinary approved PR closing #7001            -> unchanged row
#   9002 held (blocked) approval + release prerequisite, closes #7002
#   9003 held (operator), no park record
#   9004 held, record with only a blocker (repo-qualified)
#   9005 held, hostile reason
#   9006 UNHELD approved PR carrying a stale record    -> row unchanged
#   9007 held, record present but empty (no reason, no blocker)
#   9008 held, two records: the LAST wins
#   9009 held, parser fails (exit 2)
#   9010 review-requested + loom:blocked (held review row)
#   9011 frozen + approved                              -> excluded entirely
cat > "$FIX/pr-.json" <<'JSON'
[
  {"number":9001,"title":"Ordinary approved PR","state":"open","labels":[{"name":"loom:pr"}],"closingIssuesReferences":[{"number":7001}],"body":"Closes #7001"},
  {"number":9002,"title":"Held approval awaiting the release","state":"open","labels":[{"name":"loom:pr"},{"name":"loom:blocked"}],"closingIssuesReferences":[{"number":7002}],"body":"Closes #7002\n<!-- loom:park PARK:RELEASE -->"},
  {"number":9003,"title":"Held with no record","state":"open","labels":[{"name":"loom:pr"},{"name":"loom:operator"}],"closingIssuesReferences":[],"body":"Waiting for the bridge release in the title? no: prose only"},
  {"number":9004,"title":"Held on a blocker only","state":"open","labels":[{"name":"loom:pr"},{"name":"loom:blocked"}],"closingIssuesReferences":[],"body":"PARK:BLOCKER"},
  {"number":9005,"title":"Held with a hostile reason","state":"open","labels":[{"name":"loom:pr"},{"name":"loom:blocked"}],"closingIssuesReferences":[],"body":"PARK:HOSTILE"},
  {"number":9006,"title":"Unheld PR with a stale record","state":"open","labels":[{"name":"loom:pr"}],"closingIssuesReferences":[],"body":"PARK:RELEASE"},
  {"number":9007,"title":"Held, empty record","state":"open","labels":[{"name":"loom:pr"},{"name":"loom:operator"}],"closingIssuesReferences":[],"body":"PARK:BARE"},
  {"number":9008,"title":"Held, two records","state":"open","labels":[{"name":"loom:pr"},{"name":"loom:blocked"}],"closingIssuesReferences":[],"body":"PARK:MULTI"},
  {"number":9009,"title":"Held, parser failure","state":"open","labels":[{"name":"loom:pr"},{"name":"loom:blocked"}],"closingIssuesReferences":[],"body":"PARK:FAIL"},
  {"number":9010,"title":"Held review row","state":"open","labels":[{"name":"loom:review-requested"},{"name":"loom:blocked"}],"closingIssuesReferences":[],"body":"PARK:RELEASE"},
  {"number":9011,"title":"Frozen approved PR","state":"open","labels":[{"name":"loom:pr"},{"name":"loom:frozen"}],"closingIssuesReferences":[],"body":""}
]
JSON
jq -c '[.[] | select(any(.labels[]?.name; . == "loom:pr"))]' "$FIX/pr-.json" > "$FIX/pr-loom_pr.json"
jq -c '[.[] | select(any(.labels[]?.name; . == "loom:review-requested"))]' "$FIX/pr-.json" > "$FIX/pr-loom_review-requested.json"
# Ready: #7001/#7002 are closed by open PRs (excluded), #7100 is plain.
cat > "$FIX/issue-loom_issue.json" <<'JSON'
[
  {"number":7001,"title":"Closed by 9001","labels":[{"name":"loom:issue"}]},
  {"number":7002,"title":"Closed by held 9002","labels":[{"name":"loom:issue"}]},
  {"number":7100,"title":"Plain ready issue","labels":[{"name":"loom:issue"}]}
]
JSON

# One runner file: the extracted functions + a command to run.
{
    printf 'PATH="%s/bin:$PATH"\n' "$TMPROOT"
    printf '%s\n' "$HELPERS_BODY" "$RPB_BODY" "$VPP_BODY"
} > "$TMPROOT/fns.sh"
run() { LOOM_FIXTURE_DIR="$FIX" GH_READ="$TMPROOT/gh-stub" GUIDE_REPO=owner/mine bash -c 'source "$1"; shift; eval "$@"' _ "$TMPROOT/fns.sh" "$@"; }

RENDER1="$(run render_plan_body)"
RENDER2="$(run render_plan_body)"

# ---------------------------------------------------------------------------
echo ""
echo "Test 2: two consecutive renders over the same input are byte-identical"
if [[ -n "$RENDER1" ]] && [[ "$RENDER1" == "$RENDER2" ]]; then
    pass "two renders are byte-identical"
else
    fail "renders differ (or are empty)"
fi

section_of() { awk -v h="## $1" '$0 ~ "^"h {f=1; next} /^## /{f=0} f' <<<"$RENDER1"; }
APPROVED="$(section_of 'Approved')"
OPATT="$(section_of 'Operator Attention')"
REVIEW="$(section_of 'PRs Awaiting Review')"
READY="$(section_of 'Ready')"
OPENPR="$(section_of 'Issues with Open PRs')"

row() { grep -F -- "- **#$2**:" <<<"$1" || true; }
expect_row() { # section-text number expected-row description
    local got; got="$(row "$1" "$2")"
    if [[ "$got" == "$3" ]]; then pass "$4"; else fail "$4 — got '$got', expected '$3'"; fi
}

# ---------------------------------------------------------------------------
echo ""
echo "Test 3: scenarios"
REL=' *(held: bridge release PR must land and the v0.10.0 tag must exist)*'

expect_row "$APPROVED" 9001 '- **#9001**: Ordinary approved PR — closes #7001' \
    "ordinary approved PR: row unchanged"
expect_row "$APPROVED" 9002 "- **#9002**: Held approval awaiting the release — closes #7002$REL" \
    "held approval: Approved row shows the prerequisite, closes annotation kept"
expect_row "$OPATT" 9002 "- **#9002**: Held approval awaiting the release$REL" \
    "held approval: Operator Attention row shows the same prerequisite"
expect_row "$APPROVED" 9003 '- **#9003**: Held with no record *(held)*' \
    "held, no record: visible *(held)*, no reason inferred from prose/title (Approved)"
expect_row "$OPATT" 9003 '- **#9003**: Held with no record *(held)*' \
    "held, no record: visible *(held)* (Operator Attention)"
expect_row "$APPROVED" 9004 '- **#9004**: Held on a blocker only *(held: blocked by example-org/tool-repo#8322)*' \
    "blocker-only record: 'blocked by repo#N' form"
expect_row "$OPATT" 9004 '- **#9004**: Held on a blocker only *(held: blocked by example-org/tool-repo#8322)*' \
    "blocker-only record: same form under Operator Attention"
expect_row "$APPROVED" 9006 '- **#9006**: Unheld PR with a stale record' \
    "unheld PR carrying a record: record ignored, row byte-identical"
if [[ -z "$(row "$OPATT" 9006)" ]]; then pass "unheld PR is not listed under Operator Attention"; else fail "unheld PR leaked into Operator Attention"; fi
expect_row "$APPROVED" 9007 '- **#9007**: Held, empty record *(held)*' \
    "held, record with neither reason nor blocker: bare *(held)*"
expect_row "$APPROVED" 9008 '- **#9008**: Held, two records *(held: second reason wins)*' \
    "two records: the LAST record's reason is used"
expect_row "$APPROVED" 9009 '- **#9009**: Held, parser failure *(held)*' \
    "parser failure (not exit 1) degrades to bare *(held)*"
if [[ -z "$(row "$APPROVED" 9011)$(row "$OPATT" 9011)" ]]; then pass "loom:frozen approved PR stays excluded"; else fail "loom:frozen PR rendered"; fi

# ---------------------------------------------------------------------------
echo ""
echo "Test 4: hostile reason renders as one safe, bounded line"
HOSTILE_ROW="$(row "$APPROVED" 9005)"
HOSTILE_SFX="${HOSTILE_ROW#*Held with a hostile reason}"
if [[ "$(grep -c '^- \*\*#9005\*\*:' <<<"$APPROVED")" -eq 1 && "$(wc -l <<<"$HOSTILE_ROW")" -eq 1 ]]; then
    pass "hostile reason stays on one line (one row, no continuation)"
else
    fail "hostile reason broke the one-row grammar: $HOSTILE_ROW"
fi
INNER="${HOSTILE_SFX# \*(held: }"; INNER="${INNER%)\*}"
clean_prefix="needs 'v0.10.0' / tag now bx/b link(http://x) --bold-- backslash second line"
if [[ "$HOSTILE_SFX" == " *(held: $clean_prefix long "*")*" ]]; then
    pass "the held suffix preserves readable text and sanitizes Markdown and whitespace"
else
    fail "unexpected sanitized hold suffix: $HOSTILE_SFX"
fi
if [[ "${#INNER}" -le 160 ]] && [[ "$INNER" == *"…" ]]; then
    pass "reason capped at 160 characters with an ellipsis (${#INNER})"
else
    fail "reason not bounded/ellipsised (${#INNER}): $INNER"
fi

# ---------------------------------------------------------------------------
echo ""
echo "Test 5: verify_plan_provenance() — accepts the render, rejects tampering"
verify() { run 'verify_plan_provenance "$BODY"' 2>/dev/null; }
vrun() { BODY="$1" LOOM_FIXTURE_DIR="$FIX" GH_READ="$TMPROOT/gh-stub" GUIDE_REPO=owner/mine \
    bash -c 'source "$1"; verify_plan_provenance "$BODY"' _ "$TMPROOT/fns.sh" 2>/dev/null; }
vok() { if vrun "$1"; then pass "accepted: $2"; else fail "rejected: $2"; fi; }
vno() { if vrun "$1"; then fail "accepted: $2"; else pass "rejected: $2"; fi; }

vok "$RENDER1" "the full render (held suffixes, hostile reason, closes annotations)"
vno "$(printf '## Operator Attention: Merge-Risk-Hold Pileup\n%s' "$(row "$APPROVED" 9006)")" \
    "an unheld Approved row copied into Operator Attention, even with a stale park record"
vno "${RENDER1//"$REL"/}" "every held reason dropped (Approved and Operator Attention)"
vno "${RENDER1//bridge release PR must land/bridge release PR may land}" "tampered reason text"
vno "${RENDER1/"- **#9001**: Ordinary approved PR — closes #7001"/"- **#9001**: Ordinary approved PR — closes #7001 *(held)*"}" \
    "a hold suffix invented on an unheld row"
vno "${RENDER1/"- **#9003**: Held with no record *(held)*"/"- **#9003**: Held with no record"}" \
    "a held suffix dropped from an Approved row with no record"
vno "${RENDER1/"- **#9006**: Unheld PR with a stale record"/"- **#9006**: Unheld PR with a stale record *(held: bridge release PR must land and the v0.10.0 tag must exist)*"}" \
    "a stale record's reason invented on an unheld PR"
OPATT_BAD="$(awk '/^## Operator Attention/{f=1} f&&/^- \*\*#9002\*\*:/{sub(/ \*\(held: .*\)\*$/,"")} /^## Very Important/{f=0} {print}' <<<"$RENDER1")"
vno "$OPATT_BAD" "the suffix dropped from the Operator Attention row only"

# ---------------------------------------------------------------------------
echo ""
echo "Test 6: unchanged behaviour and self-description"
expect_row "$REVIEW" 9010 '- **#9010**: Held review row *(held)*' \
    "held review row keeps its existing *(held)* suffix (no park reason there)"
if grep -qF -- '- **#7100**: Plain ready issue' <<<"$READY" \
   && ! grep -qE '#700[12]' <<<"$READY" \
   && grep -qF -- '#7002**: Closed by held 9002 — open PR #9002 (held)' <<<"$OPENPR"; then
    pass "Ready / open-PR classification unchanged (linked issues leave Ready; held stage still shown)"
else
    fail "Ready/open-PR classification changed. Ready: $(tr '\n' '|' <<<"$READY") OpenPR: $(tr '\n' '|' <<<"$OPENPR")"
fi
BLURB_A="$(awk '/^## Approved/{f=1; next} f&&NF{print; exit}' <<<"$RENDER1")"
BLURB_O="$(awk '/^## Operator Attention/{f=1; next} f&&NF{print; exit}' <<<"$RENDER1")"
if grep -qF 'unmarked PRs are queued for Champion auto-merge' <<<"$BLURB_A" && grep -qF 'held …' <<<"$BLURB_A"; then
    pass "Approved blurb no longer claims every PR is queued for auto-merge"
else
    fail "Approved blurb still overclaims: $BLURB_A"
fi
if grep -qF 'or the prerequisite shown' <<<"$BLURB_O" && ! grep -qF 'only a human merge decision' <<<"$BLURB_O"; then
    pass "Operator Attention blurb no longer claims only a merge decision is missing"
else
    fail "Operator Attention blurb still overclaims: $BLURB_O"
fi
APPROVED_COUNT="$(grep -c '^- ' <<<"$APPROVED" || true)"
if grep -qF "| Approved PRs awaiting merge | $APPROVED_COUNT |" <<<"$RENDER1" \
   && grep -qF "| Operator merge-risk holds | $(grep -c '^- ' <<<"$OPATT" || true) |" <<<"$RENDER1"; then
    pass "Backlog Balance counts still agree with the section bullets"
else
    fail "Backlog Balance counts disagree with the sections"
fi

# NEGATIVE CONTROL: the pre-fix row expression has no held marker at all.
PRE="$(jq -r '.[] | select(.number == 9002) | "- **#\(.number)**: \(.title)"' "$FIX/pr-loom_pr.json")"
if [[ "$PRE" == '- **#9002**: Held approval awaiting the release' ]]; then
    pass "negative control: the pre-fix row carried neither hold marker nor prerequisite"
else
    fail "negative control unexpected: $PRE"
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
