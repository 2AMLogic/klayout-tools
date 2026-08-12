# `klt mom`

Quasi-static **RLC** extraction from a GDSII/OASIS layout:

- **C** — the Method of Moments (MoM): discretise conductor surfaces, fill the
  potential-coefficient matrix, and solve for the Maxwell capacitance matrix.
- **L, R** — PEEC (Partial Element Equivalent Circuit): discretise the same
  conductors into current-carrying *filaments*, fill the partial-inductance
  matrix in closed form, and sum each conductor's DC series resistance.
  **Opt-in** — see `conductivity_S_per_m` in "Spec file" below.

Phase 0/1 of the Method-of-Moments epic ([#701](https://github.com/2AMLogic/klayout-tools/issues/701)),
delivered by [#718](https://github.com/2AMLogic/klayout-tools/issues/718)
(capacitance) and [#797](https://github.com/2AMLogic/klayout-tools/issues/797)
(inductance/resistance).
This command's own bar is "produces a numeric result".
How close those numbers are to the true answer is a separate question,
answered by [`docs/design/mom-validation.md`](../design/mom-validation.md)
([#719](https://github.com/2AMLogic/klayout-tools/issues/719)): the solver is
checked against the parallel-plate and coaxial closed forms, against the
mean-distance asymptote for a straight bar's partial self inductance and the
exact `l/(σA)` resistance, and shown to converge under mesh refinement. Read
it before trusting a number from here — in particular for what
`panel_size_um` you need for a given accuracy.

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
narrower, Rust-internal contract (axis-aligned box lists in micrometers, a
background permittivity and optional per-conductor conductivities, in;
capacitance / partial-inductance matrices and resistances, out) — the Python layer
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

The spec file is a JSON object with three keys:

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

…plus two optional keys that turn on the PEEC (inductance/resistance) path:

```json
{
  "background_permittivity": 3.9,
  "panel_size_um": 0.2,
  "filament_subdivisions": 1,
  "stackup": [
    { "layer": "1/0", "conductor": "sig", "z0_um": 0.0, "z1_um": 0.5,
      "conductivity_S_per_m": 5.8e7 },
    { "layer": "2/0", "conductor": "gnd", "z0_um": 0.0, "z1_um": 0.5,
      "conductivity_S_per_m": 5.8e7 }
  ]
}
```

- `background_permittivity` (required, number) — relative permittivity of
  the uniform dielectric surrounding every conductor. The MVP solves a
  single homogeneous medium (see "Scope and limitations" below).
- `panel_size_um` (optional, number, default `0.5`) — target discretisation
  panel edge length in micrometers. Smaller values give more panels (more
  accurate, more expensive); the solver rejects a request whose panel count
  would exceed an internal safety cap (see "Panel-count and filament-count guards" below) rather
  than risk exhausting memory. **Keep it at or below the smallest
  conductor-to-conductor separation in the geometry** — a coarser value
  breaks the point-collocation fill down and is reported in `warnings` (see
  "Warnings" below).
- `stackup` (required, non-empty array) — each entry maps one GDS
  `(layer, datatype)` pair's shapes to an electrical conductor's z-extent:
  - `layer` (string, `"<layer>/<datatype>"`) — the GDS layer/datatype to
    read shapes from.
  - `conductor` (string) — the electrical node name. **Multiple entries may
    share the same `conductor` name** — their shapes merge into one
    electrical conductor. This is how a conductor spread across several GDS
    layers (e.g. a coax-style shield's four wall segments, each its own
    layer) is expressed as a single node; see "Worked example: coax" below.
  - `z0_um`/`z1_um` (numbers) — the conductor's z-extent at this layer.
    `z0_um == z1_um` models an idealised zero-thickness flat plate (the
    common case for a simple parallel-plate test); `z1_um > z0_um` models a
    conductor with real thickness (all six faces of the resulting
    rectangular prism are discretised).
  - `conductivity_S_per_m` (optional, number) — the conductor's bulk DC
    conductivity in siemens per metre (copper ≈ `5.8e7`, aluminium ≈
    `3.77e7`). **Declaring it on any entry switches the whole solve into
    PEEC mode** (see below). Conductivity is a property of the *conductor*,
    not of the layer, so when several `stackup` entries share a `conductor`
    name they must all give the same value (a disagreement is rejected
    rather than silently resolved).
- `filament_subdivisions` (optional, integer, default `1`, max `8`) —
  sub-filaments per axis for the PEEC discretisation; each box becomes
  `filament_subdivisions³` filaments. Consulted only in PEEC mode. Because
  the partial-element integrals are evaluated in closed form, refining this
  is a **self-consistency** knob, not an accuracy knob — it must reproduce
  the same numbers (measured drift under refinement: `< 8e-11` relative, see
  [`docs/design/mom-validation.md`](../design/mom-validation.md)). Raise it
  only when you want the finer mesh for its own sake; the fill is dense and
  `O(n_filaments²)`, and a total above 512 filaments is rejected.

### Turning on inductance and resistance (PEEC mode)

`klt mom` extracts capacitance only unless a `conductivity_S_per_m` appears
somewhere in the `stackup`. That is deliberate rather than a default:
the capacitance path happily accepts a zero-thickness plate, which has no
cross-section to push current through, so silently inventing a current path
for one would be worse than not extracting an inductance at all.

In PEEC mode:

- **Every** conductor must declare a conductivity. A partial declaration is
  an error, not an implicit "perfect conductor" — a silently-zero resistance
  is exactly the kind of number that gets trusted by mistake.
- Every box must have a real extent along all three axes. A
  `z0_um == z1_um` plate is rejected with a message telling you to give it a
  thickness or drop the conductivity.
- The response gains `inductance_matrix_nh`, `resistance_ohm` and
  `filament_count` (see "JSON schema" below). Without it they are `null` /
  `0` and the capacitance result is byte-for-byte what it was before.

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

Below is a real solve, not an illustration: two 100 × 1 × 1 µm copper bars on
a 10 µm pitch in vacuum (`tests/test_mom_inductance.py`'s fixture).

```json
{
  "schema_version": 2,
  "file": "coupled_lines.gds",
  "spec": "coupled_lines.mom.json",
  "background_permittivity": 1.0,
  "panel_size_um": 1.0,
  "filament_subdivisions": 1,
  "conductors": ["sig", "gnd"],
  "capacitance_matrix_ff": [
    [1.409361, -0.596003],
    [-0.596003, 1.409361]
  ],
  "inductance_matrix_nh": [
    [0.102172, 0.041866],
    [0.041866, 0.102172]
  ],
  "resistance_ohm": [1.724138, 1.724138],
  "panel_count": 804,
  "filament_count": 2,
  "warnings": []
}
```

| Field                     | Type              | Description                                                                                     |
| ------------------------- | ----------------- | ------------------------------------------------------------------------------------------------- |
| `schema_version`          | integer           | Version of this command's JSON shape (`2` since [#797](https://github.com/2AMLogic/klayout-tools/issues/797) added the inductance/resistance fields). |
| `file`                    | string            | The input layout path exactly as provided.                                                        |
| `spec`                    | string            | The spec file path exactly as provided.                                                           |
| `background_permittivity` | number            | Resolved from the spec (echoed for provenance).                                                   |
| `panel_size_um`           | number            | Resolved panel size — the spec's value, or the `0.5` default when omitted.                        |
| `filament_subdivisions`   | integer \| null   | Resolved PEEC subdivision count, or `null` on a capacitance-only solve (so a report never implies a knob that was not consulted). |
| `conductors`              | array\<string\>   | Conductor names, in the same order as every matrix's rows/columns and as `resistance_ohm` (first-seen order in `stackup`). |
| `capacitance_matrix_ff`   | array\<array\<number\>\> | The Maxwell (short-circuit) capacitance matrix, in femtofarads — see "Reading the capacitance matrix" below. |
| `inductance_matrix_nh`    | array\<array\<number\>\> \| null | The PEEC **partial**-inductance matrix, in nanohenries; `null` unless PEEC mode was requested. See "Reading the inductance matrix" below. |
| `resistance_ohm`          | array\<number\> \| null | Per-conductor DC series resistance in ohms; `null` unless PEEC mode was requested. |
| `panel_count`             | integer           | Total discretisation panel count across every conductor (informational).                          |
| `filament_count`          | integer           | Total PEEC filament count across every conductor (`0` on a capacitance-only solve) — the inductance-side analogue of `panel_count`. |
| `warnings`                | array\<string\>   | Non-fatal physicality/geometry diagnostics — empty on a well-resolved solve. See "Warnings" below.          |

### Reading the capacitance matrix

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

### Reading the inductance matrix

`inductance_matrix_nh[j][k]` is the **partial** mutual inductance between
conductor `j`'s and conductor `k`'s current paths, in nanohenries; the
diagonal is each conductor's partial *self* inductance. The matrix is
symmetric by construction (only the upper triangle of filament pairs is
evaluated), and its entries are positive for parallel conductors whose
current-flow axes point the same way.

**"Partial" is load-bearing.** In Ruehli's formulation a partial inductance
is defined for a conductor *segment on its own*, with no reference to a
return path — the return path's contribution is a separate partial element
that a caller adds in when it closes the circuit. A single number from this
matrix is therefore **not** a loop inductance and not what you would measure
on a physical two-terminal structure. The loop inductance of a signal
returning on a ground conductor is `L_loop = Lp_ss + Lp_gg − 2 Lp_sg`; other
topologies compose differently. Feeding a raw diagonal entry into a netlist
as "the inductance of this wire" will over-predict it, usually badly.

Two consequences of the MVP's geometry model worth stating plainly:

- **Current flows along each box's longest axis.** `BoxRequest` is six
  coordinates and carries no port or direction information, so the direction
  has to be inferred, and "current runs the long way down a wire" is the
  standard PEEC reading for a bar-shaped conductor. A box whose longest
  extent is less than **2×** its next-longest is not bar-shaped (a square
  pad has no long way), and gets a `warnings` entry naming the conductor
  rather than a hard error — the same "return the numbers, say plainly they
  should not be trusted" convention the capacitance path's physicality
  warnings use.
- **Perpendicular segments do not couple.** The Neumann integrand carries a
  `dl_a · dl_b` factor, so the partial mutual inductance between an x-running
  and a y-running filament is exactly zero. An L-shaped path needs no special
  handling; it also means a conductor made of boxes along different axes gets
  no cross-terms between them, which is correct for partial inductance and
  *not* the same as the loop inductance of the bend.

### Reading the resistances

`resistance_ohm[j]` is conductor `j`'s DC series resistance,
`Σ_boxes l / (σ · A)` with `l` the box's length along its current-flow axis
and `A` its cross-section perpendicular to it. This is exact for a straight
bar (no approximation is involved) and is independent of
`filament_subdivisions` to round-off.

**The boxes of one conductor are assumed to be in series.** That is the usual
case for a routed net expressed as a chain of segments, but the MVP has no
connectivity information, so boxes that are actually in *parallel* have their
resistance over-counted rather than combined. It is DC only: there is no skin
or proximity effect, so the number is a low-frequency floor.

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
- **Partial elements, not loop quantities.** The inductance matrix holds
  Ruehli *partial* inductances, defined per segment without a return path;
  turning them into a loop inductance is the caller's job (see "Reading the
  inductance matrix"). There is no mesh/loop reduction, no port definition,
  and no `L`-`C` co-solve producing an impedance.
- **Current direction is inferred from box shape.** Each box's longest axis
  is taken as its current-flow axis; a box that is not bar-shaped (aspect
  ratio below 2:1) is warned about, not rejected. A conductor whose real
  current path does not follow its bounding boxes' long axes — a bend
  expressed as one square box, a plane, a via array — is outside what this
  MVP models.
- **Conductor boxes are assumed to be in series** for resistance. Parallel
  boxes are over-counted; the MVP has no connectivity to distinguish them.
- **DC only.** No skin effect, no proximity effect, no frequency dependence,
  no S-parameters. `resistance_ohm` is a low-frequency floor and the partial
  inductances are the DC (uniform current density) limit. Frequency-dependent
  extraction is a later phase of epic #701.

### Panel-count and filament-count guards

The dense potential-coefficient matrix is `O(n^2)` in memory and `O(n^3)` in
solve time. A request whose discretisation would exceed an internal 8000-panel
cap is rejected with a clear error before any allocation is attempted —
most commonly triggered by a `panel_size_um` sized for a small conductor
being reused against a much larger one in the same request (a scale
mismatch). Increase `panel_size_um`, or split the request, to work around it.

The PEEC path has its own, much tighter cap: **512 filaments**. The
partial-inductance matrix is dense and `O(n²)` to fill, each entry costing a
64-term closed-form evaluation, so the cap is sized for a fill that takes
about a second rather than for a matrix that fits in memory. Since
`filament_subdivisions` multiplies the box count by `n³`, that knob is the
usual way to hit it.

## Exit codes

| Exit code | Meaning                                                                                   |
| --------- | ------------------------------------------------------------------------------------------ |
| `0`       | Success — the matrices were computed and returned.                                         |
| `1`       | Failed to run: layout/spec file not found or unreadable, a `stackup` entry matched no shapes, ambiguous top cell (pass `--top`), the `klt_mom_native` extension is not installed, a bad `conductivity_S_per_m` / `filament_subdivisions` in the spec, a PEEC request whose geometry cannot carry current (a zero-thickness plate) or that declares a conductivity on only some conductors, or a solver-level failure (e.g. a singular potential-coefficient matrix, or either count guard above). |
| `2`       | Usage error (argparse) — missing/invalid arguments.                                        |

## See also

- [`docs/design/em-field-sim-spike.md`](../design/em-field-sim-spike.md) —
  the original E&M field-sim survey (issue #103) that ranked quasi-static
  capacitance extraction as a high-value use case and separately noted a
  lighter-weight alternative (reusing geode-fem's quasi-static solver mode)
  worth revisiting if the cost/accuracy tradeoff argues for it.
- [#701](https://github.com/2AMLogic/klayout-tools/issues/701) — the parent
  Method-of-Moments epic (later phases: ports, coupling, resistance/inductance).
- [`docs/design/mom-validation.md`](../design/mom-validation.md) — closed-form
  validation and convergence-under-refinement for this solver
  ([#719](https://github.com/2AMLogic/klayout-tools/issues/719)): the analytic
  oracles, the measured agreement, and the stated tolerances.
