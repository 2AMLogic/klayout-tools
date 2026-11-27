# Test coverage (report-only)

CI measures Python line + branch coverage of `klayout_tools` on **one** leg of
the test matrix (Python 3.12, to respect the
[wall-clock budget](ci-wall-clock-budget.md)). It is **report-only**: there is
no `fail-under` threshold. The point is to make skip-masked gaps visible --
code that is only exercised by Docker/oracle-gated tests that skip on most
hosts -- and to give refactor and Contracts-phase prioritisation some data.

## Where to read it

- The `Tests (Python 3.12)` job summary carries a per-module table (lowest
  line coverage first), rendered by `scripts/coverage_summary.py`.
- The raw `coverage.json` is uploaded as the `coverage-report` artifact
  (30-day retention).

## Running it locally

Per the host rules, run it on a module-scoped subset, not the whole suite:

```bash
uv run --extra dev pytest tests/test_coverage_summary.py \
  --cov --cov-report=term --cov-report=json:coverage.json
python scripts/coverage_summary.py coverage.json
```

Configuration lives in `pyproject.toml` (`[tool.coverage.run]` /
`[tool.coverage.report]`): `source = ["klayout_tools"]`, `branch = true`,
and `_vendor/` omitted. A subset run reports low numbers for everything it did
not touch; that is expected.

## The floor (ratchet)

`coverage-baseline.json` records a line and a branch floor per `src/` module
(plus a total), in the style of `type-baseline.json`. The `Coverage floor
(ratchet)` step runs `scripts/check_coverage_baseline.py coverage.json` on the
3.12 leg and fails when:

- a baselined module drops more than `tolerance_pct` (1.0) below its floor,
  line or branch, or the total does; or
- a module not in the baseline lands below `new_module_floor_pct` (80) line
  coverage.

Improvements and deleted modules never fail. Floors are the **minimum** over
several completed `main` runs, rounded down, because skip-gated tests make
coverage vary slightly run to run. To raise or re-seed them, download the
`coverage-report` artifacts of a few completed `main` runs (CI cancels many
runs, so pick successful ones) and run:

```bash
python3 scripts/check_coverage_baseline.py --update-baseline run1/coverage.json run2/coverage.json
```

Lowering a floor needs reviewer sign-off, as with the type baseline. The
initial seed used five completed `main` runs from 2026-11-10.

Wall-clock: the check reads an existing JSON file and takes well under a
second; it adds no test time (coverage collection itself was measured in
#3223).
