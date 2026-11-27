# Loom pin drift inventory

`.loom/resync-ignore` pins installed Loom surfaces so
`.loom/scripts/resync-installed.sh` does not overwrite local customizations.
The pin granularity is the **whole file**: a pin added to protect one section
also blocks every unrelated upstream fix to that file, indefinitely. This page
records what a pin protects, what it is currently missing, and when it can be
dropped. It lives under `docs/` (not `.loom/docs/`) because `.loom/docs/` is
itself resynced from upstream.

Tracking issue: #2501. The curator/judge/doctor pins (#668) were retired by
#1966: their frozen-timeline protection now lives in the pinned
`scripts/claim-staleness.sh`. This page does not cover them.

## Resync on a shared worker

A shared dispatch worker is provisioned once for every sweep it hosts, so a
resync must leave the host alone. The apply step of
`.loom/scripts/resync-installed.sh` does not, unless the apply says so. Stage
the resync in two commands, with `/path/to/loom` a plain clone of the Loom
source repository at the version being moved to:

```bash
# 1) Preview.
./.loom/scripts/resync-installed.sh --dry-run --output /tmp/loom-resync-preview --source /path/to/loom

# 2) Apply. The opt-out goes on this command itself.
LOOM_RESYNC_SKIP_USER_SCOPE=1 ./.loom/scripts/resync-installed.sh --output /tmp/loom-resync-apply --source /path/to/loom
```

Neither `--output` path may exist yet, and the two must differ: each run
creates its own staging worktree at the path it is given, and the script
refuses a path that already exists. A preview removes its worktree before
exiting; an apply keeps it, prints the commands to commit from, and leaves
its removal to you. On a shared host, add the issue number to both paths so a
concurrent sweep's apply cannot occupy them.

