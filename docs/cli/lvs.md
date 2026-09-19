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
  — see `docs/cli/pdk.md`). This engine does not resolve a PDK on its own to
  find that file (`provenance.pdk` stays `null` unless `reference.form` is
  `"gate-level-verilog"` — see the `provenance` row further down), so
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

**This engine does not have the `"klayout"` engine's `counts` scope
mismatch (issue #1887).** For `"engine": "klayout"`, `counts.nets/pins`'
`layout`/`reference` are top-circuit-only while `matched` is hierarchy-wide,
so `matched` can exceed both (see the `counts` field description in
"Top-level fields" below). Here, by contrast, `layout`, `reference`, and
`matched` are all top-circuit-scoped by construction (`matched` is derived
from the exact-match/no-match verdict above, never a hierarchy-wide tally),
so that exceedance cannot occur for `"engine": "netgen"` — do not assume
the scope split documented for `"klayout"` also applies here.

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

- `reference.deck` — `"sky130"` / `"gf180mcu"` / `"sg13g2"` /
  `"sg13cmos5l"`, selects that deck's device map (and validates names
  against it). Its coverage is not a hand-maintained MOS list: every
  resistor and capacitor class the named deck's own `ExtractionDeck`
  declares is resolvable, so a deck recognises the same devices on this
  reference side that it recognises for extraction (issue #1464 — see
  "Per-deck coverage" below). Adding a new PDK family's device map
  (`_MOS_MODEL_TABLE`, `_KNOWN_PDK_FAMILIES`, `_PDK_VARIANT_FAMILY_ALIASES`
  in `klayout_tools.pdk_models`) is covered in
  [`../guides/pdk-family-port-checklist.md`](../guides/pdk-family-port-checklist.md).
