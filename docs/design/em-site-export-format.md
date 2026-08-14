# geode-fem site export format (Epic #840 Phase 1a)

Issue [#849](https://github.com/2AMLogic/klayout-tools/issues/849), Phase 1a
of Epic [#840](https://github.com/2AMLogic/klayout-tools/issues/840) ("real
E&M results in the browser on klayout-tools.org — integrate geode-fem
(WebGPU)"). This document defines the JSON export format a
[geode-fem](https://github.com/rjwalters/geode-fem) solver run is converted
into for browser display, and records the one real generated instance this
issue ships: the patch-antenna benchmark.

**Scope note**: this issue defines the format and generates one export. It
does not build the gallery UI ([#851](https://github.com/2AMLogic/klayout-tools/issues/851),
an unapproved Architect proposal) or wire `FieldViewer` up to consume it
live ([#850](https://github.com/2AMLogic/klayout-tools/issues/850), not yet
built) — those are separate, sibling issues.

## Why this shape

Three things had to be reconciled:

1. **`FieldViewer` already exists** ([#841](https://github.com/2AMLogic/klayout-tools/issues/841),
   `site/src/components/field/`) and defines its own prop shape,
   `FieldViewerData = { mesh: FieldMesh, frames: FieldFrame[] }`
   (`site/src/components/field/types.ts`). That component has no dependency
   on geode-fem — it renders whatever mesh+field data it's handed — but its
   types.ts docstring says it was "shaped to match the geode-fem
   browser-EM epic's (#840) Phase-1a export format so #850 can feed it
   directly without a translation layer." This export's `mesh`/`frames`
   fields are therefore a **strict superset** of `FieldMesh`/`FieldFrame`:
   pulling `{mesh, frames: [{label, scalar, vector}, ...]}` out of an export
   file validates directly as `FieldViewerData`.
2. **A solver-run record needs more than a viewer prop.** S-parameters,
   the NTFF radiation pattern, and full reproducibility provenance
   (solver commit, geometry, solve parameters) have no place in a generic
   mesh+field viewer's prop shape, and shouldn't — `FieldViewer` stays
   agnostic to where its data came from. So the export nests the viewer-
   compatible slice inside a larger document that also carries
   `s_parameters`, `radiation_pattern`, and `provenance`.
3. **"No illustrative/faked data."** Per the issue's Champion-approval
   comment, every number in the shipped export must trace to a real,
   reproducible geode-fem solver run — see "Generating the export" below.

The full schema (JSON Schema, draft 2020-12) is
[`docs/schemas/em-site-export.schema.json`](../schemas/em-site-export.schema.json).
This document is the narrative rationale; the schema file is the
machine-checked contract, validated in `tests/test_em_site_export.py`.

## Shape summary

```jsonc
{
  "schema_version": 1,
  "benchmark": "patch_antenna",
  "mesh": { "vertices": [[x, y, z], ...], "cells": [[i0, i1, i2], ...] },
  "frames": [
    { "label": "2.275 GHz", "frequency_hz": 2274530000.0, "scalar": [...], "vector": [[x,y,z], ...] }
  ],
  "s_parameters": {
    "ports": ["p1"],
    "reference_impedance_ohm": 50,
    "points": [ { "frequency_hz": ..., "s11_db": ..., "z_re_ohm": ..., "efficiency": ..., ... }, ... ],
    "resonance": { "f_res_hz": ..., "s11_dip_db": ..., "bandwidth_10db_hz": null, "bandwidth_10db_note": "..." }
  },
  "radiation_pattern": {
    "directivity_broadside_dbi": ..., "gain_broadside_dbi": ...,
    "cuts": { "e_plane": { "theta_deg": [...], "e_norm": [...] }, "h_plane": { ... } }
  },
  "provenance": {
    "generator": { "repo": "...", "commit": "<40-hex sha>", "version": "0.3.0", "backend": "...", "build_profile": "release" },
    "geometry": { "fixture": "tests/fixtures/patch_2g4.msh", "fixture_sha256": "<hex>", "description": "..." },
    "solve_parameters": { ... },
    "mesh_reduction": { "method": "...", "slice_z_mm": 0.8, "crop_bbox_mm": { "x": [...], "y": [...] } },
    "generated_at": "2026-08-12T04:12:16Z"
  }
}
```

### `mesh` / `frames` — the `FieldViewer`-compatible slice

For a volumetric FEM solve, `mesh` is **not** the solver's native
tetrahedral volume mesh — a tetrahedron's 4 vertices are not a planar
polygon, so dumping them into `FieldMesh.cells` verbatim would not be
renderable (and at tens of thousands of tets, would not be a
committable-sized asset either). Instead `mesh` is a **derived planar
cross-section**: a marching-tetrahedra slice through the volumetric solve
at a fixed height, linearly interpolating the real, solved per-node field
onto the new cut vertices. This is exactly the operation ParaView's own
"Slice" filter performs (which is how geode-fem's own near-field tearsheet
images are made, per its README) — a real, deterministic derivation of the
real solve, not a fabrication. Every scalar/vector sample in `frames[]`
traces to a linear interpolation between two real solved nodal E-field
values. The exact slice height/crop window used for a given export is
recorded in `provenance.mesh_reduction`, so the reduction is auditable, not
a silent lossy step.

### `s_parameters` — the frequency-swept port response

One entry in `points[]` per swept frequency, plus a `resonance` summary
(FEM resonant frequency, S11 dip, -10 dB bandwidth). `bandwidth_10db_hz` is
explicitly nullable: a sweep whose frequency grid doesn't bracket both -10
dB crossings around the dip cannot report a bandwidth without
extrapolating past its own data, so the field is `null` with a
`bandwidth_10db_note` explaining why — never a fabricated interpolation.

### `radiation_pattern` — NTFF

Optional (present when the benchmark exercises a near-to-far-field
transform, as the patch antenna does): broadside directivity, gain, and
E-/H-plane principal-plane radiation cuts (`theta_deg` vs. normalized
`|E|`).

### `provenance` — reproducibility

Mirrors the discipline of `klt`'s own shared `provenance` block
(`docs/json-contract.md`, `src/klayout_tools/_provenance.py`) and
`examples/sim/`'s generated-fixture convention (`examples/sim/generate.py`):
solver identity (`repo`/`commit`/`version`/`backend`/`build_profile`),
exact geometry (`fixture` path + `fixture_sha256`), the solve's boundary
conditions/material parameters, and (when `mesh`/`frames` are a derived
reduction, not the solver's raw output) exactly how that reduction was
computed. Fields that cannot be resolved are `null`, never fabricated —
the same rule `_provenance.py`'s docstring states for `klt`'s own block.

## Generating the export

`examples/em/patch_antenna/generate.py` is the reproducible generation
script (module docstring has the full detail). It drives a **separate,
locally-built** [rjwalters/geode-fem](https://github.com/rjwalters/geode-fem)
checkout (MIT; not vendored into this repo — see
[`docs/design/em-field-sim-spike.md`](em-field-sim-spike.md) Section 5,
"wrap the numerics, build the geometry pipeline," which this issue's Phase
1a does not yet touch since it consumes geode-fem's own bundled benchmark
fixture, not a klayout-tools-produced geometry) through its own
`examples/patch_antenna` benchmark binary:

```sh
git clone https://github.com/rjwalters/geode-fem.git /tmp/geode-fem
cd /tmp/geode-fem && cargo build -p patch_antenna --release
cd /path/to/klayout-tools
uv run python3 examples/em/patch_antenna/generate.py --geode-fem-dir /tmp/geode-fem
```

This runs three real solves against geode-fem's own bundled, validated
`tests/fixtures/patch_2g4.msh` fixture (built from its own
`reference/gmsh/patch_antenna.geo`, the same reference geometry the
Champion-approval comment on issue #849 cites): the 13-point S11 sweep
(`results.toml`), the NTFF radiation pattern (`pattern.toml`), and the
per-node driven near field (`E_patch.vtu`) — all at the identical FEM
resonant frequency on the identical fixture, so the shipped export's mesh,
S-parameters, and radiation pattern are facets of one physically
consistent antenna solve, not stitched together from mismatched runs.

## The shipped instance: `patch_antenna.em-export.json`

[`examples/em/patch_antenna/patch_antenna.em-export.json`](../../examples/em/patch_antenna/patch_antenna.em-export.json)
was generated exactly this way, from geode-fem commit
`90759f103fdbdc42e47b1941ccd8d0e0b031c4e6`, `geode-fem` workspace version
`0.3.0`, on the `burn::backend::NdArray<f64, i32>` CPU backend (no GPU
feature compiled in — the sandbox that generated this file had no GPU
device; geode-fem's own README documents this as its headless-CI fallback
path, so nothing about the physics differs from a GPU run, only wall-clock
time). Its `provenance` block carries the full detail; see that file
directly rather than duplicating the numbers here (they would drift out of
sync with the committed file otherwise).

Why the patch antenna: per the issue and its Champion-approval comment, it
is geode-fem's own validated benchmark that exercises S11, bandwidth, and
NTFF together, and has a recognizable geometry for a future gallery
(#851). The 13-point default sweep used here does not bracket a -10 dB
bandwidth (its own committed `results.toml` documents this — the shallow
-6 dB dip on this coarse grid); this is an honest, documented gap
(`s_parameters.resonance.bandwidth_10db_hz: null`), not a shortcoming that
blocks this issue — the schema fully supports a bracketed bandwidth
number (geode-fem's own finer 21-point `results_matched.toml` sweep
demonstrates one, `bw_10db_ghz = 3.87e-2` GHz), and a future export can
choose that finer sweep without any schema change.

## Regenerating / validating

```sh
uv run python3 examples/em/patch_antenna/generate.py --geode-fem-dir /tmp/geode-fem
uv run pytest tests/test_em_site_export.py -v
```

`tests/test_em_site_export.py` validates the schema itself is well-formed,
validates the committed export instance against it, and asserts internal
consistency (frame/mesh vertex-count alignment, cell indices in bounds,
provenance fields non-empty) — the executable form of the claims in this
document.

## Addendum: `capacitance` — a real fleet-geometry benchmark (issue #842)

Issue [#842](https://github.com/2AMLogic/klayout-tools/issues/842) closes
the loop between klayout-tools.org and the canary program: it extends the
Electromagnetics gallery (#851) with a result computed on a **real fleet
block's own GDS geometry**, not a textbook fixture. As of that issue
(2026-08-12) no fleet-designed *analog* canary had a GDS layout yet, so the
chosen geometry is the real `met1` VGND/VPWR power-rail pair of a
standard-cell gallery block (`sky130_fd_sc_hd__nand2_2` by default) — the
issue's own scope explicitly allows "a canary's power/ground mesh" /
"an on-chip interconnect coupling case" as a qualifying geometry.

A driven-port S-parameter sweep has no meaning for this kind of DC
multi-conductor electrostatic extraction, so this addendum:

1. Demotes `s_parameters` from the schema's top-level `required` list to
   optional (backward compatible — every existing valid document, including
   the committed patch-antenna export, still validates unchanged).
2. Adds a new optional top-level `capacitance` object: the N×N Maxwell
   capacitance matrix (self- and mutual/coupling capacitance), a Maxwell-
   reciprocity hard check (`max_rel_asymmetry`), and an honestly-banded
   independent `oracle` cross-check — see the schema file's own
   `capacitance` property description.
3. Adds a schema-level `anyOf` requiring at least one of `s_parameters` /
   `capacitance`, so every document still carries *some* physical-response
   payload.

`mesh`/`frames`/`provenance` are reused **unchanged** — this is additive to
the existing format, not a second export shape, per this issue's own
Curator guidance ("reuse #849's export format... rather than inventing a
second one").

### The shipped instance: `interconnect_coupling.em-export.json`

[`examples/em/interconnect_coupling/interconnect_coupling.em-export.json`](../../examples/em/interconnect_coupling/interconnect_coupling.em-export.json)
is generated by [`examples/em/interconnect_coupling/generate.py`](../../examples/em/interconnect_coupling/generate.py),
which:

1. Reads the real `met1` (sky130 GDS layer 68/20) VGND/VPWR rail
   geometry directly out of a gallery block's committed GDS via `pya`
   (rail width/length, edge-to-edge gap) — live, so a re-run after that
   block's layout changes picks up the new dimensions automatically.
2. Feeds those real dimensions (plus the public sky130 open-PDK tech-LEF's
   published `met1` thickness) to a hand-authored structured-grid tet mesh
   ([`examples/em/interconnect_coupling/geode_driver/`](../../examples/em/interconnect_coupling/geode_driver/),
   this repo's own reproducibility-seam Rust crate, the same "separate,
   already-built geode-fem checkout" pattern `examples/em/patch_antenna/generate.py`
   established), which calls geode-fem's real, production
   `assemble_electrostatic`/`extract_capacitance` electrostatic FEM
   assembler — the same code path `benchmarks/electrostatic/results.toml`'s
   own oracles exercise, on this repo's own geometry rather than a bundled
   fixture.
3. Converts the driver's JSON output (capacitance matrix + a driven-field
   mid-length cross-section slice) into this schema's `capacitance` shape.

Validated two ways, mirroring the repo's own oracle discipline: a **hard**
Maxwell-reciprocity check (`capacitance.max_rel_asymmetry`, an exact
discrete identity of the energy method regardless of mesh quality — the
same convention `benchmarks/electrostatic/results.toml`'s `two_sphere_box`
oracle uses), and an **honest, wide-tolerance** sanity cross-check against
the sky130 open PDK's own published `met1` area+edge parasitic-capacitance
model (`capacitance.oracle` — see that field's own description for why it
is a magnitude cross-check, not a strict physical oracle: different
reference-plane distance than this solve's modeled grounded box).

`provenance.geometry` carries both geode-fem's own generator identity
*and* the klayout-tools source: `source_repo`/`source_commit`/
`source_block` name the exact gallery block and klayout-tools commit the
GDS geometry traces to (the commit that last touched that block's `.gds`
file, not necessarily this issue's own PR commit) — the acceptance
criterion this issue's provenance panel renders.

### Regenerating / validating

```sh
uv run python3 examples/em/interconnect_coupling/generate.py --geode-fem-dir /tmp/geode-fem
uv run pytest tests/test_em_interconnect_coupling_export.py tests/test_em_site_export.py -v
```

## Addendum: `block` — per-gallery-block exports (issue #958)

Issue [#958](https://github.com/2AMLogic/klayout-tools/issues/958) (Epic #840
Phase 3a) takes the same format one step further: instead of a benchmark that
*borrows* a real block's dimensions, each gallery block gets **its own
export**, written to `blocks/<slug>/output/<slug>.em-export.json` as a sibling
of that block's `layout.json` and `signals/` waveforms. The site's EM story
becomes part of each project's page rather than a separate demo section (the
DetailPage panel itself is Phase 3b).

The only schema change is one optional top-level string, `block`: the gallery
block slug the geometry came from. It is deliberately *not* a rename of
`benchmark` — `benchmark` names the solver-side geometry family
(`"block_coupling"`), and two blocks share it while belonging to different
gallery projects. Everything else — `mesh`, `frames`, `capacitance`,
`provenance` — is reused unchanged, so the standalone benchmark exports under
`examples/em/` continue to validate untouched.

### The shipped instances

[`blocks/sky130-bandgap/output/sky130-bandgap.em-export.json`](../../blocks/sky130-bandgap/output/sky130-bandgap.em-export.json)
and
[`blocks/gf180-bandgap/output/gf180-bandgap.em-export.json`](../../blocks/gf180-bandgap/output/gf180-bandgap.em-export.json)
are generated by
[`examples/em/block_coupling/generate.py`](../../examples/em/block_coupling/generate.py),
which generalises `interconnect_coupling`'s seam in three ways it had
hard-coded:

1. **Nets, not two hand-picked rectangles.** The routing layer is merged into
   nets, named from the GDS's own label texts, and searched for the best
   *extrudable bundle* — a contiguous group of nets on one cut line that
   actually runs that way uniformly for many cross-sections. The length filter
   is load-bearing: without it the search finds dense "bundles" where a set of
   perpendicular wires merely terminate.
2. **The real process stackup.** Conductors sit at their true height above a
   grounded substrate plane, read from the *installed* open PDK's own magic
   tech file (`height <layer> <bottom> <thickness>`), so the dominant
   substrate term is physical rather than an arbitrary symmetric box.
3. **Real widths and spacings.** The mesh is breakpoint-aligned to every
   conductor edge, so a drawn 0.24 µm space is solved as 0.24 µm instead of
   being quantised to the mesh step.

The `capacitance.oracle` cross-check is likewise sourced from *this repo's
own* curated open-PDK parasitics table
(`klayout_tools.decks.get_parasitics_deck`, the coefficients `klt extract
--parasitics` already ships) rather than from an externally-transcribed
tech-LEF constant.

`provenance.geometry` additionally records `conductors` (each net's drawn
intervals, and whether the GDS labeled it) and `selection` (the exact bundle
search parameters), which is what lets `tests/test_em_block_exports.py` replay
the search against the committed GDS and assert the artifact is regenerable —
not just well-formed.

### Regenerating / validating

```sh
uv run python3 examples/em/block_coupling/generate.py --block sky130-bandgap --geode-fem-dir /tmp/geode-fem
uv run python3 examples/em/block_coupling/generate.py --block gf180-bandgap --prefer-net vref --geode-fem-dir /tmp/geode-fem
uv run pytest tests/test_em_block_exports.py -v
```

See [`examples/em/block_coupling/README.md`](../../examples/em/block_coupling/README.md)
for the recipe for adding the next block.
