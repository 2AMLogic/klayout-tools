# `klt lvs`

Compare a layout-derived netlist against a reference (schematic/golden) SPICE
netlist and report structured, categorised mismatches — the LVS half of
Epic #153, following the layout-vs-schematic pattern
[`klt extract`](extract.md) established for extraction.

```
klt lvs <request> [--format text|json]
klt lvs --check <report.json> [--rerun] [--format text|json]
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
- `--check` — verify a previously committed `--format json` report instead
  of running a fresh compare (issue #1106). Mutually exclusive with
  `<request>` (inputs are read from the report itself) — see "`--check` /
  `--rerun`" below.
- `--rerun` — full mode for `--check`: best-effort re-run of the compare the
  report describes, instead of only re-hashing its inputs. Requires
  `--check`.
- `--format` — `text` (default, a human-readable summary) or `json`.

## Engine

`request.engine` is a data field, not a code path (spike section 2b) — it
selects one of two independent comparator implementations behind the same
request/response contract. An unsupported value is an application error
(exit 1).

### `"klayout"` (default)

Runs fully headless via the pip `klayout` package's native
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

### `"netgen"` (issue #343 — independent open-flow cross-check)

Wraps the ecosystem-standard open-flow LVS comparator,
[`RTimothyEdwards/netgen`](https://github.com/RTimothyEdwards/netgen), as a
subprocess (`netgen -batch lvs`), the same "wrap a proven engine" pattern
`klt sim` uses for `ngspice`. Requires a `netgen` binary on `$PATH` — not
bundled or pip-installable; a missing binary is a clear, actionable error
(exit 1), never a traceback.

**Netlist-vs-netlist mode only — no `magic` dependency.** Per the accepted
spike (`docs/design/lvs-extraction-spike.md` section 1, "netgen (contrast
candidate)"), netgen has no layout front-end of its own; the open flow pairs
it with `magic` for extraction. Wiring up `magic` as a second extraction
backend was explicitly ruled out (the "two-backends sprawl" the spike's
`klt sim`-precedent "wrap the proven engine" pattern exists to prevent) — so
this engine feeds netgen the *same* layout/reference SPICE netlists the
`"klayout"` engine path already resolves (from `layout.netlist`, or from
inline extraction via `layout.file` + `layout.deck`), written to temporary
files for the subprocess. This validates **comparator/contract
independence** — does the JSON contract generalise to a second, independent
graph-matching implementation? — not **extraction independence**: a
connectivity bug in `klt extract` itself would still be invisible to this
engine, since both "engines" compare the same layout-side netlist. A true
independent oracle would need `magic`'s own extraction, which is out of
scope here.

`hints` (`same_nets`/`equivalent_pins`) has no netgen equivalent in this
scope and is an application error (exit 1) when given alongside `"engine":
"netgen"` — silently ignoring a caller's stated hint would mask an
expected-different-result assumption instead of surfacing it. The same applies
to `reference.device_bulk` (issue #506): it is a `klayout.db`-side device-class
normalisation applied to the in-memory reference netlist before
`NetlistComparer` runs, and netgen reads its own SPICE files through its own
device model.

`options.parameter_tolerance` (issue #589) draws the same boundary, for a
concrete reason rather than an arbitrary one: netgen's own per-property
tolerances are declared in its setup file as **absolute, per-device-class**
values (`property {-circuit1 <class>} tolerance <name> <value>`), so a single
engine-neutral *relative* tolerance has no faithful translation into one —
deriving per-class absolute values would mean this command inventing a
device-by-device conversion the caller never asked for. Callers on this engine
express a tolerance in netgen's own vocabulary through `options.netgen_setup`,
which is strictly more expressive here. Passing `options.parameter_tolerance`
with `"engine": "netgen"` is an application error (exit 1) rather than a
silent no-op — an opted-in tolerance the caller believes is in force but is
not would be worse than not supporting it at all.

Two additional `options` apply only to this engine:

- `options.netgen_setup` — an explicit path to a netgen LVS setup `.tcl`
  file (e.g. the PDK's own, resolvable via `klayout_tools.pdk.netgen_setup_file`
  — see `docs/cli/pdk.md`). `klt lvs` resolves no PDK on its own
  (`provenance.pdk` is always `null`, matching the `"klayout"` engine), so
  this is not looked up automatically — pass it explicitly when a PDK-native
  setup (device-class merging, per-parameter tolerances) matters. Omitted →
  netgen's own documented "trivial default setup" (still compares device/net
  topology correctly for the built-in MOSFET/resistor/etc. device types; no
  PDK-specific tolerances apply). A path that does not exist is an
  application error (exit 1), not a silent fallback.
- `options.netgen_timeout_s` — wall-clock budget for the `netgen` subprocess
  (default `300`). A timeout is an application error (exit 1) — mirrors
  `klt sim`'s `options.timeout_s`.

`environment.engine_version` for this engine is netgen's own reported
version (parsed from its startup banner, `"Netgen <version> compiled on
..."`, verified against a from-source build for this issue — see the dated
addendum in `docs/design/lvs-extraction-spike.md`), never a hardcoded
string — the same convention `klt sim`'s `_ENGINE_VERSION_RE` uses for
`ngspice`.

**Known limitation — `counts.*.matched`/`net_correspondence` are not
reconstructed for this engine.** netgen's own text report does not expose a
stable, structured per-net/per-device correspondence the way the `"klayout"`
engine's `NetlistComparer` callbacks do, and reconstructing one by parsing
netgen's fixed-width side-by-side tables would require trusting column
alignment that is not a documented, versioned part of netgen's report
format. So for `"engine": "netgen"`: `counts.nets/devices/pins.matched`
equals the (real, always-accurate) `layout`/`reference` counts on a
`"match"` verdict (exact by construction — a unique match requires equal
cardinality on both sides) and is `0` on a `"mismatch"` verdict (the
conservative floor, never a fabricated estimate); `net_correspondence` is
always `[]` for this engine, which keeps the documented
`len(net_correspondence) == counts.nets.matched` invariant intact (both
sides of that equation are `0` together on a mismatch). Consult `status`,
`mismatches[]`, and `category_counts` for the actual defect detail on a
netgen-engine mismatch, not `counts`/`net_correspondence`.

**netgen report parsing never silently defaults to a match.** If netgen's
log has no recognisable `"Final result:"` verdict text at all — a changed
report format, a crash before completing the compare — `klt lvs` raises an
application error (exit 1) rather than guessing; this is the exact failure
mode ("a bad report parse could silently produce a false match") this
engine exists to catch, so it is designed to fail loud instead.

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

