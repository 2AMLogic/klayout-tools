# Dependency-gated CI: skip, refuse, or fail

`klayout-tools`'s runtime dependency set is deliberately two packages —
`klayout` and `jsonschema` (see `src/klayout_tools/ir_solver.py`'s docstring
for the reasoning) — and that minimum is a contract, not an accident. Tests
that need something heavier than the contract (numpy as a numerical oracle,
an optional native extension, an external engine) still have to behave
honestly everywhere that dependency is absent. Issue #2276 is the pattern
statement, written after downstream evidence: three PRs in one day hit the
same failure class, and one drift check had *literally never executed with
its dependency present on CI* while its output drifted cross-platform
undetected.

## The convention

Three situations, three correct behaviors:

| Situation | Correct behavior |
|---|---|
| Dependency absent by contract (stdlib-only host) | The suite **skips** (`pytest.importorskip`) and/or the code **refuses** with its declared sentinel/error. |
| Dependency present (the dedicated dep-full job) | The suite runs its **full-strength assertions** against the real oracle. |
| A check whose purpose is catching drift | It must itself **execute** in the dep-full job — not merely be importable there. |

Two corollaries that are the actual teeth:

- **A refusal is correct behavior and must never be recorded as a pass.** A
  stdlib-only host that returns a declared `*_unavailable` sentinel is not
  "working" — it is honestly declining, and the test asserted *that*. A test
  that would accept either a value or the sentinel asserts nothing.
- **A silent skip is indistinguishable from a green lie.** A job where the
  dependency-gated suite skipped is evidence of nothing. So wherever CI
  provisions the dependency, CI also asserts the tests actually ran (below).

## Stdlib-only hosts assert the declared unavailability

`tests/test_ir_solver_numpy_crosscheck.py` is the numpy example: the whole
module gates on `pytest.importorskip("numpy")`, so a stdlib-only run skips
with a clear declared reason and never fails and never false-passes. The
same shape appears in code paths, not just tests: a feature that cannot run
without its dependency reports its declared refusal (`numpy_unavailable`
sentinel, `no_pad` / `not_converged` reasons, a clean build pointer for a
missing Rust extension) — a caller on a stdlib-only host asserts *the
refusal*, never a value.

## Full-strength assertions live in the `<suite>-numerical` job

The mirror image: where the dependency IS provisioned, the loose or skipped
tier is not enough. The `test-numerical` job
([`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)) is the one
place `uv sync --locked --extra dev --group numerical` installs the
uv.lock-pinned numpy (the `numerical` PEP 735 group — see pyproject.toml's
comment on why it is a group, and why numpy's pin is floor-only) so the
pure-Python conjugate-gradient IR solver and `sim.py`'s statistics are
cross-checked against `numpy.linalg.solve` / `numpy.percentile` at
round-off-level tolerances.

It mirrors the native jobs' no-silent-skip pattern (the extension freshness
gate, issue #1889): after pytest, the job asserts numpy is importable **and**
that the pytest summary shows real passes and zero skips — an importable
oracle with a skipped suite is exactly the false green this job exists to
make impossible.

## Drift detectors run in the dep-full job too

The gap that motivated #2276: a `-numerical` job that ran only unit tests
left the publication `--check` never executed with dependencies present on
CI. A check that exists to catch drift must itself be *executed* where the
dependency is present — otherwise a dependency-dependent code path in the
thing it guards is only ever validated by a run that cannot exercise it.

So `test-numerical` also re-runs
[`scripts/check_artifact_determinism.py`](../../scripts/check_artifact_determinism.py)
under the dep-full environment (two checkouts, same shape as the numpy-less
`artifact-determinism` job, which keeps running independently). If a
golden-artifact generator ever grows a numpy-dependent branch, both runs now
exist and must agree byte-for-byte.

## Checklist for a new dependency-gated surface

1. The code refuses with a declared sentinel/error when the dependency is
   absent — no exception leaks, no fabricated value.
2. The test suite skips cleanly (`pytest.importorskip` or an equivalent
   explicit skip) with a reason naming the dependency.
3. The full-strength assertions live in a test module that only runs under
   the dep-full job, and that job asserts the suite actually ran (no silent
   skip).
4. If the surface guards generated output, the drift detector runs in the
   dep-full job as well.
5. The dependency is a uv.lock-pinned PEP 735 group (or an extra, when end
   users legitimately opt in), never a runtime dependency — and
   `.github/ci-wall-clock-budget.json` gets a row or the job inherits the
   documented default.
