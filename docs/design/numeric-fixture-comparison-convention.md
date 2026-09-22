# Numeric-fixture comparison convention: tolerance vs. byte-exact

Committed reference fixtures over **numeric output** (a solved matrix, a
simulated waveform, a measured quantity) must state, region by region,
whether that region is compared to its reference **byte-exact** or **within
a declared tolerance**. This doc exists so a future contributor adding such
a fixture makes that choice deliberately instead of defaulting to whatever
`assert_eq!`/`==` happens to compile — and so a byte-exact comparison that
flakes across CI hosts is triaged correctly the first time, instead of
being mistaken for a code regression.

This is not a new rule invented for this doc: `native/mom` and `klt sim`
(below) already follow it. What's new here is writing it down as a
repo-wide convention.

## The rule

1. **Declare the comparison kind per region, not per artifact.** A fixture
   covering multiple output regions (e.g. several fields of a JSON report,
   or several columns of a numeric table) may byte-compare one region and
   tolerance-compare another — but the split must be explicit in the test
   or the fixture's own documentation. **Never apply one blanket tolerance
   (or one blanket byte-exact requirement) over an entire artifact** just
   because most of it happens to be deterministic; a single non-deterministic
   field (a timestamp, an engine version, a last-ulp float) forces the
   comparison style for the *whole* artifact if the split isn't explicit,
   which is exactly how a fixture that was 99% legitimately byte-exact ends
   up either flaking (kept byte-exact) or silently under-checked (loosened
   wholesale to a tolerance).
2. **A byte-exact-comparison flake gets forensics before blame-code triage.**
   If a committed byte-exact fixture fails on CI but passes locally (or on a
   different runner), do **not** treat it as a real regression until both of
   the following are done, in order:
   - **Prove the inputs were bit-identical** between the failing run and a
     passing run — same commit, same generator inputs, same declared seed
     (if any). If the inputs differ, this isn't a comparison-tolerance
     question at all; go fix the actual input drift.
   - **Get one green rerun at the identical head.** A single rerun that
     passes, with bit-identical inputs confirmed, means the failure was
     environmental (see below), not a code regression — re-open only if a
     rerun at the same head fails again.

   Only after both steps should the failure be treated as evidence of a
   real regression in the code under test.

## Where cross-host variance actually comes from in this repo

The failure class this convention exists for — a byte-exact comparison that
passes on one CI host and fails on another with verified-identical inputs —
is a real risk in other stacks from BLAS/PyTorch/oneDNN ISA-dispatch
variance (`ATEN_CPU_CAPABILITY`, `MKL_CBWR`, `ONEDNN_MAX_CPU_ISA` and
similar env vars pin the dispatch path there). **That risk class does not
apply to klayout-tools as shipped today**: this repo has no numpy/PyTorch/
BLAS/oneDNN dependency, so there is no ISA-dispatch env var to pin here, and
adding one to a workflow would pin nothing.

The concrete source of cross-host variance in *this* repo is
[`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)'s runner-pool
split: several jobs (currently `native`, `native-statime`, and `test` — grep
the workflow for `self-hosted` / `ubuntu-latest` rather than trusting a line
number here, since this file changes often) route to `[self-hosted, heavy]`
for same-repo PRs and fall back to `ubuntu-latest` otherwise. Any future
numeric fixture that wants byte-exact comparison must be evaluated against
*that* split as its source of host heterogeneity, not against a BLAS/PyTorch
env var that has no analogue in this codebase.

## Existing examples already following this convention

- **`native/mom/src/solver.rs`** — the MoM iterative solver compares its
  computed capacitance/potential values against a **relative tolerance**
  (`ITERATIVE_REL_TOL: f64 = 1e-12`), not byte equality. There is no
  `assert_eq!` on an `f64` result anywhere in `native/mom/src/*.rs`; every
  numeric check goes through this tolerance. This is the tolerance-compared
  half of the convention, already in place.
- **`klt sim`** (`docs/cli/sim.md`, "Worked example" section) — deliberately
  ships **no committed byte-exact golden fixture** for simulation output,
  because `runtime_s`/`engine_version` make a byte-exact fixture flaky by
  construction. The worked example instead documents the deterministic
  values a run reproduces (a form of declared, checkable-but-not-byte-exact
  comparison). This is the "don't blanket-byte-compare an artifact that has
  a non-deterministic field" half of the convention, already in place.

A new numeric fixture should look like one of these two, not invent a third
pattern: either commit to a stated relative/absolute tolerance on the
numeric regions (like `native/mom`), or, where a field genuinely cannot be
made deterministic, exclude it from the comparison explicitly and document
why (like `klt sim`) — rather than either byte-comparing a
non-deterministic field, or loosening an otherwise-exact artifact's every
field to a blanket tolerance to work around one non-deterministic one.

## What this doc does not cover

This doc is about **comparison style for numeric fixtures already known to
vary in ways that are acceptable** (tolerance) or **known not to vary at
all** (byte-exact). It is a distinct concern from
[`../guides/golden-artifact-determinism.md`](../guides/golden-artifact-determinism.md),
which is about verifying that a *supposedly*-deterministic generator (no
numeric tolerance involved — set/dict ordering, host-absolute paths,
host-libm float provenance) actually reproduces the same bytes it claims
to. A fixture whose generator has a `host_float_op` finding under that
check is a determinism bug to fix, not a case for this doc's tolerance
convention — reach for this doc only once a region's cross-host numeric
variance is expected and bounded, not when it's an undeclared bug.

## Related

- [`../guides/golden-artifact-determinism.md`](../guides/golden-artifact-determinism.md)
  — generator-determinism checking (a different, related concern; see above),
  including the manifest-level `platform-variable` declaration (#2275) that
  implements this convention's "declare which regions are tolerance-compared"
  half for checked golden-artifact generators, plus the forensics + rerun
  triage pack its CI jobs attach to failures.
- [`../cli/sim.md`](../cli/sim.md) — "Worked example" section, the `klt sim`
  no-byte-exact-fixture example cited above.
- `native/mom/src/solver.rs` — the `ITERATIVE_REL_TOL` tolerance-comparison
  example cited above.
