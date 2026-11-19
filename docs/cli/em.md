# `klt em`

Compute a Maxwell capacitance matrix in farads with the released Geode CLI.
A request can supply a tagged mesh or ask `klt em` to mesh a labelled GDS
region using its PDK stackup. Both paths use `engine: "geode_fem"`,
`solve_mode: "quasi_static"` and `outputs: ["c"]`.

```
klt em request.json --format json
```

## Layout request

With sky130A installed, save this request at the repository root to run the
inverter example. Its layout path is relative to the request file.

```json
{
  "layout": "tests/corpus/sky130/sky130_fd_sc_hd__inv_1.gds",
  "region": {"cell": "sky130_fd_sc_hd__inv_1", "bbox_um": [0.1, 1.1, 1.2, 1.65]},
  "stackup": "sky130A"
}
```

The selected region contains the nets `A`, `VPWR` and `Y`. Add
`"ground": ["VPWR"]` to hold the power rail at 0 V and return a two-terminal
matrix for `A` and `Y`. Checks use these same GDS labels.

| Field | Meaning |
| --- | --- |
| `layout` | GDSII or OASIS stream. |
| `region.cell` | Cell to flatten, in its local coordinates. Defaults to the unique top cell. |
| `region.bbox_um` | Optional `[left, bottom, right, top]` crop in micrometres, rounded to the database grid. `null` or omission includes the whole cell. |
| `stackup` | Installed PDK variant resolved by `klt pdk stackup`, using its default corner and curated contiguous elevations. |
| `ground` | Optional array of selected net labels to ground in addition to Geode's six grounded outer walls. All other selected nets are terminals, in label order. |

Conductors and vias use the stackup's GDS layers, elevations and thicknesses.
The extraction deck identifies each metal's label purpose and the layers each
via connects. Connectivity and label association are resolved before clipping,
so a label can lie outside the crop. Each selected connected conductor must
have one distinct nonblank label. Conflicting labels, unlabeled conductors and a
label shared by disconnected selected nets produce an application error.
Geode's physical-group identifiers (`net_0`, `net_1`, …) follow sorted label
order, including ground nets. Rectilinear holes are preserved.

**Dielectric and boundary model.** Each planar slab uses its own stackup
permittivity. The bottom of the dielectric stack is the grounded substrate
surface. At the first unknown permittivity the domain ends, and the response
records this in `diagnostics`. In sky130A that puts the grounded top at
5.3711 µm, the start of the conformal passivation. A selected conductor
reaching that boundary gives exit 1. Geode v1 describes horizontal slabs
and cannot express conformal coatings. The lateral walls lie one stack
height beyond the clipped conductors. The maximum mesh size is half that
height, and the surface target is the thinnest selected conductor or via.

**Meshing dependency.** The adapter runs `geode mesh --analysis capacitance`
and passes its generated starter spec directly to `geode capacitance`.
This path was tested with Geode 0.9.0. Install Gmsh 4.11 or newer with
OpenCASCADE and put `gmsh` on `PATH`, or set `GEODE_GMSH` to its executable.
A missing or unrunnable Gmsh returns exit 1 with Geode's diagnostic in the
standard error envelope. [Geode meshing documentation](https://github.com/rjwalters/geode-fem/blob/v0.9.0/crates/geode-cli/examples/mesh/README.md)

## Supplied mesh request

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
| `options.timeout_s` | Wall-clock budget shared by meshing and solving, in seconds (default 1800). |
| `options.keep_artifacts` | Default `true`. Keep outputs under `output_dir` (default `.klt/em/<request-stem>/` beside the request). Layout runs use a fresh `run-*` directory. With `false`, temporary outputs are removed and a caller-supplied mesh still appears in `artifacts`. |
| `options.output_dir` | Artifact directory; must be a string when present. |

### Resolving `geode`

`options.geode_binary` → `$KLT_GEODE_BINARY` → `geode` on `PATH`. A binary named
by the first two that is not runnable is an error (no silent fallback). If none
resolves the run exits `1` with an error naming `geode`, all three sources and
install guidance. It never produces a result without running the engine.

## Response (`schema_version: 1`)

- `status`: `pass` or `fail` (`fail` = trustworthy result, a check missed its limits).
- `environment`: `engine`, `engine_version` (from `geode --version`, `null` if unparseable), `solve_mode`, `binary`, `mesh_sha256`.
- For layout requests, `environment` also includes `stackup`, `layout_sha256` (stream bytes), `stackup_sha256` (the full resolved stackup JSON, UTF-8 with sorted keys, compact separators and ASCII escaping), and `mesh_stats` with Geode's `nodes`, `tets` and boundary `triangles` counts.
- `results.c`: `unit` (`"F"`), `terminals` (label order for a layout, request order for a supplied mesh), `matrix` (Maxwell capacitance, farads).
- `checks[]`: `name`, `output`, `terminal_pair`, `unit`, `limits`, `status`, `value`, `margin` (positive headroom, negative violation, `null` without limits).
- `diagnostics[]`: model limitations encountered while translating the stackup.
- `artifacts`: `mesh`, and with `keep_artifacts` `spec`, `report`, `log`, each a hashed `{path, scope, sha256}` envelope. Retained layout runs also reference the generated Geode layout JSON (`layout`), `mesh_report` and `mesh_log`. The mesh hash and statistics survive temporary artifact cleanup.

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
