# Layout plan execution (`klayout_tools.layout_plan_execute`)

**Phase C** of the netlist-driven-layout epic
([`docs/design/netlist-driven-layout-spike.md`](../design/netlist-driven-layout-spike.md),
section 3 for the compilation strategy, section 4 for the phase breakdown)
— the phase that turns a validated
[`klt.layout_plan.request/1`](layout-plan.md) document into a real,
generated/placed/routed layout. Phases A
([`netlist_digest.py`](../../src/klayout_tools/netlist_digest.py)) and B
([`layout_plan.py`](../../src/klayout_tools/layout_plan.py),
[`layout-plan.md`](layout-plan.md)) are the prerequisites this phase builds
on; [`klt gen`](gen.md) and [`klt gen-compose`](gen-compose.md) are the two
already-shipped verbs it compiles onto — this phase adds no new
generator, no new placement/routing engine, and (per the same "library
first" posture Phase B chose) no new `klt` CLI subcommand.

> **There is no `klt layout-plan execute` verb.** Like Phase B, this ships
> as a **library-level** module
> ([`src/klayout_tools/layout_plan_execute.py`](../../src/klayout_tools/layout_plan_execute.py)).
> This file lives under `docs/cli/` because it documents a request/response
> contract in the field-table convention every other `klt`-family verb's
> doc uses, not because a subcommand exists. Revisit once a real caller
> wants to run this from the shell rather than from a larger pipeline.

## What execution does

Per [`layout-plan.md`](layout-plan.md)'s own "What it deliberately does not
do" table, Phase B stops at "is this plan internally consistent." Execution
picks up from there, entirely by calling existing, already-shipped
machinery:

1. **Per-group generation** — resolve each `device_groups[]` entry's `klt
   gen` `params` (netlist-derived sizing from the Phase A digest, with
   `params` overrides layered on top — see "Parameter resolution" below),
   then call [`klt gen`](gen.md)'s `generate()`.
2. **Row-of-rows placement** — compile `rows[]` (plural, stacked
   vertically) onto [`klt gen-compose`](gen-compose.md)'s existing
   `"explicit"` placement strategy: each row's horizontal offsets reuse
   `compute_row_offsets` unchanged; rows stack vertically by each
   preceding row's tallest group, with no additional inter-row margin.
3. **Abutment as a placement constraint** — `abutment[]` (edge-to-edge,
   with a declared `gap_um`) is solved entirely in this module, as a
   translation applied on top of the row-of-rows result, before
   `gen-compose`'s `"explicit"` strategy ever runs.
4. **Derived connectivity** — the netlist digest's own device-to-net map is
   compiled into `gen-compose`'s existing `connectivity[]`; a net touching
   three or more device-group ports becomes an ordinary bundle entry —
   `gen-compose.compose()` already routes every `connectivity[]` entry
   through `route_bundle()` regardless of pin count (issue #1073), so this
   module never calls the router directly.
5. **Composition** — one [`klt gen-compose`](gen-compose.md) `compose()`
   call does the actual generation/placement/routing pass.

No new placement or routing primitive is added anywhere in this phase —
every one already existed in `gen_compose.py`.

## Using it

```python
from klayout_tools.layout_plan_execute import (
    LayoutPlanExecuteError,
    exit_code_for,
    execute_layout_plan,             # plan + an already-built digest
    execute_layout_plan_document,    # plan; builds the digest itself
    partial_success,
)
from klayout_tools.layout_plan import LayoutPlanError  # Phase B's own errors

try:
    response = execute_layout_plan_document(request, request_dir=".")
except LayoutPlanError as exc:      # Phase B validation failure
    raise SystemExit(1)
except LayoutPlanExecuteError as exc:  # Phase C execution failure
    raise SystemExit(1)
raise SystemExit(exit_code_for(response))
```

- `execute_layout_plan(request, digest, *, request_dir=None, work_dir=None)`
  — the pure-ish function: `digest` is a
  [`build_netlist_digest()`](../../src/klayout_tools/netlist_digest.py)
  result the request's own `netlist` fields were ingested from. Calls
  [`layout_plan.validate_layout_plan`](layout-plan.md) itself — never a
  second, independent validation pass — before doing anything else.
- `execute_layout_plan_document(request, *, request_dir=None, work_dir=None)`
  — builds the digest itself from the plan's own `netlist` fields,
  mirroring `validate_layout_plan_document`'s own convenience-wrapper
  shape.
- `work_dir` is where each per-group `klt gen` GDS artifact lands before
  `compose()` reads it back in. Defaults to a fresh, auto-cleaned temporary
  directory — the composed output is fully self-contained afterwards, so
  nothing under `work_dir` needs to survive the call. Pass an explicit
  directory to keep the intermediate per-group artifacts around for
  inspection.
- `partial_success(response)` / `exit_code_for(response)` — see "Exit
  codes" below.

## Parameter resolution

For every `device_groups[]` entry, this module resolves the `params` a
`klt gen` request needs in two layers, in this order:

1. **Netlist-derived sizing** — for the families that carry a real
   length/width-style parameter in the Phase A digest *and* map one
   netlist device onto one generated unit device:

   | Generator | Digest params consumed | `klt gen` params set |
   | --- | --- | --- |
   | `mos_array`, `diff_pair` | MOS `L`, `W` | `l_um`, `w_um` |
   | `esd_device` | MOS `L`, `W` | `l_um`, `finger_width_um` |
   | `res_array` | resistor `L`, `W` | `length_um`, `width_um` |

   A digest value of exactly `0.0` (KLayout's own structural default for
   an unset device-class parameter — e.g. a resistor written in the bare
   `R<name> n1 n2 <ohms>` SPICE form, with no `L=`/`W=` at all) is treated
   as "the netlist did not specify a geometric size," not "the netlist
   specifies a zero-sized device" — it is skipped, falling back to the
   generator's own documented default (or a `params` override) instead of
   handing the generator a rejected value. `bjt_array` gets no
   netlist-derived sizing at all — a real schematic-flow bipolar device
   (sky130's fixed-geometry `pnp_05v5` family) carries no length/width
   call-site parameter to derive from. `resistor_strip`, `guard_ring`,
   `cap_array`, and `bond_pad` get none either — `resistor_strip` draws its
   whole unit-resistor string as **one** 2-port cell with no per-device
   mapping to derive from (its DRC-clean successor `res_array` is the
   generator to use for a real netlist-driven block); the other three are
   enclosure/boundary generators with no per-device sizing concept.

   When a group's devices disagree on a netlist-derived value (a
   pathological case — every unit in a matched group should share the same
   schematic sizing), the first device's value wins and every disagreement
   is recorded in `warnings[]`, never silently averaged or dropped.

2. **`device_groups[].topology`** (when declared) is layered in as
   `params.topology` — the plan's own top-level matching-pattern field maps
   directly onto the identically-named `klt gen` params field
   `mos_array`/`bjt_array` already accept.

3. **`device_groups[].params` overrides** (read from the request document,
   not from Phase B's validated response — Phase B only shape-checks
   `params`, so it does not echo it back) are applied last, winning over
   both of the above. A `params` override that diverges from a
   netlist-derived value is legitimate (e.g. a layout-only fudge factor)
   but is always reported in `warnings[]`, never silent, per the spike's
   own "advisory, not authoritative" precedent for a schematic/layout
   sizing mismatch.

## Connectivity derivation

A `device_groups[]` entry's `devices[]` (already resolved by Phase B to
fully-qualified `{name, device_class}` pairs) is looked up directly in the
digest to read each device's own `terminals` map (`{terminal_name:
net_name}`). Which generated port a given terminal corresponds to is
resolved generically, without hardcoding any generator's port-*prefix*
convention: every port in that group's own `generate()` response whose
`name` ends in `_<suffix>` (e.g. `_S`, `_D`, `_G`, `_A`, `_B`) is a
candidate for that suffix, in the order `generate()` emitted them (always
per-unit-device contiguous); the `k`-th declared device in
`device_groups[].devices` binds to the `k`-th candidate for each of its own
terminals' suffixes.

| Generator | Digest terminals mapped | Generated port suffix |
| --- | --- | --- |
| `mos_array`, `diff_pair`, `esd_device` | `S`, `D`, `G` | `_S`, `_D`, `_G` |
| `bjt_array` | `E`, `B` | `_E`, `_B` |
| `res_array` | `A`, `B` | `_A`, `_B` |
| `cap_array` | `A`, `B` | `_BOT`, `_TOP` |

A terminal absent from a generator's own row above (MOS `B`/bulk, BJT
`C`/collector) is **deliberately** unmapped — both conventionally tie to a
shared substrate/well tap a `guard_ring`/collector ring already owns, not
to a per-device port. `guard_ring`, `bond_pad`, and `resistor_strip` have no
row at all — no per-device port concept to map onto (see "Parameter
resolution" above for the last one). A device index past the end of a
suffix's own candidate list (every unit-arity mismatch, including
`mos_array`'s `finger_topology: "series"` mode, whose per-finger port names
never end in a bare `_S`/`_D`/`_G`) degrades to a `warnings[]` entry and
that one pin is simply left off its net — never a wrong, silently-bound
pin.

**This assumes a plan author declares `device_groups[].devices` in the same
relative order the named generator numbers its own unit instances.** This
is exact for `topology: "array"` (row-major — the natural declaration
order) and for every non-reordering generator; a `topology:
"common_centroid"` plan still resolves deterministically, but is only
correctly *paired* electrically when the author's declaration order already
matches the generator's own nearest-center-first numbering — this module
does not reconstruct that numbering itself.

Every digest net with two or more resolved pins becomes one
`gen-compose` `connectivity[]` entry (two pins or ten — `compose()` already
routes every entry through `route_bundle()` regardless of count). A net
resolving to exactly one pin has nothing to route and is silently skipped
(not an error, not `unmapped_netlist_nets[]` — see below). A net resolving
to zero pins across every declared device — including a supply/bulk net no
device's mapped terminal ever reaches — lands in `unmapped_netlist_nets[]`
unconditionally; see "Response" below.

## Row-of-rows placement and abutment

`rows[]` compiles onto `gen-compose`'s `"explicit"` placement strategy
exactly as the spike's section 3 describes: per row,
`compute_row_offsets()` computes horizontal offsets unchanged; rows then
stack vertically, each row's baseline sitting exactly at the running total
of every preceding row's tallest group's height (no additional inter-row
margin — the spike names none). `rows[].align` (`"bottom"`/`"top"`/
`"center"`) positions each group within its own row's vertical band
relative to the row's tallest member.

`abutment[]` is resolved as a final constraint pass, once every `rows[]`-
listed group has an offset: for a pair `{a, b, edge, gap_um}`, the side that
already has a placement anchors the other, translating it so the declared
`edge` sits exactly `gap_um` away. A group with **no** `rows[]` entry at all
(e.g. an enclosure-shaped `guard_ring` group) can still be placed this way,
purely via an `abutment[]` chain anchored to a row-placed group — resolved
iteratively until every group has an offset. A group reachable by *neither*
`rows[]` nor a resolvable `abutment[]` chain is an application error (exit
1): `device_groups[].encloses` is **not** consumed for automatic
enclosure sizing/placement in this phase (see "Scope decisions" below), so
it does not, on its own, place anything.

**Abutment always wins over a row-derived position on the same pair** (the
spike's own open question, resolved here): applying an abutment constraint
to an already row-placed pair changes *only* the coordinate along the
abutment's own axis (top/bottom → the vertical axis; left/right →
horizontal), leaving the perpendicular axis exactly as the row placement
left it. A `warnings[]` entry records every time this actually changes an
already-placed group's offset, so the override is never silent.

## Response

Extends [`klt gen-compose`](gen-compose.md)'s response shape (per the
spike's section 2 "Response") rather than inventing a parallel one, since
execution's terminal step *is* a composition:

```json
{
  "schema_version": 1,
  "cell_name": "bandgap_top_0",
  "gds_path": "bandgap_top_0.gds",
  "pdk": { "name": "sky130A", "variant": "sky130A", "version": "open_pdks 0fe599b" },
  "bbox_um": { "x0": 0.0, "y0": 0.0, "x1": 58.3, "y1": 22.4 },
  "device_groups": [
    {
      "id": "diffpair",
      "generator": "diff_pair",
      "devices": [{ "name": "1", "device_class": "PFET" }, { "name": "2", "device_class": "PFET" }],
      "resolved_params": { "w_um": 2.0, "l_um": 0.5 },
      "offset_um": { "x": 0.0, "y": 0.0 },
      "bbox_um": { "x0": 0.0, "y0": 0.0, "x1": 12.4, "y1": 9.6 }
    }
  ],
  "nets": [
    {
      "net": "vbe1",
      "pins": [
        { "block": "diffpair", "port": "Q1_0_G" },
        { "block": "rref_string", "port": "R0_A" }
      ],
      "routed": true,
      "route_length_um": 3.2,
      "legs": [ { "pins": [ /* ... */ ], "routed": true, "route_length_um": 3.2, "reason": null } ]
    }
  ],
  "pins": [],
  "unrouted_nets": [],
  "unmapped_netlist_nets": [],
  "drc_hints": { "min_spacing_um": null, "matched_groups": [], "notes": [] },
  "warnings": []
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | integer | Bumped only on a non-additive change to this response shape. |
| `cell_name` / `gds_path` / `pdk` / `bbox_um` / `pins[]` / `drc_hints` | — | Echoed unchanged from `gen-compose.compose()`'s own response — see [`gen-compose.md`](gen-compose.md). |
| `device_groups[]` | array\<object\> | Replaces `gen-compose`'s `blocks[]` — per group: `id`, `generator`, `devices` (fully qualified, from Phase B), `resolved_params` (see "Parameter resolution"), `offset_um`, `bbox_um` (the latter two straight from `compose()`'s own `blocks[]`). |
| `nets[]` | array\<object\> | Straight from `gen-compose.compose()` — `net`, `pins[]`, `routed`, `route_length_um`, `legs[]` (a 2-pin net is the degenerate one-leg case of the same shape a bundle net uses). |
| `unrouted_nets[]` | array\<string\> | Straight from `gen-compose.compose()` — every placed group, but this net's pins could not all be connected. |
| `unmapped_netlist_nets[]` | array\<string\> | **The one field with no `gen-compose` analogue.** Every digest net that resolved to *zero* `device_groups[]` ports — including a deliberately unrouted supply/bulk net (`VDD`/`VSS`-style, or a MOS bulk/BJT collector terminal, both deliberately unmapped — see "Connectivity derivation"). Always present, empty when every netlist net touching a declared group's devices resolved to at least one port. No `netlist.ignore_nets[]`-style opt-out exists — a caller judges an entry here benign or not; nothing is silently dropped. |
| `warnings[]` | array\<string\> | Netlist-derived-vs-override parameter divergences, abutment-overrides-rows notices, and connectivity-derivation degradations (an index past a suffix's candidate list), plus `gen-compose.compose()`'s own warnings (e.g. an `"explicit"`-placement clearance advisory). |

## Exit codes

| Exit code | Meaning |
| --- | --- |
| `0` | Every device group generated, every row/abutment placed, every resolvable net routed, and `unmapped_netlist_nets[]` is empty. |
| `1` | `LayoutPlanExecuteError` (or a propagated `klayout_tools.layout_plan.LayoutPlanError` from Phase B's own validation) — an unresolvable PDK, a generation failure (an invalid resolved `params` value), an empty `device_groups[]` (nothing to build), a `device_groups[]` entry with no `rows[]`/`abutment[]` path to a placed position, or a `gen-compose.compose()` failure. |
| `2` | Reserved for a future CLI subcommand's own argparse usage-error layer — this phase adds none. |
| `3` | Partial success — `partial_success(response)` is `True`: every group placed, but `unrouted_nets[]` and/or `unmapped_netlist_nets[]` is non-empty. `exit_code_for(response)` returns this. |

## Scope decisions (this increment)

The acceptance criteria this phase shipped against left several choices to
the implementer; recorded here (and in the module's own docstring) rather
than only in a PR description:

- **`device_groups[].encloses` is not consumed for automatic enclosure
  sizing/placement.** A `guard_ring`-shaped group still needs a `rows[]`
  entry or a resolvable `abutment[]` chain to be placed at all in this
  phase; its `inner_width_um`/`inner_height_um` come from `params`
  overrides or the generator's own defaults, never auto-derived from the
  groups it `encloses`.
- **Netlist-derived sizing** is scoped to the generator families listed
  under "Parameter resolution" above — see that section for the reasoning
  per family.
- **Connectivity/port derivation** assumes declaration-order-matches-
  generator-numbering — see "Connectivity derivation" above.
- **Abutment always overrides a row-derived position** on the same pair,
  with a `warnings[]` entry recording every override.
- **`unmapped_netlist_nets[]` reports a deliberately-unrouted supply net
  unconditionally** — no request-schema extension (e.g.
  `netlist.ignore_nets[]`) was added.
- **No `klt` CLI subcommand** — library-only surface, mirroring Phase B.
- **`request.routing`** — Phase B's merged schema has no `routing` field;
  this module accepts an optional, additive one (`layer_role`/`width_um`,
  identical shape to `gen-compose.compose()`'s own), defaulting to
  `{"layer_role": "metal", "width_um": 0.17}` when omitted.

## See also

- [`docs/design/netlist-driven-layout-spike.md`](../design/netlist-driven-layout-spike.md)
  — the accepted spike this phase implements (section 3 for the
  compilation strategy, section 4 for the phase breakdown).
- [`layout-plan.md`](layout-plan.md) — Phase B's contract + validator,
  reused unchanged as this phase's entry validation step.
- [`gen.md`](gen.md) / [`gen-compose.md`](gen-compose.md) — the two
  already-shipped verbs this phase compiles onto; no new generator or
  placement/routing primitive is added anywhere in this phase.
