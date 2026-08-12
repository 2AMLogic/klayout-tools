# `klt mom`

Quasi-static **capacitance** extraction via the Method of Moments (MoM):
discretise conductor surfaces from a GDSII/OASIS layout, fill the
potential-coefficient matrix, and solve for the Maxwell capacitance matrix.
Phase 0/1 of the Method-of-Moments epic ([#701](https://github.com/2AMLogic/klayout-tools/issues/701)),
delivered by [#718](https://github.com/2AMLogic/klayout-tools/issues/718).
This command's own bar is "produces a numeric capacitance-matrix result".
How close those numbers are to the true answer is a separate question,
answered by [`docs/design/mom-validation.md`](../design/mom-validation.md)
([#719](https://github.com/2AMLogic/klayout-tools/issues/719)): the solver is
checked against the parallel-plate and coaxial closed forms and shown to
converge under mesh refinement. Read it before trusting a number from here —
in particular for what `panel_size_um` you need for a given accuracy.

`klt mom` can also, optionally (`compute_inductance: true` in the spec file),
extract **partial inductance and DC resistance** via PEEC (Partial Element
Equivalent Circuit) filaments — Phase 1a of the same epic, delivered by
[#797](https://github.com/2AMLogic/klayout-tools/issues/797). This is a
separate, opt-in solve path with its own (narrower) geometric scope — see
"PEEC inductance/resistance" below — validated the same way, against
closed-form straight-wire and two-wire-loop inductance oracles (see
`docs/design/mom-validation.md`'s "Inductance/resistance" section).

```
klt mom <file> <spec> [--top <cell>] [--format text|json]
```

- `<file>` — path to a GDSII (`.gds`) or OASIS (`.oas`) layout file.
- `<spec>` — path to a JSON **spec file** (see "Spec file" below): the
  stackup mapping GDS layers to electrical conductors, the background
  permittivity, and the discretisation panel size.
- `--top` — top cell to discretise when the stream has more than one
  (**required** in that case — unlike `klt layers`, which defaults to
  summing across every top cell, `klt mom` needs exactly one root to
  discretise and errors otherwise).
- `--format` — `text` (default, a human-readable matrix table) or `json`.

The command is headless (`klayout.db` batch API only, no GUI) and safe to
run in CI, **except** that it additionally requires the `klt_mom_native`
Rust extension to be built and importable — see "Building the native
extension" below.

## Why Rust

This is the first Rust component in klayout-tools. Surface discretisation,
the dense potential-coefficient matrix fill, and its linear solve are
numerically hot in a way pure-Python is a poor fit for — the same rationale
`docs/ARCHITECTURE.md`'s "Rewrite rule" gives for moving hot paths to Rust
once they are a measured bottleneck. Unlike most of this repo's engines
(KLayout wrapped for geometry, `ngspice` wrapped for simulation), there is no
existing open-source engine being wrapped here: this is a fresh, purpose-built
BEM/MoM core, intended (per epic #701) to also serve as a second, independent
oracle to cross-check against the sibling [geode-fem](https://github.com/rjwalters/geode-fem)
FEM/DG solver once that integration lands.

## Architecture

- **`native/mom/`** — the Rust crate (`klt-mom-native`), built as a
  `maturin`-managed [pyo3](https://pyo3.rs/) extension module importable as
  `klt_mom_native`. It is a standalone Python project (its own
  `Cargo.toml` + `pyproject.toml`, `maturin` as its PEP 517 build backend)
  rather than folded into the top-level `pyproject.toml`'s `hatchling`
  build — every other `klt` command's packaging is unaffected by this
  change, and future Rust engines get the same `native/<engine>/`
  convention. The top-level `pyproject.toml` reaches it through a PEP 735
  `[dependency-groups] mom` group plus `[tool.uv.sources]`, so it is
  checkout-local and never lands in the published wheel's metadata (there is
  no `klt-mom-native` on PyPI to install).
- **`src/klayout_tools/mom.py`** — the Python library layer:
  `klayout.db`-based GDS layer extraction (per the spec file's stackup),
  building the request JSON `klt_mom_native.solve_mom_json` expects, and
  shaping its response into this command's documented payload (below).
- **`src/klayout_tools/cli/mom_cmd.py`** — the `klt mom` CLI wiring, through
  the shared `output.py` envelope helpers like every other verb.

### The native extension's own JSON interface

`klt_mom_native.solve_mom_json(request_json: str) -> str` is documented in
`native/mom/src/contract.rs`'s `MomRequest`/`MomResponse` structs. It is a
narrower, Rust-internal contract (axis-aligned box lists in micrometers +
a background permittivity, in; a capacitance matrix, out) — the Python layer
above is what turns real GDS geometry into that request and wraps the
response into `klt`'s shared envelope (`docs/json-contract.md`). Consumers of
`klt mom` should use the CLI/`run_mom()` contract documented below, not call
the native extension directly.

## Building the native extension

`klt mom` requires the `klt_mom_native` extension to be built once per
environment (it is not (yet) published as a prebuilt wheel). Building it
needs a Rust toolchain (`cargo`/`rustc`) — which is exactly why it is
**optional** rather than a hard dependency: every other `klt` verb stays
installable with no Rust toolchain in sight.

```bash
# From a repo checkout, with a Rust toolchain installed. `mom` is a PEP 735
# dependency group in the top-level pyproject.toml, resolved from native/mom/
# via [tool.uv.sources]; uv drives maturin to compile and install it.
uv sync --extra dev --group mom
uv run klt mom plates.gds plates.mom.json
```

Equivalently, without uv:

```bash
pip install maturin   # or: uv tool install maturin
cd native/mom
maturin develop --release   # builds + installs into the active venv
```

> **Rebuilding after a Rust change**: the crate's version does not change
> when you edit its source, so `uv sync` will happily reuse the cached wheel
> and your edit will appear to have no effect. Force it with
> `uv sync --extra dev --group mom --reinstall-package klt-mom-native`, or
> use `maturin develop` (which always rebuilds).

If `klt_mom_native` cannot be imported, `klt mom` fails cleanly with exit
code `1` and a message pointing back to this section — never a bare
`ImportError` traceback.

### Running the Rust tests

```bash
cd native/mom
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
```

`cargo test` works from the same manifest as the maturin build because
`Cargo.toml` deliberately does **not** enable pyo3's `extension-module`
feature; `native/mom/pyproject.toml`'s `[tool.maturin] features` turns it on
for the wheel build only. (With it always on, the `cargo test` binary has no
interpreter to resolve Python's symbols against and fails to link.)

## Spec file

The spec file is a JSON object:

```json
{
  "background_permittivity": 3.9,
  "panel_size_um": 0.5,
  "stackup": [
    { "layer": "1/0", "conductor": "top",    "z0_um": 1.0, "z1_um": 1.0 },
    { "layer": "2/0", "conductor": "bottom", "z0_um": 0.0, "z1_um": 0.0 }
  ]
}
```

- `background_permittivity` (required, number) — relative permittivity of
  the uniform dielectric surrounding every conductor. The MVP solves a
  single homogeneous medium (see "Scope and limitations" below).
- `panel_size_um` (optional, number, default `0.5`) — target discretisation
  panel edge length in micrometers. Smaller values give more panels (more
  accurate, more expensive); the solver rejects a request whose panel count
  would exceed an internal safety cap (see "Panel-count guard" below) rather
  than risk exhausting memory. **Keep it at or below the smallest
  conductor-to-conductor separation in the geometry** — a coarser value
  breaks the point-collocation fill down and is reported in `warnings` (see
  "Warnings" below).
- `compute_inductance` (optional, bool, default `false`) — opt into the PEEC
  partial-inductance/DC-resistance solve alongside capacitance. See "PEEC
  inductance/resistance" below for the (narrower) geometric scope this
  requires.
- `filament_size_um` (optional, number, default `1.0`) — target PEEC
  cross-section filament edge length in micrometers. Only consulted when
  `compute_inductance` is set; same "smaller is more accurate, more
  expensive" tradeoff as `panel_size_um`.
- `stackup` (required, non-empty array) — each entry maps one GDS
  `(layer, datatype)` pair's shapes to an electrical conductor's z-extent:
  - `layer` (string, `"<layer>/<datatype>"`) — the GDS layer/datatype to
    read shapes from.
  - `conductor` (string) — the electrical node name. **Multiple entries may
    share the same `conductor` name** — their shapes merge into one
    electrical conductor. This is how a conductor spread across several GDS
    layers (e.g. a coax-style shield's four wall segments, each its own
    layer) is expressed as a single node; see "Worked example: coax" below.
    (PEEC's bar-shaped-conductor restriction below means a
    `compute_inductance: true` request cannot use this to merge several
    boxes into one conductor — see "PEEC inductance/resistance".)
  - `z0_um`/`z1_um` (numbers) — the conductor's z-extent at this layer.
    `z0_um == z1_um` models an idealised zero-thickness flat plate (the
    common case for a simple parallel-plate test); `z1_um > z0_um` models a
    conductor with real thickness (all six faces of the resulting
    rectangular prism are discretised).
  - `conductivity_S_per_m` (optional, number) — the conductor's bulk
    conductivity, siemens per meter. **Required** (and used only) when
    `compute_inductance` is set, to compute this conductor's DC resistance.
    If several `stackup` entries share a `conductor` name, they must agree
    on this value everywhere it is set (a `conductor`'s conductivity cannot
    differ by layer).

Every shape on a `stackup` entry's GDS layer, within the selected top cell,
is read via its **axis-aligned bounding box** — not its exact outline. This
is the MVP's headline geometric simplification: a non-rectangular or
rotated shape is discretised as its bbox, not its true footprint. It is
adequate for the rectangle-dominated MOM-cap and coax-approximation test
geometries this issue targets; general-polygon discretisation is a
follow-up (see "Scope and limitations").

A `stackup` entry naming a layer absent from the given layout is not itself
an error (a shared spec can list layers a particular fixture doesn't use);
but every named `conductor` must end up with **at least one** matched shape,
or the command fails with a clear "matched no shapes" error.

## JSON schema (the contract)

**JSON is the API.** See [`docs/json-contract.md`](../json-contract.md) for
the shared envelope (`schema_version`, error shape, exit codes).

```json
{
  "schema_version": 2,
  "file": "coupled_lines.gds",
  "spec": "coupled_lines.mom.json",
  "background_permittivity": 3.9,
  "panel_size_um": 0.5,
  "conductors": ["top", "bottom"],
  "capacitance_matrix_ff": [
    [0.386710, -0.184208],
    [-0.184208, 0.386710]
  ],
  "panel_count": 32,
  "warnings": []
}
```

| Field                     | Type              | Description                                                                                     |
| ------------------------- | ----------------- | ------------------------------------------------------------------------------------------------- |
| `schema_version`          | integer           | Version of this command's JSON shape. `1` = capacitance-only (#718/#719); `2` = adds the PEEC fields below, each present only when `compute_inductance` was set (#797). |
| `file`                    | string            | The input layout path exactly as provided.                                                        |
| `spec`                    | string            | The spec file path exactly as provided.                                                           |
| `background_permittivity` | number            | Resolved from the spec (echoed for provenance).                                                   |
| `panel_size_um`           | number            | Resolved panel size — the spec's value, or the `0.5` default when omitted.                        |
| `conductors`              | array\<string\>   | Conductor names, in the same order as `capacitance_matrix_ff`'s (and, when present, `inductance_matrix_nh`'s) rows/columns (first-seen order in `stackup`). |
| `capacitance_matrix_ff`   | array\<array\<number\>\> | The Maxwell (short-circuit) capacitance matrix, in femtofarads — see "Reading the matrix" below. |
| `panel_count`             | integer           | Total discretisation panel count across every conductor (informational).                          |
| `warnings`                | array\<string\>   | Non-fatal physicality diagnostics — empty on a well-resolved solve. See "Warnings" below.          |
| `filament_size_um`        | number            | **Only present when `compute_inductance: true`.** Resolved PEEC filament size — the spec's value, or the `1.0` default. |
| `inductance_matrix_nh`    | array\<array\<number\>\> | **Only present when `compute_inductance: true`.** The PEEC partial-inductance matrix, in nanohenries — see "PEEC inductance/resistance" below. |
| `resistance_ohm`          | array\<number\>   | **Only present when `compute_inductance: true`.** Per-conductor DC resistance, in ohms, same order as `conductors`. |
| `filament_count`          | integer           | **Only present when `compute_inductance: true`.** Total PEEC filament count across every conductor (informational, mirrors `panel_count`). |

### Reading the matrix

`capacitance_matrix_ff[j][k]` is the charge (in femtofarads, i.e. per volt)
induced on conductor `j` when conductor `k` is held at 1V and every other
conductor is grounded — the standard multiconductor Maxwell capacitance
matrix (`Q_j = sum_k C_jk * V_k`). The matrix is symmetric
(`C_jk == C_kj`, up to solver floating-point noise) for this reciprocal
system. Diagonal entries are positive; off-diagonal entries are typically
**negative** — bringing one conductor to 1V induces opposite-sign charge on
a grounded neighbour. This is the raw solver output, not yet a SPICE
"coupling capacitance" convention (`C_coupling(j,k) = -C_jk`); a consumer
wiring this into a netlist should apply that sign flip itself.

### Warnings

A Maxwell capacitance matrix of any physical multiconductor system has a
**positive diagonal** and **non-positive off-diagonal** entries. When the
solve returns a matrix violating either property, `klt mom` still returns
the numbers (this command's bar is "produces a numeric result") but records
one entry in `warnings` per violation, naming the conductors involved and
the knob to change. Exit code stays `0` — a warning is a diagnostic, not a
failure.

In practice a warning always means the same thing: **`panel_size_um` is too
coarse relative to the smallest conductor-to-conductor separation.** Two
panels on facing conductors are then much closer to each other than the
panels are wide, and the centroid-to-centroid `1/r` kernel (see "Scope and
limitations") wildly overestimates their coupling — badly enough to flip the
mutual term's sign. As a rule of thumb, keep `panel_size_um` at or below the
smallest gap you care about resolving, and re-run with a smaller value to
confirm the answer has stopped moving. Convergence-under-refinement is
validated systematically in
[`docs/design/mom-validation.md`](../design/mom-validation.md) (#719), which
also measures what a given `panel_size_um`/gap ratio costs you in accuracy:
`panel_size_um = gap` lands within ~1% of the converged answer on a
parallel-plate fixture, `gap/2` within ~0.1%.

```
$ klt mom plates.gds coarse.mom.json
...
(femtofarads)

warnings:
  mutual capacitance between conductors "top" and "bottom" is 0.118396 fF, but a
  physical Maxwell capacitance matrix has non-positive off-diagonal entries -- the
  point-collocation fill has broken down, most likely because panel_size_um is
  coarse relative to the spacing between these conductors; reduce panel_size_um
  and re-run
```

## PEEC inductance/resistance

Set `compute_inductance: true` in the spec file to also extract
**partial self/mutual inductance** and **DC resistance**, via PEEC (Partial
Element Equivalent Circuit) filaments — Ruehli's classical formulation
("Inductance Calculations in a Complex Integrated Circuit Environment", IBM
J. Res. Dev. 16(5), 1972). Unlike the capacitance solve (which discretises
each conductor's outer *surface* into panels), PEEC needs *volumetric*
current-carrying filaments, spanning each conductor's full length along its
current-flow axis. That requires a materially narrower geometric scope than
the capacitance solve's — read this section before turning it on.

### The bar-shaped-conductor MVP restriction

`compute_inductance: true` requires every conductor in the request to reduce
to a single well-defined current-flow "bar":

- **Exactly one box per conductor.** A conductor merged from several
  `stackup` entries (e.g. the coax shield's four wall segments in "Worked
  example: coax" below) has no single well-defined bar cross-section under
  this MVP's model, and is rejected with a clear error. Multi-box PEEC
  (Ruehli's general mesh) is a follow-up.
- **A true 3-D bar.** All three of a box's extents (x, y, z) must be
  non-zero — a flat, zero-thickness plate (fine for capacitance) has no
  cross-sectional area to carry current.
- **Bar-shaped, not cubic.** The box's longest extent (the current-flow
  axis — the MVP's simplest defensible choice: **current flows along the
  box's longest axis**, matching how bar/wire conductors are treated in
  introductory PEEC codes) must be at least 3x each of the other two
  extents. A conductor closer to square/cubic than that (e.g. a pad or a
  via) has no well-defined single current-flow direction under this MVP's
  model and is rejected — this mirrors how the capacitance solver's own
  "Scope and limitations" documents *its* MVP simplifications rather than
  silently returning a number that doesn't mean what it looks like it means.
- **Every conductor must share the same current-flow axis and the same
  axial extent** (start/end coordinate along that axis). This is what lets
  the mutual-inductance formula below (parallel, equal-length, aligned
  filaments) apply directly; a request mixing axes, or with offset/unequal
  bar lengths (e.g. an L-shaped loop, or two loop sides that don't line up
  end-to-end), is rejected. The general unequal-length/off-axis Neumann
  formula is a follow-up.
- Every conductor must set `conductivity_S_per_m` (used for DC resistance;
  see "Spec file" above).

A rectangular loop (two long, parallel, aligned bars — "Worked example:
straight wire and loop" below) and a single straight bar both satisfy this
scope; a coax shield, a pad, or an L-shaped trace do not (yet).

### Method: filament bundle + Neumann's formula

Each PEEC-eligible conductor's cross-section is discretised into a grid of
filaments (target edge length `filament_size_um`), each spanning the full
bar length. The partial mutual inductance between two parallel, equal-length,
axially-aligned filaments separated by perpendicular distance `d` is the
closed-form Neumann double integral:

```
M(l, d) = (mu0 / 2*pi) * l * [ asinh(l/d) - sqrt(1 + (d/l)^2) + d/l ]
```

A filament's own self term is computed exactly, via Hoer & Love's closed
form for the partial inductance of a rectangular bar against itself (rather
than substituting an equivalent-circle self geometric mean distance into the
formula above, which over-predicted self inductance by a systematic ~0.3%
— see [issue #836](https://github.com/2AMLogic/klayout-tools/issues/836)).
A conductor's partial self inductance, and the partial mutual inductance
between two conductors, are each the plain average of every pairwise
filament term within (or between) their filament bundles — the standard
PEEC bundle-of-filaments technique. This combination reproduces two
independent textbook closed forms in the thin-wire limit (Rosa's
straight-wire self-inductance, and the classical two-wire transmission-line
loop inductance) — see `native/mom/src/peec.rs`'s module docs for the full
derivation and `docs/design/mom-validation.md`'s "Inductance/resistance"
section for the measured validation against both.

DC resistance needs no approximation: `R = length / (conductivity *
cross_sectional_area)` (Ohm's law), computed directly from the same bar
geometry.

### Reading the inductance matrix

`inductance_matrix_nh[j][k]` is conductor `j`'s partial self inductance
(`j == k`) or the partial mutual inductance between conductors `j` and `k`
(`j != k`), in nanohenries, in the Ruehli PEEC sense — a **partial**
inductance has no implied current-return path; it becomes physically
meaningful once combined into a loop (e.g. `L_loop = L[0][0] + L[1][1] -
2*L[0][1]` for a two-conductor "go/return" loop). The matrix is symmetric.
`resistance_ohm[j]` is conductor `j`'s DC resistance in ohms.

## Worked example: parallel plate

```bash
klt mom plates.gds plates.mom.json --format json
```

with `plates.mom.json`:

```json
{
  "background_permittivity": 3.9,
  "panel_size_um": 0.5,
  "stackup": [
    { "layer": "1/0", "conductor": "top",    "z0_um": 1.0, "z1_um": 1.0 },
    { "layer": "2/0", "conductor": "bottom", "z0_um": 0.0, "z1_um": 0.0 }
  ]
}
```

`plates.gds` has two identical 2x2 um squares on layers `1/0` and `2/0`
(1 um vertically apart) — the two idealised flat plates. See
`tests/test_mom.py`'s `_parallel_plate_fixture` for the exact fixture this
example is built from.

## Worked example: coax (square-wall approximation)

The MVP's rectangle-only discretisation (see "Spec file" above) cannot
express a true circular/annular coax cross-section. A coax-style structure
is instead approximated as a square inner conductor surrounded by four
rectangular wall segments (north/south/east/west), each its own GDS layer,
all mapped to the same `conductor` name so they act as one electrical
shield:

```json
{
  "background_permittivity": 1.0,
  "panel_size_um": 0.5,
  "stackup": [
    { "layer": "5/0",  "conductor": "inner", "z0_um": 0.0, "z1_um": 0.5 },
    { "layer": "10/0", "conductor": "outer", "z0_um": 0.0, "z1_um": 0.5 },
    { "layer": "11/0", "conductor": "outer", "z0_um": 0.0, "z1_um": 0.5 },
    { "layer": "12/0", "conductor": "outer", "z0_um": 0.0, "z1_um": 0.5 },
    { "layer": "13/0", "conductor": "outer", "z0_um": 0.0, "z1_um": 0.5 }
  ]
}
```

See `tests/test_mom.py`'s `_coax_fixture` for the exact wall geometry.

## Worked example: straight wire and loop

`compute_inductance: true` on a single 100x2x2 µm bar (self-inductance and
resistance only):

```json
{
  "background_permittivity": 1.0,
  "panel_size_um": 5.0,
  "compute_inductance": true,
  "filament_size_um": 0.5,
  "stackup": [
    {
      "layer": "1/0", "conductor": "wire",
      "z0_um": 0.0, "z1_um": 2.0,
      "conductivity_S_per_m": 5.96e7
    }
  ]
}
```

A rectangular loop — two long, parallel, aligned 4x4 µm bars 60 µm apart,
`"go"` and `"return"` — additionally exercises the mutual term (the loop's
own inductance is `L[0][0] + L[1][1] - 2*L[0][1]`):

```json
{
  "background_permittivity": 1.0,
  "panel_size_um": 10.0,
  "compute_inductance": true,
  "filament_size_um": 0.5,
  "stackup": [
    {
      "layer": "1/0", "conductor": "go",
      "z0_um": 0.0, "z1_um": 4.0,
      "conductivity_S_per_m": 5.96e7
    },
    {
      "layer": "2/0", "conductor": "return",
      "z0_um": 0.0, "z1_um": 4.0,
      "conductivity_S_per_m": 5.96e7
    }
  ]
}
```

See `tests/test_mom.py`'s `_wire_fixture`/`test_run_mom_peec_loop`, and
`tests/test_mom_peec_validation.py` (which runs both against their closed-form
oracles), for the exact geometry and measured accuracy.

## Scope and limitations

- **Rectangular geometry only (MVP).** Every conductor is discretised from
  the axis-aligned bounding box of each matched GDS shape, not its exact
  polygon outline. Non-rectangular or rotated shapes are approximated by
  their bbox — a known, documented simplification, not a bug. General
  polygon discretisation is a natural follow-up once needed.
- **Single homogeneous dielectric.** `background_permittivity` is one value
  for the whole solve; there is no per-layer dielectric stack (e.g. a real
  oxide/nitride stack with different permittivities per layer). Multi-layer
  dielectric support is a follow-up.
- **Simplified point-collocation BEM fill.** Off-diagonal
  potential-coefficient entries use the bare point-charge kernel between
  panel centroids rather than a true panel-to-panel double integral; the
  diagonal (self) term uses the standard closed-form equivalent-square-panel
  approximation. This is adequate for "produces a numeric result" (this
  command's bar); accuracy-vs-refinement is measured in
  [`docs/design/mom-validation.md`](../design/mom-validation.md). The kernel's
  known failure mode — panels wider than the gap they face — is detected and
  surfaced in `warnings` rather than returned silently (see "Warnings").
- **No ports/S-parameters, no frequency dependence.** Both the capacitance
  and the PEEC inductance/resistance solve are quasi-static (DC/low-frequency)
  — no ports, no skin effect, no S-parameters. Those are separate, later
  phases of epic #701.
- **PEEC inductance/resistance is bar-shaped-conductors-only (MVP).** See
  "PEEC inductance/resistance" above's "bar-shaped-conductor MVP restriction"
  — a materially narrower scope than the capacitance solve's (single box,
  true 3-D, elongated, shared axis/extent across every conductor in the
  request). A request that does not fit is rejected with a clear error
  naming which restriction it violates, not silently approximated.

### Panel-count guard

The dense potential-coefficient matrix is `O(n^2)` in memory. A request whose
discretisation would exceed an internal 8000-panel cap is rejected with a
clear error before any allocation is attempted — most commonly triggered by a
`panel_size_um` sized for a small conductor being reused against a much
larger one in the same request (a scale mismatch). Increase `panel_size_um`,
or split the request, to work around it.

### The matrix solve

The solve step is preconditioned Conjugate Gradient (Jacobi/diagonal
preconditioner), not a direct (LU) factorisation — the potential-coefficient
matrix is symmetric positive definite by construction, and CG converges in
well under `n` iterations on the geometries this command targets, which is
materially cheaper at scale than the `O(n^3)` direct solve it replaced. See
[`docs/design/mom-iterative-solver.md`](../design/mom-iterative-solver.md)
([#799](https://github.com/2AMLogic/klayout-tools/issues/799)) for why CG
(not GMRES) is the solver appropriate to this system, the measured
convergence rate, and the solve-time comparison against the direct solve at a
larger-than-MVP conductor count.

### Filament-count guard

The PEEC mutual-inductance fill is `O(f^2)` in the total filament count `f`
across every conductor (cheaper than the capacitance solve's dense `O(n^3)`
solve, but still unbounded without a guard). A `compute_inductance: true`
request whose filament discretisation would exceed an internal 6000-filament
cap is rejected with a clear error before any allocation is attempted, the
same role the panel-count guard plays for capacitance. Increase
`filament_size_um` to work around it.

## Exit codes

| Exit code | Meaning                                                                                   |
| --------- | ------------------------------------------------------------------------------------------ |
| `0`       | Success — the capacitance matrix (and, if requested, the PEEC inductance/resistance) was computed and returned. |
| `1`       | Failed to run: layout/spec file not found or unreadable, a `stackup` entry matched no shapes, ambiguous top cell (pass `--top`), the `klt_mom_native` extension is not installed, or a solver-level failure (e.g. a singular potential-coefficient matrix, the panel-count guard above, a `compute_inductance: true` conductor that does not satisfy the PEEC bar-shape scope, a missing `conductivity_S_per_m`, or the filament-count guard above). |
| `2`       | Usage error (argparse) — missing/invalid arguments.                                        |

## See also

- [`docs/design/em-field-sim-spike.md`](../design/em-field-sim-spike.md) —
  the original E&M field-sim survey (issue #103) that ranked quasi-static
  capacitance extraction as a high-value use case and separately noted a
  lighter-weight alternative (reusing geode-fem's quasi-static solver mode)
  worth revisiting if the cost/accuracy tradeoff argues for it.
- [#701](https://github.com/2AMLogic/klayout-tools/issues/701) — the parent
  Method-of-Moments epic (later phases: ports, coupling, general PEEC mesh).
- [#797](https://github.com/2AMLogic/klayout-tools/issues/797) — delivered the
  PEEC inductance/resistance solve documented above.
- [`docs/design/mom-validation.md`](../design/mom-validation.md) — closed-form
  validation and convergence-under-refinement for both the capacitance solver
  ([#719](https://github.com/2AMLogic/klayout-tools/issues/719)) and the PEEC
  inductance/resistance solve (#797): the analytic oracles, the measured
  agreement, and the stated tolerances.
- [`docs/design/mom-iterative-solver.md`](../design/mom-iterative-solver.md) —
  the iterative (preconditioned Conjugate Gradient) solve step
  ([#799](https://github.com/2AMLogic/klayout-tools/issues/799)): why CG over
  GMRES, the preconditioner, and the measured convergence rate and solve-time
  comparison against the direct solve it replaced.
