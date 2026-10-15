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

## What comes next

After a few weeks of data, a follow-up can add a ratcheting per-module floor
in the style of `complexity-baseline.json`. Nothing here enforces one.