**gf180mcu MOS flavour subcircuits** (issue #1111): the curated table also
recognises `nfet_06v0`/`pfet_06v0` (gf180mcu's `Dualgate`-scoped 5V/6V MOS
flavour `klt extract --pdk` binds a transistor drawn inside `Dualgate` to —
see [`klt extract`'s "Voltage-domain markers and per-flavour MOS binding"
section](extract.md#voltage-domain-markers-issue-552-and-per-flavour-mos-binding-issue-1111))
alongside the default `nfet_03v3`/`pfet_03v3`. Both flavours convert to the
*same* plain-element class, `nfet`/`pfet` — not a separate `nfet_06v0`
class — matching the layout side: `klt extract`'s own structural
`devices[].class` never varies by MOS flavour either (only the *bound SPICE
model name* does, under `--pdk`), so a reference netlist naming either
flavour's subcircuit compares correctly against the extracted layout's
`nfet`/`pfet` devices.

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

## Machine-generated macro-scale fixture (issue #389)

`docs/cli/drc.md`'s own "Macro-scale, machine-generated (standard-cell)
layout" section documents running `klt drc`/`klt precheck`/`klt
layout-metrics` against a real `sky130_fd_sc_hd` GCD macro produced end to
end by `klt synthesize` + `klt place-and-route` (real Yosys + real OpenROAD,
see `tests/corpus/place_and_route/README.md`'s provenance note) — thousands
of instances, one level of real hierarchy, `met1`-`met5` routing. Issue #389
closed the one verb that macro-scale check left uncovered: `klt lvs`.

Applying "Negative controls" above to that same committed fixture
(`tests/corpus/place_and_route/gcd.gds.gz`, top cell `gcd`), via the
corpus round-trip / self-consistency methodology `#456` established for a
real OpenROAD-produced layout:

- **Clean self-compare.** Extracting the macro's own netlist (**4355
  devices**, 2961 nets, 1571 pins across the flattened layout) and comparing
  the layout against it reports
  `status: "match"`, with every device accounted for on both sides
  (`counts.devices` layout = reference = matched = 4355 — no silently-
  dropped devices at this scale). The only mismatches are the same deck-
  structural **warnings** the hand-drawn corpus round-trip already carries:
  one `device.body_unverified` (the synthetic-substrate net every NMOS body
  lands on, since sky130 draws no distinct NMOS tap layer — see
  [`docs/cli/extract.md`](extract.md), "Coverage") plus ambiguous-net and
  unused-device-class `topology` warnings. None are `error` severity.
- **Deliberately-broken variant.** Corrupting exactly one standard-cell-
  region transistor's drawn width in the reference netlist (`W=0.42U` →
  `W=1.0U` on a single device line, a pure `device.property` change leaving
  all connectivity intact) is caught: `status: "mismatch"`, exit 3, a single
  `device.property` entry (`severity: "error"`, `class: "nfet"`,
  `description`: "matched device parameter 'w_um' differs") naming that exact
  instance — correctly attributed to the one changed device, not smeared
  across the ~4000 unchanged standard-cell devices.

This is a **self-consistency** check (the layout's own extracted netlist as
its own reference), not an independent-reference LVS: a true golden-reference
check (translating `klt synthesize`'s gate-level Verilog netlist to SPICE)
is out of scope — no Verilog→SPICE bridge exists in this repo today. What it
establishes is that `klt lvs`'s extract-and-compare path scales to macro-
scale, thousands-of-device input without dropping devices, and that a single-
instance parameter defect in the standard-cell fabric is caught and correctly
attributed. See `tests/test_lvs.py`'s "Corpus round-trip: machine-generated
macro-scale fixture" tier for the regression pin.

## Mixed sky130_fd_sc_hd + analog-macro netlist (Epic #393 Phase 3, #456)

Applying "Negative controls" above to a genuinely **mixed** design: issue
#456 extracted a real mixed layout (a `klt gen diff_pair` analog macro
placed by `klt place-and-route`'s `request.macros` alongside ~15 real
`sky130_fd_sc_hd` standard cells — real Yosys + real OpenROAD, see
[`docs/cli/drc.md`](drc.md)'s own "Mixed sky130_fd_sc_hd + analog-macro
layout" section for the same artifact) and used its own extracted netlist
(256 devices: 129 nfet + 127 pfet) as the reference, per the two-independent-
corruptions negative-control methodology above — but with each corruption
placed in a **different domain** rather than both in the same cell:

- A `device.property` corruption on a standard-cell-region transistor (one
  of the ~254 devices contributed by the digital fabric — identifiable by
  device class/parameter combination unique to the standard-cell library)
  was caught: `status: "mismatch"`, a `device.property` entry naming that
  exact instance.
- A `device.property` corruption on one of the analog macro's own two
  transistors (identifiable by their unique `w_um`/`l_um` — the only nfet
  pair in the merged extraction matching the standalone macro's own
  reported device parameters) was caught the same way, naming that
  instance.
- The unmodified reference against the unmodified layout reports `status:
  "match"` (`counts`: 173/173/173 nets, 256/256/256 devices, 71/71/71 pins),
  modulo the two documented `severity: "warning"` entries above
  (`device.body_unverified`, the class-with-no-instances `topology` note).

This confirms `klt lvs` does not have an implicit single-domain assumption
that silently drops connectivity from either the analog macro or the
digital standard-cell fabric on a layout produced via `klt
place-and-route`'s `request.macros` field — a defect in either region's
devices is caught and correctly attributed regardless of which domain it
came from. See #456 for the full transcript (extracted reference, both
corrupted variants, and all three `klt lvs` runs).

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
| `engine` | string | Engine selector: `"klayout"` (default) or `"netgen"` — see "Engine" above. |
| `layout` | object | The layout side — see "`layout` shapes" below. Exactly one of `file`/`netlist` is required. |
| `layout.deck_options` | object\<string, string\> | Optional `{"<key>": "<value>"}` pairs, the JSON-request-document counterpart of `klt extract --deck-option <key>=<value>` (issue #600/#595) — selects a caller-visible sheet-rho flavour of a shared-geometry resistor family (e.g. gf180mcu's `poly_res`, one of `1k`/`2k`/`3k`). Honored for both `layout` shapes wherever `layout.deck` is given — inline extraction (`layout.file`) and the pre-extracted `layout.netlist` shape (issue #585). Omitting it (or giving `{}`) resolves every deck exactly as before this field existed. An unrecognised key or value is an application error (exit 1), not a silent no-op or a silently-kept default; giving it without `layout.deck` is likewise an application error (there is no deck to apply it to). The resolved mapping is echoed under `provenance.deck.options` when non-empty — see "`layout` shapes" below and the `provenance` row further down. |
| `reference.netlist` | string, required | Path to the reference (schematic/golden) SPICE netlist, parsed via `NetlistSpiceReader`. Relative paths resolve against the request file's directory (or the current working directory for the `-`/inline-JSON request forms — see the `<request>` bullet above). |
| `reference.top` | string | The subcircuit in the reference netlist to compare. Omit when the reference file has exactly one top-level circuit (auto-selected, same convention as `layout.top`/`klt extract`'s `--top`). |
| `reference.form` | string | `"plain-element"` (default) or `"subckt-call"`. `"plain-element"` reads the reference as the schematic-equivalent form `klt lvs` requires, and detects/errors on a misfiled simulation-form netlist. `"subckt-call"` converts a PDK schematic flow's simulation-form netlist to the plain-element form first — see "Netlist form" above. |
| `reference.deck` | string | Only used with `form: "subckt-call"`. `"sky130"`/`"gf180mcu"` — selects that deck's curated device-name map for the conversion (and validates device names against it). Omit to auto-resolve each device subcircuit name against the whole curated table. |
| `reference.device_map` | object\<string, string\> | Only used with `form: "subckt-call"`. Explicit `{ "<subckt-name>": "<nfet\|pfet>" }` overrides, merged on top of `reference.deck`'s map — for a device subcircuit name the curated table does not cover. |
| `reference.device_bulk` | object\<string, string\> | Optional `{ "<device-class / model name>": "<reference net name>" }` — declares that the reference netlist's device class of that name carries an *implicit* bulk/well/collector terminal on the named net, which the layout side's same-named class declares explicitly (issue #506). `klt lvs` adds that one terminal to the reference class and ties it to the named net on every reference-side instance before `NetlistComparer.compare()` runs, so a deck's bulk-terminal device flavour can match a schematic reference that does not model the terminal at all — the reconciliation `device.class_arity` only diagnoses. The net is looked up on each circuit that instantiates the class (matched exactly, then case-insensitively) and **created** there when the reference does not model that node; to bind the added terminal to a layout-side net of a different name, compose with a `hints.same_nets` pair. Every reconciled class emits a `severity: "warning"`, `category: "device.bulk_reconciled"` disclosure entry — see "`device.bulk_reconciled`" below. Model names are matched exactly first and then case-insensitively (`NetlistSpiceReader` upper-cases `res_x` to `RES_X`). A name that resolves on neither side, a class the reference is *not* actually missing a terminal from, and a class two or more terminals apart (this hook reconciles exactly one extra terminal per class, since the entry names exactly one net) are each an application error (exit 1), not a silent no-op. `"engine": "klayout"` only. |
| `hints.same_nets` | array\<[string, string]\> | Optional `[layout_net_name, reference_net_name]` pairs — ties a named net in the layout's top circuit to a named net in the reference's top circuit. A name that does not resolve on the stated side is an application error (exit 1), not a silent no-op. |
| `hints.equivalent_pins` | object\<string, array\<[string, string]\>\> | Optional per-subcircuit swappable-pin groups, keyed by **reference**-side subcircuit name (`NetlistComparer.equivalent_pins` only accepts circuits from the netlist passed as `compare()`'s second argument, which is always the reference netlist in this command's `compare(layout, reference)` call order). |
| `options.keep_extracted` | boolean | When `layout.file` is given (inline extraction), retain the intermediate extracted netlist on disk at `<request-dir>/.klt/lvs/<top>.spice` and echo its path in `environment.extracted_netlist`, where `<request-dir>` is the request file's directory (or the current working directory for the `-`/inline-JSON forms). Default `false` (nothing is written to disk). Written *before* `options.combine_devices` runs (a genuinely intermediate, pre-combine snapshot); when `combine_devices: true` and the deck sets `ResistorDevice.fixed_offset_ohm` (issue #559), the retained netlist also predates that correction (deferred until after combining — see `options.combine_devices` below), so its `R` values are the raw, uncorrected-and-uncombined per-primitive figures, not what the compare itself uses. |
| `options.combine_devices` | boolean | When `true`, calls `klayout.db.Netlist.combine_devices()` on **both** the layout and reference netlists before comparing — merging devices a device class recognises as combinable (e.g. parallel/series MOSFETs sharing gate/source/drain/body connectivity). This is what makes folded/multi-finger devices (a wide transistor drawn as N parallel fingers of width `W/N`) and split/interleaved matched-pair segments (common-centroid, interdigitated layout) comparable against a single lumped schematic device — without it, each finger/segment reports as its own unmatched device. Default `false` (today's per-drawn-device matching, unchanged) because unconditional merging would also collapse genuinely-distinct parallel devices (e.g. a DAC array's intentionally-separate legs) that some callers want reported individually — opt in only when the layout actually uses folded/split constructions. Applied identically for both engines. After combining, `klt lvs` purges the interior nets `combine_devices()` empties — the N-1 interior nodes of a collapsed series string, left with zero terminals and zero pins once their devices are folded — so `counts.nets.*` and `mismatches[]` reflect the post-combine, post-purge netlist rather than the raw post-combine one (otherwise those disconnected nodes would inflate `counts.nets.layout` and surface as spurious `net.unmatched` findings no caller could act on). The purge is scoped to genuinely-empty nets (no terminals, no pins, no subcircuit pins), so a genuinely-unused top-level pin's net is never dropped and `counts.pins.*` is unaffected; it runs only when combining actually ran (`false` leaves counts exactly as before). On a **partial-match device group** — N real (matching-relevant) instances plus M dummy instances that all share two of three terminals, but only the N real instances also share the third (e.g. a matched bipolar/MOS array's flanking dummies) — `klayout.db`'s own `combine_devices()` can raise an internal-consistency `RuntimeError` rather than combining just the maximal matching subset; `klt lvs` catches that specific error per netlist instead of letting it abort the run, keeps whatever it had already combined, leaves the rest of that netlist's devices individual, and records a `severity: "warning"`, `category: "device.combine_incomplete"` entry in `mismatches[]` — see "`device.combine_incomplete`" below. **Whether this error fires is not fully deterministic across otherwise-identical runs (issue #1185)** — the same layout GDS + reference netlist can combine cleanly on one invocation and hit the error on the next, because KLayout's own `combine_devices()` groups candidates using an ordering that depends on process heap addresses, not on netlist content, and that ordering is not controllable from Python. `klt lvs` retries the combine per side against independent netlist copies to cut the observed flake rate sharply (not to zero — it cannot be, per the above); see "`device.combine_incomplete`" below for the full explanation and the retry budget. **Fixed-offset resistor correction (issue #559):** for a deck row that sets `ResistorDevice.fixed_offset_ohm` (see `klt extract`'s docs, "Drawn resistors" — currently only sky130's `res_high_po`), inline extraction (`layout.file` + `layout.deck`) normally applies that fixed per-instance correction to `R` at extraction time, once per drawn primitive. When `combine_devices: true`, `klt lvs` instead defers that correction and applies it once, after combining — so N series-connected drawn primitives folded into one logical device get the fixed offset exactly once (`total_L/W*sheet_rho + 1*fixed_offset_ohm`), not once per primitive (`total_L/W*sheet_rho + N*fixed_offset_ohm`), which is what KLayout's own `combine_devices()` would otherwise produce by summing each primitive's already-corrected `R`. Only the layout side is affected (the correction is a layout-deck geometric property, not a schematic one). This deferred correction also applies to the **pre-extracted `layout.netlist` shape when a `layout.deck` is supplied alongside it** (issue #585): `layout.deck` there does not trigger extraction, but it does name the deck whose `fixed_offset_ohm` `klt lvs` applies once per post-combine device, exactly as for inline extraction. For that to produce the correct result the pre-extracted SPICE must have been written with the correction *deferred* — extract it with `klt extract --defer-resistor-fixed-offset` (the CLI, issue #588) or `run_extract(..., apply_resistor_fixed_offset=False)` (the Python API, the same switch), which omits the per-primitive offset from the written `R` so this option can add it once after the series fold. Those two are the extraction-time half of this contract, reachable from a subprocess-only flow and from an importing one respectively; see `docs/cli/extract.md`'s "Deferring the fixed resistor offset". A `layout.netlist` extracted the default way already has the offset baked into each primitive; feeding that through `combine_devices: true` with a `layout.deck` would double-count it (the already-summed per-primitive offset cannot be un-summed after folding), so pair `combine_devices` with a deferred extraction, or omit `layout.deck` to leave the pre-extracted `R` values untouched. Omitting `layout.deck` entirely (the bare `{"netlist": ..., "top": ...}` shape) attempts no correction at all — the pre-extracted `R` values are used exactly as written. |
| `options.parameter_tolerance` | number | Optional relative tolerance for numeric device parameters, expressed as a **fraction** (`0.001` is 0.1%), applied to every parameter of every device class (issue #589). `"engine": "klayout"` only. Omit (or `null`) for today's exact compare — the default is unchanged and no existing verdict moves unless a caller opts in. When given, a device pair whose *every* differing parameter is within the tolerance is compared as if those values agreed, so a physically-clean design whose extracted value is a deck's 5–6-significant-figure model fit can reach `status: "match"` against a schematic reference rounded to 2–3 figures. Each absorbed difference is disclosed as a `severity: "warning"`, `category: "device.parameter_tolerated"` entry carrying both original values — see "`device.parameter_tolerated`" below, which also documents the mechanism and its limits. Must be a number in `[0, 1)`; anything else (a string, a per-parameter object, a negative value, `1.0` or above) is an application error (exit 1), not a silent fallback to the default. |
| `options.netgen_setup` | string | Only used with `"engine": "netgen"`. Path to a netgen LVS setup `.tcl` file — see "Engine" -> `"netgen"` above. Omit to run with netgen's own default setup. |
| `options.netgen_timeout_s` | number | Only used with `"engine": "netgen"`. Wall-clock budget (seconds) for the `netgen` subprocess. Default `300`. |
| `options.flatten_reference` | boolean | When `true`, calls `klayout.db.Netlist.flatten()` on the **reference** netlist in-process, right after it is read and before circuit selection (issue #1085). `klt extract` always extracts a *flat* layout-side netlist — a single top circuit, no subcircuit calls (see `klt extract`'s "flat, not hierarchical" limitation note) — so a hierarchical reference (one leaf `.subckt` plus N instance calls of it, the shape a macro built by tiling one verified leaf cell naturally takes) can never structurally match it: `NetlistComparer` compares circuit-by-circuit, and the flat layout side simply has no subcircuit-call circuit to pair against the reference's, producing an undiagnosable `topology` "circuit could not be matched to a counterpart" mismatch on both sides. Flattening the reference first collapses every subcircuit-call instance in place, so only its top-level circuit(s) remain — directly comparable against the already-flat layout side. Default `false` (today's unconditional per-circuit matching, unchanged) — a caller who genuinely wants a hierarchy-preserving compare (e.g. because both sides are hierarchical, see `tests/test_lvs.py`'s `test_net_correspondence_scopes_dedup_by_circuit`) is never silently flattened out from under them. A `reference.top` name that only existed as an interior circuit flatten would inline away no longer resolves after flattening — pass the name of whatever remains a genuine top-level circuit. Each side that is actually flattened (its circuit count changes) is disclosed as a `severity: "warning"`, `category: "topology.flattened"` entry — see "`topology.flattened`" below — so a `"match"` reached after flattening is never silently indistinguishable from one reached against the netlist's original hierarchy; a netlist that already had only its top circuit(s) (nothing to flatten) adds no such entry. |
| `options.flatten_layout` | boolean | The symmetric counterpart of `options.flatten_reference`, for the **layout** side (issue #1085's item 2): calls `Netlist.flatten()` on the layout netlist right after it is resolved, before circuit selection. Meaningful for the pre-extracted `layout.netlist` shape, which can itself be hierarchical (a hand-written or externally-extracted SPICE netlist is under no obligation to be flat the way `klt extract`'s own output always is) — a no-op for the `layout.file` (inline extraction) shape, since that netlist is already flat. Default `false`; same `topology.flattened` disclosure and no-op-on-an-already-flat-netlist behaviour as `options.flatten_reference` above. |

### `layout` shapes

Extraction and compare are separable steps that also compose in one call:

- **Inline extraction** — `{"file": "design.gds", "deck": "sky130", "top": "ota_5t"}`. Runs `klt extract`'s core extraction (the same `extract_netlist_from_layout` function `klt extract` itself calls) against the named curated deck (`sky130`/`gf180mcu`), then compares the resulting in-memory netlist directly — no SPICE round-trip is required unless `options.keep_extracted` is set. `top` is optional (defaults to the layout's sole top cell, same as `klt extract --top`); `deck` is required in this shape. An optional boolean `top_cell_pins` (default `false`) mirrors `klt extract --top-cell-pins`: when `true`, only labels drawn directly in the top cell are promoted to top-level pins, so a net named only by a label inside an instanced sub-cell stays internal instead of demanding a matching port in `reference.netlist` (issue #291). An optional array of strings `declared_pins` (default unset) mirrors `klt extract --pins` (issue #514): when given, every promoted pin not named in this set is demoted back to an internal net, so naming an internal node of a lumped schematic device (e.g. one tap of a metal-option ladder) for documentation no longer promotes it to a pin `options.combine_devices` cannot fold through. Applied after `top_cell_pins`'s own reconciliation — it can only further restrict the promoted set. An empty `declared_pins` array is a request error (omit the field entirely to keep every named net promoted). Both `top_cell_pins` and `declared_pins` are only meaningful in this inline-extraction shape — they have no effect on a pre-extracted `layout.netlist`, whose pins are already fixed. An optional object `deck_options` (issue #600) mirrors `klt extract --deck-option`: selects a caller-visible sheet-rho flavour of a shared-geometry resistor family for this extraction — see the `layout.deck_options` field-table row above.
- **Pre-extracted netlist** — `{"netlist": "design.spice", "top": "ota_5t"}`. Reads an existing extracted (or hand-written) SPICE netlist directly via `NetlistSpiceReader`, skipping extraction entirely. `top` is optional (defaults to the sole top circuit). `deck_options` is also honored here when `deck` is supplied alongside `netlist` (issue #585's pre-extracted-plus-deck shape): no extraction runs in this shape, so `deck_options` only affects `device_classes` and the deferred resistor `fixed_offset_ohm` correction (`options.combine_devices`) — it has no effect on the SPICE `R` values already baked into the supplied netlist.

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
  "reference_top": "ota_5t",
  "parameter_tolerance": null,
  "options": {
    "combine_devices": false,
    "flatten_layout": false,
    "flatten_reference": false,
    "netgen_setup": null,
    "parameter_tolerance": null
  },
  "status": "match",
  "mismatch_count": 0,
  "error_count": 0,
  "category_counts": {},
  "category_error_counts": {},
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
    "deck": { "name": "sky130", "content_hash": "sha256:<hex>", "released": true }
  },
  "mismatches": [],
  "net_correspondence": [
    { "layout": "A", "reference": "A", "pin": true },
    { "layout": "VGND", "reference": "VGND", "pin": true },
    { "layout": "VPWR", "reference": "VPWR", "pin": true },
    { "layout": "Y", "reference": "Y", "pin": true }
  ]
}
```

A `"netgen"`-engine mismatch (issue #343), showing `environment.engine_version`
sourced from netgen's own banner and a `details`-carrying entry for a report
section this engine buckets rather than fully structures:

```json
{
  "schema_version": 1,
  "engine": "netgen",
  "status": "mismatch",
  "mismatch_count": 1,
  "error_count": 1,
  "category_counts": { "net.unmatched": 1 },
  "category_error_counts": { "net.unmatched": 1 },
  "counts": {
    "nets": { "layout": 4, "reference": 4, "matched": 0 },
    "devices": { "layout": 2, "reference": 2, "matched": 0 },
    "pins": { "layout": 4, "reference": 4, "matched": 0 }
  },
  "environment": {
    "engine": "netgen",
    "engine_version": "1.5.323"
  },
  "mismatches": [
    {
      "category": "net.unmatched",
      "severity": "error",
      "description": "netgen reported one or more net mismatch(es) -- see the 'details.raw' field for netgen's own side-by-side report",
      "side": "both",
      "net": null,
      "device": null,
      "property": null,
      "details": { "raw": "NET mismatches: Class fragments follow ...\n..." },
      "circuit": null,
      "instance": null,
      "subcircuit": null
    }
  ],
  "net_correspondence": []
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
| `reference_top` | string | The **reference** side's resolved top circuit name (issue #1205). Equal to `top` for the ordinary compare, but different by construction for an LVS negative control — a deliberately-broken `<cell>_shorted` layout compared against the *intact* `<cell>`'s reference netlist. Recording only one top made such a report unreconstructable by `--check --rerun` (it applied the single `top` to both sides and failed with "top cell/subcircuit not found in reference netlist"). |
| `parameter_tolerance` | number \| `null` | Echo of the effective `options.parameter_tolerance` (issue #589) — `null` when the option was omitted (the default exact compare). Always present, never omitted, so a consumer reading only the response can always tell whether a `"match"` was reached under a caller-supplied design tolerance at all. |
| `options` | object | Echo of every request option that shapes *what was compared*, as resolved (issue #1205): `combine_devices`, `flatten_layout`, `flatten_reference` (booleans), `netgen_setup` (string \| `null`, echoed exactly as given, not resolved against the request file's directory), and `parameter_tolerance` (number \| `null`, the same value as the top-level field above, repeated here so this block is a complete request-side view). Every key is always present with its effective value, never omitted — so a consumer reading only the response can tell which compare the verdict belongs to, and `--check --rerun` can re-run *that* compare rather than a differently-shaped one whose difference it would then report as drift. `options.keep_extracted` is deliberately not echoed here (it is an output-side flag that cannot change a verdict, and is already visible as `environment.extracted_netlist`), nor is `options.netgen_timeout_s` (a runtime guard, not a compare input). |
| `status` | `"match"` \| `"mismatch"` | `"match"` when `NetlistComparer.compare()` reports the netlists equivalent; `"mismatch"` otherwise. Never `"error"` in-band — a failed run does not emit this envelope at all (see "Exit codes"). This is always the engine's own verdict, including when `options.parameter_tolerance` is in force — that option is implemented by re-running a real `compare()` on values snapped into agreement, never by re-deriving the verdict from this command's own findings (see "`device.parameter_tolerated`" below). |
| `mismatch_count` | integer | `len(mismatches)`. Can be nonzero even when `status` is `"match"` — a `severity: "warning"` entry (e.g. an ambiguity the comparer resolved on its own) does not change the verdict. |
| `error_count` | integer | Issue #1132: the number of `mismatches[]` entries with `severity: "error"` — `sum(category_error_counts.values())`. `mismatch_count` alone cannot tell a caller this without re-reading every entry, since a nonzero `mismatch_count` can be entirely `severity: "warning"` (e.g. a report whose only finding is a `device.bulk_reconciled` disclosure). `0` on a `status: "match"` report exactly (a `"match"` verdict never carries an `error` entry). |
| `category_counts` | object\<string, int\> | Per-category mismatch counts (`error` and `warning` entries combined), keys sorted for determinism — the LVS analogue of `klt drc`'s `rule_counts`. |
| `category_error_counts` | object\<string, int\> | Issue #1132: `category_counts`, but counting only `severity: "error"` entries per category — lets a caller gate on "does category X have any real defect" without re-reading `mismatches[]` and re-filtering by `severity` itself. Same sorted-keys convention as `category_counts`; a category with zero `error` entries (all `warning`, e.g. an all-`warning` `topology.flattened` run) is simply absent from this object rather than reported as `0`, matching `category_counts`'s own "no entries of this category" convention. `sum(category_error_counts.values()) == error_count`. |
| `counts` | object | Side-by-side `layout`/`reference`/`matched` tallies for `nets`, `devices`, `pins`. `matched` counts only a **strictly successful** pairing (e.g. a device paired with identical parameters and class) — a device paired despite a `device.property`/`device.class` mismatch is *not* counted as matched. For `"engine": "netgen"`, `matched` is exact on a `"match"` verdict and `0` on a `"mismatch"` verdict (a known limitation — see "Engine" -> `"netgen"` above). |
| `device_classes` | array\<string\> \| `null` | The layout-side deck's `ExtractionDeck.device_classes` (see `klt extract`'s own field of the same name) — what that deck is structurally capable of recognising, not what this compare found. Present (currently `["nfet", "pfet", "resistor"]` for both registered decks — MOS plus one drawn precision resistor, see `klt extract`'s "Drawn resistors") whenever a `layout.deck` is given — always for `layout.file` (inline extraction, where the deck is required), and also for the pre-extracted `layout.netlist` shape when a `layout.deck` is supplied alongside it (issue #585). `null` only when no `layout.deck` was given (the bare `{"netlist": ..., "top": ...}` shape). |
| `environment` | object | Reproducibility block: `engine`, `engine_version` (the installed `klayout` package version for `"engine": "klayout"`; netgen's own reported version, parsed from its startup banner, for `"engine": "netgen"` — `null` if unparseable), `layout_sha256` (of `layout.file`, or of `layout.netlist` when no extraction ran), `reference_sha256` (of `reference.netlist`), `extracted_netlist` (path to the retained intermediate netlist when `options.keep_extracted` is set and `layout.file` was given; `null` otherwise). |
| `provenance` | object | Shared reproducibility block (`klt_version`, `klayout_version`, `pdk`, `deck`) defined once in [`docs/json-contract.md`](../json-contract.md). `pdk` is always `null` (LVS is topological and resolves no PDK); `deck` pins the layout-side extraction deck by name and `sha256:` content hash whenever a `layout.deck` is given (both the `layout.file` and the pre-extracted `layout.netlist` shapes), and is `null` only when no `layout.deck` was given (matching `device_classes`). `deck.released` (issue #1193) is a non-fatal tri-state signal for whether that content hash ships in any released `klayout-tools` version — `false` flags an unreleased/dev-edited deck, `null` when unresolvable (e.g. the generated deck history table is missing). `deck.options` (issue #600) echoes the resolved `layout.deck_options` mapping — present only when non-empty, matching `klt extract`'s own `provenance.deck.options` shape exactly. `klayout_version` is populated the same way for both engines (it is this process's own `klayout` package build, used for netlist parsing/writing either way, not the comparator). |
| `mismatches` | array\<object\> | One entry per structured mismatch — see below. Empty on a clean match; always present. |
| `net_correspondence` | array\<object\> | The layout↔reference net pairing `NetlistComparer` produced — see "`net_correspondence[]` entries" below. `len(net_correspondence) == counts.nets.matched` (the example above is illustrative, not exhaustive, for a 7-net compare). Always `[]` for `"engine": "netgen"` (see "Engine" -> `"netgen"` above). |

### `mismatches[]` entries

Field-for-field the LVS counterpart of `klt drc`'s `violations[]`: a stable
`category` id (never renumbered/repurposed once shipped, exactly like a DRC
rule id), a curated human `description` (not raw engine log text), and the
objects involved.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `category` | string | One of `net.unmatched`, `net.merged`, `net.split`, `device.unmatched`, `device.class`, `device.class_arity`, `device.bulk_reconciled`, `device.property`, `device.parameter_tolerated`, `device.body_unverified`, `device.combine_incomplete`, `pin.unmatched`, `topology`, `topology.flattened`, `hints.rejected`. |
| `severity` | `"error"` \| `"warning"` | `"error"` breaks equivalence; `"warning"` is informational and never changes `status`. Informational cases include an ambiguous net pairing the comparer resolved on its own (see `hints.same_nets` above), a `topology` device-class-mismatch entry for a device class with zero actual instances on the side that registered it (e.g. an all-`nfet` layout compared against an all-`nfet` reference netlist that never mentions `pfet` — `klt extract` always registers both polarities' device classes even when only one is instantiated), every `device.body_unverified` entry (see below), every `device.combine_incomplete` entry (see below), and the collateral `device.unmatched`/`net.unmatched` entries left over when a minimal cell's parameter defect is recovered into a `device.property` entry (see "Negative controls" above). A device-class mismatch where the class has one or more real instances still reports `"error"`. Every `hints.rejected` entry (see below) is always `"error"` — `hints.same_nets` is a hard assertion (`must_match=True`), never a suggestion, so the comparer refusing it is always a real finding. Every `device.class_arity` entry (see below) is always `"error"` — a same-named device class the comparer cannot pair on either side is never merely informational. Every `device.bulk_reconciled` entry (see below) is always `"warning"` — it discloses a request-side reconciliation applied before the compare, so it never changes `status` (a request whose only finding is this entry reports `status: "match"` with a nonzero `mismatch_count`). Every `device.parameter_tolerated` entry (see below) is always `"warning"` for the same reason — it discloses a numeric difference `options.parameter_tolerance` absorbed, so it never changes `status` either. Every `topology.flattened` entry (see below) is likewise always `"warning"` — it discloses a request-side structural flatten `options.flatten_reference`/`options.flatten_layout` applied before the compare, so it never changes `status` either. |
| `description` | string | Curated, human-readable explanation of this mismatch — never raw `NetlistComparer` log text (which is version-dependent and, per this repo's own testing, sometimes empty). |
| `side` | `"layout"` \| `"reference"` \| `"both"` | Which netlist the offending object(s) live on. |
| `net` | object \| `null` | `{"layout": <name\|null>, "reference": <name\|null>}` when a net is involved. |
| `device` | object \| `null` | `{"layout": <name\|null>, "reference": <name\|null>, "class": <string\|null>}` when a device is involved. |
| `property` | object \| `null` | `{"name": <string>, "layout": <value>, "reference": <value>}` for a `device.property` mismatch, and for a `device.parameter_tolerated` disclosure (whose `reference` is always the reference netlist's *original* value, never the snapped one). `name` is `w_um`/`l_um` for the width/length parameters (matching `klt extract`'s own convention); every other declared device-class parameter is reported under its own lower-cased name. |
| `details` | object \| `null` | Engine-specific/category-specific data that does not map cleanly onto the fields above (issue #343) — additive, not a schema fork. Populated for every `"klayout"`-engine `device.class_arity` entry (see below) with `{"layout_terminals": [<string>, ...], "reference_terminals": [<string>, ...]}`, and for every `device.bulk_reconciled` entry (see below) with `{"terminal": <string>, "reference_net": <string>, "reference_net_created": <bool>, "devices": <integer>, "layout_terminals": [<string>, ...], "reference_terminals": [<string>, ...]}` (`reference_terminals` is the pre-reconciliation list), and for every `device.parameter_tolerated` entry (see below) with `{"relative_delta": <number>, "tolerance": <number>}` (the observed `|layout - reference| / max(|layout|, |reference|)` and the effective `options.parameter_tolerance` it was accepted under). Also populated by the `"netgen"` engine for a `net.unmatched`/`device.unmatched` entry bucketing a whole side-by-side report section it does not further structure: `{"raw": <string>}`, netgen's own report text for that section verbatim. `null` for every other entry (including `"netgen"`-engine device-class-arity mismatches, which this issue's fix does not cover — see "`device.class_arity`" below). |
| `circuit` | object \| `null` | Issue #1132: `{"layout": <name\|null>, "reference": <name\|null>}` — the circuit (module) involved, for a `topology` entry from an unmatched *circuit* (the circuit itself has no counterpart) or an unmatched subcircuit *instance* (the circuit **containing** the instance, not the instance's own name — see `instance`/`subcircuit` below). `null` for every other entry, matching `net`/`device`'s own "populated only on the categories that involve one" convention. Currently only the `"klayout"` engine populates this field — the `"netgen"`-engine `net.unmatched`/`device.unmatched` entries (see `details` above) do not name a circuit, since netgen's own report does not structure one out. |
| `instance` | object \| `null` | Issue #1132: `{"layout": <name\|null>, "reference": <name\|null>}` — the subcircuit instance's own name (e.g. `"Xfill_1_0"`), populated only for an unmatched-subcircuit-*instance* `topology` entry. `null` for an unmatched-*circuit* entry (there is no instance — the whole circuit definition has no counterpart) and for every other category. |
| `subcircuit` | object \| `null` | Issue #1132: `{"layout": <name\|null>, "reference": <name\|null>}` — the name of the circuit the unmatched instance refers to (its "cell type", e.g. `"sky130_fd_sc_hd__fill_1"`), populated only alongside `instance` above. `null` everywhere `instance` is `null`. |

`mismatches` is sorted by `(category, side, device.layout, device.reference,
net.layout, net.reference)` (missing fields sort first) so repeated runs
against the same inputs produce identical, diff-clean output — the same
canonical-ordering guarantee `klt drc` makes about `violations`.

### `net_correspondence[]` entries

`klt lvs` computes a full layout-net ↔ reference-net correspondence
internally (that is what makes `counts.nets.matched` meaningful), but until
this field existed the report never surfaced it — a caller could not attach
anything to a *named* schematic node without re-deriving the pairing itself.
An extracted netlist's net names are mostly not the schematic's: extraction
names a net after a drawn label if there is one and positionally (KLayout's
own `$5`, `$12`, … placeholder, reported here backslash-escaped to `\$5`,
`\$12`, … — issue #1162, see `klt extract`'s docs, "Anonymous nets are
backslash-escaped") otherwise, so for every internal node the schematic name
exists only on the reference side and the extracted name only on the layout
side.
`net_correspondence` closes that gap directly from the comparer's own
`match_nets`/`match_ambiguous_nets` callbacks — no re-derivation, no graph
isomorphism reimplemented downstream.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `layout` | string | The layout net's name — the same helper `mismatches[].net` uses, so a net two drawn labels merged carries both aliases `\|`-joined, e.g. `"VPWR\|VDD"`, and an anonymous net's KLayout-synthesized placeholder is backslash-escaped, e.g. `"\$5"` (issue #1162), byte-identical to `klt extract`'s `nets[].name`/`merged_net_labels[].net` and the written netlist's own node spelling for that net (issue #696), not KLayout's own un-escaped, comma-joined `Net.expanded_name()`. |
| `reference` | string | The paired reference net's name, same convention. |
| `pin` | boolean | Whether this net is one of the compared circuit's declared pins (`Net.pin_count() > 0`), read from the layout side. `same_circuits` pins the layout/reference top circuits together before the compare runs, so a matched pair's declared-pin status agrees on both sides by construction. |

Populated for every successful pairing the comparer made — both an
unambiguous `match_nets` event and an ambiguously-resolved
`match_ambiguous_nets` event (the same events that also produce the
`topology`/`"warning"` entry in `mismatches[]` — see "Ambiguous net
pairing" below; a pairing can appear in both places at once, since one
documents *that* an ambiguity was resolved and the other documents *what*
it resolved to). Emitted whenever the comparer produced at least one net
pairing, regardless of `status` — on a partial/failed compare, the pairs
that *did* match are still useful for localising the ones that did not (a
net with no counterpart at all, e.g. one side dropped a device entirely,
simply has no entry). `device_correspondence` (the same idea for devices)
is not yet implemented — track it separately if needed.

Sorted by `(reference, layout)`, so repeated runs against the same inputs
produce identical, diff-clean output — the same ordering guarantee
`mismatches[]` makes. Deduplication is scoped **per circuit** (by the
comparer's circuit scope, not by net name alone): a hierarchical netlist
routinely reuses a local net name — `MID`, `OUT`, `A` — across unrelated
subcircuits, and each such net is a distinct correspondence with its own
`pin` flag. Two entries can therefore share the same `layout`/`reference`
name (one per circuit) — that is expected, and is what keeps
`len(net_correspondence) == counts.nets.matched` exact across a hierarchy.

#### `device.class_arity`: same device-class name, different terminal count on each side

A curated deck can extract a device flavour through a device extractor that
declares an extra terminal beyond the plain two/three-node element a
schematic-derived reference netlist states it as — e.g. a `bulk_to_substrate`
resistor flavour extracted via `DeviceExtractorResistorWithBulk` writes a
three-node (`A`/`B`/`W`) `R` card, which `NetlistSpiceReader` reads back as
`DeviceClassResistorWithBulk`; a schematic reference's plain two-node `R` card
for the *same model name* reads back as the two-terminal `DeviceClassResistor`
instead (issue #504). Both sides register a device class of the same name,
but with a different terminal list — `NetlistComparer` cannot pair any
instance of that class at all, and since the class *names* agree it does not
report this as `device.class` (a matched-but-differently-classed pair) or as
a `topology` device-class-mismatch (a class registered on only one side)
either. Left unclassified, it degrades into an unattributable
`device.unmatched`/`net.unmatched` cascade with no entry naming the actual
cause — the "silent 0/0" this issue describes on a circuit small enough that
nothing else anchors the compare.

`klt lvs` detects this case directly from the `NetlistComparer` event that
carries **both** device instances (unlike an ordinary one-sided
`device.unmatched`, where only one side has a device at all) and reports one
`category: "device.class_arity"`, `severity: "error"`, `side: "both"` entry
per affected device pair, naming both classes' terminal lists:

```json
{
  "category": "device.class_arity",
  "severity": "error",
  "description": "device class 'RES_X' is declared with a different terminal list on each side (layout: ['A', 'B', 'W'], reference: ['A', 'B']) -- the comparer cannot pair devices of this class at all; see docs/cli/lvs.md, 'device.class_arity'",
  "side": "both",
  "net": null,
  "device": {"layout": "1", "reference": "R1", "class": "RES_X"},
  "property": null,
  "details": {"layout_terminals": ["A", "B", "W"], "reference_terminals": ["A", "B"]}
}
```

This entry is **diagnostic**: it turns the unattributable cascade into a
one-line diagnosis naming both terminal lists, but does not itself let the two
sides' devices match — `status` still reports `"mismatch"` when this entry
appears, and the collateral `net.unmatched`/`device.unmatched` entries the
comparer's own event stream produces for the same device pair are still
reported alongside it (unlike the issue #282 minimal-cell parameter recovery,
which suppresses genuinely collateral entries — there is no such suppression
here, since the affected nets/devices are not necessarily otherwise accounted
for). Only implemented for `"engine": "klayout"`; the `"netgen"` engine's
report parser does not produce this category (its own report format does not
distinguish this case from an ordinary unmatched device/net).

To *reconcile* the two classes rather than only diagnose them — so the compare
can legitimately reach `status: "match"` — declare the reference side's
implicit bulk terminal with **`reference.device_bulk`** (issue #506, issue
#504's option 1); see "`device.bulk_reconciled`" immediately below. A class the
request reconciles that way no longer emits `device.class_arity` at all (the
two classes are the same arity by the time `compare()` sees them); a
bulk-terminal class the request does *not* name still does, so a remaining
arity gap is never turned into a silent pass.

#### `device.bulk_reconciled`: `reference.device_bulk` normalised a reference class before comparing

Only possible when `request.reference.device_bulk` is given (issue #506,
`"engine": "klayout"` only). For each `{"<model>": "<reference net>"}` entry,
`klt lvs` gives the reference-side device class of that name the one terminal
its layout-side namesake declares and it does not — the deck's bulk/well/
collector terminal (e.g. the `W` of a `bulk_to_substrate` resistor flavour's
three-terminal `RES_X`) — and ties that terminal to the named reference net on
every reference-side instance of the class, *before* `NetlistComparer` is
constructed. Without it, no request whose layout side uses a bulk-terminal
device flavour against a schematic reference that does not model that terminal
can ever report `status: "match"`; `device.class_arity` above is that
situation's diagnosis, and this is its resolution.

The added terminal's connectivity is a **caller assertion**, not something read
off the reference netlist, so every reconciled class is disclosed in-band as
one `severity: "warning"`, `category: "device.bulk_reconciled"`,
`side: "reference"` entry — the same discipline `device.body_unverified`
applies to an unverified MOS body. A `"match"` reached through this hook is
therefore never silently indistinguishable from a fully independent one:

```json
{
  "category": "device.bulk_reconciled",
  "severity": "warning",
  "description": "request.reference.device_bulk reconciled reference device class 'RES_X' with the layout side: a 'W' terminal was added to the reference class (layout: ['A', 'B', 'W'], reference was: ['A', 'B']) and tied to reference net 'BULK' on 1 device instance(s), a net created for this compare -- that terminal's connectivity was asserted by the request, not read from the reference netlist, so this dimension of the compare is not independently verified (see docs/cli/lvs.md, 'device.bulk_reconciled')",
  "side": "reference",
  "net": null,
  "device": {"layout": null, "reference": null, "class": "RES_X"},
  "property": null,
  "details": {
    "terminal": "W",
    "reference_net": "BULK",
    "reference_net_created": true,
    "devices": 1,
    "layout_terminals": ["A", "B", "W"],
    "reference_terminals": ["A", "B"]
  }
}
```

Notes on the semantics:

- **The named net is resolved per circuit that instantiates the class**
  (matched exactly, then case-insensitively) and **created** there when the
  reference netlist does not model that node at all —
  `details.reference_net_created` says which happened. Binding to an
  already-modelled reference net (`false`) is the stronger case: the compare
  then checks the bulk terminal against real reference connectivity, and only
  the *claim that the class carries the terminal* is asserted.
- **It composes with `hints.same_nets`.** When the layout-side bulk net is the
  deck's synthesized substrate net (`vsubs`) and the reference models it as a
  real rail (`VSS`), point `device_bulk` at `VSS` and pair the two names with a
  `hints.same_nets` entry.
- **It never changes `status`.** The entry is always `severity: "warning"`, so
  a request whose only finding is this one reports `status: "match"` with a
  nonzero `mismatch_count` (the same relationship `device.body_unverified` has
  to a clean compare).
- **Malformed or inapplicable entries are application errors (exit 1)**, never
  silent no-ops — a model name that resolves on neither side, a reference class
  that is not actually missing a terminal, a class two or more terminals apart
  (this hook reconciles exactly one extra terminal per class, since the entry
  names exactly one net), and use with `"engine": "netgen"` all raise, matching
  `hints.same_nets`'s own "a typo'd hint should be visible" convention.
- **The reference side may still declare fewer top-level pins** than the layout
  when the created net corresponds to a layout port (a `bulk`/`vsubs` pin the
  schematic never had); that shows up in `counts.pins.*`, not as a mismatch
  entry. Declare the port in the reference netlist if you want pin parity too.
- **It runs after `options.combine_devices`**, so combining still sees each
  side's own, unmodified device classes (today's behaviour, unchanged) — a
  reference-side class is combined as the two-terminal element the reference
  netlist actually declares, then reconciled up to the layout's arity for the
  compare.

#### `device.parameter_tolerated`: `options.parameter_tolerance` absorbed a numeric difference

Only possible when `options.parameter_tolerance` is given (issue #589,
`"engine": "klayout"` only). Extraction is geometrically exact against the
curated deck's *own* device model, while a schematic reference's values
routinely come from a rounded design-level model — a datasheet-style
`R ≈ A + B·L` carried to three figures, a hand-computed `W/L`, a rounded cap
value. The two then differ by well under 0.1%, far inside any real
manufacturing tolerance and not a design error, but with no request-level knob
that difference is a hard `device.property` error per device and a physically
clean compare can never read `match`.

`options.parameter_tolerance` is that knob: a single relative tolerance,
expressed as a fraction (`0.001` is 0.1%), applied to every numeric parameter
of every device class.

```json
{
  "category": "device.parameter_tolerated",
  "severity": "warning",
  "description": "matched device parameter 'w_um' differs by 0.08992%, within the requested options.parameter_tolerance -- the reference value was snapped to the layout value for the comparison, so this dimension of the compare was verified only to that tolerance, not exactly (see docs/cli/lvs.md, 'device.parameter_tolerated')",
  "side": "both",
  "net": null,
  "device": {"layout": "2", "reference": "2", "class": "PFET"},
  "property": {"name": "w_um", "layout": 1.0, "reference": 1.0009},
  "details": {"relative_delta": 0.0008991907283446126, "tolerance": 0.001}
}
```

Notes on the semantics:

- **It is not a widened comparison epsilon.** `status` is always
  `NetlistComparer.compare()`'s own boolean (see the field table above), and
  `compare()` decides parameter equality with its own, tighter,
  non-configurable tolerance *before* `klt lvs` classifies anything — so
  loosening this command's own float-noise epsilon could only ever suppress a
  `device.property` *entry* for a pair the engine had already called
  mismatched, never move the verdict. Instead, `klt lvs` snaps the
  reference-side value of every in-tolerance parameter to its layout-side
  counterpart and runs a **second, real `compare()`** on the resulting
  netlists. A tolerance-assisted `"match"` is therefore still a genuine
  engine verdict, on inputs the caller declared equivalent.
- **It is all-or-nothing per device pair.** A pair whose `W` is in tolerance
  but whose `L` is not is left completely alone and reports both parameters
  exactly as it would with the option omitted — dropping the in-tolerance one
  from a report that still says `mismatch` would be strictly less information
  about a pair the tolerance cannot rescue anyway.
- **Nothing is absorbed silently.** Every snapped parameter emits one
  `severity: "warning"` entry naming the device, the parameter, **both
  original values** (`property.reference` is always the reference netlist's
  pre-snap number) and the observed relative delta, and the effective
  tolerance is echoed at the top level as `parameter_tolerance`. A
  tolerance-assisted match is never indistinguishable from one where the
  numbers actually agreed.
- **The default is unchanged.** Omitting the option (or passing `null`) skips
  the whole mechanism — the same single `compare()` call, the same verdict,
  the same `mismatches[]` as before this option existed.
- **It covers both parameter-difference shapes.** The clean case, where
  `NetlistComparer` pairs the devices from surrounding connectivity and then
  reports differing parameters, and the minimal-cell case (issue #282, see
  "Negative controls") where the comparer never pairs them at all and
  `klt lvs` reconstructs the pair from the resulting `device.unmatched`/
  `net.unmatched` cascade. **Known limit:** that reconstruction is scoped to a
  *single* unmatched device pair, so a circuit small enough to degrade that way
  with *two or more* out-of-agreement devices still reports `mismatch`
  regardless of the tolerance — the same verdict it reports today. Circuits
  large enough for the comparer to pair devices structurally (the ordinary
  case) go through the clean path, which handles any number of pairs.
- **It never masks a connectivity defect.** Only numeric parameters of an
  otherwise-corresponding device pair are ever snapped; a rewired device, a
  device-class swap, a merged/split net and every other structural finding are
  untouched and still report `mismatch`.
- **Malformed values are application errors (exit 1)**, never a silent
  fallback to the default: a string, a per-parameter object, a negative value,
  and `1.0` or above (a relative tolerance of 1 would call almost any two
  values equal) all raise, matching `hints.same_nets`'s own "a typo'd hint
  should be visible" convention. `"engine": "netgen"` also raises — see
  "Engine" → `"netgen"` above for why a relative tolerance has no faithful
  translation into netgen's absolute per-device-class setup syntax.

#### `device.body_unverified`: MOS body terminals compared against a deck-synthesized net

The curated extraction decks can give some MOS body terminals a net that does
not come from any drawn/derived tap or well-label geometry
(`docs/cli/extract.md` → "Coverage"). On **sky130**, `tap.drawing` does
double duty: a shape drawn outside every `nwell` is a genuine, drawable
P-substrate tie, so a layout that draws one and contacts it up to a named
net gives the NMOS body terminal (and the identically-modelled
`bulk_to_substrate` resistor bulk and collector-less bipolar collector) a
real net (issue #490) — only a layout with **no** such ring falls back to
the deck's global substrate net (`connect_global`, e.g. `vsubs`).
**gf180mcu** has no distinct drawn tap layer at all (`Comp` is shared with
ordinary transistor active), but (issue #1084) declares `tap_nplus`/
`tap_pplus` so `klt extract` can *derive* an equivalent tap region from a
drawn `Nplus`/`Pplus`-over-`Comp` tie — a layout that draws one gets the
same real-net resolution as sky130's drawn tap; a layout that draws neither
still lands its NMOS/PMOS bodies on an anonymous, deck-synthesized net
unconditionally, exactly as before #1084. Comparing a synthesized net
against a schematic reference's real ground/rail net still produces a
genuine `NetlistComparer` finding if they disagree, but a *clean* compare on
that dimension does not mean the well/substrate tie was actually verified
against the schematic — it means both sides were forced onto the same
synthetic net.

`klt lvs` surfaces this as one or two `severity: "warning"` entries
(`category: "device.body_unverified"`, `side: "layout"`) whenever
`layout.file` + `layout.deck` (inline extraction) is used — never for the
pre-extracted `layout.netlist` form, since no deck (and therefore no known
synthetic-net behaviour) is involved there:

- An NMOS entry fires when the layout has one or more NMOS devices whose
  body terminal **still** resolved to the deck's synthesized `substrate_net`
  (`device.class` is the deck's `nfet_class`, e.g. `"nfet"`) — a device whose
  body terminal resolved to a real, drawn- or derived-tap net (only where a
  layout actually draws one) is not counted.
- A PMOS entry additionally fires when the layout-side deck has **no tap
  mechanism at all** — neither a distinct drawn `tap` layer nor a derivable
  one (`ExtractionDeck.tap`/`tap_nplus`/`tap_pplus` all `None`; gf180mcu
  before issue #1084) — **and** the layout has one or more PMOS devices
  (`device.class` is the deck's `pfet_class`, e.g. `"pfet"`). A deck that has
  *either* mechanism gives PMOS bodies a real, named net unconditionally
  (every PMOS sits inside an `nwell` by construction), so neither sky130 nor
  (since #1084) gf180mcu emits this entry — even for a specific device whose
  own `nwell` island happens to draw no tie, mirroring this deck-structural
  check's existing (optimistic) treatment for sky130.

Both entries reflect real device-level extraction outcomes (per-device for
NMOS since #490; still deck-structural for PMOS, a property of which deck
ran extraction rather than of any individual device pairing or `hints`),
always `severity: "warning"`, and never change `status` or break
`mismatch_count`'s error semantics — they only make it visible, in-band,
that this dimension of the compare was not fully verified against the
schematic.

#### `device.combine_incomplete`: `options.combine_devices` could not fully combine a partial-match device group

Only possible when `options.combine_devices: true` (issue #466). KLayout's
own `klayout.db.Netlist.combine_devices()` can raise an unhandled internal-
consistency `RuntimeError` on a *partial-match* device group: N real
(matching-relevant) instances plus M dummy instances that all share two of
three terminals (e.g. a bipolar device's base and collector, tied to a
matched array's common well and substrate), but only the N real instances
additionally share the third (e.g. an emitter bussed to one signal net) —
each of the M dummy instances has its own, mutually distinct, third
terminal. That is a `klayout.db` behavior this command merely surfaces, not
a defect in `klt lvs` itself.

`klt lvs` catches this specific error per netlist (narrowly — only a
`RuntimeError` carrying KLayout's own `"...in Netlist.combine_devices"`
marker text; any other `RuntimeError` still propagates as an application
error) instead of letting it abort the whole run: whatever `combine_devices()`
had already merged before hitting the error stays merged, the rest of that
netlist's devices are left as individual devices (the same state they would
be in with `options.combine_devices: false`), and a `severity: "warning"`,
`side: "layout"` or `side: "reference"` entry is added recording that combine
did not fully apply on that side. Never changes `status` or breaks
`mismatch_count`'s error semantics on its own — a caller relying on
`options.combine_devices` to fully lump a matched array should treat this
entry as a signal to inspect `counts.devices` rather than assume every
combinable device actually got combined.

**Run-to-run nondeterminism (issue #1185):** whether this error fires at all
is not a property of the layout GDS + reference netlist content — the exact
same byte-identical input pair can combine cleanly on one `klt lvs`
invocation and hit this error on the next. The root cause is internal to
KLayout's C++ implementation: `combine_devices()` groups combination
candidates in a `std::map` keyed on each net's raw process heap address
(not on anything about the netlist's content), and repeats its parallel/
serial combination passes to a fixed point in an order that is not provably
confluent — so a process-to-process difference in heap layout (e.g. ASLR)
can walk a partial-match device group in a different order and land on a
different outcome. Neither that ordering nor the underlying merge
primitives are exposed to, or overridable from, Python, so `klt lvs` cannot
force this to be deterministic. What it does instead: `_combine_devices_
safely` retries the combine, per side, against up to 5 independent
`klayout.db.Netlist.dup()` copies of the not-yet-combined netlist before
giving up and reporting `device.combine_incomplete` — each retry is a fresh,
uncorrelated sample of KLayout's internal ordering, so at the ~1-in-5
single-attempt failure rate this issue was originally filed against, all 5
attempts failing together is only about 1-in-30,000. This does not make a
`status: "match"` result on a `combine_devices`-eligible design provably
deterministic — it cannot be, for the reason above — but it cuts the
caller-visible flake rate enough that a single committed LVS report is once
again a practical signoff artifact. A `device.combine_incomplete` entry's
`description` field discloses the number of attempts that were made
(`"...after 5 attempts against independent netlist copies (issue #1185)..."`)
when more than one was tried.

#### `topology.flattened`: `options.flatten_reference`/`options.flatten_layout` collapsed a side's hierarchy before comparing

Only possible when `options.flatten_reference: true` and/or
`options.flatten_layout: true` (issue #1085), and only emitted for a side
whose circuit count actually changed — a netlist that already had only its
top circuit(s) (nothing to flatten) adds no entry.

`klt lvs` calls KLayout's own `klayout.db.Netlist.flatten()` on the opted-in
side, in-process, right after that netlist is read/resolved and before
`reference.top`/`layout.top` circuit selection runs — every subcircuit-call
instance is substituted in place, leaving only the netlist's top-level
circuit(s). This is the fix for the flat-vs-hierarchical seam `klt extract`'s
always-flat extraction creates: a hierarchical reference (one leaf `.subckt`
plus N instance calls of it) can never structurally match a flat layout-side
netlist without one side being flattened first — see `options.flatten_reference`
above for the full mechanism and rationale.

`severity` is always `"warning"` — flattening a side is a request-side
transform this command applies before the compare runs, not a
`NetlistComparer` finding, so it never changes `status` on its own. `side` is
`"layout"` or `"reference"`, matching which option fired. `description`
reports the before/after circuit count (e.g. "2 circuit(s) were collapsed
into 1 top-level circuit(s)"), so a caller can tell how much hierarchy was
actually removed. Present so a `"match"` reached after an opted-in flatten is
never silently indistinguishable from one reached against the netlist's
original hierarchy — the same transparency precedent
`device.parameter_tolerated`/`device.bulk_reconciled` establish for their own
opt-in normalisations.

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
  `severity: "error"`. Names the circuit in `circuit` (issue #1132) —
  `instance`/`subcircuit` stay `null` (there is no instance; the whole
  circuit definition has no counterpart).
- **Subcircuit mismatch** (`lvs.py:1143-1152`, from
  `logger.subcircuit_mismatches`) — a subcircuit instance has no
  counterpart. Always `"error"`. Names the containing circuit (`circuit`),
  the instance itself (`instance`), and the circuit it instantiates
  (`subcircuit`) (issue #1132), so a macro-scale report attributes *which*
  instance failed to pair without a side-channel netlist diff.
- **Device-class mismatch** (`lvs.py:1154-1192`, from
  `logger.device_class_mismatches`) — a device class (e.g. `nfet`)
  registered on one side has no counterpart class on the other side.
  Downgraded to `"warning"` when that side's netlist has zero actual
  instances of the class (`klt extract` always registers both
  `nfet`/`pfet` device classes even when a layout only instantiates one
  polarity, so an all-`nfet` layout compared against an all-`nfet`
  reference is not a real defect); `"error"` when the class has one or
  more real instances.
- **Ambiguous net pairing** (`lvs.py:2191-2235`, from
  `logger.ambiguous_net_matches`) — nets were paired ambiguously and the
  comparer resolved it structurally on its own (consider adding a
  `hints.same_nets` entry to pin the pairing down explicitly). Always
  `"warning"`; never changes `status`. **Issue #596:** when the paired net
  on *both* sides has exactly one device terminal (`Net.terminal_count() ==
  1`) and no declared pin (`Net.pin_count() == 0`), the `description` is
  distinct — it reads as a real connectivity finding ("there is no DC path
  through this node on either side... not a routine ambiguous-pairing/
  hints.same_nets nit") rather than the generic "resolved it structurally"
  wording, since a net with only one device terminal on each side is almost
  always an undriven device input reproduced identically on the layout and
  the reference (see `klt extract`'s "Single-device-terminal nets" for the
  layout-side detail, `docs/cli/extract.md`). Both cases stay
  `category: "topology"`, `severity: "warning"` — only `description`
  differs; a caller that needs to filter on this distinction programmatically
  should match the `description` text rather than `category`.
- **Net identity conflict with no leftover** (`lvs.py:1539-1549`, inside
  `_classify_net_mismatches`) — two nets were paired despite a name/identity
  conflict, and neither side has an accompanying one-sided leftover net
  (the merge/split case documented below, which absorbs the same
  underlying event when a leftover is present). Always `"error"`.

A one-sided subcircuit-instance mismatch (issue #1132) — the layout's `top`
circuit instantiates a `fill_1` cell the reference does not:

```json
{
  "category": "topology",
  "severity": "error",
  "description": "subcircuit instance could not be matched to a counterpart",
  "side": "layout",
  "net": null,
  "device": null,
  "property": null,
  "details": null,
  "circuit": { "layout": "top", "reference": null },
  "instance": { "layout": "Xfill_1_0", "reference": null },
  "subcircuit": { "layout": "sky130_fd_sc_hd__fill_1", "reference": null }
}
```

A seventh entry (`lvs.py:377`) is a safety net, not a classification site: if
`NetlistComparer.compare()` reports a mismatch but none of the sources above
produced any structured entry (a gap in this module's own event coverage,
not a clean run), `klt lvs` reports one generic `severity: "error"`,
`side: "both"` `topology` entry rather than silently reporting
`status: "match"` — see this module's docstring on the "`compare()` is
always authoritative" invariant. It is built through the same entry
constructor as every classification site above, so it carries the full field
set in the table — `net`/`device`/`property`/`details` and
`circuit`/`instance`/`subcircuit` all present and `null`, never omitted.

#### `hints.rejected`: a declared `hints.same_nets` pairing the comparer refused

Every `hints.same_nets` entry is passed to `NetlistComparer.same_nets(...,
must_match=True)` — a hard assertion, not a suggestion. If the comparer does
not end up confirming a declared pair as an actual net match, the caller's
assertion was refused, and `klt lvs` reports that refusal as one
`category: "hints.rejected"`, `severity: "error"`, `side: "both"` entry per
unhonored pair, `net: {"layout": <layout net name>, "reference": <reference
net name>}` naming both sides exactly as declared in the request. This is
detected structurally — after `compare()` runs, each declared pair is checked
against the comparer's own record of every net pairing it actually confirmed
— not by parsing `NetlistComparer`'s own log text (which is version-dependent
and, per this repo's own testing, sometimes empty; see the `description`
field's own contract above). A run with every declared `hints.same_nets` pair
honored produces zero `hints.rejected` entries. `hints.equivalent_pins` has no
comparable "rejected" outcome (it declares swappable pins, not an assertion
about a specific pairing) and never produces this category.

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

## `--check` / `--rerun`

A `klt lvs --format json` report is often committed as evidence alongside a
design (e.g. as a `klt signoff` manifest citation), but nothing previously
let a consumer *verify* that a committed report still reproduces against the
current layout/reference/deck/tool version without hand-rolling a
normalize-and-diff (issue #1106). `klt lvs --check <report.json>` closes
that gap:

```
klt lvs --check design.lvs.json                  # cheap mode (default)
klt lvs --check design.lvs.json --rerun           # full mode
```

`--check` is mutually exclusive with the positional `<request>` argument —
the layout/reference paths, engine, and (when used) extraction deck are all
read from `<report.json>` itself, not given again on the command line.

### Cheap mode (default)

Reconciles all three hashes an LVS report can carry (unlike `klt drc`'s
single input hash — see the `provenance`/`environment` fields
[`docs/json-contract.md`](../json-contract.md) documents): re-hashes
`layout`/`reference` (the paths exactly as the report echoes them back) via
`klayout_tools._provenance.sha256_file` and compares against
`environment.layout_sha256`/`environment.reference_sha256`; when the report
used an extraction deck (`provenance.deck` is non-`null`), also re-hashes
that deck's source and compares against `provenance.deck.content_hash`.
**No compare engine re-run.** Response shape:

```json
{
  "schema_version": 1,
  "mode": "check",
  "report": "design.lvs.json",
  "status": "match",
  "checks": [
    {
      "field": "environment.layout_sha256",
      "expected": "<hex>",
      "actual": "<hex>",
      "match": true
    },
    {
      "field": "environment.reference_sha256",
      "expected": "<hex>",
      "actual": "<hex>",
      "match": true
    }
  ]
}
```

(A `provenance.deck.content_hash` entry is appended to `checks[]` only when
the original run used an extraction deck.) `status` is `"match"` only when
every `checks[]` entry's `match` is `true` — a report with no recorded hash
to compare against always renders that check `match: false`, never a false
pass.

**Known limitation**: `layout`/`reference` are echoed exactly as given in
the original request document, which may have been relative to that request
*file's own directory* — not necessarily the current working directory (see
"Request" above). This re-hashes them relative to the current working
directory of the `--check` invocation, the same convention `klt drc
--check`'s `file` field uses; if the original request used request-file-
relative paths, invoke `--check` from that same directory, or commit
reports whose `layout`/`reference` are already absolute paths.

### Full mode (`--rerun`)

Best-effort re-run of the compare the committed report describes,
reconstructed from the fields the response actually echoes (`engine`,
`layout`/`reference`, each side's own top — `top` and `reference_top` — the
deck name/options under `provenance.deck`, and the whole `options` block),
then diffs the fresh report against the committed one field by field — same
volatile-field exclusions as `klt drc --rerun`
(`provenance.klt_version`/`klayout_version`/`pdk.version`; `pdk` is always
`null` for LVS, so only the first two ever apply in practice).
Response shape mirrors `klt drc --rerun`'s:

```json
{
  "schema_version": 1,
  "mode": "rerun",
  "report": "design.lvs.json",
  "status": "drifted",
  "drift": [
    { "field": "status", "committed": "match", "fresh": "mismatch" },
    { "field": "mismatch_count", "committed": 0, "fresh": 1 }
  ],
  "fresh": { "...": "the freshly produced klt lvs report, full shape" }
}
```

**Known limitations** (best-effort, not a byte-exact replay of the original
request): the pre-extracted-netlist-with-deck combo (`layout.netlist` +
`layout.deck`, issue #585) is indistinguishable from plain `layout.file`
inline extraction from the response alone — both populate `provenance.deck`
— so `--rerun` always reconstructs `layout.file`; in that specific combo it
fails loudly (a clean error, not a silent wrong answer) since the named path
is actually a SPICE netlist, not a layout stream. A non-default
`reference.form` (`"subckt-call"`), `reference.device_map`/`device_bulk`, and
`layout.top_cell_pins`/`declared_pins` are never echoed anywhere in the
response and are always reconstructed as each option's own default. Use cheap
mode instead when any of these apply to the original request.

A **report committed before issue #1205** added `reference_top` and the
`options` echo is reconstructed the way it always was: the single recorded
`top` is applied to both sides, and `options` is rebuilt from the top-level
`parameter_tolerance` alone. Both newly-added fields are excluded from the
drift diff for such a report (a field the committed report never carried
cannot itself have drifted), so re-running one is exactly as accurate — and
exactly as best-effort — as it was before. If that older report came from a
request with an asymmetric `reference.top` or any `options` entry, full mode
still cannot reconstruct it; use cheap mode, or re-commit the report with a
current `klt`.

### Exit codes for `--check` / `--rerun`

Both modes reuse the same 0/3 split a normal run uses: `0` when `status` is
`"match"`, `3` when it is `"drifted"` (see "Exit codes" below). A missing or
unparseable `<report.json>` exits `1` with a clean error message, same as
any other application-level failure.

## Exit codes

| Code | Meaning |
| ---- | ------- |
| `0` | LVS clean — layout matches reference (`status: "match"`). Under `--check`: the committed report still holds (`status: "match"`). |
| `1` | Failed to run — bad request, unresolvable layout/reference input, unknown `--deck`, unparseable reference netlist, unsupported engine, or an engine error. Under `--check`: a missing/unparseable committed report. |
| `2` | Usage error (missing argument, bad `--format` value, or combining `<request>` with `--check`) — from argparse. |
| `3` | Ran successfully; mismatches found (`status: "mismatch"`); the documented payload is on stdout. Under `--check`: drifted (`status: "drifted"`) — see "`--check` / `--rerun`" above. |

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
explicitly out of scope for this command. A `magic` extraction backend for
the `"netgen"` engine (issue #343) is likewise out of scope — that engine is
netlist-vs-netlist only (comparator/contract independence, not extraction
independence — see "Engine" -> `"netgen"` above).
