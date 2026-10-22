# `klt corner-matrix`

Mechanical check of a repo-committed **corner matrix** against a `klt sim`
request or report. Tracks gap G3 of the shared-sim-harness survey (issue
#4045).

`klt sim` owns the corner *axes*; it does not know which grid a chip calls
"full". The repo commits that grid as a matrix file, and `klt corner-matrix
check` reports whether a request or a finished report covers it. It reports,
it does not judge: which matrix is full and any `subset_reason` policy stay
repo-owned (see
[`sim-evidence-discipline-spike.md`](../design/sim-evidence-discipline-spike.md)
item D).

```
klt corner-matrix check <matrix.json> (--request <req.json> | --report <sim-report.json>) [--format text|json]
```

## Matrix file

A JSON object using the same axis spelling as a `klt sim` request's
`corners` block, plus an optional top-level `exclude[]` with the same match
semantics `klt sim` uses:

```json
{
  "process": ["tt", "ss", "ff"],
  "supply_v": { "vdd": [1.62, 1.8, 1.98] },
  "temperature_c": [-40, 27, 125],
  "exclude": [{ "process": "ff", "temperature_c": -40 }]
}
```

Allowed keys: `process`, `supply_v`, `temperature_c`, `params`, `exclude`,
plus informational `schema` / `description`. Any other key is an error (no
silent narrowing). Expected tuples are expanded by `klt sim`'s own
expansion and named by its `corner_id`, so matrix and request cannot disagree.

## Modes

- `--request`: expands the request's `corners`/`exclude` and diffs against the
  matrix: `missing[]`, `extra[]`, `duplicate[]`.
- `--report`: every matrix tuple must appear exactly once in `corners[]`.
  Also reported: `not_simulated[]` (corner status other than `pass`/`fail`,
  e.g. `error` incl. `lost_shard`, or `inconclusive`), `non_finite[]` (a
  non-finite or absent measurement value) and `out_of_bounds[]` (corner
  status `fail`, read from the existing `measurements[].limits` result, not
  re-evaluated). Monte Carlo reports (`/mc<N>` corner ids) are not supported.

## Output

Shared envelope fields (`docs/json-contract.md`) plus: `mode`, `source`,
`matrix`, `complete`, `matrix_size`, `observed_count`, and the lists above
(`not_simulated`/`non_finite`/`out_of_bounds` only in `--report` mode).

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | `complete: true` |
| 1 | could not run (bad file, unknown matrix key, invalid axes) |
| 2 | argparse usage error |
| 3 | ran, grid not complete |
