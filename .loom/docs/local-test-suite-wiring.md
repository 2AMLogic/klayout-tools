# Wiring a locally-added `.loom/scripts/tests/test-*.sh` suite

`.loom/scripts/tests/ci-wired.txt` (and its sibling `ci-excluded.txt`, plus
`run-ci-suites.sh` and `check-ci-suite-manifest.sh`) are **vendored wholesale**
from upstream `rjwalters/loom`'s own `defaults/scripts/tests/` — they are the
Loom *project's* own CI harness for the Loom *project's* own test suite, copied
into every installed `.loom/` tree by `resync-installed.sh` (the routine
`chore: resync installed Loom surfaces` commit).

**They are not this repo's CI, and this repo's CI does not invoke them.** No
workflow under `.github/workflows/` calls `run-ci-suites.sh`, and
`check-ci-suite-manifest.sh` cannot even run here — most of the ~500 suite
names it expects reference scripts upstream ships that were never installed
into this consumer repo. A line in `ci-wired.txt` has **zero effect** on
klayout-tools' CI regardless of whether it's present.

## Do not add local suites to `ci-wired.txt`

Because `ci-wired.txt` is copied verbatim from upstream on every resync, any
local addition is silently reverted the next time that commit lands — often
within hours (`test-check-dep-recheck-idempotency.sh` was added by PR #1524
and reverted by commit `ce6a5cc` the same day; see issue #1532). Pinning the
file via `.loom/resync-ignore` is **not** the fix either — that would freeze
the manifest against upstream's own legitimate future additions.

If you author a new `.loom/scripts/tests/test-*.sh` suite that covers a
klayout-tools-local customization (e.g. a guard script or a `curator.md`/
`champion.md` behavior that only exists in this repo — see
`.loom/resync-ignore`'s header for the list of local customizations), it has
two possible durable outcomes, and no third one:

1. **Upstream it.** If the suite exercises genuinely general Loom behavior
   (not a klayout-tools-only customization), send it to `rjwalters/loom`. Once
   merged there, the next resync brings back both the suite *and* its
   `ci-wired.txt` entry automatically — no local edit needed, and nothing to
   revert.
2. **Run it manually, and say so.** If the suite is klayout-tools-local and
   not upstream-worthy, it has no automated CI runner in this repo. Leave it
   out of `ci-wired.txt` (an entry there would be reverted and does nothing
   here anyway) and note in the suite's own header comment, or in the issue/PR
   that added it, that it is run manually:
   `./.loom/scripts/tests/test-<name>.sh`.

A suite only earns a **durable, automated** runner in this repo by being added
as its own explicit step in `.github/workflows/ci.yml` — never by routing it
through `run-ci-suites.sh`, which this repo never invokes.

## Current status

As of issue #1541 (2026-09-07), three klayout-tools-local suites exist under
`.loom/scripts/tests/` with no automated runner in this repo — **run them
manually**:

- `./.loom/scripts/tests/test-check-dep-recheck-idempotency.sh` (tests
  `check-dep-recheck-idempotency.sh`, added by #1523/#1524)
- `./.loom/scripts/tests/test-dep-recheck-fingerprint.sh` (tests
  `dep-recheck-fingerprint.sh`, added by #1528/#1534)
- `./.loom/scripts/tests/test-curator-dep-recheck-recipe.sh` (executes
  curator.md's own documented "Re-check Idempotency" bash recipe verbatim
  against the deployed `dep-recheck-fingerprint.sh` /
  `check-dep-recheck-idempotency.sh`, added by #1541 — the regression guard
  against the doc/script coupling breaking silently again, as it did on
  2026-09-06 when a same-day resync restructured the script's CLI without
  curator.md's recipe following)

A prior attempt to list the first two in `ci-wired.txt` (PR #1524, restored
as a stopgap by PR #1534) had no effect on this repo's CI either way (see
issue #1532 for the full history) — do **not** add `test-curator-dep-recheck-
recipe.sh` there either, for the same reason. Whether any given entry
currently happens to survive a resync is incidental (verified 2026-09-07:
`test-dep-recheck-fingerprint.sh` is present in the vendored `ci-wired.txt`,
`test-check-dep-recheck-idempotency.sh` is not) — neither presence nor
absence changes anything here, since no workflow in this repo consults that
file at all. If any of these suites is ever upstreamed, remove it from this
list once its own `ci-wired.txt` entry starts surviving resyncs *because*
`run-ci-suites.sh` is genuinely exercising it, not by coincidence.
