# Issue ↔ PR Linking

How a Loom-opened PR declares which issue it implements, and why every PR in a
multi-PR landing needs its own declaration. The operative rules live in
`builder-pr.md` → "Multi-PR landings"; this file is the rationale, the
incidents, and the parser contract behind them.

## Why a second, machine-readable link exists

GitHub's `closedByPullRequestsReferences` / `closingIssuesReferences` is the
only structured issue↔PR link the forge gives us, and it exists **only** for a
PR that carries a closing keyword. That makes it structurally blind to every
non-final PR of a multi-PR landing, which is not a rare shape:

- Measured over 329 landed issues (2026-08-15..09-29, issue #9465), **19 of
  329** sweep-landing PRs were absent from that connection entirely.
- **20** issues landed through more than one merged PR on a reused
  `feature/issue-N` branch; one (`example-org/hardware-repo#237`) landed
  through **47**, whose lifecycle churn was up to 835× the final PR's diff.

So "work delivered for issue N" is either under-counted (final PR only) or has
to be recovered by scraping branch names — which is not a link, only a
correlation. The `Loom-Issue: owner/repo#N` trailer is the link: fully
qualified, greppable, and independent of both GitHub's keyword parser and of
matching a prose phrase.

It is deliberately **additive**. It does not replace `Closes #N` (GitHub's
auto-close is still how an issue gets closed) or `Part of #N` (which is what
`merge-pr.sh` parses to fire the #3667 `loom:building` → `loom:issue` reset).

## The trailer contract

```
Loom-Issue: 2AMLogic/loom#9465
```

- **Owner/repo is required.** A bare `Loom-Issue: #9465` does not parse. The
  whole point is that a consumer reading a telemetry record or a cross-repo
  PR list can resolve the issue without already knowing which repo the PR came
  from — the storyline rollups that motivated #9465 join across repos.
- **Line-leading**, optionally behind a list marker or blockquote, exactly like
  `Part of #N`. A mid-sentence mention is a mention, not a declaration.
- **Emphasis is fine.** `**`, `__`, `*` or `_` emphasis around a line-leading
  `Part of #N` / `Contributes to #N` is accepted (also behind a list marker or
  blockquote). This applies to the keyword form, not `Loom-Issue:`. Backticks
  remain excluded.
- **Plain text, never inside backticks.** See "The backtick pitfall" below.
- Repeatable: a PR may carry more than one trailer (deduped by the parser).

Parser: `loom-daemon merge-pr-refs loom-issue-trailer-refs` (body on stdin,
one `owner/repo#N` per line, deduped and sorted).
`loom-daemon merge-pr-refs loom-issue-trailer-warnings --pr N` reports lines
that *look* like a trailer but will not parse — a backticked one, or one
missing the `owner/repo` slug. Both live in
[`loom-daemon/src/merge_pr/refs.rs`](https://github.com/2AMLogic/loom/blob/main/loom-daemon/src/merge_pr/refs.rs).

## Why not `Refs #N`

Issue #9465 asked for `Refs #N` as the non-closing reference. It is **not**
adopted: nothing in Loom parses it. `merge-pr.sh`'s partial-increment guard
matches only `Part of` / `Contributes to`, and that match is what fires the
#3667 label reset on merge. A PR that wrote `Refs #N` instead would look
correct to a reviewer while the reset silently no-opped and the issue sat at
`loom:building` — the exact failure shape of the #5686/#5240 incident below.
One non-closing keyword set, already parsed, plus the new trailer for the
machine-readable half.

## The backtick pitfall (#5234, #8796)

`partial_increment_refs()` blanks inline code spans before matching, on
purpose: #5234 saw a bare `grep` read a backticked, mid-sentence, conditional
mention (``…I will switch the reference to `Part of #4574` ``) as a declared
intent and reopen an issue that had been correctly closed.

The cost of that correct exclusion is that a Builder who writes the
convention's literal syntax — which *reads* like syntax, so it *looks* like it
belongs in backticks — produces a PR that is right to every human reviewer and
invisible to the automation:

```markdown
Part of #123              <- declaration: parsed, the #3667 reset fires on merge
`Part of #123`            <- code span: NOT a declaration, reset silently skipped
```

Merging PR #5686 into #5240 (rjwalters/kicad-tools) did exactly this. Nothing
logged the skip, so #5240 was stranded at `loom:building` until a stale-claim
pass reclaimed it. `merge-pr.sh` now warns (non-blocking) on a whole-line
backticked trailer, but that warning reaches only whoever runs the merge.

The `Loom-Issue:` trailer inherits the exclusion, and has its own detector —
`loom-daemon merge-pr-refs loom-issue-trailer-warnings --pr N` — which
`merge-pr.sh` runs in the same pre-merge pass (#9502): a backticked or
slug-less trailer gets the same non-blocking warning (`[dry-run] `-prefixed
under `--dry-run`), and a daemon too old to know the verb reports a skipped
advisory check naming the version to roll to — never a refused merge.

## A stray closing keyword anywhere in the body defeats `Part of #N` (#4569)

`Part of #N` is not a shield. GitHub does not weigh the two references against
each other — **one** closing keyword adjacent to `#N` anywhere in the body
closes the issue on merge, however explicitly the rest of the body says
otherwise. Observed, not hypothetical: a partial-increment PR ended with the
deliberate trailer `Contributes to #2` plus an operator-handoff section reading

```markdown
## Operator follow-up (after merge)

3. Verify `npm view censusapi` resolves to `0.0.1`, then close #2.   ← closes #2 on merge!
```

GitHub honored that `close #2`. The issue closed on merge and had to be
reopened by hand. (The head branch was `feature/issue-2`, but branch naming was
*not* the cause — `feature/issue-N` creates no Development-sidebar link.)

The remedies are in `builder-pr.md`: never put a closing keyword immediately
before the tracked issue's number (an intervening word breaks the link —
`then close issue #2`), scan the whole body including checklists and
operator-handoff sections, and scan every commit message on the branch too
(#4595 — for consistency: GitHub parses only the PR body and the merge
commit's subject, #9105). `merge-pr.sh` detects the contradiction pre-merge and
reopens the issue if GitHub closed it anyway, but that leaves a close/reopen
flicker plus notification churn — it is a backstop, not the plan.

## A quoted example can close a different issue on a final PR (#15046)

The #4569 rules, and the scan that backed them, were scoped to the tracked issue
of a non-closing PR: the scan substituted only that issue's number, so on a final
PR that closes issue A it said nothing about an example in the body naming issue
B — and the whole section was framed as a rule for `Part of #N` bodies, which a
Builder on a final PR had no reason to run.

The final-PR shape came from a downstream report (#15046; observed by the
reporter, not reproduced in this repo): a final PR that quoted a closing
reference in an inline code span had the quoted keyword and number read as a
closing reference. **Measured since (#15058), GitHub's `closingIssuesReferences`
ignored every inline-code-span closer it was possible to isolate** — the report
is not reproducible on today's parser; whatever closed that issue, the backticks
were not it. The rewrite rule is kept for code spans regardless: one word of
cost, and no dependence on GitHub's parser staying put. The Guide's docs-only PR
is the same failure in another template (#14176): `### Changes` prose of the
shape `drops closed #N` registered as a closing reference —
`closingIssuesReferences` returned the unrelated issue before the wording was
changed.

The remedy stays in `builder-pr.md` → "Multi-PR landings": break the adjacency in
every quoted example (an intervening word, `Closes issue #N`, or a nonnumeric
placeholder) on every surface — inline code, fenced blocks, checklists, test plans,
commit subjects and bodies — and scan the body and the branch's commit messages
for a closing keyword adjacent to **any** issue number, not only the tracked one,
including the full-URL form since #15058. A final PR's scan may print only issues
it delivers; a partial PR's must print nothing.

## Measured GitHub closing-parser behavior (#15058)

Every surface below was settled from attributable existing PRs — real bodies,
real `closingIssuesReferences` readouts, real close events — plus a live
placement fixture carried by #15058's own PR body, PR #15089 (open, base
`main`): five adjacencies targeting already-closed issues plus the real control,
read back as `closingIssuesReferences = [4569, 14176, 15058]` — blockquote and
URL fixtures present, fenced and inline-code fixtures absent, control present.
No disposable repository, no merge experiments on active issues.

| Placement in a PR body (base = default branch) | `closingIssuesReferences` | Evidence |
|---|---|---|
| Plain prose, any position — mid-sentence, checklist, test plan | **counts** | #4569 (observed: `then close #2` in an operator-handoff list closed #2 on merge); #14176 (measured: `drops closed #N` prose listed the unrelated issue) |
| Blockquoted line (`> … closes #N …`) | **counts** | PR #12122: body's only keyword-adjacent `#10444` sat inside a blockquote; refs = `[10444, 11613, 12039]` alongside the two plain-text closers |
| Full issue URL (`Closes https://github.com/OWNER/REPO/issues/N`) | **counts, and closes on merge** | 2AMLogic/2am PR #4161: refs = `[4160]`, issue #4160 closed 2 s after the merge; PR #3983: same shape (closed at merge, later reopened and re-closed manually) |
| Inline code span (`` `Closes #N` ``) | **does not count** | PR #14383: `` `Closes #14059` `` / `` `Closes #14052` `` twice each (prose + checklist), tracked issue plain; refs = `[14351]` only |
| Fenced code block | **does not count** | PR #8199: fenced parser-fixture line naming `#1`/`#2`; refs = `[]`. Canonical single-space form: PR #15089's fenced `Closes #9879` fixture absent from its readout `[4569, 14176, 15058]` |

GitHub's documentation names only the two shorthand syntaxes (`KEYWORD #N`,
`KEYWORD OWNER/REPOSITORY#N`), colons and uppercase — it is silent on URLs,
fences, quotes and code spans, so the table is the authority ([docs: linking a
pull request to an issue](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue)).

### Reconciling the #9879 comment

`forge-helpers.sh`'s Gitea fallback (`forge_pr_close_targets`, #9879) strips
fenced blocks **and blockquoted lines** before its regex, and its comment claimed
GitHub's API-computed field carries "this same fence-awareness". Measured above,
that claim is half right: GitHub ignores fences (agreeing with the strip) but
**does** parse blockquoted closers (PR #12122) and full-URL closers (2am #4161),
which the Gitea regex does not match at all. The Gitea strip is therefore
deliberately **stricter** than GitHub for a blockquoted closer — a divergence
noted here and left alone: changing Gitea-side behavior is out of scope for
#15058. The comment in `forge-helpers.sh` now records the same.

### What the scan is

The Builder scan is a **precautionary local approximation**, deliberately
over-strict: it matches inline-code and fenced examples GitHub (measured)
ignores, because a match a Builder cannot explain is rewritten rather than
trusted to a parser GitHub documents nothing about. Since #15058 it also matches
the full-URL form, which really does close. Once a PR is open, GitHub's own
answer is `closingIssuesReferences`, the field `forge_pr_close_targets()` reads
— the readout, not the scan, is the authority.

## What this does not fix

A reliable forge-side link does **not** by itself make `sweep.outcome`'s
`pr_number` / `pr_numbers` populated in the durable store. Those are derived
correctly (PR #9481) but two independent code paths emit `sweep.outcome` for
the same `sweep_id`, and the dashboard's `INSERT OR IGNORE` lets the
impoverished live-collector record win the race — issues #9476 / #9477 own
that. End-to-end "100% of landings resolve to their issue" is measurable only
once both halves have shipped and had a week of real PRs to observe.