- `reference.device_map` — an explicit `{ "<subckt-name>": <override> }`
  override, merged on top of the deck's map, for a device subcircuit name
  the curated table does not cover. `<override>` is either a bare
  device-class string (`"nfet"`/`"pfet"`, always a 4-terminal MOS `l`/`w`
  binding — the original shape, unchanged) or an object naming an explicit
  non-MOS (or MOS) binding, e.g. `{ "kind": "resistor", "class":
  "res_generic_po" }` (issue #1271) — see the `reference.device_map` field
  description below for the full object shape.

The converter recognises four device families through that same curated
table, never a second, independent device-name mapping — MOS, resistor,
capacitor, and bipolar (issue #1130 extended the original, MOS-only #280
converter). See `netlist_normalize.py`'s module docstring for the full
per-family rationale. It is deliberately narrow and loud (a wrong
parameter/unit mapping into a sign-off tool must never pass silently):

- **Detection.** An `X` card is a conversion candidate when its subcircuit
  name resolves in the curated table (any family, including bipolar — see
  below), *or*, for MOS/resistor/capacitor only, when it carries an
  `l`/`w`/`r_length`/`r_width`/`c_length`/`c_width`-style geometry parameter
  even though the name did not resolve (the original #280 MOS-only
  heuristic, now shared across those three families). Bipolar carries no
  distinguishing geometry parameter at all, so it is recognised **only** by
  a positive subcircuit-name match against the curated table — never by the
  geometry-parameter heuristic. Anything else (a genuine hierarchical
  subcircuit instance) passes through untouched. A device-like `X` card
  whose subcircuit name is not in the resolved device map is a hard error,
  never a silent pass-through.
- **MOS** converts to a plain `M` card. `L`/`W` are carried and converted to
  explicit micrometre-suffixed literals (`0.5u` → `L=0.5U`; SI metres
  `1.5e-6` → `W=1.5U`). A `.option scale` bare-micrometre convention is
  **not** inferred — emit explicit unit suffixes (see "Unit suffixes
  matter" below). Every other parameter is dropped: the parasitic-only
  `ad`/`as`/`pd`/`ps`/`nrd`/`nrs`/`sa`/`sb`/`sd` (which `klt extract` does
  not carry either) and any other model parameter. **A folded `nf>1` call
  is expanded, not rejected** (issue #1487): it converts to `nf` parallel
  unit-width plain `M` cards instead of one, named deterministically
  `<instance>_f0`, `<instance>_f1`, ... so device identity (and therefore
  LVS pairing) is stable across repeated conversions. `w` on the call site
  is the device's **total** (un-folded) width — the verified SPICE/BSIM
  convention (confirmed empirically against the installed sky130A ngspice
  model library: a diode-connected device measures the same drain current
  at `nf=1` and `nf=4` for the same `w`, and a quarter of that current at
  `w/4`) — so each expanded finger gets `w / nf`, matching the per-finger
  width a real drawn multi-finger layout extracts as. Pair this with
  `options.combine_devices` (see below) to reconcile the expanded fingers
  against a real layout's own folded fingers. `nf` must be a positive
  integer — a fractional or negative value is still a hard error, since
  there is no way to fold a fractional number of physical gate fingers.
- **Resistor and capacitor** convert to plain `R`/`C` cards the same way —
  their own length/width call-site parameters (`l`/`w` for sky130,
  `r_length`/`r_width` or `c_length`/`c_width` for gf180mcu) are carried
  onto explicit `L=`/`W=` (resistor) or a derived `A=`/`P=` plate
  area/perimeter (capacitor: `area = L*W`, `perimeter = 2*(L+W)` —
  elementary geometry, not a PDK-specific coefficient). Geometry is carried
  only when the call actually supplies it; the subcircuit's own default
  geometry applies otherwise. **The card's positional *value* token is a
  literal `0` placeholder** — `klt lvs` has no PDK sheet-resistance /
  capacitance-per-area table to compute a real resistance/capacitance from
  the call's geometry. That value is therefore excluded from the compare on
  both sides (otherwise it defeats `NetlistComparer`'s device pairing for the
  whole class, issue #1907) and disclosed as a `severity: "warning"`
  `device.placeholder_value` entry — see "`device.placeholder_value`" below.
- **Bipolar** converts to a plain `Q` card. It carries no length/width-style
  parameter at all (sky130's fixed-geometry `pnp_05v5` cells are selected
  purely by subcircuit name); its only real call-site parameter is an
  optional `mult`, carried onto the plain-element `Q` card's `NE`
  (KLayout's `DeviceClassBJT3Transistor` natively represents multiple
  parallel emitters via `NE`).
- `nf`/`m`/`mult` > 1 on a resistor/capacitor call, and `m`/`mult` > 1 on a
  MOS call (a multiplied device the curated plain-element form cannot
  represent) is **rejected** with a specific error naming the device —
  never silently dropped or misinterpreted. Flatten it (one device per
  drawn gate) in the schematic netlist first. Two exceptions: bipolar's
  `mult` is carried onto `NE`, not rejected, since
  `DeviceClassBJT3Transistor` can represent multiple parallel emitters that
  way; and MOS's own `nf` is *expanded*, not rejected (see above) — `m`/
  `mult` on a MOS call is a different, whole-device replication count (not
  finger-folding) and is still rejected exactly like resistor/capacitor's.

A reference netlist that *mixes* plain-element (`M`/`R`/`C`/`Q`) cards and
subckt-call `X` device cards converts correctly under `form:
"subckt-call"` — the plain-element cards pass through unchanged.

**Per-deck coverage** (issue #1464). What `reference.deck: "<deck>"` resolves
is the union of two sources:

| Source | Contributes | Example |
|---|---|---|
| Curated subcircuit-name tables in `klayout_tools.pdk_models` | Every MOS/resistor/capacitor/bipolar subcircuit name hand-verified against a real fetched PDK install, including the ones whose subcircuit name differs from the deck's device-class label | sky130's `sky130_fd_pr__cap_mim_m3_1` (class `sky130_fd_pr__model__cap_mim`), `sky130_fd_pr__pnp_05v5_W0p68L0p68` (class `pnp`), sg13g2's `cap_rfcmim` (class `rfcmim`, issue #1470) |
| The named deck's own `ExtractionDeck.resistors`/`.capacitors` | One assumed-identity binding per declared class — the class name taken as its own subcircuit name — but **only** for a family that has no curated table entry for that deck at all, and only useful where that identity assumption actually holds upstream | `sg13cmos5l`'s `rsil`/`rppd`/`rhigh` |

The second source exists so a deck cannot recognise a device for extraction
and then refuse to read that same device back here — the round-trip
asymmetry issue #1464 reported. A curated entry always wins over a derived
one, and a family the curated tables *do* cover is never extended by
derivation, so a class a curated entry deliberately omits (`sg13g2`'s
`res_metal1`/`res_metal2`, which the IHP PDK ships as a bare
model-reference `R` card rather than a subcircuit) is not fabricated.
Bipolar is never derived at all: it carries no geometry parameter to
cross-check a name against, so an assumed identity could silently rewrite a
genuine hierarchical instance into a `Q` card.

Anything outside that union — including a subcircuit whose real upstream
name differs from the deck's device-class label and has no curated entry yet
— still needs an explicit `reference.device_map` entry, and the
"not a known device for the requested deck" error lists the full resolved
set so the gap is visible from the failure itself.

**`sg13g2`'s `rfcmim` was the one live case of that gap** (issue #1470, now
closed). Derivation binds the *declared class* name as its own subcircuit
name, so it never reached `rfcmim`: IHP ships the device as `.subckt
cap_rfcmim`, not `.subckt rfcmim`. Derivation is deliberately not taught to
guess `cap_<class>`-style aliases — the identity assumption is the only one
that can be made without a hand-verified name, and a wrong guess here is a
silently wrong device binding. The gap is closed instead by a curated
`_CAPACITOR_MODEL_TABLE` entry for `("sg13g2", "sg13g2")`, verified against a
real fetched IHP-Open-PDK v0.3.0 install, mapping `rfcmim -> cap_rfcmim` (and
`cap_cmim -> cap_cmim`, since a curated entry for the pair takes the whole
family out of the derived fallback's reach — see the table above). Both of
`sg13g2`'s declared MIM capacitors now round-trip from `reference.deck`
alone, with no `device_map` override required:

```
XC1 PLUS MINUS cap_rfcmim w=5u l=5u
```

converts to `C1 PLUS MINUS 0 rfcmim A=25P P=20U` with no `reference.device_map`
entry at all.

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
convention `klt extract`'s `M`-card writer uses (`W=0.65U`). A reference
netlist authored without unit suffixes will parse with a `1e6`-scaled
device parameter that only ever mismatches or matches *relative to itself*
consistently, but produces a nonsensical absolute value in a
`device.property` mismatch's reported numbers. Always write reference
netlists with explicit unit suffixes on `W`/`L`.

This is a **reader-side** rule for `form: "plain-element"` (the default),
and is deliberately unaffected by issue #1396, which changed only what `klt
extract --pdk sky130*` writes onto a **simulation-form** `X` card (bare
micrometres, matching that vendor deck's own `.option scale=1.0u`). `klt
lvs`'s layout netlist is the unbound `M`-card form, which still carries
explicit suffixes.

**`form: "subckt-call"` resolves a bare literal per `reference.deck`
(issue #1492).** A real xschem/ngspice schematic-flow netlist for a deck
whose model library sets an ambient `.option scale=1.0u` (confirmed for
sky130 against a real fetched `open_pdks` install) writes a *bare*
`L=0.15`/`W=0.84` meaning already-micrometres, not SI metres — handing that
straight to `NetlistSpiceReader` (or reading it as `1.5e-1` SI metres) used
to silently multiply every device geometry by `1e6` with no diagnostic
pointing at units, indistinguishable from a genuine topology mismatch.
`normalize_reference_netlist` now resolves this per `reference.deck`'s
`klayout_tools.pdk_models.geometry_style_for_family` convention instead of
always assuming SI metres:

- `sky130` (`GEOMETRY_STYLE_BARE_UM`): a bare literal is read as
  already-micrometres, matching its real `.option scale=1.0u` convention.
- `gf180mcu`/`sg13g2`/`sg13cmos5l` (`GEOMETRY_STYLE_UNIT_SUFFIX`, unchanged):
  a bare literal is still SI metres — these decks' model libraries set no
  ambient scale.
- No `reference.deck` given: a bare literal is now a hard `NormalizeError`
  naming the ambiguous device/parameter/value, rather than a silent
  metres assumption — pass `reference.deck`, or write an explicit unit
  suffix or exponent literal (`L=0.5u` / `L=0.15e-6`), either of which is
  unambiguous and unaffected by `reference.deck` either way.

## Custom device classes round-tripped through an `X ... PARAMS:` card (issue #1942)

Every device class `klt extract` recognises through a native
`kdb.DeviceExtractor*` (MOS, drawn resistor, MIM capacitor, bipolar, diode)
writes with its own SPICE element letter (`M`/`R`/`C`/`Q`/`D`). One device
family — `sg13g2`'s `cap_cmomi`/`cap_cmomf` MoM (Metal-oxide-Metal)
capacitors, recognised through a custom `kdb.GenericDeviceExtractor` (issue
#1466) — has no native element letter, so `kdb.NetlistSpiceWriter` writes it
as an ordinary subcircuit-call `X` card instead:

```
XD_$1 A B cap_cmomi PARAMS: W=4 L=10
```

Reading that card back through a plain `kdb.NetlistSpiceReader()` (no
delegate) does not error: an `X` card naming an undefined subcircuit
synthesises an *abstract circuit* whose parameters are baked into its own
mangled name (`CAP_CMOMI(L=10,W=4)`), and the device is then compared by
that circuit-name string, never as a device — invisible in `counts.devices`,
unreachable by `options.parameter_tolerance`, and any real mismatch degrades
to a generic `topology`/`circuit could not be matched to a counterpart`
finding with no device/parameter/net name.

**`layout.deck`/`reference.deck` fixes this.** When a pre-extracted
`layout.netlist` gives `layout.deck`, or `reference.netlist` gives
`reference.deck` (any `reference.form`), `klt lvs` recognises an `X` card
naming one of that deck's own custom device classes (today: its
`mom_capacitors` entries) and creates a real device of that class instead —
the identical `kdb.DeviceClass` shape (`A`/`B` terminals, declared
equivalent, `W`/`L` parameters) the layout-extraction side itself registers,
built from one shared function (`klayout_tools.extract
.mom_capacitor_device_class`) so both sides cannot drift apart. The device
then participates in `klt lvs`'s ordinary device-level compare exactly like
an `M`/`R`/`C`/`D` card already does:

- **Device census.** `counts.devices.layout`/`.reference`/`.matched` counts
  it like any other device — no longer silently absent.
- **`options.parameter_tolerance` reaches it.** A small `W`/`L` delta within
  tolerance absorbs (`status: "match"`, a `device.parameter_tolerated`
  entry) instead of hard-failing on circuit-name string inequality.
- **A real mismatch is actionable.** A parameter/connectivity difference
  reports the ordinary `device.property`/`device.unmatched` entry, naming
  the device instance and its class (`cap_cmomi`), instead of a generic,
  un-named `topology` finding.

A hand- or tool-generated reference netlist for this family must emit the
identical `X <name> <net> <net> cap_cmomi PARAMS: W=<value> L=<value>` card
shape `klt extract`'s own writer produces (see the example above) — there is
no plain-element card form for this device family to convert to instead.

**Residual gap: no deck given.** `layout.deck`/`reference.deck` are already
required for `layout.file` (inline extraction) and commonly given for
`reference.netlist` (`form: "subckt-call"`'s own device-name resolution,
"Netlist form" above), but both are optional for a pre-extracted
`layout.netlist`, and `reference.deck` is not required for `form:
"plain-element"`/`"gate-level-verilog"`. Omitting the relevant side's `deck`
on a netlist that round-trips a custom device class leaves this recognition
unable to run — the pre-#1942 mangled-abstract-circuit degradation described
above still applies, silently, on that side. There is no separate
diagnostic for this today; give `layout.deck`/`reference.deck` whenever a
pre-extracted netlist may contain a custom device class.

## Digital gate-level LVS: `reference.form = "gate-level-verilog"` (issue #1336)

`klt place-and-route`'s `verilog_path` (issue #996) writes the as-built,
gate-level Verilog netlist of a routed digital design — module
instantiations of standard-cell library types, port-connected by name. `klt
lvs` compares SPICE netlists only, so nothing in `klt` could turn that
Verilog into a comparable reference: `klt extract --abstract-cells` (issue
#620) gives the *layout* side of a digital gate-level LVS a black-box SPICE
netlist, but the reference side had no code path at all. Setting
`reference.form` to `"gate-level-verilog"` closes that gap: `klt lvs` reads
`reference.netlist` as Verilog instead of SPICE and converts it to
plain-element-shaped SPICE first, exactly like `"subckt-call"` converts a
schematic flow's simulation-form SPICE.

**The layout side must be abstracted too.** Both sides of this compare
describe a standard cell as a pin-only black box, so the layout netlist has
to come from `klt extract --abstract-cells`, not a default extraction — a
default extraction recognises the real transistors inside every cell and
would compare thousands of real devices against device-less stubs, which
can never be clean. The full worked sequence, on a `klt place-and-route`
result (this is exactly what `scripts/place-and-route-smoke.sh` runs per
design, and what was measured live for `gcd` in issue #1336):

```bash
# 1. place-and-route -> routed GDS (gds_path) + as-built netlist (verilog_path)
klt place-and-route par_request.json --format json >par.json

# 2. layout side: every standard cell a pin-only black box, with the
#    design's own DEF net names recovered so top-level pin names line up.
#    Abstract every instantiated cell, filler/tap included --
#    `sky130_fd_sc_hd__fill_*` (issue #1442's row-rail fix inserts these
#    unconditionally, even without `request.power`, to close standard-cell
#    row gaps) and `sky130_fd_sc_hd__tapvpwrvgnd_1` (the `tapcell` stage's
#    own unconditional insertion) carry no logical function, so
#    `write_verilog` (the reference side) correctly never emits them --
#    `klt lvs` recognizes a power-only cell (every declared pin is a
#    power/ground pin of `reference.library`, derived from that library's
#    own .subckt data) as out of scope for this signal-only compare and
#    prunes it before comparing (issue #1622; see
#    "`topology.power_only_pruned`" below). No cell-name glob needed.
klt extract "$(jq -r .gds_path par.json)" --deck sky130 \
  --abstract-cells 'sky130_fd_sc_hd__*' --def-net-names \
  -o gcd.gate.spice --format json >extract.json

# 3. compare against the same run's own verilog_path
klt lvs lvs_request.json --format json
```

```json
{
  "layout": { "netlist": "gcd.gate.spice", "top": "gcd" },
  "reference": {
    "netlist": ".klt/place-and-route/gcd.v",
    "top": "gcd",
    "form": "gate-level-verilog",
    "library": "sky130_fd_sc_hd"
  }
}
```

For `gf180mcu`, the only changes are the deck and the library — `--deck
gf180mcu --abstract-cells 'gf180mcu_fd_sc_mcu9t5v0__*'` on the layout side
and `"library": "gf180mcu_fd_sc_mcu9t5v0"` on the reference side. Nothing
else in the request differs: the pin names and pin order that library uses
(`I`/`ZN` and signals-before-supplies, versus sky130's `A`/`Y` and
alphabetical interleaving) are read out of its own `.spice`/`.cdl` file.

`reference.library` (required for this form) names the standard-cell
library (e.g. `"sky130_fd_sc_hd"`, `"gf180mcu_fd_sc_mcu9t5v0"`) whose real
`libs.ref/<library>/spice/<library>.spice` (falling back to
`.../cdl/<library>.cdl` when no `spice/` asset exists — both are byte-
identical `.SUBCKT` headers on a PDK that ships both, e.g. gf180mcu)
resolves each instantiated cell type's real pin order — via the same
`libs_ref` asset `klt pdk`/`klt place-and-route` already resolve, never a
second, hardcoded pin-order table. `reference.pdk`/`reference.pdk_root`
select the PDK install exactly like `klt extract`'s own `--pdk`/`--pdk-root`
flags (omit both to fall back to `klt pdk find`'s usual `$PDK_ROOT`/store
resolution). A cell type the resolved library does not define is an
application error (exit 1) naming the missing cell — never a silent skip.

**Each standard-cell instance becomes a black-box subcircuit call on both
sides.** The conversion writes `X<inst> <nets...> <cell_type>` for every
instance, with a pin-only `.SUBCKT <cell_type> ... .ENDS` stub for every
distinct instantiated cell type — the same shape `klt extract
--abstract-cells` already writes for the layout side (issue #620), so both
sides describe a standard cell as a pin-only black box rather than a real-
devices-vs-black-box mismatch that could never be clean.

**No power/ground pins — this compare is signal-connectivity only.**
`docs/cli/place-and-route.md`'s "As-built netlist" section documents that
`verilog_path` is written without `-include_pwr_gnd`, so it never carries
`VPWR`/`VGND`/well-tie connections — there is nothing in the Verilog to
recover them from, and this converter deliberately does not invent them.
Each converted stub's declared pin list is therefore the real library pin
order's *signal-only* subset.

A layout side that *does* carry power pins (which `klt extract
--abstract-cells` normally will, since sky130's and gf180mcu's own cells
draw in-cell `VPWR`/`VGND`/well-tie labels) still compares **cleanly**:
KLayout's comparer matches the black-box cell circuits on their common,
name-matched signal pins and tolerates the layout's extra pins and extra
power nets — measured on the real routed `gcd` sequence above (460
abstracted, non-filler `sky130_fd_sc_hd` instances, re-measured against
the fixture #1443 regenerated with #1442's row-rail fix applied): `status:
"match"`, `mismatch_count: 0`, with no `pin.unmatched` entry. You
do **not** need to narrow the layout-side pin resolution to signal pins to
get a verdict.

**A layout-side cell with *only* power/ground pins is pruned automatically**
(issue #1622) — a filler cell (`sky130_fd_sc_hd__fill_*`, inserted
unconditionally whenever issue #1442's row-rail fallback fires) or a tap
cell (`sky130_fd_sc_hd__tapvpwrvgnd_1`, inserted unconditionally by the
`tapcell` stage) carries no logic function, so `write_verilog` never
instantiates it on the reference side — there is nothing for a signal-only
compare to describe. `klt lvs` recognizes this structurally — every one of
the cell's declared pins is a pin `reference.library` declares for a cell
the reference instantiates but the Verilog never carries, i.e. a
power/ground pin, derived from the library's own `.subckt` data rather than
a cell-name glob like `fill_*`/`tap*` or a hardcoded PDK power-pin table —
and removes the layout-side instance before
comparing, disclosing what it removed as a `severity: "warning"`,
`category: "topology.power_only_pruned"` entry — see
"`topology.power_only_pruned`" below — rather than reporting a false
`topology` mismatch. You do not need to exclude these cells from
`--abstract-cells` yourself.

**A reference port declared only via `assign` is joined onto its target's
net automatically** (issue #2021) — gate-level Verilog routinely carries a
port-to-port alias like `assign dbg_uart_byte[i] = rx_byte[i];` (e.g. a
debug/monitor tap port for a "real" signal port), and the conversion above
resolves an `assign` alias for every *instance* connection but never for a
module's own declared port list, so the aliased port would otherwise read
back as its own isolated, disconnected reference net even though the layout
has exactly one physical net for both names. `klt lvs` joins the alias
port's net onto its canonical target's net before comparing, disclosing
what it joined as a `severity: "warning"`, `category:
"topology.reference_port_alias_joined"` entry — see
"`topology.reference_port_alias_joined`" below — rather than reporting a
false `pin.unmatched`/`net.unmatched` mismatch. A genuinely unconnected or
differently-wired reference port is untouched and still reports as a real
mismatch.

What that costs is real and must not be misread: **a power-net defect is
invisible to `status`**. A standard cell whose `VGND` pin is wired to
the power rail in the layout still reports `status: "match"`, because the
reference has no power connectivity to contradict it (verified directly —
see `tests/test_lvs.py`'s
`test_run_lvs_gate_level_verilog_power_miswire_leaves_signal_status_match`).
A gate-level-Verilog reference proves the routed layout implements the
netlist's *signal* connectivity, and `status` says exactly that and nothing
more.

**Since issue #1952, that is no longer the whole report.** The power/ground
half is checked separately and reported in its own `power_connectivity`
block — the same miswire above is caught there, while `status` stays
`"match"` (see `test_run_lvs_gate_level_verilog_power_miswire_is_detected`).
Read "Power/ground connectivity" below before citing a gate-level LVS report
as evidence: **a caller wanting full LVS on a digital block gates on both
`status` and `power_connectivity.status`.** Power-*grid* correctness in the
physical sense — rail continuity, IR drop, electromigration — is still a
separate check (`klt power`'s IR-drop/EM analysis, `klt ring-check`'s
annulus assertion, `klt drc`'s deck rules), and neither this form nor that
block claims to establish it.

**Deliberately narrow, deliberately loud**, mirroring `"subckt-call"`'s own
discipline: only the constructs a flattened, technology-mapped netlist
actually needs are supported — `module`/`endmodule`, `input`/`output`/
`inout` port declarations (with an optional `[msb:lsb]` bus range, expanded
to one boundary pin per bit), named (`.PORT(NET)`) instance connections
only (a plain net, a single-index bit-select `bus[3]`, a `1'b0`/`1'b1`
constant, or `.PORT()` for an explicit no-connect), and a simple `assign
<net> = <net>;` alias. A Verilog **escaped identifier** (`\name`) is
accepted wherever a name is, and is read exactly as Verilog defines it —
one atomic token running from the backslash to the next whitespace, so
every character in between belongs to the literal name. A backend-emitted
name that embeds a flattened `generate`/`genvar` hierarchy path, e.g.
`\my_array[0].u_inst/_01_`, is therefore the net named
`my_array[0].u_inst/_01_`, never re-read as a bit-select of a bus
`my_array` (issue #1371). A genuine bit-select of an escaped net is
written the way Verilog requires, with the terminating whitespace explicit:
`\bus [3]`. A hierarchical module (uncommon for `verilog_path`,
which is always a single flat module post-place-and-route, but not assumed
away) converts correctly too — an instantiated cell type that matches
another parsed `module` in the same file is treated as a genuine
subcircuit reference, never confused with a library cell of the same name.
Anything else — positional (non-named) instance connections, concatenation,
a multi-bit range-slice connection, a general expression, `always`/`case`/
other behavioral statements, an ANSI-style inline port declaration — is an
application error (exit 1) naming the offending construct, never a silent
best-effort guess.

## Power/ground connectivity (issue #1952)

The section above is the *signal* half of a digital compare. This is the
other half: **`power_connectivity`**, a report block that verifies, per
abstracted standard-cell instance, that every pin the PDK library declares
as power/ground actually lands on the net it should.

The full design record — why this lives on `klt lvs` rather than `klt erc`
or a new `klt pg-check` verb, what was measured about each option, and the
contract decisions behind the shape below — is
[`docs/design/pg-connectivity-check-decision.md`](../design/pg-connectivity-check-decision.md).

### What it checks, and what it does not

**In scope: per-cell-instance pin-to-net verification.** For every
subcircuit instance in the layout netlist, every power/ground pin its master
declares must reach the net it is supposed to reach.

**Out of scope: rail/grid continuity.** Whether the `VPWR` rail is
geometrically unbroken across the die is a different question, answered
elsewhere — `klt ring-check` (is this guard/tap ring a single closed
annulus?) and `klt power` (does the grid carry the current without
excessive droop?). It needs the routed *geometry*, which `klt lvs` never
sees: by the time this compare runs, the layout side is already abstracted
to black-box cells.

In practice the pin-to-net question catches most of what people mean by "the
power grid doesn't connect what it should", because the extraction probes
each pin against the actually-routed conductor: a broken rail segment, a
filler cell shorting a rail onto a signal net (issue #1442), or a via-less
tie all surface as *some instance's power pin reaching a different net than
its peers'*.

### Why it is checkable when the reference is signal-only

The reference Verilog carries no power connectivity, so there is nothing to
compare *against*. The check does not need one. It needs:

- **which pins are power/ground** — derived from the resolved standard-cell
  library's own `.subckt` pin orders (the same structural derivation issue
  #1622's power-only pruning already uses: the library's full pin order
  minus the reference's signal-pin universe). No hardcoded per-PDK power-pin
  table, no `fill_*`/`tap*` cell-name glob.
- **what each instance's power pins are connected to** — the layout netlist
  already carries it. `klt extract --abstract-cells` resolves and probes
  *every* declared pin, power pins included; the signal-only compare simply
  ignores the power ones.

…plus one **invariant** the layout must satisfy on its own:

> In a single-power-domain block (what `klt place-and-route` produces),
> every instance's same-named supply pin must reach the same net.

When the layout SPICE comes from `klt extract --abstract-cells`, extraction
preserves the original instance-transformed well polygons as conductors.
Abutted cells' well/body pins therefore retain their real continuity; actual
well gaps, wrong well ties, and metal-supply disconnects remain detectable.
This is resolved in the extracted connectivity, with no pin-name exception
in LVS. Re-extract older standalone SPICE artifacts that lost well geometry:
without their layout, LVS cannot distinguish that loss from a real defect.
See [cell abstraction](extract.md#cell-level-black-box--pins-abstraction---abstract-cells-issue-620).

### The three finding rules

| `findings[].rule` | Fires when |
| --- | --- |
| `power.inconsistent_pin_net` | One power/ground pin name reaches **more than one distinct net** across the design's instances — the single-power-domain invariant, violated. One finding per offending *pin name*, naming every net and the instances on each. It deliberately does **not** nominate which net is correct: with two instances disagreeing there is no majority to appeal to. Declare `options.power_connectivity.expected_nets` to get a verdict that does. **This same signature — every instance's supply pin landing on its own distinct net — is also exactly what a layout with no power distribution network routed at all produces** (issue #1978's first-tester report); the finding's `description` names `request.power` in `klt place-and-route` as the more likely fix for that case, alongside `expected_nets`/`power_connectivity: false`. |
| `power.unexpected_pin_net` | `options.power_connectivity.expected_nets` names this pin and at least one instance reaches a **different net than declared**. Strictly stronger than the rule above — it also catches a design in which *every* instance is miswired the same way, which no amount of cross-instance agreement can. Replaces the consistency rule for any pin the mapping names. As with the rule above, the finding's `description` also names a missing `request.power` block as a possible cause, alongside re-checking `expected_nets` itself. |
| `power.unconnected_pin` | An instance's power/ground pin resolved to **no net at all** — it never landed on routed conductor. Reported separately because the defect differs in kind: not "wired to the wrong rail" but "wired to nothing". |

Every finding is `severity: "error"`. These are `findings[].rule` values on a
new field, **not** `mismatches[].category` values — `category_counts` and
`category_error_counts` are untouched, and every existing consumer keyed on
them sees no change.

**Note what is deliberately *not* a rule: two distinct power pins of one
instance landing on the same net.** It looks like an obvious short, and it
is not — sky130's `VPWR`/`VPB` legitimately share a net, as do `VGND`/`VNB`,
so such a rule would fire on every correctly-wired cell in the PDK.

### Checked before filler/tap pruning, not after

Issue #1622's `_prune_power_only_layout_circuits` removes every layout-side
circuit whose entire pin list is power/ground — fillers, taps, decaps —
because a signal-only reference never instantiates them (see
"`topology.power_only_pruned`" below). This check runs **before** that
prune, deliberately: those cells are precisely the ones whose power
connectivity is the *only* thing about them any check could ever verify, and
issue #1442's unconditional filler placement shorting `VPWR`/`VGND` onto
signal nets is exactly that defect class. A check running after the prune
would silently exempt them.

### Report shape

```json
"power_connectivity": {
  "status": "mismatch",
  "reason": null,
  "power_pins": ["VGND", "VNB", "VPB", "VPWR"],
  "instance_count": 462,
  "expected_nets": null,
  "unchecked_expected_pins": [],
  "findings": [
    {
      "rule": "power.inconsistent_pin_net",
      "severity": "error",
      "pin": "VGND",
      "expected_net": null,
      "description": "standard-cell power/ground pin 'VGND' reaches 2 distinct nets across 462 instance(s) -- ...",
      "instance_count": 462,
      "nets": [
        {
          "net": "VGND",
          "instance_count": 461,
          "instances": [{"circuit": "TOP", "instance": "1", "cell": "SKY130_FD_SC_HD__INV_1"}],
          "instances_truncated": true
        },
        {
          "net": "VPWR",
          "instance_count": 1,
          "instances": [{"circuit": "TOP", "instance": "417", "cell": "SKY130_FD_SC_HD__BUF_1"}],
          "instances_truncated": false
        }
      ]
    }
  ],
  "finding_count": 1
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `status` | `"match"` \| `"mismatch"` \| `"unchecked"` | The power/ground verdict, **independent of the report's top-level `status`** (which stays exactly `NetlistComparer.compare()`'s own signal-connectivity result). `"match"` when the check ran and found nothing; `"mismatch"` when it produced findings; `"unchecked"` when it did not run — never a clean verdict on absent evidence. |
| `reason` | string \| `null` | Why the check did not run, for `status: "unchecked"`; `null` otherwise. One of: a `reference.form` other than `"gate-level-verilog"` (that form's reference carries its own power pins and nets, which the ordinary compare already checks); `options.power_connectivity: false`; no power-pin universe derivable from the reference library's pin-order data; or a layout netlist whose instances declare none of those pins (e.g. an `--abstract-cell-lef` that never declared PG pins). |
| `power_pins` | array\<string\> | The power/ground pin names actually found on layout-side instances, upper-cased and sorted — what was checked, not what the library declares. `[]` when `status` is `"unchecked"`. |
| `instance_count` | integer | How many distinct layout-side instances carried at least one of those pins. `0` when `status` is `"unchecked"`. |
| `expected_nets` | object\<string, string\> \| `null` | The resolved `options.power_connectivity.expected_nets` mapping (upper-cased, key-sorted), or `null` when none was declared. |
| `unchecked_expected_pins` | array\<string\> | Issue #1978. `expected_nets` keys that named a power/ground pin no layout-side instance was actually observed to carry — a typo, or a PDK standard cell whose tie pin has no in-cell label/LEF port at all. Such a pin matches zero rows in `_power_pin_connections` and so produces zero findings, which reads identically to "checked and found correct" unless this field is consulted; a non-empty list means at least one declared expectation was never exercised by this run. `[]` on a clean check, and always `[]` when `status` is `"unchecked"`. |
| `findings` | array\<object\> | One entry per offending **pin name** (never per instance) — see the rule table above. `[]` on a clean check. |
| `finding_count` | integer | `len(findings)`. |
| `findings[].nets[]` | array\<object\> | Per finding, the nets that pin reached, each with the exact untruncated `instance_count` and a bounded `instances` sample (at most 10 `{circuit, instance, cell}` entries, with `instances_truncated` saying whether anything was left out). A real routed block puts hundreds of instances on one rail; a finding that dumped all of them would bury the few that differ. A `net` of `null` is an unconnected pin. |

### Recommended usage

Run the default (consistency-only) check on any gate-level compare — it
costs nothing and catches the asymmetric miswires. For a block being taken
to signoff, additionally declare `expected_nets`, which is the only form
that catches a *uniformly* miswired design:

```json
"options": {
  "power_connectivity": {
    "expected_nets": {
      "VPWR": "VPWR", "VGND": "VGND", "VPB": "VPWR", "VNB": "VGND"
    }
  }
}
```

A genuinely multi-domain design — one that routes the same pin name to two
different nets by design — should set `"power_connectivity": false` and say
so; the response records that opt-out in `reason` rather than reporting an
empty result that reads like a clean one.

**Check `unchecked_expected_pins` alongside `findings`, not just
`status`.** Declaring `expected_nets` for a pin name and then reading
`status: "match"` is not, on its own, proof that pin was verified — if the
layout never actually carried an instance with that pin (see the field
table above), `status` is `"match"` because there was nothing to disagree
with, not because the pin was checked and found correct. A signoff gate
that wants "every pin I named was actually exercised" should additionally
assert `power_connectivity.unchecked_expected_pins == []`.

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

- **Clean self-compare.** Extracting the macro's own netlist (**4857
  devices**, 2048 nets, 541 pins across the flattened layout — re-measured
  against the fixture #1443 regenerated with #1442's row-rail fix applied;
  see `tests/test_power.py`'s docstring on the same fixture for why the
  exact counts shifted) and comparing the layout against it reports
  `status: "match"`, with every device accounted for on both sides
  (`counts.devices` layout = reference = matched = 4857 — no silently-
  dropped devices at this scale). The only mismatches are the same
  tap-coverage **warnings** the hand-drawn corpus round-trip already carries:
  one `device.body_unverified` (the synthetic-substrate net every NMOS body
  lands on, since sky130 draws no distinct NMOS tap layer — see
  [`docs/cli/extract.md`](extract.md), "Coverage"; no PMOS counterpart, since
  sky130's `well_label` names every PMOS body here) plus ambiguous-net and
  unused-device-class `topology` warnings. None are `error` severity.
- **Deliberately-broken variant.** Corrupting exactly one standard-cell-
  region transistor's drawn width in the reference netlist (`W=0.42U` →
  `W=1.0U` on a single device line, a pure `device.property` change leaving
  all connectivity intact) is caught: `status: "mismatch"`, exit 3, a single
  `device.property` entry (`severity: "error"`, `class: "nfet"`,
  `description`: "matched device parameter 'w_um' differs") naming that exact
  instance — correctly attributed to the one changed device, not smeared
  across the ~4800 unchanged standard-cell devices.

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

## Composing macros with opposing `combine_devices` needs (issue #1552)

`klt gen-compose`-ing several already-independently-verified macros into one
top-level design, then running a single top-level `klt lvs` pass, can hit a
gap the whole-request `options.combine_devices` boolean cannot close: two
macros with genuinely opposing `combine_devices` needs, each already reached
at their own scope. `options.combine_devices_per_circuit` closes it by
letting each macro keep its own already-verified setting once composed.

Two macros, each its own `.subckt`, sharing a top-level `VPWR`/`VGND` rail:

- `macroa`'s layout draws its NMOS as two parallel W=0.325µm fingers (a
  common matching/drive-strength construction); its reference declares one
  lumped W=0.65µm device. It needs `combine_devices: true` to re-lump the
  fingers before comparing.
- `macrob`'s layout draws the *same-looking* two parallel W=0.325µm fingers,
  but its own reference deliberately keeps them as two separate
  device-for-device cards — the workaround for issue #1497's silent
  parameter-corruption risk in `Netlist.combine_devices()` at scale (a large
  binary-weighted unit-capacitor array, in the original report; two devices
  is enough to reproduce the shape here). It needs `combine_devices: false`
  so nothing folds it.

Composed under a shared rail and flattened for comparison (mirroring `klt
extract`'s always-flat layout output — see `options.flatten_layout`/
`options.flatten_reference` above), **neither** whole-request setting gets
both macros right at once:

- `combine_devices: false` never folds `macroa`'s fingers, so it mismatches
  against `macroa`'s lumped reference (`device.unmatched`).
- `combine_devices: true` folds `macroa` correctly — but `Netlist.
  combine_devices()` is a whole-netlist operation, applied symmetrically to
  *both* the layout and reference netlists, so it also folds `macrob`'s
  identical-looking fingers on **both** sides. The compare still reports
  `status: "match"` (both sides were folded the same way), but it has
  silently collapsed `macrob`'s own already-verified device-for-device
  reference down to one lumped device too — visible only as `counts.devices`
  dropping below the honest per-macro total, not as any mismatch entry.

`options.combine_devices_per_circuit` gives each macro its own setting,
applied via `Circuit.combine_devices()` before either side is flattened, so
`macrob`'s circuit boundary is never crossed by `macroa`'s combine choice
even after the top-level rail connects them:

```json
{
  "layout": { "netlist": "composed.layout.spice", "top": "top" },
  "reference": { "netlist": "composed.reference.spice", "top": "top" },
  "options": {
    "combine_devices_per_circuit": { "MACROA": true, "MACROB": false },
    "flatten_layout": true,
    "flatten_reference": true
  }
}
```

This reports `status: "match"` with the honest device count (`macroa`'s
folded 2 plus `macrob`'s real, uncombined 3 — 5 total, not `combine_devices:
true`'s silently-corrupted 4), plus the two `topology.flattened` disclosures
from the flatten step. Note the upper-case `"MACROA"`/`"MACROB"` glob keys —
`NetlistSpiceReader` upper-cases circuit names read back from SPICE, so a
pattern written in the source `.subckt`'s own lower-case spelling would
silently match nothing (surfaced as a `combine_devices_per_circuit.unmatched`
warning, not an error — see below). The full runnable fixture (both SPICE
netlists and every request variant above) lives in `tests/test_lvs.py`'s
`test_combine_devices_per_circuit_satisfies_both_composed_macros_simultaneously`
and its neighboring tests.

**This worked example's `layout.netlist` is hand-written, not `klt
extract`-produced — that distinction matters.** `combine_devices_per_circuit`
only has something to scope against on a side that already has separate
circuits to match its glob patterns against, which is true of `macroa`/
`macrob` above only because `composed.layout.spice` was authored directly
with two `.subckt`s. Run the *same* top-level `klt gen-compose` + `klt lvs`
workflow against a layout side produced by `klt extract` instead — the
`layout.file` shape, or a `layout.netlist` that `klt extract` itself wrote —
and `combine_devices_per_circuit` is a no-op there: `klt extract` has no
hierarchical extraction mode (issue #1085 was closed without adding one), so
it always writes one flat `.SUBCKT` for the whole composed GDS, with no
per-macro boundary left for this option to scope on that side. See the
`options.combine_devices_per_circuit` field description below for the full
explanation and the currently-working alternative (issue #1552's option 3:
`klt extract --abstract-cells` plus a matching hand-authored reference).

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
| `reference.form` | string | `"plain-element"` (default), `"subckt-call"`, or `"gate-level-verilog"` (issue #1336). `"plain-element"` reads the reference as the schematic-equivalent form `klt lvs` requires, and detects/errors on a misfiled simulation-form netlist. `"subckt-call"` converts a PDK schematic flow's simulation-form netlist to the plain-element form first — see "Netlist form" above. `"gate-level-verilog"` reads `reference.netlist` as a `klt place-and-route` `verilog_path` gate-level Verilog netlist instead of SPICE and converts it to plain-element-shaped SPICE first — see "Digital gate-level LVS" above. |
| `reference.deck` | string | Only used with `form: "subckt-call"`. `"sky130"`/`"gf180mcu"`/`"sg13g2"`/`"sg13cmos5l"` — selects that deck's device-name map for the conversion (and validates device names against it). That map is the curated subcircuit-name tables plus, for a resistor/capacitor family those tables do not cover for this deck, one assumed-identity binding per class the deck's own `ExtractionDeck` declares (issue #1464) — see "Per-deck coverage" above. Omit to auto-resolve each device subcircuit name against the whole curated table (curated names only — the per-deck derived bindings are not part of that cross-deck table, so a deck-declared class it does not cover needs `reference.deck` or `reference.device_map`). |
| `reference.device_map` | object\<string, string \| object\> | Only used with `form: "subckt-call"`. Explicit `{ "<subckt-name>": <override> }` overrides, merged on top of `reference.deck`'s map — for a device subcircuit name the curated table does not cover. Two `<override>` shapes (issue #1271): a bare string (`"<nfet\|pfet>"`, e.g. `"nfet"`) always means a 4-terminal MOS (`d g s b`) `l`/`w` binding — the original shape, unchanged, so every existing caller's `device_map` keeps working exactly as before. An object `{ "kind": "mos"\|"resistor"\|"capacitor"\|"bipolar", "class": "<device-class>", "length_param": "<param>", "width_param": "<param>" }` opts into an explicit non-MOS (or MOS) binding: `kind` and `class` (the plain-element device-class label, e.g. `"res_generic_po"`) are required; `length_param`/`width_param` (the real subcircuit's own call-site geometry parameter spellings, e.g. gf180mcu's `"r_length"`/`"r_width"`) default to `"l"`/`"w"` and are ignored for `kind: "bipolar"` (no geometry call-site parameter at all) and `kind: "mos"` (the MOS conversion always reads the literal `l`/`w` parameter keys). A malformed object entry (an unrecognised `kind`, a missing/empty `class`, or an empty `length_param`/`width_param`) is an application error (exit 1) naming the offending entry, never a silent fallback. A bare-string entry naming a subcircuit that is not actually MOS-shaped (wrong terminal count) still fails with the original `device_map only supports MOS-shaped ...` error — pass the object form naming the real `kind` instead, or `reference.deck` when the device is one of a registered deck's curated classes. |
| `reference.library` | string | Required with `form: "gate-level-verilog"` (issue #1336); ignored otherwise. Names the standard-cell library (e.g. `"sky130_fd_sc_hd"`, `"gf180mcu_fd_sc_mcu9t5v0"`) whose real `libs.ref/<library>/spice/<library>.spice` (falling back to `.../cdl/<library>.cdl`) file resolves each instantiated cell type's pin order. Omitting it is an application error (exit 1). |
| `reference.pdk` / `reference.pdk_root` | string | Only used with `form: "gate-level-verilog"`. Forwarded verbatim to `klt pdk`'s resolver (`find_pdk(variant=reference.pdk, root=reference.pdk_root)`) exactly like `klt extract`'s own `--pdk`/`--pdk-root` flags — see `docs/cli/pdk.md`. Omit both to fall back to `$PDK_ROOT`/the ciel/volare store search. An unresolvable PDK, or a resolved variant with no `libs.ref` asset, is an application error (exit 1). |
| `reference.device_bulk` | object\<string, string\> | Optional `{ "<device-class / model name>": "<reference net name>" }` — declares that the reference netlist's device class of that name carries an *implicit* bulk/well/collector terminal on the named net, which the layout side's same-named class declares explicitly (issue #506). `klt lvs` adds that one terminal to the reference class and ties it to the named net on every reference-side instance before `NetlistComparer.compare()` runs, so a deck's bulk-terminal device flavour can match a schematic reference that does not model the terminal at all — the reconciliation `device.class_arity` only diagnoses. The net is looked up on each circuit that instantiates the class (matched exactly, then case-insensitively) and **created** there when the reference does not model that node; to bind the added terminal to a layout-side net of a different name, compose with a `hints.same_nets` pair. Every reconciled class emits a `severity: "warning"`, `category: "device.bulk_reconciled"` disclosure entry — see "`device.bulk_reconciled`" below. Model names are matched exactly first and then case-insensitively (`NetlistSpiceReader` upper-cases `res_x` to `RES_X`). A name that resolves on neither side, a class the reference is *not* actually missing a terminal from, and a class two or more terminals apart (this hook reconciles exactly one extra terminal per class, since the entry names exactly one net) are each an application error (exit 1), not a silent no-op. `"engine": "klayout"` only. |
| `hints.same_nets` | array\<[string, string]\> | Optional `[layout_net_name, reference_net_name]` pairs — ties a named net in the layout's top circuit to a named net in the reference's top circuit. A name that does not resolve on the stated side is an application error (exit 1), not a silent no-op. Each pair is a hard assertion (`must_match=True`): a pair the comparer refuses is reported as a `hints.rejected` entry, never silently dropped. Its purpose is to **disambiguate** a pairing the comparer would otherwise resolve arbitrarily (or not at all) — it is *not* a way to reconcile two differently-named nets, which the comparer already matches on its own without any finding, nor a way to clear a `topology` "name/identity conflict" entry. See "`hints.rejected`" below for both refusal shapes and why the name-conflict case is not one a hint can fix. |
| `hints.equivalent_pins` | object\<string, array\<[string, string]\>\> | Optional per-subcircuit swappable-pin groups, keyed by **reference**-side subcircuit name (`NetlistComparer.equivalent_pins` only accepts circuits from the netlist passed as `compare()`'s second argument, which is always the reference netlist in this command's `compare(layout, reference)` call order). A name that does not resolve on either axis (an unknown subcircuit, or an unknown pin on a subcircuit that does resolve) is an application error (exit 1), the same "typo must be visible" discipline `hints.same_nets` applies. Every grouping actually applied is disclosed back in the response's top-level `hints_applied` field (issue #1998) — see below — since, unlike `hints.same_nets`, it has no "rejected" outcome of its own to otherwise reveal that it changed anything. |
| `options.keep_extracted` | boolean | When `layout.file` is given (inline extraction), retain the intermediate extracted netlist on disk at `<request-dir>/.klt/lvs/<top>.spice` and echo its path in `environment.extracted_netlist`, where `<request-dir>` is the request file's directory (or the current working directory for the `-`/inline-JSON forms). Default `false` (nothing is written to disk). Written *before* `options.combine_devices` runs (a genuinely intermediate, pre-combine snapshot); when `combine_devices: true` and the deck sets `ResistorDevice.fixed_offset_ohm` (issue #559), the retained netlist also predates that correction (deferred until after combining — see `options.combine_devices` below), so its `R` values are the raw, uncorrected-and-uncombined per-primitive figures, not what the compare itself uses. |
| `options.combine_devices` | boolean \| array\<string\> | When `true`, calls `klayout.db.Netlist.combine_devices()` on **both** the layout and reference netlists before comparing — merging devices a device class recognises as combinable (e.g. parallel/series MOSFETs sharing gate/source/drain/body connectivity). This is what makes folded/multi-finger devices (a wide transistor drawn as N parallel fingers of width `W/N`) and split/interleaved matched-pair segments (common-centroid, interdigitated layout) comparable against a single lumped schematic device — without it, each finger/segment reports as its own unmatched device. Default `false` (today's per-drawn-device matching, unchanged) because unconditional merging would also collapse genuinely-distinct parallel devices (e.g. a DAC array's intentionally-separate legs) that some callers want reported individually — opt in only when the layout actually uses folded/split constructions. Applied identically for both engines. After combining, `klt lvs` purges the interior nets `combine_devices()` empties — the N-1 interior nodes of a collapsed series string, left with zero terminals and zero pins once their devices are folded — so `counts.nets.*` and `mismatches[]` reflect the post-combine, post-purge netlist rather than the raw post-combine one (otherwise those disconnected nodes would inflate `counts.nets.layout` and surface as spurious `net.unmatched` findings no caller could act on). The purge is scoped to genuinely-empty nets (no terminals, no pins, no subcircuit pins), so a genuinely-unused top-level pin's net is never dropped and `counts.pins.*` is unaffected; it runs only when combining actually ran (`false` leaves counts exactly as before). On a **partial-match device group** — N real (matching-relevant) instances plus M dummy instances that all share two of three terminals, but only the N real instances also share the third (e.g. a matched bipolar/MOS array's flanking dummies) — `klayout.db`'s own `combine_devices()` can raise an internal-consistency `RuntimeError` rather than combining just the maximal matching subset; `klt lvs` catches that specific error per netlist instead of letting it abort the run, keeps whatever it had already combined, leaves the rest of that netlist's devices individual, and records a `severity: "warning"`, `category: "device.combine_incomplete"` entry in `mismatches[]` — see "`device.combine_incomplete`" below. **Whether this error fires is not fully deterministic across otherwise-identical runs (issue #1185)** — the same layout GDS + reference netlist can combine cleanly on one invocation and hit the error on the next, because KLayout's own `combine_devices()` groups candidates using an ordering that depends on process heap addresses, not on netlist content, and that ordering is not controllable from Python. `klt lvs` retries the combine per side against independent netlist copies to cut the observed flake rate sharply (not to zero — it cannot be, per the above); the number of attempts is `options.combine_devices_max_attempts` (default `5`, see its own field-table row below); see "`device.combine_incomplete`" below for the full explanation, the retry budget, and a reported exhaustion-rate observation against a much larger netlist than this budget was originally tuned against. **Fixed-offset resistor correction (issue #559):** for a deck row that sets `ResistorDevice.fixed_offset_ohm` (see `klt extract`'s docs, "Drawn resistors" — currently only sky130's `res_high_po`), inline extraction (`layout.file` + `layout.deck`) normally applies that fixed per-instance correction to `R` at extraction time, once per drawn primitive. When `combine_devices: true`, `klt lvs` instead defers that correction and applies it once, after combining — so N series-connected drawn primitives folded into one logical device get the fixed offset exactly once (`total_L/W*sheet_rho + 1*fixed_offset_ohm`), not once per primitive (`total_L/W*sheet_rho + N*fixed_offset_ohm`), which is what KLayout's own `combine_devices()` would otherwise produce by summing each primitive's already-corrected `R`. Only the layout side is affected (the correction is a layout-deck geometric property, not a schematic one). This deferred correction also applies to the **pre-extracted `layout.netlist` shape when a `layout.deck` is supplied alongside it** (issue #585): `layout.deck` there does not trigger extraction, but it does name the deck whose `fixed_offset_ohm` `klt lvs` applies once per post-combine device, exactly as for inline extraction. For that to produce the correct result the pre-extracted SPICE must have been written with the correction *deferred* — extract it with `klt extract --defer-resistor-fixed-offset` (the CLI, issue #588) or `run_extract(..., apply_resistor_fixed_offset=False)` (the Python API, the same switch), which omits the per-primitive offset from the written `R` so this option can add it once after the series fold. Those two are the extraction-time half of this contract, reachable from a subprocess-only flow and from an importing one respectively; see `docs/cli/extract.md`'s "Deferring the fixed resistor offset". A `layout.netlist` extracted the default way already has the offset baked into each primitive; feeding that through `combine_devices: true` with a `layout.deck` would double-count it (the already-summed per-primitive offset cannot be un-summed after folding), so pair `combine_devices` with a deferred extraction, or omit `layout.deck` to leave the pre-extracted `R` values untouched. Omitting `layout.deck` entirely (the bare `{"netlist": ..., "top": ...}` shape) attempts no correction at all — the pre-extracted `R` values are used exactly as written. **Restricting which device classes are combined (issue #1370):** this field also accepts an **array of device-class name strings** (e.g. `["nfet_01v8"]`) instead of a boolean, in which case only the named classes are combined and every other class is left as drawn. That is the escape hatch for a netlist where KLayout's own `combine_devices()` trips the internal-consistency error above *deterministically* rather than intermittently — the retry budget below then has nothing to resample, so scoping the combine to the class that actually needs folding (and away from the one whose partial-match group trips the error) is the only way to get the compare the caller wanted to run at all. Names are matched case-insensitively against both netlists' registered device classes, so the SpiceReader-uppercased (`RES_HIGH_PO`) and deck-declared (`res_high_po`) spellings are interchangeable. An empty array, a non-string entry, or a bare string is a request error (exit `1`) — use `false` to disable combining. Naming a class that exists in **neither** netlist is likewise a request error rather than a silent no-op (a typo would otherwise restrict combining to nothing and surface as a full `device.unmatched` cascade that looks exactly like a real design error); a class present on only one side is accepted, since a layout-only parasitic flavour or a reference-only lumped model is legitimate. The resolved value is echoed back verbatim under `options.combine_devices` in the response — a boolean for the boolean shape, the normalised array for the array shape — so `--check --rerun` reproduces the *restricted* compare rather than an unrestricted one whose difference it would then report as drift. **Symmetric degrade and `status: "inconclusive"` (issue #1370):** when the retry budget below is exhausted on either side, `klt lvs` no longer ships the resulting lopsided state to the comparer. Both netlists are rolled back to snapshots taken *before* any combining ran, so the compare that does run is apples-to-apples (exactly the state `combine_devices: false` would have produced on both sides) rather than a partially-folded layout against a fully-folded reference — and this holds for an asymmetric failure too, where one side combined cleanly and the other did not. Because the compare the caller asked for never ran, a resulting `"mismatch"` is reported as `status: "inconclusive"` (exit `4`) instead — see "`device.combine_incomplete`" and "Exit codes" below. A `"match"` is *not* downgraded: an uncombined compare that still matched is strictly stronger evidence than a combined one, and this command never re-derives a verdict the engine did not reach. **Capacitor `C` sum-conservation check (issue #1497):** independent of the `RuntimeError` case above, KLayout's own `combine_devices()` can also return normally with a capacitor device's `C` parameter left inconsistent with its pre-combine parallel group's summed total (while the same group's `A`/`P` parameters combine correctly) — no exception, no `device.combine_incomplete` warning. `klt lvs` checks and corrects this in place after every successful combine and records a `severity: "warning"`, `category: "device.combine_parameter_corrected"` entry when it fires — see "`device.combine_parameter_corrected`" below. |
| `options.combine_devices_max_attempts` | integer | Issue #1412. The retry budget behind `options.combine_devices`'s run-to-run nondeterminism mitigation (issue #1185, see "`device.combine_incomplete`" below): how many independent `Netlist.dup()` attempts `klt lvs` makes per side before falling back to a `device.combine_incomplete` warning and (if it fires on either side) `status: "inconclusive"`. Default `5`, unchanged from before this option existed. Must be a positive integer (`>= 1`); anything else is an application error (exit 1), not a silent fallback to the default. Ignored (but still validated) when `options.combine_devices` is falsy. Raising it trades runtime (each attempt is its own full `combine_devices()` pass over a netlist copy) for a lower observed exhaustion rate on a large/complex netlist that hits the default budget's limit more often than the default's own small-fixture derivation predicts — see "Run-to-run nondeterminism" under "`device.combine_incomplete`" below for a reported observation at that scale and why no single value can be recommended generically; lowering it trades the reverse, useful for fast iteration against a small netlist where an occasional degrade is cheap to re-run. Not echoed in the response's `options` block (a runtime guard, not a compare input, the same convention as `options.netgen_timeout_s`) and not reconstructed by `--check --rerun` for the same reason. |
| `options.combine_devices_per_circuit` | object\<string, boolean\> | Issue #1552. A per-macro alternative to the single, whole-request `options.combine_devices` above, for a **composed** design whose own macros were each already independently verified under their own — possibly *opposite* — `combine_devices` setting: `{ "<circuit-name-glob>": <boolean> }`, applied via `klayout.db.Circuit.combine_devices()` (not the whole-netlist `Netlist.combine_devices()`) to each side's own matching circuits, **before** `options.flatten_layout`/`options.flatten_reference` run — see "Composing macros with opposing `combine_devices` needs" below for the full worked example and why the whole-request boolean cannot satisfy two such macros at once. Keys are `fnmatch`-style glob patterns matched **case-sensitively** against each side's own circuit names — `NetlistSpiceReader` upper-cases circuit names read back from SPICE (a `.subckt macroa` declaration reads back as circuit `"MACROA"`), so a pattern written in the source SPICE's own case will not match; a pattern matching zero circuits on a side is a `severity: "warning"`, `category: "combine_devices_per_circuit.unmatched"` entry (not an application error — a pattern legitimately naming a circuit that exists on only one side, e.g. a reference-only lumped macro, is not itself a mistake) — see "`combine_devices_per_circuit.unmatched`" below. Applied in **declaration order**: the first pattern that matches a given circuit name wins, so listing specific circuit names ahead of a catch-all `"*"` gets "combine everything except these", while listing only the circuits that need combining gets "combine nothing except these" (every unmatched circuit's own default). Mutually exclusive with a truthy `options.combine_devices` (a clean application error, exit 1) — an explicit `combine_devices: false` alongside it is a harmless no-op. Meaningful only for circuits that already exist as separate circuits on that side: a `layout.file` inline extraction is always a single flat circuit (no subcircuit boundary yet — see `options.flatten_layout` below), so it is a no-op there — **and the same is true for a pre-extracted `layout.netlist`, whenever that netlist was itself produced by `klt extract` against a `klt gen-compose`d, multi-macro GDS.** `klt extract` has no hierarchical extraction mode (issue #1085 was closed without adding one — see `klt extract`'s own "flat, not hierarchical" limitation note), so its written/returned netlist is always exactly **one flat `.SUBCKT`** regardless of how many macros the source GDS composed or which of `layout.file`/`layout.netlist` reads it back — there is no per-macro subcircuit boundary on the layout side for this option's glob patterns to match against in that shape either. A hierarchical, pre-extracted `layout.netlist` is only useful here when it comes from somewhere *other* than `klt extract` (hand-written, or produced by a tool that preserves per-macro subcircuit boundaries) — each macro its own subcircuit is where this option has something real to scope against. A composed-macro caller who hits this — exactly the `klt gen-compose` + top-level `klt lvs` workflow this option was filed to help (issue #1552) — should reach for issue #1552's option 3 instead: extract the layout side with `klt extract --abstract-cells` (see "Digital gate-level LVS" above for the full worked sequence) so each macro becomes its own black-box circuit, paired with a matching hand-authored reference at the same per-macro granularity. See also "Composing macros with opposing `combine_devices` needs" below, which notes this same limitation where it first arises. Each named circuit gets its own independent `options.combine_devices_max_attempts`-bounded retry (issue #1185's nondeterminism mitigation, scoped per circuit) and its own `device.combine_incomplete` warning on exhaustion — but, unlike the whole-request `options.combine_devices`'s symmetric degrade (issue #1370), an exhausted circuit is simply left uncombined and reported, never rolled back alongside every other circuit's already-successful combine, since each circuit's own combine choice is already an independent, caller-declared decision. **Carries the same post-combine corrections as `options.combine_devices` (issue #1557):** the fixed-offset resistor correction (issue #559/#585, above) and the capacitor `C` sum-conservation check (issue #1497, above) both apply here too, scoped to just the circuit(s) that combined *cleanly* on that side (an exhausted circuit's devices are left untouched, so neither correction runs against a fold that never completed) — see those two sections above for the mechanism; only the scoping differs. Not echoed as `null`\|`{}` interchangeably — `null` when the option was omitted, the resolved mapping otherwise, both in the response's `options` block and (when non-empty) reconstructed verbatim by `--check --rerun`. |
| `options.parameter_tolerance` | number | Optional relative tolerance for numeric device parameters, expressed as a **fraction** (`0.001` is 0.1%), applied to every parameter of every device class (issue #589). `"engine": "klayout"` only. Omit (or `null`) for today's exact compare — the default is unchanged and no existing verdict moves unless a caller opts in. When given, a device pair whose *every* differing parameter is within the tolerance is compared as if those values agreed, so a physically-clean design whose extracted value is a deck's 5–6-significant-figure model fit can reach `status: "match"` against a schematic reference rounded to 2–3 figures. Each absorbed difference is disclosed as a `severity: "warning"`, `category: "device.parameter_tolerated"` entry carrying both original values — see "`device.parameter_tolerated`" below, which also documents the mechanism and its limits. Must be a number in `[0, 1)`; anything else (a string, a per-parameter object, a negative value, `1.0` or above) is an application error (exit 1), not a silent fallback to the default. |
| `options.compare_parameters` | object\<string, array\<string\>\> | Issue #1928. Scopes *which* device-class parameters take part in the compare at all: `{ "<device-class name>": [<parameter name>, ...] }`. Every numeric parameter a device class declares is compared by default (`klayout.db.NetlistComparer` reads each `DeviceClass`'s own primary-parameter set), with no request-level way to narrow that — so a single always-compared parameter neither side can state identically (e.g. a geometry-derived layout-side value a reference netlist's own device cards never carry at all) makes `status: "match"` unreachable, and `options.parameter_tolerance` cannot help: it is a *relative* tolerance and can never call a zero-vs-nonzero structural difference equal (`_relative_delta()` returns `1.0` whenever exactly one side is zero). For each named class, `klt lvs` enables exactly the listed parameters and disables every other parameter that class declares via `klayout.db.DeviceClass.enable_parameter(name, false)`, on **both** sides' own class object (`NetlistComparer` consults each device's own class, not a single shared one — the same reason `reference.device_bulk`/the `subckt-call` placeholder-value exclusion also touch both sides), before the comparer runs. `"engine": "klayout"` only — the `netgen` engine has no equivalent per-parameter compare-scoping hook, so a `netgen` request with this option set is an application error (exit 1), same boundary as `options.parameter_tolerance`/`hints`/`reference.device_bulk` above. Class names are matched case-insensitively against both netlists' registered device classes (`NetlistSpiceReader` upper-cases class names read back from SPICE while a deck-declared class keeps its own casing), and parameter names are matched case-insensitively against that class's own declared parameters. **Validation, never a silent no-op:** a device-class name present in **neither** netlist, or a parameter name not declared by that class on either side it resolves on, is a clean application error (exit 1) naming the typo and what is actually available — the same "typo must be visible" discipline `hints.same_nets` and `options.combine_devices`'s array form already apply (a silently-ignored typo here would look exactly like a device class whose every parameter happens to agree, which is far more dangerous than a typo that simply does nothing). A class named here but present on only one side is legitimate (mirrors `options.combine_devices`'s own "present on just one side is not an error" rule) and is scoped on that side alone. Every parameter this option disables is disclosed as its own `severity: "warning"`, `category: "device.parameter_excluded"` entry — see "`device.parameter_excluded`" below — so a `"match"` reached this way is never silently indistinguishable from a full parameter compare. This option does not compute or reconcile a value for the excluded parameter on either side (unlike `reference.device_bulk`'s terminal reconciliation) — it only removes that parameter from the compare; teaching one side to state the missing parameter correctly is explicitly out of scope, narrower, and not what this option does. The resolved mapping is echoed back verbatim under `options.compare_parameters` in the response (`null` when the option was omitted) and reconstructed verbatim by `--check --rerun`, the same always-present-but-nullable convention `options.combine_devices_per_circuit` already follows. |
| `options.power_connectivity` | boolean \| object | Issue #1952. Controls the **power/ground connectivity check** that pairs with a signal-only `reference.form: "gate-level-verilog"` compare — see "Power/ground connectivity" below for what it verifies and why it can be verified at all when the reference carries no power connectivity. Three accepted shapes: omitted (the default — the cross-instance consistency check runs), `false` (opt out entirely; the intended setting for a genuinely multi-domain design, recorded in the response's `power_connectivity.reason` rather than silently producing nothing), `true` (identical to omitting it, stated explicitly so a committed request document records that the check was wanted), or `{"expected_nets": {"<PIN>": "<NET>", ...}}` (declare which net each power/ground pin name must reach, which upgrades the *relative* consistency check to an *absolute* one — see `power.unexpected_pin_net` below). Declaring a subset of pins is fine: every pin the mapping does not name still gets the consistency check. Pin and net names are matched case-insensitively (`NetlistSpiceReader` upper-cases what it reads; a netlist handed over in-process from `klt extract` does not). Honored only for `reference.form: "gate-level-verilog"` — every other form's reference is arbitrary SPICE that carries its own power pins and nets, which the ordinary compare already checks, so there is no signal-only gap for this option to fill (the response's `power_connectivity.reason` says so explicitly rather than leaving the field absent). A wrong-shaped value is a clean application error (exit 1), never a silent no-op. Echoed back verbatim under `options.power_connectivity` in the response (`null` when the option was omitted) and reconstructed verbatim by `--check --rerun`, the same always-present-but-nullable convention `options.compare_parameters` follows. |
| `options.netgen_setup` | string | Only used with `"engine": "netgen"`. Path to a netgen LVS setup `.tcl` file — see "Engine" -> `"netgen"` above. Omit to run with netgen's own default setup. |
| `options.netgen_timeout_s` | number | Only used with `"engine": "netgen"`. Wall-clock budget (seconds) for the `netgen` subprocess. Default `300`. |
| `options.flatten_reference` | boolean | When `true`, calls `klayout.db.Netlist.flatten()` on the **reference** netlist in-process, right after it is read and before circuit selection (issue #1085). `klt extract` always extracts a *flat* layout-side netlist — a single top circuit, no subcircuit calls (see `klt extract`'s "flat, not hierarchical" limitation note) — so a hierarchical reference (one leaf `.subckt` plus N instance calls of it, the shape a macro built by tiling one verified leaf cell naturally takes) can never structurally match it: `NetlistComparer` compares circuit-by-circuit, and the flat layout side simply has no subcircuit-call circuit to pair against the reference's, producing an undiagnosable `topology` "circuit could not be matched to a counterpart" mismatch on both sides. Flattening the reference first collapses every subcircuit-call instance in place, so only its top-level circuit(s) remain — directly comparable against the already-flat layout side. Default `false` (today's unconditional per-circuit matching, unchanged) — a caller who genuinely wants a hierarchy-preserving compare (e.g. because both sides are hierarchical, see `tests/test_lvs.py`'s `test_net_correspondence_scopes_dedup_by_circuit`) is never silently flattened out from under them. A `reference.top` name that only existed as an interior circuit flatten would inline away no longer resolves after flattening — pass the name of whatever remains a genuine top-level circuit. Each side that is actually flattened (its circuit count changes) is disclosed as a `severity: "warning"`, `category: "topology.flattened"` entry — see "`topology.flattened`" below — so a `"match"` reached after flattening is never silently indistinguishable from one reached against the netlist's original hierarchy; a netlist that already had only its top circuit(s) (nothing to flatten) adds no such entry. |
| `options.flatten_layout` | boolean | The symmetric counterpart of `options.flatten_reference`, for the **layout** side (issue #1085's item 2): calls `Netlist.flatten()` on the layout netlist right after it is resolved, before circuit selection. Meaningful for the pre-extracted `layout.netlist` shape, which can itself be hierarchical (a hand-written or externally-extracted SPICE netlist is under no obligation to be flat the way `klt extract`'s own output always is) — a no-op for the `layout.file` (inline extraction) shape, since that netlist is already flat. Default `false`; same `topology.flattened` disclosure and no-op-on-an-already-flat-netlist behaviour as `options.flatten_reference` above. |

### `layout` shapes

Extraction and compare are separable steps that also compose in one call:

- **Inline extraction** — `{"file": "design.gds", "deck": "sky130", "top": "ota_5t"}`. Runs `klt extract`'s core extraction (the same `extract_netlist_from_layout` function `klt extract` itself calls) against the named curated deck (`sky130`/`gf180mcu`/`sg13g2`/`sg13cmos5l`), then compares the resulting in-memory netlist directly — no SPICE round-trip is required unless `options.keep_extracted` is set. `top` is optional (defaults to the layout's sole top cell, same as `klt extract --top`); `deck` is required in this shape. An optional boolean `top_cell_pins` (default `false`) mirrors `klt extract --top-cell-pins`: when `true`, only labels drawn directly in the top cell are promoted to top-level pins, so a net named only by a label inside an instanced sub-cell stays internal instead of demanding a matching port in `reference.netlist` (issue #291). An optional array of strings `declared_pins` (default unset) mirrors `klt extract --pins` (issue #514): when given, every promoted pin not named in this set is demoted back to an internal net, so naming an internal node of a lumped schematic device (e.g. one tap of a metal-option ladder) for documentation no longer promotes it to a pin `options.combine_devices` cannot fold through. Applied after `top_cell_pins`'s own reconciliation — it can only further restrict the promoted set. An empty `declared_pins` array is a request error (omit the field entirely to keep every named net promoted). An optional array of strings `pin_source_cells` (default unset, issue #1513) mirrors `klt extract --pin-source-cells`: a *positional* counterpart to `declared_pins`, for a `klt gen-compose`d assembly of several pre-labelled macros with no governing top-level DEF of its own — every drawn pin-name label physically located inside an instance of one of these named cells (anywhere in the hierarchy, at any depth) is resolved to its real net by probing that label's own position, not by matching its text; every currently-promoted pin not reached this way is demoted, exactly as `declared_pins` demotes on a miss. Applied after `declared_pins`'s own reconciliation (when both are given) — it can only further restrict. An empty `pin_source_cells` array is likewise a request error. `top_cell_pins`, `declared_pins`, and `pin_source_cells` are all only meaningful in this inline-extraction shape — they have no effect on a pre-extracted `layout.netlist`, whose pins are already fixed. An optional object `deck_options` (issue #600) mirrors `klt extract --deck-option`: selects a caller-visible sheet-rho flavour of a shared-geometry resistor family for this extraction — see the `layout.deck_options` field-table row above.
- **Pre-extracted netlist** — `{"netlist": "design.spice", "top": "ota_5t"}`. Reads an existing extracted (or hand-written) SPICE netlist directly via `NetlistSpiceReader`, skipping extraction entirely. `top` is optional (defaults to the sole top circuit). `deck_options` is also honored here when `deck` is supplied alongside `netlist` (issue #585's pre-extracted-plus-deck shape): no extraction runs in this shape, so `deck_options` only affects `device_classes` and the deferred resistor `fixed_offset_ohm` correction (`options.combine_devices`) — it has no effect on the SPICE `R` values already baked into the supplied netlist. **Capacitor device-class recovery (issue #1876):** `klt extract -o` writes an unbound capacitor's `C` card bare and value-only, with no trailing device-class token (issue #1558 — see `klt extract`'s own docs for why). This shape (and `reference.netlist`, below) recovers that capacitor's real device-class name from the writer's own preceding `* device instance … <class>` comment instead of falling back to KLayout's generic `CAP` class, so a two-step `klt extract -o netlist.spice && klt lvs` pipeline against a reference that names the capacitor's class explicitly still reaches `status: "match"`. A missing or malformed comment (e.g. a hand-edited SPICE file) degrades gracefully to the generic class, exactly as before this recovery existed — it never raises. This is unrelated to `options.combine_devices_per_circuit` immediately below except that both are only meaningful against this pre-extracted shape; recovery runs regardless of whether per-circuit combining is used.

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
  "hints_applied": null,
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
  "device_classes": ["nfet", "pfet", "pnp", "sky130_fd_pr__model__cap_mim", "sky130_fd_pr__model__cap_mim_m4", "resistor"],
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
    "klayout_version_mismatch": false,
    "pdk": null,
    "deck": { "name": "sky130", "content_hash": "sha256:<hex>", "released": true },
    "input": { "content_hash": "sha256:<hex>" }
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
| `options` | object | Echo of every request option that shapes *what was compared*, as resolved (issue #1205): `combine_devices` (boolean, or the normalised array of device-class names when the array shape was used — issue #1370), `combine_devices_per_circuit` (object \| `null`, issue #1552 — the resolved `{"<circuit-name-glob>": <boolean>}` mapping, or `null` when the option was omitted), `flatten_layout`, `flatten_reference` (booleans), `netgen_setup` (string \| `null`, echoed exactly as given, not resolved against the request file's directory), `parameter_tolerance` (number \| `null`, the same value as the top-level field above, repeated here so this block is a complete request-side view), `compare_parameters` (object \| `null`, issue #1928 — the resolved `{"<device-class>": [<parameter>, ...]}` mapping, or `null` when the option was omitted), and `power_connectivity` (boolean \| object \| `null`, issue #1952 — the caller's own `options.power_connectivity` value verbatim, or `null` when the option was omitted; note that `null` here means the check ran under its default-on setting, *not* that it was skipped — read `power_connectivity.status` for that). Every key is always present, never omitted — so a consumer reading only the response can tell which compare the verdict belongs to, and `--check --rerun` can re-run *that* compare rather than a differently-shaped one whose difference it would then report as drift. `options.keep_extracted` is deliberately not echoed here (it is an output-side flag that cannot change a verdict, and is already visible as `environment.extracted_netlist`), nor is `options.netgen_timeout_s` (a runtime guard, not a compare input). |
| `hints_applied` | object\<string, array\<array\<string\>\>\> \| `null` | Issue #1998. Every `hints.equivalent_pins` grouping actually passed to `NetlistComparer.equivalent_pins()` for this run, keyed by the (reference-side) subcircuit name it was declared against, with each group echoed verbatim as the caller wrote it — e.g. `{"ota_5t": [["inp", "inn"]]}`. `null` when the request supplied no `hints.equivalent_pins` (including a request with only a `hints.same_nets` hint, or no `hints` at all) — the same always-present-but-nullable convention `options.compare_parameters` follows for an optional dict-shaped echo, never a spuriously present empty `{}`. This is the only visibility a report gives into an applied `equivalent_pins` hint: unlike `hints.same_nets` (a hard assertion the comparer can refuse, surfaced as a `hints.rejected` mismatch entry — see below), a swappable-pin group has no "rejected" outcome, so without this field a reader could not tell whether — or how broadly — an `equivalent_pins` hint reshaped the verdict. Does not itself change `status`, `mismatch_count`, or any other verdict field; it only discloses that the hint was applied. A request naming an unknown subcircuit or pin fails the run outright (`LvsError`, exit 1, per `hints.equivalent_pins`'s own field description above) before any report is produced, so this field is never populated with a partial or invalid entry from a failed application. |
| `status` | `"match"` \| `"mismatch"` \| `"inconclusive"` | `"match"` when `NetlistComparer.compare()` reports the netlists equivalent; `"mismatch"` otherwise. `"inconclusive"` (issue #1370) is the third outcome: the compare the request asked for could **not** be performed, so this run reached no verdict about the design. It has exactly one cause today — `options.combine_devices` was requested and `combine_devices()` exhausted its retry budget on at least one side, so both sides were rolled back to their uncombined state (the symmetric degrade described under `options.combine_devices`) and the resulting engine `"mismatch"` was downgraded. A `"match"` is never downgraded. This mirrors `klt equiv`'s own `"inconclusive"` vocabulary and gets its own exit code (`4`, the same value `klt equiv` uses) so an automation gate can tell "the design differs" from "the comparison could not be performed". Never `"error"` in-band — a failed run does not emit this envelope at all (see "Exit codes"). This is always the engine's own verdict, including when `options.parameter_tolerance` is in force — that option is implemented by re-running a real `compare()` on values snapped into agreement, never by re-deriving the verdict from this command's own findings (see "`device.parameter_tolerated`" below). |
| `power_connectivity` | object | Issue #1952. The **power/ground half** of the verdict, reported beside `status` rather than folded into it — see "Power/ground connectivity" above for the full field table, what the check verifies, and the invariant it rests on. Always present, for every `reference.form`: `status` is `"match"`/`"mismatch"` when the check ran, and `"unchecked"` (with a human-readable `reason`) when it did not, so "was power connectivity verified by this run?" is answerable from any `klt lvs` report on its own. **A caller wanting full LVS on a digital block gates on both `status == "match"` and `power_connectivity.status == "match"`** — this field never changes `status`, `mismatch_count`, `error_count`, `category_counts` or `category_error_counts`, all of which stay exactly the signal-connectivity compare's own results. |
| `body_verification` | object | Issue #1983. Whether this layout's MOS **body terminals** were resolved from real drawn/derived tap geometry or from a deck-synthesized net — the machine-checkable form of the `device.body_unverified` warning, reported beside `status` rather than folded into it. See "The same condition, machine-checkable" below for the full field table. Always present: `status` is `"verified"`/`"unverified"` when a `layout.deck` was given, and `"unchecked"` (with a human-readable `reason`) for the pre-extracted `layout.netlist` form, so "were the device bodies verified by this run?" is answerable from any `klt lvs` report on its own rather than being inferred from the *absence* of a warning. This field never changes `status`, `mismatch_count`, `error_count`, `category_counts` or `category_error_counts`. |
| `mismatch_count` | integer | `len(mismatches)`. Can be nonzero even when `status` is `"match"` — a `severity: "warning"` entry (e.g. an ambiguity the comparer resolved on its own) does not change the verdict. |
| `error_count` | integer | Issue #1132: the number of `mismatches[]` entries with `severity: "error"` — `sum(category_error_counts.values())`. `mismatch_count` alone cannot tell a caller this without re-reading every entry, since a nonzero `mismatch_count` can be entirely `severity: "warning"` (e.g. a report whose only finding is a `device.bulk_reconciled` disclosure). `0` on a `status: "match"` report exactly (a `"match"` verdict never carries an `error` entry). |
| `category_counts` | object\<string, int\> | Per-category mismatch counts (`error` and `warning` entries combined), keys sorted for determinism — the LVS analogue of `klt drc`'s `rule_counts`. |
| `category_error_counts` | object\<string, int\> | Issue #1132: `category_counts`, but counting only `severity: "error"` entries per category — lets a caller gate on "does category X have any real defect" without re-reading `mismatches[]` and re-filtering by `severity` itself. Same sorted-keys convention as `category_counts`; a category with zero `error` entries (all `warning`, e.g. an all-`warning` `topology.flattened` run) is simply absent from this object rather than reported as `0`, matching `category_counts`'s own "no entries of this category" convention. `sum(category_error_counts.values()) == error_count`. |
| `counts` | object | Side-by-side `layout`/`reference`/`matched` tallies for `nets`, `devices`, `pins`. `matched` counts only a **strictly successful** pairing (e.g. a device paired with identical parameters and class) — a device paired despite a `device.property`/`device.class` mismatch is *not* counted as matched. **Scope mismatch for `"engine": "klayout"` (issue #1887):** `layout`/`reference` are scoped to the **top circuit only** (`layout_circuit.each_net()`/`pin_count()` and the reference-side equivalents — one circuit's own declared nets/pins), while `matched` is scoped to the **entire compared hierarchy** (every matched circuit, top and subcircuits alike — the same accumulators that back `net_correspondence`, see below). These are genuinely different scopes reported side by side under names that read as three comparable numbers for the same quantity, so `matched` can — and, once any subcircuit below top also matches, routinely does — numerically **exceed both** `layout` and `reference`. That is not a bug in the compare; it is a fact about what each field counts, and a caller comparing `matched` against `layout`/`reference` as if they shared a denominator will misread it. For `"engine": "netgen"`, this scope split does **not** apply: `layout`, `reference`, and `matched` are all top-circuit-scoped, and `matched` is exact on a `"match"` verdict and `0` on a `"mismatch"` verdict (a separate, netgen-only known limitation — see "Engine" -> `"netgen"` above). |
| `device_classes` | array\<string\> \| `null` | The layout-side deck's `ExtractionDeck.device_classes` (see `klt extract`'s own field of the same name) — what that deck is structurally capable of recognising, not what this compare found. Deck-dependent, not MOS-only (issue #1130 added resistor/capacitor/bipolar/diode extraction to both registered decks) — as of 2026-08-21, sky130 reports `["nfet", "pfet", "pnp", "sky130_fd_pr__model__cap_mim", "sky130_fd_pr__model__cap_mim_m4", "resistor"]` and gf180mcu reports `["nfet", "pfet", "bjt", "cap_mim_2f0_m4m5_noshield", "resistor", "diode_nd2ps_06v0", "diode_pd2nw_06v0"]` — re-check the installed deck's own `device_classes` rather than treating either list as a value this doc pins for future decks/versions. Present whenever a `layout.deck` is given — always for `layout.file` (inline extraction, where the deck is required), and also for the pre-extracted `layout.netlist` shape when a `layout.deck` is supplied alongside it (issue #585). `null` only when no `layout.deck` was given (the bare `{"netlist": ..., "top": ...}` shape). |
| `environment` | object | Reproducibility block: `engine`, `engine_version` (the installed `klayout` package version for `"engine": "klayout"`; netgen's own reported version, parsed from its startup banner, for `"engine": "netgen"` — `null` if unparseable), `layout_sha256` (of `layout.file`, or of `layout.netlist` when no extraction ran), `reference_sha256` (of `reference.netlist`), `extracted_netlist` (path to the retained intermediate netlist when `options.keep_extracted` is set and `layout.file` was given; `null` otherwise — excluded from `klt lvs --check --rerun`'s drift diff, see "Full mode (`--rerun`)" below). |
| `provenance` | object | Shared reproducibility block (`klt_version`, `klayout_version`, `pdk`, `deck`, `input`) defined once in [`docs/json-contract.md`](../json-contract.md). `input` (issue #1969) is `{"content_hash": "sha256:<hex>"}` for both engines, pinning the layout side of the compare — the same file `environment.layout_sha256` hashes, in the `sha256:`-prefixed form the shared block uses (`environment.layout_sha256` itself is unchanged, still a bare hex digest). It was `null` before #1969 on the reasoning that `environment.layout_sha256`/`reference_sha256` already covered it; `klt signoff --manifest`'s staleness gate reads `provenance.input.content_hash` generically and cannot see an LVS-only field, so every `content_hash`-pinned "LVS clean" citation graded `stale_evidence`. `pdk` (issue #1901) is `{"name": ..., "source": ..., "version": ...}` when `reference.form` is `"gate-level-verilog"` and `reference.pdk`/`reference.pdk_root` resolve a PDK (the same resolution the `"gate-level-verilog"` form already performs to read each standard cell's real pin order — see "Netlist form" above), else `null` — a plain SPICE-vs-SPICE (or `"subckt-call"`) reference genuinely resolves no PDK. An unresolvable `reference.pdk`/`reference.pdk_root` fails the run the same as any other application error (exit 1); `deck` pins the layout-side extraction deck by name and `sha256:` content hash whenever a `layout.deck` is given (both the `layout.file` and the pre-extracted `layout.netlist` shapes), and is `null` only when no `layout.deck` was given (matching `device_classes`). `deck.released` (issue #1193) is a non-fatal tri-state signal for whether that content hash ships in any released `klayout-tools` version — `false` flags an unreleased/dev-edited deck, `null` when unresolvable (e.g. the generated deck history table is missing). `deck.options` (issue #600) echoes the resolved `layout.deck_options` mapping — present only when non-empty, matching `klt extract`'s own `provenance.deck.options` shape exactly. `klayout_version` is populated the same way for both engines (it is this process's own `klayout` package build, used for netlist parsing/writing either way, not the comparator). `klayout_version_mismatch` (issue #1490, `true`\|`false`) flags whether that `klayout_version` differs from the version this `klayout-tools` build/commit was tested against (`klt version --format json`'s `klayout_version_expected`) — see [`../json-contract.md`](../json-contract.md)'s "Pinning the KLayout engine version". |
| `mismatches` | array\<object\> | One entry per structured mismatch — see below. Empty on a clean match; always present. |
| `net_correspondence` | array\<object\> | The layout↔reference net pairing `NetlistComparer` produced — see "`net_correspondence[]` entries" below. `len(net_correspondence) == counts.nets.matched` (the example above is illustrative, not exhaustive, for a 7-net compare). Always `[]` for `"engine": "netgen"` (see "Engine" -> `"netgen"` above). |

### `mismatches[]` entries

Field-for-field the LVS counterpart of `klt drc`'s `violations[]`: a stable
`category` id (never renumbered/repurposed once shipped, exactly like a DRC
rule id), a curated human `description` (not raw engine log text), and the
objects involved.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `category` | string | One of `net.unmatched`, `net.merged`, `net.split`, `device.unmatched`, `device.class`, `device.class_arity`, `device.bulk_reconciled`, `device.placeholder_value`, `device.property`, `device.parameter_tolerated`, `device.parameter_excluded`, `device.body_unverified`, `device.combine_incomplete`, `device.combine_parameter_corrected`, `pin.unmatched`, `topology`, `topology.flattened`, `topology.power_only_pruned`, `topology.reference_port_alias_joined`, `hints.rejected`. |
| `severity` | `"error"` \| `"warning"` | `"error"` breaks equivalence; `"warning"` is informational and never changes `status`. Informational cases include an ambiguous net pairing the comparer resolved on its own (see `hints.same_nets` above), a `topology` device-class-mismatch entry for a device class with zero actual instances on the side that registered it (e.g. an all-`nfet` layout compared against an all-`nfet` reference netlist that never mentions `pfet` — `klt extract` always registers both polarities' device classes even when only one is instantiated), every `device.body_unverified` entry (see below), every `device.combine_incomplete` entry (see below), and the collateral `device.unmatched`/`net.unmatched` entries left over when a minimal cell's parameter defect is recovered into a `device.property` entry (see "Negative controls" above). A device-class mismatch where the class has one or more real instances still reports `"error"`. Every `hints.rejected` entry (see below) is always `"error"` — `hints.same_nets` is a hard assertion (`must_match=True`), never a suggestion, so the comparer refusing it is always a real finding. Every `device.class_arity` entry (see below) is always `"error"` — a same-named device class the comparer cannot pair on either side is never merely informational. Every `device.bulk_reconciled` entry (see below) is always `"warning"` — it discloses a request-side reconciliation applied before the compare, so it never changes `status` (a request whose only finding is this entry reports `status: "match"` with a nonzero `mismatch_count`). Every `device.placeholder_value` entry (see below) is always `"warning"` for the same reason — it discloses that a `reference.form: "subckt-call"` conversion's placeholder `0` resistance/capacitance was excluded from the compare, so it never changes `status` either. Every `device.parameter_tolerated` entry (see below) is always `"warning"` for the same reason — it discloses a numeric difference `options.parameter_tolerance` absorbed, so it never changes `status` either. Every `device.parameter_excluded` entry (see below) is always `"warning"` for the same reason — it discloses that `options.compare_parameters` removed a parameter from a device class's compare entirely, so it never changes `status` either. Every `topology.flattened` entry (see below) is likewise always `"warning"` — it discloses a request-side structural flatten `options.flatten_reference`/`options.flatten_layout` applied before the compare, so it never changes `status` either. Every `topology.power_only_pruned` entry (see below) is likewise always `"warning"` — it discloses that a power-only layout circuit (and every instance of it) was removed before comparing, against a `reference.form: "gate-level-verilog"` reference, so it never changes `status` either. Every `topology.reference_port_alias_joined` entry (see below) is likewise always `"warning"` — it discloses that a reference port declared only via a plain `assign` alias (e.g. `assign dbg_uart_byte[i] = rx_byte[i];`) had its net joined onto its canonical target's net before comparing, against a `reference.form: "gate-level-verilog"` reference, so it never changes `status` either. Every `device.combine_parameter_corrected` entry (see below) is likewise always `"warning"` — it discloses that a capacitor device's `C` parameter was corrected in place after `combine_devices()` produced a value inconsistent with its pre-combine group's sum, so `status` reflects the corrected value, not the discovery of the inconsistency. |
| `description` | string | Curated, human-readable explanation of this mismatch — never raw `NetlistComparer` log text (which is version-dependent and, per this repo's own testing, sometimes empty). |
| `side` | `"layout"` \| `"reference"` \| `"both"` | Which netlist the offending object(s) live on. |
| `net` | object \| `null` | `{"layout": <name\|null>, "reference": <name\|null>}` when a net is involved. |
| `device` | object \| `null` | `{"layout": <name\|null>, "reference": <name\|null>, "class": <string\|null>}` when a device is involved. |
| `property` | object \| `null` | `{"name": <string>, "layout": <value>, "reference": <value>}` for a `device.property` mismatch, and for a `device.parameter_tolerated` disclosure (whose `reference` is always the reference netlist's *original* value, never the snapped one). `name` is `w_um`/`l_um` for the width/length parameters (matching `klt extract`'s own convention); every other declared device-class parameter is reported under its own lower-cased name. |
| `details` | object \| `null` | Engine-specific/category-specific data that does not map cleanly onto the fields above (issue #343) — additive, not a schema fork. Populated for every `"klayout"`-engine `device.class_arity` entry (see below) with `{"layout_terminals": [<string>, ...], "reference_terminals": [<string>, ...]}`, and for every `device.bulk_reconciled` entry (see below) with `{"terminal": <string>, "reference_net": <string>, "reference_net_created": <bool>, "devices": <integer>, "layout_terminals": [<string>, ...], "reference_terminals": [<string>, ...]}` (`reference_terminals` is the pre-reconciliation list), and for every `device.placeholder_value` entry (see below) with `{"parameter": <string>, "device_kind": "resistor"\|"capacitor", "reference_devices": <integer>, "layout_devices": <integer>, "layout_values": [<number>, ...]}` (the excluded parameter's name, the converted family, how many instances each side has, and the distinct layout-side values that were *not* compared, sorted ascending), and for every `device.parameter_tolerated` entry (see below) with `{"relative_delta": <number>, "tolerance": <number>}` (the observed `|layout - reference| / max(|layout|, |reference|)` and the effective `options.parameter_tolerance` it was accepted under), and for every `device.parameter_excluded` entry (see below) with `{"parameter": <string>, "compared_parameters": [<string>, ...]}` (the one excluded parameter's own name, and the full sorted list of parameters `options.compare_parameters` named for that class). Also populated by the `"netgen"` engine for a `net.unmatched`/`device.unmatched` entry bucketing a whole side-by-side report section it does not further structure: `{"raw": <string>}`, netgen's own report text for that section verbatim. `null` for every other entry (including `"netgen"`-engine device-class-arity mismatches, which this issue's fix does not cover — see "`device.class_arity`" below). |
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
  `hints.same_nets` entry. The hint's job here is to *pin down* which reference
  net the synthesized substrate net corresponds to, not to reconcile the name
  difference on its own — the comparer already matches differently-named nets
  that are topologically identical. If the pair comes back as a
  `hints.rejected` entry saying the two nets are "not identical
  topologically", the substrate/rail connectivity genuinely differs and the
  hint cannot bridge it; see "`hints.rejected`" below.
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

#### `device.placeholder_value`: a converted resistor/capacitor class's value was excluded from the compare

Only possible with `reference.form: "subckt-call"` (issue #1907,
`"engine": "klayout"` only). The conversion writes a literal `0` into a
converted `R`/`C` card's positional *value* slot — `klt lvs` has no PDK
sheet-resistance / capacitance-per-area table to compute a real resistance or
capacitance from a call's `l`/`w` geometry (that data lives in the extraction
decks, deliberately not a dependency of the converter). Geometry is still
carried (`L=`/`W=` for a resistor, a derived `A=`/`P=` for a capacitor); only
the value is a placeholder.

That placeholder is not a cosmetic difference: `R` and `C` are the **primary**
(compared) parameters of KLayout's `DeviceClassResistor` /
`DeviceClassCapacitor`, and `NetlistComparer` uses primary-parameter equality
to seed device correspondence. Left in the compare, a reference class whose
every instance reads `0` against a layout side carrying real,
geometry-computed values does not produce a per-device parameter finding — it
fails to pair the class *at all*, collapsing into a wholesale
`device.unmatched` / `topology` cascade over every instance and every net that
touches one, even when each instance sits on its own distinct, unambiguous net
pair. So `klt lvs` excludes that one parameter from the compare (on **both**
sides' class — the comparer consults each side's own class) before
`NetlistComparer` is constructed, letting topology pair the devices exactly as
an equivalent hand-written `form: "plain-element"` reference carrying real
values already does.

Every excluded class is disclosed in-band as one `severity: "warning"`,
`category: "device.placeholder_value"`, `side: "reference"` entry — the same
discipline `device.bulk_reconciled` applies to a reconciled bulk terminal. A
`"match"` reached this way is never silently indistinguishable from one where
the two sides' values actually agreed:

```json
{
  "category": "device.placeholder_value",
  "severity": "warning",
  "description": "reference device class 'RES_XHIGH_PO' was converted from a subcircuit call (request.reference.form: \"subckt-call\"), so its 'R' value is the literal 0 placeholder on all 4 reference instance(s) -- klt lvs has no PDK sheet-resistance/capacitance-per-area data to compute a real one. 'R' was therefore excluded from this compare on both sides (layout: 4 instance(s), R 15000..120000) and the two sides were paired on topology alone -- that dimension of the compare is not independently verified. Supply a reference in the plain-element form carrying real 'R' values to compare it (see docs/cli/lvs.md, 'device.placeholder_value')",
  "side": "reference",
  "net": null,
  "device": {"layout": null, "reference": null, "class": "RES_XHIGH_PO"},
  "property": null,
  "details": {
    "parameter": "R",
    "device_kind": "resistor",
    "reference_devices": 4,
    "layout_devices": 4,
    "layout_values": [15000.0, 30000.0, 60000.0, 120000.0]
  }
}
```

Notes on the semantics:

- **It never changes `status`.** The entry is always `severity: "warning"`, so
  a request whose only finding is this one reports `status: "match"` with a
  nonzero `mismatch_count` — the same relationship `device.bulk_reconciled`
  and `device.parameter_tolerated` have to a clean compare.
- **It is scoped to the provable placeholder, never to a coincidental zero.**
  Only a class the `"subckt-call"` conversion actually emitted is considered
  (a `"plain-element"` reference never goes through the conversion, so a
  genuine `0` it carries is still compared and still an error), and only when
  *every* reference-side instance of that class reads exactly `0` — the
  invariant the conversion guarantees by construction. A reference that mixes
  a converted card with a hand-written one carrying a real value on the same
  class is left completely alone, so a genuine value defect there is still
  compared and still reported as `device.property`.
- **Only the value parameter is excluded**, not the class's geometry. `L`/`W`
  (resistor) and `A`/`P` (capacitor) are *secondary* parameters KLayout does
  not compare by default either way, so nothing else about the compare
  changes: terminal count, device class, and full net topology are all still
  checked exactly as before.
- **It does not create matches out of ambiguity.** Excluding the value only
  removes a parameter from the equivalence test; a group the two sides
  genuinely cannot distinguish (e.g. three layout resistors against two
  reference ones on the same net pair) still reports `device.unmatched` and
  `status: "mismatch"`, with this disclosure alongside rather than instead.
- **To verify the value dimension**, supply the reference in the plain-element
  form with real `R`/`C` values (`details.layout_values` reports what the
  layout side measured, so a reference can be written against it), or compare
  the extracted values separately with `klt extract`. Pair a rounded
  design-level reference value with `options.parameter_tolerance` — see
  "`device.parameter_tolerated`" immediately below. Note that
  `options.parameter_tolerance` cannot substitute for this exclusion: it is a
  *relative* tolerance and is rejected at `>= 1.0` by design, so no value ever
  reconciles against zero.

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

#### `device.parameter_excluded`: `options.compare_parameters` scoped a device class's parameter set

Only possible when `options.compare_parameters` is given (issue #1928,
`"engine": "klayout"` only). Every numeric parameter a device class declares
is compared by default — `options.parameter_tolerance` above can absorb a
small *numeric* disagreement, but it cannot help when the disagreement is
structural: a parameter one side derives from geometry that the other side's
device cards never carry at all is a zero-vs-nonzero difference, and a
*relative* tolerance can never call that equal (`_relative_delta()` returns
`1.0` whenever exactly one side is zero, and the tolerance is rejected once it
gets anywhere near that). With no request-level way to reach
`klayout.db.DeviceClass.enable_parameter`, that one always-compared parameter
made `status: "match"` unreachable regardless of how clean every other
dimension of the compare was.

`options.compare_parameters` names, per device class, exactly the parameters
to compare; every other parameter that class declares is disabled via
`enable_parameter(name, false)` on both sides before the comparer runs:

```json
{
  "options": { "compare_parameters": { "NFET_01V8": ["W", "L"] } }
}
```

```json
{
  "category": "device.parameter_excluded",
  "severity": "warning",
  "description": "options.compare_parameters scoped device class 'NFET_01V8' to compare only ['L', 'W'] -- 'AS' was excluded from this compare and is NOT verified; a 'match' does not confirm the two sides agree on it (see docs/cli/lvs.md, 'device.parameter_excluded')",
  "side": "both",
  "net": null,
  "device": {"layout": null, "reference": null, "class": "NFET_01V8"},
  "property": null,
  "details": {"parameter": "AS", "compared_parameters": ["L", "W"]}
}
```

Notes on the semantics:

- **One entry per excluded parameter, not per device instance.** Unlike
  `device.parameter_tolerated` (one entry per snapped device *pair*), this is
  a request-side compare-scoping decision applied once per named class — the
  entry's `device` block names the class, not an instance pair (`layout`/
  `reference` are both `null`, matching `device.placeholder_value`'s own
  class-level disclosure shape), and `side` is always `"both"` since the
  parameter is disabled on both netlists' own class object.
- **It removes the parameter from the compare — it does not reconcile a
  value for it.** Unlike `reference.device_bulk`'s terminal reconciliation
  (which supplies a caller-asserted net for a missing terminal), this option
  does not compute or synthesize anything: the excluded parameter simply does
  not participate in `NetlistComparer.compare()` on either side, for the
  named class, at all. Teaching one side to state the missing parameter
  correctly — the narrower, per-case fix — is explicitly out of scope; this
  is the generic escape hatch for when that narrower fix is not available.
- **Typos are never a silent no-op.** A device-class name that resolves on
  **neither** netlist, or a parameter name not declared by that class on
  either side it does resolve on, is an application error (exit 1) naming the
  typo and what is actually available — the same "typo must be visible"
  discipline `hints.same_nets` and `options.combine_devices`'s array form
  already apply. A silently-ignored typo here would be worse than usual: it
  would look exactly like a device class whose every parameter happens to
  agree.
- **A class present on only one side is accepted**, scoped on that side
  alone — mirrors `options.combine_devices`'s own "present on just one side
  is not an error" rule (a layout-only parasitic flavour or a reference-only
  lumped model is legitimate).
- **The default is unchanged.** Omitting the option (or passing `null`) skips
  the mechanism entirely — every parameter every device class declares is
  compared exactly as before this option existed.
- **`"engine": "netgen"` raises** (application error, exit 1) rather than
  silently ignoring the option — the `netgen` engine has no equivalent
  per-parameter compare-scoping hook, the same boundary
  `options.parameter_tolerance`/`hints`/`reference.device_bulk` already draw.

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
- A PMOS entry fires when the layout has one or more PMOS devices
  (`device.class` is the deck's `pfet_class`, e.g. `"pfet"`) whose body
  terminal landed on an **anonymous, KLayout-synthesized net** — the `"$<n>"`
  placeholder `Net.expanded_name()` returns for a net no label reached — or
  on no net at all. A device whose body resolved to a real, named net from a
  drawn `tap`/`well_label` or a derived `tap_nplus`/`tap_pplus` tie is not
  counted.

  This arm used to be **deck-structural** (issue #2048 corrected it): it
  fired only when the deck declared no tap mechanism at all
  (`ExtractionDeck.tap`/`tap_nplus`/`tap_pplus` all `None`; gf180mcu before
  issue #1084), which treated *declaring* a mechanism as proof that every
  PMOS in every layout used it. It is not — a gf180mcu layout that draws no
  well tie still leaves each PMOS body on an anonymous net with no DC bias
  path, and `klt extract`/`klt pex` have always reported exactly that case
  (`unbiased_pmos_body_nets[]`, `body_bias.status: "unbiased"`; issue #555).
  The two commands now agree on the same layout. sky130 still emits no PMOS
  entry on an ordinary standard cell, but because its `well_label` (64/5)
  demonstrably names every PMOS body (e.g. `VPB`), not because the deck
  declares a mechanism.

Both entries reflect real device-level extraction outcomes (per-device for
NMOS since #490, for PMOS since #2048 — a property of what the layout
actually drew, not of which deck ran extraction, nor of any individual
device pairing or `hints`), always `severity: "warning"`, and never change
`status` or break `mismatch_count`'s error semantics — they only make it
visible, in-band, that this dimension of the compare was not fully verified
against the schematic.

##### The same condition, machine-checkable: `body_verification` (issue #1983)

A `mismatches[]` warning is not *gradeable*. Answering "were this layout's
device bodies verifiably tied?" from a committed report meant string-matching
a `category` inside an array whose other entries are ordinary compare
findings — so in practice nothing downstream asked, and a record carrying
the warning was indistinguishable, at every consumer that reads only
`status`, from one that did not.

The top-level **`body_verification`** block (always present) states it as a
field:

```json
"body_verification": {
  "status": "unverified",
  "reason": null,
  "device_classes": ["nfet"],
  "device_count": 2,
  "findings": [{"class": "nfet", "device_count": 2}],
  "finding_count": 1
}
```

| Field | Type | Meaning |
|---|---|---|
| `status` | string | `"verified"` — a deck was given and every MOS body terminal in the layout's top circuit resolved to a real drawn/derived net. `"unverified"` — at least one did not (the `device.body_unverified` condition above). `"unchecked"` — no `request.layout.deck` was given (the pre-extracted `request.layout.netlist` form), so nothing establishes this layout's tap convention and this run verified nothing about the bodies either way. |
| `reason` | string \| `null` | Why the question could not be answered, for `status: "unchecked"`; `null` otherwise. |
| `device_classes` | array\<string\> | The deck device-class names with unverified bodies (e.g. `["nfet"]`), sorted. Empty for `"verified"`/`"unchecked"`. |
| `device_count` | integer | Total unverified device count across those classes. `0` for `"verified"`/`"unchecked"`. |
| `findings` | array\<object\> | One `{"class", "device_count"}` entry per affected device class, class-sorted. |
| `finding_count` | integer | `len(findings)`. |

It is rendered from the *same* determination as the `device.body_unverified`
warnings above, so the two can never disagree.

**It also agrees with `klt pex`'s `body_bias` block** (issue #2048). Both
now apply the same per-device test to the same layout — a PMOS body on an
anonymous, KLayout-synthesized net — so a floating well tie that shows up as
`body_bias.status: "unbiased"` on the post-layout artifact
([`pex.md`](pex.md)) can no longer be reported as
`body_verification.status: "verified"` here. The two spell the same
anonymous net differently (`klt extract`/`klt pex` report the
backslash-escaped `\$<n>` that matches the written netlist's node spelling,
issue #1162; `klt lvs` works from KLayout's raw in-memory `$<n>`), but they
describe the same devices.

**`"unchecked"` never means "verified".** Before this block existed, the
*absence* of a `device.body_unverified` warning meant "checked and clean" on
an inline extraction and "not checked at all" on a pre-extracted netlist,
and nothing in the report told those apart.

**This changes no verdict.** `status`, `mismatch_count`, `error_count` and
the category counts are exactly what they were — a layout with unverified
bodies still reports `status: "match"` when the compare matched, and the
warnings are still `severity: "warning"`. `klt signoff` surfaces
`body_verification.status` on an `lvs` check (`detail.body_verification_
status`) but does not grade on it; see [`signoff.md`](signoff.md) and
[`../design-evidence-tiers.md`](../design-evidence-tiers.md) item 7 for why
disclosure rather than hard-fail, and for the downstream consequence an
untied body has for a post-layout `klt pex` citation.

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
did not fully apply on that side. The entry itself is always a `"warning"` and
never contributes to `error_count` — but see "Symmetric degrade" below for
what its *presence* does to `status`.

**Identifiers (issue #1370).** The entry carries the failing attempt's own
machine-readable identifiers, not just prose:

| Field | Value |
| --- | --- |
| `circuit` | `{"<side>": "<circuit name>", "<other side>": null}` — the circuit KLayout named in its own error text. |
| `device` | `{"<side>": "<device name>", "<other side>": null, "class": "<device-class name>"}` when KLayout printed a device name *and* that device is still resolvable in the named circuit. KLayout frequently reports an **empty** device name (the device is mid-removal when the invariant trips), in which case the instance name is unrecoverable but the class often is not: when exactly one device class present in that circuit declares a terminal with the reported name, the entry reports `{"<side>": null, "<other side>": null, "class": "<that class>"}`. When even that is ambiguous, `device` stays `null` — never guessed. |
| `net` | `{"<side>": "<net name>", "<other side>": null}` — the net wired to the reported terminal of the named device. `null` when the device itself could not be resolved. |
| `details` | `{"terminal": "<terminal name>", "klayout_error": "<KLayout's raw message>"}` — the terminal KLayout said was still connected, plus the unparsed original for anything this table does not structure. |

**Symmetric degrade and `status: "inconclusive"` (issue #1370).** When the
retry budget below is exhausted on a side, that side is left partially folded
while the other may be fully folded — comparing those two directly is not an
apples-to-apples comparison, and every resulting `device.property` /
`device.unmatched` finding is cascade rather than a real design difference (the
failure mode issue #1370 was filed against: 362 mismatches against a reference
that had not changed). So `klt lvs` rolls **both** netlists back to snapshots
taken before any combining ran, and compares those instead — the exact state
`options.combine_devices: false` would have produced on both sides, including
`counts.nets.*` (the post-combine interior-net purge is skipped too, since
nothing was folded). This covers the asymmetric case as well: if the layout
combined cleanly and the reference did not, the layout's fold is rolled back
too.

Because the compare the caller asked for never ran, an engine verdict of
`"mismatch"` on that degraded compare is reported as `status: "inconclusive"`
(exit `4`) rather than as a design mismatch — a folded layout compared
uncombined against a lumped reference differs *by construction*, so that
verdict says nothing about the design. A `"match"` is deliberately **not**
downgraded: an uncombined compare that still matched is strictly stronger
evidence than a combined one would have been (the fold turned out to be
unnecessary), and this command never re-derives a verdict the engine did not
reach. Either way the `device.combine_incomplete` warning stays in
`mismatches[]`, so a caller can always see that combining did not apply.

A caller who hits this repeatedly on the same design should reach for the
array form of `options.combine_devices` (see its field-table row above) to
scope combining away from the device class whose partial-match group trips the
error.

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
safely` retries the combine, per side, against up to
`options.combine_devices_max_attempts` (issue #1412, default `5`) independent
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

**Exhaustion rate scales with netlist size and shape — the default budget's
derivation does not generalize (issue #1412).** The ~1-in-30,000 figure above
was derived from a single fixture: one multi-finger NMOS device (~200 gate
fingers), one device class. A caller re-running the shipped mitigation 7
times against an *identical* ~8,600-device, 8-device-class merged netlist
(no code/manifest change between runs — layout composed from several
independently-authored sub-blocks plus a PDK-shipped pad-ring library with
many structurally-interchangeable ESD/protection device instances) reported
`device.combine_incomplete` firing in 2 of 7 runs (~29%) at the default
`combine_devices_max_attempts: 5` — nowhere near what the small-fixture
derivation would predict even generously extrapolated. The 7 runs also did
not converge on one outcome: 4 landed on one reproducible `mismatch_count`, 2
landed on a second, and a 7th matched neither cluster. This is a **reported
observation from a caller's own design**, not a measurement this repository's
own test fixtures reproduce — it has not been independently re-measured
here, and is documented as reported rather than verified. Two candidate
explanations, not distinguished by that observation alone: a design with
more/larger partial-match device groups (e.g. many structurally-identical
pad-ring cells) may have a materially higher single-attempt failure rate than
the ~20% the default budget was tuned against, so a fixed retry count leaves
a much higher compound failure probability for that class of design than the
~1-in-30,000 figure advertises; or repeated `Netlist.dup()` calls made in the
same process may be less independent samples of the heap-address-dependent
ordering than the derivation assumed (e.g. allocator arena reuse across
same-shaped allocations) — neither is confirmed. **Practical guidance:**
there is no retry-budget value this document can recommend generically —
the exhaustion rate is netlist-size/shape-dependent, and a budget tuned
against a small fixture is not a reliable predictor for a large,
multi-class, structurally-repetitive design. A caller building an automated
gate around a large/complex netlist should raise
`options.combine_devices_max_attempts` well above the default and expect to
tune it empirically against their own design rather than trust the default's
derivation, and should treat a `status: "inconclusive"` result as requiring
investigation (or a re-run) rather than as a rare, safely-ignorable edge
case.

**Every `category_counts` key can be volatile under `options.combine_devices`,
not only `device.unmatched` (issue #1412).** The same 7-run comparison found
every `category_counts` key varying across runs, not just the
`device.unmatched`/`device.property` cascade `device.combine_incomplete`'s
own "Symmetric degrade" section above calls out. A caller building an
automated `klt lvs` gate around a large/complex netlist with
`options.combine_devices: true` should treat **every** `category_counts`
entry as potentially volatile run-to-run, not only the categories this
document happens to call out by name elsewhere — gate on `status` (and, for a
`status: "inconclusive"` result, on the presence of `device.combine_incomplete`
in `mismatches[]`), not on an exact `category_counts`/`mismatch_count` value
matching a previously-committed report.

#### `device.combine_parameter_corrected`: `options.combine_devices` produced a capacitor with a wrong `C` and it was corrected

Only possible when `options.combine_devices: true` (issue #1497). This is a
**distinct failure mode** from `device.combine_incomplete` above: that
category covers KLayout's own `Netlist.combine_devices()` raising an
unhandled `RuntimeError` on a partial-match device group (issue #466/#1185) —
an exception `klt lvs` can catch and retry against. This category covers the
opposite shape: `combine_devices()` returns **normally**, the device topology
folds correctly (exactly one capacitor device remains per parallel group,
with the expected terminal connectivity), and the group's secondary `A`
(area) and `P` (perimeter) parameters are correctly summed across the group —
but the primary `C` (capacitance) parameter is sometimes left at a single
pre-combine instance's own value instead of the group's summed total. No
exception is raised, so the existing `device.combine_incomplete` retry
mitigation (`options.combine_devices_max_attempts`) has no signal to act on:
the call simply "succeeds" with a wrong number.

Because a parallel capacitor's `C` is mathematically a simple per-device sum
(the same rule `klt extract`'s `DeviceExtractorCapacitor` deck formula relies
on: `C = area_cap_f_um2 * A + perim_cap_f_um * P`), the total `C` summed
across every capacitor device that shares one (circuit, terminal-connectivity)
identity is a quantity `combine_devices()` can only redistribute across fewer
surviving devices, never change. `klt lvs` checks that invariant directly: it
snapshots each side's netlist before `combine_devices()` runs (the same
snapshot issue #1370's symmetric degrade already takes), and after a
successful combine compares every resulting capacitor device's `C` against
its own pre-combine group's summed total. Wherever they disagree beyond
floating-point tolerance, `klt lvs` overwrites that device's `C` with the
correct pre-combine sum **in place** and records one `severity: "warning"`
`device.combine_parameter_corrected` entry per affected side, naming every
corrected device (and its before/after `C` values) — so a caller relying on
the combined value can see that a correction happened rather than silently
trusting a value KLayout itself got wrong.

This never changes `status`: the value is corrected before comparison
proceeds, so a run whose only finding is this entry still reports
`status: "match"` with a nonzero `mismatch_count`, exactly like
`device.bulk_reconciled`, `device.parameter_tolerated`, and
`topology.flattened` above. It also never triggers the `device.combine_
incomplete` symmetric-degrade rollback (issue #1370): that check runs, and
`status: "inconclusive"` is decided, before this correction pass — a
correction is a successful combine that needed a follow-up fix, not a failed
one.

**Reproducing the underlying KLayout behavior is not required for this
mitigation to apply.** The invariant check above is unconditional — it runs
on every `options.combine_devices: true` request with capacitor devices,
correcting a mismatch whenever KLayout's own combine produces one, regardless
of whether the specific failure shape can be forced on demand. In practice it
was observed reliably (10/10 repeat calls) against one real ~1000-device/
~20-group `DeviceExtractorCapacitor`-produced netlist, but neither that
report's own reduction attempt nor this project's own investigation could
force it from a from-scratch synthetic netlist built directly via the
`klayout.db` device/circuit API at a comparable scale — consistent with
`options.combine_devices_max_attempts`'s documented root cause for
`device.combine_incomplete` above (KLayout's internal grouping keyed on raw,
process-heap-address-dependent `db::Net*` pointer values, not on netlist
content, and therefore not reliably reproducible from a fresh Python
process). A caller should not read the absence of a live reproduction as
evidence the underlying KLayout behavior does not exist — only this
mitigation's own presence protects against it either way.

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

#### `topology.power_only_pruned`: a power-only layout circuit was removed before comparing

Only possible when `reference.form: "gate-level-verilog"` (issue #1622), and
only emitted when at least one layout circuit declares nothing but
power/ground pins.

A `gate-level-verilog` reference conversion never instantiates a cell with
no logic function — a filler cell (`sky130_fd_sc_hd__fill_*`, inserted
unconditionally whenever issue #1442's row-rail fallback fires) or a tap
cell (`sky130_fd_sc_hd__tapvpwrvgnd_1`, inserted unconditionally by
`klt place-and-route`'s `tapcell` stage) has nothing on the reference side
to describe it. `klt lvs` removes every such layout circuit, along with
every subcircuit instance of it, before the compare runs. A circuit with
even one pin not established as power/ground is never pruned, even if it
also has power pins (see "Negative controls" above for this module's
general discipline on not masking a real defect).

**How "power/ground pin" is decided.** Not by cell name, and not from a
hardcoded PDK power-pin table (`VPWR`/`VGND`/`VPB`/`VNB` for sky130 vs.
gf180mcu's `VDD`/`VSS`/`VNW`/`VPW`) — it is derived, per run, from two
things `klt lvs` has already read:

1. `reference.library`'s own `.subckt` declarations give each standard
   cell's **full** pin order, signal and power/ground alike.
2. The Verilog conversion emits, for each cell the reference instantiates,
   only the pins the Verilog connects — structurally never a power/ground
   pin.

So for a cell the reference *does* instantiate, every pin the library
declares but the reference does not carry is a power/ground pin. The
power-pin set is that difference, taken over exactly the cells the
reference instantiates, minus every pin name the reference carries anywhere.

**Restricting it to cells the reference instantiates is what keeps it
sound.** A cell the reference never mentions tells you nothing about its own
pins: a stray `sky130_fd_sc_hd__dfxtp_1` in the layout has pins
`CLK`/`D`/`Q` that appear nowhere in a reference built only from inverters
and buffers, yet they are plainly signal pins and that stray flip-flop is a
real missing-cell defect. Pins are therefore only ever admitted as
power/ground on the evidence of a cell the reference actually instantiates.

**Scope and limits.** The pruning also covers, deliberately, other purely
physical cells a P&R flow inserts without the logic netlist knowing —
decoupling capacitors, whose PDK pin list is supplies and well ties only —
which are equally invisible to a signal-only compare and equally not a
topology defect. It stops where the evidence stops: a physical-only cell
that declares any pin not established as power/ground (sky130's antenna
diode, whose pin list includes `DIODE`, is the usual example) is **not**
pruned and still reports a `topology` mismatch. That is the intended
direction of error — an un-pruned cell costs you a reported mismatch, a
wrongly-pruned one would hide a real defect. It is applied **only** when
`reference.form: "gate-level-verilog"`, whose conversion is known never to
carry power pins and which is the only form with a resolved
`reference.library` behind it; a `"plain-element"`/`"subckt-call"` reference
is arbitrary SPICE with no library to derive anything from, so the same
inference is never made there. Every removal is named in this entry's
`description`, so a `"match"` that depended on one is always auditable from
the report alone.

`severity` is always `"warning"` — this is a request-side transform applied
before the compare, not a `NetlistComparer` finding, so it never changes
`status` on its own (a request whose only finding is this entry reports
`status: "match"` with a nonzero `mismatch_count`). `side` is always
`"layout"`. `description` names every circuit removed. Present for the same
reason `topology.flattened` is: a `"match"` reached after this pruning is
never silently indistinguishable from one reached against the layout
netlist's original, unpruned shape.

Removing the instance — not just leaving it in place and filtering its own
finding out of `mismatches[]` — is what makes this work at all. `status` is
always derived from the comparer's own boolean result, never re-derived from
`mismatches[]` (see the module's docstring), so a report-level filter would
leave `status: "mismatch"` regardless. And it is not the only finding: left
in place, a power-only circuit's *parent* fails to verify too —
`NetlistComparer` cannot pair the parent's subcircuit-instance list against
the reference's while one side has an extra instance the other cannot
describe, and reports a second, consequential `topology` "circuit could not
be matched to a counterpart" finding for the *parent*.

This mirrors the known-safe downstream workaround issue #1622 cites
(2AMLogic/sky130-fpga issue #20: stripping a layout-side `.SUBCKT` whose pin
list is a non-empty subset of `{VPWR, VGND, VPB, VNB}`, plus its instance
lines, from the extracted SPICE netlist before calling `klt lvs`) —
implemented natively here, and derived from the library rather than from a
fixed pin-name set, instead of requiring a caller to pre-filter their
netlist.

#### `topology.reference_port_alias_joined`: a reference port declared only via `assign` was joined onto its target's net

Only possible when `reference.form: "gate-level-verilog"` (issue #2021), and
only emitted when the reference declares a port whose only Verilog-level
connection is a plain `assign <port> = <net>;` alias — routine output of
synthesis whenever a module port is driven directly by another net or port,
e.g. `assign dbg_uart_byte[i] = rx_byte[i];` (a debug/monitor tap port
carrying the same node as a "real" signal port).

**The gap this closes.** The gate-level-Verilog-to-SPICE conversion resolves
an `assign` alias transparently for every *instance* connection — a net
used as `.PORT(<aliased net>)` reads back as its ultimate target — but never
for a module's own declared port list. An aliased port is therefore emitted
as its own `.SUBCKT` pin, with nothing inside the body ever referencing it
(every instance that would have used it was rewritten to the alias's target
instead), which reads back as an isolated, disconnected reference net even
though the layout has exactly one physical net serving both names. Before
this fix, that could surface as a false `pin.unmatched`/`net.unmatched`
finding on a design that is electrically correct — the same "making the
layout more correct makes the report worse" failure shape issue #1994
describes, one layer up in the comparison rather than in extraction (that
issue's own tie-cell/VPWR half of the same investigation).

**The fix.** `klt lvs` joins the alias port's net onto its canonical
target's net (following a multi-hop `assign` chain to its ultimate target,
same as the instance-connection resolution above) before the compare runs —
both port names stay individually declared, now pointing at the same net,
matching a correctly-wired layout's own "one net, two named pins" shape.
Only a port whose value comes purely from an `assign` is ever joined; a
genuinely unconnected or differently-wired reference port is untouched and
still reports as a real mismatch.

`severity` is always `"warning"` — this is a request-side transform applied
to the reference before the compare, not a `NetlistComparer` finding, so it
never changes `status` on its own (a request whose only finding is this
entry reports `status: "match"` with a nonzero `mismatch_count`). `side` is
always `"reference"`. `circuit.reference` names the module the alias was
declared in; `description` and `details.canonical_net`/
`details.aliased_ports` name the target net and every alias port folded
into it. Present for the same reason `topology.power_only_pruned` is: a
`"match"` reached this way is never silently indistinguishable from one
reached against the reference's original, unresolved port list.

#### `combine_devices_per_circuit.unmatched`: an `options.combine_devices_per_circuit` glob matched no circuit

Only possible when `options.combine_devices_per_circuit` is given (issue
#1552). One entry per glob pattern that matched zero circuits on a given
side — most commonly a typo, or a pattern written in the request's own
source-file case instead of the upper-cased form `NetlistSpiceReader` reads
circuit names back as (a `.subckt macroa` declaration reads back as circuit
`"MACROA"`; see `options.combine_devices_per_circuit` above).

`severity` is always `"warning"`, never `"error"` — unlike
`options.combine_devices`'s list-shape validation (which rejects a class
name absent from *both* sides outright, since there is no legitimate reason
to name one), a pattern here can legitimately name a circuit that exists on
only *one* side (e.g. a reference-only lumped macro with no layout-side
counterpart yet), so this only warns. `side` is `"layout"` or `"reference"`
(matched independently per side — a pattern can be unmatched on one side and
matched on the other), and `details.pattern` carries the offending glob
verbatim.

#### `topology`: catch-all for circuit-, device-class-, and net-identity-level mismatches

`topology` is the category for structural findings that don't fit any of the
narrower `net.*`/`device.*`/`pin.unmatched` categories: a whole circuit or
subcircuit instance with no counterpart, a device class with no counterpart,
an ambiguous net pairing the comparer resolved on its own, or two nets paired
despite a name/identity conflict. Six classification sites inside
`_build_mismatches`/`_classify_net_mismatch_pool`
(`src/klayout_tools/lvs.py`) each map one `NetlistComparer` event kind to a
`topology` entry:

- **Circuit mismatch** (`lvs.py:3726-3740`, from `logger.circuit_mismatches`)
  — a circuit (module) on one side has no counterpart on the other. Always
  `severity: "error"`. Names the circuit in `circuit` (issue #1132) —
  `instance`/`subcircuit` stay `null` (there is no instance; the whole
  circuit definition has no counterpart).
- **Subcircuit mismatch** (`lvs.py:3742-3768`, from
  `logger.subcircuit_mismatches`) — a subcircuit instance has no
  counterpart. Always `"error"`. Names the containing circuit (`circuit`),
  the instance itself (`instance`), and the circuit it instantiates
  (`subcircuit`) (issue #1132), so a macro-scale report attributes *which*
  instance failed to pair without a side-channel netlist diff.
- **Device-class mismatch** (`lvs.py:3770-3808`, from
  `logger.device_class_mismatches`) — a device class (e.g. `nfet`)
  registered on one side has no counterpart class on the other side.
  Downgraded to `"warning"` when that side's netlist has zero actual
  instances of the class (`klt extract` always registers both
  `nfet`/`pfet` device classes even when a layout only instantiates one
  polarity, so an all-`nfet` layout compared against an all-`nfet`
  reference is not a real defect); `"error"` when the class has one or
  more real instances.
- **Ambiguous net pairing** (`lvs.py:3810-3855`, from
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
- **Net identity conflict with no leftover** (`lvs.py:4738-4753`, inside
  `_classify_net_mismatch_pool`) — two nets were paired despite a name/identity
  conflict, and neither side has an accompanying one-sided leftover net
  (the merge/split case documented below, which absorbs the same
  underlying event when a leftover is present). Always `"error"`. The
  differing names are the *label* on this entry, not its cause:
  `NetlistComparer` pairs differently-named but topologically identical nets
  with no finding at all, so this entry always means the two nets are not
  topologically identical, and the real difference is reported by another
  entry in the same `mismatches[]`. A `hints.same_nets` entry cannot clear it
  — see "`hints.rejected`" below. Suppressed for a pair the caller declared
  via `hints.same_nets` (issue #1484), because that declaration already
  produces a narrower `hints.rejected` entry for the same comparer event.

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

A seventh entry (`lvs.py:1053-1061`) is a safety net, not a classification site: if
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

**Two refusal shapes, two `description` texts (issue #1484).** A declared pair
can be refused in either of two ways, and the `description` says which:

- *The comparer never associated the two nets at all* — "…the comparer did not
  confirm it as a topological match". The declared correspondence is simply not
  one the comparer can make.
- *The comparer associated the pair and then flagged it* — "…the comparer
  associated the two nets and found them not identical topologically". The two
  nets **are** each other's counterparts, but something structural about them
  differs, so the `must_match=True` assertion still fails. **The real
  difference is reported separately in the same `mismatches[]`** (a
  `device.property`, `device.unmatched`, `net.*` entry elsewhere in the same
  circuit); fix that and the hint becomes unnecessary. Re-declaring the hint,
  or restating it differently, cannot resolve this shape.

**A `hints.same_nets` entry cannot clear a `topology` "name/identity conflict"
entry — and it does not need to.** `NetlistComparer` pairs two *topologically
identical* nets whose names differ on each side with **no finding at all**: it
does not compare net names. The composition pattern where a flattened
hierarchical reference calls a node `fb` and the flat layout calls it `out`
therefore needs no hint. It follows that a `topology` "nets were paired despite
a name/identity conflict" entry only ever arises for a pair that is *not*
topologically identical — the second refusal shape above — so the fix is always
the underlying structural difference, never the hint. Declaring the hint for
such a pair does not make the report worse (the narrower `hints.rejected` entry
*replaces* the `topology` duplicate rather than being added alongside it, since
both describe the one comparer event), but it does not make it better either.

Where `hints.same_nets` *does* pay off is the ambiguous-pairing case: a
symmetric structure the comparer resolves on its own and reports as a
`topology`/`"warning"` entry (see "Ambiguous net pairing" above). Declaring the
correspondence there removes the ambiguity — and the warning — outright.

#### Net-merge/net-split classification (a documented simplification)

`NetlistComparer`'s own event stream does not label a net mismatch as
"merged" or "split" — it only reports individual net-pairing events. This
command distinguishes the three net categories from the *pattern* of
co-occurring events: an isolated, one-sided unmatched net (no counterpart on
the other side, and nothing else nearby) is `net.unmatched`. When a
one-sided leftover net on the **layout** side co-occurs with a differently-
named net pairing, it is classified `net.split` (a reference net's role
divided across more layout nets than expected); the mirror case (a leftover
on the **reference** side) is `net.merged`. This heuristic is verified
against synthetic single-defect merge/split fixtures in `tests/test_lvs.py`,
but — like `klt extract`'s documented curated-deck connectivity limits — is
not a formal proof for every possible multi-defect input; a compare run with
several independent net defects at once may classify some of them
generically (`topology`) rather than precisely.

**"Co-occurring" means *in the same weakly-connected component*, not
"anywhere in the same compare run"** (issue #1533). Composing a second,
electrically unrelated block into the same top circuit — via `klt
gen-compose` or otherwise — does not change how the first block's own nets
are classified, as long as the two share no connectivity and no net the
comparer pairs across them: each block's events are pooled and matched
separately. Before this scoping, one unrelated block contributing a single
renamed net pairing was enough to rewrite every isolated `net.unmatched`
elsewhere in the run as `net.split` — and, since the merge/split branch does
not apply the `device.property` collateral downgrade (see "Negative
controls"), to promote an already-tolerated `severity: "warning"` finding to
`"error"` with no change to that block's own geometry or connectivity.

The components are taken over the layout and reference netlists' own
device/subcircuit connectivity, *glued together along the pairings the
comparer itself made*. That gluing is what keeps a genuine split classified
as `net.split`: splitting a net severs its fragments' layout-side connection
by construction (a broken series chain leaves two disconnected layout
pieces), but both fragments still reach the single reference net they came
from through the surrounding matched nets, so they stay in one pool.

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
`provenance.input.content_hash` (issue #1969) gets no `checks[]` entry of its
own: it is by construction the `sha256:`-prefixed form of the very digest
`environment.layout_sha256` records for the very same file, so a moved layout
is already a hash-integrity **failure** via that entry — it is never silently
ignored, and a second entry would only report the same drift twice.
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
  ],
  "advisories": []
}
```

(A `provenance.deck.content_hash` entry is appended to `checks[]` only when
the original run used an extraction deck.) `status` is `"match"` only when
every `checks[]` entry's `match` is `true` — a report with no recorded hash
to compare against always renders that check `match: false`, never a false
pass.

**`advisories` (issue #1373):** a list, always present (possibly empty), of
non-fatal notices that the *engine itself* differs between the committed
report and the one currently running `--check` — the input hashes above can
all still be `[OK]` while a fresh compare against the same inputs would now
use a different `klt`/KLayout build. Each entry names one differing
`provenance.klt_version`/`provenance.klayout_version`:

```json
"advisories": [
  {
    "field": "provenance.klt_version",
    "report": "0.3.0+g634e074ff484",
    "current": "0.3.1+gabc1234"
  }
]
```

This is purely informational — it **never** affects `status` or the exit
code, and is never folded into `checks[]`'s `[OK]`/`[DRIFTED]` list, so a
scripted gate that greps that list for a failure marker does not start
matching on it. `provenance.pdk.version` is deliberately excluded from this
check: unlike the tool build, a PDK release routinely differs by design
across machines/CI runners, so including it here would make the advisory
noisy rather than informative (`pdk` is always `null` for LVS reports in
practice regardless). A version missing from either side — an older report
predating these fields, or an unresolvable current version — is treated as
"unknown", not "drift", and is silently omitted rather than reported. Text
output (`--format text`, the default) renders each entry as `[DRIFT]
provenance.klt_version: 0.3.0+g634e074ff484 (report) vs 0.3.1+gabc1234
(current)`, printed after the `checks[]` lines.

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
(`provenance.klt_version`/`klayout_version`/`pdk.version`; `pdk` is `null`
for most LVS runs, populated only for a `"gate-level-verilog"` reference —
see the `provenance` row above), plus one LVS-only exclusion (issue #1223):
`environment.extracted_netlist`. That
field is populated only when the *original* request set
`options.keep_extracted: true`, and its value is a path anchored to the
original request document's own directory — `--rerun` never re-asserts
`keep_extracted` (an output-side flag that cannot change a verdict, and
re-running it would write files as a side effect), so a fresh rerun always
reports it as `null` even when nothing else about the compare changed. This
exclusion is scoped to `klt lvs --rerun` only; `klt drc --rerun` has no
analogous output-path field. (A report committed before issue #1205 also has
`reference_top`/`options` excluded — see below.) Response shape mirrors `klt
drc --rerun`'s:

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
`layout.top_cell_pins`/`declared_pins`/`pin_source_cells` are never echoed
anywhere in the response and are always reconstructed as each option's own
default. Use cheap mode instead when any of these apply to the original
request.

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
| `4` | Ran, but the compare the request asked for could not be performed, so no verdict about the design was reached (`status: "inconclusive"`, issue #1370). The documented payload is still on stdout. Never `0`, and never folded into `3`. |

`0`/`1`/`2`/`3` are `klt lvs`'s direct analogue of `klt drc`'s split — LVS is
a clean/dirty verdict like DRC, so a normal run has the same two success
outcomes DRC has, and `3` is DRC's "ran clean but found findings".

`4` (issue #1370) is `klt equiv`'s `EXIT_INCONCLUSIVE` reused verbatim — the
same numeric value, for the same reason: a run that could not reach a verdict
must not be reported under one of the two verdict codes a gate keys on. It has
exactly one cause, `options.combine_devices` exhausting its retry budget (see
that option's field-table row and "`device.combine_incomplete`" above); a
request that does not set `options.combine_devices` can never produce it, so
every previously-shipped `klt lvs` invocation keeps its original `0`/`3`
behaviour. `klt equiv`'s own version of this outcome is documented in
[`docs/cli/equiv.md`](equiv.md)'s "Timeout and the inconclusive verdict".

**The exit code reflects the *signal* verdict only — `power_connectivity`
never changes it** (issue #1952). A run whose signal compare matched but
whose power/ground check found a defect exits `0` with `status: "match"` and
`power_connectivity.status: "mismatch"`. This is deliberate and is the same
additive discipline the field itself follows: the check is on by default, so
folding it into the exit code would silently turn a previously-green CI gate
red on a design the tool has never checked before (including on a genuinely
multi-domain design, where the consistency invariant does not hold — see
"Power/ground connectivity" above). **An automation gate that wants full LVS
on a digital block must read the JSON and assert both:**

```bash
klt lvs req.json --format json > report.json || exit 1
jq -e '.status == "match" and .power_connectivity.status == "match"' report.json
```

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
