# Spike: E&M field simulation — engine survey

**Status:** spike / proposal. Nothing here is scheduled, and nothing here
authorises implementation. Per
[docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "How capabilities arrive," a
major capability arrives by spiking a design epic first — candidate-engine
survey, proposed JSON contract, wrap/build decision — and this document is
that spike for E&M field simulation. It is the sibling of
[docs/design/spice-corner-runner-spike.md](spice-corner-runner-spike.md)
(issue #27), which did the same thing for SPICE corner running; the shape of
this document mirrors that one deliberately. A follow-up epic, filed
separately once this spike is reviewed, would carry the build.

**Demand signal:** operator request (2026-07-31) to add full field
simulation, and a concrete stalled use case: the knowledge-base entry
[`kb/entries/sky130-spiral-inductor.json`](../../kb/entries/sky130-spiral-inductor.json)
already documents that sky130 ships no characterized inductor primitive, so a
spiral's L/Q/self-resonant-frequency come only from the Yue & Wong analytic
pi-model as a first-pass estimate — the entry's own `notes` field says
outright to "budget schedule for EM simulation … to validate the analytical
sizing estimate before tapeout." Any PHY-class block with an on-chip
inductor, MOM/MIM cap, deliberately-modeled interconnect, or a characterized
block boundary hits the same wall: the closed loop's
"simulation-verified" step (docs/ARCHITECTURE.md → Vision) does not cover
passives and interconnect, because SPICE has no model for them without a
field solve upstream of it. `docs/ARCHITECTURE.md` → "In-house prior art"
already names geode-fem as a candidate engine for exactly this gap, not just
an idea source — this spike is the survey that decision requires before
`klt` grows an E&M verb.

## 1. Use cases, ranked

| Rank | Use case | Regime | Why |
| ---- | -------- | ------ | --- |
| 1 | Spiral inductor L / Q / SRF extraction | Quasi-static up to self-resonance; **full-wave** to capture Q and SRF accurately | Has a KB entry today (`sky130-spiral-inductor`) documenting the exact gap, and is geode-fem's own flagship validated benchmark — the closest thing to a ready-made oracle comparison this survey can point to. |
| 2 | MOM/MIM cap C + Q | Mostly **quasi-static** (electrostatic C dominates); finite-frequency Q needs loss-tangent/skin-effect physics a static solve omits | Geometrically the simplest 3D field problem of the four (parallel/interdigitated plates, no wave ports), so the cheapest place to validate the geometry pipeline end-to-end before tackling ports. |
| 3 | On-chip T-line / interconnect RLGC | **Quasi-static**, as long as line length stays well under the signal wavelength (true for most on-chip routing; stops being true for mm-wave PHY lines) | Classically FastHenry/FastCap territory (per-unit-length RLGC from a 2D cross-section solve) — cheapest regime, but needs the geometry path to walk a real routing cross-section, not just a lumped device footprint. |
| 4 | Block-boundary S-parameters | **Full-wave**, essentially always (wave ports, propagation, radiation/coupling all require it) | The general case and the hardest one: wave-port definition and a full driven solve are exactly what geode-fem's port infrastructure (lumped + multi-mode wave ports, block S-matrix) already targets, but it is also the most demanding on the geometry pipeline (arbitrary port placement at a layout boundary, not just device terminals). |

The ranking is by tractability-times-demand, not by importance: #1 and #2 are
where the geometry pipeline (Section 2) can be proven out on genuinely cheap
problems (a spiral is one net wound in one metal layer; a MOM cap is two
plates) before the pipeline has to handle arbitrary wave-port placement for
#4. A follow-on epic's first vertical slice should be #1, not because it is
easiest in isolation but because it already has a KB entry, an in-house
benchmark to anchor an oracle comparison against, and a documented open gap
in a real block.

## 2. Geometry path: GDS + sky130 stackup → solver mesh

This is the hard integration, and this spike does not attempt to solve it —
only to describe its shape and confirm it stays headless and NDA-free.

**Inputs already available in this repo.** `klt layers` (Phase 1) already
parses per-layer polygon geometry out of a GDSII/OASIS file headlessly via
`pya`; `klt layout-metrics`'s "block directory" convention
(docs/cli/layout-metrics.md) already gives a standard place to point a
command at a block. Neither currently carries a third (z) dimension — GDS
layers are 2D polygons keyed by `(layer, datatype)`, with no per-layer height
or thickness in the file itself.

**What is missing: a stackup.** The z-axis geometry — each metal/via layer's
height above substrate, thickness, and each dielectric's permittivity/loss
tangent, plus per-metal sheet resistance/conductivity — is not in the GDS and
is not currently modeled anywhere in this repo. It has to come from a
stackup table keyed by PDK variant. **This data is not NDA'd**: sky130 is an
open PDK, and SkyWater/Google/efabless publish the process stack-up
(layer names, nominal heights and thicknesses, dielectric constants) as part
of the open PDK documentation — the same "open PDKs only" rule
(CLAUDE.md) that lets `klt sim` reference sky130's ngspice model library by
path (docs/cli/pdk.md) applies here: a stackup table is public data, checked
in or resolved the same way `klt pdk` resolves `libs.tech/`, never a
proprietary design rule.

**Proposed path, in stages:**

1. **Region selection.** A layout ref (block directory, per
   `layout-metrics.md`) plus a bounding region — either an explicit bounding
   box or a cell/instance reference — selects the subset of the layout to
   simulate. E&M solves are never full-chip; they are always a bounded
   region (a device, a net, a block boundary), matching all four ranked use
   cases.
2. **Per-layer polygon extraction.** Reuse the same `pya` batch traversal
   `klt layers`/`klt cells` already do, filtered to the region and to the
   layers the stackup table says are conductor or via layers.
3. **Extrusion.** Each 2D polygon is extruded to a 3D prism using the
   stackup table's height/thickness for its `(layer, datatype)`, and
   dielectric slabs are added between/around them from the same table. This
   is a pure geometry step (headless, no engine dependency yet).
4. **Meshing.** The extruded 3D solid model needs to become a mesh the
   solver consumes — a tetrahedral mesh for geode-fem's FEM/DG kernels, or a
   Cartesian/graded grid for openEMS's FDTD. This is the step the issue
   correctly flags as the hard part: acute-angle polygon corners, thin
   dielectric slivers between closely spaced traces, and via-to-metal
   junctions are exactly the features that make automatic 3D meshing of
   IC-style geometry harder than the CAD-solid meshing FEM tools are usually
   built for. The obvious bridge is an existing open-source 3D mesher run
   headlessly (e.g. `gmsh`, LGPL, scriptable via its own `.geo`/Python API
   with no GUI requirement) rather than writing a mesher from scratch — but
   this spike does not commit to one, since neither candidate engine's mesh
   ingestion format has been evaluated against real sky130 extrusions yet.
5. **Ports.** For use cases 1, 3, and 4, the solver needs port definitions
   (where current is driven in / S-parameters are referenced). The natural
   anchor is pin/net geometry — the same layout objects Phase 4 (`klt lvs`,
   extracted netlist) will eventually resolve to nets — so a port
   definition is proposed as a small companion list (layer + polygon or pin
   name + port type) rather than requiring the solver's own port syntax to
   be hand-authored per run.

Every step above is scriptable in batch (`pya`, a stackup JSON, a headless
mesher invocation, the solver's own CLI/API) with no GUI in the loop,
satisfying the headless-always rule end to end.

## 3. Engine survey

### geode-fem

| Property | Finding |
| -------- | ------- |
| Upstream | [rjwalters/geode-fem](https://github.com/rjwalters/geode-fem) — in-house, named as a candidate engine in docs/ARCHITECTURE.md. Burn-based ([burn.dev](https://burn.dev)) Rust FEM/DG electromagnetic solver, targeting the same problem class as [AWS Labs Palace](https://awslabs.github.io/palace/stable/) but on a differentiable, multi-backend GPU tensor IR. Not present in this machine's `~/GitHub` checkout at spike time; findings below come from the public repo's README via the GitHub API, not a local skim. |
| License | MIT (confirmed via the GitHub API) — no compatibility question for either an in-process or subprocess wrap, unlike the mixed ngspice/Xyce licensing the SPICE spike had to navigate. |
| Maturity | Driven + eigenmode FEM with multi-mode wave ports is on `main`, validated across five benchmark families: Mie sphere (eigenmode + driven scattering), patch antenna (S11/bandwidth/NTFF/efficiency), **spiral inductor (L/Q)**, an SLCFET capstone, and a slotless-PM motor torque benchmark. 200+ merged PRs; sparse `[nnz]` Nédélec assembly, Krylov COCG + ILU(0) iterative solver, block S-matrix wave ports (rank-N SMW augmentation) are all shipped, not roadmap items. |
| Headless story | Structurally headless already — a pure-Rust workspace invoked via `cargo run`/`cargo test`, no GUI dependency at any layer. GPU backend (`wgpu`/`cuda`/`metal`) is a compile-time feature; the default `wgpu` backend runs on Metal at runtime on macOS and falls back to the `ndarray` CPU backend on headless Linux CI, by the repo's own account — directly compatible with this repo's "headless always" rule with no adaptation needed. Optional visualization (benchmark "tearsheets", VTK/ParaView field export) is itself headless via `pvbatch`. |
| Accuracy vs. analytic oracles | Directly on point for our #1 use case: spiral-inductor L extracted from `Im(Z)/ω` across a frequency sweep is **within −4.9% of the Mohan analytic formula and −6.8% of MoM PEEC at 1 GHz** on a 3.5-turn 54,428-edge fixture; a second "SLCFET" spiral capstone (Au-on-SiC, 76,964 edges) extracts a quasi-static L₀ via Richardson extrapolation **within −2.1% of MoM PEEC and +2.8% of Mohan**, explicitly meeting a documented 5% bar. The patch-antenna benchmark validates S11/bandwidth against a Balanis cavity-model oracle, and the Mie-sphere benchmark validates eigenmode/driven scattering against the analytic Mie series — i.e. geode-fem already runs the exact style of oracle comparison this spike's wrap/build section (Section 5) proposes requiring. |
| Runtime cost | Not established by this survey. The repo's benchmark meshes (tens of thousands of edges) run on a sparse iterative solver, but no wall-clock numbers surface in the public README; a follow-on epic must benchmark geode-fem directly against mesh sizes a real sky130 block region produces (Section 2's pipeline does not exist yet, so today there is nothing to benchmark it against). |
| Fit to our geometry path | None of Section 2's GDS→mesh pipeline exists today — geode-fem consumes meshes, not GDS/OASIS files or a PDK stackup. Being in-house buys no head start here; the integration work is the same regardless of which engine is chosen. |

### strata-fdtd

| Property | Finding |
| -------- | ------- |
| Upstream | [rjwalters/strata-fdtd](https://github.com/rjwalters/strata-fdtd) — geode-fem's sister FDTD project, named in docs/ARCHITECTURE.md alongside geode-fem. MIT license. |
| Current scope | **Not an E&M solver today.** Its own README describes it as an "FDTD acoustic simulation toolchain for loudspeaker design and metamaterials" — a pressure-velocity acoustic FDTD with native C++/OpenMP kernels, optional GPU acceleration via PyTorch, and SDF/CSG geometry modeling for enclosures and Helmholtz resonators. geode-fem's own project-family table lists strata-fdtd's discretization as "FDTD" and status as "acoustic today, EM in progress" — confirming this is a planned, not shipped, capability. |
| Why it is surveyed anyway | geode-fem's README names the Mie-resonance benchmark as the intended cross-check triangulating "analytic Mie series ↔ strata FDTD ↔ GEODE-FEM eigenmode" — i.e. strata-fdtd is architecturally positioned to become a second in-house FDTD candidate once its EM mode lands, sitting alongside openEMS as an independent-discretization oracle for geode-fem. |
| Recommendation | **Track, do not wrap.** Not a candidate for this spike's contract; re-survey in the follow-on epic only if strata-fdtd's EM support has shipped by then. |

### openEMS

| Property | Finding |
| -------- | ------- |
| Upstream | [openEMS](https://openEMS.de) / [thliebig/openEMS-Project](https://github.com/thliebig/openEMS-Project) — mature, independently developed, in active use since ~2010; the reference open-source FDTD solver named explicitly in docs/ARCHITECTURE.md → "Mining the outside world." Octave/Matlab and Python scripting interfaces via the companion CSXCAD geometry library. |
| License | **GPL-3.0** (confirmed via the GitHub API on `thliebig/openEMS`). Same category as Xyce in the SPICE spike: fine to invoke as a subprocess, forecloses in-process/library embedding inside this repo's MIT surface. |
| Headless story | Achievable, with a scripting discipline. Structures are normally built via Octave/Python calls into CSXCAD (a geometry-definition API, not a GUI), and the `openEMS` FDTD engine itself is a CLI binary with no GUI dependency; the companion `AppCSXCAD` viewer is optional tooling for humans, never required to run a solve — the same "GUI is a viewer, never a dependency" shape this repo already holds KLayout to. |
| Accuracy vs. analytic oracles | Not independently measured in this survey (no local checkout, and this spike does not run either engine). openEMS's documentation and the broader FDTD literature cite validation against canonical antenna/scattering problems — the same problem class (patch antennas, resonant scattering) geode-fem validates against with its Mie-sphere and patch-antenna benchmarks. That overlap is exactly what makes openEMS a strong **independent-implementation oracle** for geode-fem, per Section 5, rather than something this spike can rank on absolute accuracy without running both. |
| Runtime cost | FDTD's cost scales with Courant-limited time-step count and total grid-cell count. IC routing geometry — sub-micron traces against a mm-scale die/block region — plausibly forces a much finer grid than an unstructured tetrahedral FEM mesh needs for the same geometry, even with openEMS's graded/nonuniform mesh support. This is a hypothesis for the follow-on epic to measure, not a finding of this spike. |
| Role | Mature, well-documented, and exactly the kind of outside engine the architecture doc calls for surveying — but the GPL-3 license caps its integration mode the same way Xyce's did for SPICE, and its clearest value here is as the cross-validation oracle for geode-fem's numbers rather than as a competing default (Section 5, Section 6). |

### Quasi-static extractors (FastHenry/FastCap class)

| Property | Finding |
| -------- | ------- |
| Upstream | FastHenry (inductance/resistance via partial-element-equivalent-circuit) and FastCap (capacitance via multipole boundary-element), both originally from MIT's Research Laboratory of Electronics. Their own project descriptions call them "the premium inductance/capacitance solver … a de-facto golden reference standard," and they are the classical tools for exactly the quasi-static regime our #1–#3 use cases live in (L/C extraction below self-resonance, per-unit-length RLGC). |
| License | **Unresolved, and disqualifying until resolved.** The canonical mirrors (`ediloren/FastHenry2`, `ediloren/FastCap2`) report no SPDX-detected license via the GitHub API — these predate the OSI-approved-license era and have historically circulated under an MIT-authored research-use license whose exact terms need a real legal read, not an assumption, before any dependency decision. This is a sharper version of the SPICE spike's Xyce caution: there the ambiguity was about GPL-3's *terms*, here it is about whether a clean permissive grant exists at all. |
| Maturity | Numerically mature (decades of PEEC/multipole-method use), but the canonical implementations are aging Unix-era C with most current GitHub activity in downstream forks and wrappers rather than upstream maintenance — a different risk profile than ngspice's actively maintained upstream in the SPICE spike. |
| Fit | Right method class for the cheap end of Section 1 (T-line RLGC is FastHenry's original design point), but between the licensing ambiguity and the fact that geode-fem's own SLCFET benchmark already demonstrates a quasi-static extraction mode (L₀ via Richardson extrapolation to f→0 on the same FEM solver, within 5% of MoM PEEC), there is a credible in-house alternative that avoids the licensing question entirely. |
| Recommendation | Name the method class as the right regime for the cheap use cases; do not adopt the specific unmaintained tools. Treat "cheaper than full FEM/FDTD" as a **solver mode** of whichever full-wave engine is adopted (quasi-static/DC-extrapolation), not a new external dependency — unless a follow-on epic's cost/accuracy measurements argue otherwise. |

## 4. Proposed JSON contract: `klt em`

Documented in the same field-table style as `klt sim`
(docs/cli/sim.md) and the shared envelope
([docs/json-contract.md](../json-contract.md)): `schema_version` (integer,
versioned per command, not the string-typed `"schema"` field the SPICE
spike originally proposed before that convention existed — `klt sim` itself
notes this deviation, and this spike adopts the settled convention directly
rather than repeating the now-corrected mistake). **This is a proposed
shape for review, not a shipped contract**, and no `klt` verb, dependency,
or line of solver-invoking code exists yet.

### Request

```json
{
  "layout": "blocks/lc-vco/layout.gds",
  "region": { "cell": "spiral_l1", "bbox_um": null },
  "stackup": "sky130A",
  "engine": "geode_fem",
  "solve_mode": "driven",
  "ports": [
    { "name": "p1", "pin": "spiral_l1/IN", "type": "lumped", "z0_ohm": 50 },
    { "name": "p2", "pin": "spiral_l1/OUT", "type": "lumped", "z0_ohm": 50 }
  ],
  "frequency": { "start_hz": 1e8, "stop_hz": 1e10, "points": 41 },
  "outputs": ["s_parameters", "l", "q", "srf"],
  "checks": [
    { "name": "l_at_2ghz", "output": "l", "at_hz": 2e9, "unit": "nH", "limits": { "min": 1.8, "max": 2.2 } },
    { "name": "q_at_2ghz", "output": "q", "at_hz": 2e9, "unit": null, "limits": { "min": 8 } }
  ],
  "options": { "mesh_resolution": "default", "timeout_s": 1800, "keep_artifacts": true }
}
```

| Field                | Type              | Description                                                                                                                                                |
| -------------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `layout`              | string            | Path to a block directory or a GDSII/OASIS file, per the `layout-metrics.md` block-directory convention. A *reference*, not inline geometry.                |
| `region`              | object            | Selects the layout subset to simulate: a `cell` (instance/cell name) and/or an explicit `bbox_um`. E&M solves are always bounded, never full-chip (Section 2). |
| `stackup`             | string            | PDK variant name (`sky130A`) resolved to a stackup table the same way `klt pdk` resolves `libs.tech/` — the metal/via height, thickness, and dielectric table from Section 2. Open PDK data only, never NDA'd. |
| `engine`              | string            | Engine selector (`geode_fem`, `openems`, …). Present from day one so engine choice is data, not a code path — the same convention `klt sim`'s `engine` field established. |
| `solve_mode`          | string            | `quasi_static`, `driven`, or `eigenmode` — maps directly to Section 1's per-use-case regime call.                                                            |
| `ports[]`             | array\<object\>   | `name`, a `pin` reference (net/pin name, anchored to layout geometry per Section 2) or explicit geometric port, `type` (`lumped`/`wave`), and `z0_ohm` reference impedance. |
| `frequency`           | object            | `start_hz`/`stop_hz`/`points` (linear sweep) or an explicit `points_hz` array — same sweep-vs-explicit-list shape `klt sim`'s corner axes already use.        |
| `outputs[]`           | array\<string\>   | Which reductions to compute from the raw field solve: `s_parameters`, `l`, `q`, `c`, `rlgc`, `srf`. Requesting an output not meaningful for the given ports/solve_mode is a documented application error. |
| `checks[]`            | array\<object\>   | Optional pass/fail assertions, in the same shape as `klt sim`'s `measurements[]`: `name`, which `output` and frequency point it reads, `unit`, and `limits` (`min`/`max`, either optional). A check with no `limits` is reported but never fails — the same "collect a value you're still characterising" escape hatch `klt sim` documents. |
| `options.mesh_resolution` | string        | Coarse knob (`default`, or engine-specific tiers) rather than exposing raw mesh parameters in v1 — the mesh pipeline itself (Section 2) is unbuilt, so this field is deliberately underspecified pending that work. |
| `options.timeout_s`   | integer           | Wall-clock budget for the whole solve. E&M solves are expected to run far longer than a SPICE corner, so this is a single run-level budget, not per-sub-step. |
| `options.keep_artifacts` | boolean        | Retain the mesh, field-solution files (VTK/HDF5), and solver logs on disk, referenced from the response — never inlined into JSON, the same rule `klt sim` applies to rawfiles/logs. |

### Response

```json
{
  "schema_version": 1,
  "layout": "blocks/lc-vco/layout.gds",
  "status": "pass",
  "environment": {
    "engine": "geode_fem",
    "engine_version": "0.4.0",
    "stackup": "sky130A",
    "stackup_sha256": "7ac2…",
    "layout_sha256": "1ab7…",
    "mesh_stats": { "nodes": 41203, "edges": 118440 }
  },
  "results": {
    "s_parameters": { "unit": "complex", "ports": ["p1", "p2"], "touchstone": ".klt/em/spiral_l1/spiral_l1.s2p" },
    "l": [{ "frequency_hz": 2e9, "value_nh": 1.97 }],
    "q": [{ "frequency_hz": 2e9, "value": 9.4 }],
    "srf": { "value_hz": 6.1e9 }
  },
  "checks": [
    { "name": "l_at_2ghz", "unit": "nH", "limits": { "min": 1.8, "max": 2.2 }, "status": "pass", "value": 1.97, "margin": 0.03 },
    { "name": "q_at_2ghz", "unit": null, "limits": { "min": 8 }, "status": "pass", "value": 9.4, "margin": 1.4 }
  ],
  "diagnostics": [],
  "artifacts": { "mesh": ".klt/em/spiral_l1/mesh.msh", "field": ".klt/em/spiral_l1/field.vtu", "log": ".klt/em/spiral_l1/solve.log" }
}
```

Field semantics deliberately reuse `klt sim`'s precedents rather than
inventing new ones:

- **`fail`/`error` stay distinct**, exactly as `klt sim` defines them: `fail`
  means the solve produced a trustworthy result and a `checks[]` entry
  missed its limits; `error` means no trustworthy result exists (mesh
  generation failure, solver non-convergence, timeout). A missing/`null`
  result for a requested output is an `error`, never a silent `pass`.
- **`margin` sign convention** (positive headroom, negative violation,
  `null` when the value is `null`) is carried over unchanged from `klt sim`.
- **Reproducibility is in-band**: `environment` hashes both the layout and
  the resolved stackup, the same discipline `klt sim` applies to the
  netlist and model library.
- **Large artifacts are always references, never inlined** — mesh files and
  volumetric field solutions are far larger than SPICE rawfiles, making this
  rule more load-bearing here than it already is for `klt sim`.

### Proposed exit codes

| Exit code | Meaning                                                                        |
| --------- | ------------------------------------------------------------------------------- |
| `0`       | Solve completed and every declared `checks[]` entry passed (or none declared). |
| `1`       | Application-level error (bad layout/stackup ref, mesh generation failure, unresolved port). Documented error shape on stderr. |
| `2`       | Usage error — from argparse.                                                    |
| `3`       | Solve completed but at least one `checks[]` entry failed its limits.            |

This reuses `klt sim`'s open question rather than resolving it: `klt layers`
uses `1` for input errors and `2` for usage with no third code, while `klt
sim` (and now this proposal) need a pass/fail/broken trichotomy. That
convention should be settled once, repo-wide, when it is actually
implemented — not invented a third time by this spike.

## 5. Wrap or build?

Per docs/ARCHITECTURE.md → "Rewrite rule," a rewrite (here: absorbing
geode-fem's numerics into this repo rather than depending on it) is
permitted only when bottleneck-or-ceiling, oracle-exists, and unlock all
hold. **Geode-fem being in-house does not exempt it from this test** — the
issue is explicit about that, and this section applies the test as if
geode-fem were any outside engine:

1. **Bottleneck or ceiling — fails, and more so than the SPICE case.** There
   is no existing E&M capability to be a bottleneck in; this is greenfield.
   Framed as "would forking geode-fem's numerics into this repo ever be
   justified," the answer is no for the same reason SPICE numerics were a
   poor rewrite target: FEM/DG Maxwell solvers (mesh generation, absorbing
   boundary conditions, wave-port formulations) are an active research
   target — geode-fem's own roadmap lists open items (e.g. the "Whiteroom
   L4 mapping" tracker) — not a settled, forkable artifact.
2. **Oracle exists — holds, clearly.** Closed-form analytic references
   (Mohan's spiral-inductor equations — already the KB entry's own citation,
   parallel-plate/coupled-line capacitance formulas, telegrapher's-equation
   RLGC, canonical antenna/scattering solutions) plus openEMS as an
   independent-implementation cross-check give this domain more oracle
   coverage than SPICE had, not less — geode-fem already builds its own
   validation suite exactly this way.
3. **Unlock — holds, but for a different capability than this spike scopes.**
   geode-fem's differentiability (gradient-based inverse design of an
   inductor or T-line cross-section) is a genuine "a subprocess wrapper
   structurally cannot do this" unlock — but that is a reason to *choose*
   geode-fem as the wrapped engine for its optimization potential, per
   docs/ARCHITECTURE.md → "In-house prior art," not a reason to fork its
   numerics into this repo. The capability this spike scopes (S-parameter
   and L/Q/C extraction behind a JSON contract) does not itself require
   in-repo numerics.

One of three holds outright, one holds but argues for adoption rather than
forking, and one fails. **Recommendation: wrap, not rewrite** — the same
conclusion the SPICE spike reached, reached independently for a domain with
even less argument for absorbing the engine's internals.

**As with the SPICE spike, "wrap" undersells the whole answer.** Two
different things are in play:

- **Wrap the numerics.** Solver, mesh discretization, port/boundary
  formulations — geode-fem (or openEMS, or a future strata-fdtd EM mode),
  unmodified, invoked as a subprocess/library dependency. A poor rewrite
  target by the test above.
- **Build the geometry pipeline and the contract.** GDS region selection,
  stackup-driven extrusion, mesh-format translation, port definition from
  layout pins, frequency-sweep orchestration, `checks[]` evaluation, JSON
  emission (Section 2, Section 4). **Nothing exists to wrap here** — no
  engine, in-house or outside, ships GDS-to-EM-mesh translation for an IC
  metal stackup. This is the layer that is ours, first-class, written from
  scratch, exactly as the corner-matrix orchestration was the substantive
  finding of the SPICE spike.

**Validation bar before geode-fem becomes the *default* engine** (usable
behind the `engine` selector from day one is a lower bar than being the
default): the spike proposes reusing geode-fem's own documented target —
**≤5% relative error against an analytic closed form (Mohan, coupled-line
capacitance, telegrapher's-equation RLGC as applicable) and against openEMS
as an independent-implementation oracle** — but requires clearing it on
**geometry produced by this repo's own GDS→mesh pipeline** on a real sky130
fixture (the `sky130-spiral-inductor` KB entry's structure is the natural
first target), not merely citing geode-fem's existing idealized benchmark
fixtures. The pipeline is new integration surface geode-fem's own benchmark
suite has not exercised, and that gap is exactly where accuracy could
silently regress.

## 6. Alternatives considered

**Skip the spike, wrap geode-fem directly.** Rejected for the same reason
the SPICE spike rejected its analogous alternative: it violates "How
capabilities arrive" (docs/ARCHITECTURE.md), and the in-house relationship
is precisely the case the architecture doc warns is *not* exempt. Skipping
the survey also risks a contract shaped by geode-fem's internal mesh/port
representations leaking into the JSON shape, rather than being derived from
the four ranked use cases independently.

**openEMS-first.** Rejected as the default, not as a candidate. openEMS is
mature and well-documented, and the survey should weigh that maturity
against geode-fem's differentiability and in-house maintainability rather
than assume incumbency wins by default. Its GPL-3 license also caps its
integration mode to subprocess-only, the same constraint that shaped the
SPICE spike's Xyce assessment. openEMS's strongest role coming out of this
survey is as the independent cross-validation oracle for geode-fem's
numbers — arguably a better use of its maturity than taking on a second
maintained dependency as the default path.

## Out of scope for this spike

No dependency was added to `pyproject.toml`, no `klt` subcommand was added,
no mesh-generation or GDS-to-solver-geometry code was written, no solver was
invoked, and no MCP surface was touched. Those remain candidate follow-up
epics gated on this spike's findings.

## Open questions for a follow-up epic

- The GDS→mesh geometry pipeline (Section 2) is the largest scoping
  question and was deliberately left unresolved here: which headless mesher
  bridges extruded sky130 solids to geode-fem's/openEMS's mesh formats, and
  how acute-angle polygon corners and thin dielectric slivers are handled
  without manual cleanup.
- Where the sky130 stackup table itself lives as a `klt`-owned asset —
  bundled data file, derived from open_pdks files at resolve time, or a new
  `klt pdk stackup` subcommand alongside `klt pdk find`/`list`/`env`.
- Port definition convention: pin/net-name based (anchored to Phase 4's
  eventual LVS-extracted nets) versus raw geometric ports defined
  independently of extraction — the request shape above assumes the former
  is preferred but does not commit.
- The exit-code trichotomy question `klt sim` already left open for `klt
  drc` now has a third claimant (`klt em`); it should be settled once,
  repo-wide, not invented a third time.
- Whether `s_parameters` should also emit a Touchstone (`.sNp`) artifact
  alongside the JSON scalar rollup, for direct use in downstream SPICE
  S-parameter blocks.
- Caching: an E&M solve is a pure function of (layout, stackup, ports,
  frequency-sweep) hashes, the same caching argument the SPICE spike made,
  and a stronger one here given the runtime cost gap between a SPICE corner
  and a field solve.
- Whether openEMS graduates from oracle-only to a second real
  implementation behind `engine` (the way Xyce is positioned for `klt sim`),
  or stays a validation-only tool that never ships behind the CLI.

## Recommendation

**geode-fem is the recommended default candidate for the full-wave use
cases (#1 spiral inductor near/above quasi-static, #4 block-boundary
S-parameters), once it clears the Section 5 validation bar on geometry
produced by this repo's own pipeline** — not on its existing idealized
benchmark fixtures alone. Its differentiability is also a forward-looking
asset for the optimization/inverse-design story docs/ARCHITECTURE.md already
names geode-fem for, which no other candidate here offers. **openEMS is
recommended as the primary cross-validation oracle**, not a competing
default — mature, independently implemented, and GPL-3-constrained to
subprocess invocation the same way Xyce was for SPICE. **strata-fdtd is not
yet a candidate** for E&M work (acoustic-only today) and should be
re-surveyed if/when its EM mode ships. For the quasi-static end of the
ranking (#2 MOM cap C, #3 T-line RLGC), the recommendation is to use
geode-fem's own quasi-static/DC-extrapolation solve mode rather than adopt
the FastHenry/FastCap class of tools, given their unresolved licensing.

**The follow-on implementation epic** (to be filed separately, not by this
spike) should scope, in order: (1) the GDS+stackup→mesh geometry pipeline as
its own milestone — likely the majority of the engineering effort, the same
role corner-matrix orchestration played for the SPICE build; (2) a single
first vertical slice on the #1 use case (spiral inductor L/Q), reproducing
the `sky130-spiral-inductor` KB entry's structure as the fixture and
geode-fem's own Mohan/MoM-PEEC comparison as the oracle bar, before
generalizing to MOM caps, T-lines, or block S-parameters; and (3) settling
the exit-code trichotomy this spike, `klt sim`, and `klt drc` all now share
an open question about.
