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
  "mesh": {"path": "pair.msh", "length_unit_m": 1e-6},
  "terminals": [{"name": "sig", "surface": "S1"}, {"name": "gnd", "surface": "S2"}],
  "ground": [{"name": "sub", "surface": "S3"}],
  "materials": [{"physical_group": "oxide", "eps_r": [3.9, 0.0]}],
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
| `mesh` | Object: `path` (relative to the request file) and `length_unit_m` (metres per mesh length unit, finite > 0). Both required — geode's `MeshSpec` needs the length unit to scale the matrix to farads. |
| `terminals[]` | `name` (caller-facing) and the mesh dimension-2 physical group `surface` it binds to. `surface` values go into the geode spec in matrix row/column order; `name` never reaches the engine. |
| `ground[]` | The 0 V reference conductor(s), same `{name, surface}` shape. At least one is required (geode schema v1 rejects a ground-less spec at load). A conductor is a terminal or ground, never both. |
| `materials[]` | Optional dielectrics: `{physical_group, eps_r: [re, im]}`; `eps_r` defaults to vacuum `[1, 0]` — omitting `materials` leaves every unlisted region at vacuum. `im` must be 0 and `re` > 0: the electrostatic solve is real and lossless. |
| `checks[]` | `name`, `output: "c"`, `terminal_pair` (`[a, a]` = self term), `unit` (`F`/`pF`/`fF`, default `fF`), `limits` (`min`/`max`, either optional; none = reported, never fails). |
| `options.geode_binary` | Binary name/path; see resolution below. |
| `options.timeout_s` | Wall-clock budget (default 1800). |
| `options.keep_artifacts` | Default `true`: keep spec/report/log under `output_dir` (default `.klt/em/<request-stem>/` beside the request). `false` keeps only the mesh reference. |
| `options.output_dir` | Artifact directory; must be a string when present. |

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
- `artifacts`: `mesh`, and with `keep_artifacts` `spec`, `report`, `log`, each a hashed `{path, scope, sha256}` envelope. Fields/meshes are never inlined.

`error` (no trustworthy result: bad request, engine failure/timeout, a report
whose terminals or matrix do not match the request) is the standard error
envelope on stderr; a failed solve's stdout/stderr is kept in `solve.log`, and
the engine's `kind: "error"` report message is surfaced in the error text.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Solve completed; every declared check passed (or none declared). |
| `1` | Application error, error envelope on stderr, no report. |
| `2` | Usage error (argparse). |
| `3` | Solve completed; at least one check failed. Report is on stdout. |

## Engine interface (read from geode-fem's source)

The engine is the public `rjwalters/geode-fem` repo; the interface below was
read from `crates/geode-cli` (not assumed). It is isolated in
`build_geode_spec` / `build_geode_command` / `parse_capacitance_report`
(`src/klayout_tools/_geode.py`).

Invocation — `crates/geode-cli/src/main.rs`, `StaticArgs` + `CapacitanceArgs`:

```
geode capacitance <SPEC> [-o|--output PATH] [--threads N] [--spice PATH] [--spice-ret-pin]
```

- `<SPEC>` is a **problem spec** (JSON or TOML; `problem::load` parses it),
  not a mesh path. There is no `--terminals` and no `--format` flag.
- `--output` moves the JSON report from stdout to a file; the adapter always
  passes it so the report lands in the artifacts dir.
- `geode --version` prints `geode <crate-version> (<git-sha>)` (`main.rs`
  `VERSION`); the adapter reports the crate version.
- On failure the CLI still writes `{"kind": "error", "status": "error",
  "error": {code, message}}` to the output path (`main.rs` `finish`), then
  exits nonzero. The adapter surfaces `error.message`.

Spec — `crates/geode-cli/src/spec.rs`, `ProblemSpec`
(`#[serde(deny_unknown_fields)]`, so unknown fields are rejected; the
adapter sends exactly these fields):

```json
{
  "schema_version": 1,
  "mesh": {"path": "/abs/pair.msh", "length_unit_m": 1e-6},
  "materials": [{"physical_group": "oxide", "eps_r": [3.9, 0.0]}],
  "capacitance": {"terminals": ["S1", "S2"], "ground": ["S3"]}
}
```

- `schema_version` must be 1 (`SPEC_SCHEMA_VERSION`).
- `mesh.length_unit_m` is required: `capacitance.rs` scales the solved
  matrix to farads by exactly this factor.
- `capacitance.terminals` / `ground` are lists of dimension-2
  physical-group names; `ground` needs at least one entry in v1 —
  rejected at load otherwise (`problem/capacitance.rs`,
  `validate_capacitance`). The request's `name`↔`surface` indirection is
  resolved here: only `surface` names reach the engine, in matrix
  row/column order.
- `materials[].eps_r` defaults to vacuum `[1, 0]`; a capacitance spec
  rejects `Im(eps_r) != 0` and `Re <= 0` (`validate_capacitance`).

Report — `crates/geode-cli/src/report.rs`, `CapacitanceReport`: top-level
`terminals` (physical-group names, matrix row/column order) and `c_farad`,
the row-major Maxwell capacitance matrix in **farads** (`capacitance.rs`
`scaled_matrix`). The adapter validates the matrix against the requested
terminal surfaces and reorders it to the request's terminal order.

The unit tests replay a fixture with these engine-side keys; the live test
against a real `geode` binary stays skipped where the binary is absent.
