# Spike: programmatic layout generators (BAG-class)

**Status:** spike / proposal. Nothing here is scheduled, and nothing here
authorises implementation. Per [docs/ARCHITECTURE.md](../ARCHITECTURE.md) →
"How capabilities arrive," a major capability arrives by spiking a design
epic first — candidate-framework survey, proposed JSON contract, build/wrap
decision — and this document is that spike for programmatic layout
generation (ROADMAP.md Phase 3: "cell instantiation, geometry ops,
parametric cells"). A follow-up epic, decomposed into implementation
sub-issues, would carry the build.

**Demand signal:** `klt` reads, checks, renders, and simulates; nothing
writes layout yet (`src/klayout_tools/` has no module that emits geometry —
`_layout.py` only loads streams, `cells.py`/`layers.py`/`render.py` only
report on them). Phase 3 names "programmatic layout modification... parametric
cells" as the next deliverable. The operator's long-term intent (per #104's
problem statement) is a BAG-class generator engine, eventually with a Rust
core — but BAG itself is the cautionary tale: a powerful idea (parametrized,
grid-aware analog layout generation) wrapped in an architecture that predates
headless-CI practice and is notoriously hard to stand up outside its home lab.
The job of this spike is to take the idea without the baggage: survey what
BAG and its peers actually got right, define a JSON contract that does not
name any one of them, and score a hypothetical Rust engine honestly against
the rewrite rule before anyone writes Rust.

## 1. Candidate-framework survey

Six candidates, per the issue's starting scope: BAG3 + xbase, laygo2,
gdsfactory, ALIGN, MAGICAL, and KLayout's native PCells. Licenses and
maintenance status below were fetched live from each project's GitHub API
and `LICENSE`/README content during this spike (not from memory), the same
verification discipline as
[docs/design/lambdalib-survey.md](lambdalib-survey.md).

### BAG3 + xbase (Berkeley → BlueCheetah)

