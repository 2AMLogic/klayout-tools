# Loom pin drift inventory

`.loom/resync-ignore` pins installed Loom surfaces so
`.loom/scripts/resync-installed.sh` does not overwrite local customizations.
The pin granularity is the **whole file**: a pin added to protect one section
also blocks every unrelated upstream fix to that file, indefinitely. This page
records what a pin protects, what it is currently missing, and when it can be
dropped. It lives under `docs/` (not `.loom/docs/`) because `.loom/docs/` is
itself resynced from upstream.

Tracking issue: #2501. Separate work: #1966 re-evaluates the curator/judge/
doctor pins (#668). This page does not cover those pins or duplicate that
issue's scope.

## `commands/loom/champion-epic.md`

Checked **2026-10-09** against `rjwalters/loom` @
`4487a6a0114498e9e2d44a24259702593a2f4db0` (upstream `main`; the last commit
touching `defaults/.claude/commands/loom/champion-epic.md` is `dc87ce9e`,
2026-10-01). Local file: `.claude/commands/loom/champion-epic.md` @
klayout-tools `5e5b5599` (816 lines, last changed by `2ded6cb3`, #2500).
Upstream file: 1020 lines.

### Protected local behavior

Pinned since #606 (restored by #607 after resync commit `71d46d09` reverted
it). The local section `## Re-approval Guard and Rejection Idempotency`
provides:

- **Re-approval guard** - stops Step 3 from re-creating Phase 1 issues on an
  already-approved epic (epic #375 had Phase 1 created three times).
- **Rejection idempotency + N=2 escalation** - body-hash verdict marker caps
  unrevised rejection loops by escalating to `loom:operator-only` (#520).
- **Human override detection** - a later human comment (#766) or a bare
  `loom:operator-only` label removal (#774) suppresses re-escalation.

#2500 additionally hand-ported upstream's Step 0 / Step 0a / Step 0.5 into the
pinned copy (#2494).

### Upstream differences observed (2026-10-09)

| Area | Local (pinned) | Upstream @ `4487a6a0` |
|---|---|---|
| Rejection guard | `Re-approval Guard and Rejection Idempotency` | `Idempotency Guard for Unrevised Epics` (#5865), stray-marker tally fix (#8795) |
| Duplicate phase creation | Re-approval guard in Step 1 | `Step 2.75` pre-creation existence check (#6601) + `canonicalize_phase()` (#6967) |
| Human un-park | Comment / label-event override (#766, #774) | `OPERATOR_RULED` (#7921), `BOT_UNESCALATABLE` (#7965) |
| `ALREADY_ROUTED` | `loom:operator-only` only | `loom:operator-only`, `loom:blocked`, or `loom:operator` (#7734) |
| Escalation labels | `--remove-label "loom:epic"` (removes the epic label) | add-only; `loom:epic` preserved (#6715) |
| Step 0 | 0a-0c | 0a-0d (adds 0d behavioral checks) |

Installed guard suites (they ship with unpinned resyncs) give these results:

| Suite (`.loom/scripts/tests/`) | Pinned local file | Upstream file |
|---|---|---|
| `test-champion-epic-verdict-marker-scope.sh` | 13 pass / 6 fail | 22 / 0 |
| `test-champion-epic-phase-marker-normalization.sh` | 21 / 6 | 27 / 0 |
| `test-champion-epic-escalation-respects-human-hold.sh` | 10 / 14 | 27 / 0 |
| `test-epic-label-preserved-on-escalation.sh` | 4 / 3 | 7 / 0 |

These suites are listed in `.loom/scripts/tests/ci-wired.txt`, but no workflow
in this repo runs them, so the local failures are silent.

### Reproduction

```bash
# Upstream file at a fixed commit (or ~/GitHub/loom/defaults/... if checked out)
SHA=4487a6a0114498e9e2d44a24259702593a2f4db0
mkdir -p /tmp/pin-drift
gh api "repos/rjwalters/loom/contents/defaults/.claude/commands/loom/champion-epic.md?ref=$SHA" \
  -H 'Accept: application/vnd.github.raw' > /tmp/pin-drift/upstream.md

# Section and feature-token comparison
L=.claude/commands/loom/champion-epic.md
diff <(grep -n '^##' "$L") <(grep -n '^##' /tmp/pin-drift/upstream.md)
for t in 'Idempotency Guard for Unrevised Epics' 'Step 2.75' canonicalize_phase \
         OPERATOR_RULED BOT_UNESCALATABLE 'remove-label "loom:epic"'; do
  printf '%-40s local=%s upstream=%s\n' "$t" \
    "$(grep -cF -- "$t" "$L")" "$(grep -cF -- "$t" /tmp/pin-drift/upstream.md)"
done

# Guard suites against the pinned file
for t in test-champion-epic-verdict-marker-scope test-champion-epic-phase-marker-normalization \
         test-champion-epic-escalation-respects-human-hold test-epic-label-preserved-on-escalation; do
  bash ".loom/scripts/tests/$t.sh" >/dev/null 2>&1; echo "$t rc=$?"
done

# Same suites against the upstream file, in a scratch copy (never edit the pin in place)
T=/tmp/pin-drift/tree; rm -rf "$T"; mkdir -p "$T/.loom" "$T/.claude/commands"
cp -R .loom/scripts "$T/.loom/"; cp -R .claude/commands/loom "$T/.claude/commands/"
cp /tmp/pin-drift/upstream.md "$T/.claude/commands/loom/champion-epic.md"
for t in test-champion-epic-verdict-marker-scope test-champion-epic-phase-marker-normalization \
         test-champion-epic-escalation-respects-human-hold test-epic-label-preserved-on-escalation; do
  bash "$T/.loom/scripts/tests/$t.sh" >/dev/null 2>&1; echo "$t rc=$?"
done
```

### Conditions to retire the pin

Upstream appears to cover each protected behavior with a different mechanism
(Step 2.75 for duplicate phases, the unrevised-epic guard for N=2 escalation,
`OPERATOR_RULED` for human un-parking). That is a candidate equivalence, not a
verified one. Drop the pin only when all of these hold:

1. A reviewed check confirms upstream prevents duplicate Phase 1 creation on a
   re-approved epic (the #375 case), including Phase-N progression.
2. Upstream caps unrevised rejection loops (the #520 case) and escalates once.
3. Upstream does not re-escalate after a human ruling expressed **either** as a
   comment (#766) **or** as a bare `loom:operator-only` removal with no comment
   (#774).
4. The four suites above pass against the resynced file.
5. Any other local edits to the file (including the #2500 Step 0 port) are
   either present upstream or deliberately abandoned.

Alternatively, keep the local section and retire the whole-file pin once the
upstream section-preservation mechanism below ships.

## Upstream proposal: section-scoped resync (PROPOSED, not shipped)

> **Status: proposed, not filed, not implemented.** No upstream issue exists
> as of 2026-10-09. The curator's attempt to file it from this repo was
> refused by `create-issue.sh`'s write-scope guard (this installation does not
> manage `rjwalters/loom`). It must be filed from an upstream-managed session.
> Nothing below exists in the installed `resync-installed.sh`; do not
> implement it in the installed copy, which a resync would overwrite.

Source: curator handoff comment on #2501 (2026-10-04). Summary:

- **Scope:** add opt-in, named local regions to upstream
  `defaults/scripts/resync-installed.sh`. Whole-file pins stay authoritative.
  Text templates only; no automatic migration of consumer pins. A region has
  a stable identifier and a matching anchor in the upstream template; the
  destination's region bytes are preserved and surrounding content is
  replaced from the source. If the anchor is missing or ambiguous, report a
  conflict and leave the destination unchanged - never guess an insertion
  point.
- **Acceptance criteria:**
  - Documented begin/end marker syntax, identifier, placement contract, and
    precedence relative to whole-file pins.
  - A valid region preserves destination bytes inside its markers while
    surrounding content receives upstream updates.
  - Repeated resync is idempotent; a whole-file pin still skips the file.
  - Unmatched, nested, duplicate, or missing-source anchors leave the
    destination unchanged with an actionable conflict report; no partial
    overwrite.
  - Dry-run reports intended updates/conflicts without writing.
  - Existing resync guards (including local-fix detection) keep protecting
    files; their interaction with regions is documented.
- **Affected upstream files:** `defaults/scripts/resync-installed.sh`,
  `defaults/scripts/tests/test-resync-installed.sh`, plus a focused fixture
  suite if needed.
- **Test plan:** isolated source/destination fixtures; multiple regions;
  malformed/nested/duplicate markers; missing anchors; whole-file pin
  precedence; dry-run; two consecutive runs; existing copy and local-fix
  guard suites.
- **Related:** rjwalters/loom#8920 (stale copies classified as local fixes) is
  a separate defect whose guard interaction needs consideration.

When an upstream issue is filed, replace this section's status note with a
link to it.
