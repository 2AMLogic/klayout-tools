# `klt lvs`

Compare a layout-derived netlist against a reference (schematic/golden) SPICE
netlist and report structured, categorised mismatches — the LVS half of
Epic #153, following the layout-vs-schematic pattern
[`klt extract`](extract.md) established for extraction.

```
klt lvs <request> [--format text|json]
```

This is phase 3 of Epic #153 (`klt lvs`/`klt extract`), the build carried by
the accepted spike,
[`docs/design/lvs-extraction-spike.md`](../design/lvs-extraction-spike.md)
(section 2b) — read it first for the engine survey and the reasoning behind
the contract shape below. This document is the shipped contract; where the
two disagree, this document (and the code) win.

Unlike `klt extract`/`klt drc`, `klt lvs` takes a **request document** (like
`klt sim`/`klt gen`), not positional netlist file args — it binds two
netlist inputs plus optional matching hints, richer than a flag line carries
cleanly.

- `<request>` — a request document (see "Request" below), in any of three
  forms, mirroring `klt gen --params`'s path-or-inline convention:
  - a **path** to a request JSON file, e.g. `klt lvs request.json`.
  - `-` to read the request JSON document from **stdin**, e.g.
    `cat request.json | klt lvs -`.
  - an **inline JSON object** string, e.g.
    `klt lvs '{"layout": {...}, "reference": {...}}'`. An existing file
    always wins first — a value that both names a readable file *and*
    happens to parse as JSON is read as that file, not decoded inline.
  - **Relative paths inside the request** (`layout.file`, `reference.netlist`,
    etc.) resolve against the **request file's own directory** for the path
    form, but against the **current working directory** for the stdin and
    inline-JSON forms — there is no request file to anchor them to in that
    case. Prefer absolute paths (or paths relative to your invocation's
    `cwd`) when using `-` or inline JSON.
- `--format` — `text` (default, a human-readable summary) or `json`.

## Engine