**Why the prefix matters even after a clean preview.** After syncing the
repository's installed files, an apply also provisions Loom at user scope
from the source checkout (#10238). The step runs when all of these hold:

- the run is not `--dry-run`;
- `LOOM_RESYNC_SKIP_USER_SCOPE` is not `1`;
- the source checkout is a plain clone, where `.git` is a directory;
- that clone carries `scripts/install/provision-dispatcher.sh` and
  `scripts/install/provision-skills.sh`.

A preview with that same source skips the step at the first condition, so its
output says nothing about it. `--output` is not in the list: it moves only
the repository-side writes into a staging worktree. Without the opt-out, the
apply can still install `~/.local/bin/loom` and, under `~/.claude/`, create
the `commands/loom` and `commands/star.md` links and one
`agents/loom-<role>.md` link per role. The opt-out covers that step, and Loom
documents it in `.loom/docs/machine-dispatcher.md` ("Daemonless machines").

None of that is part of a shared worker's provisioned state, and the
user-scope copies take precedence over each repository's own
`.claude/commands/loom/` and agents. While they exist, every new session on
the host resolves `/loom:*` and `loom-*` through those links instead of the
repository's copies. The first occurrence, and its cleanup, are recorded in
#7984.

**Why this note lives here.** This section and the pointers to it in
`CLAUDE.md` and `AGENTS.md` sit in repository-owned text, which a resync does
not rewrite: it never regenerates either root file (it only deletes a
leftover Loom version header from `CLAUDE.md`), and `docs/` is not a
Loom-installed surface. The installed Builder prompt is resynced, so the same
guidance added there would not survive. That is the rule in its
[Never fix an installed Loom file in place (consumer repos)](../.loom/roles/builder.md#never-fix-an-installed-loom-file-in-place-consumer-repos)
section: upstream the change to Loom, or pin the file. The matching change to
the Builder prompt belongs upstream, not in the installed copy.

## Pin audit 2026-11-23 (#7601)

Keep the four groups of pins at the recorded install, `v0.19.1306`
(`e7af83ea2`). The configured resync checkout also predates the three merged
fixes. To test the next version, we substituted its files into an isolated
copy of klayout-tools at `85b21713e` on macOS. All three fixes are in
[the upstream v0.20.25 tag](https://github.com/2AMLogic/loom/tree/5a5eae1d86f5ef068b8da000fc54f6d8d82a246d).

| Pinned files | What the upstream change covers | Result and retirement condition |
|---|---|---|
| `sweep-lease-fence.sh` and its test | A sweep ID distinguishes concurrent sweeps on the same host. | Local suite passed 113/113 on the pin and 108/113 on the candidate. A fresh lease from another sweep on this host returns 4 (`SUPERSEDED`) upstream. The local contract expects 0 (fail open). Four diagnostic assertions also differed. Reconcile that exit contract before unpinning. [Upstream fence change](https://github.com/2AMLogic/loom/pull/15057) |
| Both `builder-pr.md` copies (commands and roles) | The guidance covers quoted closing keywords in final and partial PR bodies, including inline and fenced examples. | The same files supply the fence call. Upstream passes `LOOM_SWEEP_ID`, while the pins use `RUN_ID` or `LOOM_SWEEP_RUN_ID` to match the publisher. Verify the published ID reaches the fence before retiring both copies. The separate quoted-reference helper passed 10/16 on both baseline and candidate, with the same six missed references. [Upstream quoting change](https://github.com/2AMLogic/loom/pull/15053) |
| `pr-worktree.sh` | The post-worktree hook runs on successful creation and refresh for attached and detached checkouts. | Local hook suite passed 14/14 on the pin and 13/14 on the candidate. The sole failure greps for a call with an explicit path argument. Upstream reads that path inside its helper. The live `uv` invocation passed, as did all 38 local isolation checks. Recheck the behavioral hook tests when the configured install source includes this fix. [Upstream hook change](https://github.com/2AMLogic/loom/pull/15062) |
| `claim-staleness.sh` and its test | The frozen-timeline change is still open upstream. | All 115 local staleness checks passed on the pin. Retest the frozen-timeline cases when the change lands in the install source. [Upstream report](https://github.com/2AMLogic/loom/issues/15044) and [proposed fix](https://github.com/2AMLogic/loom/pull/15049) |

The upstream isolation suite passed 62/64 with its original macOS fixture.
Git reported a path under `/private/tmp`, while the fixture requested the
same checkout under `/tmp`. Two attached refresh checks exited early. Changing
only the fixture's temporary-directory prefix to `/private/tmp` yielded 64/64
with the same upstream script. The local lease-publisher control passed 83/83.
The earlier lease-host reconciliation remains tracked separately in [#6236](https://github.com/2AMLogic/klayout-tools/issues/6236).

## Pin audit 2026-11-17 (#6236)

All five pins made by the 2026-10-14 re-port (`d532f8e94`, 2am#4345) were
re-audited against upstream tag `v0.19.1306` (`e7af83ea2`, the version
`.loom/install-metadata.json` records). Method: the local delta and the
upstream drift were separated by diffing the pinned files against both the
re-port base and `v0.19.1306`, porting every upstream hunk that does not touch
the protected delta. Every file below was **re-ported** (pin kept); none was
unpinned, because each still carries its protected delta. The upstream
reports filed for #6306 are linked with their corresponding pins below.

| Pinned file | Protected delta | Upstream drift found (v0.19.1306) | Disposition |
|---|---|---|---|
| `scripts/pr-worktree.sh` | #2020 [post-worktree hook call](https://github.com/2AMLogic/loom/issues/15045) | #11194 header/help rework; #10120 emitter **removed** upstream (`cfcb24902`) | re-ported: drift in, stale emitter out |
| `scripts/claim-staleness.sh` (+ test) | #1966 [frozen-timeline guard](https://github.com/2AMLogic/loom/issues/15044) | #11947 PR-lease outranks clocks; #13134 marker fallback; stale-minute defaults 30→45 (already present locally) | re-ported; test T16 aligned to #13134 |
| `.claude/commands/loom/builder-pr.md` | #1674 [quoted-keyword guidance](https://github.com/2AMLogic/loom/issues/15046) and #2237 [`--sweep-id` call](https://github.com/2AMLogic/loom/issues/15043) | #13089 DCO rewrite; #12759 conflict stacking; remainder-hold wording; trailer note | re-ported |
| `.loom/roles/builder-pr.md` | same [quoting guidance](https://github.com/2AMLogic/loom/issues/15046) and [sweep identity](https://github.com/2AMLogic/loom/issues/15043) as its commands twin (upstream `defaults/roles/builder-pr.md` is a symlink to the commands file) | same | re-ported (identical to twin) |
| `scripts/sweep-lease-fence.sh` (+ test) | #2237 [`--sweep-id`](https://github.com/2AMLogic/loom/issues/15043) | `LOOM_LEASE_HOST_ID` resolution and more | **not re-ported here** — owned by #6108 / PR #6222 |

Notable findings:

- The pinned `builder-pr.md` still taught `git commit --signoff` under a
  `commit.signoff` knob; upstream's #13089 operator ruling (2026-10-25, no
  role attests a DCO sign-off) had never arrived through the pin. Ported.
- The pinned `pr-worktree.sh` carried the #10120 `live-output-attend` emitter
  that upstream itself has since deleted; the re-port drops it (nothing else
  in this tree referenced it).
- `claim-staleness.sh`'s `_usage` uses an anchored `sed` range instead of
  upstream's fixed line numbers — that is part of the #1966 delta (the local
  header grew), kept.

Verification: `test-claim-staleness.sh` 104/104 (was 102/103 before the T16
alignment), `test-pr-worktree-post-hook.sh` 14/14,
`test-pr-worktree-isolation.sh` 38/38, `test-sweep-lease-publish.sh` 81/81.
After #6222 merged, `test-sweep-lease-fence.sh` passed 110/110 on the updated
audit branch. This completes the #6236 fence drift check with the
`LOOM_LEASE_HOST_ID` fix from #6108.

Retire a pin only after upstream resolves its linked reports with equivalent
behavior and the pin's local verification conditions pass.


## `commands/loom/champion-epic.md`

> **RETIRED 2026-10-15** (2AMLogic/2am#4345, operator ruling Q5 option 1).
> The pin is dropped and the file carries installed upstream (v0.19.1160).
> Re-checked against rjwalters/loom `7e4d69c7a`: conditions 1-3 below are met
> by Step 2.75 (#6601), the unrevised-epic cap (#5865, default 2) and
> `OPERATOR_RULED` (#7921), and condition 4 holds — all four guard suites pass
> on the upstream file and fail on the old pinned one. One accepted change: a
> human comment alone no longer un-parks a held epic; removing the hold label
> is the release, matching the post-#11795 hold taxonomy. The rest of this
> section is kept as the historical record.

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

2am#4345 (2026-10-14): `loom:operator-only` is retired into `loom:operator`
(rjwalters/loom#11795), so the pinned file now escalates to, checks for and
watches removals of `loom:operator`. The 2026-10-09 snapshot below predates
that rename.

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

## `scripts/verify-proposal-refs.sh` (+ its test)

Checked **2026-10-15** against `rjwalters/loom` tag `v0.19.1160`
(2AMLogic/2am#4345 pin audit). The pin's original reason, #1898 (SIGPIPE race
and subshell cache bug, #1863/#1881), is **upstream** (#7736/#7771,
`full_tree_contains`) and no longer justifies it.

### Protected local behavior

#2128 (`d0b770ad`): bare `name.ext:L` citations are verified. A basename with
a known extension is resolved to a unique tracked path and its line range
checked; no match reports MISSING, several matches report AMBIGUOUS. The
known-extension filter keeps prose and host:port strings out. Fixtures 6-9b
and the bare-sibling case in `test-verify-proposal-refs.sh` assert it; 17
local cases fail against upstream's script.

### Conditions to retire the pin

Upstream resolves bare `name.ext:L` citations, with unique-basename
resolution and MISSING/AMBIGUOUS reporting, and the local fixtures pass
against the upstream script.

## `scripts/pr-worktree.sh`

Checked **2026-11-17** against `rjwalters/loom` @
`92556a147b18e43d0763c94f396e1aa68f81a5e2`. Re-ported in #6009: the installed
file now differs from that upstream script only by the local hook helper and
its two call sites. The whole-file pin remains in `.loom/resync-ignore`.

### Protected local behavior

#2020: `run_post_worktree_hook` invokes the repo-owned
`.loom/hooks/post-worktree.sh` after both fresh creation and reuse of a PR
review worktree. The hook runs `uv sync --extra dev` to give the worktree its
own virtual environment and editable install. Without it, tests in a review
worktree can silently import the main checkout's `src/`, hiding regressions
or missing fixes. Hook failure warns without aborting worktree creation.

### Upstream differences observed (2026-11-17)

The previous re-port (`d532f8e94`, upstream v0.19.1160) retained stale header
and help text that redirected issue PRs to `worktree.sh`, contradicting the
Judge's supported fallback. Upstream #11194 (`e9d652800`, tightened by
`3883cf25e`) documents `pr-worktree.sh` as the review checkout for any PR
shape, and says to reuse the named holder or stop after a branch collision.
#6009 takes that text unchanged.

The old pin also retained the #10120 `live-output-attend --pr` block. Upstream
deliberately removed it in
[`cfcb24902`](https://github.com/rjwalters/loom/commit/cfcb24902f2956edf52db51c27f7c06b5ec2349f)
(#12536, 2026-10-16), retiring attended-session telemetry emitters and their
callers. It was removed here too; it was not moved elsewhere.

### Reproduction

```bash
SHA=92556a147b18e43d0763c94f396e1aa68f81a5e2
mkdir -p /tmp/pr-worktree-pin-drift
gh api "repos/rjwalters/loom/contents/defaults/scripts/pr-worktree.sh?ref=$SHA" \
  -H 'Accept: application/vnd.github.raw' > /tmp/pr-worktree-pin-drift/upstream.sh
# Only run_post_worktree_hook and its reuse/fresh-creation calls should differ.
diff -u /tmp/pr-worktree-pin-drift/upstream.sh .loom/scripts/pr-worktree.sh

bash .loom/scripts/pr-worktree.sh --help
bash -n .loom/scripts/pr-worktree.sh
bash .loom/scripts/tests/test-pr-worktree-isolation.sh
bash .loom/scripts/tests/test-pr-worktree-post-hook.sh
```

The isolation suite reported 36/38 before this re-port (the two help checks
failed) and 38/38 afterward. The post-hook suite still passes. These are
targeted local checks; this repo's CI does not run `.loom/scripts/tests`.
The fixture suites require an executable temporary directory and no inherited
`LOOM_WORKTREE_ROOT` override; the post-hook suite's missing-uv case also
requires a test PATH without a real `uv` binary.

### Conditions to retire the pin

Drop the pin when upstream `pr-worktree.sh` invokes an equivalent
post-worktree hook on both the fresh and reuse paths, and the local post-hook
suite passes against the upstream file. [Upstream hook issue](https://github.com/2AMLogic/loom/issues/15045)

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
