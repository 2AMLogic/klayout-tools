# Spike: multi-block placement + routing composition for `klt gen`

**Status:** spike / proposal. Nothing here is scheduled, and nothing here
authorises implementation. Per [docs/ARCHITECTURE.md](../ARCHITECTURE.md) →
"How capabilities arrive," a major capability arrives by spiking a design
epic first — candidate-approach survey, proposed JSON contract, build/wrap
decision — and this document is that spike for composing `klt gen`'s
single-cell primitive generators into one placed-and-routed circuit. It
follows the structure the prior accepted spikes set:
[docs/design/layout-generator-spike.md](layout-generator-spike.md) (#104,
for `klt gen` itself) and
[docs/design/lvs-extraction-spike.md](lvs-extraction-spike.md) (#161, for
`klt extract`/`klt lvs`). A follow-up epic, decomposed into implementation
sub-issues (mirroring Epic #152's own phase structure), would carry the
build if one is chosen — this document does not add a dependency, a `klt`
subcommand, or any generator code.

**Demand signal:** Epic #152 (phases 1–4, all closed 2026-08-01) shipped
five single-cell primitive generators — `resistor_strip`, `mos_array`,
`res_array`, `guard_ring`, `diff_pair`, `bjt_array` — documented in
[docs/cli/gen.md](../cli/gen.md). Every phase of that epic, and the spike
that started it, named placement/routing of multiple generated blocks into
one circuit as an explicit, deliberate scope boundary rather than an
oversight (`layout-generator-spike.md` §1: *"Placement and routing of
multiple generated blocks ... is explicitly out of scope here — a candidate
for a later spike once single-cell generators from §4 exist and produce
something worth placing"*). That precondition is now met, and curating #164
(the `klt lvs`/`klt extract` loop-closure phase against the Epic #105
worked example's sky130 5T OTA) turned the gap from hypothetical to
concrete and blocking: a 5T OTA needs a differential pair, a current-mirror
load, and a tail device placed and wired together as one connected circuit,
and nothing in `klt gen` does that today. This spike is issue #186, filed
specifically to carry that judgment call before any implementation is
attempted — the same process #54 went through before Epic #153 (`klt
extract`/`klt lvs`) was spiked.

## 1. Candidate-approach survey

Per the issue's own framing, two structurally different approaches anchor
this survey: **(a)** rule-based grid/row assembly with point-to-point metal
routing between named ports, and **(b)** constraint-solved placement
(MAGICAL-class, i.e. an optimization/solver-driven placement engine). Three
of the six frameworks `layout-generator-spike.md` §1 already surveyed bear
directly on this question and are revisited here rather than re-surveyed
from scratch; one new candidate (gdsfactory's routing module, not covered
by the prior spike, which only examined gdsfactory's `Component`/`Port`
primitives) is added because it is the one candidate whose *routing* code —
not just its device-generation code — is directly relevant to this spike's
scope. License/maintenance data below was re-fetched live from the GitHub
API during this spike (same verification discipline as the prior two
spikes), not carried over from memory of #104's findings.

### (a) Rule-based grid/row assembly + point-to-point routing

**ALIGN** (`ALIGN-analoglayout/ALIGN-public`, BSD-3-Clause,
`pushed_at: 2026-07-05` — actively maintained) is the strongest prior art
for this side of the split. `layout-generator-spike.md` §1 already
identified ALIGN's four-stage flow (*circuit annotation → design-rule
abstraction → primitive cell generation → placement and routing*) as
independently arriving at the same "primitive families" split this repo's
`klt gen` generators cover. Its *placement and routing* stage — the part
out of scope for the prior spike, in scope for this one — combines
constraint-driven block placement (symmetry/common-centroid constraints
expressed as solver inputs, not purely rule-based) with a channel/detailed
router. So ALIGN itself actually straddles (a) and (b): its primitive-cell
stage is rule-based, its placement stage is constraint-solved. This is
useful precedent for *why* a first cut can be simpler than ALIGN's full
flow: ALIGN's constraint solver exists because it places generic,
un-annotated device instances with no *a priori* floorplan — this repo's
generators already report `bbox_um` and named `ports[]` with
`drc_hints.matched_group_id` populated (Epic #152 phase 2, #159), so a
composition step here starts from strictly more information than ALIGN's
placement stage assumes it has to derive.

**gdsfactory's routing module** (`gdsfactory/gdsfactory`, MIT,
`pushed_at: 2026-07-31`) is the new finding this spike adds.
`gdsfactory/routing/` ships `route_single.py`, `route_bundle.py`, and
`route_astar.py` — a genuinely rule-based (not solver-based) point-to-point
router. Reading `route_single.py`'s own docstring: routing a pair of named
ports is a three-step mechanical procedure — *(1)* generate a "Manhattan
backbone": a list of orthogonal (right-angle-only) coordinates connecting
the two ports' positions and orientations; *(2)* replace each backbone
corner with a bend; *(3)* fill the straight segments between corners/bends.
`route_bundle.py` generalizes this to many ports at once with parallel-wire
spacing and net ordering, and `route_astar.py` adds obstacle-avoiding
pathfinding for the cases where a straight Manhattan backbone would cross
another already-placed block. This is precisely the "point-to-point metal
routing between named ports" the issue's suggested scope names, already
implemented, maintained, and MIT-licensed — but (per
`layout-generator-spike.md` §1's already-settled finding on gdsfactory)
built on `kfactory`/`gdstk`, a second in-process geometry engine distinct
from `pya`/`klayout.db`. The same "wrap the proven engine, don't run two"
argument that spike already made about gdsfactory's device generators
applies unchanged to its router: the *algorithm* (Manhattan backbone →
bends → straights, or full A* pathfinding when backbones collide) is
directly reusable as an idea; the *code* is not, because adopting it means
running a second geometry backend alongside the one (`pya`) every existing
`klt` verb already depends on.

**KLayout's own native capabilities** are the third data point for this
side of the split, and (per the prior spike's KLayout PCell entry) the
actual implementation substrate if a rule-based approach is chosen.
`pya.Path` (`klayout.db.Path`) is a first-class shape type carrying an
ordered point list plus a width — exactly the data a Manhattan backbone
needs, with no external router required: a composition step can compute a
backbone (an ordered `(x, y)` list connecting two ports' positions,
respecting their `direction_deg` outward orientation) and hand it directly
to `pya.Path`, which KLayout already round-trips through GDS/OASIS output
identically to every other shape `klt gen`'s existing PCells draw.
`pya.Region` boolean ops (already used by `klt drc`) can then merge routed
paths with placed-block geometry into one output stream. Nothing new needs
to be adopted; the algorithm from gdsfactory's `route_single`/`route_astar`
can be reimplemented directly against `pya.Path`/`pya.Region`.

### (b) Constraint-solved placement (MAGICAL-class)

**MAGICAL** (`magical-eda/MAGICAL`, BSD-3-Clause, `pushed_at: 2024-04-24`)
remains, as `layout-generator-spike.md` §1 already found, stale relative to
every other candidate surveyed (over two years old as of this spike) and
carries the heaviest native dependency chain of any candidate examined
across both spikes: Boost, Flex, Zlib, Limbo, LPSolve 5.5, Lemon, multiple
git submodules for constraint generation, placement, and routing — the
README's own "NOT RECOMMENDED" flag on building from source, unchanged
since the prior spike. Its core idea — symmetry/common-centroid constraints
expressed as solver inputs to an optimization-based placer, rather than
hand- or rule-placed — is real and relevant to *this* spike's scope
specifically (unlike single-cell generation, placement is exactly where
symmetry constraints matter), but the maintenance gap and dependency weight
are unchanged findings, not new ones this spike needed to re-derive.

**Why a constraint solver is heavier than this problem currently needs.**
A general placement-and-routing solver earns its complexity when the
input is under-constrained: many candidate devices, no fixed floorplan,
and a large combinatorial space of legal arrangements to search. That is
MAGICAL's actual problem (and, in its placement stage, ALIGN's). This
spike's problem is smaller and more constrained than that on both ends: **(1)**
inputs already arrive with a fixed shape — each generator response already
reports `bbox_um` (a fixed rectangle) and `ports[]` (fixed positions on
that rectangle's boundary) — there is no floorplan search, only an
*ordering* and *spacing* decision; and **(2)** the connectivity a
first-cut composition step needs to satisfy is a small, explicit net list
supplied by the caller (the connectivity spec), not something to be
inferred or optimized against a cost function. Solving a small, explicitly-
constrained combinatorial problem (place N already-sized rectangles in a
row/grid order, route M explicit nets between named points on their
boundaries) with a general MINLP/ILP-class solver is exactly the kind of
premature-generality the "build only what's needed, backed by real
friction" discipline (ROADMAP.md → "How progress is driven") argues
against — there is no friction-log evidence yet (no block has been placed
by any means through `klt gen`) that a rule-based row/grid assembly is
insufficient for the primitives Epic #152 shipped.

### Cross-cutting takeaways

- **The two sides of the split are not symmetric in evidentiary weight.**
  (a) has a working, license-clear, MIT reference implementation
  (gdsfactory's router) whose *algorithm* is reusable today with zero new
  dependency, plus this repo's own generators already supplying the fixed-
  shape, fixed-port inputs that make the problem simpler than either
  ALIGN's or MAGICAL's. (b) has one credible reference (MAGICAL) that is
  stale, heavy, and — critically — solves a harder, less-constrained
  version of this exact problem than the one this repo currently has.
- **ALIGN's own architecture is the strongest single argument for
  sequencing (a) before (b):** even ALIGN, which *does* use a solver for
  placement, still separates "primitive cell generation" (rule-based, done)
  from "placement and routing" (solver-based) as later, more advanced
  stages — implying rule-based composition is a legitimate, load-bearing
  intermediate step, not merely a corner someone cut.
- **No candidate's routing *code* is both license-clear and
  geometry-engine-compatible with this repo**, mirroring the prior spike's
  identical finding about device-generation code: gdsfactory's router is
  license-clear (MIT) but `gdstk`/`kfactory`-based; MAGICAL's placement
  code is neither actively maintained nor built on anything this repo
  shares.

## 2. Proposed composition contract

Documented in the field-table style `docs/cli/gen.md` and
`docs/json-contract.md` already establish. This is a **proposed** shape for
review — no `klt` subcommand, dependency, or code is added by this spike.
The working name below, `klt gen compose`, extends the existing `gen`
namespace (a composition step is still "generation," of a different unit —
a placed-and-routed *group* of generator outputs rather than one PCell) but
is not a commitment to that exact CLI shape; an eventual implementing epic
may reasonably choose a distinct top-level verb instead. Field names stay
generic (`blocks`, `connectivity`, `placement`) rather than borrowing any
one surveyed framework's vocabulary, per the same "must not leak a single
framework's internals" discipline #104's contract already applied.

### Request

```json
{
  "schema": "klt.gen_compose.request/1",
  "pdk": { "variant": "sky130A", "root": null },
  "blocks": [
    { "id": "diffpair", "generator_report": "diffpair.json" },
    { "id": "mirror", "generator_report": "mirror.json" },
    { "id": "tail", "generator_report": "tail.json" }
  ],
  "placement": {
    "strategy": "row",
    "order": ["diffpair", "mirror", "tail"],
    "spacing_um": 1.0
  },
  "connectivity": [
    {
      "net": "VOUT",
      "pins": [
        { "block": "diffpair", "port": "Q1_D" },
        { "block": "mirror", "port": "M1_D" }
      ]
    },
    {
      "net": "TAIL",
      "pins": [
        { "block": "diffpair", "port": "TAP_S" },
        { "block": "tail", "port": "U0_D" }
      ]
    }
  ],
  "routing": {
    "layer_role": "metal",
    "width_um": 0.17
  },
  "options": {
    "cell_name": "ota_top_0",
    "output": "ota_top_0.gds"
  }
}
```

| Field                          | Type               | Description                                                                                                                                                                    |
| ------------------------------ | ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `schema`                       | string             | Contract identifier and major version. Bumped on any breaking change.                                                                                                          |
| `pdk.variant`/`pdk.root`       | string \| null     | The exact fields `klt pdk find --pdk`/`--pdk-root` already accept — a composition step resolves its PDK through the same one resolver every other verb uses (`docs/cli/pdk.md`), not a private lookup. |
| `blocks[]`                     | array\<object\>    | Each already-generated primitive to place. `id` is a caller-chosen label used to address the block's ports elsewhere in this request; `generator_report` is a path to (or, per §"inline variant" below, an inline copy of) that block's own `klt gen` JSON response (`docs/cli/gen.md`'s response shape) — the composition step's *only* input about a block's geometry is its already-reported `bbox_um`/`ports[]`, never a second, private inspection of the GDS stream. |
| `placement.strategy`           | string             | `"row"` (single horizontal row, left to right in `order`) or `"grid"` (wraps `order` into `placement.cols` columns) — the two rule-based layouts this spike scopes. No `"solver"`/constraint-based strategy is proposed at this phase (§1's build/wrap decision). |
| `placement.order`              | array\<string\>    | Block `id`s in placement order. Every `id` in `blocks[]` must appear exactly once.                                                                                              |
| `placement.spacing_um`         | number             | Fixed gap between adjacent blocks' bounding boxes. Must be `>= 0`.                                                                                                              |
| `connectivity[]`               | array\<object\>    | One entry per net: a `net` label (caller-chosen, for the response's traceability only — no schematic-level net-name resolution is implied) and `pins[]`, each `{block, port}` addressing one named port from that block's own `generator_report.ports[]`. A net with 2 pins is a direct point-to-point route; more than 2 pins is a bundle (per gdsfactory's `route_bundle` precedent, §1) — routed as a single connected tree, not pairwise, to avoid redundant metal. |
| `routing.layer_role`           | string             | A layer *role* (`"metal"`, matching the same role vocabulary `klt gen`'s phase-2 generators already resolve per PDK family via `_PDK_ROLE_LAYERS`, per `docs/cli/gen.md`'s PDK-family support section) — never a raw `{layer, datatype}` pair, so routing stays on the one per-PDK layer resolver every generator already uses. |
| `routing.width_um`             | number             | Route wire width. Must be `> 0`.                                                                                                                                                 |
| `options.cell_name`/`output`   | string             | Same semantics as `klt gen`'s own `options` fields (`docs/cli/gen.md`).                                                                                                        |

**Inline variant:** `blocks[].generator_report` is shown above as a file
path (the shape a caller gets from having already run `klt gen ... -o
diffpair.json --format json > diffpair.json`-style capture) but an
implementing epic should also accept the parsed JSON object inline, mirroring
`klt gen --params`'s own "a path, or an inline JSON object" duality
(`docs/cli/gen.md`) — a caller scripting several `klt gen` calls in sequence
should not be forced to round-trip through the filesystem only to satisfy
this step's input shape.

### Response

```json
{
  "schema_version": 1,
  "cell_name": "ota_top_0",
  "gds_path": "ota_top_0.gds",
  "pdk": { "name": "sky130A", "variant": "sky130A", "version": "open_pdks 0fe599b" },
  "bbox_um": { "x0": 0.0, "y0": 0.0, "x1": 42.1, "y1": 9.6 },
  "blocks": [
    {
      "id": "diffpair",
      "generator": "diff_pair",
      "offset_um": { "x": 0.0, "y": 0.0 },
      "bbox_um": { "x0": 0.0, "y0": 0.0, "x1": 12.4, "y1": 9.6 }
    }
  ],
  "nets": [
    {
      "net": "VOUT",
      "pins": [
        { "block": "diffpair", "port": "Q1_D" },
        { "block": "mirror", "port": "M1_D" }
      ],
      "routed": true,
      "route_length_um": 3.2
    }
  ],
  "unrouted_nets": [],
  "drc_hints": {
    "min_spacing_um": 0.21,
    "matched_groups": [
      {
        "matched_group_id": "diff_pair:pair:2",
        "blocks": ["diffpair"],
        "placement_symmetric": null
      }
    ],
    "notes": []
  },
  "warnings": []
}
```

#### Top-level fields

| Field            | Type              | Description                                                                                                                            |
| ---------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `schema_version` | integer           | Version of this command's JSON shape.                                                                                                 |
| `cell_name`      | string            | Name of the top cell written into `gds_path`, containing every placed block instance plus all routed metal.                            |
| `gds_path`       | string            | Resolved output path.                                                                                                                    |
| `pdk`            | object            | Echo of the resolved PDK reference — same shape `klt gen`'s own response already reports (`docs/cli/gen.md`).                          |
| `bbox_um`        | object            | Bounding box of the *composed* cell (the union of every placed block, after `placement.spacing_um` is applied) — same `_um`-suffixed shape as every existing bounding-box field in this repo. |
| `blocks[]`       | array\<object\>   | Per-block placement result: `id` (echo of the request), `generator` (echoed from that block's own `generator_report.generator`), `offset_um` (the translation applied to place it), and `bbox_um` (that block's bounding box *after* translation, in the composed cell's coordinate frame). |
| `nets[]`         | array\<object\>   | Per-net routing result: echo of `net`/`pins` plus `routed` (boolean) and `route_length_um` (total routed wire length, for a caller doing a first-order parasitic estimate before extraction). |
| `unrouted_nets[]`| array\<string\>   | Net labels the routing step could not connect (e.g. two ports whose Manhattan backbone would require crossing a third block with no channel available at the requested `routing.width_um`) — always present, empty when everything routed. A non-empty array is a **partial success**, not silently dropped connectivity (see exit codes below). |
| `drc_hints`      | object            | Same "advisory, not authoritative" semantics as `klt gen`'s own `drc_hints` (`docs/cli/gen.md`) — see below for the composition-specific fields. |
| `warnings[]`     | array\<string\>   | Non-fatal notes, same discipline as every other verb's `warnings[]`.                                                                    |

#### `drc_hints` fields (composition-specific)

| Field                          | Type                     | Description                                                                                                                                                                    |
| ------------------------------ | ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `min_spacing_um`               | number                   | The tightest spacing actually used across placement and routing (the minimum of `placement.spacing_um` and any routed-wire-to-block clearance) — same purpose as `klt gen`'s own field. |
| `matched_groups[]`             | array\<object\>          | **This is where the composition step consumes `drc_hints.matched_group_id`**, the hook Epic #152 phase 2 (#159) populated on every array/matched-device generator response but that nothing downstream read until now. One entry per distinct `matched_group_id` found among the input `blocks[]`' own `generator_report.drc_hints.matched_group_id` values: `matched_group_id` (echoed), `blocks` (which request-level block `id`s carry it — usually one, but a composition could legitimately place two independently-generated blocks sharing a matching intent, e.g. two halves of a split array), and `placement_symmetric` (boolean \| null — `true`/`false` once a symmetry check is meaningful, e.g. when `placement.strategy` places a matched block on a caller-declared symmetry axis and the composition step verifies the mirrored block's offset is actually symmetric; `null` when no symmetry axis was declared for that group, i.e. the hint was seen but no symmetry claim was made to check). |
| `notes[]`                      | array\<string\>          | Free-form composition-specific notes (e.g. "block `mirror` carries `matched_group_id` but no `placement.symmetry_axis` was declared — matching intent not verified at composition time"). Always present, empty when there is nothing to report. |

**How this differs from doing nothing with the hook (the status quo).**
Before this spike, `matched_group_id` was write-only: `klt gen` populated
it, and no code anywhere read it back. The design above makes reading it
back a first-class part of the composition contract — even at this spike's
minimal, non-solver scope, the composition step is required to *report*
every matched group it saw among its inputs, and *may* (via an optional
`placement.symmetry_axis`, left for the implementing epic to fully specify)
verify a placement claim against it. This is intentionally lighter than
MAGICAL-class symmetry-constrained placement (§1(b)) — it is a check, not a
solve — while still being real consumption of the hook, not merely passing
it through unread a second time.

### Semantics and guarantees

Same guarantees as `klt gen` itself (`docs/cli/gen.md` → "Semantics and
guarantees," inherited from the original spike): the contract is
engine-neutral (nothing names `pya`, `kfactory`, or any router by name),
layer resolution goes through the one PDK role-layer table every generator
already uses, `drc_hints` is advisory not authoritative (`klt drc` remains
the actual rule-compliance authority on the composed output, exactly as it
already is on any single generator's output), PDK resolution goes through
the one resolver, and the envelope is additive.

**One new guarantee specific to composition:** a block's `bbox_um`/`ports[]`
are consumed exactly as its own `klt gen` response reported them — the
composition step never re-derives a block's geometry from its GDS stream.
This keeps the composition contract's only coupling to a generator's output
the same JSON fields `docs/cli/gen.md` already documents, so a future
generator family (or a differently-implemented existing one) composes
identically as long as it reports the same `ports[]`/`bbox_um` shape —
mirroring the same "engine swap invisible to the caller" property the
original generator contract already has relative to its own PCell backend.

### Proposed exit codes

Extending `klt gen`'s own trichotomy (`docs/cli/gen.md`):

| Exit code | Meaning                                                                                                       |
| --------- | ------------------------------------------------------------------------------------------------------------ |
| `0`       | Every block placed and every net routed; `gds_path` was written and the report above is on stdout.            |
| `1`       | Application error — unresolvable PDK, a `blocks[]`/`connectivity[]` reference to a nonexistent `id`/`port`, or an unroutable placement strategy for the given `blocks[]` shape. |
| `2`       | Usage error (missing argument, bad `--format` value) — from argparse.                                          |
| `3`       | **Partial success** — every block placed, but `unrouted_nets[]` is non-empty (mirroring `klt drc`'s own `3` for "ran clean but found violations" — a successful run whose *output* still needs attention, so the documented success payload is still on stdout, not swallowed into a bare error). |

This is the one place this spike's contract diverges from the original
generator contract's stated position ("no third 'partial success' code...
a generator either produces a cell or it doesn't"): unlike single-cell
generation, a composition step legitimately *can* place every block
successfully while failing to route one net (e.g. a channel too narrow at
the requested `routing.width_um`), and that is a materially different,
recoverable outcome from an application error — exactly the case the
original spike flagged as an open question for "a generator family [that]
turns out to need one."

## 3. Build vs wrap decision

Scored against the same three-part rewrite-rule test
[docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "Rewrite rule" applies, adapted
here to a build-vs-wrap-an-external-framework question rather than a
Python-vs-Rust one (the same adaptation `layout-generator-spike.md` §3 made
for its own build/wrap call):

1. **Bottleneck or ceiling — not yet measurable, and, as with the original
   generator spike, that absence is itself the finding.** No block has ever
   been placed and routed through `klt gen` by any means; there is no
   friction log entry showing rule-based row/grid assembly is a capability
   *ceiling* (as opposed to merely not yet built). Scoring "is rule-based
   composition insufficient" before a single reference implementation
   exists to be insufficient would be inventing a number, the same failure
   mode both prior spikes avoided.
2. **Oracle exists — holds.** A composed cell's connectivity can be checked
   against its `connectivity[]` input directly (does every declared net's
   `pins[]` end up electrically connected in the output stream? — a job
   `klt extract`/`klt lvs`, Epic #153, already do generically), and its
   geometry can be checked against `klt drc` exactly as any other generator
   output already is. No new oracle infrastructure is needed beyond what
   Epic #152's own generators and Epic #153's LVS pipeline already provide.
3. **Unlock — a rule-based build unlocks something a wrapped MAGICAL
   dependency structurally cannot: staying inside this repo's one-geometry-
   engine, headless-in-CI posture.** Wrapping MAGICAL means adding Boost,
   Flex, Zlib, Limbo, LPSolve, and Lemon to this repo's dependency surface
   (or a Docker-only invocation path, which does not satisfy "every
   command must be runnable in CI" the way a native `pya`-based
   implementation trivially does) for a capability whose *actual* first-cut
   problem (§1) is small and explicitly constrained enough not to need a
   general solver. Conversely, wrapping gdsfactory's router means running a
   second geometry backend (`gdstk`) alongside `pya` for every composed
   cell, the identical sprawl the original spike already rejected for
   gdsfactory's device generators.

**Recommendation: build, not wrap — a minimal, rule-based row/grid
placement + point-to-point Manhattan routing step, implemented natively
against `pya.Path`/`pya.Region` (§1's KLayout-native finding), reusing
gdsfactory's `route_single`/`route_astar` *algorithm* (Manhattan backbone →
bends/corners → straight fill, falling back to obstacle-avoiding
pathfinding only when a straight backbone would cross another placed
block) as a validated design reference, not a runtime dependency.**
Constraint-solved (MAGICAL-class) placement is not rejected outright — §1
already grants its core idea (symmetry constraints as solver inputs) is
real and eventually relevant — but per the rewrite rule's own bottleneck
criterion, it should wait for a real friction-log entry showing the
rule-based first cut cannot place a real block's matched-device
requirements (the same "measure first" discipline both prior spikes
applied to their own build/wrap calls), not be adopted speculatively ahead
of that evidence.

## 4. Resolution for #164

**This capability, once built along the lines recommended in §3, unblocks
#164 against the #105 worked example's sky130 5T OTA as originally
scoped — no human rescope of #164 is needed.** Checking the concrete case:
a 5T OTA needs (i) a differential input pair, (ii) a current-mirror load,
and (iii) a tail current source, wired together. Every one of those three
blocks is already producible by an existing, shipped `klt gen` generator —
`diff_pair` (with `params.mirror: false`) for (i), the same `diff_pair`
generator (with `params.mirror: true`, per `docs/cli/gen.md`'s own naming-
convention note — "geometry is identical either way") for (ii), and
`mos_array` with `rows=1`/`cols=1` for (iii) — so the composition step this
spike proposes, at its stated minimal (row/grid + point-to-point routing)
scope, is sufficient to assemble a real 5T OTA: three blocks in a row,
`connectivity[]` wiring the differential pair's drains to the mirror's
drains (the `VOUT`/mirror-node nets) and the pair's tail node to the tail
device's drain — exactly the worked example in §2's request above. No
topology in this circuit requires constraint-solved placement (§1(b)) to
realize; a caller-declared `placement.order` (pair, mirror, tail, left to
right) plus point-to-point routing between named ports is structurally
sufficient.

This means **#164's blocker is purely sequencing, not a scope mismatch**:
once a follow-up epic implements the contract in §2 (which, per §3, is not
this issue's job), #164 can proceed against the 5T OTA exactly as it was
originally written — its own Dependencies section already names #186 (this
issue) as the blocker to resolve, and this spike's finding is that
resolving it does not require also rescoping #164 itself.

## 5. Scope proposal for a first implementing epic

Mirroring `layout-generator-spike.md` §4's "first generators" scope
proposal, for whoever eventually spikes the implementation epic:

1. **`placement.strategy: "row"`** first (a single horizontal row in
   caller-declared order) — the minimum needed to assemble the #164 5T OTA
   case in §4. `"grid"` (wrapping into multiple rows) is a natural
   follow-on, not required for the first cut.
2. **Two-pin, point-to-point routing** (a direct Manhattan backbone between
   exactly two ports) before bundle/multi-pin routing — `route_bundle`-style
   many-pin nets (§1) are the natural next increment once two-pin routing
   is proven against a real block.

   *Status (issue #1073): both increments have landed.* Two-pin routing
   shipped with Epic #191 phase 2 and was hardened against real blocks
   (#199/#433/#453/#454/#461/#469/#492/#496/#634/#999/#1057); the bundle
   increment this item reserved is now implemented as
   `gen_compose.route_bundle` — an N-pin net routes as a spanning tree of
   two-pin legs, nearest pair first, every leg going through the same
   `route_two_pin` checks. This item is recorded as *sequencing* rather than
   as a permanent scope boundary, and that sequencing held: bundle routing
   was built on the two-pin primitive rather than instead of it.
3. **`matched_groups[]` reporting** (§2, read-only echo of every
   `matched_group_id` seen) before `placement.symmetry_axis` verification —
   consuming the hook by *reporting* it is strictly simpler than consuming
   it by *verifying* a symmetry claim, and is itself real, non-trivial
   consumption per §2's "how this differs from doing nothing" note.
4. **sky130 first**, matching every prior phase's PDK ordering (Epic #152's
   phases 1–4, Epic #153's phases 1–4) — gf180mcu support as a bonus, not a
   requirement, mirroring #152/#160's "at least one canary" bar.

## Out of scope for this spike

No dependency was added to `pyproject.toml`, no `klt` subcommand was added,
no composition code was written, and no MCP surface was touched. Full
constraint-solved/optimization-based placement (MAGICAL-class) is not
implemented or scheduled — §3 explicitly defers it pending a real
friction-log entry. No specific circuit topology beyond the illustrative
5T OTA case in §4 (used only to test this spike's contract against a real,
already-scoped worked example) is designed here. The `klt gen` primitive
generator families themselves (#152/#159/#176) are untouched.

## Open questions for a follow-up epic

- Whether `placement.strategy: "grid"`'s column-wrapping behavior should
  auto-derive a column count from the input block count, or require an
  explicit `placement.cols` — an implementation-level detail this spike
  does not need to resolve to make its build/wrap call.
- Whether `routing.width_um` should support a per-net override (some nets,
  e.g. a bias net, plausibly wanting a narrower route than a signal net)
  rather than one width for the whole composition request — flagged here,
  not decided.
- How `unrouted_nets[]` (exit code `3`) should interact with a caller
  retrying with a different `placement.order` or wider spacing — this
  spike proposes the field as a diagnostic signal, not an auto-retry loop;
  whether `klt gen compose` itself should attempt alternate orderings
  before giving up is a question for whoever implements it.
- Whether `placement.symmetry_axis` (§2's `matched_groups[].placement_symmetric`
  hook) should be a first-cut requirement or a fast-follow — this spike
  scopes only the read-only `matched_groups[]` report as required (§5,
  item 3), leaving symmetry verification itself as an open question.
- Whether gdsfactory's `route_astar` obstacle-avoidance behavior (§1) is
  needed at the first-cut `"row"` placement strategy (where blocks are, by
  construction, laid out with no gaps a route could get trapped behind) or
  only becomes necessary once `"grid"` placement is added — a question for
  whoever implements the routing step, not this spike.
