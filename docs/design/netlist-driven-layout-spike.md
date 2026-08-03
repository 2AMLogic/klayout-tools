# Spike: netlist-driven block-layout builder (plan-driven `klt gen`/`klt gen compose`)

**Status:** spike / proposal. Nothing here is scheduled, and nothing here
authorises implementation. Per [docs/ARCHITECTURE.md](../ARCHITECTURE.md) →
"How capabilities arrive," a major capability arrives by spiking a design
epic first — a scoped survey, a proposed JSON contract, and a build/wrap
decision — and this document is that spike for the gap issue #346 records:
there is no `klt` path from an arbitrary block's netlist to a placed,
routed layout. It follows the structure the two accepted prior spikes set:
[docs/design/layout-generator-spike.md](layout-generator-spike.md) (#104,
`klt gen`'s single-cell primitives) and
[docs/design/gen-composition-spike.md](gen-composition-spike.md) (#186,
`klt gen compose`'s placement/routing of already-generated blocks). This is
the **third** spike on the same underlying gap: both prior documents named
exactly this capability and deliberately deferred it, on the grounds that it
was not yet concrete enough to design. A follow-up epic, decomposed into
implementation sub-issues (§4), would carry the build if one is chosen —
this document adds no dependency, no `klt` subcommand, and no generator or
composition code.

**Demand signal.** `layout-generator-spike.md` §1 scoped single-cell PCell
generation and explicitly deferred "placement and routing of multiple
generated blocks." `gen-composition-spike.md` shipped as `klt gen compose`
(`src/klayout_tools/gen_compose.py`) — placement (`"row"`/`"explicit"`
strategies) and two-pin Manhattan routing of *pre-generated* `klt gen`
blocks, driven by a caller-authored `blocks[]`/`connectivity[]` request. Its
own "Out of scope" section defers "netlist-driven / constraint-solved
placement" a second time. Issue #346 is the friction record that makes the
deferred gap concrete: gf180-bandgap (a public canary block, external to
this repo) shipped its layout by hand-writing `layout/bandgap_top/generate.py`
+ `netlist_model.py` (parses and flattens the xschem netlist) +
`plan.py` (a declarative row/matching plan) directly against `klayout.db`,
because — per its own docstring — `klt` had no verb that takes a real
block's netlist and a matching plan and produces a layout; `klt gen` only
runs named primitives one at a time, and `klt gen compose` only wires
already-generated blocks the caller names by hand. The bespoke generator
works (551 KB deterministic GDS, DRC/LVS-checked), but nothing in it is
reusable through a contract, and every future block repo faces the same
build-it-yourself wall. This spike's job is to turn "no schema exists yet"
(the Curator's finding when scoping #346 down from a direct implementation)
into a schema someone can decompose against.

## 1. Netlist ingestion: survey and sufficiency verdict

### The direction correction, restated precisely

`klt extract` (`src/klayout_tools/extract.py`) runs `kdb.LayoutToNetlist`
plus `NetlistSpiceWriter` — **layout → netlist**, extracting devices and
connectivity from GDS/OASIS geometry and *writing* SPICE text. It has no
custom SPICE *parser*; it is the wrong direction for "read an existing block
netlist, drive a layout from it," and reading it as a candidate for this
gap (the original issue text's item 1 implicitly did) was the Curator's
correction. The parser this gap actually needs already exists, one layer
away: `src/klayout_tools/lvs.py`'s `_read_reference_netlist`
(lvs.py:595-...) constructs a `kdb.Netlist()` and reads SPICE text into it
via `kdb.NetlistSpiceReader()` (lvs.py:583-586, and again at lvs.py:683-684
for the reference side) — **SPICE text → device/connectivity graph**, the
correct direction. It is wired today to produce one side of an LVS
comparison, not to drive placement, but the object model it produces is
generic and reusable regardless of what consumes it next.

### What `kdb.Netlist`/`NetlistSpiceReader` actually gives a caller

Reading `lvs.py`'s own use of the resulting `kdb.Netlist` (not just the
parse call) shows the shape of the graph a plan-driven generator would walk:

- `netlist.each_circuit()` — one `kdb.Circuit` per `.subckt`/top-level
  circuit (a hierarchical netlist parses into a hierarchy of circuits, not
  a single flat list — `lvs.py`'s `_select_circuit` (lvs.py:698) already
  picks the top circuit by name or by "the only one with no callers").
- `circuit.each_device()` — every device instance in that circuit, each a
  `kdb.Device` carrying `device.device_class()` (a typed class object, e.g.
  the curated MOS/resistor/capacitor/bipolar classes `pdk_models.py`
  resolves — see `lvs.py`:1017-1018, 1057-1058, 1078-1079 filtering devices
  by `device_class().name`), `device.expanded_name()` (the instance name,
  e.g. `M1`), and per-terminal net connectivity via `device.each_terminal()`
  / a device class's own `terminal_definitions()` (`lvs.py`:547, 561 — used
  today to walk parameters/terminals for the LVS mismatch report).
- `circuit.each_net()` / `circuit.net_by_name()` — every net in the circuit,
  each carrying `net.each_terminal()` (which device terminals attach to this
  net) and `net.pin_count()`/`net.subcircuit_pin_count()` (whether the net is
  externally visible or purely internal — used in `lvs.py`:1328-1332 to
  distinguish a genuinely floating net from a legitimately internal one).
- `circuit.pin_by_name()` / `circuit.pin_count()` — the circuit's own
  external terminals (its `.subckt` pin list), the natural anchor for "which
  nets are this block's I/O" when a plan needs to know what to leave
  unrouted-to-nothing versus what must reach the composed cell's boundary.

This is a real, walkable device/connectivity graph — not merely "SPICE
parses without crashing." Nothing about it is LVS-specific; `lvs.py`'s use
of it (comparing two circuits) is one consumer, and a plan-driven generator
walking the same graph to decide placement/matching is a legitimate second
consumer of the identical parse.

### `netlist_normalize.py`'s role, and its real limit

Real open-PDK schematic-capture flows (xschem/ngspice) do not emit the
plain-element form `NetlistSpiceReader` reads cleanly for a curated PDK
device — they emit a *subcircuit call* (`XM1 d g s b nfet_03v3 L=0.5u
W=1u ...`), because sky130/gf180mcu ship their primitive MOS as a `.subckt`.
Handed to `NetlistSpiceReader` as-is, each such `X` card reads as an
instance of an *undefined* subcircuit and the graph degrades. `lvs.py`'s
`_read_reference_netlist` already anticipates exactly this
(`form="subckt-call"`, lvs.py:614-618) and delegates the text-level rewrite
to `netlist_normalize.py`
(`normalize_reference_netlist`/`detect_subckt_call_devices`), which resolves
device names through the same curated table `pdk_models.py` already
maintains for the opposite (plain-element → subckt-call) direction used by
`klt extract --pdk` (issue #341, `create_model_binding_delegate`). This is
exactly the ingestion step a netlist-driven generator needs and would
otherwise have to reinvent — the "form" duality is precisely "does this
netlist look like a hand-authored plain-element netlist, or a real
schematic-flow SPICE deck," which is what any real bandgap/OTA/etc. netlist
sourced from a schematic tool will be.

Its real limit, read directly from its own module docstring and
`_CARRIED_PARAMS`: **it is scoped to MOS devices only, and only `L`/`W`
survive the rewrite** (`ad`/`as`/`pd`/`ps`/`nrd`/`nrs`/`sa`/`sb`/`sd` are
dropped by design, and `nf`/`m` > 1 is a hard rejection, not a
degrade-silently case). A real bandgap-class block's netlist has resistors,
capacitors, and (per the sky130-bandgap-reference KB entry's own topology)
bipolar diode-connected devices — device families `netlist_normalize.py`
does not carry parameters for today. This is not a dead end: issue #341
(landed just before this spike, `87043e1`) already extended the *opposite*
direction — `klt extract --pdk`'s `create_model_binding_delegate` now binds
resistor/capacitor/bipolar devices to PDK subcircuits, meaning the curated
device-class table `pdk_models.py` maintains already models those device
families' terminals and parameters; `netlist_normalize.py`'s subckt-call
rewrite would need parallel extension (new `_CARRIED_PARAMS`-equivalents per
device family) to ingest a resistor/capacitor/bipolar-bearing schematic-flow
netlist the same way it does MOS today. That is real, scoped, additive work
on an existing module — not a case for a new parser.

### Sufficiency verdict

**Sufficient as the ingestion substrate; not sufficient as the whole of
"ingestion."** `kdb.Netlist`/`NetlistSpiceReader` (via `lvs.py`'s two
`form=` paths, generalizing through `netlist_normalize.py`) already provide
a real, walkable device/connectivity graph from SPICE text, covering both
netlist dialects (plain-element and subcircuit-call) a real block will
plausibly arrive in. Two additive gaps, both scoped extensions of existing
modules rather than new parsers:

1. **`netlist_normalize.py`'s carried-parameter set is MOS-only.** Extending
   it to resistor/capacitor/bipolar devices (mirroring #341's device-class
   work on the write side) is required before a plan-driven generator can
   ingest a real analog block's full device mix, not just its transistors.
2. **Neither module produces a plain, JSON-serializable digest.** Both
   return/operate on live `kdb.Netlist`/`kdb.Circuit` SWIG objects — correct
   for `lvs.py`'s in-process comparison, but not directly a caller-facing
   contract (the objects are not JSON; there is no `docs/schemas/*netlist*`
   equivalent). A thin adapter — walk the parsed `kdb.Netlist` once into a
   plain Python/JSON device list (`{name, device_class, terminals:
   {terminal_name: net_name}, params}`) and net list (`{name, terminals:
   [...], is_pin}`) — is new code this spike proposes as **Phase A** (§4),
   built *on* `lvs.py`'s reader/normalize path, not a parallel one. This
   digest is also the natural place to keep ingestion engine-neutral per
   `docs/ARCHITECTURE.md`'s contract-first rule: nothing downstream of Phase
   A needs to import `klayout.db` directly to read a device list.

No new SPICE parser, and no reuse of `extract.py`, is proposed anywhere in
this spike.

## 2. Proposed declarative plan contract

Field-table style, matching `docs/cli/gen.md`/`docs/cli/gen-compose.md`'s
established convention. This is a **proposed** shape for review — no `klt`
subcommand, dependency, or code is added by this spike. Informed by, but
deliberately not copying, gf180-bandgap's `plan.py` (external to this
checkout; described only by the issue text, not read directly — the shape
below is derived independently from what a row/matching/abutment plan
structurally needs, cross-checked against this repo's own generator/
composition contracts rather than against `plan.py`'s actual Python). Field
names stay generic (`device_groups`, `rows`, `abutment`) rather than naming
any one external framework, per the same discipline both prior spikes'
contracts applied.

### The core idea: a plan sits *between* an ingested netlist and existing `klt gen`/`klt gen compose` calls

A plan does not describe geometry — it describes **intent**: which netlist
devices form a matched group, what topology that group should use, what
order groups occupy in a row, and which groups must abut rather than be
routed apart. Executing a plan (§3) means using that intent plus the
ingested netlist's own device parameters (§1) to synthesize the
already-existing `klt gen` request per group and the already-existing `klt
gen compose` request for the whole block — the plan itself never draws a
polygon.

### Request

```json
{
  "schema": "klt.layout_plan.request/1",
  "netlist": {
    "path": "bandgap_core.spice",
    "top": "bandgap_core",
    "form": "subckt-call",
    "deck": "sky130A"
  },
  "pdk": { "variant": "sky130A" },
  "device_groups": [
    {
      "id": "diffpair",
      "devices": ["MP1", "MP2"],
      "generator": "diff_pair",
      "topology": "common_centroid",
      "dummy": { "rows": 1, "cols": 1 }
    },
    {
      "id": "rref_string",
      "devices": ["R1", "R2", "R3", "R4"],
      "generator": "res_array",
      "topology": "interdigitated",
      "dummy": { "leading": 1, "trailing": 1 }
    },
    {
      "id": "core_guard_ring",
      "devices": [],
      "generator": "guard_ring",
      "encloses": ["diffpair", "rref_string"],
      "params": { "tap": "pwell" }
    }
  ],
  "rows": [
    { "order": ["diffpair", "mirror"], "spacing_um": 1.0, "align": "bottom" },
    { "order": ["rref_string", "tail"], "spacing_um": 1.0, "align": "bottom" }
  ],
  "abutment": [
    { "a": "diffpair", "b": "core_guard_ring", "edge": "top", "gap_um": 0.0 }
  ],
  "options": { "cell_name": "bandgap_top_0", "output": "bandgap_top_0.gds" }
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema` | string | Contract identifier and major version. Bumped on any breaking change. |
| `netlist.path`/`top`/`form`/`deck` | string | The **same** shape `klt lvs`'s reference-side request already accepts (`docs/cli/lvs.md`, `lvs.py`'s `_read_reference_netlist`) — a plan resolves its netlist through the identical ingestion path, never a private parse. |
| `pdk.variant`/`pdk.root` | string \| null | The exact fields `klt pdk find`/`klt gen`/`klt gen compose` already accept — one resolver, no exception for plans. |
| `device_groups[]` | array\<object\> | One entry per matched/placed unit. See below. |
| `device_groups[].id` | string | Caller-chosen label, addressed elsewhere in `rows`/`abutment` — mirrors `klt gen compose`'s `blocks[].id`. |
| `device_groups[].devices` | array\<string\> | Netlist device instance names (the ingestion digest's, §1 Phase A) this group is generated from. Every name must resolve in the ingested netlist digest — an unresolvable name is an application error, exit code `1`, mirroring `klt gen compose`'s existing `connectivity[]`-reference validation. Empty for a group like a guard ring that encloses other groups rather than wrapping specific devices (see `encloses`). |
| `device_groups[].generator` | string | Which existing `klt gen` generator produces this group's geometry (`mos_array`, `res_array`, `guard_ring`, `diff_pair`, `bjt_array`, `resistor_strip` — `docs/cli/gen.md`'s existing set; a plan invents no new generator). |
| `device_groups[].topology` | string | `"array"`, `"common_centroid"`, `"interdigitated"`, or `"single"` — the matching pattern for this group. `"array"`/`"common_centroid"` map directly onto the `topology` param `mos_array`/`bjt_array` already accept (`gen.py`); `"interdigitated"` (ABAB placement of alternating-identity unit devices, e.g. two resistor strings woven together) and `"single"` (one instance, no matching) are new values this spike proposes generators grow support for, not values that exist in `klt gen` today — flagged explicitly as new generator-side scope, not something this contract alone delivers (§4 Phase C). |
| `device_groups[].dummy` | object | Leading/trailing or row/col dummy-element counts — the same concept `mos_array`/`res_array`'s own `dummy_rows`/`dummy_cols`-style params already expose per-generator; a plan's job is to route this value to the right generator param, not to invent a second dummy-element convention. |
| `device_groups[].encloses` | array\<string\> | For an enclosure-shaped generator (`guard_ring`): which other `device_groups[].id`s it must surround, used to size/place the ring from the enclosed groups' already-placed bounding boxes rather than a caller-guessed `inner_width_um`/`inner_height_um`. |
| `device_groups[].params` | object | Generator-specific overrides layered on top of parameters the plan executor (§3) resolves automatically from the ingested netlist's own device sizing (`w_um`/`l_um`/`fingers` for MOS, resistor length/width, etc. — the schematic-driven sizing handoff `docs/ARCHITECTURE.md`'s vision line names: spec → schematic/generator → **sized circuit** → layout). A plan does not require the caller to re-type sizes the netlist already states. |
| `rows[]` | array\<object\> | Ordered rows, stacked vertically — this is the "grid"/multi-row placement neither `klt gen compose`'s `"row"` (single row only) nor its still-unimplemented `"grid"` strategy (`gen-composition-spike.md` §5, open question) currently expresses on its own; see §3 for how a plan compiles this without requiring a new `gen_compose` placement strategy. |
| `rows[].order` | array\<string\> | `device_groups[].id`s placed left-to-right in this row. |
| `rows[].spacing_um` | number | Horizontal gap between adjacent groups in this row — same semantics as `klt gen compose`'s `placement.spacing_um`. |
| `rows[].align` | string | `"bottom"`/`"center"`/`"top"` — vertical alignment of this row's groups relative to each other (groups in one row are not guaranteed the same height). |
| `abutment[]` | array\<object\> | Pairs of groups that must share an edge with a caller-declared `gap_um` (typically `0.0`) rather than being connected by a routed net — e.g. a guard ring directly against the devices it protects, or two matched groups sharing a well/substrate tap with no metal gap. This is the one placement relationship `klt gen compose`'s row/explicit strategies do not express today (they place independently-spaced blocks in a row or at caller-given absolute origins, never "these two must touch"). |
| `abutment[].edge` | string | Which edge of `a` touches `b` (`"top"`/`"bottom"`/`"left"`/`"right"`). |
| `options.cell_name`/`output` | string | Same semantics as `klt gen compose`'s own `options` fields. |

**Connectivity is derived, not hand-declared.** Unlike `klt gen compose`'s
`connectivity[]` (which the caller writes by hand, naming block/port pairs),
a plan's connectivity comes from the ingested netlist digest itself (§1): a
net connecting two devices in two different `device_groups[]` is a net the
plan executor (§3) must route between those groups' generated ports,
resolved automatically from the netlist's own terminal-to-net map. This is
the concrete payoff of doing ingestion first — a plan author declares
*placement intent* (groupings, topology, order, abutment); the *wiring* is
recovered from the schematic, the same way a human PCB/analog layout
engineer works from a netlist rather than re-declaring it.

### Response

Extends `klt gen compose`'s response shape (`docs/cli/gen-compose.md`)
rather than inventing a parallel one, since plan execution's terminal step
*is* a composition (§3):

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
      "devices": ["MP1", "MP2"],
      "resolved_params": { "w_um": 2.0, "l_um": 0.5, "fingers": 2 },
      "offset_um": { "x": 0.0, "y": 0.0 },
      "bbox_um": { "x0": 0.0, "y0": 0.0, "x1": 12.4, "y1": 9.6 }
    }
  ],
  "nets": [
    {
      "net": "vbe1",
      "pins": [
        { "device_group": "diffpair", "port": "Q1_G" },
        { "device_group": "rref_string", "port": "R1_A" }
      ],
      "routed": true,
      "route_length_um": 3.2
    }
  ],
  "unrouted_nets": [],
  "unmapped_netlist_nets": [],
  "drc_hints": {
    "min_spacing_um": 0.21,
    "matched_groups": [
      { "matched_group_id": "diff_pair:pair:2", "device_groups": ["diffpair"], "placement_symmetric": null }
    ],
    "notes": []
  },
  "warnings": []
}
```

The only field with no `klt gen compose` analogue is `unmapped_netlist_nets`
— net names present in the ingested netlist digest that could not be
resolved to any `device_groups[]` port (e.g. a supply net the plan never
routes because it is left for a later, hand-authored `klt gen compose`
pass, or a genuinely floating net that is itself a netlist bug). Always
present, empty when every netlist net the plan's `device_groups[]` cover
was either routed or explicitly out of scope — the same "always report the
array, never silently drop" discipline `unrouted_nets[]` already applies
one contract layer down.

### Semantics and guarantees

- **A plan never invents device or composition semantics `klt gen`/`klt gen
  compose` do not already have.** `device_groups[].generator` must name an
  existing generator; `rows`/`abutment` compile to `klt gen compose` inputs
  (§3), not to a second placement/routing implementation.
- **Netlist-derived parameters take precedence; `params` overrides layer on
  top.** A plan author overriding a device's own netlist-stated size is
  legitimate (e.g. a layout-only fudge factor) but is a `warnings[]`-worthy
  divergence from the schematic, always reported, never silent — the same
  "advisory, not authoritative" posture `drc_hints` already has, applied to
  "does the layout match what the schematic said to build."
- **PDK and ingestion resolution go through the one resolver/one reader
  each already has.** No private PDK lookup, no private SPICE parse.
- **Additive envelope**, same rule as every other verb.

### Proposed exit codes

Extending `klt gen compose`'s own trichotomy-plus-partial-success
(`docs/cli/gen-compose.md`):

| Exit code | Meaning |
| --- | --- |
| `0` | Every device group generated, every row placed, every abutment satisfied, every resolvable net routed. |
| `1` | Application error — unresolvable PDK/netlist, a `device_groups[]` reference to a nonexistent netlist device, a `topology` a named generator does not support, an `abutment[]` pair whose declared edge geometry is inconsistent (e.g. mismatched heights with no aligning `align`). |
| `2` | Usage error — from argparse. |
| `3` | Partial success — every group placed, but `unrouted_nets[]` and/or `unmapped_netlist_nets[]` is non-empty. |

## 3. Execution: extend `gen_compose.compose()`, do not build a new engine

**Recommendation: extend `gen_compose.compose()` in place, behind a new,
separate plan-compiler module that calls it — no new placement/routing
engine.**

### What a plan executor actually has to do

Reading `gen_compose.py`'s current state (`compose()`, `gen_compose.py:1147`)
against §2's contract, plan execution decomposes into three mechanical
steps, none of which duplicates placement or routing math that does not
already exist:

1. **Per-group generation.** For each `device_groups[]` entry, resolve its
   parameters (netlist-derived sizing + `params` overrides, §2) and call
   `klt gen`'s existing `generate()` (`gen.py:947`) for the named generator
   — exactly the call a caller would make by hand today, just synthesized
   from the plan instead of typed out.
2. **Row-of-rows placement.** `gen_compose.py`'s current
   `SUPPORTED_PLACEMENT_STRATEGIES` (`gen_compose.py:93`) is `{"row",
   "explicit"}` — a single horizontal row, or caller-given absolute origins
   per block (`resolve_explicit_offsets`, #321). A plan's `rows[]` (plural,
   stacked vertically) is **not** a new placement primitive to add to
   `gen_compose` — it compiles directly onto the existing `"explicit"`
   strategy: compute each row's horizontal offsets independently (reusing
   `compute_row_offsets`, `gen_compose.py:178`, per row), then stack rows
   vertically by adding a per-row Y offset derived from the tallest group in
   each preceding row, and hand the whole thing to `compose()` as
   `placement.origins_um`. This is exactly the "grid" gap
   `gen-composition-spike.md` §5's open questions flagged as unresolved
   ("auto-derive a column count... or require an explicit `placement.cols`")
   — a plan compiler answering it at the plan layer, without `gen_compose`
   itself needing a new `"grid"` strategy, is a smaller, better-justified
   change than adding one to the shared module speculatively.
3. **Derived connectivity + abutment as a zero-gap placement constraint.**
   §2's netlist-derived connectivity compiles directly onto `gen_compose`'s
   existing `connectivity[]` (`_parse_connectivity`, `gen_compose.py:483`) —
   the plan compiler builds that array from the netlist digest instead of a
   human typing it. `abutment[]` (edge-to-edge, zero-gap placement) has no
   existing `gen_compose` analogue and compiles to a placement **constraint**
   during offset computation (constrain two groups' offsets so the declared
   edges coincide, before handing the result to `"explicit"` placement) —
   solved entirely in the plan compiler, before `compose()` ever runs, not a
   new `gen_compose` concept.

### Where a real gap exists, and where it does not

**Real, load-bearing gap: `gen_compose.compose()`'s routing is two-pin only**
(`gen_compose.py`'s own module docstring, "bundle (>2-pin) routing is out of
scope this phase" — the >2-pin check at `gen_compose.py`:1294, reporting an
`unrouted_net` rather than routing it). A netlist-derived connectivity set
will almost immediately produce >2-pin nets — `VDD`/`VSS`/bias nets touching
three or more device groups are the common case, not the exception, in any
real analog block (the sky130-bandgap-reference KB entry's own topology has
at least a shared bias/tail node across the mirror and diff pair). This is
the one place this spike's recommendation is **not** "the plan compiler
alone is enough" — bundle routing needs to land inside `gen_compose.py`
itself (per `gen-composition-spike.md` §5 item 2's own next-increment
framing, using the same `route_bundle`-style backbone-per-net-with-shared-
channel-allocation idea that spike already scoped and left for later),
because it is a routing-engine capability, not a plan-compilation one — a
plan compiler sitting above `gen_compose` cannot route a 3-pin net any
better than `gen_compose` can, since routing needs the same block
bbox/port/obstacle information `gen_compose` already holds internally
(`route_two_pin`, `gen_compose.py:925`) and a plan compiler has no
independent way to do that work without duplicating it.

**Not a gap: multi-row/grid placement and abutment (§3.2, §3.3) fully
compile onto capabilities `gen_compose` already has** (`"explicit"`
placement, #321) — no `gen_compose` change is required for those two pieces.

### Reasoned recommendation (not a new engine)

Scored against the same considerations `docs/ARCHITECTURE.md`'s rewrite
rule and both prior spikes' build/wrap sections apply, adapted to "extend
in place vs. build a new engine" rather than "adopt an external framework":

1. **A new engine would duplicate, not add, capability.** Every placement/
   routing primitive a plan needs (row offsets, bbox translation, Manhattan
   backbone routing, `drc_hints.matched_group_id` consumption) already
   exists in `gen_compose.py`, proven against a real worked case (`klt gen
   compose`'s own §4 resolution of #164's 5T OTA, `gen-composition-spike.md`
   §4). A parallel engine would re-implement all of it to add one new
   capability (bundle routing) that belongs inside the existing module
   anyway.
2. **The one real capability gap (bundle routing) is already-scoped,
   already-deferred work inside `gen_compose`, not evidence for a different
   engine.** `gen-composition-spike.md` itself named this as the natural
   next increment (§5 item 2) before this issue existed — #346 is the real
   friction-log evidence that increment needs to land, not a reason to
   abandon the module that already does two-pin routing correctly.
3. **A new, plan-compiler-only module (not a new engine) is still the right
   shape for the pieces that are genuinely new** (netlist ingestion
   adaptation, row-of-rows/abutment compilation, generator-parameter
   resolution from netlist sizing) — these are orchestration, sitting above
   both `klt gen` and `klt gen compose` and calling each as a library/CLI,
   exactly the "orchestration is ours to own, the engine underneath stays
   wrapped" split both prior spikes already reached for their own build/wrap
   calls (`layout-generator-spike.md` §3, `gen-composition-spike.md` §3).

## 4. Phase breakdown for a follow-up epic

Mirroring how #344 (Monte Carlo) was decomposed into #348 (sampling engine),
#349 (statistics report), and #350 (docs), each phase below is scoped to be
independently Builder-sized and independently mergeable, in dependency
order:

1. **Phase A — Netlist ingestion digest.** Extend `netlist_normalize.py`'s
   carried-parameter set to resistor/capacitor/bipolar devices (mirroring
   #341's device-class work on the write side, §1), and add a new, thin
   adapter (a new module, e.g. `netlist_digest.py`) that walks a parsed
   `kdb.Netlist` (via `lvs.py`'s existing `_read_reference_netlist`/
   `form=`/`deck=` path — reused, not reimplemented) into a plain,
   JSON-serializable device/net digest (§1's proposed shape). Independently
   testable and mergeable with no plan contract yet — it is useful on its
   own as a "netlist inspection" building block.
2. **Phase B — Plan JSON contract + validator.** Define
   `klt.layout_plan.request/1` (§2) and a pure validation pass (no
   generation): every `device_groups[]` reference resolves against Phase
   A's digest, declared `topology` values are ones the named generator
   documents support, `abutment[]` edges are geometrically consistent given
   each group's (not-yet-generated) expected shape class. Mirrors
   `gen-composition-spike.md`'s own contract-first sequencing before
   `klt gen compose`'s mechanism landed.
3. **Phase C — Plan compiler / execution.** The orchestration §3 describes:
   per-group `klt gen` calls with netlist-derived parameter resolution,
   row-of-rows/abutment compilation onto `gen_compose`'s existing
   `"explicit"` placement, and netlist-derived `connectivity[]` construction
   feeding `gen_compose.compose()`. Depends on Phases A and B.
4. **Phase D — Bundle (>2-pin) routing in `gen_compose`.** The one real
   engine-level gap §3 identifies. Independent of Phases A–C in
   implementation (it is a `gen_compose.py`-internal change,
   `gen-composition-spike.md` §5 item 2's own next increment) but load-
   bearing for Phase C to succeed on a real block without an artificially
   high `unrouted_nets[]`/exit-code-`3` rate — sequence it alongside or just
   before Phase C's own worked-example validation, not strictly after it.
5. **Phase E (worked-example canary, stretch) — a real multi-device-family
   block end-to-end.** Run Phases A–D against a real block with transistors,
   resistors, and bipolar devices together (the sky130-bandgap-reference KB
   entry's own topology, `kb/entries/sky130-bandgap-reference.json`, is the
   natural target — its `layout_idioms` array already names common-centroid
   bipolar placement, matched-orientation resistor strings with dummies, a
   guard ring, and a symmetric diff-pair placement, i.e. every plan concept
   §2 proposes, independently motivated before this spike existed), checked
   DRC/LVS-clean. This is the "friction log from real block designs"
   evidentiary bar `ROADMAP.md`'s "How progress is driven" section requires
   before scoping `"interdigitated"` topology support inside generators, or
   `abutment[].edge` geometric-consistency edge cases, any further — both
   are named in §2 as new generator-side/contract-side scope this spike
   does not fully specify, deliberately left for whoever executes Phase E
   to surface concretely.

A Curator decomposing this spike's findings into sub-issues should treat
Phases A/B as the first, lowest-risk pair to file (each independently
useful and testable with no dependency on the other's completion order
being strict — Phase B's schema can be drafted in parallel with Phase A's
implementation, since Phase B only needs to *agree on* the digest shape,
not consume a finished one), Phase C as the phase that actually closes
#346's stated gap, and Phase D/E as the two items most likely to reveal that
a phase boundary drawn here needs adjusting once real code exists —
consistent with how both prior spikes' own "Open questions for a follow-up
epic" sections were treated as living, not final.

## Out of scope for this spike

No dependency was added to `pyproject.toml`, no `klt` subcommand was added,
no plan-compiler or `gen_compose` code was written, and no MCP surface was
touched. `gdsfactory`/BAG3/laygo2/ALIGN/MAGICAL are not re-surveyed here —
`layout-generator-spike.md` §1 and `gen-composition-spike.md` §1 already
did that survey for single-cell generation and composition respectively,
and nothing in this spike's scope (netlist ingestion + a plan contract +
extending an existing composition engine) revisits either build/wrap
decision. gf180-bandgap's actual `plan.py` source was not read (external to
this checkout) — this spike's contract is derived independently, informed
only by the issue text's description of it, and should be treated as a
starting proposal, not a claim of parity with that file. No specific new
generator `topology` value (`"interdigitated"`) is implemented — §2 and §4
Phase E both flag it as new generator-side scope this spike names but does
not design in full.

## Open questions for a follow-up epic

- Whether `device_groups[].topology: "interdigitated"` should be a new
  value each relevant generator (`mos_array`, `res_array`, `bjt_array`)
  grows support for independently, or a plan-compiler-level post-process
  that reorders/re-labels an already-generated `"array"` topology's unit
  instances — the former keeps geometry generation inside the generator
  that already owns unit-device placement; the latter avoids touching
  three generators for one new value. Not resolved here.
- Whether `abutment[]`'s zero-gap constraint should be allowed to conflict
  with a `rows[].spacing_um` for the same pair of groups (i.e., can a group
  be abutted to one neighbor and row-spaced from another in the same row),
  and what the resulting error/precedence rule should be.
- Whether Phase A's netlist digest should be exposed as its own `klt`
  subcommand (e.g. a `klt netlist inspect`-shaped verb) independent of any
  plan, given it is useful for netlist inspection on its own — flagged here,
  not decided; Phase A's acceptance criteria (§4) do not require a CLI
  surface, only a library-level digest function.
- How a plan's `unmapped_netlist_nets[]` (§2) should interact with a
  supply-only net (`VDD`/`VSS`) that a caller deliberately leaves for a
  separate, hand-authored `klt gen compose` power-routing pass rather than
  expecting the plan to route it — whether the contract needs an explicit
  `netlist.ignore_nets[]`-style opt-out, or whether reporting it in
  `unmapped_netlist_nets[]` (and letting the caller judge it benign) is
  sufficient, is left for Phase B's validator design.
- Whether Phase D's bundle-routing implementation should also become
  available to a hand-authored (non-plan) `klt gen compose` request — it
  almost certainly should, since Phase C's plan compiler is only one caller
  of `gen_compose.compose()`, but this spike does not attempt to design
  the bundle-routing mechanism itself, only to place it in the phase
  sequence (§4 Phase D).