`klt lvs` runs fully headless via the pip `klayout` package's native
`klayout.db.NetlistComparer` (graph-isomorphism netlist comparison, net/
device/pin matching with hint support) and `klayout.db.NetlistSpiceReader`
(parsing the reference netlist) — the same wrapped dependency `klt extract`,
`klt drc`, and `klt render` already use. There is no dependency on the
standalone `klayout` application binary, netgen, or magic — only `pip install
klayout` (already this repo's sole runtime dependency).

Comparison is **in-process**, not a subprocess — unlike `klt sim`'s
per-corner `ngspice` fan-out, `NetlistComparer` is a deterministic graph
operation on in-memory data structures with no external process to time out
or kill (spike section 1, "Invocation strategy").

`request.engine` is a data field, not a code path — only `"klayout"` is
implemented in this version; an unsupported value is an application error
(exit 1).

## Scope: schematic-equivalent, topological compare only

Per the phase 1 spike's resolution (section 4, "Resolving the
`matched_group_id` open question"), `klt lvs` does **not** read
`matched_group_id` — a geometric-matching check deferred to a follow-up
epic — and does no layout-vs-layout geometric diffing. This command compares
device/net/pin topology only, exactly as `NetlistComparer` does natively.

## Netlist form: the schematic-equivalent, plain-element form

The reference (and any pre-extracted layout) netlist must use the same
**schematic-equivalent** device form `klt extract` writes: a plain element
line whose leading letter names the device class and whose parameters are
geometric literals —

```
M1 d g s b nfet L=0.15U W=0.65U
```

— **not** a SPICE simulation deck's subcircuit-call form for a PDK whose
models are subcircuits (`XM1 d g s b sky130_fd_pr__nfet_01v8 L=... W=...`).
Real open-PDK schematic flows (xschem/ngspice against sky130 or gf180mcu) emit
that simulation form, because both PDKs ship their primitive MOS device as a
`.subckt` rather than a built-in model.

### Detection (default): a specific error, not a silent cascade

Handing `NetlistSpiceReader` the subcircuit-call form directly would not
error — it reads the call as an instance of an undefined subcircuit, the
circuit collapses toward a single merged net, and the compare reports a
confusing `net.merged`/`topology` mismatch that reads like a layout bug but is
actually a netlist-form mismatch. `klt lvs` guards against this: when a
reference netlist (in the default `plain-element` form) instantiates a
**curated PDK device subcircuit** (e.g. `sky130_fd_pr__nfet_01v8`,
`nfet_03v3`) via an *undefined* `X` card, the run fails with a specific,
actionable error naming the form mismatch instead of producing the misleading
cascade.

### Conversion (opt-in): `reference.form = "subckt-call"`

To convert the simulation form automatically, set the reference's `form` to
`"subckt-call"`:

```json
{
  "layout":    { "file": "block.gds", "deck": "sky130" },
  "reference": { "netlist": "block.sim.spice", "form": "subckt-call" }
}
```

Device subcircuit names resolve through the same curated table `klt extract
--pdk` uses (`klayout_tools.pdk_models`), so the common case needs no map.
When a device name is not one of the curated devices, supply it explicitly:

- `reference.deck` — `"sky130"` / `"gf180mcu"`, selects that deck's device
  map (validates names against it).
- `reference.device_map` — an explicit `{ "<subckt-name>": "<nfet|pfet>" }`
  override, merged on top of the deck's map.

The conversion is deliberately narrow and loud (a wrong parameter/unit mapping
into a sign-off tool must never pass silently):

- Only `X` cards that carry an `l`/`w` parameter are treated as MOS devices;
  a genuine hierarchical subcircuit instance passes through untouched.
- `L`/`W` are carried and converted to explicit micrometre-suffixed literals
  (`0.5u` → `L=0.5U`; SI metres `1.5e-6` → `W=1.5U`). A `.option scale`
  bare-micrometre convention is **not** inferred — emit explicit unit
  suffixes (see "Unit suffixes matter" below).
- Every other parameter is dropped: the parasitic-only
  `ad`/`as`/`pd`/`ps`/`nrd`/`nrs`/`sa`/`sb`/`sd` (which `klt extract` does not
  carry either) and any other model parameter.
- `nf`/`m` > 1 (a multi-finger/multiplied device the curated plain-element
  form cannot represent) is **rejected** with a specific error naming the
  device — never silently dropped or misinterpreted. Flatten it (one device
  per drawn gate) in the schematic netlist first.
- A device-like `X` card whose subcircuit name is not in the resolved device
  map is a hard error, never a silent pass-through.

A reference netlist that *mixes* plain-element `M` cards and subckt-call `X`
device cards converts correctly under `form: "subckt-call"` — the `M` cards
pass through unchanged.

**Unit suffixes matter.** `NetlistSpiceReader` interprets a bare numeric
literal for a MOS `W`/`L` parameter as plain SI (metres) per the SPICE
standard, but an explicit `U` (or `UM`) suffix as micrometres — the
convention `klt extract`'s own writer always uses (`W=0.65U`). A reference
netlist authored without unit suffixes will parse with a `1e6`-scaled
device parameter that only ever mismatches or matches *relative to itself*
consistently, but produces a nonsensical absolute value in a
`device.property` mismatch's reported numbers. Always write reference
netlists with explicit unit suffixes on `W`/`L`.

## Negative controls: two independent corruptions

Per this issue's field notes, "LVS clean" alone is not evidence — a
mis-wired invocation that silently compares nothing also passes. A negative
control needs **two independent corruptions**, because they fail
independently:

- **topology** — short two nets that should be separate (`net.merged`), or
  split one net into two (`net.split`).
- **device parameters** — change one device's width without touching
  connectivity (`device.property`). This is the one that catches a compare
  that checks the connection graph and ignores parameters entirely.

`tests/test_lvs.py` exercises both independently, per this guidance.

### On a minimal cell, read the severities

`NetlistComparer` pairs devices from the *surrounding* net structure and only
then compares their parameters. On a cell small enough that the corrupted
device's own terminals are that structure — the canonical case being a
two-device inverter whose bulk terminals sit on their own substrate/well nets
rather than on the supplies — a parameter-only defect leaves it nothing to
anchor the pairing on. Its raw event stream degrades: an unmatched device on
each side, plus one unmatched net for every net only those two devices
touched, and no parameter event at all.

`klt lvs` recovers the intended signal from that stream (issue #282). When
exactly one device per side is unmatched, the two share a device class and
terminal set, every terminal lands on the same net on both sides, and a
parameter actually differs, the report gets the `device.property` entry the
negative control is looking for — with the `property: {name, layout,
reference}` naming the wrong parameter — and the collateral
`device.unmatched`/`net.unmatched` entries are downgraded to `severity:
"warning"`. So on a minimal cell:

- **filter on `severity: "error"`** and the width change is the only finding;
- `category_counts` still counts the collateral entries (they did happen, and
  `mismatches[]` never disagrees with the comparer's own log), so assert on
  `category_counts["device.property"]`, not on the exact `category_counts`
  dict.

The recovery is deliberately narrow, since a wrong claim would mask a real
connectivity defect: any additional unmatched device, a device-class
difference, or a terminal that lands on a *different* net on each side makes
it decline, and the degraded `device.unmatched` + `net.unmatched` cascade is
reported as-is, all at `severity: "error"`. Two corrupted devices in the same
minimal cell also decline — nothing in the event stream says which layout
device belongs to which reference device.

The verdict itself is unaffected either way: `status` and the exit code come
from `compare()`, which reports the mismatch in every one of these cases.

**Also worth knowing**: `klayout.db.NetlistComparer`'s default net matching
does not lock onto layout net *labels* to constrain the compare — a pure
pin/device-topology isomorphism is accepted even when net names differ
structurally (only a top circuit's own declared *pin order* is a hard
anchor). A clean LVS run therefore does not by itself establish that a
top-level pinout is correct; something else (e.g. comparing `pins[]` names
directly) has to check pin order.

## Request

Accepted as a file path, `-` (stdin), or an inline JSON object string on the
command line — see the `<request>` bullet above for the three forms and how
each resolves relative paths inside the document.

```json
{
  "schema": "klt.lvs.request/1",
  "engine": "klayout",
  "layout": { "file": "design.gds", "deck": "sky130", "top": "ota_5t" },
  "reference": { "netlist": "ota_5t.schematic.spice", "top": "ota_5t" },
  "hints": {
    "same_nets": [["vsubs", "GND"]],
    "equivalent_pins": { "ota_5t": [["inp", "inn"]] }
  },
  "options": { "keep_extracted": true, "combine_devices": false }
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema` | string | Request contract identifier (not required — `load_request` does not validate it, matching `klt sim`'s convention for user-authored input). |
| `engine` | string | Engine selector. Only `"klayout"` is supported; omit to use the default. |
| `layout` | object | The layout side — see "`layout` shapes" below. Exactly one of `file`/`netlist` is required. |
| `reference.netlist` | string, required | Path to the reference (schematic/golden) SPICE netlist, parsed via `NetlistSpiceReader`. Relative paths resolve against the request file's directory (or the current working directory for the `-`/inline-JSON request forms — see the `<request>` bullet above). |
| `reference.top` | string | The subcircuit in the reference netlist to compare. Omit when the reference file has exactly one top-level circuit (auto-selected, same convention as `layout.top`/`klt extract`'s `--top`). |
| `reference.form` | string | `"plain-element"` (default) or `"subckt-call"`. `"plain-element"` reads the reference as the schematic-equivalent form `klt lvs` requires, and detects/errors on a misfiled simulation-form netlist. `"subckt-call"` converts a PDK schematic flow's simulation-form netlist to the plain-element form first — see "Netlist form" above. |
| `reference.deck` | string | Only used with `form: "subckt-call"`. `"sky130"`/`"gf180mcu"` — selects that deck's curated device-name map for the conversion (and validates device names against it). Omit to auto-resolve each device subcircuit name against the whole curated table. |
| `reference.device_map` | object\<string, string\> | Only used with `form: "subckt-call"`. Explicit `{ "<subckt-name>": "<nfet\|pfet>" }` overrides, merged on top of `reference.deck`'s map — for a device subcircuit name the curated table does not cover. |
| `hints.same_nets` | array\<[string, string]\> | Optional `[layout_net_name, reference_net_name]` pairs — ties a named net in the layout's top circuit to a named net in the reference's top circuit. A name that does not resolve on the stated side is an application error (exit 1), not a silent no-op. |
| `hints.equivalent_pins` | object\<string, array\<[string, string]\>\> | Optional per-subcircuit swappable-pin groups, keyed by **reference**-side subcircuit name (`NetlistComparer.equivalent_pins` only accepts circuits from the netlist passed as `compare()`'s second argument, which is always the reference netlist in this command's `compare(layout, reference)` call order). |
| `options.keep_extracted` | boolean | When `layout.file` is given (inline extraction), retain the intermediate extracted netlist on disk at `<request-dir>/.klt/lvs/<top>.spice` and echo its path in `environment.extracted_netlist`, where `<request-dir>` is the request file's directory (or the current working directory for the `-`/inline-JSON forms). Default `false` (nothing is written to disk). |
| `options.combine_devices` | boolean | When `true`, calls `klayout.db.Netlist.combine_devices()` on **both** the layout and reference netlists before comparing — merging devices a device class recognises as combinable (e.g. parallel/series MOSFETs sharing gate/source/drain/body connectivity). This is what makes folded/multi-finger devices (a wide transistor drawn as N parallel fingers of width `W/N`) and split/interleaved matched-pair segments (common-centroid, interdigitated layout) comparable against a single lumped schematic device — without it, each finger/segment reports as its own unmatched device. Default `false` (today's per-drawn-device matching, unchanged) because unconditional merging would also collapse genuinely-distinct parallel devices (e.g. a DAC array's intentionally-separate legs) that some callers want reported individually — opt in only when the layout actually uses folded/split constructions. |

### `layout` shapes

Extraction and compare are separable steps that also compose in one call:

- **Inline extraction** — `{"file": "design.gds", "deck": "sky130", "top": "ota_5t"}`. Runs `klt extract`'s core extraction (the same `extract_netlist_from_layout` function `klt extract` itself calls) against the named curated deck (`sky130`/`gf180mcu`), then compares the resulting in-memory netlist directly — no SPICE round-trip is required unless `options.keep_extracted` is set. `top` is optional (defaults to the layout's sole top cell, same as `klt extract --top`); `deck` is required in this shape. An optional boolean `top_cell_pins` (default `false`) mirrors `klt extract --top-cell-pins`: when `true`, only labels drawn directly in the top cell are promoted to top-level pins, so a net named only by a label inside an instanced sub-cell stays internal instead of demanding a matching port in `reference.netlist` (issue #291). Only meaningful in this inline-extraction shape — it has no effect on a pre-extracted `layout.netlist`, whose pins are already fixed.
- **Pre-extracted netlist** — `{"netlist": "design.spice", "top": "ota_5t"}`. Reads an existing extracted (or hand-written) SPICE netlist directly via `NetlistSpiceReader`, skipping extraction entirely. `top` is optional (defaults to the sole top circuit).

`layout.top`/`reference.top` are always compared as a declared pair, even
when their names differ — the two selected circuits are pinned together
(`NetlistComparer.same_circuits`) rather than left to the comparer's default
by-name matching. Without this, a layout top named differently from the
reference's `.SUBCKT` would collapse every finding to a generic `topology`
"could not be matched to a counterpart" entry instead of the specific
`net`/`device` mismatches underneath.

## Response

```json
{
  "schema_version": 1,
  "engine": "klayout",
  "layout": "design.gds",
  "reference": "ota_5t.schematic.spice",
  "top": "ota_5t",
  "status": "match",
  "mismatch_count": 0,
  "category_counts": {},
  "counts": {
    "nets": { "layout": 7, "reference": 7, "matched": 7 },
    "devices": { "layout": 5, "reference": 5, "matched": 5 },
    "pins": { "layout": 4, "reference": 4, "matched": 4 }
  },
  "device_classes": ["nfet", "pfet", "resistor"],
  "environment": {
    "engine": "klayout",
    "engine_version": "0.30.10",
    "layout_sha256": "1ab7...",
    "reference_sha256": "c93e...",
    "extracted_netlist": null
  },
  "provenance": {
    "klt_version": "0.4.2",
    "klayout_version": "0.30.10",
    "pdk": null,
    "deck": { "name": "sky130", "content_hash": "sha256:<hex>" }
  },
  "mismatches": []
}
```

### Top-level fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema_version` | integer | Version of this command's JSON shape (starts at `1`; per-command, per [`docs/json-contract.md`](../json-contract.md)). |
| `engine` | string | Echo of the request's `engine` (or the default, `"klayout"`). |
| `layout` | string | Echo of `layout.file` or `layout.netlist`, exactly as provided. |
| `reference` | string | Echo of `reference.netlist`, exactly as provided. |
| `top` | string | The compared top circuit's name (the layout side's resolved top cell/circuit name). |
| `status` | `"match"` \| `"mismatch"` | `"match"` when `NetlistComparer.compare()` reports the netlists equivalent; `"mismatch"` otherwise. Never `"error"` in-band — a failed run does not emit this envelope at all (see "Exit codes"). |
| `mismatch_count` | integer | `len(mismatches)`. Can be nonzero even when `status` is `"match"` — a `severity: "warning"` entry (e.g. an ambiguity the comparer resolved on its own) does not change the verdict. |
| `category_counts` | object\<string, int\> | Per-category mismatch counts, keys sorted for determinism — the LVS analogue of `klt drc`'s `rule_counts`. |
| `counts` | object | Side-by-side `layout`/`reference`/`matched` tallies for `nets`, `devices`, `pins`. `matched` counts only a **strictly successful** pairing (e.g. a device paired with identical parameters and class) — a device paired despite a `device.property`/`device.class` mismatch is *not* counted as matched. |
| `device_classes` | array\<string\> \| `null` | The layout-side deck's `ExtractionDeck.device_classes` (see `klt extract`'s own field of the same name) — what that deck is structurally capable of recognising, not what this compare found. Present (currently `["nfet", "pfet", "resistor"]` for both registered decks — MOS plus one drawn precision resistor, see `klt extract`'s "Drawn resistors") when `layout.file` + `layout.deck` (inline extraction) was given; `null` when `layout.netlist` (pre-extracted, no deck involved) was given instead. |
| `environment` | object | Reproducibility block: `engine`, `engine_version` (the installed `klayout` package version), `layout_sha256` (of `layout.file`, or of `layout.netlist` when no extraction ran), `reference_sha256` (of `reference.netlist`), `extracted_netlist` (path to the retained intermediate netlist when `options.keep_extracted` is set and `layout.file` was given; `null` otherwise). |
| `provenance` | object | Shared reproducibility block (`klt_version`, `klayout_version`, `pdk`, `deck`) defined once in [`docs/json-contract.md`](../json-contract.md). `pdk` is always `null` (LVS is topological and resolves no PDK); `deck` pins the layout-side extraction deck by name and `sha256:` content hash, and is `null` for the pre-extracted `layout.netlist` form (matching `device_classes`). |
| `mismatches` | array\<object\> | One entry per structured mismatch — see below. Empty on a clean match; always present. |

### `mismatches[]` entries

Field-for-field the LVS counterpart of `klt drc`'s `violations[]`: a stable
`category` id (never renumbered/repurposed once shipped, exactly like a DRC
rule id), a curated human `description` (not raw engine log text), and the
objects involved.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `category` | string | One of `net.unmatched`, `net.merged`, `net.split`, `device.unmatched`, `device.class`, `device.property`, `device.body_unverified`, `pin.unmatched`, `topology`. |
| `severity` | `"error"` \| `"warning"` | `"error"` breaks equivalence; `"warning"` is informational and never changes `status`. Informational cases include an ambiguous net pairing the comparer resolved on its own (see `hints.same_nets` above), a `topology` device-class-mismatch entry for a device class with zero actual instances on the side that registered it (e.g. an all-`nfet` layout compared against an all-`nfet` reference netlist that never mentions `pfet` — `klt extract` always registers both polarities' device classes even when only one is instantiated), every `device.body_unverified` entry (see below), and the collateral `device.unmatched`/`net.unmatched` entries left over when a minimal cell's parameter defect is recovered into a `device.property` entry (see "Negative controls" above). A device-class mismatch where the class has one or more real instances still reports `"error"`. |
| `description` | string | Curated, human-readable explanation of this mismatch — never raw `NetlistComparer` log text (which is version-dependent and, per this repo's own testing, sometimes empty). |
| `side` | `"layout"` \| `"reference"` \| `"both"` | Which netlist the offending object(s) live on. |
| `net` | object \| `null` | `{"layout": <name\|null>, "reference": <name\|null>}` when a net is involved. |
| `device` | object \| `null` | `{"layout": <name\|null>, "reference": <name\|null>, "class": <string\|null>}` when a device is involved. |
| `property` | object \| `null` | `{"name": <string>, "layout": <value>, "reference": <value>}` for a `device.property` mismatch. `name` is `w_um`/`l_um` for the width/length parameters (matching `klt extract`'s own convention); every other declared device-class parameter is reported under its own lower-cased name. |

`mismatches` is sorted by `(category, side, device.layout, device.reference,
net.layout, net.reference)` (missing fields sort first) so repeated runs
against the same inputs produce identical, diff-clean output — the same
canonical-ordering guarantee `klt drc` makes about `violations`.

#### `device.body_unverified`: MOS body terminals compared against a deck-synthesized net

The curated extraction decks give some MOS body terminals a net that does not
come from any drawn tap/well-label geometry (`docs/cli/extract.md` →
"Coverage"): every NMOS body is tied to the deck's global substrate net
(`connect_global`, e.g. `vsubs`), since no curated deck draws a distinct NMOS
substrate/tap layer today; gf180mcu additionally has no distinct PMOS
well-tap layer (`Comp` is shared with ordinary transistor active), so its
PMOS bodies land on an anonymous, deck-synthesized well net instead of a real
one. Comparing those against a schematic reference's real ground/rail net
still produces a genuine `NetlistComparer` finding if they disagree, but a
*clean* compare on that dimension does not mean the well/substrate tie was
actually verified against the schematic — it means both sides were forced
onto the same synthetic net.

`klt lvs` surfaces this as one or two `severity: "warning"` entries
(`category: "device.body_unverified"`, `side: "layout"`) whenever
`layout.file` + `layout.deck` (inline extraction) is used — never for the
pre-extracted `layout.netlist` form, since no deck (and therefore no known
synthetic-net behaviour) is involved there:

- An NMOS entry fires whenever the layout has one or more NMOS devices
  (`device.class` is the deck's `nfet_class`, e.g. `"nfet"`).
- A PMOS entry additionally fires when the layout-side deck has no distinct
  well-tap layer (`ExtractionDeck.tap is None`, gf180mcu today) **and** the
  layout has one or more PMOS devices (`device.class` is the deck's
  `pfet_class`, e.g. `"pfet"`). sky130's `tap` layer gives PMOS bodies a real,
  named net, so sky130 never emits this entry.

Both entries are deck-structural (a property of which deck ran extraction,
not of any individual device pairing or `hints`), always `severity:
"warning"`, and never change `status` or break `mismatch_count`'s error
semantics — they only make it visible, in-band, that this dimension of the
compare was not fully verified against the schematic.

#### `topology`: catch-all for circuit-, device-class-, and net-identity-level mismatches

`topology` is the category for structural findings that don't fit any of the
narrower `net.*`/`device.*`/`pin.unmatched` categories: a whole circuit or
subcircuit instance with no counterpart, a device class with no counterpart,
an ambiguous net pairing the comparer resolved on its own, or two nets paired
despite a name/identity conflict. Six classification sites inside
`_build_mismatches`/`_classify_net_mismatches`
(`src/klayout_tools/lvs.py`) each map one `NetlistComparer` event kind to a
`topology` entry:

- **Circuit mismatch** (`lvs.py:1132-1141`, from `logger.circuit_mismatches`)
  — a circuit (module) on one side has no counterpart on the other. Always
  `severity: "error"`.
- **Subcircuit mismatch** (`lvs.py:1143-1152`, from
  `logger.subcircuit_mismatches`) — a subcircuit instance has no
  counterpart. Always `"error"`.
- **Device-class mismatch** (`lvs.py:1154-1192`, from
  `logger.device_class_mismatches`) — a device class (e.g. `nfet`)
  registered on one side has no counterpart class on the other side.
  Downgraded to `"warning"` when that side's netlist has zero actual
  instances of the class (`klt extract` always registers both
  `nfet`/`pfet` device classes even when a layout only instantiates one
  polarity, so an all-`nfet` layout compared against an all-`nfet`
  reference is not a real defect); `"error"` when the class has one or
  more real instances.
- **Ambiguous net pairing** (`lvs.py:1194-1207`, from
  `logger.ambiguous_net_matches`) — nets were paired ambiguously and the
  comparer resolved it structurally on its own (consider adding a
  `hints.same_nets` entry to pin the pairing down explicitly). Always
  `"warning"`; never changes `status`.
- **Net identity conflict with no leftover** (`lvs.py:1539-1549`, inside
  `_classify_net_mismatches`) — two nets were paired despite a name/identity
  conflict, and neither side has an accompanying one-sided leftover net
  (the merge/split case documented below, which absorbs the same
  underlying event when a leftover is present). Always `"error"`.

A seventh entry (`lvs.py:377`) is a safety net, not a classification site: if
`NetlistComparer.compare()` reports a mismatch but none of the sources above
produced any structured entry (a gap in this module's own event coverage,
not a clean run), `klt lvs` reports one generic `severity: "error"`,
`side: "both"` `topology` entry rather than silently reporting
`status: "match"` — see this module's docstring on the "`compare()` is
always authoritative" invariant.

#### Net-merge/net-split classification (a documented simplification)

`NetlistComparer`'s own event stream does not label a net mismatch as
"merged" or "split" — it only reports individual net-pairing events. This
command distinguishes the three net categories from the *pattern* of events
in one compare run: an isolated, one-sided unmatched net (no counterpart on
the other side, and nothing else nearby) is `net.unmatched`. When a
one-sided leftover net on the **layout** side co-occurs with a differently-
named net pairing elsewhere in the same circuit, it is classified
`net.split` (a reference net's role divided across more layout nets than
expected); the mirror case (a leftover on the **reference** side) is
`net.merged`. This heuristic is verified against synthetic single-defect
merge/split fixtures in `tests/test_lvs.py`, but — like `klt extract`'s
documented curated-deck connectivity limits — is not a formal proof for
every possible multi-defect input; a compare run with several independent
net defects at once may classify some of them generically (`topology`)
rather than precisely.

## Exit codes

| Code | Meaning |
| ---- | ------- |
| `0` | LVS clean — layout matches reference (`status: "match"`). |
| `1` | Failed to run — bad request, unresolvable layout/reference input, unknown `--deck`, unparseable reference netlist, unsupported engine, or an engine error. |
| `2` | Usage error (missing argument, bad `--format` value) — from argparse. |
| `3` | Ran successfully; mismatches found (`status: "mismatch"`); the documented payload is on stdout. |

This is `klt lvs`'s direct analogue of `klt drc`'s `3` ("ran clean but found
findings") — LVS is a clean/dirty verdict like DRC, so it uses `drc`'s
two-outcome success split, **not** `klt sim`'s three-outcome `3`/`4` split
(which exists only because a corner sweep has a distinct "broken run"
outcome — a netlist comparer has no such third state).

On error (exit `1`), a concise message is written to **stderr** and nothing
is written to stdout. No Python traceback is printed.

- `--format text` (default): a plain-text line prefixed `klt lvs:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "lvs", "message": "reference netlist not found: missing.spice" } }
  ```

## Out of scope

`matched_group_id` (a geometric-matching check, deferred to a follow-up
epic per the phase 1 spike's section 4), any layout-vs-layout geometric
diffing, and loop closure through `klt sim` (Epic #153 phase 4) are all
explicitly out of scope for this command.
