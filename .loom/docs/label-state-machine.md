# The `loom:operator` state — "a human is needed"

Loom's coordination substrate is labels (see `CLAUDE.md` § "Label-Based
Workflow") — every pipeline transition (`loom:triage` → `loom:curated` →
`loom:issue` → `loom:building`, `loom:review-requested` → `loom:pr`, etc.) is
a label change on an issue or PR. Before `loom:operator` existed, one state
was the exception: "the engine has stopped and a human is the only way
forward." Champion's merge-risk hold expressed that state as an HTML comment
marker (`<!-- champion:merge-risk-hold -->`) buried inside a PR comment —
invisible to `gh pr list`, the dashboard, or any label-filtered query. See
[#5502](https://github.com/rjwalters/loom/issues/5502) for the incident that
prompted this (four Judge-approved PRs sat held-but-invisible for up to 126
hours).

`loom:operator` moves that state onto the label substrate, where every other
pipeline state already lives.

## Definition

> `loom:operator`: the engine will not work this item further; a human is the
> only transition out.

## The three holds (#11795)

| Label | Question it answers | Does sweep/shepherd skip it? |
|---|---|---|
| `loom:blocked` | Waiting on a dependency, but still automatable once that clears | Yes — a park; released automatically once the blocker resolves |
| `loom:operator` | A human must act or decide; only a human transition takes it out | See the 2am#4345 note below |
| `loom:frozen` | Not now; don't touch (#11743) | Yes — a human removes it |

### How the old labels map

#11795 folded five labels into the three. `loom-daemon labels migrate`
moves open items (closed items keep their labels):

| Retired label | Now |
|---|---|
| `loom:operator-only` | `loom:operator` |
| `loom:operator-decision` | `loom:operator` + a `decision` block |
| `loom:operator-mechanical` | `loom:operator` + a capability marker (a human hold since #11861; relabel it `loom:blocked` to offer it to the capability lane) |
| `loom:operator-blocked` | `loom:operator`; the named blocker stays in the body as a `Blocked by #N` line |
| `loom:needs-capability` | `loom:blocked` + a capability marker |

Three queue labels sit next to the holds and are not holds:
`loom:blocked-unnamed` (the Curator's queue of `loom:blocked` issues naming no
blocker, [`unnamed-block-review.md`](unnamed-block-review.md)),
`loom:decision-malformed` (the Curator's repair queue for a bad decision
block, below) and `loom:sequenced` (PR merge ordering, #11708).

**2am#4345 / klayout-tools pin note.** This pinned file used to define
`loom:operator` as the **re-evaluable** human-needed state ("applying it must
never cause sweep/shepherd dispatch to skip the item"), in contrast to the
terminal `loom:operator-only`. That contrast no longer exists: the retired
sub-kinds collapsed into `loom:operator` (table above), and the installed
`loom-daemon`'s hold set (`loom-daemon labels list --property hold`) parks
`loom:operator` for every role. Upstream kept Champion's PR merge holds
re-evaluable by moving them to `loom:blocked` (rjwalters/loom#11861); this
repo's pinned `champion-pr-merge.md` still applies `loom:operator` for those
holds. Reconciling that is a pin-drift decision, not a label rename, so the
"re-evaluable" statements below describe the pinned design, not the
runtime's current behaviour.

## Entry points

| Role | Trigger | Status |
|---|---|---|
| Champion (PR merge) | Posts a merge-risk hold (`champion:merge-risk-hold`) because a safety axis is red (criterion #2) | **Wired** — `defaults/.claude/commands/loom/champion-pr-merge.md`, "Hold behavior" |
| Champion (PR merge) | Posts a critical-file hold (`champion:critical-file-hold`) because criterion #3 matched a critical-file pattern | **Wired** (#6879) — `defaults/.claude/commands/loom/champion-pr-merge.md`, Safety Criteria → 3 → "Durable hold on FAIL" |
| Champion (issue close) | Holds a merged PR's linked **issue** open because one of its acceptance criteria needs out-of-band verification (live source, real scheduled run, observation over time) and no `loom:ac-verified` marker attests it | **Wired** (#6883) — `defaults/.claude/commands/loom/champion-pr-merge.md`, Step 4 → "Out-of-Band Acceptance-Criteria Gate" |
| Champion (PR merge, klayout-tools-local) | Mirrors a held PR's `loom:operator` (criterion #2 or #3) onto the issue(s) that PR would close, as `loom:blocked` | **Wired** (#1749, klayout-tools-local — see below) — `.claude/commands/loom/champion-pr-merge.md`, "Propagating a hold to linked issues" |
| Curator / Builder / Doctor / Judge | Encounters work that needs a ruling or a human's hands | **Wired** (#11795) — `loom:operator`, per "Routing to a human" below |
| Human | Applies the label directly to any issue or PR | Always available (labels are always human-writable) |

**Scope note**: this first pass (#5502) wired only the Champion merge-risk
hold entry point end-to-end; #6879 added a second Champion entry point
(criterion #3's critical-file hold), and #6883 a third (Step 4's out-of-band
acceptance-criteria hold) — all three reuse the same label and the same overall
shape. #6883 is the first entry point that applies the label to an **issue**
rather than a PR; see "The out-of-band AC hold on an issue (#6883)" under the
exit rule for how the exit rule reads there. `curator.md`, `builder.md`, `doctor.md`, `judge.md`,
`champion.md`, `champion-common.md`, `champion-issue-promo.md`,
`champion-reference.md`, `loom.md`, `sweep.md`, and `watch.md` referenced the
now-retired `loom:operator-only` and/or `loom:blocked` at the time; none of them assume that set
is exhaustive in a way that required editing for this PR, but none of them
have been migrated to *use* `loom:operator` yet either. Extending
`loom:operator` to the Builder/Doctor/Judge entry points above is explicitly
out of scope here — file a follow-up issue per entry point once the Champion
wiring has run in production.

## Exit rule

`loom:operator` is cleared when the artifact the engine judged **materially
changes** — never merely because a role re-read the same artifact and changed
its mind. For the Champion hold, this reuses the *existing* release precheck
(`champion-pr-merge.md`, "Sticky holds" / criterion #2), which already
computes exactly this distinction for the hold marker itself. `loom:operator`
does not add a second, independent state-tracking mechanism — it piggybacks
on the same four precheck outcomes:

| Precheck outcome | `loom:operator` |
|---|---|
| Never held (`PRIOR_HOLD=false`) | Never applied |
| Held, no release signal yet | Stays applied (label add is idempotent — re-asserted, not re-added, each tick the hold stands) |
| Held, released by `loom:auto-merge-ok` override | Removed in the same pass as the reversal comment |
| Held, released by an explicit operator-comment, a new push (head SHA changed), or a new Judge review | Removed in the same pass as the reversal comment |

A human can also clear `loom:operator` directly at any time by removing the
label — the automated exit rule above is the *default* path, not the only
one.

### The out-of-band AC hold on an issue (#6883)

Champion's Step 4 gate applies `loom:operator` to an **issue** whose acceptance
criteria include a step CI structurally cannot perform, when nothing attests it
was performed (`champion-pr-merge.md` → "Out-of-Band Acceptance-Criteria Gate").
The exit rule reads the same way it does for the PR holds — the artifact must
**materially change** — but the artifact here is the *evidence*, not a diff:

| Event | `loom:operator` on the held issue |
|---|---|
| Someone performs the step and posts a comment ending `<!-- loom:ac-verified sha=<head> -->` | Cleared **by that human**, along with closing the issue — the marker records the evidence, it does not itself trigger a re-scan |
| The criterion is reworded because it was never really out-of-band | Cleared by whoever rewords it |
| Nothing happens | Stays applied — correctly. Nobody has done the thing. |

**There is no automated release for this entry point, and that is deliberate,
not an oversight.** The PR that would have re-evaluated it is already merged, so
no Champion tick returns to it; and the condition it encodes ("a human must
observe something in the world") cannot be discharged by any engine re-read. The
hold comment therefore states the exit path explicitly rather than implying a
later pass will notice. `loom:operator`'s defining property still holds — the
issue stays in every normal queue and is never skipped — so a human, a Curator
re-read, or a fresh sweep can all still act on it.

### PR→issue derived propagation: `loom:blocked` (#1749, klayout-tools-local)

The two PR-side holds above (criterion #2's merge-risk hold, criterion #3's
critical-file hold) apply `loom:operator` to the **PR**, not to the issue(s)
that PR would close. Left alone, that is an invisible gap on the issue side:
`loom-daemon`'s work-finder has no signal that the issue is, transitively,
also stuck on a human, so it kept re-offering a dispatch that could only ever
re-derive "sole open PR is held, skip" — a real, confirmed recurrence on
issue #1651 / PR #1659 (three separate no-op sweeps before this fix).

This is a **second, PR→issue-derived** use of the `loom:operator` state —
distinct from the issue-level out-of-band AC hold above (#6883), which
Champion applies directly to an issue it is evaluating. Here Champion instead
*mirrors* a hold that already exists on the PR onto the issue(s) the PR
closes, and does so as `loom:blocked` rather than `loom:operator` itself:
`loom-daemon`'s `PARK_LABELS` already hard-skips `loom:blocked` issues
unconditionally, so reusing it needs zero daemon-side change, whereas
`loom:operator` is explicitly defined (see "The three holds" above) to **never** cause sweep/shepherd to skip the item it is on — applying
it to the issue would have been the wrong label for the desired effect.

| Event | `loom:blocked` on the linked issue |
|---|---|
| PR held (criterion #2 or #3 applies `loom:operator` to the PR) AND the issue was not already `loom:blocked` | Applied, with a `Blocked by #<PR>` comment (the same machine-parseable phrasing `detect-dependency-cycle.sh`'s `parse_dependency_refs()` and `warn-operator-gated.sh` already regex-match) |
| PR held, but the issue already carried `loom:blocked` for an unrelated reason | **Left untouched** — no label churn, no comment, no provenance marker. This propagation never claims an issue it did not itself block, so the later release path cannot misattribute or clobber that unrelated block either |
| PR hold released (criterion #2's `$HOLD_REVERSAL_BLOCK`, or criterion #3's cleared branch) AND this propagation is what applied `loom:blocked` | Removed, with a reversal comment |
| PR hold released, but this propagation never claimed the issue | No-op — the unrelated block reason is undisturbed |
| PR carries no `closingIssuesReferences` | No-op — nothing to propagate |

Ownership (which issue this propagation is allowed to touch on release) is
tracked the same way criterion #3 tracks hold/cleared state — "last matching
marker wins" — via a pair of HTML comment markers on the issue itself
(`champion:pr-hold-block:<PR>` / `champion:pr-hold-unblock:<PR>`), rather than
re-deriving "did we cause this" from label state alone. This mirrors the
"superseding block" caution `curator.md` documents for the *inverse*
direction (an issue's own Dependencies section closing does not mean the
issue's *current* `loom:blocked` justification has cleared, if something else
re-blocked it since) — the shared principle in both directions is: never let
one blocking mechanism assume it owns a label state it did not itself set.

**This is a klayout-tools-local customization, not upstream Loom behavior.**
It was never upstreamed into `rjwalters/loom`'s own `champion-pr-merge.md` /
`label-state-machine.md` templates, so both files are pinned in
`.loom/resync-ignore` (see the `#1749` entry there) — without the pin, an
unpinned `resync-installed.sh` run would silently revert this section and the
implementation it documents, the same reversion pattern documented for
`#606`/`#662`/`#1292`/`#1316`/`#1356`/`#1546`/`#1674`. If upstream Loom ever
adopts an equivalent PR→issue propagation, these pins can be dropped and both
files resynced normally.

Entry/exit implementation: `.claude/commands/loom/champion-pr-merge.md`,
"Propagating a hold to linked issues" (the `propagate_pr_hold()` /
`release_pr_hold()` functions), called from criterion #2's "Hold behavior",
criterion #3's "Durable hold on FAIL", and both holds' release paths.

### The stale-PR route out of `loom:pr` (#5802, narrowed by #6720)

Champion's stale-PR policy swaps `loom:pr` → `loom:changes-requested` on a PR
untouched for 24h, so it reaches Doctor for a rebase. That route now fires from
a **held** state too — before #6720 the sticky-hold bail-out ran first and
dropped the PR from the pass entirely, so the only automated path from `loom:pr`
to Doctor was unreachable for exactly the PRs that most needed it (measured:
21 held PRs, 20 `CONFLICTING`, Doctor's queue empty).

`loom:operator` behaves differently on the two variants, and the split is a
consequence of the exit rule above rather than an exception to it — the question
is only whether the human-needed condition is *resolved* by the departure:

| Stale departure to Doctor | `loom:operator` | Rationale |
|---|---|---|
| No hold in force (#5802) | **Removed** | Nothing outstanding requires a human; the label was never applied in the first place, so the removal is a no-op. |
| Hold in force (#6720) | **Kept** | The hold is unresolved and the PR is expected to return still needing a human merge decision. Clearing it would assert "no human is needed" for the whole Doctor round-trip, which is false. |

Keeping the label on that route is safe precisely because of `loom:operator`'s
defining property — it is re-evaluable and **must never cause an item to be
skipped**. The consumers confirm it: Doctor's `loom:changes-requested` queue
excludes `loom:blocked` and `loom:operator` (the 2am#4345 collapse — see the
note under "The three holds" above); Judge does
not filter on it; sweep C1c's exclusion (#6398) is a *merge*-route skip, which
is what the hold wants; and `verdict-staleness-guard.sh` declines to un-park it
(#5686). Doctor's *Priority 1* queue does exclude `loom:operator` (#5978, so
autonomous work never force-pushes a held PR), but that queue is `loom:pr`-scoped
and the routed PR has just left it.

The `champion:merge-risk-hold` marker comment is **preserved** across this
round-trip. Doctor's rebase is a genuine release *signal* (the diff moved, so the
axes are re-judged), but the *record* of the hold must survive — it is what keeps
`PRIOR_HOLD=true`, and therefore keeps Step 2's hold-reversal comment mandatory
if the PR later merges. A rebase must not launder a held PR into an unheld one.

**#6852 narrows the "Hold in force" row further.** Routing to Doctor on that row
is now conditional on the PR carrying genuine unresolved feedback (a failing
required check) *in addition to* the hold — a **hold-only** stale PR (the hold
is the sole blocker; CI is passing or has no checks) is left on `loom:pr`
instead of being routed, to stop an unproductive "rebase treadmill" where `main`
moving repeatedly forces a rebase that cannot resolve the actual blocker (a
pending human merge decision). `loom:operator` and the hold marker are
unaffected either way — see `champion-pr-merge.md` → "Held-PR Health Pass" →
"#5 under a hold" for the exact routing decision, and "Hold-only Stale PR —
suspend the route" for the suspended path.

## Current implementation

Two Champion entry/exit pairs are wired today, both in the same file:

- **Criterion #2 (merge-risk hold)**:
  - **Entry** — `defaults/.claude/commands/loom/champion-pr-merge.md`, criterion
    #2's "Hold behavior" block (`gh pr edit ... --add-label loom:operator`,
    posted alongside the `champion:merge-risk-hold` marker).
  - **Exit** — the same file's Step 2 ("Add Pre-Merge Comment"), gated on the
    non-empty `$HOLD_REVERSAL_BLOCK` built by the release precheck (`gh pr edit
    ... --remove-label loom:operator`, posted alongside the
    `champion:merge-risk-hold-cleared` marker).
  - **Conditional exit** — the same file's "PR Rejection Workflow → Stale PR"
    block, which removes the label only when no hold is in force
    (`MERGE_BLOCKED_BY_HOLD != true`) and keeps it otherwise; see "The stale-PR
    route out of `loom:pr`" above (#5802 / #6720).
  - Reuses the release precheck at `champion-pr-merge.md` ("Sticky holds — a
    hold does NOT clear on a re-read alone") rather than re-deriving release
    state independently.
- **Criterion #3 (critical-file hold, #6879)**:
  - **Entry/exit** — `defaults/.claude/commands/loom/champion-pr-merge.md`,
    Safety Criteria → 3 → "Durable hold on FAIL" block, run immediately after
    criterion #3's check-loop. `gh pr edit ... --add-label loom:operator`
    alongside the `champion:critical-file-hold` marker on FAIL;
    `gh pr edit ... --remove-label loom:operator` alongside the
    `champion:critical-file-hold-cleared` marker the first time a later push
    no longer matches any critical-file pattern.
  - Needs **no** sticky-hold precheck: criterion #3's check-loop is a
    deterministic file-pattern match, not a judgment call, so there is no
    "same diff scores differently on a later read" case to guard against —
    the FAIL/PASS verdict itself, recomputed fresh every tick, is the release
    signal.
- **PR→issue propagation (`loom:blocked`, #1749, klayout-tools-local)**:
  - **Entry/exit** — `.claude/commands/loom/champion-pr-merge.md`,
    "Propagating a hold to linked issues" (`propagate_pr_hold()` /
    `release_pr_hold()`), called from both criterion #2's "Hold behavior" and
    criterion #3's "Durable hold on FAIL" (entry), and from both criteria's
    release paths (exit).
  - See "PR→issue derived propagation: `loom:blocked`" above for the full
    entry/exit table and the ownership-marker mechanism.

**One consumer honors the hold without ever setting it (#5686)**: the
stale-verdict machinery (`defaults/scripts/verdict-staleness-guard.sh` and
`loom-daemon`'s `reconcile_pr_verdicts`) clears a review verdict whose head SHA
has moved — but **not** on a PR carrying `loom:operator` or `loom:blocked`. Re-queueing such a PR for review would silently un-park it,
which is precisely the transition only a human may make. It still reports the
verdict as stale, so the PR is not merged either; it simply stays exactly where
the operator left it.

<!-- 2am#4345: the `loom:operator-only` sub-kind, `loom:needs-capability`
     and bidirectional-routing sections that stood here described labels
     retired by rjwalters/loom#11795. They are replaced by upstream's current
     "Routing to a human" and "The kind comes from the body" sections,
     copied verbatim; the klayout-tools-local #1749 sections above are
     unchanged. -->

## Routing to a human (#10001, #11795)

This is the fleet's one statement of the rule; every role points here.

**Park only for a PO-level decision or a human-hands step.** No hold is a
default for "hard", "uncertain", "two attempts failed" or "I'd rather not":
if an agent could do or decide it, do it, or leave it in the normal queue.
Before applying any hold, the comment says what an agent tried or why it
structurally cannot.

| The situation | Apply |
|---|---|
| A ruling only a human may make: a preference call, or an authority act (binds the entity, irreversible disclosure, spending, credentials only the operator holds, accepting risk on the entity's behalf, physical-world action) | `loom-daemon operator-decision apply <N> --input d.json`: writes the `decision` block (2-4 ranked options, each with a why) and adds `loom:operator` in one write |
| A step needing a human's hands: hardware, a credential or access no fleet host holds | `loom:operator`, plus a comment naming the exact step |
| A human action that waits on something named first | `loom:operator`, plus a literal `Blocked by #N` line in the body or comment |
| Waiting on something automation will resolve: an issue, a PR, infrastructure | `loom:blocked` with a park record (`loom-daemon park-record apply`, [`park-record.md`](park-record.md)), or at least a literal `Blocked by #N` / `Depends on #N` / `Requires #N` line in the same comment (the phrasings `detect-dependency-cycle.sh` and `warn-operator-gated.sh` parse; a backtick-quoted reference does not count) |
| Waiting on a capability a fleet host may hold (host or admin access, a credential profile) | `loom:blocked` with a reason-only park record and a `<!-- loom:capability=<name> -->` marker naming it, plus a linked capability-request issue: the capability lane (below) proposes it on a host that holds it |

**One hold per decision.** The operator's ruling (a dashboard vote, or a
human removing the label) lifts `loom:operator`, so never add an unrelated
`loom:operator` hold to an item that already carries a `decision` block: the
ruling would lift both.

**Unsure which applies means the analysis is incomplete, not that a hold is
safe (#5826).** A decision is never a safe default: run the falsifiability
test below and name the axis two well-informed people would still disagree
on, showing it is a preference, not a fact. If you cannot name it, the item is
determined: finish it. A missing objective is a decision whose options are the
candidate objectives (#10000). An issue's own filing is never operator
approval (#7855, below).

**Composes with the work-finder.** `loom:issue` comes off when a hold goes on
(the atomic transition in `CLAUDE.md` § "Label Discipline"); a human who
removes `loom:operator` from an issue re-adds `loom:issue` to release it.

**Notifier contract.** Loom signals a human ask with labels only (#11087):
no mail, no inbox or chat post, so a notifier failure can never reach a
role's tick. The registry property `human_gated`
(`loom-daemon labels list --property human_gated`) is the authoritative set of
labels that mean "a human must act": `loom:operator` and `external`
(maintainer approval). Not in it: `loom:blocked` (self-clearing),
`loom:frozen` (nothing is pending), `loom:decision-malformed` (Curator
repairs it), and the curation and claim labels. An external notifier (the 2am
`github-events` worker) reads that property instead of hard-coding names,
turns a human-gated label being added into a notification, and resolves it
when the label is removed or the item closes or merges. Pinned by
`label_registry::tests::human_gated_is_exactly_the_human_must_act_labels`.

## The kind comes from the body (#11795)

`loom:operator` used to be refined by sub-kind labels applied beside a base
label (#5671); 137 of 533 open `loom:operator-only` items already carried a
`decision` block, so the label was a second, drifting copy of what the body
said. Now the body is the only copy:

| Body carries | Kind | Who reads it |
|---|---|---|
| A ```` ```decision ```` block (`operator-decision apply`) | A ruling | The operator UI's decision queue; telemetry `hold_kind` keeps an `operator_decision` stage by reading the block |
| A `Blocked by #N` / `Depends on #N` / `Requires #N` line (alone or with a decision block) | The human action waits on something named first | Humans (Curator's premise re-check is retired, #11861) |
| Neither | A human must act; the comment says what | Humans |

No role acts on any of these (#11861): the kind tells the human what to do.
A capability marker on a `loom:operator` item is inert; the capability lane
reads `loom:blocked` only (below).

### Capability marker (`<!-- loom:capability=<name> -->`, #6885/#6892)

The marker states, machine-readably, what capability an item needs. On a
`loom:blocked` item whose only blocker is that capability (no park record
naming a blocker, no decision block) it makes the item one the capability lane
may propose on a host that holds it (#11861; the old `loom:needs-capability`).
On a `loom:operator` item it is inert: no role acts on that label.

**Marker syntax.** Mirrors the `<!-- loom:complexity=<tier> -->` convention
(see `defaults/.claude/commands/loom/curator.md` → "Complexity routing
marker") exactly in form: an HTML comment in the issue/PR body, invisible in
rendered Markdown, trivially greppable. One marker per required capability,
each on its own line:

```html
<!-- loom:capability=host-sudo -->
<!-- loom:capability=cloud-profile:prod-aws -->
```

An item may carry **more than one** capability marker. Multiple markers are
**ANDed** — a worker must hold every declared capability, not just one of
them, before the item is dispatchable. A `loom:blocked` item with no marker
declares no capability and is never lane-eligible.

**Closed vocabulary (small, extensible, fail-closed).**

| Value | Meaning |
|---|---|
| `host-sudo` | Needs root/administrator access on the execution host |
| `forge-admin-token` | Needs a GitHub/Gitea token with admin (not just repo-write) scope |
| `cloud-profile:<name>` | Needs a named cloud credential profile, e.g. `cloud-profile:prod-aws` |
| `tailnet-access` | Needs access to the private tailnet/VPN |

The vocabulary is deliberately small — extend it by adding a row here (and
the matching entry in `defaults/scripts/extract-capability-markers.sh`'s
`KNOWN_LITERALS`/`KNOWN_PREFIXES`), not by any item inventing its own value.
**An unrecognized or misspelled value MUST fail closed**: treated identically
to "no capability declared" (the item stays held), never silently
ignored or treated as satisfied. This applies symmetrically — an item
declaring a typo'd value is exactly as undispatchable as one declaring
nothing, and a worker's own declared holds are checked against the same
closed vocabulary.

**Parser convention.** Anchor to the full `<!-- loom:capability=<value> -->`
comment form, never a bare substring — the same anchoring the retired
complexity-marker gate used, and for the same reason (#4840): prose that merely *quotes* the marker syntax as literal
example text (exactly as this section does above) must never be mistaken for
a live marker. The value grammar is `[a-z0-9][a-z0-9:_-]*`. A
capability-bearing item collects **every** matching marker, deduplicated.
The whitespace immediately before the closing `-->` is optional, and both
reference parsers anchor extraction on the closing delimiter itself (#6914).

The reference implementation is `defaults/scripts/extract-capability-markers.sh`
(tests: `defaults/scripts/tests/test-extract-capability-markers.sh`). The
daemon (`loom-daemon/src/capability.rs`) and sweep parse a body identically
to it, so the two surfaces cannot diverge on what counts as a valid marker.

**Alerters never write a marker.** An alert issue or a Champion merge-risk PR
must never become lane-eligible, and the lane requires the marker.

### Dispatch path (#6893) — propose-only, opt-in per host, fail-closed

Two surfaces read the marker and may route a capability-only `loom:blocked`
item somewhere other than the park (#11861; before, the lane read
`loom:operator`, which no role acts on any more):

| Surface | Where |
|---|---|
| Daemon (Tier 2 dispatch) | `loom-daemon/src/capability.rs` + `WorkItem::is_skipped_with_capabilities` / the `SweepRegistry` step-2.7 park guard |
| Sweep (`all` sentinel + Mode C C0) | `defaults/.claude/commands/loom/sweep.md` → "Capability-aware lane" |

Both apply the **same four gates**, in order, and any failure means "skipped
exactly as before":

1. **The host declares capabilities.** `LOOM_WORKER_CAPABILITIES` (a comma- or
   whitespace-separated list of closed-vocabulary values) is set in the
   *environment*. Unset — the default everywhere — makes the whole path inert.
   It is deliberately **not** readable from `.loom/config.json`: a capability is
   a property of the machine and its credentials, and a file committed to git
   must not be able to assert that the host running it has root.
2. **The item is the capability-only shape**: `loom:blocked` (or the retired
   `loom:needs-capability`), **no** park record naming a blocker, **no**
   `decision` block in the body, and **none** of `loom:operator` or
   `loom:frozen`.
3. **The body declares ≥1 marker and every declared value is recognized** (the
   fail-closed rule above).
4. **The host holds every declared capability** (markers are ANDed).

**What the lane may then do is produce a proposal — never a live credentialed
action.** The worker emits the exact commands (or a PR) for an operator to
approve and stops; the item keeps `loom:blocked` and is not closed. A
live-execution mode is a separate, explicit opt-in that was deliberately not
built.

**When a declared capability is not held**, the item still parks — but the
worker leaves a comment naming the missing capability, turning a silent stall
into a capability request.

**When anything encountered turns out to require a judgement call** rather than
mechanical execution, the worker hard-stops and files a ranked decision
(`operator-decision apply`), which makes the stop durable: gate 2 refuses an
item with a decision block on every later pass.

A `loom:blocked` item that also names a blocker in a park record is **not**
lane-eligible: it waits on another change, which no capability satisfies.
Nor does the stale-blocked release pass lift a capability-only park (it has no
named blocker to resolve): it stays parked until a capable host proposes it or
a human releases it, and the linked capability-request issue says which host
setting would unblock it.

### The falsifiability test, before filing a decision (#5826)

"Requires judgement" does not, by itself, identify work only a human can do —
an agent can research, weigh trade-offs, and rule, given grounding. Before
filing a decision, classify the item along a three-way split instead of
asking "how hard is this call":

| Kind | Definition | Correct response |
|---|---|---|
| **Determined** | The answer follows from physics/constraints/prior art once the analysis is finished — nobody has derived it yet | Derive it. This was never a decision — keep working. |
| **Underdetermined** | Multiple defensible answers survive *full* analysis because the objective function is contested | A decision: the options are the candidate objectives, or the sides of the preference/authority call (#10000) |
| **Authority** | Orthogonal to the above — the act requires authority an agent structurally cannot hold, however determined the answer is (see "Routing to a human") | A decision, or a mechanical `loom:operator` ask, whichever fits |

**The test** — what makes "underdetermined" checkable instead of a vibe:
name the axis along which two well-informed people would still disagree, and
show that axis is a preference, not a fact. If the axis cannot be named, the
item is **not** underdetermined — it is an incomplete analysis wearing a
judgement call as a disguise. Keep working; don't park it. The options' whys
in the decision block carry the axis.

**A missing capability is not operator-by-right.** A block that is really
"automation could do this once a tool/agent capability exists" is
`loom:blocked` + capability marker, linked (`Depends on #N`) to a filed or
reused capability-request issue in the repo that owns the capability, with a
comment on that issue naming what it unblocks (#5817, #5818). An existing
`loom:operator` item of that shape is a human's to re-park (#11861: no role
acts on `loom:operator`); the human can run, in one write:

```bash
loom-daemon park-record apply --issue <number> --blocked-by <capability-request> \
  --by human --remove-label loom:operator
```

**Where this is wired** — every role that can apply the label:

| Role | Site | What it applies |
|---|---|---|
| Champion | Unrevised-proposal bound (`champion-issue-promo.md` Step 4), reached only after the Curator revision loop (#10753) | A ranked decision via `loom-daemon operator-decision apply`; never a bare hold. **Exception, no label applied (#7657)**: when every recurring finding is `premise-false` (a cited path/line-range/repo-state claim, re-verified false on current `main`), Champion closes the proposal (`<!-- champion:premise-false-closed:<main-sha> -->`, `gh issue close --reason "not planned"`) instead of escalating — no hold label, since nothing is routed to a human. |
| Champion | Epic-complete-unpromoted escalation (`champion-common.md`) | `loom:operator` with a `Blocked by #N` line when the recurring finding is itself a live, open dependency; a decision otherwise |
| Champion | Dependency-cycle detector (`detect-dependency-cycle.sh`), capped-PR close recommendation (`champion-pr-merge.md`) | A decision ("breaking a cycle is a human decision" / "the approach itself is not viable") |
| Curator, Builder, Judge, Doctor | "Routing to a human" above, which each role prompt points to | Caller's choice; a human-hands `loom:operator` is the typical Doctor case |

`defaults/scripts/tests/test-operator-hold-routing.sh` fails CI if a role
prompt, doc or script applies a retired hold label, or if a role stops
pointing at "Routing to a human".

### An issue's own autonomous filing is not operator approval (#7855, #8309)

Filing an issue — by any role, including one footed "🤖 Generated with Claude
Code" — is not itself an operator ruling, even when the issue's substance is
a proposal to reverse a documented design decision, safety posture, or a
test's asserted intent. That reversal still needs a decision filed with
`operator-decision apply`; the filing agent's own presence on the issue is
never a substitute for that routing.

**Named anti-pattern**: on #7855, a Curator pass reasoned "the operator
filing it is the ruling that reverses it" about an issue — itself filed
autonomously — proposing to drop the deliberate no-auto-restart posture for a
wedged-but-alive daemon (`loom-daemon/src/watchdog/help.txt` and
`loom-daemon/src/watchdog/mod.rs`'s "no automatic kill/restart" rationale,
asserted by `defaults/scripts/tests/test-loom-daemon-watchdog.sh`'s Test 14).
Champion caught the reversal at the promotion gate, one stage after Curator
had already spent a pass enriching and scoping it on that premise. Do not
restate this reasoning — an issue's own filing, autonomous or not, carries no
approval weight over a documented decision.

This is narrow: it flags proposed *reversals* of existing, documented/tested
behaviour, not autonomously-filed issues in general (see CLAUDE.md § "Issues
Are Suggestions").

**Since #8396 this rule has a mechanism, not only a prose statement.**
`./.loom/scripts/premise-check.sh --issue N` (`loom-daemon premise-check`)
runs **before** Curator enrichment — from the sweep orchestrator's Curator
phase, and from `curator.md` § "Before Starting Curation" on every other
path. For a scoped population (`loom:architect`/`loom:hermit`/`loom:auditor`,
an incident-report heading, or an issue whose own text claims a reversal) it
requires a recorded premise check, and it **refuses** the one combination this
section names as the anti-pattern: a record saying `deliberate=yes
reversal=yes` is admissible only with `verdict=operator-decision`, which is
this exact routing. Exit `11` means "file a decision (`loom:operator` + a
`decision` block) naming the disagreement axis". Contract and design
rationale: [`premise-gate.md`](premise-gate.md).

**`loom:decision-malformed` (#10057)**: the operator UI bounces a
`loom:operator` decision whose `decision` block is missing or invalid into
this label. Curator repairs the block (`operator-decision apply` clears the
bounce label) or, if no operator call exists, removes it and re-routes; a
repeat bounce is left alone. It is a work-finder skip label (#11480), so a
bounced Decision issue is never build work. No daemon release path (claim
release, quarantine release, no-op hold lift, reclaim) re-adds `loom:issue`
beside it or beside any other skip or lane-veto label (#11480's shared
`restore_veto`).

**Champion's revise verdict (#10753, #11860)**: a `NEEDS REVISION` verdict
removes `loom:curated`, adds `loom:triage` and carries a
`<!-- champion:revise:body-<HASH> -->` marker, so Curator curates the issue a
second time against the findings and ends it at `loom:curated` again. Every
Champion evaluation is terminal: promote, revise, block (`loom:blocked` + park
record) or escalate (`loom:operator`). After at most two rounds plus one final
round, Champion files a ranked decision with `operator-decision apply` when a
preference or authority question is named (never a bare `loom:operator`
hold); factual findings get one Curator disposition round first.
`loom:needs-revision`, the label this used to set, is retired onto
`loom:triage` (`labels migrate` also strips `loom:curated`). Contract and
bound: [`promotion-throughput.md`](promotion-throughput.md).

## Follow-up work

- Derive `loom:blocked-unnamed` from "blocked without a park record" instead
  of labelling it; that needs a new demand trigger for the Curator queue, so
  #11795 kept the label.