| Property | Finding |
| -------- | ------- |
| Upstream | Originated as [`ucb-art/BAG_framework`](https://github.com/ucb-art/BAG_framework) (UC Berkeley); BAG3 is now maintained by BlueCheetah Analog Design under [`bluecheetah/bag`](https://github.com/bluecheetah/bag), with [`bluecheetah/xbase`](https://github.com/bluecheetah/xbase) as the layout-generator framework and [`bluecheetah/bag3_analog`](https://github.com/bluecheetah/bag3_analog) / [`bluecheetah/bag3_digital`](https://github.com/bluecheetah/bag3_digital) as generator libraries built on it. |
| License | `bag` core: `BSD-3-Clause AND Apache-2.0` (an SPDX compound expression — different files under each, per the repo's own README: "some source files are licensed under both... the user must comply with the terms and conditions of both"). `xbase`: Apache-2.0. `bag3_analog`/`bag3_digital` (the actual generator libraries, and the closest thing to reusable device generators): **no `LICENSE` file in either repository** — GitHub reports no detected license, meaning no explicit grant of reuse rights exists for the code most relevant to this spike's scope-4 primitives, only for the framework underneath it. |
| Headless posture | Better than the "notoriously hard to run headless" framing suggests, but still heavy. BAG's original workflow leaned on a Cadence Virtuoso SKILL bridge (`bag_server`) for schematic capture; BAG3 decoupled the layout database from any EDA tool via `pybag` (a `pybind11` C++ extension backed by `cbag`), so layout-only generation without Virtuoso is architecturally possible. But standing that up means building a multi-repo C++/Python graph (`bag`, `cbag`, `pybag`, `xbase`, plus a PDK-specific primitives library such as `cds_ff_mpt`, and there is no public sky130 primitives library in the `bluecheetah` org) — a real, measured burden against "every command must be runnable in CI," not merely a reputation. |
| Sound ideas | Three, and they recur across every framework below: (1) a **template abstraction** — a generator is a class with a declared parameter schema and a `draw_layout()`-style method, not a script; (2) a **routing grid abstraction** — wires and vias snap to a per-metal-layer track grid derived from technology rules (`TrackID`/`WireArray` in BAG's vocabulary), so a generator reasons in track indices, not raw coordinates; (3) a **technology plugin** — device geometry rules (contact enclosure, poly pitch, …) live in a swappable per-PDK data file the generator code never hardcodes. Also notable: BAG's schematic and layout generators are **paired** — the same sizing parameters that produce a netlist also produce the layout, which is exactly the "schematic-driven sizing handoff" ARCHITECTURE.md's vision line names (spec → schematic/generator → sized circuit → layout). |
| Reusable code, headless | None, directly. The grid/template classes are tightly bound to BAG's own layout database types and its C++ `pybag` backend — porting them means adopting BAG's whole object model and build, not lifting a geometry helper. This repo already anchors on `pya`/`klayout.db` as its one geometry backend (`klt drc`, `klt render`); running BAG's stack alongside it would mean two parallel geometry engines in one project, which "wrap the proven engine" argues against without a much stronger reason than "BAG happens to exist." |
| Verdict | **Ideas only.** Grid/template/tech-plugin/schematic-handoff are worth reimplementing against `pya`; no code or dependency is adopted. |

### laygo2 (POSTECH)

| Property | Finding |
| -------- | ------- |
| Upstream | [`niftylab/laygo2`](https://github.com/niftylab/laygo2) ("LAYout with Gridded Objects v2"), PyPI package `laygo2`. |
| License | BSD-3-Clause, confirmed via the GitHub API's detected license. |
| Maintenance | The README states the project has been in **"Maintenance Mode"** since September 2024 — "updates are restricted to bug fixes and marginal improvements," with advanced features reserved for a private fork. Still receiving commits as of 2026-07 (`pushed_at: 2026-07-04`), but the open project itself signals it is not where new capability lands — a caution against depending on it going forward, though fine as a stable idea source. |
| Sound ideas | The same template/grid pair as BAG, arrived at independently and documented as laygo2's own headline concept ("templates and grids" is literally the README's pitch). Notably more explicit about the grid as a first-class object separate from any one template — a `Grid` maps abstract row/column indices to physical coordinates per technology, and templates place onto it — which is a cleaner factoring than bundling grid logic inside each generator class. |
| Reusable code, headless | `laygo2/interface/` ships `gds.py` and `gdspy.py` (GDS export via [gdspy](https://github.com/heitzmann/gdspy), no EDA tool involved) alongside optional `bag.py`, `skill.py`, `skillbridge.py` bridges — confirming the project was deliberately designed to run without Virtuoso, unlike BAG's Cadence-first history. Still, `gdspy`/`gdstk`-family output is a second geometry backend distinct from `pya`; adopting the code, not just the grid idea, would repeat the two-backends problem from the BAG entry. No sky130-specific technology setup for laygo2 was found or verified live within this spike's time-box, so PDK-readiness for this project's target (sky130) is unproven either way. |
| Verdict | **Ideas only** — the `Grid`-as-first-class-object factoring is a cleaner reference shape than BAG's for this repo's own reimplementation, but the code stays on `gdspy`, not `pya`, and the project itself is not actively growing new capability. |

### gdsfactory — plus a directly relevant discovery: `gdsfactory/skywater130`

| Property | Finding |
| -------- | ------- |
| Upstream | [`gdsfactory/gdsfactory`](https://github.com/gdsfactory/gdsfactory) — general parametric-layout framework (photonics-first, now also RF/AMS/analog/digital), ~1000 GitHub stars, 4M+ downloads per its own README, actively maintained (`pushed_at: 2026-07-31` — the day this spike was written). |
| License | MIT. |
| Headless posture | Fully headless by design — pure Python, GDS/OASIS output via `gdstk`, no EDA-tool dependency at any point; `gf.Component`/`gf.cell` decorators build a hierarchy in-process and stream out. This is the strongest "headless always" fit of any candidate surveyed. |
| Sound ideas | A `Component` object carrying named, typed `Port`s (layer, position, width, orientation) is a clean, EDA-agnostic port abstraction — closer to what a JSON contract needs than BAG's `WireArray`/`TrackID` vocabulary, which is routing-grid-specific. A `PDK` object (`gf.gpdk.PDK.activate()`) centralizes the technology plugin the same way BAG's `tech_params` does, but as a first-class, swappable Python object rather than a data file consumed by generator code. |
| **Directly relevant finding** | gdsfactory ships an actively maintained, MIT-licensed **sky130 PDK package**, [`gdsfactory/skywater130`](https://github.com/gdsfactory/skywater130) (`pushed_at: 2026-07-31` — pushed the same day this spike was written; ships its own `CLAUDE.md`/`AGENTS.md`, i.e. it is itself built agent-facing). Its `sky130/pcells/` directory contains exactly the primitive family this spike's §4 scopes: `mosfets.py`, `resistors.py`, `capacitors.py`, `guard_ring.py`, `bjts.py`, `diodes.py`, `via_generator.py`. Reading `mosfets.py` and `guard_ring.py` directly: each generator is a documented, parametrized function (`gate_width`, `gate_length`, `nf` for MOSFETs; `inner_width`/`inner_height`/`ring_width`/`spacing` for guard rings) built from named design-rule constants sourced from Magic's sky130 device generators ("Magic-parity MOSFET generators," per the module docstring — contact size, poly/diff enclosures, gate-to-contact spacing, all as named floats with inline provenance comments), producing a `gf.Component` with named ports (e.g. a guard ring's `"VSS"` port) rather than raw polygons. |
| Reusable code, headless | This is the one candidate in the survey where **direct code reuse is not merely legally clear (MIT) but technically close** — the design-rule constant tables and the parametrization shape (W/L/nf/rows/cols → geometry) are almost exactly what §4's generators need for sky130, and they're maintained today, not archived research code. The one real obstacle is architectural, not legal: the output object is a `gf.Component` built on `gdstk`, a second in-process GDS engine alongside `klayout.db`/`pya`, which this repo already depends on for every existing verb (`klt drc`, `klt render`, `klt layers`). Running both means two boolean-op engines, two DRC-adjacent geometry models, and two sets of numerical edge cases to reason about — exactly the sprawl "wrap the proven engine" exists to prevent, for a library that (unlike KLayout) is not this repo's anchor engine per ARCHITECTURE.md's "Scope" section. |
| Verdict | **Idea and design-rule-constant source, not a runtime dependency.** The right move is to treat `sky130/pcells/mosfets.py`'s design-rule table and parametrization shape as a validated reference — a second, independent implementation to differential-test a `pya`-based generator against (see §3's oracle discussion) — while writing the actual generator against `pya`/`klayout.db` so this repo keeps one geometry backend. This is "mining the outside world" in its most literal form: the code is sound and even license-compatible, but the engine choice it's built on is not ours to adopt twice. |

### ALIGN (Analog Layout, Intelligently Generated from Netlists)

| Property | Finding |
| -------- | ------- |
| Upstream | [`ALIGN-analoglayout/ALIGN-public`](https://github.com/ALIGN-analoglayout/ALIGN-public) — DARPA IDEA-program project, University of Minnesota / Texas A&M / Intel. |
| License | BSD-3-Clause. |
| Maintenance | Active (`pushed_at: 2026-07-05`). |
| Headless posture | A CLI (`schematic2layout.py <netlist_dir> -p <pdk_dir> -c`), Docker image published for turnkey use, but also pip-installable from source with a real native-build dependency chain (a C++14 toolchain, optional Boost/`lp_solve`). Genuinely headless — no GUI at any point — but with more build weight than gdsfactory. |
| Sound ideas | ALIGN's own README breaks its flow into exactly the stages relevant here: *circuit annotation* (hierarchy extraction from a SPICE netlist), *design-rule abstraction* (a **JSON-format** per-PDK design-rule file — the same "contract over engine internals" instinct this repo already applies elsewhere), *primitive cell generation* (parametrized instances of NMOS/PMOS, resistor, capacitor, and **guard ring** primitives — literally the same four families §4 of this spike scopes, independently arrived at), and *placement and routing* (constraint-driven block assembly, out of this spike's first-generators scope but the natural next epic once single-cell generators exist). |
| Reusable code, headless | The *idea* of JSON-format PDK design-rule abstraction is directly reusable and consistent with this repo's own `docs/json-contract.md` philosophy. The primitive-generator *code* targets ALIGN's own mock FinFET-14nm PDK and its internal geometry/constraint types, not sky130 and not `pya` — no direct code lift, same pattern as BAG. ALIGN's *placement and routing* engine is out of scope for this spike's single-cell-generator scope entirely, but is the right prior art to revisit when a placement/routing epic is eventually spiked. |
| Verdict | **Idea source**, and the strongest external validation that "matched device arrays, passive arrays, guard rings" (§4) is the right first primitive scope — ALIGN's own architects reached the same four-primitive split independently, for the same reason (they are the lowest hierarchy level every larger analog block is built from). |

### MAGICAL (Machine Generated Analog IC Layout)

| Property | Finding |
| -------- | ------- |
| Upstream | [`magical-eda/MAGICAL`](https://github.com/magical-eda/MAGICAL) — also a DARPA IDEA-program project. |
| License | BSD-3-Clause. |
| Maintenance | Stale relative to the other candidates: `pushed_at: 2024-04-24` — over two years old as of this spike (2026-07-31). |
| Headless posture | Headless-capable (CLI-driven, Docker image published) but with a substantially heavier native dependency chain than any other candidate: Boost, Flex, Zlib, [Limbo](https://github.com/limbo018/Limbo), LPSolve 5.5, [Lemon](https://lemon.cs.elte.hu/trac/lemon) — the README itself recommends Docker over building from source ("NOT RECOMMENDED"). Multiple git submodules for constraint generation, placement, and routing, integrated by a top-level Python flow. |
| Sound ideas | Constraint-driven, optimization-based placement of matched devices (symmetry constraints, common-centroid constraints expressed as solver inputs rather than hand-placed) — a genuinely different and more automated idea than ALIGN's more rule-based primitive generation, relevant to a *future* placement/routing epic, not to the single-cell generator scope here. |
| Reusable code, headless | None practical — build weight and staleness both argue against depending on it, and the repository's own primitive-cell story is thinner in its public README than ALIGN's. |
| Verdict | **Idea source only, and lower-priority than ALIGN** for this repo's near-term needs given the maintenance gap; worth a second look only once a placement/routing epic is actually spiked. |

### KLayout PCells (native)

| Property | Finding |
| -------- | ------- |
| Upstream | Ships inside KLayout itself — the `pya`/`klayout.db` package this repo already depends on (`pyproject.toml`: `klayout>=0.29`), used today by `klt drc` and `klt render`. |
| License | KLayout core is GPL-3.0. Already the posture this repo accepts for every existing verb — invoking/importing the `klayout` package as a wrapped dependency (the same relationship `klt drc` already has to KLayout's DRC engine) is precedent, not a new decision this spike introduces. |
| Headless posture | Already proven headless in this repo — every existing `klt` command runs it in CI with no GUI. |
| Sound ideas / reusable code | A **PCell** is a Python class (`pya.PCellDeclarationHelper` subclass) with a declared parameter schema (`self.param(name, type, description, default=...)`) and a `produce_impl(self)` method that builds geometry against `self.layout`/`self.cell` using the same `Region`/`Polygon`/`Layer` primitives `klt drc` and `klt render` already use. Parameters are declared data, not ad hoc script arguments — structurally the same "declared parameter schema + geometry-producing method" shape as BAG's `TemplateBase` and laygo2's templates, arrived at inside the engine this repo already wraps. Unlike PCells' native use case (interactive placement inside the KLayout GUI, or instantiation via `layout.create_cell(pcell_name, lib, params)` from any embedding `pya` context, GUI or headless), nothing about the class shape requires the GUI — `layout.create_cell()` works identically in a batch script. |
| Verdict | **This is the implementation substrate, not a framework to accept or reject.** The PCell parameter-declaration + `produce_impl` shape is the natural scaffold for this repo's own reference generators: it reuses the one geometry backend already wrapped, needs no new dependency, and is already proven headless. The gap PCells leave — a JSON-contracted request/response envelope, a structured port/bbox/DRC-metadata report, engine-neutral naming — is exactly what §2 defines on top of it. |

### Cross-cutting takeaways

- **Every framework surveyed converges on the same three ideas**: a declared-parameter template abstraction, a technology/design-rule plugin separate from generator code, and (where routing is in scope) a track-grid abstraction. That convergence, arrived at independently by Berkeley/BlueCheetah, POSTECH, gdsfactory, and the DARPA IDEA-program teams, is strong evidence these are the right ideas to reimplement — not evidence to adopt any one project's code.
- **No candidate's generator code is both license-clear and geometry-engine-compatible with this repo.** `gdsfactory`/`skywater130` is license-clear (MIT) but built on `gdstk`, a second geometry engine. Everything else is either license-ambiguous for the actually-relevant generator code (BAG3's `bag3_analog`/`bag3_digital`) or bound to a project-specific object model with no PDK overlap (laygo2, ALIGN, MAGICAL). KLayout PCells are geometry-engine-compatible by construction, because they *are* the engine already wrapped here.
- **The honest scope split**: single-cell parametrized generation (BAG/laygo2/gdsfactory's core strength, and ALIGN's "primitive cell generation" stage) is this spike's §4 scope. Placement and routing of multiple generated blocks (ALIGN's later stages, MAGICAL's whole focus) is explicitly **not** in scope here — it is the natural next spike once single-cell generators exist and produce something to place.

## 2. Proposed generator contract

Documented in the field-table style already established for `klt render` /
`klt pdk` (see [docs/cli/render.md](../cli/render.md) → "JSON schema (the
contract)"). This is a **proposed** shape for review, not a shipped
contract — no `klt` subcommand, dependency, or code is added by this spike.
Field names are deliberately generic (`ports`, `bbox`, `params`) rather than
borrowed from any one framework's vocabulary (not `WireArray`/`TrackID`
[BAG], not `Component`/`Port` used verbatim [gdsfactory]) — per the issue's
own requirement that the contract "must not leak any single framework's
internals."

A **generator** is a named, versioned capability: given a parametrized spec
and a PDK reference, it produces a GDS/OASIS stream plus a structured report.
Following `docs/json-contract.md`'s pattern, the request carries its own
`schema` identifier (mirroring
[docs/design/spice-corner-runner-spike.md](spice-corner-runner-spike.md)'s
request shape, since a generator request — like a corner-sweep request — is
richer than the flag-driven inputs of today's read-only verbs), while the
response conforms to the shared envelope (`schema_version` + flat top-level
fields).

### Request

```json
{
  "schema": "klt.gen.request/1",
  "generator": "mos_array",
  "pdk": { "name": "sky130", "variant": "sky130A" },
  "params": {
    "device": "nfet_01v8",
    "w_um": 2.0,
    "l_um": 0.5,
    "fingers": 4,
    "rows": 2,
    "cols": 2,
    "topology": "common_centroid",
    "dummy_rows": 1,
    "dummy_cols": 1
  },
  "options": {
    "cell_name": "mos_array_cc_0",
    "output": "mos_array_cc_0.gds"
  }
}
```

| Field                | Type            | Description                                                                                                                      |
| -------------------- | --------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `schema`             | string          | Contract identifier and major version. Bumped on any breaking change.                                                              |
| `generator`          | string          | Which generator to run (e.g. `mos_array`, `res_array`, `guard_ring`, `diffpair_mirror` — see §4). Engine-neutral; never a class or framework name. |
| `pdk.name`/`variant` | string          | PDK reference, same shape `klt pdk find`/`klt pdk env` already resolve (`docs/cli/pdk.md`) — a generator resolves its design rules through the same one PDK resolver every other verb uses, not a private lookup. |
| `params`             | object          | Generator-specific parameter set: device sizes, array shape, topology, matching options. Each generator documents its own `params` schema (mirroring how `analysis`/`measurements` are generator-specific in the SPICE contract) — this envelope does not attempt one universal parameter shape across generator families. |
| `options.cell_name`  | string          | Name for the generated top cell. Defaults to `<generator>_<n>` if omitted. |
| `options.output`     | string          | Path to write the GDS/OASIS stream. Format (`.gds`/`.oas`) inferred from the extension, matching `klt render`'s auto-detection posture. |

### Response

```json
{
  "schema_version": 1,
  "generator": "mos_array",
  "cell_name": "mos_array_cc_0",
  "gds_path": "mos_array_cc_0.gds",
  "pdk": { "name": "sky130", "variant": "sky130A", "version": "<SOURCES stamp, per klt pdk>" },
  "bbox_um": { "x0": 0.0, "y0": 0.0, "x1": 12.42, "y1": 8.16 },
  "device_count": 16,
  "ports": [
    {
      "name": "D0",
      "net": null,
      "layer": { "layer": 68, "datatype": 20, "name": "li1drawing" },
      "x_um": 1.235, "y_um": 0.0,
      "width_um": 0.17,
      "direction_deg": 90
    }
  ],
  "drc_hints": {
    "min_spacing_um": 0.21,
    "matched_group_id": "mos_array_cc_0/M0",
    "snapped_to_grid": true,
    "notes": []
  },
  "warnings": []
}
```

#### Top-level fields

| Field           | Type            | Description                                                                                                          |
| --------------- | --------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `schema_version`| integer         | Version of this command's JSON shape (per-command, per `docs/json-contract.md`).                                       |
| `generator`     | string          | Echo of the request's `generator`.                                                                                     |
| `cell_name`     | string          | Name of the top cell written into `gds_path`.                                                                          |
| `gds_path`      | string          | Resolved output path (echoes `options.output`, or the computed default).                                               |
| `pdk`           | object          | Echo of the resolved PDK reference, including the version stamp `klt pdk find` already reports — so a generated block's provenance is traceable the same way a corner-sweep response's `environment` block is (spice contract §2). |
| `bbox_um`       | object          | Bounding box of the generated cell in micrometres — `_um` suffix per this repo's units-in-field-name convention (`dbu_um` in `klt layers`, `supply_v`/`temperature_c` in the SPICE contract). |
| `device_count`  | integer         | Number of matched-device instances placed (array generators) or `1` for a single-instance generator.                   |
| `ports`         | array\<object\> | Named terminals for downstream connection — see below. Deliberately independent of which generator family produced them, so a placement/routing step (out of scope here, per §1) can consume any generator's output uniformly. |
| `drc_hints`     | object          | DRC-relevant metadata the generator itself already knows, so a downstream `klt drc` run is informed, not blind — see below. |
| `warnings`      | array\<string\> | Non-fatal generator notes (e.g. a requested dimension was snapped to the technology grid). Always present, empty when there is nothing to report — same "always report the array" discipline as the SPICE contract's `corners[]`. |

#### `ports[]` entries

| Field           | Type             | Description                                                                                                    |
| --------------- | ---------------- | ---------------------------------------------------------------------------------------------------------------- |
| `name`          | string           | Stable port/pin name (e.g. `D0`, `G`, `S`, `VSS`) — the generator's own naming, documented per-generator.        |
| `net`           | string \| null   | Caller-supplied net label, when the request associates one; `null` when the generator assigns none.             |
| `layer`         | object           | `{ layer, datatype, name }` — the **same** layer/datatype numbering `klt layers` already reports for this stream, so a port is locatable without a second, generator-private layer convention. |
| `x_um`/`y_um`   | number           | Port location in micrometres, relative to the cell origin.                                                       |
| `width_um`      | number           | Port width (for a wire-shaped port) or contact size (for a via-shaped port).                                     |
| `direction_deg` | number           | Outward-facing direction in degrees (0/90/180/270 for Manhattan ports), the same convention KLayout's own PCell/port examples use. |

#### `drc_hints` fields

| Field              | Type              | Description                                                                                                                    |
| ------------------ | ----------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `min_spacing_um`   | number            | The tightest design-rule spacing the generator actually used, so a caller can sanity-check a generator against a known PDK rule without re-deriving it from the geometry. |
| `matched_group_id` | string \| null    | Identifier tying together instances that must remain electrically/geometrically matched (e.g. a common-centroid array's unit devices) — the hook a future LVS/extraction step (`klt lvs`, Phase 4, tracked in #54) can use to verify matching intent survived downstream edits, without inventing a second matching-annotation format. |
| `snapped_to_grid`  | boolean           | Whether any requested dimension was rounded to the technology grid (`true`) or used exactly as given (`false`).               |
| `notes`            | array\<string\>   | Free-form, generator-specific DRC-adjacent notes (e.g. "guard ring width increased to meet minimum tap width"). Always present, empty when there is nothing to report. |

### Semantics and guarantees

- **The contract is engine-neutral by construction.** Nothing in the request
  or response names `pya`, `TemplateBase`, `Component`, or any other
  framework type. A generator could be reimplemented against a different
  engine entirely (§3's Rust-via-`pyo3` question) without changing this
  shape — the same "contract over engine" property `klt drc` already has
  relative to KLayout's DRC engine.
- **Layer numbering is never generator-private.** `ports[].layer` reuses
  `klt layers`' own `{layer, datatype, name}` triple so a port is
  cross-referenceable against the same stream's `klt layers`/`klt render`
  output with no translation step.
- **`drc_hints` is advisory, not authoritative.** It reports what the
  generator *believes* it did; `klt drc` (Phase 2, already shipped) remains
  the actual authority on rule compliance. A generator that reports
  `snapped_to_grid: false` and `min_spacing_um: 0.21` and is then run through
  `klt drc` and found to violate a 0.21 µm rule is a **generator bug**, not a
  contract violation — the hint is a fast, in-band signal to catch the
  common case cheaply, not a replacement for the deck-based check.
- **PDK resolution goes through the one resolver.** `pdk.name`/`pdk.variant`
  in the request are the exact fields `klt pdk find`'s `--pdk`/`variant`
  already accept (`docs/cli/pdk.md`) — a generator does not invent its own
  PDK lookup.
- **Additive envelope, same rule as every other verb.** New fields may be
  added to either the request or the response without a schema bump; renaming,
  removing, or retyping an existing field requires bumping `schema`
  (request) or `schema_version` (response) respectively.

### Proposed exit codes

Following the trichotomy `docs/cli/drc.md` and the SPICE corner-runner spike
both use for "successful but not clean" outcomes:

| Exit code | Meaning                                                                              |
| --------- | --------------------------------------------------------------------------------------- |
| `0`       | Generation succeeded; `gds_path` was written and the report above is on stdout.         |
| `1`       | Application error — unknown generator name, unresolvable PDK, invalid/out-of-range `params`. |
| `2`       | Usage error (missing argument, bad `--format` value) — from argparse.                    |

No third "partial success" code is proposed here, unlike `klt drc`'s `3`:
a generator either produces a cell or it doesn't — there is no analogue to
"ran clean but found violations" at the generation step itself (that's what
running the *output* through `klt drc` next is for). This is flagged as an
open question for the eventual epic in case a generator family turns out to
need one (e.g. a generator that produces a best-effort layout with recorded
DRC-adjacent violations rather than failing outright).

## 3. Rewrite-rule test for a hypothetical Rust engine

[docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "Rewrite rule" permits a
rewrite only when **all three** hold: bottleneck/ceiling, oracle, unlock.
Scored honestly for a hypothetical Rust generator engine (invoked from
Python via `pyo3`, behind the exact contract in §2):

1. **Bottleneck or ceiling — cannot yet be measured, and that itself is the
   finding.** There is no Python/pya reference generator running in any
   agent's edit loop today — Phase 3 has not started. Scoring "is Python
   generation the ceiling" before anything exists to be a ceiling of would be
   inventing a number, exactly the failure mode ARCHITECTURE.md's rewrite
   rule exists to prevent ("sequenced by a decision rule, not by ambition").
   The honest position is: **build the Python/pya reference first**, run it
   in real block work (the same "forcing function is real IP" discipline
   ROADMAP.md names), and revisit this test once there is a friction log
   entry to point at — the same evidentiary bar #54 (LVS friction) and this
   very issue (#104, filed against a stated long-term Rust intent) were both
   held to.
2. **Oracle exists — holds, and is buildable now.** A second, independent
   implementation of the same generator contract (§2) can be differential-
   tested against a `pya` reference via `pya.Region` boolean-op diffs
   (XOR the two output streams' geometry, layer by layer) — infrastructure
   this repo already has proven in shape, if not in this exact form, via its
   corpus-golden DRC tests. §1's discovery of `gdsfactory/skywater130`'s
   MOSFET/guard-ring generators is directly useful here even without
   depending on the package at runtime: it is a second, independently
   written, magic-parity implementation of the same devices, usable as an
   **oracle input** for a one-time differential check during development,
   without ever being a runtime dependency of `klt`.
3. **Unlock — likely fails, and conflating two different "geometry layers"
   is the trap to avoid.** ARCHITECTURE.md names "the geometry layer —
   GDSII/OASIS parsing, polygon ops, hierarchy traversal" as a good rewrite
   target. But `pya`/`klayout.db`'s polygon boolean operations are **already
   a compiled C++ engine** under a thin Python binding — the same engine
   `klt drc` already calls. A "Rust generator" would mostly be rewriting
   *orchestration* (parameter validation, array/topology expansion, port
   bookkeeping, JSON assembly) sitting on top of geometry ops that are
   already fast and already not Python. That is a different, much weaker
   rewrite case than "rewrite the geometry engine itself" — it is closer to
   the SPICE corner-runner spike's own finding that the reusable, ours-to-own
   layer is orchestration, and orchestration around an already-fast engine is
   a poor unlock target until proven otherwise (e.g. by a measured sweep
   workload — batch-regenerating hundreds of sizing variants — that shows
   Python-level per-call overhead, not engine-level polygon math, dominating
   wall time).

**Recommendation: Python/pya reference implementation first — it *is* the
oracle — with a `pyo3`-backed Rust core only later, and only for the specific
sub-piece (most plausibly batch/sweep orchestration across many parameter
variants, not single-instance polygon generation) that a real measurement
shows is the bottleneck, behind the exact same contract in §2 so the choice
of engine remains invisible to every caller.** This mirrors the SPICE spike's
own wrap/build conclusion almost exactly: distinguish "the numerics/geometry
engine" (already fast, already wrapped, poor rewrite target) from "the
orchestration around it" (ours to own, and the only plausible future rewrite
candidate) — and do not schedule the second until it is measured, not
assumed.

## 4. Scope proposal: first generators

The primitives PHY-class sky130 blocks need, refined against the two
sources the issue names: `docs/ARCHITECTURE.md`'s knowledge-base workstream
(epic #102) and its `layout_idioms` field, and ALIGN's independently-reached
"primitive cell generation" stage (§1). As a concrete anchor, every one of
the four families below already appears — independently of this spike — in
the seed KB entry `kb/entries/sky130-bandgap-reference.json`'s
`layout_idioms` array:

> `"common-centroid / cross-quad placement of the matched bipolar devices to cancel linear gradients across the die"`, `"resistor arrays laid out as unit-resistor strings with dummy elements at both ends, matched in orientation to cancel process-gradient mismatch"`, `"guard ring around the bipolar devices tied to substrate/well potential..."`, `"op-amp differential pair placed on an axis of symmetry with the current-summing node kept short and symmetric to the two resistor branches"`

That is not a coincidence this spike is manufacturing — it is the same
"friction log from real block designs" evidentiary bar ROADMAP.md's "How
progress is driven" section requires, already present in the one KB entry
that exists today. Four generator families, each implementable as a
`pya`-native module following the PCell parameter-declaration + geometry-
producing shape identified in §1's KLayout PCell entry:

1. **Matched transistor arrays (common-centroid).** Places unit transistors
   (or unit fingers of a larger device) in an ABBA/ABAB-style 2D array with
   dummy devices at the array edges, so linear process gradients (oxide
   thickness, implant dose) cancel across matched pairs by symmetry rather
   than by absolute accuracy. `params` needs at minimum: unit device size
   (`w_um`/`l_um`/fingers), array shape (`rows`/`cols`), topology
   (`common_centroid` vs. a plain array), and dummy-device counts. This is
   the `mos_array` generator sketched in §2's example request, and the
   family §1's ALIGN and `gdsfactory/skywater130` entries both already
   implement in their own right.
2. **Resistor / capacitor arrays.** Unit resistor strings (or unit MoM/MiM
   capacitor cells) with dummy elements at both ends and orientation-matched
   placement, per the KB entry's own wording. Shares its parameter shape with
   the transistor-array family (unit size, array shape, dummy counts) closely
   enough that a shared `params` sub-schema across `res_array`/`cap_array` is
   worth designing for in the eventual epic, rather than two independent
   ad hoc shapes.
3. **Guard rings.** A ring of substrate/well tap tied to a supply/ground net,
   surrounding a sensitive device group to collect injected minority
   carriers and isolate switching noise — the family `gdsfactory/skywater130`
   already implements per-well-type (`pwell_guard_ring`/`nwell_guard_ring`)
   with a single named port (`VSS`/`VDD`-style) on the tap ring, a shape
   directly reusable as a reference for this repo's own `ports[]` design in
   §2. `params`: inner area (`inner_width_um`/`inner_height_um`), ring width,
   spacing to the enclosed devices, and which well/tap type.
4. **Differential pair + current mirror cells.** A device-level composition
   (not just an array) — a matched pair placed on an axis of symmetry with a
   short, symmetric current-summing node, per the KB entry's wording almost
   verbatim. This is the first generator family that composes the array/
   guard-ring primitives above rather than standing alone (a diff pair is
   itself often a 2-device common-centroid array; a current mirror often
   needs its own guard ring), making it a natural integration-proving case
   for whether §2's contract composes cleanly — one generator's response
   (`ports`, `bbox_um`) becoming a plausible input to a compositing step, not
   just a terminal artifact.

Sizing/topology for all four are the sizing-side of the closed loop the
schematic-generator layer this ARCHITECTURE.md vision line already claims
(spec → schematic/generator → sized circuit → layout) — this spike scopes
layout generation only; sizing itself belongs to whatever schematic-generator
work feeds these `params`, not to this document.

## Out of scope for this spike

No dependency was added to `pyproject.toml`, no `klt` subcommand was added,
no generator code was written, and no MCP surface was touched. Placement and
routing of multiple generated blocks (ALIGN's and MAGICAL's actual focus) is
explicitly out of scope — a candidate for a later spike once single-cell
generators from §4 exist and produce something worth placing.

## Open questions for a follow-up epic

- Whether `params` needs any shared sub-schema across the array-shaped
  generators (`mos_array`/`res_array`/`cap_array`), or whether per-generator
  documentation (the SPICE contract's precedent: `analysis`/`measurements`
  are generator/analysis-specific, not unified) is the right level of
  reuse.
- Whether a third, "generated but with recorded DRC-adjacent caveats" exit
  code is needed alongside `0`/`1`/`2` (flagged in §2), once a real
  generator implementation surfaces a concrete case for it.
- How `matched_group_id` (§2) is actually consumed once `klt lvs`/extraction
  exists (Phase 4, tracked by #54) — this spike proposes the field as a
  forward-compatible hook, not a finished design for matching verification.
- Whether the `gdsfactory/skywater130` design-rule constants (§1) are close
  enough to sky130A's actual PDK data to use directly as the oracle input in
  §3, or whether the reference generator's design-rule constants should be
  independently re-derived from the PDK's own `libs.tech` data — a question
  for whoever implements the first reference generator, not this spike.
- Sizing/schematic-generator handoff (`params` provenance) is named as
  explicitly out of scope in §4 but will need its own contract eventually;
  this spike does not attempt to anticipate that design.
