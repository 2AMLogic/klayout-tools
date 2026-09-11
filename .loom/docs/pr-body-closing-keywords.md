# A quoted `Closes #N`-shaped example can falsely auto-close an unrelated issue (#1674)

GitHub's issue auto-close keyword scanner (`Closes #N` / `Fixes #N` / `Resolves #N`, and their
conjugations) matches on the **raw text** of a PR body or commit message. It is not restricted
to a genuine closing directive, and it does **not** respect markdown code-span or fenced-code
delimiters — a closing keyword quoted inside single backticks or a triple-backtick block reads
to GitHub exactly the same as one sitting in plain prose.

## The incident (2026-09-11)

PR #1671 (a fix to `.loom/scripts/dep-recheck-fingerprint.sh`'s checklist-line regex) included,
as an inline-code illustrative example of the bug it was fixing:

> `` `- [ ] PR #1607 (Closes #1600) merged - ...` did not match, so the ``

That `(Closes #1600)` was quoted example text describing a *different* issue's (#1606's)
dependency-checklist wording — not a real closing directive, and not even about the same issue
PR #1671 itself closed (`Closes #1669`). Every human reviewer read it as an inert code sample.
GitHub's scanner matched it anyway and closed **issue #1600** the instant #1671 merged, even
though #1600's actual implementation PR (#1607) was — and still was, at the time — open and
unmerged.

A Curator pass caught this only because it happened to be re-checking a dependent issue (#1601)
whose blocker (#1600) had unexpectedly "resolved" — nothing else in the pipeline would have
surfaced the false close on its own. #1600 was manually reopened with an explanatory comment.

## Why this is a distinct hazard from the `Part of #N` stray-keyword hazard (#4569)

`builder-pr.md`'s "A stray closing keyword ANYWHERE in the body defeats `Part of #N`" section
(#4569) covers a keyword accidentally closing the PR's **own tracked issue** when the PR meant
to keep it open. This is different: the keyword here is adjacent to **some other issue's**
number, mentioned only because the PR body is quoting or describing that other issue's text —
there is no intent to reference it as a closing target at all, and the PR's own tracked-issue
handling can be completely correct. Any grep that only checks "does `#<this PR's issue>` have a
keyword next to it" (the #4569 backstop) will not catch this shape, because the accidentally-
closed issue is never the PR's own tracked number.

## The fix: break the keyword/`#N` adjacency in quoted example text

When a PR body or commit message needs to show example text shaped like `Close(s|d) #N` /
`Fix(es|ed) #N` / `Resolve(s|d) #N` — for **any** issue number, not just the PR's own — break
the literal adjacency so GitHub's plain-text scanner cannot read it as a directive. Wrapping it
in backticks is **not sufficient**; the scanner ignores markdown entirely.

Options, in order of preference:

1. **Rephrase to avoid the literal adjacency.** Usually the cleanest fix and the most readable
   for a human too:
   - `"...merged, per its own closing reference to #1600..."` instead of
     `"...(Closes #1600) merged..."`
   - `"...whose checklist line named issue 1600 as closed by that PR..."`
2. **Insert a space or zero-width character between the keyword and `#`.** Use when the exact
   original wording must be preserved verbatim (e.g. quoting another issue's checklist line
   exactly):
   - `` Closes # 1600 `` (a literal space — GitHub does not match `Closes` immediately followed
     by whitespace then `#`, the "immediately followed" requirement `builder-pr.md`'s own
     Auto-Close section documents)
   - `` Closes #&#8203;1600 `` (HTML zero-width-space entity — renders as an ordinary `Closes
     #1600` on GitHub's rendered page, but the raw text GitHub's scanner reads has the entity
     between `#` and the digits, so it does not match)

## The guard: `check-pr-body-closing-keywords.sh`

`.loom/scripts/check-pr-body-closing-keywords.sh` is advisory tooling that reproduces exactly
what a human reviewer cannot see by eye: it scans a PR body (or any text) and reports every
closing-keyword occurrence found **inside a markdown inline-code span or fenced code block** —
the shape GitHub's scanner cannot tell apart from a real directive. A closing keyword in plain
prose, including the PR's own genuine `Closes #N` line, is never flagged; that is the intended,
working case the guard must stay silent on.

```bash
# Against an already-open PR:
./.loom/scripts/check-pr-body-closing-keywords.sh --pr 1671

# Against a draft body before creating the PR:
./.loom/scripts/check-pr-body-closing-keywords.sh --body-file /tmp/pr-body.txt

# Or pipe it in:
gh pr view 1671 --json body -q .body | ./.loom/scripts/check-pr-body-closing-keywords.sh
```

Exit codes: `0` — no code-span/fence closing-keyword risk found; `1` — usage error; `2` — one or
more risky occurrences found, printed with their offending text. `--self-test` runs the guard
against the exact #1671/#1600 incident shape (and the negative cases: a clean body, a fenced
false-positive shape that also must be caught, and a genuine own-line closing directive that
must NOT be flagged) without touching the network. Regression coverage lives in
`.loom/scripts/tests/test-check-pr-body-closing-keywords.sh` (run manually — see
`local-test-suite-wiring.md` for why locally-added suites under `.loom/scripts/tests/` have no
automated CI runner in this repo).

This is advisory, not a substitute for the convention above: it cannot stop GitHub's own
scanner, and it only catches the shape at review time if someone actually runs it. Builder
should run it before pushing a PR body that quotes example text containing an issue reference;
see `builder-pr.md`'s "Quoting a `Closes #N`-shaped example inside a PR body closes an UNRELATED
issue" section.

## Related

- #1600 — the issue falsely auto-closed by PR #1671's quoted example (reopened 2026-09-11 with
  the full incident writeup in its comment history)
- #1601 — the dependent issue whose Curator re-check surfaced this
- #1671 — the PR whose body triggered the false close
- #1607 — the actual, then-still-open implementation PR for #1600's work
- #4569 — the related but distinct "stray keyword defeats `Part of #N`" hazard, documented in
  `builder-pr.md`
