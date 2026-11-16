# `klt em`

Field-solver verb ([#5616](https://github.com/2AMLogic/klayout-tools/issues/5616),
brief 1.A of Epic [#708](https://github.com/2AMLogic/klayout-tools/issues/708)).
Wraps the released `geode` binary (geode-fem) as a subprocess, the way `klt sim`
wraps `ngspice`; no FEM numerics live in this repo. The contract is the one
proposed in [`docs/design/em-field-sim-spike.md`](../design/em-field-sim-spike.md)
§4.

**Shipped slice:** `engine: "geode_fem"`, `solve_mode: "quasi_static"`,
`outputs: ["c"]` on a **caller-supplied** mesh and terminals. GDS + stackup →
mesh is not part of this slice; any other engine, solve mode or output is
rejected (exit 1).

```
klt em request.json --format json
```

## Request

```json
{
  "engine": "geode_fem",
  "solve_mode": "quasi_static",
  "outputs": ["c"],
  "mesh": "pair.msh",
  "terminals": [{"name": "sig", "surface": "S1"}, {"name": "gnd", "surface": "S2"}],
  "checks": [
    {"name": "c_sig_gnd", "output": "c", "terminal_pair": ["sig", "gnd"],
     "unit": "fF", "limits": {"min": -20, "max": -10}}
  ],
  "options": {"geode_binary": null, "timeout_s": 1800, "keep_artifacts": true, "output_dir": null}
}
```

| Field | Description |
| --- | --- |
| `engine`, `solve_mode`, `outputs` | Optional; default to the only supported values. |
| `mesh` | Mesh path (relative to the request file). Required. |
| `terminals[]` | `name` and the mesh `surface` it binds to. Written to `terminals.json` and passed to `geode`. |
| `checks[]` | `name`, `output: "c"`, `terminal_pair` (`[a, a]` = self term), `unit` (`F`/`pF`/`fF`, default `fF`), `limits` (`min`/`max`, either optional; none = reported, never fails). |
| `options.geode_binary` | Binary name/path; see resolution below. |
| `options.timeout_s` | Wall-clock budget (default 1800). |
| `options.keep_artifacts` | Default `true`: keep report/log/terminals under `output_dir` (default `.klt/em/<request-stem>/` beside the request). `false` keeps only the mesh reference. |

### Resolving `geode`

`options.geode_binary` → `$KLT_GEODE_BINARY` → `geode` on `PATH`. A binary named
by the first two that is not runnable is an error (no silent fallback). If none
resolves the run exits `1` with an error naming `geode`, all three sources and
install guidance. It never produces a result without running the engine.

## Response (`schema_version: 1`)

- `status`: `pass` or `fail` (`fail` = trustworthy result, a check missed its limits).
- `environment`: `engine`, `engine_version` (from `geode --version`, `null` if unparseable), `solve_mode`, `binary`, `mesh_sha256`.
- `results.c`: `unit` (`"F"`), `terminals` (request order), `matrix` (Maxwell capacitance, farads).
- `checks[]`: `name`, `output`, `terminal_pair`, `unit`, `limits`, `status`, `value`, `margin` (positive headroom, negative violation, `null` without limits).
- `diagnostics[]`: reserved, currently empty.
- `artifacts`: `mesh`, and with `keep_artifacts` `report`, `log`, `terminals`, each a hashed `{path, scope, sha256}` envelope. Fields/meshes are never inlined.

`error` (no trustworthy result: bad request, engine failure/timeout, a report
whose terminals or matrix do not match the request) is the standard error
envelope on stderr; a failed solve's stdout/stderr is kept in `solve.log`.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Solve completed; every declared check passed (or none declared). |
| `1` | Application error, error envelope on stderr, no report. |
| `2` | Usage error (argparse). |
| `3` | Solve completed; at least one check failed. Report is on stdout. |

## Engine interface assumed

The adapter runs `geode capacitance <mesh> --terminals terminals.json --format json`
and reads `{"terminals": [...], "capacitance_matrix_f": [[...]]}` from stdout.
This is isolated in `build_geode_command` / `parse_capacitance_report`
(`src/klayout_tools/em.py`) and has not yet been confirmed against a live
`geode` binary (none on the fleet workers); the unit tests replay a
hand-authored fixture and the live test is skipped without the binary.
