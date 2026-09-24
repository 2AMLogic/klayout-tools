# Changelog

`klt` has not yet reached `1.0`; per [`docs/json-contract.md`](docs/json-contract.md),
`schema_version` only bumps for non-additive (breaking) shape changes to a
command's own payload, and is versioned **per command** — so one command's
breaking change never forces a bump on another, nor on the package version.
Additive behavior changes — including new `mismatches[].category` values
`klt lvs` can emit — land within a package version and are recorded here
instead. This file is the source of truth for which categories exist as of a
given date; pin `provenance.deck` (sha256) and `provenance.klayout_version`,
not `klt --version`, if you need to detect this kind of drift. See
[`RELEASING.md`](RELEASING.md)'s "Release cadence" section for when the
`## Unreleased` entries below actually ship to PyPI.

## Unreleased

- **Added** (#2421, `klt lvs`, additive — **no** `schema_version` bump: one
  new `mismatches[].category` value, no new request field and no change to
  any existing field's value for any input that does not trigger it): a new
  `severity: "error"`, `side: "reference"` category
  **`device.class_family_unsatisfiable`**, reported when the reference
  netlist instantiates **two or more** device classes that the layout-side
  deck derives from one and the same drawn geometry and tells apart only by
  a whole-run deck option — gf180mcu's `poly_res` family (`ppolyf_u_1k`/
  `_2k`/`_3k`, one drawn `Resistor`-marked poly segment at three sheet-rho
  interpretations) being the worked example, with `mim_cap` and `metal_top`
  its siblings. Such a reference cannot pass LVS under *any* option value:
  the deck recognises exactly one of those names per run, so whichever is
  selected, the instances of the other class(es) are compared against a
  device class the extraction never produces. Until now that surfaced only
  as an ordinary `device.class`/`device.unmatched` mismatch, **identical for
  every option value** — so the obvious next step ("try the other value")
  reproduced the same failure, and only reading the PDK deck's own
  derivation rules revealed that the classes share one drawn layer. The new
  entry names the family, the option that selects between its members, and
  the classes the reference actually used, in both the `description` and a
  machine-readable `details` block (`{"option", "device_kind", "family",
  "reference_classes"}`). Run as a **pre-flight** check — on the reference
  as given, against the deck's own `flavour_option`/`flavours` table,
  independent of which `deck_options` value this run selected — so it fires
  identically under every value, and is entirely data-driven: a PDK that
  later declares another option-selected shared-geometry family is covered
  with no further code change. Diagnostic only, exactly like
  `device.class_arity`: it explains the mismatch the compare already
  reported and never moves `status` on its own. Requires a resolved
  `layout.deck` (the pre-extracted `layout.netlist` shape with no deck
  reports nothing), counts only classes with at least one instantiated
  reference-side device, and stays silent for a reference naming exactly one
  class of a family (the normal, supported `deck_options` case). See
  `docs/cli/lvs.md`'s "`device.class_family_unsatisfiable`" section.
- **Added** (#2389, `klt erc`, additive — **no** `schema_version` bump: one
  new `erc_coverage` key, no new spec key, no new finding kind, and no
  change to any existing field's value for any input): a new
  `erc_coverage.layers_in_stream_without_declaration` names the drawn
  `(layer, datatype)` pairs this layout carries that the cited spec declares
  **nowhere** — the inverse-direction counterpart of `klt drc`'s
  `coverage.layers_in_stream_without_rules`, and the same plain set
  difference. `klt erc` scopes its connectivity graph down to the layers the
  spec declares, and a `stackup`/`vias` entry naming a layer the layout does
  not draw is explicitly not an error; the opposite direction was invisible.
  That matters because an ERC supply spec is a committed, long-lived
  artifact (`docs/design-evidence-tiers.md` item 11) graded against a layout
  that gets re-routed under it: a rail that *moves* onto an undeclared level
  already reports loudly as extra `erc.unconnected_net` islands, but a rail
  that merely *gains* routing there while staying connected through the
  declared stack produced a clean report whose connectivity model was
  narrower than the layout, with nothing in the envelope recording it —
  forcing a consumer writing an item-11 flow to hand-roll a `klayout.db`
  pre-flight beside `klt erc` just to diff "layers drawn" against "layers
  declared". **Reported, never graded**, exactly as `klt drc`'s own coverage
  block is: a non-empty list never changes `status`/`erc_status`, emits no
  finding, and is not an input to either roll-up; `[]` means every drawn
  layer is declared. "Declared" means named anywhere in the spec — a
  `stackup[].label_layer`, `stackup[0].active_layer`, a `ties[]`
  `well_layer`/`tap_layer` with its `tap_requires`/`well_requires`/
  `well_excludes` markers, a `devices[].body_layer` — because a layer the
  spec explicitly names is not one it never mentioned, and counting it would
  put a false positive in the field of every fully-declared spec. Scoped to
  the analysed top cell's own hierarchy, like `klt drc --top`'s own
  `coverage`. With `--deck <name>` the list is narrowed to that curated
  extraction deck's conducting layers (`metals`, `vias`, `contact`,
  `active`, `poly`, plus an optional distinct `tap`/`poly_interconnect`),
  so implants, markers and wells stop reading as coverage gaps; without a
  deck there is no PDK-agnostic way to tell a conductor from a marker, so
  the unfiltered drawn-layer list is reported — noisier, and still strictly
  more information than silence. `provenance.deck` says which of the two a
  reader has. `docs/design-evidence-tiers.md` item 11 now asks a claimant to
  quote a non-empty value, on the same claimant-enforced (not
  tool-enforced) terms item 3 already applies to
  `coverage.layers_in_stream_without_rules`.
- **Fixed** (#2396, `klt extract --abstract-cells` — no JSON shape change, so
  **no** `schema_version` bump; the extracted connectivity itself changes for
  an abstracted cell that contains MiM capacitors): black-boxing a macro made
  every MiM capacitor inside it read as a hard **short between its own top and
  bottom plates**, merging otherwise-distinct *parent* nets that each reached
  one plate. `--abstract-cells` erases a matched cell's capacitor `top_plate`
  (sky130's `capm` — a device-recognition layer) but deliberately keeps its
  `top_plate_via` (`via3`), because a parent's connection to a black-boxed
  cell's pin must still reach through the cell's own local interconnect. The
  #364/#1388 exclusion that keeps such a via's DRM-required overlap with the
  `met3` bottom plate out of the deck's generic `vias[]` connectivity is
  scoped to `bottom_region.interacting(top_region)`, so with the top plate
  erased it went empty over the whole black box and the top-plate via fell
  back into the generic per-layer via loop as an ordinary `met3`↔`met4` short.
  The exclusion is now captured **before** `--abstract-cells` erasure and
  unioned into the one derived afterwards — the same pre-erasure-capture
  pattern #1911 already uses for the `nwell`/`substrate_isolation` body-identity
  cover. Because the captured region is exactly what a *flat* extraction of
  the same stream excludes, this can only withhold connections flat extraction
  also lacks: `top_plate_via` shapes are **not** erased wholesale (a declared
  pin routed through that via layer still reaches its access point), ordinary
  routing vias on the same physical layer with no top plate over them are
  untouched inside and outside a black box, and a genuine via short stays
  visible. Runs without `--abstract-cells`, and decks declaring no capacitor
  `top_plate_via`, are byte-for-byte unchanged. See
  [`docs/cli/extract.md`](docs/cli/extract.md)'s "Cell-level (black-box +
  pins) abstraction".
- **Fixed** (#2374, `klt lvs` `options.combine_devices` — additive, **no**
  `schema_version` bump: no category is added or removed, and the existing
  `device.combine_parameter_corrected` entry's `details.corrected[]` objects
  gain one new key, `parameter`): `options.combine_devices: true` could
  report a **nondeterministic** verdict — `status: "match"` on ~76% of runs
  and a false `status: "mismatch"` with a dozen `device.property` findings on
  the rest, against byte-identical inputs, the same request document, and the
  same build. KLayout's own `Netlist.combine_devices()` groups combination
  candidates in an order derived from raw heap addresses, so a freshly
  allocated `Netlist.dup()` copy walks a parallel
  `DeviceClassBJT3Transistor` array in a different order and, on ~24% of
  copies, lands in a different accumulation branch that returns **normally**
  with the array's parameter accumulation *inverted*: `AE`/`NE` (which the
  correct fold sums) left at a single instance's value, and `AB`/`PB`/`AC`/
  `PC` (which the correct fold leaves at the array's shared value) summed
  instead. No exception is raised, so #1185's retry budget had no signal to
  act on and no `device.combine_incomplete` warning was emitted. #1185's own
  mitigation made this the *normal* path: the first attempt — the one whose
  result is adopted — always ran against a `dup()` copy. Two changes:
  - The adopted combine now runs directly against the netlist itself (0/1000
    observed failures), exactly as before #1185. Only the retries — reached
    solely when that first attempt raised the internal-consistency
    `RuntimeError` #466/#1185 already degrade gracefully on — resample
    against independent copies, and those copies are taken from a pristine
    pre-combine snapshot rather than from the partially-merged netlist, so
    #1185's "every attempt is an independent trial" property is preserved.
    The retry budget, its `options.combine_devices_max_attempts` knob, the
    `device.combine_incomplete` warning, and #1370's symmetric degrade are
    all unchanged.
  - #1497's post-combine capacitor `C` sum-conservation check is generalised
    into a per-device-class parameter-conservation check covering capacitors
    (`C` sums) and bipolars (`AE`/`PE`/`NE` sum; `AB`/`PB`/`AC`/`PC` stay at
    the group's shared value), so a mis-accumulation is corrected in place
    and disclosed as a `device.combine_parameter_corrected` warning whichever
    attempt produced it — never adopted silently. The check only asserts
    against a group that actually folded to a single surviving device, and
    only for classes whose every combination is a parallel fold with a known
    rule (MOS transistors and resistors are never inspected). Each
    `details.corrected[]` entry now names the `parameter` it corrected
    alongside the existing `circuit`/`device`/`class`/`before`/`after`.
- **Changed** (#2394, `provenance.deck.options` on `klt extract`/`klt lvs`/
  `klt pex` — additive, **no** `schema_version` bump: two new keys beside an
  existing one whose *contents* broaden; no field is renamed or removed):
  `provenance.deck.options` recorded only the deck options a caller passed
  explicitly, so a run that silently took a deck default carried no
  `options` key at all — provenance-identical to a run against a deck with
  no caller-selectable options whatsoever, even though the recorded device
  class and parameter values were *one option's* answer (gf180mcu's
  `poly_res='1k'` default yields `ppolyf_u_1k` at 1000 Ω/□; the `3k`
  flavour of the identical drawn geometry is 3× that). It is now the
  **fully resolved** set — every option key the deck declares, mapped to
  the value actually wired, defaults included — and gains two siblings:
  - `options_explicit` — the same key set mapped to `true` (the caller
    pinned this value) / `false` (the deck applied its own default), so
    "pinned `poly_res=1k`" is distinguishable from "defaulted to `1k`"
    without deck-specific knowledge.
  - `options_hash` — a `sha256:` digest over the resolved *values* (never
    over `options_explicit`), so two records can be gated option-for-option
    with a string compare instead of a structural dict diff. Two runs that
    extracted identically — one pinning a value, one defaulting to it —
    hash equal. `content_hash` is deliberately unchanged and still pins the
    deck *source* only: folding options into it would break `klt deck
    resolve --content-hash`'s lookup against the released-deck history
    table for every optioned run.

  All three are omitted together for a deck that declares no selectable
  option at all (e.g. sky130), so absence of the field still means "this
  deck has no options", not "this run happened to pass none". `klt drc
  --engine klayout`'s `--deck-var` globals reach the same `options` key and
  are **unchanged** (caller-passed keys only, no siblings): an external
  `.drc` file has no declared option surface `klt` can enumerate, so there
  are no defaults to resolve and none are invented. `klt extract --check
  --rerun` / `klt lvs --check --rerun` replay only the caller-pinned subset
  — re-pinning a defaulted key would replay over exactly the
  changed-deck-default drift those modes exist to surface — and a report
  written before this change (no `options_explicit`) reruns exactly as it
  used to. `klt extract --format text`'s `deck_options:` line now marks
  each silently-applied value `(default)`.
- **Added** (#2382, `klt synthesize` request-level standard-cell exclusion —
  additive, **no** `schema_version` bump: two new optional request fields,
  one new always-present response block `cell_exclusions`, one new
  `warnings.by_category` value `dont_use_unsupported`): the `abc -dont_use`
  list was a hardcoded per-cell-library table
  (`synthesize.py`'s `_ABC_DONT_USE_GLOBS`) with no way for a request to add
  to it, replace it, or turn it off. That table is a *library*-scoped policy
  (keep non-logic cells — isolation, probe, scan — out of a mapped netlist)
  and stays correctly hardcoded; the exclusion class it cannot express is
  *corner*-scoped, where a cell family unusable at a slow corner is entirely
  reasonable at the nominal one, and `constraints.clock_period_ns` alone does
  not avoid it (ABC optimises area against its own model subject to the
  target and measurably still selects such a family). New
  `constraints.dont_use` (array of ABC cell names/globs — the same syntax the
  built-in table's entries use) is **merged with** the built-in table by
  default; new `constraints.dont_use_mode` is the explicit override —
  `"additive"` (default), `"replace"` (only the request's, requires a
  non-empty list), `"none"` (no exclusion at all, rejects a non-empty list),
  so "exclude nothing" can never be spelled the same way as "exclude only
  these". A pattern matching **zero** cells in the resolved liberty is a
  `SynthesizeError` naming it rather than a silent no-op, since a typo in an
  exclusion list otherwise changes the mapped netlist invisibly (checked
  against the liberty's own `cell (...)` group names; skipped, and disclosed
  as `cell_exclusions.validated: null`, when that cell list cannot be
  established). The new `cell_exclusions` response block records `mode`,
  `requested`, `library_defaults`, and the deduplicated `effective` list
  actually handed to ABC, so a committed report says exactly what was
  excluded instead of leaving it implicit in the tool version. On a Yosys
  build whose `abc` has no `-dont_use` (Ubuntu 24.04's 0.33) a request-level
  list degrades away through the same existing gate the built-in table
  does — never attempted anyway, which would be a hard Yosys error — but,
  unlike the table's long-standing silent degradation, that case is disclosed
  via `cell_exclusions.engine_supports_dont_use: false` plus a
  `dont_use_unsupported` warning, because the netlist then still contains the
  cells the request meant to keep out of it. Behaviour is unchanged for every
  request that supplies neither new field. The derived
  percentage-of-`clock_period_ns` exclusion rule and the
  "last implementation of a logic function" warning remain unimplemented
  follow-ups; the explicit list is the primitive.
- **Fixed** (#2373, `klt lvs --engine netgen` binary resolution — additive,
  **no** `schema_version` bump: one new always-present `environment` key,
  `netgen_binary`): the netgen engine invoked the hardcoded binary name
  `netgen`. On Debian and Ubuntu the netgen LVS tool is packaged as
  `netgen-lvs` and installs `/usr/bin/netgen-lvs` (the name `netgen` there
  belongs to an unrelated FEM mesh generator), so a host with netgen
  correctly installed from its distribution got `could not launch netgen:
  binary not found on PATH` — an error whose own remediation ("install
  netgen") was already satisfied — and callers had to shim `PATH` with a
  `netgen` symlink. The binary is now resolved, first runnable candidate
  wins: `options.netgen_binary` (new, engine-scoped — a bare name or a path
  resolved against the request file's directory, validated like
  `options.netgen_setup`, an application error when not runnable rather
  than a silent fallback), then `$KLT_NETGEN_BINARY`, then `netgen` on
  `PATH`, then `netgen-lvs`. When none resolves the error names both
  built-in candidates. `environment.netgen_binary` records the absolute
  path of the executable that produced the verdict (`null` for `"engine":
  "klayout"`, which launches no subprocess), so a committed report
  distinguishes a from-source `netgen` from a packaged `netgen-lvs`.
  Behaviour is unchanged on a host with `netgen` on `PATH` and neither
  override set. The new key is a host-local path, so it is excluded from
  `klt lvs --check --rerun`'s drift diff — re-verifying a committed report
  on a host that resolves the same comparator elsewhere (or under the other
  packaged name) is not drift in what was compared, and the exclusion also
  keeps every report committed *before* this change (which carries no
  `netgen_binary` key at all) re-running clean. `environment.engine_version`
  — netgen's own reported version, the comparator's semantic identity — is
  still diffed, so an actually-different netgen build still surfaces as
  drift. `examples/signoff/lvs.json` is regenerated for the new key.
- **Added** (#2365, `klt deck devices` + a `klt extract` `warnings[]`
  near-miss diagnostic — additive, **no** `schema_version` bump on either
  command: `klt deck devices` is a new verb with its own
  `schema_version: 1`, and `klt extract`'s `device_classes`/`warnings[]`
  keep their existing shapes — `warnings[]` is documented as an open-ended
  prose list): a curated deck's device recognition can depend on drawn
  layers that are **not** part of the device's own conductor/diffusion
  geometry — a device-recognition marker layer plus per-terminal
  `*_requires`/`*_excludes` predicates. None of that set was discoverable
  from outside the deck module's Python source, and a layout missing any
  single member extracted as `device_count: 0` with `warnings: []` — a
  result indistinguishable from "this layout legitimately contains no
  devices of that class", whose only downstream symptom is a `klt lvs`
  device-count mismatch with no cause in either tool's output. Two halves:
  - `klt deck devices --deck <name> [--class <name>]` reports, per declared
    device class, its `marker`, each terminal's `layer`/`requires`/
    `excludes`, a flattened `required_layers`/`excluded_layers` union, the
    upstream PDK-LVS `provenance` citation, and two booleans
    (`marker_gated`/`predicate_gated`) so "which classes need more than
    drawn geometry" is answerable by filtering. Every layer carries its
    `layer`/`datatype` numbers plus the deck's published name when it has
    one. An unknown `--class` is a clean error, never an empty list.
  - `klt extract` now emits one aggregate `warnings[]` entry per
    junction-diode class that recognised nothing while geometry on the
    layout satisfies every predicate except one or more members of its
    marker/`requires` set, naming each missing layer and pointing at
    `klt deck devices`. Several missing members are reported at once; a
    terminal's own declared layer is never reported (without the
    anode/cathode conductor there is no junction-shaped region), and
    candidates a MOS active island or the deck's well/substrate-tap
    geometry already explains are dropped, so an ordinary CMOS layout
    produces no near-miss lines. `excludes` layers and the `dummy` marker
    are never dropped from the search. Connectivity is unchanged — this is
    a disclosure, not a re-wiring.
  Also names five gf180mcu device-recognition layers the deck previously
  rendered as bare pairs (`diode_mk` 115/5, `RES_MK` 110/5, `SAB` 49/0,
  `Resistor` 62/0, `DNWELL` 12/0).
  Diode classes only for the `warnings[]` half; the marker-gated
  bipolar/resistor/MoM-cap families share the silent-zero shape and are
  covered by `klt deck devices` today but not yet by the diagnostic. See
  `docs/cli/deck.md`'s "`klt deck devices`" and `docs/cli/extract.md`'s
  "Marker-gated and predicate-gated device classes" sections.
- **Fixed** (#2363, `klt functional-verification` `options.sdf` on Icarus —
  additive, **no** `schema_version` bump: one new `environment.sdf.dropped`
  class key, emitted only on a run that has entries to drop, so a run with
  none is byte-for-byte unchanged): an `INTERCONNECT` entry running from a
  bit-selected top-level input port onto a pin of a **physical-only**
  instance — a router-inserted antenna-effect diode, or any filler/tapcell
  whose Verilog model has no `specify` block and no output pin — failed
  `$sdf_annotate` with `Could not find intermodpath!`, which the transcript
  gate (correctly) treats as a hard failure. This is shape (b) of the issue
  #1619/#1857 bit-selected-port failure, so neither that issue's
  safe/deferred SDF split nor #2285's alias-map normalization resolved it.
  Reproduced live against Icarus 13.0, which pinned down a condition the
  report did not state: the failure needs the port bit's net to carry more
  than one `INTERCONNECT` entry (a net whose only entry is the diode's own
  already annotated cleanly), which a real router produces by inserting one
  diode per violating pin on a multi-load input bit. Such entries are now
  removed from the SDF text handed to `$sdf_annotate` and counted under a
  new `environment.sdf.dropped` class,
  `zero_delay_physical_only_interconnect`, kept distinct from #2285's
  `zero_delay_alias_port_interconnect`. The exemption carries #2285's own
  bound verbatim: an entry is dropped only when *every* `min:typ:max` member
  of every rvalue it carries is zero, so dropping it cannot change simulated
  timing at any corner — a non-zero-delay entry onto the identical pin is
  left in place and still fails the run loudly. Destinations that *can*
  carry a modpath are untouched: an entry onto an ordinary
  `specify`-bearing standard cell (the general shape (b) case) still fails,
  and "physical-only" is decided structurally from the elaborated model
  rather than from a `*_diode_*` cell-name pattern, with every ambiguity
  (model not among `request.sources`, a top module the gate-level parser
  cannot read, any `specify`/`output`/`inout` declaration) resolving toward
  *not* dropping. The normalized copy is kept as
  `.klt/functional-verification/klt_sdf_physical_only_dropped.sdf`;
  `environment.sdf.file` still reports the caller's own SDF path. See
  `docs/cli/functional-verification.md`'s "SDF back-annotation" section.
- **Fixed** (#2362, `klt signoff --manifest` evidence keys — **no**
  `schema_version` bump; purely additive, every manifest that graded before
  grades identically): a partition-qualified evidence key
  (`"7.digital"`) was silently ignored on a manifest whose `kind` is plainly
  `"analog"` or `"digital"`. `docs/cli/signoff.md` documents
  `"<item id>.<analog|digital>"` as *the* spelling for a per-kind T1 item
  (1, 2, 5, 7, 11) without restricting it to mixed-signal blocks, but the
  lookup was keyed off the report row's `partition` field — `None` outside a
  mixed-signal manifest — so the qualified key was never consulted, the bare
  `"<item id>"` key was missing, and the item rendered
  `unmet`/`no_evidence` even though the author had cited exactly what the
  docs asked for. Evidence lookup is now keyed off the partition **being
  graded** (the block's own `kind` for a pure-kind manifest, each of
  `analog`/`digital` in turn for a mixed-signal one), so both spellings
  resolve everywhere. Only the graded partition's qualifier is ever tried,
  so a mismatched one (`"7.analog"` on a `"digital"` manifest) still matches
  nothing and falls through to the bare key rather than borrowing the other
  kind's evidence; the qualified-key-first, bare-key-fallback priority order
  mixed-signal manifests already had is unchanged, as is every rendered
  `"partition"` field. See `docs/cli/signoff.md`'s evidence-key section.
- **Added** (#2357, `klt place-and-route`, additive — **no** `schema_version`
  bump): two new fields alongside the existing
  `max_transition_violation_count`/`max_capacitance_violation_count` (top
  level, each `stages[]` `"route"` entry, and each `corners[]` entry) —
  `max_transition_violation_count_vs_library` /
  `max_capacitance_violation_count_vs_library`. The existing pair is a
  verdict against whichever of the caller's own
  `constraints.max_transition_ns`/`.max_capacitance_pf` guard-band (when
  given) or the liberty deck's own per-pin limit ends up tighter, and never
  said which one bound — a clean library result and a guard-band overrun
  read identically. The new pair re-runs the same
  `report_check_types -max_slew`/`-max_capacitance -violators` check a
  second time in the same already-paid-for sweep invocation, before any
  caller-stated constraint is applied, so it reflects only what the loaded
  liberty decks themselves declare. Reading both together — `_vs_library ==
  0` while the effective count is nonzero — distinguishes a guard-band
  overrun from a genuine library violation directly from the report,
  without reading the retained OpenROAD `stdout.log`. Identical to the
  existing pair whenever the caller sets neither constraint field. See
  [`docs/cli/place-and-route.md`](docs/cli/place-and-route.md)'s "Library-only
  counterpart" section.
- **Fixed** (#2355, `klt extract --parasitics` MoM-capacitor cards — **no**
  `schema_version` bump; `devices[].params` is unchanged, only the written
  SPICE text): an extracted `cap_cmomi`/`cap_cmomf` (MoM capacitor, issue
  #1466) device is registered as a plain, generic `kdb.DeviceClass()` — not
  a `DeviceClassCapacitor`/`DeviceClassResistor` subclass — so it had no
  curated `_CAPACITOR_MODEL_TABLE` entry and fell through
  `write_device()`'s unbound path all the way to KLayout's own default
  primitive-card writer, which emits a non-standard `PARAMS:`-keyword card
  with bare-micron geometry (`XD_$1 A B cap_cmomi PARAMS: W=40 L=40`). A
  SPICE parser under `.option scale=1` reads that as 40 *metres* — a
  ~1e6x oversize that silently models the capacitor as a near-short in AC
  analysis, with no diagnostic. The unbound path now recognises this
  device shape structurally (non-empty class name, terminals exactly
  `{A, B}`, parameters exactly `{W, L}` — mirroring
  `mom_capacitor_device_class`'s own registration, issue #1927's resistor
  fix for the analogous gap) and writes `XD_$1 A B cap_cmomi W=40U L=40U`
  instead: no `PARAMS:` keyword, and `W`/`L` unit-suffixed the same way
  this family's drawn-resistor cards already are. The SPICE writer/reader
  round trip `klt lvs` relies on (issue #1942) is updated to match:
  `netlist_capacitor_recovery._recover_mom_x_card` now scales a recovered
  card's `W`/`L` back to micrometres the same way the resistor recovery
  path already does, so a pre-extracted `layout.netlist`/`reference.netlist`
  giving `layout.deck`/`reference.deck` still recovers the correct
  `w_um`/`l_um` off the new card shape. See `docs/cli/extract.md`'s "MoM
  capacitor devices" and `docs/cli/lvs.md`'s "Custom device classes
  round-tripped through an `X` card" sections.
- **Fixed** (#2377, `klt erc`, additive — **no** `schema_version` bump: one
  new coverage skip reason, no new output field, no new coverage list; a
  spec whose `ties[]` well layer already draws geometry produces
  byte-identical output): a `ties[]` entry naming a *drawn* `well_layer`
  with **no geometry at all** in the stream — a typo'd layer/datatype, a
  PDK whose tub layer number changed, a GDS written without the tub layer —
  used to be graded as fully checked work: the per-well loop then runs zero
  times, so zero `erc.missing_tie` findings read as "every well is tied"
  rather than "nothing was there to examine". That was the one remaining
  unselected form of the absence-of-evidence pass the rest of this family
  (`degenerate_tap_declaration` #2199, `degenerate_well_assertion` #2255,
  `degenerate_well_selection` #2339) already rejects — the declared-selection
  case `well_requires`/`well_excludes` keeping *no* shapes of the layer was
  already caught; a bare `well_layer` naming an absent layer, with no
  selection declared at all, was not. Such a tie's `erc.missing_tie` work
  now lands in `erc_coverage.skipped` with reason `empty_well_region`, and
  the connectivity roll-up reports `erc_status: "clean_partial"` rather than
  `"clean"`. Never applies to an asserted substrate region (`well_layer:
  null` + `well_boxes`, #2255): boxes are validated non-degenerate at
  spec-parse time, so an asserted region can never be empty by the time this
  test runs. `klt signoff`'s T1 item 11 needed no change: it already matches
  on the `erc.missing_tie:` work-identity prefix rather than on the specific
  skip reason.
- **Added** (#2339, `klt erc`, additive — **no** `schema_version` bump: two
  new optional spec keys and one new coverage skip reason, no new output
  field and no new coverage list; a spec that uses neither key produces
  byte-identical output): a drawn well/tub layer carrying **two
  differently-biased well classes** — device-body wells strapped to one
  supply beside a vertical bipolar's base tub strapped to the other,
  ordinary for a bandgap/bias block on any PDK with a single n-tub layer —
  can now declare both, via the new `ties[].well_requires` /
  `ties[].well_excludes`. A `ties[]` entry graded *every* merged shape of
  its `well_layer` against that entry's single `net`, so whichever class
  was not declared reported a false `erc.missing_tie`, one per
  correctly-tapped well of the other class; the two declarations' finding
  sets were disjoint and together covered every well, which demonstrated
  the layout was right while leaving no single report able to say so. Every
  narrowing key before this acted on the **tap** (`tap_requires`,
  `tap_is_dedicated`, `tap_boxes`) and could not reach it — excluding the
  other class's taps leaves its wells untapped, the same finding with a
  different cause — and `well_boxes` (#2255) is rejected alongside a drawn
  `well_layer` by construction. A well shape is now kept only when it
  interacts with geometry on every `well_requires` layer and with none of
  the `well_excludes` layers, so a PDK device marker over one class selects
  it and a complementary entry naming the same marker under
  `well_excludes` selects the rest, each with its own `net`. Both keys
  ship together because the complementarity is the feature: a PDK
  generally marks the special device, not the ordinary wells. Unlike
  `tap_requires`, this **selects whole drawn shapes rather than
  intersecting the layer** — the unit `erc.missing_tie` loops over is one
  merged well shape, and a device marker generally covers only part of the
  tub it identifies (often not the tap ring), so intersecting would shrink
  the graded region below the tub's real tap and re-report the same false
  finding from the other direction. Valid only with a drawn `well_layer`
  (an asserted substrate region already names exactly what it claims, box
  by box). Falsifiability is enforced on #2199's principle: a *declared*
  selection that kept every merged shape of the layer (it partitions
  nothing) or none of them (the per-well loop examines nothing, so zero
  findings mean zero evidence) is recorded in `erc_coverage.skipped` with
  the new reason `degenerate_well_selection` (`erc_status:
  "clean_partial"`), applied ahead of the tap test. A tie with no well-side
  selector is never selection-degenerate — it claims every shape on its
  layer, which is the strongest claim available and what every spec written
  before this meant. No new coverage list: a selection narrows *drawn*
  geometry, exactly as `tap_requires` does, so it is graded as an ordinary
  geometrically-derived pass rather than under an assertion
  classification. `klt signoff`'s T1 item 11 needed no change — it matches
  skipped `erc.missing_tie` work on the work-identity prefix, not on the
  reason token — and such a block, previously able to reach only
  `supply_spec_disclosed_tool_limitation` (unmet) with every well correctly
  tapped, now reaches `met`.

- **Fixed** (#2333, `klt drc --engine klayout`, additive — **no**
  `schema_version` bump and no change to any response field; one new opt-out
  flag, `--allow-missing-host-tools`, plus its request-document field
  `allow_missing_host_tools`): a PDK's own DRC-DSL driver script carries that
  vendor's *host* assumptions, and they routinely have nothing to do with
  rule checking — a real open-PDK driver installs a Ruby `Logger` formatter
  that shells out to procps `pmap(1)` on every log line, so on a host without
  `pmap` (macOS, minimal container images) the backtick yields `""`,
  `""[10, 40]` is `nil`, and `nil.strip` raises: the deck dies on its
  **first** `logger.info`, before executing a single rule. Two defects made
  that a ~30-minute misdiagnosis rather than a one-line answer, both fixed
  here. (1) The deck-abort error quoted only klayout's `ERROR`-prefixed
  lines, so the surfaced message was a nil dereference that reads like a
  broken rule deck, while the `sh: pmap: command not found` line that
  explains it — captured, but not `ERROR`-prefixed — was dropped. The raised
  error now carries the full captured stdout/stderr (stdout first, tail-
  truncated with an explicit marker for an enormous capture, never losing an
  `ERROR` line). What counts as a *deck error* is unchanged: still the
  `ERROR`-prefix test, so a rule named `ERROR_CHECK.1` is still not misread
  as a failure. (2) There was no way to learn this before paying for the
  run. `klt drc` now scans the deck script and its literal-path Ruby includes
  for shelled-out commands (backticks, `%x{...}`, `system`/`exec`/`spawn`,
  `IO.popen`, `Open3.*`) *before* launching `klayout`, and refuses to start
  when one is missing from `PATH`: `deck requires 'pmap' (not found on
  PATH) -- shelled out to at <file>:<line>`. The scan is static and
  deliberately conservative — an interpolated/computed command, a shell
  builtin, or a commented-out one is never reported, so a shell-out it cannot
  see still fails the way it always did (now with the causal line quoted),
  and a shell-out on a branch the run never takes is skipped with
  `--allow-missing-host-tools`. klt deliberately supplies no stand-ins for
  what a driver shells out to: that does not generalise, and it would
  silently change what the run measured. Still open, and deliberately not
  addressed here: a PDK's rule tables are the portable part, and there is
  still no way to run them without the vendor's driver.

- **Fixed** (#2327, `klt lvs`'s `reference.form: "subckt-call"` conversion,
  additive — **no** `schema_version` bump, and no change to any request
  field or response shape): a reference netlist that mixes a curated
  subckt-call device (a MOS or drawn-resistor `X` card) with a
  round-tripped *custom* device class (`X D1 A B cap_cmomi PARAMS: W=4.0
  L=10.0` — IHP's MoM capacitors, the family issue #1942 taught the reader
  to recognise) now converts instead of failing, so one request compares
  both device families against an extracted layout. Two defects, both in
  `netlist_normalize.py`: SPICE's `PARAMS:` keyword carries no `=`, so it
  was tokenized as a *positional* token — and, being the last one, read as
  the subcircuit name itself, making every such card fail with `subcircuit
  'PARAMS:' is not a known device`; and even spelled without `PARAMS:`, a
  custom class is deliberately absent from the curated binding table, so
  its carried `W=`/`L=` tripped the device-like heuristic into the same
  error. `PARAMS:` is now read as the separator it is (matching
  `kdb.NetlistSpiceReader`) for every family, and an `X` card naming one of
  `reference.deck`'s own custom device classes passes through untouched —
  that family has no plain-element card to convert *to*, and the
  passed-through card is exactly what the #1942 recovery reader already
  reads as a real device. Keyed off the requested deck's declared classes,
  never a global name list, so a genuinely unknown subcircuit name is still
  the same hard error (now naming the real subcircuit rather than
  `PARAMS:`). This retires the caller-side workaround of splicing the
  custom cards out, converting the remainder, and resubmitting as
  `plain-element` — which also had to re-derive the
  `device.placeholder_value` disclosure the `subckt-call` path emits on its
  own.

- **Added** (#2016, `klt sim`, no `schema_version` bump — the response shape
  is unchanged, `environment.engine` simply may now read `"xyce"`): a Xyce
  execution path and cross-validation oracle. `engine: "xyce"` (Sandia's
  Xyce, a from-scratch SPICE implementation — a genuinely independent oracle
  for the ngspice results, pairing #5 of oracle-tracking #2007) runs DC/OP/
  TRAN analyses over process/temperature corners on the `local`/
  `local-parallel` backends, with waveforms via Xyce's ASCII rawfile.
  Everything else the engine does not implement — `corners.supply_v` (Xyce
  has no `alter`; and Xyce's `.temp` card is a silent no-op, so temperature
  rides `.options device temp=`), `monte_carlo`, `options.fail_fast_probe`,
  `remote`/`batch` — is refused up front with an error naming what is
  supported. The oracle itself (`tests/test_sim_xyce_oracle.py`, real-binary
  gated on both `Xyce` and `ngspice`) asserts ngspice-vs-Xyce agreement
  within 3e-3 relative on DC/transient/temperature fixtures, a 21-point
  sweep evidence check, and a seeded-defect negative control both engines
  must flag; measured agreement and the divergences that shaped the deck
  generator are recorded in `docs/design/xyce-oracle.md`.
  `scripts/install-xyce.sh` installs the pinned official macOS arm64 build
  (checksum-verified, smoke-tested); `.github/workflows/xyce-oracle.yml`
  runs the oracle on dispatch, off the per-PR budget. `klt size` remains
  ngspice-only.
- **Fixed** (#1157, `klt extract` bare-mode cards for bulk-bearing drawn
  resistors — **no** `schema_version` bump; the written SPICE card shape for
  3-terminal resistor classes changes, see below): a drawn-resistor class
  whose recognised terminal set includes a bulk/tap node — sky130's
  `res_high_po`/`res_xhigh_po`, gf180mcu's `ppolyf_u` family, sg13g2's
  `rsil`/`rppd`/`rhigh` — is no longer written (without `--pdk`) as the
  KLayout-shaped 3-net `R` card (`R$1 A B W 12000 res_xhigh_po`), which
  ngspice cannot parse at all: its native `R` element accepts exactly two
  nodes, so it consumed the third net and the value as the
  `<value>`/`<model>` positions and aborted with
  `unknown parameter (res_xhigh_po)` — the netlist was not a simulatable
  deck for such a layout, not merely a fidelity tradeoff. Those classes now
  write `X$1 A B W res_xhigh_po r=12000 L=6U W=1U`: the class name becomes a
  **caller-suppliable subcircuit name** (the testbench supplies a matching
  3-pin `.subckt <class> a b w r= l= w=` wrapper declaring every parameter
  the card carries), and the extracted resistance moves onto a declared
  `r=` parameter so it stays on the written card the way issues #521/#588
  established. Two-terminal classes keep the `R` card + `.model <class> r`
  convention byte-for-byte. `klt lvs` re-ingests the new card shape
  transparently whenever `layout.deck`/`reference.deck` is given, restoring
  the identical `DeviceClassResistorWithBulk` (A/B/W, R/L/W) the old card's
  read-back produced — series `combine_devices` folding and the deferred
  `fixed_offset_ohm` correction (issue #585) behave exactly as before; see
  `docs/cli/extract.md`'s "Verified compatible with `klt sim`" and
  `docs/cli/lvs.md`'s new round-trip section.
- **Fixed** (PR #2336 follow-up, the #1157 round-trip reader — no
  `schema_version` bump): recovered `X` cards naming the same class now all
  share one `DeviceClass` object per read, restoring the series
  `combine_devices` fold the round-trip promised above on every platform.
  The first cut looked the class up via `Netlist.device_class_by_name`,
  which normalizes its argument through the netlist's case convention
  (uppercases it for a SPICE netlist) but compares against each registered
  class's stored name verbatim — the deck's canonical lowercase
  `res_high_po` never matched, so every recovered card silently registered
  another class object, and `Netlist.combine_devices()` — which groups
  devices by class *object identity* — could no longer fold the chain
  (Linux CI: the 3-segment `res_high_po` chain survived as 3 devices and
  `test_pre_extracted_netlist_with_deck_applies_fixed_offset_once` failed
  deterministically; macOS only passed because `Netlist.dup()`'s clones
  happened to read back a shared id there — `DeviceClass`'s copy
  constructor copies an indeterminate `tl::UniqueId` in klayout 0.30.10,
  so that path's fold was never portable). The same latent per-card
  registration is fixed for the #1942 MoM-capacitor and recovered-`C`-card
  paths. Regression-locked at the reader/combine boundary by
  `tests/test_lvs.py`'s
  `test_recovered_resistor_x_cards_share_one_device_class_and_fold` (a
  no-dup, no-retry `combine_devices()` call) and
  `test_custom_class_recovery_x_cards_share_one_device_class`.
- **Documented** (#1159, `klt extract --pdk` sky130 resistor geometry — the
  code fix already shipped with issue #1396's bare-micrometre convention,
  one week after #1159 was filed; this closes the loop with a regression
  lock + vendor-deck proof, and the docs it asked for): the sky130
  geometry-convention table in `docs/cli/extract.md`'s "SPICE model
  binding" section now names the affected classes explicitly — a
  `--pdk`-bound `res_high_po`/`res_xhigh_po`/`res_generic_po` `X` card
  carries its geometry suffix-free (`l=6 w=1`) because those vendor
  subcircuits' own `.param` blocks compute `leff = {l-0.0592}` and
  `Efac = {... log(leff/w)}` in bare micron-scale units, so the
  unit-suffixed spelling the issue reported (`l=180U w=0.42U`) drove `leff`
  negative at any drawn length and ngspice's `Efac` parse-tree check to a
  `nan` / `parameter value out of range` abort. Verified against the real
  unmodified `sky130_fd_pr__res_xhigh_po.model.spice`: the suffix-free card
  solves a sane operating point, the suffixed one reproduces the exact
  failure. Regression-locked for the with-bulk classes by
  `tests/test_extract.py`'s
  `test_pdk_resolved_binds_three_terminal_resistor_sky130_suffix_free`, and
  end-to-end against the real vendor deck (where an install + ngspice
  resolve) by `test_real_sky130_bound_resistor_card_simulates_against_the_vendor_deck`.

## 0.6.0 (2026-09-22)

270 commits on `main` since v0.5.0, cut under the 25-commit backstop in
[`RELEASING.md`](RELEASING.md)'s "Release cadence" (the event-based condition
is met too: #2173/#2238 document downstream consumers blocked on unreleased
fixes). Minor bump: the range adds verbs and request fields (`klt deck rules`,
`klt sim`'s 2am EDA batch-fleet backend, `klt signoff`'s grading expansion,
`klt erc`'s connectivity rollup, `klt mom`'s PEEC increments) with no
conventional-commit breaking marker (`!:` / `BREAKING CHANGE`) and no
`schema_version` value under `src/` decreased (the only literal changes are
1→2 bumps plus a new constant at 1; `FLEET_REPORT_SCHEMA_VERSION` rose 1→3);
compatibility notes, where they matter, are in the entries below.

- **Fixed** (#2312, `klt gen bjt_array` geometry + `klt gen-compose`
  via-drop, **no** `schema_version` bump — but the `COLL_*` ports' reported
  `layer` and the drawn collector-ring layer both change on sky130, see
  below): a `bjt_array` collector ring strapped to an already-connected net
  by `klt gen-compose` now actually shows up merged in `klt extract`'s
  netlist. The reported failure was a silent one — `gen-compose` reported
  the `COLL_*` leg `routed: true`, `klt drc --deck sky130` was clean, and
  `klt extract --deck sky130` still recovered the collector diffusion as a
  separate, floating `vsubs` node with no connection to the strapped net.
  The strap itself was real (#1894's contact ladder draws it); what was
  missing was *recognition*. An extraction deck derives its substrate-tie
  region from the **`tap`** mask outside every `nwell` and unifies it with
  the deck's synthesized substrate net via `connect_global`, and this
  generator drew its collector ring on the bare `active`/diffusion role
  instead — making the ring an unrecognised diffusion island no strap could
  ever tie to the substrate identity every collector-less bipolar's
  collector terminal carries. The ring (and therefore the `COLL_*` ports'
  reported `layer`) now uses the `tap` role, the same role `guard_ring`'s
  ring and each `bjt_array` unit's own base-tie pad already used: on sky130
  that moves the ring from `diff.drawing` 65/20 to `tap.drawing` 65/44; on
  gf180mcu, whose `tap` role *is* its `active` role (`22/0`), the geometry
  is byte-identical. `gen_compose`'s via-drop resolution treats both
  diffusion-*class* roles (`ExtractionDeck.active` **and**
  `ExtractionDeck.tap`) as reachable through `ExtractionDeck.contact`, so a
  port on either still gets a real licon/mcon ladder rather than the
  uncontacted metal stub a `tap`-role port used to get.
- **Fixed** (#2061, `klt mom` numerics — **no** `schema_version` bump, same
  JSON shape): the capacitance solver's off-diagonal kernel no longer lumps
  each panel's charge at its centroid for nearby panel pairs. The kernel fill
  now splits near field from far field: when two panels' centroids are
  closer than a few panel widths, the source panel's potential is integrated
  properly (4-point-per-axis Gauss–Legendre quadrature at the target
  centroid, symmetrised across the pair so the matrix — and the
  Conjugate Gradient solve — stays symmetric); well-separated pairs keep
  the cheap centroid point-charge kernel, and the diagonal (self) term keeps
  its closed form. Motivated and measured by the FastCap 2.0 cross-validation
  oracle: on the same mesh that used to disagree by 3.29% on closely spaced
  parallel plates, `klt mom` now agrees with FastCap to 0.32% (coupled lines
  0.17% → 0.03%, shielded triple 1.29% → 0.46%). The correction also
  removed the old coarse-mesh failure mode where the mutual capacitance of a
  pair whose gap was far narrower than its panels came back sign-flipped:
  the sign is now physical, and the unresolvable-magnitude condition is
  instead flagged by a new coarseness diagnostic in `warnings` (same
  `panel_size_um` guidance, one entry per offending conductor pair), so the
  under-resolved-solve warning contract is unchanged. Two closed-form
  validation tolerances were recalibrated to the cross-validated
  discretisation with FastCap co-witness runs (parallel-plate/Kirchhoff 5%
  → 6%, square-coax 2% → 3%, refinement-order floor first-order → 0.4):
  the pre-#2061 agreement there leaned on the kernel's over-coupling
  cancelling the constant-density basis's under-resolution of gap-facing
  charge — FastCap sits at the same values (0.1–0.3% from `klt mom` on
  every co-witnessed fixture). Fill cost rises only for near-field pairs
  (+45 ms of 1.5 s on the largest oracle fixture); no measurable end-to-end
  solve-time change.
- **Added** (#2308, `klt deck rules` + docs, additive — **no**
  `schema_version` bump, a new verb with its own `schema_version: 1`): a
  read-only query for the *numbers* a built-in deck enforces —
  `klt deck rules --deck sky130 [--rule poly.width.1]` lists every
  registered rule's `id`, `description`, `check`, `layers`, `scope`,
  `value_um`/`value_dbu`, kind-specific `limits`, and structured
  `provenance`, alongside the deck's own `content_hash`. No layout file and
  no check run: `klt drc` reports violations and therefore needs a stream,
  so pre-layout arithmetic (area budgeting, device pitch, whether a proposed
  segmentation is drawable at all) previously had to transcribe constants
  out of deck comments by hand — a copy that silently stops tracking the
  deck when the deck moves, and a citation no reviewer could re-check
  mechanically. Carrying `content_hash` with the values makes a cited
  constant pinnable to the exact deck revision it was read from. `area`/
  `density`/`antenna` rules, which do not use a distance threshold, report
  `value_um: null` plus their own `limits` rather than the unused `0` they
  author as a placeholder; an unknown `--rule` id is a clean error envelope
  (exit 1), never an empty `rules` list.
- **Fixed** (#2306, `scripts/install-fastcap.sh`, developer tooling only —
  **no** `klt` behaviour or JSON shape change): the FastCap 2.0 capacitance
  oracle now builds on macOS without hand-rolled local aids. Two 1992-C
  problems made a clean macOS checkout fail where Linux/CI passed:
  `src/mulGlobal.h` includes `<malloc.h>`, a glibc-only header macOS does
  not ship (BSD declares `malloc`/`calloc` in `<stdlib.h>`), and
  `src/mulSetup.c`'s unprototyped K&R `getnbrs` returns no value from an
  `int`-returning function, which Apple Clang treats as a hard
  `-Wreturn-mismatch` error that the build's existing `-w` does not demote.
  The script now generates a two-line `<malloc.h>` shim into its own
  scratch source tree and passes `-I<shim> -Wno-error=return-mismatch`,
  both guarded on `uname -s == Darwin` so the Linux `make` invocation is
  byte-identical to before. Both aids are build-only and live in the
  script's shell logic rather than the committed
  `scripts/patches/fastcap-2.0-modern-toolchain.patch`, because `patch`
  hunks apply unconditionally and these are platform-conditional; the
  pinned commit, asset URL, checksum gate and patch file are unchanged, so
  the built solver is bit-for-bit the same FastCap CI already compares
  against.
- **Fixed** (#2285, `klt functional-verification`, additive — **no**
  `schema_version` bump): `options.sdf` no longer hard-fails on a zero-delay
  `INTERCONNECT` entry whose destination is a top-level output port bit the
  gate-level netlist drives through a Verilog `assign` alias — the shape
  every P&R backend produces for a constant/tie-cell-driven output
  (`assign uio_oe[0] = net0;`). Icarus 13.0 cannot insert an intermodpath
  across that `assign` join, so each such entry cost one `SDF ERROR: ...
  Could not find intermodpath!` and a real post-route SDF could not pass the
  diagnostic gate at all, even though every failing entry was
  `(0.000:0.000:0.000)` and modelled no delay. Rewriting the endpoint
  through the netlist's alias map was tried and refuted live (the rewritten
  entry names the net its own source pin drives and fails with `Could not
  find handles for both ports!`), so such entries are now removed from the
  SDF text handed to `$sdf_annotate` and counted in `environment.sdf.dropped`
  as a new class, `zero_delay_alias_port_interconnect` (beside #1102's
  `timingcheck`), with `partial: true`. The exemption is bounded by the
  entry's delay value, not by the diagnostic text: an entry is dropped only
  when *every* `min:typ:max` member of every rvalue is zero, so a
  non-zero-delay entry on the identical destination is left in place and
  still fails loudly (exit 1) — real delay is never silently discarded. The
  alias map is read from `hdl_toplevel`'s own module via the same gate-level
  `assign` parser `klt lvs` already reuses (#2021); a top module it cannot
  read yields no drops and unchanged behavior. See
  [`docs/cli/functional-verification.md`](docs/cli/functional-verification.md)'s
  "SDF back-annotation" section.
- **Added** (#2278, `klt signoff --manifest` + docs, additive — **no**
  `schema_version` bump): an optional `partition_boundary` field on a
  `kind: "mixed-signal"` block manifest, stating what each partition denotes
  (`{"analog": "...", "digital": "..."}` — which nets/pins/cells belong to
  which side). `docs/design-evidence-tiers.md`'s "Block kind" subsection
  requires a mixed-signal claim to state that boundary explicitly, and the
  manifest previously had nowhere to put it: every report's
  `"partition": "analog"`/`"digital"` rows named a side nothing defined,
  though an evidence key could already select one (`"<id>.<analog|digital>"`).
  The declaration is echoed verbatim onto the report (top-level
  `partition_boundary`) and onto every row of the partition it names
  (`items[].partition_boundary`), so a row and the definition of the silicon
  it covers are readable together; `--format text` prints it once under the
  `kind:` header. **Reported, never graded** — it moves no item's `status`
  and no block's `tier`, exactly like `drc_coverage` (#2002) and `body_bias`
  (#1983); a mixed-signal manifest that declares nothing grades identically
  and renders a byte-identical report, so `--check` sees no drift on an
  upgrade alone. Either partition may be declared alone; a declaration on an
  `analog`/`digital` manifest, an unknown partition key, or a blank/non-string
  value is refused by name (exit `1`) rather than silently dropped. See
  [`docs/cli/signoff.md`](docs/cli/signoff.md) → "The declared partition
  boundary".
- **Added** (#2275, `scripts/check_artifact_determinism.py` + CI + docs,
  additive — **no** `schema_version` bump): flake-triage forensics and
  declared platform-variable regions for golden-artifact evidence. The
  determinism check gains a fourth float discipline, `platform-variable`:
  a manifest entry that declares it — per artifact glob, per enumerated
  JSON field path, under a stated format and a `max_abs_ulps` threshold —
  is regenerated and threshold-compared instead of byte-compared, so
  irreducibly host-libm outputs (transcendental digests) get an explicit
  guarantee while everything else stays byte-exact (an undeclared region
  or artifact that drifts still fails, and every report names the declared
  regions and any accepted drift). A failing run also writes a forensics
  pack (`--forensics-dir`; both CI jobs running the check upload it on
  failure): both variants of every differing artifact, both reports, and a
  rerun script carrying the exact seeds and checkouts — the
  triage-before-blaming-code pattern of issue #2275, from the CI artifacts
  alone. The CI jobs document which dispatch-variance classes do and do
  not apply to this stack (no BLAS/PyTorch/oneDNN exists here, so no
  `ATEN_CPU_CAPABILITY`/`MKL_CBWR`/`ONEDNN_MAX_CPU_ISA`; no runtime CPU
  dispatch in `native/`; host libm is the one real class, handled by the
  scan + declaration, not env pinning). See
  [`docs/guides/golden-artifact-determinism.md`](docs/guides/golden-artifact-determinism.md)
  → "Flake triage".
- **Added** (#2280, `klt equiv` + docs, additive — **no** `schema_version`
  bump: two new optional CLI flags, one request-independent
  present-only-under-`--resume` envelope block, one new diagnostic code,
  and one new verification mode): long-run operations for evidence verbs.
  `klt equiv --resume` re-enters a killed run from committed stage
  artifacts instead of restarting: the `"yosys-sequential"` engine's
  stage 1 commits `.klt/equiv/stage1.commit.json` atomically once it
  reaches a classified outcome (request fingerprint over source *content*
  hashes — path-independent, so remote-pulled artifact sets resume;
  classification; cut-point blacklist; refinement count), and a
  fingerprint-matched, log-corroborated record skips stage 1 — an
  `all_proven` record yields the final `"equivalent"` envelope without
  re-running Yosys; an `unproven_cells` record re-enters at stage 2. The
  new additive `resume` envelope block (`{resumed_stage, record_path}`)
  appears only when the flag was given, so runs without it are
  byte-identical to before; a resumed envelope drifts from an
  uninterrupted one only in the declared fields (`elapsed_s`, the
  `resume` block, and host-scoped identity when resuming elsewhere).
  Partial artifacts are structurally never verdict-bearing: a timed-out
  stage writes a `partial: true` record, and the loader rejects any
  record whose `partial` is not exactly `false`, whose fingerprint
  mismatches, or whose committed log bytes do not corroborate the
  recorded classification — a discarded record re-runs the stage and is
  never silent (`resume_stage_record_discarded` warning). An envelope
  itself is never partial: a killed run emits no JSON. The combinational
  engine accepts `--resume` and re-runs its single stage (uniform retry
  command for agent fleets). `klt equiv <request> --check <report>
  [--rerun]` joins the shared committed-evidence verification contract
  (#2224, `_report_verify.py`) — cheap mode re-hashes without an engine
  (the cross-host verification step for retrieved remote-run envelopes,
  whose content hashes are path-independent), full mode re-runs and diffs
  verdict-bearing fields excluding volatile identity plus
  `elapsed_s`/`resume`; exit codes reuse the run mode's 0 (match) /
  3 (drifted) / 1 (unverifiable). New guide
  `docs/guides/remote-evidence-runs.md`: driving `equiv`/long `sim` on a
  remote host via the existing `remote_launcher`/`remote_transport`
  machinery (no new orchestration infrastructure), provenance continuity
  (the envelope describes the run truthfully wherever it executed), the
  checkpoint-push convention for agent fleets (commit and push after
  every green stage), and the remote round-trip walkthrough.

- **Added** (#2255, `klt erc` + `klt signoff`, additive — **no**
  `schema_version` bump on either verb: one new optional spec key, one
  *widened* existing one, one new coverage skip reason, one new
  `erc_coverage` list, and one new item-11 citation field; a spec that does
  not use the new key produces a byte-identical report apart from the new
  empty list): a block sitting in a **native substrate** — NMOS-in-bulk,
  with no *drawn* well/tub layer anywhere in the stream because the
  substrate is diffusion-derived rather than layer-marked — can now declare
  its substrate tie. `ties[].well_layer` accepts `null`, and the new
  `ties[].well_boxes` asserts the substrate region in its place as the same
  `[left, bottom, right, top]` micrometre box list `tap_boxes` takes.
  Previously `well_layer` required drawn geometry, so only the drawn-well
  (n-well) half of such a design was ever graded, and #2234's `tap_boxes`
  did not close it (that narrows an already-drawn tap layer; this
  *substitutes* for a required well layer). The two well forms are mutually
  exclusive and both are validated: `well_boxes` requires `well_layer:
  null`, and `well_layer: null` requires a non-empty `well_boxes` — "no
  well and no assertion" stays expressible only as an omitted entry plus a
  `ties_disclosure`, which cannot be mistaken for a graded check.
  Falsifiability is enforced on the same principle as #2199's degenerate
  tap, adapted to a claim with no drawn baseline to narrow: an asserted
  region covering the top cell's own bounding box to within 1% of its area
  is indistinguishable from "the whole die is the substrate", which would
  make `erc.missing_tie` satisfiable by any contact anywhere that reaches
  the declared net, so it is recorded in `erc_coverage.skipped` with the new
  reason `degenerate_well_assertion` (`erc_status: "clean_partial"`) instead
  of being accepted as evidence — and for the same reason no implicit
  whole-top-cell substrate region and no derived `substrate = extent −
  nwell` boolean is offered. A non-degenerate asserted tie is real, checked
  work (each asserted polygon must independently hold a tap that reaches the
  declared net, and one that does not produces an ordinary
  `erc.missing_tie` finding), named additionally in the new
  `erc_coverage.checked_by_well_assertion` list — kept separate from
  #2234's `checked_by_assertion` because asserting *which drawn geometry is
  the tap* and asserting *where the substrate is* are different claims.
  `klt signoff`'s T1 item 11 reaches `met` on such a tie on the same terms
  as a drawn-well one, with the weaker provenance stated in the citation's
  new `power_delivery.ties_checked_by_well_assertion`; a degenerate one is
  caught by the existing skipped-work gate and renders
  `supply_spec_incomplete`. See `docs/cli/erc.md`'s "A block with no drawn
  well at all" and `docs/design-evidence-tiers.md` item 11.
- **Fixed** (#2259, `klt power`, no `schema_version` bump — the
  `networks[].islands[].edges[]` shape is unchanged; what changes is which
  nodes a `kind: "via"` edge connects, plus one new `warnings` string): a via
  edge now attaches to the merged polygon the via **physically lands on**,
  per metal role, instead of to the nearest node anywhere on the net.
  Previously the nearest-node search ran over every rail the net owned on
  that role, so on any trunk-and-stub rail — a PDN strap with drop-downs, a
  cell-row rail with per-row risers, a long Metal1 trunk tapped by Poly2
  risers — a via tapping the trunk partway along was nearer an unrelated
  short stub's endpoint than either of the trunk's own ends, and was wired to
  the stub. That is not a positional approximation: it severed the trunk from
  every tap it fed and shorted two electrically unrelated segments together,
  fragmenting one physical island into several disconnected components inside
  the resistor network. Because `worst_case_droop_mv` is computed over
  *solved* nodes only, the orphaned nodes reported
  `unsolved_reason: "no_pad"` while the headline droop came out `0.0` — a
  silent false pass, in the unsafe direction, on both the IR and the EM
  verdict (orphaned edges carry no current). The documented behaviour in
  [`docs/cli/power.md`](docs/cli/power.md)'s "Scope and limitations" — a via
  snapping to an endpoint of *that rail*, preserving every island's real
  connectivity — is now what the code does. Two supporting changes: a via
  shape that lands on no modelled segment of a role it declares is skipped
  with a `warnings` entry naming it (rather than reaching across to a rail it
  does not touch), and each island's emitted network is checked to be **one
  connected component** — matching the one island the connectivity model
  found — with any shortfall reported in `warnings`, since that condition is
  otherwise invisible behind an unchanged `island_count`. Pad/instance
  attachment still snaps to the nearest node net-wide and is unchanged: a
  pad coordinate is a spec-declared point that need not sit on any drawn
  shape, so there is not always a polygon to scope to, and the error there is
  positional only — it cannot sever connectivity, because the node it picks
  is in the same island either way. That trade is now stated explicitly in
  the docs.
- **Added** (#2260, `klt power`, additive — **no** `schema_version` bump: a
  spec that declares no `devices[]` produces a byte-identical report apart
  from the new empty list): `klt power` specs may declare the same
  optional `devices[]` array `klt erc` grew in #2183 — `{"name",
  "body_layer": "<layer>/<datatype>", "on": "<stackup or vias name>"}` —
  naming where a drawn **device body** sits on an already-declared
  conducting role. Each entry's region is subtracted from that role's
  conductor region before connectivity is traced **and before the resistor
  network is built**. Without it, a poly load resistor or MiM capacitor
  drawn on a role the spec must declare (poly that also carries supply
  current, the via role that straps a top plate) was read as wire — and
  `klt power` does not merely mislabel the resulting net, it *solves* it:
  the body became a low-resistance path in the R network, so the IR-drop
  map and the EM verdict were computed on a rail that does not exist, with
  the worst-case droop node and the failing EM edge landing on the *other*
  supply. Dropping the layer from `stackup[]` was no answer either (it
  fragments any rail that genuinely uses that layer into padless islands).
  What each declaration actually removed is echoed in a new **top-level**
  `devices[]` (`{"name", "body_layer", "on", "body_area_um2"}`, the marker
  intersected with the role's own drawn region, `0.0` when the declaration
  matched nothing) — top-level rather than `klt erc`'s
  `provenance.devices` only because `klt power` has no `provenance` block
  to nest under. The schema, its validation, the subtraction, and the area
  accounting are now a single shared implementation
  (`src/klayout_tools/_devices.py`) used by both verbs, so the identical
  `devices[]` block can be handed to either; `klt erc`'s behaviour is
  unchanged. Semantics stay deliberately narrow — "this drawn body is not
  wire", not "this is a 3.4 kΩ resistor"; device impedance is still not
  modelled. Written up in [`docs/cli/power.md`](docs/cli/power.md)'s new
  "Device bodies are not wires" section.
- **Added** (#2245, `klt extract`, additive — **no** `schema_version` bump:
  two new optional flags and one new optional top-level field, `null` unless
  the flags are used; an extraction that does not pass them writes a
  byte-identical netlist and a report differing only by that field's
  presence): `--subcircuit <cell>` (with optional `--subcircuit-output
  <path>`) additionally writes a **second, standalone SPICE deck** carrying
  one named `.SUBCKT <cell>` block for that sub-cell's own extracted
  devices, so a post-layout testbench can instantiate a routed block's
  sub-circuit in isolation the way a schematic-level campaign instantiates a
  named `.subckt`. Extraction stays flat: the flag runs the same flat pass,
  writes the same flat netlist first, then *slices* one named sub-cell out
  of the finished circuit. Devices are attributed positionally, by the same
  `devices[].instance_path` placement chain (#1666); a net whose terminals
  are all inside stays an internal node, and a net that also carries an
  outside device terminal (or is a pin of the flat deck) is promoted to a
  `.SUBCKT` pin. **Boundary-crossing parasitic attribution rule**: an
  internal net keeps its whole star (legs, ground capacitance, ladder,
  inductor); a boundary net becomes a pin at its hub keeping only the series
  legs of the sub-cell's *own* terminals, with its shunt (ground/coupling)
  capacitance attributed to the parent — a shunt on a testbench-driven pin
  is not observable, while its series share is. A coupling capacitor is kept
  whenever either side is internal, with the far net promoted to a
  `parasitic`-role pin (dropping it would silently make post-layout timing
  optimistic); the 1 Tohm substrate DC tie (#1263) survives for every
  substrate net in the slice. Everything the boundary rule hands to the
  parent is counted in the new `subcircuit.excluded_parasitics`, so nothing
  vanishes silently, and the two decks are alternatives never co-simulated,
  so nothing is double-counted either. Near-inverse of `--abstract-cells`
  (#620), which emits an *empty* black box, and mutually exclusive with it.
  Refused rather than guessed when the named cell is the top cell, is not
  placed under it, is placed more than once (two sibling placements are
  indistinguishable by name), or contributes no recognized device. See
  `docs/cli/extract.md`'s "Sub-circuit isolation" section.
- **Added** (#2247, `klt erc` + `klt signoff`, additive — **no**
  `schema_version` bump on either verb: one new optional spec sub-key, one
  new coverage reason token it can record, and one new item-11 reason
  constant; a spec that does not use the new key produces a byte-identical
  report): `ties_disclosure.kind` distinguishes **why** a spec deliberately
  declares zero `ties[]`, because the two reasons have different remedies
  and previously collapsed into one. `"unexpressible"` (the default, and
  the only meaning #2234's disclosure had) means there is no tap to name —
  no narrowing marker, no dedicated tap layer, no nameable tap geometry;
  cleared by drawing something. `"tool_limitation"` (new) means the tap
  *is* expressible, but the `klt` build the evidence has to be produced on
  cannot grade a declared tie safely — the reported instance being #2169,
  where a declared tie on an affected build joins the well/tap regions into
  the primary connectivity graph and reports a false `erc.supply_short` on
  any routed design, so a release-pinned flow's only honest option is to
  declare no tie and say so; cleared by a different *build*, not a redrawn
  layout. `klt erc` records `ties_disclosed_tool_limitation` (beside the
  existing `ties_disclosed_unexpressible`/`no_ties_declared`) as the
  `erc_coverage.inapplicable` reason for the undeclared `erc.missing_tie`
  work, echoes `kind` back only when the spec declared it, and rejects any
  other value rather than falling back to the default — a typo that quietly
  downgraded one disclosure to the other would misdirect the reader of the
  report of record. `klt signoff`'s T1 item 11 renders that as the distinct
  reason `supply_spec_disclosed_tool_limitation`, so the three
  zero-`ties[]` states are now mechanically distinguishable:
  `supply_spec_incomplete` (nobody asked the question),
  `supply_spec_disclosed_unexpressible` (#2234, no tap to name), and
  `supply_spec_disclosed_tool_limitation` (a tap, and a build that cannot
  be trusted to grade it). **All three remain `unmet`** — a disclosure is
  the caller's word, never a computed `erc.missing_tie` result — and an
  `erc_coverage` reason token `klt signoff` does not recognise still falls
  through to the plain `supply_spec_incomplete`, so an unfamiliar string
  can never soften the verdict of record. A reader checks the report's own
  `provenance.klt_version` to see whether a disclosed tool limitation
  applies to the run in front of them. See
  [`docs/cli/erc.md`](docs/cli/erc.md)'s "When the obstacle is the build,
  not the stream" and
  [`docs/design-evidence-tiers.md`](docs/design-evidence-tiers.md) item 11.
  The separate native-substrate gap (a `ties[]` entry still requires a
  *drawn* `well_layer`, so a block with no drawn pwell cannot declare its
  substrate tie at all) is **not** closed by this and is tracked in #2255,
  now cross-referenced from both docs.
- **Added** (#2249, `klt signoff`, no `schema_version` bump — a new flag and
  a new response shape for it, no change to the tier/fleet report's own
  fields): `klt signoff --manifest|--fleet M --check REPORT` verifies that a
  previously committed tier/fleet report still reproduces, re-grading `M` and
  diffing the result against `REPORT` **excluding the `build` block** —
  `status: "match"` (exit `0`) / `"drifted"` (exit `3`, naming every field
  that moved), the same `{schema_version, mode, report, status, drift,
  fresh}` shape and exit codes the five existing `--check` verbs use
  (`docs/json-contract.md` → "Verifying committed evidence"). This replaces
  byte-comparing a committed report against a fresh re-render: a report's
  `build` block states the checkout state of the tree the running install was
  *built* from, so two byte-legitimate installs of the **same pinned commit**
  can emit different bytes for identical evidence (a `git+…@<sha>` install
  records `+g<sha>`; a `pip install` of a source tarball of that same `<sha>`
  has no `.git` at build time and honestly records `+unknown`, which no fix
  to `dirty` can collapse). Documented in `docs/cli/signoff.md`
  ("`build` describes the *install*, not only the commit", "Verifying a
  committed report: `--check`") and `docs/cli/version.md` ("These fields
  describe the install, not only the commit"), which also now names the
  byte-canonical provisioning route for a gate script.
- **Changed** (#2242, no code change — committed example artifacts only):
  `examples/critical-net-mom-fidelity/`'s three `klt extract` reports
  (`phase1-baseline.json`, `phase2a-critical-net.json`,
  `phase2b-distributed-rc.json`), their sibling `.spice` netlists, the
  `lateral-coupling.gds` fixture, and the `dfxtp2-{lumped,distributed}.spice`
  netlists are regenerated from the current `klt extract`. They had not been
  re-run since #978 and still showed `schema_version: 2` under
  `klt_version 0.2.0`; the current verb emits `schema_version: 3` plus
  `metrics`, `matched_device_groups`, per-net `net_id`/`pin_index`/
  `label_positions_um`, per-net `by_layer`/`inductance_nh`/
  `*_top_cell`, `parasitics.substrate_dc_tie`/`mom_rlc_override`/`l_count`/
  `top_cell_only`, and `provenance.deck.released`/`provenance.input.role`.
  The emitted netlists gained the matching `.GLOBAL vsubs` +
  `Rvsubs_dctie` substrate DC tie. A new dated evidence record is written
  under `evidence/sim/sky130-critical-net-fixture/mom-coupling-fidelity/`
  and `HEAD` repointed to it. The MoM-vs-extract conclusion is unchanged
  (`AGR`/`VIC` 0.044 fF vs a 0.09795 fF converged MoM oracle, −55.1%), but
  the `dfxtp_2` distributed-RC invariance check's shared
  `total_coupling_capacitance_ff` moved from `1.054535` to `0.10731` under
  the newer coupling model and deck — lumped and distributed still agree
  exactly, which is the property that check exists to assert.
  `docs/design/critical-net-mom-fidelity-phase2c.md`'s table is updated to
  the new value.
- **Fixed** (#2244, `klt lvs`, no `schema_version` bump — the `side` field
  already documented `"layout"`/`"reference"`/`"both"` as valid values;
  this changes when each is emitted, not the shape): a
  `reference.form: "gate-level-verilog"` reference that itself
  instantiates a power-only master (filler/tap/endcap — the shape a
  DEF-derived `write_verilog` reference produces, with an empty connection
  list per instance, unlike `klt place-and-route`'s own `verilog_path`
  writer) is now pruned from the reference side too, not just the layout
  side (issue #1622's original `topology.power_only_pruned` prune).
  Previously only the layout-side copy was removed, leaving the
  reference-side master and its instances with no counterpart at all — a
  `topology` "circuit could not be matched to a counterpart" error cascade
  (one per reference type, one per reference instance) around an
  otherwise-clean `power_connectivity: "match"` verdict, misreading an
  equivalent design as a signal-side `mismatch`. The prune now runs
  symmetrically: a qualifying master's type and every instance of it are
  removed from whichever side(s) actually instantiate it, disclosed once
  as a single `topology.power_only_pruned` entry (`side: "layout"` when
  only the layout side had one, `"reference"` when only the reference
  side did, `"both"` when each side did). See
  [`docs/cli/lvs.md`](docs/cli/lvs.md)'s `topology.power_only_pruned`
  section for the corrected contract.
- **Documented** (#2246): `docs/cli/gen.md`'s "Semantics and guarantees"
  section now states plainly that generator *geometry* output is not
  guaranteed stable across `klt` releases before `1.0` — only the JSON
  envelope shape carries the existing additive-fields guarantee — citing the
  `0.5.0` gf180mcu implant-ring coverage fixes (#1577, #1580) and the
  per-PDK output-dbu-grid change (#1496) as concrete precedents. A
  byte-reproduction-based consumer is told to key drift detection off
  `provenance.klt_version`/`provenance.klayout_version` (already shipped,
  unreleased, via #2035/PR #2066) rather than a bare `klt --version`, and to
  consult this file to see whether a version difference is
  geometry-affecting before assuming a byte diff means an operator edit.
  `docs/json-contract.md`'s shared `provenance` block section cross-links
  the new caveat. No functional change: the existing
  `provenance.klt_version`/`klayout_version` fields are judged sufficient
  granularity for this use case, so no dedicated per-generator
  geometry-revision field was added.

- **Added** (#2234, additive — **no** `schema_version` bump on `klt erc` or
  `klt signoff`; every field below is new, and a spec that uses neither new
  key produces the same report it did before, except for the
  always-present-but-`null` `ties_disclosure` echo and an empty
  `erc_coverage.checked_by_assertion`): two ways for a stream whose taps
  carry **no distinguishing implant/marker layer at all** to declare them —
  the implant-free full-custom case where neither `ties[].tap_requires` (no
  implant is drawn to intersect) nor `ties[].tap_is_dedicated` (the PDK ships
  no tap-only layer) has anything true to say, so every tie declaration
  lands *degenerate* (#2199) and
  [`docs/design-evidence-tiers.md`](docs/design-evidence-tiers.md) item 11's
  zero-`erc.missing_tie` condition is unreachable by construction.
  `ties[].tap_boxes` (array of `[left, bottom, right, top]` micrometre
  boxes) **asserts** where the tap geometry is, intersected into `tap_layer`
  and composable with `tap_requires`; because it rests on the caller's word
  rather than on a drawn marker it is graded under its own coverage
  classification, `erc_coverage.checked_by_assertion` (a subset of
  `checked`, additive to the four common-contract lists), and it is held to
  exactly the falsifiability bar #2199 set: an assertion that removes
  nothing from the drawn `tap_layer` inside the well is still
  `degenerate_tap_declaration`, and one matching no drawn geometry produces
  the same honest "no tap drawn" finding a genuinely absent tap would. The
  top-level `ties_disclosure` (`{"reason": <non-empty string>}`) is for a
  stream that cannot express a tap at all: it changes no geometry and no
  finding, only the `erc_coverage.inapplicable` reason recorded for the
  undeclared `erc.missing_tie` work (`ties_disclosed_unexpressible` in place
  of `no_ties_declared`), which `klt signoff`'s T1 item 11 renders as the
  distinct reason `supply_spec_disclosed_unexpressible` — still **unmet**, a
  disclosure proves nothing about the tap's actual connectivity, but no
  longer indistinguishable from a spec that never considered the question.
  A `met` item 11 citation's `power_delivery` block additionally carries
  `ties_checked_by_assertion`. See
  [`docs/cli/erc.md`](docs/cli/erc.md)'s "A tie with no distinguishing
  marker layer at all".

- **Changed** (#2230, additive — **no** `schema_version` bump on any verb; no
  `klt` payload *shape* changes, only which strings `lint-envelope` reports
  and what three committed example artifacts contain): `klt env-provenance
  lint-envelope` no longer flags two shapes that were never host paths —
  an angle-bracket **template root** (`<path-to-your-checkout>/infra/run.sh`,
  exempt for the same reason `$PDK_ROOT/…` already was; adjacency-scoped, so
  a real absolute path elsewhere in the same string is still reported) and a
  **URI fragment holding an RFC 6901 JSON Pointer**
  (`02-architecture.json#/blocks/ota_buffer`; only the fragment is excised,
  so an absolute path on the document side of the `#` is still a finding).
  Alongside it, the three committed `examples/critical-net-mom-fidelity/
  phase*.json` `klt extract` reports had the generating worktree's absolute
  path in `file`/`netlist_path` rewritten repo-relative (field *shape*
  unchanged — still plain strings), which also makes `klt extract --check` on
  them resolve their input on any checkout instead of reporting
  `provenance.input.content_hash: null` everywhere but one machine, and
  `examples/design-centering/{request,sized-device}.json`'s `/abs/path/…`
  documentation placeholder was re-spelled `<abs-path>/…`. With the tree
  clean at **zero `--allow-prefix`**, `git ls-files 'examples/**/*.json' |
  xargs klt env-provenance lint-envelope` is now a CI step so it cannot
  regress — plus two in-suite regression tests, one asserting the committed
  tree lints clean (so a bad artifact fails `pytest` before CI) and one
  asserting the CI step stays wired with an empty allow-list. See
  [`docs/cli/env-provenance.md`](docs/cli/env-provenance.md)'s "Rules" and
  "Wiring it into a repo's CI" sections.

- **Changed** (#2227, `--format text` only — **no** `schema_version` bump and
  no change to any `--format json` payload): `klt signoff`'s text rendering
  no longer emits ANSI colour unconditionally. Colour now follows
  `stdout.isatty()` — on at a terminal, off when redirected to a file or
  piped — and is suppressed outright by `--no-color`, the new
  `--color=never`, or `$NO_COLOR` (any non-empty value,
  [no-color.org](https://no-color.org/)). `--color=always` opts back in
  through a pipe and outranks `$NO_COLOR`. The motivating case is the
  committed tier report `--manifest` exists to produce: `klt signoff
  --manifest m.json --format text > signoff.txt` is now escape-free by
  default, readable in a pull-request diff, and greppable without stripping
  ANSI first — so the committed file can stay byte-identical to what the
  grader emitted. Terminal output is unchanged. **Callers that relied on the
  escapes being present in redirected/piped output must now pass
  `--color=always`.** See
  [`docs/cli/signoff.md`](docs/cli/signoff.md)'s "Colour in `--format text`"
  section.

- **Added** (#2224, additive — **no** `schema_version` bump on `klt
  synthesize` or `klt place-and-route`; the `--check`/`--rerun` payloads are
  their own `schema_version: 1` documents, not a change to either verb's
  report shape): the two *flow* verbs now have the committed-evidence
  verification `klt drc`/`klt lvs`/`klt extract` have had since #1106/#1149 —
  `klt synthesize REQUEST --check REPORT [--rerun]` and `klt place-and-route
  REQUEST --check REPORT [--rerun]`, built on the same
  `_report_verify.py` building blocks and reporting the same two-valued
  `status: "match"`/`"drifted"` (exit `0`/`3`; a report that cannot be
  verified at all is exit `1`, never a false pass). Unlike the three existing
  consumers, the positional `REQUEST` stays required: a flow-verb report
  echoes its outputs and provenance hashes but never the request that
  produced it. `engine_version` (the Yosys/OpenROAD build string) joins
  `provenance.klt_version`/`klayout_version`/`pdk.version` in the `--rerun`
  exclusion set, and run-scoped bookkeeping (`synthesize`'s per-run
  `run_id`/artifact paths, `place-and-route`'s `uuid4`-keyed `engine_logs[]`)
  is canonicalized out of both sides of the diff. `klt place-and-route` gains
  exit `3`, used *only* by `--check`. See
  [`docs/cli/synthesize.md`](docs/cli/synthesize.md)'s and
  [`docs/cli/place-and-route.md`](docs/cli/place-and-route.md)'s
  "`--check` / `--rerun`" sections.

- **Added** (#2224): `klt env-provenance lint-envelope FILE…` — walks a
  committed JSON envelope and fails (exit `3`) on any string field carrying
  an **absolute host path**, naming the offending field by dotted path, with
  `--allow-prefix` for genuinely machine-wide PDK/tool install roots.
  Deliberately broader than the existing `klt env-provenance scan`, which
  answers the *disclosure* question (home-shaped paths only): `/opt/build/
  out.def` names nobody and still makes a regenerated artifact byte-differ on
  another checkout, which is how a downstream repo's committed corpus
  artifact broke cross-checkout byte comparison (2AMLogic/gf180-surge#39).
  Not wired into CI here at the time — #2230 (below) cleared the tree and
  wired it in. `docs/json-contract.md` now states the
  repo-relative-provenance rule for committed artifacts alongside the
  existing "Output-artifact path fields" table.

- **Added** (#2223, additive — **no** `schema_version` bump on `klt equiv`):
  an optional Verilator fast-path backend for `klt equiv`'s
  counterexample/vector replay, selected by `request.sim_backend` (or
  `--sim-backend`): `"iverilog"` (default, **canonical for evidence**,
  unchanged), `"verilator"` (replays via `verilator --binary` — an
  interpreted event loop replaced by compiled C++, ~6.4× faster on a
  downstream long-vector bring-up and widening with vector length), or
  `"both"`. Under `"both"` the two backends **must agree**: a differing
  confirmation verdict, or a replayed-output difference the declared
  2-state/4-state modelling gap does not explain, is reported as an
  error-severity `sim_backend_disagreement` diagnostic with
  `confirmed_by_simulation` reset to `null` and `status` downgraded to
  `"inconclusive"` (exit `4`) — never a pass, and never silently resolved
  in favour of one backend. New additive response fields: top-level
  `sim_backend`, `counterexample.simulation.four_state`, and
  `counterexample.simulation_cross_check` (the second backend's own run
  plus `agreement` and per-signal `output_mismatches`). With no `verilator`
  installed nothing is fabricated: the Verilator-only backend degrades to
  the same `simulation_unavailable` warning a missing `iverilog` already
  produces (it never silently runs `iverilog` and labels the result
  Verilator's), and `"both"` records `agreement: "unavailable"` while the
  canonical evidence stays exactly what `sim_backend: "iverilog"` produces.
  See [`docs/cli/equiv.md`](docs/cli/equiv.md)'s "Replay backend" section.

- **Fixed** (#2226, `klt erc`, no `schema_version` bump — this is a
  *conformance* fix, not the semantics redefinition
  [`docs/json-contract.md`](docs/json-contract.md)'s "value sets within an
  unchanged shape" caveat requires a bump for: the field's documented
  meaning is the same before and after, and only the implementation moves
  to match it. The `klt precheck` precedent that did earn a bump, issue
  #452, is the opposite direction — docs and code agreed, and the
  *definition* itself changed):
  `provenance.devices[].body_area_um2` now reports the area the
  declaration **actually** subtracted from its `on` role — the marker
  layer intersected with that role's own drawn conductor region — instead
  of the marker layer's own area. Both the field table in
  [`docs/cli/erc.md`](docs/cli/erc.md) and the implementation's docstring
  already described it that way; the computation did not. Two consequences
  for readers of committed ERC reports: a well-formed declaration no longer
  over-states its carve-out (device-body markers are conventionally drawn
  with enclosure past the conductor they mark, so `area(marker)` exceeded
  the real cut for essentially every correct declaration), and a
  declaration whose `on` names a role its marker never touches now reports
  `0.0` instead of the same large number a correct declaration reports —
  restoring the wrong-`on` cross-check the field exists to perform. Such a
  declaration (marker drawn on the stream, zero overlap with its declared
  role) additionally emits a one-line warning on stderr; `--format json`
  goes to stdout only, so a piped report is unaffected. Connectivity, net
  extraction, and every finding are byte-identical — the region subtracted
  from the role was already `region - marker`, which equals
  `region - (marker ∩ region)`. `--deck`-detected entries are unaffected:
  their regions are derived from the role's own layer by construction.

- **Added** (#2216, additive — **no** `schema_version` bump on `klt version`
  or either `klt signoff` doc-parsing mode): `klt version --format json` now
  reports `grading_ruleset_id`, a `sha256:`-prefixed content hash of the
  shipped `signoff.py` grading module -- the missing half `git_commit`/
  `git_tag` could not answer, since those identify the *source checkout* a
  build was made from, not the *grading rules* actually compiled into it. A
  registry wheel built from a release tag and a `pip install git+...@<tag>`
  snapshot of that same tag now report the identical id (byte-identical
  grading code, no shared `.git` history required to prove it), while a
  full-repo checkout that has moved past the tag on `main` reports a
  different one whenever `signoff.py`'s grading logic actually changed.
  Echoed into every `klt signoff --manifest`/`--fleet` report's own `build`
  block, so a committed tier-verdict report names the grading rules that
  produced it. Also new: `klt signoff --describe-grader`, which enumerates
  -- at runtime, without reading source -- which T1 checklist item ids this
  build has grading rules for, alongside the same `grading_ruleset_id`,
  built on the existing `_build_t1_item_ids()`/`_is_graded_by_build()`
  machinery (#2176) that already computed this internally to populate each
  tier-report row's `graded_by_build` field. See
  [`docs/cli/signoff.md`](docs/cli/signoff.md)'s "Identifying the grading
  build" section.

- **Added** (#2202, additive — **no** `schema_version` bump on either mode):
  `klt signoff --manifest`/`--fleet` now report `build_t1_item_count` beside
  the existing `t1_item_count` — how many T1 rows **this build's own shipped
  doc** would have rendered, against how many the parsed doc did. This is the
  reverse of #2176's `graded_by_build`: a `--tiers-doc`/`$KLT_TIERS_DOC` copy
  *older* than the running build produces no wrong verdict (every rendered
  row is correctly graded), but the report says nothing about the items it
  never listed, so a build that grades 11 T1 items pointed at a 9-item doc
  awards `tier: "T1"` on 9/9 and reads exactly like a full-checklist claim —
  a strictly weaker claim, previously distinguishable only by a reader who
  already knew which build produced which report. The field is a **row**
  count like `t1_item_count` (so a `mixed-signal` block's pair doubles
  together), is carried on each `--fleet` `blocks[]` row as well as on the
  tier report — unlike per-item `graded_by_build`, because those rows carry
  `t1_item_count` too — and is surfaced in the text rendering as a `scope:`
  line whenever the two differ. It is `null`, never a fabricated number, when
  this build cannot read its own shipped doc, the same rule `graded_by_build`
  follows when it cannot prove divergence. The missing items are deliberately
  *not* rendered as rows: the report is the parsed doc's skeleton by design.
  No `schema_version` bump — a new key on an unchanged shape, with no grading
  change behind it, per
  [`docs/json-contract.md`](docs/json-contract.md).

- **Changed** (#2203, `schema_version` 2 → **3** for `klt signoff --fleet`'s
  report only): the fleet roll-up's `blocks[].blocking_item` now names an
  unmet T1 item **this build has no grading rules for** (`graded_by_build:
  false` — an item id only a `--tiers-doc`/`$KLT_TIERS_DOC` copy of the doc
  lists, #2176) in preference to every other unmet item, where #2178's rule
  demoted it below any gradeable one. That demotion was incidental, not
  intended: such an id is in neither grading table, so it satisfied the
  "structurally ungradeable" predicate written for items 1/2/9/10 and was
  swept up with them. The two are opposites for this purpose. Items 1/2/9/10
  are demoted because they are unmet *by construction* for every honestly
  authored manifest — background noise, and a weak answer to "why isn't this
  block T1 yet". A `graded_by_build: false` row is never expected and is the
  sharpest answer available: the roll-up could not evaluate that item at
  all, so every other explanation it prints is conditional on a completeness
  the verdict does not have — and it is the only blocker class a manifest
  edit cannot clear, since such an item can never render `"met"` on this
  build. The class is read from `graded_by_build`, so it covers both the
  cited form (`reason: "ungradeable_by_build"`) and the uncited one
  (`reason: "no_evidence"`). `blocks[].ungraded_items` is unchanged — it
  lists both ungradeable classes exactly as it did before — and no field is
  added, removed or retyped; the bump is solely because an already-shipped
  field's *meaning* moved, the same rule that took this report to `2` in
  #2178. A fleet graded against the shipped doc is byte-identical: no item
  is ever `graded_by_build: false` there, so the new rule is unreachable.
  `--manifest` and envelope-aggregation mode are untouched, and no item's
  grading changes. See [`docs/cli/signoff.md`](docs/cli/signoff.md)'s "Which
  unmet item the blocker names".

- **Added** (#2204): `klt erc` gained a new `--deck <name>` flag (currently:
  `gf180mcu`, `sg13cmos5l`, `sg13g2`, `sky130` — the same curated-registry
  lookup `klt extract --deck`/`klt lvs --deck` resolve, no PDK install
  needed) that auto-detects a curated deck's own device-body marker layers
  and carves them out of the matching `stackup`/`vias` role — the deck-driven
  alternative to hand-transcribing a `devices[]` entry (#2183). A deck
  device applies to a role only when its conducting-body layer equals that
  role's own layer/datatype exactly (`ResistorDevice.body`, or
  `CapacitorDevice.top_plate`/`top_plate_via`, the latter narrowed to only
  the via's overlap with the recognised bottom plate — never the whole via
  layer, which is typically also the deck's own ordinary inter-metal via).
  An explicit `devices[]` entry for a role still wins over the deck's own
  detection for that role. Every auto-applied carve-out is echoed in
  `provenance.devices` exactly as a hand-declared entry is, plus new
  `source` (`"declared"` vs `"deck"`) and `superseded_by` fields; a deck
  device matching no declared role is still listed (`"on": null`) rather
  than silently dropped. `provenance.deck` — previously always `null` for
  `klt erc` — is now populated when `--deck` is given. Both additions are
  conditional on `--deck`: a run that omits it (every caller before this
  issue) produces byte-identical output, so `schema_version` stays `1`. See
  [`docs/cli/erc.md`](docs/cli/erc.md)'s "Deck-driven device-marker
  auto-detection".

- **Changed** (#2199): `klt erc` no longer reports a **degenerate** `ties[]`
  declaration as a passing `erc.missing_tie` check. A tie whose declared tap
  region is indistinguishable from an ordinary source/drain contact — no
  `tap_requires` narrowing that actually removes geometry inside the well,
  and no new `tap_is_dedicated` affirmation — reports every well as "tied" as
  soon as any contact in it reaches the declared net, which on a PMOS row
  whose sources sit on VDD is every well, every time. That work is now
  recorded in `erc_coverage.skipped[]` with reason
  `degenerate_tap_declaration` (the record's work identity names the tie)
  instead of in `checked`, so `erc_status` reads `"clean_partial"` rather
  than `"clean"`. `erc_findings` is unchanged for every input, degenerate or
  well-formed: this changes which coverage list an existing identity lands
  in, not what the rule detects, so no `schema_version` bump. New optional
  spec key `ties[].tap_is_dedicated` (boolean, default `false`) asserts that
  `tap_layer` already names a tap-only layer (sky130's `tap`), which keeps
  such a tie graded as checked work. `klt signoff`'s T1 item 11 renders a
  cited ERC run with a skipped `erc.missing_tie` as `supply_spec_incomplete`
  — the same "an uncomputed check is not a clean one" rule that already
  rejects a spec declaring no `ties[]` at all. Envelopes produced before this
  change carry no skip records and grade exactly as they did. See
  [`docs/cli/erc.md`](docs/cli/erc.md)'s "A degenerate tie is reported as
  skipped, not as a pass".

- **Added** (#2198): `klt signoff --manifest` reports a **version-skewed**
  envelope distinguishably, with a new `reason` value
  `"envelope_version_skew"` on the citing item. `_classify` recognises some
  kinds by a *pair* of markers — `sta` needs a top-level `geometry_source`
  string **plus** `timing_status` or a `corners` list — so a response
  carrying the primary marker alone is refused rather than graded as timing
  evidence. That is deliberate, but `timing_status` is additive (#1865/#1915),
  so the refused shape is exactly what every `klt sta` response written
  before it has; because committed evidence is append-only, that stock only
  grows. Such a citation previously rendered `"unrecognized_envelope"` —
  indistinguishable from "this file is not a `klt` artifact at all". It is
  still never graded as `sta` evidence; what changes is that a block repo can
  now tell *"re-run this check under a newer `klt`"* from *"this citation is
  wrong"*. Envelope-aggregation mode still exits `1`, now with a message
  naming the kind the envelope nearly matched and the fields it lacks.
  Purely additive: a genuinely foreign document still renders
  `"unrecognized_envelope"`, a malformed-for-its-kind envelope still renders
  `"unrecognized_envelope"`, and a kind whose markers are both original
  (`place-and-route`'s `stage_reached`+`power`) is unaffected. No
  `schema_version` bump — the `reason` enum gains a value, none changed
  meaning. See
  [`docs/cli/signoff.md`](docs/cli/signoff.md)'s `reason` values table.

- **Added** (#2196, additive — **no** `schema_version` bump, per
  [`docs/json-contract.md`](docs/json-contract.md)): every `"met"` citation
  in `klt signoff --manifest`'s tier report now carries `input_verified`
  — whether the cited envelope's own recorded
  `provenance.input.content_hash` was checked against the **input artifact
  the envelope names**, or only against the envelope's claim about it.
  `--manifest`'s freshness gate compares a manifest's pinned `content_hash`
  against that self-report; both sides are statements *about* a revision,
  and neither is the revision, so a manifest and an envelope could go on
  agreeing with each other indefinitely while the GDS/netlist/record they
  describe was rewritten underneath them — the item stayed `met`, with a
  pinned hash, and nothing anywhere had read the file. `klt signoff` now
  re-hashes the artifact the envelope names (`drc`/`extract`/`erc`'s `file`,
  `lvs`'s `layout` — the layout side `provenance.input` actually pins —
  `sim`'s `netlist`, `pex`'s `layout`, `sta`'s `def_path`/`verilog_path`;
  `yield` already re-hashed its samples document since #870) and reports
  `true` (re-hashed, matches), `false` (re-hashed, disagrees) or `null`
  (nothing was re-hashed — no recorded hash, no resolvable input path for
  that kind, or a path that does not resolve to a readable file from the
  grading context). The key is always present, `null` included: an omitted
  key would leave the unverified case exactly as silent as it was before.
  `--format text` prints the same statement under the `cite:` line it
  qualifies. **Disclosure only**, matching #2002 (`coverage`) and #1983
  (`body_bias`): no grading rule consults it, so no item's `met`/`unmet`
  verdict moves — a citation that is `met` today stays `met` with
  `input_verified: false` beside it. A verdict-changing remedy (a distinct
  `input_changed` reason) is a deliberate follow-up. See
  [`docs/cli/signoff.md`](docs/cli/signoff.md)'s "A pinned hash is checked
  against the artifact, not only against the envelope".

- **Added** (#2194): a multi-island `erc.unconnected_net` finding from `klt
  erc` now says **where** the islands are, not only how many there are. Each
  such finding carries a new `islands[]` array — one
  `{"bbox", "layer", "shape_count"}` entry per disconnected electrical
  island, in ascending cluster-id order — and its previously always-`null`
  top-level `bbox` is now the box spanning every island. `bbox` values are
  raw database units, the same `{"left", "bottom", "right", "top"}`
  convention as `klt drc`'s `violations[].bbox`. Previously the only
  actionable content ("which island is which, and where") required a caller
  to rebuild `klt erc`'s own `LayoutToNetlist` connectivity model by hand.
  The `islands` key is present on **every** finding for a uniform key set,
  but is `null` for every rule other than multi-island
  `erc.unconnected_net` — including the zero-match `erc.unconnected_net`
  case, which has no geometry to point at. `klt erc --format text` prints
  one `island N: (left,bottom)-(right,top) layer=… shapes=…` line per
  island. Additive: no `schema_version` bump. See
  [`docs/cli/erc.md`](docs/cli/erc.md)'s "Locating the islands of a
  multi-island `erc.unconnected_net`".

- **Documented** (#2180): `klt erc`'s "Connectivity model" section in
  [`docs/cli/erc.md`](docs/cli/erc.md) now states a false-positive risk that
  was previously undocumented: `erc.unconnected_net` can fire on a declared
  supply net whose real continuity in silicon runs through diffusion/well
  material the stackup/vias graph never models — e.g. a guard-ring or n-well
  tap band strapped to a device row's supply through the well body itself,
  not through a metal jumper. `ties[]` (issue #2169) does not close this gap:
  it only ever answers "is a tap present and does it reach the declared
  net", never "does this well merge two islands the primary graph sees as
  disconnected". [`docs/design-evidence-tiers.md`](docs/design-evidence-tiers.md)'s
  item 11 (power delivery, structural) carries the corresponding caveat, plus
  a warning that an LVS cross-check is not automatically independent
  verification either — a deck whose connectivity setup ends with a
  name-joining rule (e.g. `connect_implicit('*')`) reports a clean "match"
  for two disjoint islands that merely share a net label. Documentation-only
  change: no code in `src/klayout_tools/erc.py` changed, no `schema_version`
  bump.

- **Added** (#2183): `klt erc` specs may declare an optional `devices[]`
  array — `{"name", "body_layer": "<layer>/<datatype>", "on": "<stackup or
  vias name>"}` — naming where a drawn **device body** sits on an
  already-declared conductor role (a PDK's own `RES_MK`/`SAB`/`Resistor`,
  `CAP_MK`/`MIM_L_MK`/`FuseTop`-style marker). Each entry's region is
  subtracted from that role's conductor region before connectivity is
  registered, so a rail-to-rail device string breaks the net at the device
  body instead of reading as a dead short. Without it, `erc.supply_short`
  was a **false positive** for any supply-sensing analog block — a
  power-on-reset comparator, a brown-out detector, a supply-referenced bias
  string — which made `docs/design-evidence-tiers.md`'s T1 item 11
  unsatisfiable by construction for that whole class of design. What each
  declaration actually removed is echoed in the new `provenance.devices`
  (with the measured `body_area_um2`, `0.0` when the marker layer carries
  no geometry), so a carve-out is readable from the report and not only
  from the spec. Additive on both sides: a spec declaring no `devices[]`
  produces an identical report apart from the new empty list, and
  `schema_version` stays `1`. Both the documented limitation and the
  declaration are now written up in
  [`docs/cli/erc.md`](docs/cli/erc.md)'s "Device bodies are not wires"
  section and in item 11 itself.

- **Fixed** (#2182): `klt signoff --manifest`'s staleness gate no longer
  conflates two distinct situations under one `reason: "stale_evidence"`.
  A manifest entry that pins `content_hash` now renders the new
  `"unverifiable_provenance"` when the resolved evidence carries **no**
  input hash at all — a `functional-verification` envelope (no
  `provenance` block by design) or an unprovenanced `"generic"` envelope —
  reserving `"stale_evidence"` for a genuinely *mismatched*, non-`null`
  hash (the check ran, and provably ran against a different revision).
  The two call for opposite remedies: re-run the same producer for a
  genuine mismatch, versus re-produce the evidence with a producer that
  records provenance (or unpin `content_hash`) when nothing was ever
  recorded to compare against. Applies to both the single-artifact path
  (`_grade_evidence`) and T1 item 11's compound citation
  (`_resolve_power_delivery_parts`); a `klt yield` entry is unaffected, it
  always gets a computed fallback hash via its samples document. Every
  item still renders `"unmet"` either way — diagnosis-only change, never a
  new path to `"met"`. Purely additive: no existing field changes shape or
  meaning, so no `schema_version` bump.

- **Added** (#2175): `klt signoff --manifest`'s tier report, and `--fleet`'s
  roll-up (both at the top level and per `blocks[]` row), now carry
  `source_doc_content_hash` — the `sha256:`-prefixed SHA-256 of the resolved
  `docs/design-evidence-tiers.md` (or `--tiers-doc`/`$KLT_TIERS_DOC`
  override)'s own bytes, hashed via the same `sha256_file` helper every
  `klt` envelope's `provenance.input.content_hash` already uses. Previously
  `source_doc` named only *which* doc a report was graded against, not
  *what it said* — two committed reports naming the same `source_doc` could
  have been graded against a checklist that grew or reworded an item in
  between (as the doc did on 2026-09-17, issue #2025's item 11), with no way
  to tell from the artifacts alone. Purely additive: no existing field
  changes shape or meaning, so no `schema_version` bump on either mode.

- **Added** (#2170): `klt place-and-route` now rejects a `request.power`/
  `request.floorplan` pair whose PDN strap geometry cannot physically fit
  the floorplan's core, at request-validation time — before this, a
  platform's own verbatim strap geometry paired with a too-small (or
  `floorplan.method: "utilization"`-derived) core failed deep inside the
  `"floorplan"` stage with OpenROAD's own opaque `PDN-0185` "Insufficient
  width" error, naming neither request field. The new pre-flight check
  derives the same per-layer minimum core dimension `pdngen`'s own
  `Straps::checkLayerOffsetSpecification` checks (`offset_um` + the
  strap's power/ground stripe group width) and raises `PlaceAndRouteError`
  naming both `request.floorplan` and `request.power` instead. Checked for
  `floorplan.method: "explicit"` always, and for `"utilization"` whenever
  the standard-cell area can be derived from the request's own netlist and
  resolved LEFs (falls back to OpenROAD's own `PDN-0185` otherwise, exactly
  as before); `"def"`-method floorplans are not checked. See
  `docs/cli/place-and-route.md`'s "Power delivery" section, "Minimum core
  width for a PDN grid".

- **Fixed** (#2176, additive — **no** `schema_version` bump on either mode,
  see below): `klt signoff --manifest`/`--fleet` no longer grade a cited T1
  item that the running build has no rules for as if they had. The item list
  is parsed from `design-evidence-tiers.md` and the grading rules are
  compiled into the build; `--tiers-doc`/`$KLT_TIERS_DOC` deliberately lets
  those two be at different versions, so a released `klt` handed a newer copy
  of the doc would parse an item it knows nothing about — not its accepted
  envelope kinds, not its evidence shape, not its pass conditions — and grade
  a citation for it through the unrestricted fall-through, rendering a `met`
  row indistinguishable from a correctly-graded one. **Added**:
  `items[].graded_by_build` on every T1 item (`true` when a grading rule
  names its id *or* this build's own shipped doc lists it — so every item of
  the shipped doc, items 1/2/9/10 included), and a top-level `build` block
  (`klt version --format json`'s own `{version, package_version, git_commit,
  git_tag, dirty, is_release}`) on both the tier report and the fleet
  roll-up, so a committed report still names the build that produced it. A
  cited `graded_by_build: false` item now renders
  `unmet`/`ungradeable_by_build` — a new `reason` value — refused *before*
  its evidence is resolved, so a command-backed entry for such an item is
  never run. Uncited ones keep rendering `unmet`/`no_evidence`. Nothing
  changes for the shipped doc: every item is `graded_by_build: true` and
  every verdict is byte-identical apart from the two new fields. A build
  that cannot read its *own* doc claims no divergence (every item `true`),
  since an unprovable refusal is worse than none. No `schema_version` bump:
  both fields are new keys and the new `reason` is a value set growing
  within an unchanged shape — both additive per
  [`docs/json-contract.md`](docs/json-contract.md) — and the grading change
  is a correction of a wrong verdict, like #1987's and #2044's before it,
  not a redefinition of what an already-shipped field means (which is what
  bumped `--fleet` to `2` in #2178).

- **Fixed** (#2171, `klt power`, two independent correctness bugs, both hit
  on the first real chip-level PDN run): a `power_nets` entry now matches a
  net by a **label it carries**, not only by the full comma-joined name
  KLayout assigns from *all* of a net's labels — so `"VDD_CORE"` resolves a
  bus that landed on a pad cell's own `DVDD` plate (net `DVDD,VDD_CORE`)
  instead of matching nothing and blaming the layer/datatype numbers. The
  object form `{"name": ..., "match": "exact"}` restores whole-name
  matching; both the per-net warning and the "none of the requested
  `power_nets` matched" error now list the net names actually present. And
  a **non-rectangular** merged polygon (an L/T/comb-shaped bus, a PDN ring)
  is now decomposed into rectangles and modelled per sub-segment, instead
  of becoming one resistor sized by its *bounding box* — an approximation
  that was silently non-conservative, understating resistance/droop about
  nine-fold on an L whose arms are 20 µm wide inside a 100 × 100 µm
  bounding box, while overstating the per-edge EM limit. A polygon with no
  exact decomposition (non-Manhattan, or over the `MAX_DECOMPOSITION_CELLS`
  cap) makes its island **unsolved** — new additive
  `networks[].islands[].unsolved_reason` field, empty `nodes`/`edges` —
  rather than approximated. Rectangular rails (every `klt par` per-row
  standard-cell rail, and both `gcd` corpus fixtures) take the unchanged
  single-edge path and report byte-identical networks. A pad or
  `current_model` instance that geometrically sits on an unsolved island no
  longer silently reattaches to the nearest node on a different,
  electrically unrelated island of the same net (an unsolved island has no
  nodes by construction, so it could never be "nearest" on its own): it is
  now scoped to the unsolved island it actually sits on, with a pad
  reporting that island's `island_id`/`node_id: null` and an instance's
  current counted into `unsolved_current_a`, both named in `warnings`. And
  the non-rectangular mesh's terminal-end test now derives a cell's flow
  direction from which of its neighbouring grid cells are actually part of
  the polygon, not the cell's own width-vs-height aspect ratio — a cell
  whose vertex-grid cuts happen to make it narrower along the flow than
  across it (any bar with a stub/tee, or non-square cuts) previously dropped
  its real terminal node and fabricated one on a perpendicular wall instead.

- **Added** (#2179): `klt erc` now reports `erc_status` and `erc_coverage` —
  the **connectivity** half's own roll-up and checked-work scope, beside the
  antenna-driven `status`/`coverage`. `klt erc` answers two independent
  questions in one envelope and only one of them needs a PDK: `--pdk`
  resolves against an antenna-ratio limit table this command carries for
  sky130 only, so on every other PDK (and every `--pdk`-less run) *no*
  antenna level can be graded for *any* layout, and the common rollup rule
  correctly reports `status: "not_checked"`/exit 4 regardless of what the
  design does. The `erc_findings` rules (`erc.unconnected_net`,
  `erc.multiply_driven_net`, `erc.supply_short`, `erc.floating_gate`,
  `erc.missing_tie`) are purely connectivity/geometry, run to completion
  with no `--pdk` at all, and are now readable as their own
  `clean`/`violations` verdict rather than only by re-deriving the roll-up
  from `erc_finding_count` per caller. `erc_coverage` (`scope:
  "connectivity"`) names the gates/declared nets/declared ties actually
  checked, recording an undeclared rule as *inapplicable* rather than
  skipped. Both fields are additive: `status`, its exit-code mapping, and
  every other field are byte-identical for every input. `klt signoff`'s
  envelope-aggregation mode reads `erc_status` when `status` is
  `not_checked` (an antenna *violation* still fails, and an envelope with no
  connectivity scope — every one written before this change — grades exactly
  as before); item 11's own grading never read `status` and is unaffected.

- **Fixed** (#2178, `schema_version` 1 → **2** for `klt signoff --fleet`'s
  report only): the fleet roll-up's `blocks[].blocking_item` no longer
  reports a **structurally ungradeable** T1 item — 1 (Design sources), 2
  (Layout), 9 (Testbenches shipped), 10 (Repo hygiene), the four with no
  `klt` verb behind them — as *the* blocker while a gradeable item is also
  unmet. `docs/cli/signoff.md` tells manifest authors the honest default for
  those four is to leave them uncited, so they render
  `unmet`/`no_evidence` by construction; reducing on "first unmet item in
  render order" therefore reported every honestly-authored block as "blocked
  on item 1", hiding its real gaps. The blocker is now the first unmet item
  with a check behind it, falling back to an ungradeable one only when
  nothing gradeable is unmet (a block whose only gaps are those four still
  names one of them, never `null`). **Added**: `blocks[].ungraded_items`,
  every unmet ungradeable item in `blocking_item`'s own
  `{id, title, partition, reason}` shape, so demoting them never hides them;
  the text rendering shows them on one line beside the blocker. Grading is
  unchanged — those four are still `unmet` when uncited, and `tier: "T1"`
  still requires every T1 item, them included, to be `"met"`. The
  `schema_version` bump is because an already-shipped field's *meaning*
  changed, per `docs/json-contract.md`; `--manifest` and
  envelope-aggregation mode are untouched.

- **Fixed** (#2150): `klt extract --parasitics`'s `parasitics.nets[].terminals[]
  .leg_net`/`.hub_net` now stays byte-identical to the written `.spice`
  file's own node spelling for a star/ladder leg or hub net synthesized from
  a **merged-label** net (one whose real name already contains a `|` from
  the issue #696 comma-\>`|` rewrite, e.g. parent net `Y|Y2`). Previously the
  synthesized leg/hub net's real name baked that `|` in directly
  (`Y|Y2__t0`), so KLayout's `NetlistSpiceWriter` treated the literal `|` as
  an unsafe character and hex-escaped it to `Y\x7cY2__t0` in the written
  netlist — a byte mismatch against the JSON-reported `leg_net`/`hub_net`
  value for exactly this case. `_net_identity_name()` no longer pre-converts
  the comma to `|` when minting the real net; the writer's own comma-\>`|`
  rewrite now runs exactly once, matching `spice_safe_net_name()`'s reported
  spelling. Other net-name cases (#696, #1162, #2145) are unaffected.

- **Fixed** (#2169): `klt erc`'s `ties[]` no longer collapses a routed
  design into one electrical island. A declared `well_layer` used to be
  registered and self-connected as a conductor, so a blanket well spanning
  whole standard-cell rows conducted to *every* shape overlapping it in
  plan view — producing a false `erc.supply_short` between VDD and VSS (via
  any ordinary CMOS output net contacted in both wells) and collapsing
  `gates[]` to a single entry, which silently invalidated every antenna
  ratio in the same report. A tie now contributes only its **tap sites**
  (the tap layer clipped to the well), so a well conducts only where its
  taps actually sit. `ties[]` is additionally extracted in its own
  connectivity graph, so `gates[]`, the antenna verdicts, and the `nets[]`
  findings are now identical whether `ties[]` is omitted, declared
  correctly, or declared with an over-broad `tap_layer` — a tie
  mis-declaration can no longer reach anything but `erc.missing_tie`. A
  well holding several taps now passes as soon as *one* of them reaches the
  declared net (previously only the first tap found was probed, which was
  only ever correct because the blanket well merged them all).

- **Added** (#2169): `klt erc`'s `ties[]` entries accept an optional
  `tap_requires` array of `"<layer>/<datatype>"` layers, intersected into
  `tap_layer` to derive the real tap — `{"tap_layer": "22/0",
  "tap_requires": ["32/0"]}` is `Comp ∩ Nplus`. Without it no single-layer
  value can name a real PDK tap: an implant alone is not a conductor, and a
  diffusion/contact layer alone also matches every source/drain contact in
  the same well. Same optional, opt-in shape as `stackup[0].active_layer`;
  omitted leaves `tap_layer` alone, unchanged. No `schema_version` bump —
  additive spec key, no change to the emitted field set.

- **Fixed** (#2110): `klt drc --engine curated` now applies the shared
  coverage rollup. Partial runs report `clean_partial`, zero-work runs report
  `not_checked`, and violations retain precedence. Rules skipped for absent
  layers are classified as `inapplicable` when empty input cannot produce a
  violation, or as `skipped` when it can (currently antenna protection-layer
  checks). Signoff does not count partial evidence as a passing check.

- **Fixed**: `klt sim`'s top-level `status` now applies the common
  partial-success rollup rule (issue #2109) instead of a bespoke
  "no failure means pass" check (issue #2117). Previously, a `limits` object
  whose keys `klt sim` never applies (only `min`/`max` are read — a typo'd
  `"maximum"`/`"minimum"` scored as no bound at all) silently reported the
  unconditional `status: "pass"` whenever it sat beside another measurement's
  own applied, satisfied bound — the exact false-pass this issue closes. The
  run's own `coverage.skipped` already disclosed the gap (issue #1996); this
  fix routes it through `coverage_rollup()`/`rollup_status()` so successful
  checks alongside a nonempty skip list now report the new
  `status: "pass_partial"` value (exit `0`, same token issue #1997 already
  shipped for `klt power`/`klt erc`) rather than `"pass"`. An empty corner
  matrix or a `limits` object using only unrecognized keys still reports
  `status: "not_checked"` (exit `4`) exactly as before; a real limit
  violation or corner error still outranks any coverage gap. `klt signoff`
  already read `"pass_partial"` as non-qualifying partial evidence for
  `sim`-kind checks (issue #2109), so no consumer-side change was needed.

- **Added**: `klt place-and-route`'s `request.power` accepts a `preset` key
  naming a shipped, per-platform PDN recipe — `"gf180mcu_7t_6M"`,
  `"gf180mcu_9t_6M"`, `"sky130hd"` — in place of hand-written
  `power.straps[]`/`power.connects[]` (issue #2123, follow-up to #2086).
  Building a real power grid previously required transcribing a platform's
  `pdn_grid_strategy_*.cfg` / `pdn.tcl` field for field into the request;
  #2086's measured audit catches a half-transcribed block after the fact,
  but not needing the transcription at all is strictly better. Each preset
  is this repo's own transcription of the cited ORFS config, recorded with
  its source file and the exact commit it was read at
  (`OpenROAD-flow-scripts@95ebc50a258390f4c7896e5f04db743f62279c2d`), in
  the same "verified live, not guessed" per-library table style the existing
  tapcell/filler/routing-layer tables use. A preset is expanded into
  `straps`/`connects` *before* validation, so it runs through identical
  checks, emits identical Tcl, and is echoed back in the identical
  `power.straps[]`/`power.connects[]` response fields — plus a new additive
  `power.preset` field naming which recipe produced them (`null` for a
  hand-written block), so a preset run stays auditable rather than opaque.
  `preset` and explicit `straps`/`connects` are **mutually exclusive**, not
  merged per-field: a partial override would recreate the
  "cites a platform config but silently carries one hand-edited value"
  failure mode presets exist to remove. A preset may only be used with the
  standard-cell library it was transcribed for, and an unknown name lists
  the supported set. Additive throughout — no `schema_version` bump, and a
  request that omits `preset` behaves byte-for-byte as before. Verified live
  against real `openroad` (`26Q3-2056-g41a28926b9`) + real PDK installs:
  all three presets reach `power.placed.status: "complete"` with
  `warnings: []` on the GCD worked example. See
  [`docs/cli/place-and-route.md`](docs/cli/place-and-route.md)'s "Platform
  PDN presets" section.

- **Fixed**: `klt extract` no longer writes a net name containing `.` into a
  node-reference position of its SPICE output (issue #2145). A net can arrive
  carrying an instance path joined with a dot — `XBIAS.vb1` — either from
  `klt place-and-route`'s own DEF net names replayed by `--def-net-names`
  (issue #951) or from a drawn label spelling the same convention by hand,
  and those are precisely the internal nodes of a composed, routed cell that
  a `--parasitics` post-layout run wants to probe. `.` is ngspice's own
  hierarchy separator, so such a token was read as a path expression
  (instance `XBIAS` → node `vb1`) wherever a node reference is parsed: the
  node could not be probed, `.meas`'d or `.ic`'d by the very name the netlist
  wrote for it, and the same token meant one thing in the `.SUBCKT` pin list
  and another in a probe directive. Unlike the merged-label comma (issue
  #696) and the leading `$` (issue #1162), KLayout's `NetlistSpiceWriter`
  applies no escape of its own here, so the net itself is now renamed to the
  `_`-joined spelling before the netlist is written: `XBIAS.vb1` →
  `XBIAS_vb1`. Only net/node tokens are affected — SPICE dot-commands
  (`.SUBCKT`, `.ENDS`, `.GLOBAL`) and numeric literals (`L=0.28U`) are a
  different lexical class, written from different inputs, and are untouched.
  The renamed spelling is what every artifact carries, so a simulated node
  name still joins back to the report by exact string match: `nets[].name`,
  `devices[].nets[...]`, `merged_net_labels[].net`,
  `parasitics.nets[].net`/`.hub_net`/`.terminals[].leg_net`, the `--spef`
  output, and `klt lvs`'s `net_correspondence[]`/`mismatches[].net`.
  `--critical-net`/`--distributed-rc`/`--mom-net`/`--mom-rlc-net` match
  against the post-rename namespace (`--pins`/`--def-pins`/`--top-cell-pins`
  still match the raw drawn-label text, as they already did for the comma
  case). Because no dot-free-preserving rewrite can be injective, collisions
  are resolved per netlist rather than per name — the first claimant keeps
  the unsuffixed spelling and each later one takes the smallest free `_<n>`
  suffix — so two distinct nets never share a name in one written netlist;
  identify a net by `net_id`, not by the suffix. Every run that renames at
  least one net says so in `warnings[]`, with the count and up to five
  `before -> after` examples. A net name that never contained a dot — the
  overwhelming majority — is byte-identical to before. No `schema_version`
  bump: no field changes shape, only the net-name values in them. See
  `docs/cli/extract.md`'s "Hierarchical net names are dot-free".
- **Fixed**: `klt extract --abstract-cells` no longer binds several of one
  abstracted macro's own separately declared pins onto a single synthesized
  net, nor absorbs a top-level net that is not one of that macro's pins
  (issue #2142 — a different mechanism from #1911/#1934's cross-instance
  well reclassification, as that report's own ablation matrix showed). Two
  probe-layer defects caused it. (1) The conductor a pin was probed on was
  tracked **per pin**, not per access point: a macro that labels one port on
  two conductors (an `li1.pin` text on the pad its interior drives plus a
  `met2.pin` text on the stub the parent routes to — ordinary hard-macro
  drawing) had every one of that pin's points probed against the
  first-seen label layer, so the off-layer points missed and fell through
  the bottom-up cross-layer fallback onto the first conductor with *any*
  geometry at that coordinate — in a real block, the parent's power strap
  running underneath. Because a named net correctly outranks the unnamed
  island a black-boxed macro's own pad becomes, that strap then won for
  *every* such pin at once. The probe layer is now a property of the access
  point, so each candidate probes the conductor its own label names. (2)
  The `nwell`/`tap` field layers are no longer a cross-layer fallback
  answer unless one of them is the access point's own declared layer: a
  well strap or guard/substrate ring is one continuous shape whose probed
  net is the same design-wide net everywhere it covers, so using it as a
  "nothing else is drawn here" answer merged every pin that missed its own
  conductor onto that one foreign net (PR #622's review had only *demoted*
  well/tap below the metals, not stopped them answering). Such a point now
  resolves to nothing and surfaces through the existing per-instance "no
  conductor found at its resolved access point" warning. The `#1366`
  two-pins-one-net self-check stays a warning rather than becoming a hard
  failure — on a real sky130 block every abstracted standard cell trips it
  legitimately via its `VPB`/`VPWR` and `VNB`/`VGND` body-tie pairs. See
  [`docs/cli/extract.md`](docs/cli/extract.md)'s "Pin resolution".
- **Fixed**: `klt place-and-route`'s routed GDS no longer carries metal
  below the PDK's own minimum-**area** rules, and the response says so
  either way (issue #2139, the `place-and-route` half of #2072 that PR
  #2075 could not reproduce). The DEF→GDS merge now runs a post-route
  minimum-area repair pass: every merged routed-metal polygon is floored
  against its own layer's plain `*.area.*` rule in the resolved PDK
  family's curated deck — the *same* rule set `klt drc` judges the geometry
  with — by drawing an abutting patch into the top cell. Two geometry
  classes this repo does not author drive it: the foundry tech LEF's own
  via landings (sky130's `L1M1_PR_MR` met1 landing is `0.0667 um²` against
  an `m1.6` floor of `0.083 um²`; `M2M3_PR`'s met3 landing is `0.1089 um²`
  against `m3.6`'s `0.240 um²`), and OpenROAD `pdngen`'s met5 via patches
  (`2.272 um²` against `m5.4`'s `4.0 um²`). Every patch is bounded by the
  DEF's own `DIEAREA`, by the layer's own minimum-spacing and
  minimum-width rules, and by a verified single-merged-polygon area check,
  so a repair can never author a short or a sliver. **Additive response
  field** `min_area_repair` (`null` unless `stage_reached` is `"route"`)
  reports `status`/`patches`/`repaired`/`remaining`, per-rule
  before/after counts, and any polygon the pass refused to repair — and a
  pass that could not run at all reports `status: "skipped"` with a
  `reason` rather than an unmeasured clean. See
  [`docs/cli/place-and-route.md`](docs/cli/place-and-route.md)'s
  "Minimum-area repair".
- **Added**: `klt lvs`'s `net_correspondence[]` entries now carry a
  `heuristic` boolean for a `reference.form: "gate-level-verilog"` run
  whose `reference.library` pin orders resolve (issue #2136). Such a
  reference carries no power/ground pins at all, so the comparer had no
  same-named candidate for the layout's `VGND`/`VPWR` and paired it with
  whatever its graph heuristics reached first — routinely an unrelated
  signal net — reporting that fallback as an ordinary, unmarked
  correspondence inside a `status: "match"` report. `heuristic: true` now
  marks a pairing whose layout side is demonstrably a supply net (a pin
  the library declares power/ground lands on it) while the reference side
  is not a supply pin; `heuristic: false` marks every pairing the compare
  stands behind. The key is **omitted** for every other `reference.form`
  and for any run with no derivable supply universe, so "checked and
  genuine" stays distinguishable from "never checked" — read it with
  `entry.get("heuristic")`. Additive: `status`, `mismatches[]`, and the
  `power_connectivity` block are unchanged, and `power_connectivity`
  remains the check that actually validates supply connectivity. See
  [`docs/cli/lvs.md`](docs/cli/lvs.md)'s "Supply correspondences are not
  validated supply connectivity".
- **Fixed**: `klt extract --deck sg13cmos5l --parasitics` now uses sourced
  nominal R/C coefficients for Metal1-Metal4 and TopMetal1, plus all four
  adjacent vertical-overlap pairs (issue #2113). The registered deck
  previously reported zero wire R/C and missing coefficients. Values come
  from SG13CMOS5L's own pinned Magic extraction table; the model remains
  first-order and uncalibrated, without process-corner selection.
- **Fixed**: `klt functional-verification` now refuses requested SDF
  annotation when either required build/test transcript is missing,
  unreadable, or fails during reading (issue #2130). Previously those logs
  were silently treated as clean scans, allowing `annotated: true` and
  post-layout signoff credit without inspected diagnostics. The refusal
  uses the existing error path (exit 1, no success envelope). Readable empty
  logs, known-benign partial-coverage reporting, and non-SDF runs retain
  their existing behavior; no JSON shape change.
- **Fixed** (#2108): DRC, ERC antenna, power EM, simulation and PEX report
  versioned checked-work coverage and refuse known zero checks (`not_checked`,
  exit 4). External KLayout category declarations no longer prove execution:
  absent findings it reports `coverage_unknown`/4. DRC envelope v2 retains
  declarations in `coverage.rule_categories` and corrects `rules_checked`.
  Real failures retain precedence (including ERC/power exit 3). Plain,
  numbered and compound signoff refuse zero, unknown and malformed coverage.
  Existing coverage fields and verb-specific partial behavior remain;
  [the compatibility mapping](docs/coverage-contract.md) lists migration
  details and the remaining Phase 2 adapters.
- **Added**: explicit per-family decisions for eight PDK capabilities and a
  normal-CI check against their live registries/support gates (issue #2132).
  Missing registrations now fail the invariant with contextual diagnostics;
  intentional unsupported capabilities require reasons. Registered coefficient-free
  parasitics remain distinct from missing decks. Existing sparse defaults,
  support restrictions, coefficients, and CLI/JSON behavior are unchanged.
- **Fixed**: `klt signoff` now grants SDF post-layout credit only for literal
  JSON `true` in `environment.sdf.annotated` (issue #2131). Strings, numbers,
  and malformed optional metadata report `sdf_annotated: false` and leave
  digital item 7 unmet with `not_post_layout`, while preserving a valid
  pre-layout regression's plain PASS and item-5 eligibility. Malformed
  `environment` containers also no longer crash the plain detail renderer.
- **Fixed**: `klt lvs`'s `power_connectivity` check no longer admits a
  dangling signal output as a power/ground pin when the design's only
  carrier of that pin name leaves it unconnected (issue #2076). An ordinary
  `place-and-route` output hits this: CTS hangs clock-load cells off each
  leaf clock net with their outputs unconnected, so if no other instantiated
  cell declares a pin of the same name (flip-flops output `Q`, clock buffers
  `X`), nothing in the converted reference mentioned the inverter's `Y` and
  it was admitted beside the real supplies. That inflated `power_pins`, and
  — with clock loads on two different leaf nets, the normal case on anything
  bigger than a toy — reported `power_connectivity.status: "mismatch"` with a
  `power.inconsistent_pin_net` finding on a pin that was never a supply,
  making the check unusable as a gate on exactly the designs it is most
  needed for. A pin name is now admitted only when **every** library cell the
  reference instantiates declares it *and* no reference circuit carries it;
  the previously documented behaviour for real supplies, filler/tap pruning,
  and genuine supply defects is unchanged (all still derived structurally,
  with no per-PDK power-pin name table). See `docs/cli/lvs.md` → "How the
  power-pin universe is derived".
- **Added**: `klt lvs`'s `power_connectivity` block now carries
  `power_pins_derivation` (issue #2076) — the rule that produced
  `power_pins`, the library masters the reference instantiates (the evidence
  it was applied to), and whether the instantiated masters' declared pin
  shapes genuinely corroborate each other. `corroborated` is `true` only when
  at least two instantiated masters declare *distinct* pin sets — not merely
  when more than one master name is instantiated: two drive-strength variants
  of one logical cell (e.g. `mylib__inv_1`/`mylib__inv_2`, both
  `A VGND VNB VPB VPWR Y`) declare the identical shape and corroborate
  nothing, the same evidentiary gap a single master has. Purely additive, so
  no `schema_version` bump: a consumer that quotes `power_pins` as a coverage
  claim can now see when that claim rests on uncorroborated evidence
  (`corroborated: false`, with a `reason` naming the master(s) involved)
  instead of having to reverse-engineer it from the netlist.
- **Added**: `klt place-and-route` now reports the power delivery it
  actually *placed*, and warns loudly when there is none (issue #2086).
  `request.power` is optional, and omitting it produced a run that
  completes — exit 0, a routed DEF, a merged GDS, real area/timing numbers
  — on a layout with **no PDN, no substrate/well taps, and (on a library
  with no row-rail fallback) no fill at all**, with nothing in the output
  saying so. The response's new `power.placed` block is
  measured from the DEF the run wrote (`components`/`tapcells`/`endcaps`/
  `fillers` instance counts, plus one `special_nets[]` entry per DEF
  `SPECIALNETS` net with its `FOLLOWPIN` rail segments, `STRIPE` strap
  segments, `stripe_layers` and PDN `vias`), graded into
  `status`/`missing`. A new top-level `warnings` array (`[]` when empty,
  never `null`) fires both when `request.power` was omitted and when a
  *supplied* block produced an incomplete grid — the transcription-error
  case, where a strap layer that draws nothing is just as silent. Every
  warning's prose is derived from the measured counts (an omitted-block run
  on `sky130_fd_sc_hd` really does place rails and fillers via the issue
  #1442 row-rail fallback, and the warning narrows to the taps, straps and
  PDN vias that are genuinely absent), and each expected supply is graded
  on its own, so a routed `VDD` beside a bare `VSS` entry grades `partial`
  rather than `complete`. The CLI writes each warning to **stderr** in both
  `--format json` and `--format text`, leaving stdout a single parseable
  document. `--format text` also prints the placed counts unconditionally.
  Measured zeros and unavailable evidence are kept distinct:
  `power.placed.evidence` is `"def"` only when a DEF was read and parsed —
  which requires its `COMPONENTS` and (when present) `SPECIALNETS` sections
  to carry exactly the number of records they declare and to terminate
  their last record, so a truncated or miscounted section is reported as
  unavailable rather than as a measured zero or a smaller-but-complete
  grid — otherwise every count is `null` with an `unavailable_reason`,
  never a fabricated `0`. This warns,
  it does not refuse: `status` stays `"ok"` and the exit code stays `0`, so
  a caller wanting a hard gate composes it on a non-empty `warnings` or
  `power.placed.status != "complete"`. Additive throughout; no
  `schema_version` bump.
- **Added**: `tests/corpus/place_and_route/gcd-pdn.gds.gz` (issue #2079) — the
  `gcd` worked-example design routed **with** a real `request.power` PDN
  (ORFS's own `platforms/sky130hd/pdn.tcl` met1-followpins rail plus
  met4/met5 straps, `power_net`/`ground_net` = `VPWR`/`VGND`), committed
  beside the existing, deliberately grid-less `gcd.gds.gz` rather than
  replacing it. Each supply resolves to exactly **one** net/island on the new
  fixture (`klt lvs`'s `power_connectivity`: `"match"` across 1349
  instances; `klt power` on the full met1–met5 stackup: 1 `VPWR` + 1 `VGND`
  island) versus 17 per supply on the grid-less one, which stays byte-
  identical — so every count previously measured against it, in tests and
  docs alike, is unchanged. `tests/corpus/place_and_route/regenerate.sh` now
  builds both, takes optional design-name arguments so one fixture can be
  rebuilt without rewriting the others, and gates a power-bearing fixture on
  a real gate-level `klt lvs` `power_connectivity` check before committing
  it. No `klt` command behavior changed.
- **Added**: `klt place-and-route` and `klt sta` now retain every OpenROAD
  script invocation's `stdout.log`, `stderr.log`, and `invocation.json` under
  `.klt/place-and-route/openroad-logs/<invocation_id>/` and
  `.klt/sta/openroad-logs/<invocation_id>/` respectively (issue #2124) — main
  stages, corner sweeps, and the optional SPEF timing session each get their
  own directory, and a retry allocates a new invocation ID rather than
  overwriting an earlier transcript. Successful JSON responses expose these
  as new fields: `klt place-and-route` adds `engine_logs`, an array in
  invocation order; `klt sta` adds `engine_log` for a single-corner response
  and `corners[].engine_log` per corner for a multi-corner response. Each
  entry carries `invocation_id`, `script_name`/`script_sha256`,
  `script_path`/`metrics_path`, `directory`/`stdout_path`/`stderr_path`/
  `metadata_path` (the new path fields use the shared `{path, scope}`
  envelope), `outcome`/`returncode`, and `retention_errors` for any secondary
  write failure. An engine or missing-metrics error keeps its existing
  diagnosis and appends the invocation's log locations to `error.message`;
  a log-write failure never masks or replaces the underlying engine result.
  Purely additive — no `schema_version` bump for either command. See
  [`docs/cli/place-and-route.md`](docs/cli/place-and-route.md#retained-openroad-logs)
  and [`docs/cli/sta.md`](docs/cli/sta.md#retained-openroad-logs).
- **Fixed**: `klt gen-compose`'s via-drop router now sizes a multi-hop
  ladder's intermediate landing pads against each landing layer's own
  minimum-*area* DRC rule, not just the fixed `_VIA_LANDING_SIZE_UM`
  (0.42um, 0.1764um²) square (issue #2072). A route reaching a deep pin
  (e.g. a `bond_pad`'s `top_metal`/met5 port) via sky130's full
  li1→met1→met2→met3→met4→met5 stack previously drew an isolated,
  sub-minimum-area landing pad on met3/met4 — a guaranteed
  `met3.area.1`/`met4.area.1` violation under `klt drc --deck sky130`
  once issue #1989 gave the curated deck a `met*.area.1` rule at all. The
  digital `klt place-and-route` flow (OpenROAD-authored `VIA_`-prefixed
  via cells, a separate code path) was investigated and did **not**
  reproduce this on any of the three checked-in corpus fixtures — see the
  issue for the full investigation.
- **Added**: `klt gen-compose --format json` now emits `blocks[].source_path`
  and `blocks[].source_digest` (issue #2065), recording which stream each
  composed block's geometry was read from and a digest of that stream's
  contents. A composition is a snapshot of inputs that keep moving under it,
  and the report previously described only its own output — so "does this
  composition still match what its inputs publish today?" could not be
  answered from the report at all. `source_digest` is a **layout-aware**
  digest (new `_provenance.layout_geometry_digest`), not a raw-byte file hash:
  it covers the decoded geometry (dbu, cells sorted by name, each cell's
  shapes keyed by layer/datatype and instances keyed by the placed cell's
  name, canonicalised and sorted), so a re-written but geometrically identical
  input hashes identically despite new `BGNLIB`/`BGNSTR` timestamps and
  reordered elements, while real geometry drift shows up as a different
  digest. `null` when it cannot be computed (stream moved/unreadable) — never
  fabricated. Purely additive — no `schema_version` bump (`klt gen-compose`
  stays at `1`), and the composed GDS itself is untouched. The raw-byte
  `sha256_file`/`provenance.input.content_hash` contract `klt
  drc`/`lvs`/`extract`/`sim` rely on is unchanged.
- **Added**: a shared `coverage.nothing_checked` /
  `coverage.nothing_checked_reasons` convention (issue #1996), declared once in
  `src/klayout_tools/coverage.py` and documented in
  [`docs/json-contract.md`](docs/json-contract.md). Several verbs could report a
  passing top-level verdict on a run that checked **nothing**, with no field a
  reader could consult to tell that apart from an earned pass: a PDK-native DRC
  deck whose whole rule set is gated behind a `--deck-var` the caller never set
  (`status: "clean"`, `violation_count: 0`), a `klt sim` request whose PVT corner
  matrix expanded to zero corners or whose every `measurements[].limits` object
  used keys `klt sim` does not apply (only `min`/`max` are read), and a `klt pex`
  run that produced no `delta[]` row at all. All three now say so in their own
  `coverage` block — `klt drc` gains `coverage.rules_checked` (the complement of
  the existing `rules_skipped`, and, for `--engine klayout`, the rule categories
  the deck's own RDB report declares) alongside the two roll-up keys, `klt sim`
  and `klt pex` gain a `coverage` block they previously had none of. **No
  producing verb's `status` changes**: `test_run_drc_coverage_empty_stream`'s
  `"clean"` verdict and every other pinned verdict are untouched, and the keys
  are purely additive (no `schema_version` bump). `nothing_checked` is never a
  synonym for *partial* coverage — a run that checked one rule out of eighty
  reports `false`, and a `klt pex` comparison that ran and found every row within
  tolerance emits one `delta[]` row per compared pair, so "nothing changed" and
  "nothing was compared" stay structurally distinct.
- **Changed**: `klt signoff` now **refuses** evidence whose own `coverage` block
  reports `nothing_checked: true` (issue #1996) — unlike the report-not-enforce
  treatment #2002 gave partial coverage gaps, because a run that measured nothing
  contains no statement about the design for a reviewer to weigh. Such a check's
  `passed` is `false` whatever its `status` says (with
  `checks[].detail.nothing_checked_reasons` naming why), and a tier-report item
  citing it renders `"unmet"` with the new `reason: "nothing_checked"` — grouped
  with `wrong_kind`/`not_post_layout` as a "no runnable check proves this item"
  reason, never with `check_failed`, per issue #826's invariant. Read generically
  from the shared convention rather than hard-coded per verb, so a future verb
  adopting it is picked up automatically. Back-compatible: an envelope with no
  `coverage` block, one predating the convention, or one reporting
  `nothing_checked: false` grades exactly as it did before — the refusal fires
  only on an explicit `true`.
- **Added**: `klt gen` and `klt gen-compose` `--format json` reports now carry
  the shared top-level `provenance` block (issue #2035) — the same one
  `klt drc`/`lvs`/`extract`/`sim` already emit, built by the same
  `build_provenance()` helper. `klt_version` and `klayout_version` are the
  point: a project that commits generated geometry plus its report as evidence
  and re-runs the generator later could previously not tell a real geometry
  change from a klt/KLayout upgrade — the `gen` family's report was the one
  `klt` artefact whose producing build could not be read back off the artefact.
  `provenance.pdk` mirrors the identity of each report's existing top-level
  `pdk` field; `provenance.deck` and `provenance.input` are always `null` for
  both verbs, since a generator/compose request is parameters (plus, for
  compose, the blocks' own generator sub-reports), with no rule/model deck and
  no single input layout stream to pin. Purely additive — no `schema_version`
  bump on either command (both stay at `1`).
- **Changed**: `klt signoff` now validates every ingested envelope against a
  declared shape for its kind (issue #2033, decomposed from #2011 item 2 — the
  pilot for a typed, runtime-validated evidence boundary). Each recognised
  kind is declared as a `TypedDict` (`src/klayout_tools/signoff.py`), and
  `_classify` checks the incoming JSON against that kind's required fields at
  read time: an envelope that matches a kind's discriminating shape but is
  **missing a required field or carries one of the wrong type** is now
  rejected rather than graded. Envelope-aggregation mode exits `1` naming the
  kind and the offending field; `--manifest` grading renders the citing item
  `"unmet"` with `reason: "unrecognized_envelope"`. **Grading change**: the
  case this closes is `klt extract`, the one kind with no independent
  pass/fail — a truncated extract envelope with no `status` field previously
  produced a *passing* check (the "an envelope that cannot fail satisfies a
  checklist item" failure mode of issues #1987/#1988). Required fields are
  only each kind's discriminators plus the field its verdict is derived from;
  every other field this verb reads stays optional, so evidence committed
  before a later-added block existed (a `drc` report with no `coverage`, an
  `lvs` report with no `power_connectivity`) validates and grades exactly as
  before — verified against every `klt` envelope committed under `examples/`
  and `evidence/`. No JSON shape change (no field added, renamed, removed or
  nested), and no `schema_version` bump. This catches malformed/incomplete
  envelopes only: it cannot detect a semantic mismatch between two well-formed
  values (e.g. issue #1999's escaped-identifier mismatch), and is documented
  as such in [`docs/cli/signoff.md`](docs/cli/signoff.md)'s new "Envelope
  validation" section.
- **Added**: a FastCap-backed **capacitance cross-validation oracle** for
  `klt mom` (issue #2015, pairing #4 of tracking issue #2007). `klt mom`'s
  Maxwell capacitance matrix came from one implementation
  (`native/mom/src/solver.rs`) with only two checks on its numerics: analytic
  closed forms, which exist for about four idealised shapes, and the NEC2++
  cross-check, which covers the *full-wave* solver path and says nothing about
  capacitance. `tests/test_mom_capacitance_oracle.py` now runs
  [FastCap 2.0](https://github.com/ediloren/FastCap2) — the 1992 M.I.T. solver
  `solver.rs` itself cites as the method it implements, with analytic
  panel-to-panel integrals where this repo's core uses a point-charge kernel —
  over the *same* conductor geometry, meshed to the same panel set, and
  compares every Maxwell-matrix entry inside a stated band. Measured
  agreement: **0.17%** on a coupled-line pair, **1.29%** on a three-conductor
  shielded triple, **3.29%** on the flat-lamina parallel-plate fixture (whose
  larger difference is a documented kernel accuracy gap, not a defect); two
  seeded geometry defects — a 0.5 µm spacing error and a deleted ground plane
  — move entries by 19% and 39%, an order of magnitude outside that band, and
  both solvers size each defect to within 1.3% of each other. FastCap was chosen
  over Palace (#2007's other candidate) because it is the same method class,
  takes exactly the panel set `klt mom` already builds, and builds in ~7
  seconds with no dependencies, so it gates every PR rather than needing its
  own opt-in workflow. New provisioning: `scripts/install-fastcap.sh` (pinned,
  checksummed `ediloren/FastCap2` `master` — M.I.T.'s permissive 2003
  relicensing, not the `WRCad` branch's noncommercial one — plus
  `scripts/patches/fastcap-2.0-modern-toolchain.patch`, two build-only hunks
  for a 2020s C compiler), wired into `ci.yml`'s existing `Native engines
  (Rust)` (`mom`) leg with a no-silent-skip gate. The module skips cleanly
  when `fastcap` is absent. No `klt` runtime behaviour or JSON shape changes —
  FastCap stays an oracle, never a runtime dependency. Methodology, the
  FastCap-vs-Palace decision, measured results, the declared shared surface
  and the unsupported cases: `docs/design/fastcap-oracle.md`.
- **Added**: `klt erc --format json` now emits `provenance.spec` as
  `{"content_hash": "sha256:<hex>"}` (issue #2036), pinning the *contents* of
  the stackup/vias/nets/ties spec file the run was validated against.
  `provenance.input.content_hash` (issue #1968) already pinned the layout, but
  an ERC verdict is only meaningful relative to the declarations it was run
  with: the top-level `spec` field echoed a bare path, so editing the spec
  (dropping a `ties` entry, re-pointing a `layer`) left a committed report
  silently asserting a verdict for declarations it never saw. This is a
  verb-local field rather than a new `build_provenance()` parameter, following
  `klt lvs`'s precedent for a second input (`environment.reference_sha256`).
  Purely additive — no `schema_version` bump (`klt erc` stays at `1`), and
  `provenance.input`/`provenance.pdk`/`provenance.deck` are unchanged.
- **Added**: `provenance.input.role` — a discriminator naming *which kind of
  artifact* `provenance.input.content_hash` covers (issue #2027), one of
  `"layout"` (a GDSII/OASIS stream or a DEF), `"netlist"` (a SPICE or
  gate-level Verilog netlist), or `"source"` (HDL source). The hash alone was
  kind-blind, and one verb can pin either kind: `klt lvs`'s `layout.file`
  shape hashes the original layout stream, but its pre-extracted
  `layout.netlist` shape hashes a *SPICE netlist*. **Bug fix**: `klt signoff`
  compared `input.content_hash` across every check that populated it, so a
  `klt drc` report and a pre-extracted-shape `klt lvs` report of the *same
  design* could never agree and the run rendered `status: "refused"` — a
  false alarm rather than caught staleness, reproducible with this repo's own
  `examples/signoff/` pair (whose `lvs.request.json` uses that shape). The
  gate now compares hashes only among checks declaring the same `role`,
  exactly as it already compared `deck.content_hash` only among checks naming
  the same deck; a genuine disagreement *within* a role is still refused, and
  a bundle disagreeing on two roles reports one `mismatches[]` entry per role
  (same `field`, distinguished by a new additive `mismatches[].role` key,
  also shown in `--format text`). Evidence committed before this field
  carries no `role` and is read as `"layout"` — the field's only documented
  meaning at the time — so archived reports keep participating in the
  layout-side comparison rather than being silently exempted from it.
  Additive throughout: `role` appears whenever `input` is non-`null`, nothing
  is renamed or removed, and no `schema_version` bumps. A knock-on effect is
  that `klt sim` (and any other netlist-only verb) *can* now populate
  `provenance.input` for the `--manifest` staleness pin without poisoning the
  cross-check; wiring that up is separate work.
- **Fixed**: `klt sim` now populates `provenance.input` (issue #2039), the
  follow-up #2027 explicitly deferred. It carries `role: "netlist"` and the
  same `sha256:`-prefixed digest `environment.netlist_sha256` already hashes
  for the netlist under test — deliberate duplication, matching the
  precedent issue #1969 established for `klt lvs`'s own layout-side hash.
  Before this fix, `klt sim` left `provenance.input` `null`, so a T1 item-5
  citation in `klt signoff --manifest` pinning an expected `content_hash`
  against a `klt sim` report always graded `stale_evidence` — a pinned hash
  can never match `None`, whether or not the netlist actually moved. The
  `netlist` role keeps a `drc` + `sim` bundle out of the layout-role
  comparison, so a bundle describing one design still aggregates to `pass`
  rather than `refused`. This is a user-visible `pass` → `refused` change for
  a bundle pairing `sim` with another `netlist`-role report — `klt lvs`'s
  pre-extracted (`layout.netlist`) shape, `place-and-route`, or `sta`'s
  `verilog` request — unless they pin the same netlist file: deliberate, as a
  post-layout `sim` of the netlist `lvs` verified should bind to that digest,
  while a schematic-level `sim` should not be bundled with a citation of a
  later design stage. See `docs/cli/sim.md` and `docs/json-contract.md`'s
  `provenance.input.role` section. Purely additive — no `schema_version`
  bump (`klt sim` stays at `3`). Separately, `klt wave build`/`klt wave
  query`'s role-less `provenance.input` (built by the native `klt-wave` Rust binary,
  not this module) was evaluated for the same treatment and kept as a
  deliberate, permanent exception rather than closed: neither existing
  `role` value honestly describes what either verb hashes (a waveform trace
  or a built store), and no `klt signoff` check kind consumes either block
  today — see `docs/json-contract.md`'s `provenance.input.role` section for
  the full rationale.
- **Added / Changed**: the T1 (bronze) evidence checklist in
  [`docs/design-evidence-tiers.md`](docs/design-evidence-tiers.md) gains
  **item 11, "Power delivery (structural)"** (issue #2025, operator ruling
  2026-09-17), and `klt signoff --manifest`/`--fleet` grades it. A fleet
  survey found 2 of 7 committed digital layouts had no power delivery
  network at all while still citing an LVS `match` toward item 4 — nothing
  in T1 required a power grid to *exist*. Item 11 asks the structural
  question directly, per block kind:
  - **digital** (RTL flow) — the cited `klt place-and-route` response
    reports `power.pdn: true` with a `power.tapcell_master` named, every
    `power.straps[].layer` is covered by the cited `klt erc` spec's own
    stackup, and the cited `klt lvs` report's `power_connectivity.status`
    is `"match"` (`"unchecked"` satisfies item 4 but **not** item 11).
  - **analog / custom** (and the doc's full-custom digital sub-case, which
    has no P&R run to cite) — the cited `klt lvs` report's
    `net_correspondence` pairs every declared supply net to a
    reference-side net, i.e. the reference netlist carried the supplies. A
    SPICE reference satisfies this by construction.
  - **both** — a `klt erc` **supply-spec** run declaring at least one
    `"kind": "supply"` net and at least one `ties[]` entry, reporting no
    `erc.unconnected_net`/`erc.supply_short` on a declared supply and no
    `erc.missing_tie`. Item 11 grades those rules specifically, *not* the
    ERC envelope's own `status`, so an antenna verdict on an unrelated
    signal net (or the tie-cell false positives issue #1994 tracks) cannot
    block a power-delivery claim.

  IR-drop and EM (`klt power`) deliberately stay **out** of T1 (silver-tier
  material): `_ITEMS_ACCEPTING_POWER_EVIDENCE` is still empty, and item 11
  is the *structural* question ("is the supply connected to what it
  powers"), never the *analysis* one. **Grading change**: `t1_item_count`
  is now `11` (`22` for `mixed-signal`), so every block that was at T1
  before reports one more item and drops to `tier: null` until it cites
  item 11 — which is precisely why the doc required an operator ruling
  before this item could be added (#1982). The count was already the parsed
  checklist's own length, never a literal in code.

  Supporting additive changes, all documented in
  [`docs/cli/signoff.md`](docs/cli/signoff.md): `klt erc` and `klt
  place-and-route` responses are now recognised envelope kinds (`"erc"`,
  `"place-and-route"`) in both modes, each **opt-in to item 11 alone** the
  way `"generic"` is scoped to item 8 — an `erc` or `place-and-route`
  citation for any other item renders `wrong_kind`, which matters most for
  the latter, since a P&R response passes on `status: "ok"` alone and an
  unrestricted citation would reopen the "cannot fail, therefore always
  passes" hole issue #1987 closed for `klt extract`. Item 11's manifest
  entry may be a **JSON array** of ordinary evidence entries (the first
  compound item; every other item still takes exactly one), each resolved
  through the same read/run/classify/hash path as any single citation. Its
  `citation` gains two item-11-only members, `parts[]` and
  `power_delivery`, alongside the unchanged single-citation fields. Four
  new `reason` values distinguish which artifact to go fix: `no_pdn`,
  `supply_spec_incomplete`, `supply_not_continuous`, `lvs_supply_unproven`.
- **Changed**: `klt signoff --manifest`/`--fleet` now restricts T1 items 5
  ("Full corner verification vs a ratified spec"), 6 ("Statistical claims carry
  Monte Carlo evidence") and 8 ("Characterization report") to the evidence
  kinds `docs/design-evidence-tiers.md` actually names for them (issue #2044),
  completing what issue #1987 started for items 3 and 4. All three previously
  accepted *any* recognised, passing envelope kind — including `klt extract`,
  which has no failure verdict and so always counts as passed, so a manifest
  citing `{"5": "extract.json"}` graded a corner-verification claim `met` on a
  report that ran no simulation and could not have failed. **Grading change**:
  item 5 accepts `sim` for an analog partition and `sta`/
  `functional-verification`/`sim` for a digital one (`sim` being the
  full-custom digital sub-case's own artifact); item 6 accepts `yield`; item 8
  accepts `generic`, the purpose-built envelope issue #1152 gave the one T1
  item naming no `klt` verb. Any other kind moves from `met` to `unmet` with
  `reason: "wrong_kind"`. Items 1, 2, 9 and 10 are deliberately unchanged and
  still accept any passing envelope, `extract` included — the doc documents
  them as having no tool behind them, so there is no right artifact to restrict
  them to. `klt extract` is otherwise untouched: its envelope still aggregates
  normally in envelope-aggregation mode (`checks[]`, device/net counts) and its
  `provenance` still participates in the consistency check that binds LVS to
  the DRC'd layout. `examples/signoff/manifest.json` cites only items 3 and 4
  and is unaffected. No JSON shape change (no field added, renamed, removed or
  nested).
- **Changed**: `klt signoff --manifest` now restricts T1 items 3 ("DRC clean")
  and 4 ("LVS clean") to a `drc` and an `lvs` citation respectively, for every
  block kind (issue #1987). Both items previously accepted *any* recognised,
  passing envelope kind — including `klt extract`, which has no failure
  verdict and so always counts as passed, meaning a manifest citing
  `{"3": "extract.json", "4": "extract.json"}` graded both items `met` on a
  report that ran neither check and could not have failed. **Grading change**:
  a manifest citing a non-`drc` kind for item 3, or a non-`lvs` kind for item
  4, moves from `met` to `unmet` with `reason: "wrong_kind"` — the same
  outcome item 7 has rendered for a non-`pex` citation since issue #871. The
  cited check did not fail on its own terms; it simply is not the check the
  item names. `examples/signoff/manifest.json` already cites the right kinds
  and is unaffected. Paired with issue #1969's `provenance.input.content_hash`
  population below, an item-4 "LVS clean" claim is now both *the right check*
  and *bound to the layout DRC ran on*: `provenance_consistency` refuses a
  manifest whose `drc` and `lvs` envelopes pin different layout hashes. No
  JSON shape change (no field added, renamed, removed or nested).
- **Added**: a magic-backed **cross-validation oracle** for `klt drc` and
  `klt extract` (issue #2014, pairing #1 of tracking issue #2007). Both verbs
  were backed by KLayout alone — `tests/test_lvs.py`'s netgen tier
  cross-validates LVS *comparison*, but both of its comparators are fed by
  KLayout's own extraction, so extraction and DRC themselves had no
  independent check at all. `tests/test_drc_magic_oracle.py` and
  `tests/test_extract_magic_oracle.py` now run
  [magic](http://opencircuitdesign.com/magic/) — a separate geometry engine
  with its own open_pdks sky130/gf180mcu decks — over the *same* GDS bytes and
  compare verdicts: zero violations on a clean sky130/gf180mcu corpus cell,
  and agreement on three seeded defects (a 0.09 µm met1 spacing violation, an
  input-to-output short, 11 deleted `licon1` cuts). Extraction agreement is
  exact — device count, per-class counts, every `w`/`l`/`as`/`ad`/`ps`/`pd`
  parameter, drain/gate/source connectivity, and net count all match on both
  PDKs; DRC is compared as zero/non-zero plus rule identity and location,
  because the two engines paint a spacing failure with different granularity.
  Both modules are real-binary-gated and skip cleanly (with a specific reason)
  when `magic` or a magic technology file is absent, so no existing CI leg
  changes. New provisioning: `scripts/install-magic.sh` (pinned, checksummed
  magic 8.3.683 built `--without-x`; the distro 8.3.105 is rejected by decks
  that declare `requires magic-8.3.411`), `scripts/fetch-magic-tech.sh`
  (generates `sky130A`/`gf180mcuC` decks from pinned open_pdks source rather
  than requiring a multi-GB PDK install), and a `workflow_dispatch`
  `magic-oracle.yml` job that provisions both and fails if the tests skip.
  No `klt` runtime behaviour or JSON shape changes — magic stays an oracle,
  never a runtime dependency. Methodology, measured results, the declared
  shared surface and the unsupported cases: `docs/design/magic-oracle.md`.
- **Added**: device-body tie/bias status is now a **gradeable field**, not
  only a warning (issue #1983). `klt lvs` emitted `device.body_unverified` as
  a `mismatches[]` entry that never changed `status`, and `klt pex` said
  nothing at all about whether the netlist it re-simulated had a DC bias path
  for its device bodies — even though `docs/cli/extract.md` states that an
  untied body makes such a resimulation "physically wrong, not merely
  imprecise". That silently weakened `docs/design-evidence-tiers.md` item 7
  (post-layout verification, the item with the strictest citation rule in the
  checklist): a `klt pex` citation could be backed by numbers that look like
  measurements and are not, with nothing in the evidence saying so. Three
  additive blocks close that:
  - `klt lvs` gains a top-level **`body_verification`** block — `status`
    (`"verified"`/`"unverified"`/`"unchecked"`), `reason`, `device_classes`,
    `device_count`, `findings`, `finding_count` — rendered from the same
    determination as the existing `device.body_unverified` warnings, so the
    two can never disagree. `"unchecked"` (the pre-extracted
    `layout.netlist` form, which verifies nothing either way) is now
    distinguishable from `"verified"`; before, both simply carried no
    warning.
  - `klt pex` gains a top-level **`body_bias`** block — `status`
    (`"biased"`/`"unbiased"`), `unbiased_device_count`, `unbiased_nets`, and
    `unbiased_pmos_body_nets` carried verbatim from the extraction this
    command drives itself (`klt extract`'s own issue-#555 array).
  - `klt signoff` surfaces both: `checks[].detail.body_verification_status`
    on an `lvs` check, `checks[].detail.body_bias` on a `pex` check, and
    `citation.body_bias` on a `"met"` item-7 citation (plus the `--format
    text` rendering of the latter), so item 7's verdict and the one property
    that can invalidate the numbers backing it sit in the same artifact.

  **No verdict changes anywhere**: `klt lvs` still reports `status: "match"`
  for a matching compare with unverified bodies (the warning's non-blocking
  severity is unchanged), `klt pex` still reports `status: "pass"` and the
  same exit code, and `klt signoff` grades an `lvs` check on `status ==
  "match"` (plus the existing `power_connectivity` gate) and a `pex` check on
  `status == "pass"` exactly as before. This is report-before-enforce, and a
  deliberately different default from #1965's `power_connectivity` hard-fail:
  a power-connectivity mismatch is a *defect*, whereas an unverified/unbiased
  body is a *coverage* condition some PDK decks produce on every layout they
  extract — hard-failing it would retroactively fail whole PDKs' worth of
  otherwise-valid evidence. `docs/design-evidence-tiers.md` item 7 now states
  the condition a `pex` citation is valid under, claimant-enforced exactly
  like item 3's DRC-coverage disclosure. Purely additive and back-compatible:
  the `body_bias` key is absent for every non-`pex` kind and for `pex`
  evidence committed before this change (an absent statement reads as "this
  artifact made no body-bias statement", never "every body was biased"),
  `detail.body_verification_status` is `null` for pre-#1983 `lvs` evidence,
  and `klt lvs --check --rerun` does not report either new block as drift on a
  report committed before it existed. No `schema_version` bump.
- **Added**: `klt signoff` now reports the cited DRC envelope's `coverage`
  block (issue #2002). `docs/design-evidence-tiers.md` item 3 requires a claim
  to enumerate its deck's coverage gaps and, since issue #1982, names the
  exact fields that disclosure must quote — `coverage.layers_in_stream_without_rules`,
  `coverage.rules_skipped`, `coverage.deck_scope` — but `klt signoff` read
  none of them, so a deck with twenty rule-free drawn layers and sixteen
  skipped rules produced exactly the artifact a fully-covering deck did. All
  three are now surfaced verbatim in `checks[].detail.coverage`
  (envelope-aggregation mode), in a `"met"` item's `citation.coverage`
  (tier-report mode — item 3's own artifact), and in `blocks[].drc_coverage`
  (fleet roll-up), plus the `--format text` rendering of the latter two.
  **No verdict changes**: a `drc` check still passes on `status: "clean"`
  alone, so no claim that graded `met` before this change grades differently
  after it — this is report-before-enforce, and item 3's disclosure
  requirement remains claimant-enforced (nothing compares the reported gaps
  against what a claim actually disclosed). Purely additive and back-compatible:
  the key is absent for every non-`drc` kind and for DRC evidence committed
  before `klt drc` reported coverage, so an absent coverage statement reads as
  "this artifact reported no coverage", never "this deck has no gaps", and a
  `--fleet` run mixing pre- and post-`coverage` evidence renders both without
  error. Whether a non-empty gap should ever change item 3's verdict, where a
  claimant would state a disclosure for `klt signoff` to compare against, and
  whether `klt lvs`'s own coverage-shaped disclosures deserve the same
  treatment for item 4 are open questions #2002 deliberately left unanswered.
- **Fixed**: `klt lvs` no longer reports `body_verification.status` as
  `"verified"` for a layout whose PMOS bodies actually float (issue #2048).
  The PMOS half of the determination (`_body_unverified_counts`) was
  *deck-structural*: it only counted devices when the deck declared no well-tap
  mechanism at all (`tap`/`tap_nplus`/`tap_pplus` all absent), so a deck that
  merely **declares** one — gf180mcu's derived `tap_nplus`/`tap_pplus`, issue
  #1084 — was treated as proof that every PMOS in every layout it extracts
  reached a real net. It is not: a gf180mcu layout that draws no well tie
  leaves each PMOS body on an anonymous, KLayout-synthesized `$<n>` net with no
  DC bias path, and that layout still reported `"verified"` — while `klt pex`'s
  `body_bias` block, built from `klt extract`'s per-device
  `unbiased_pmos_body_nets[]` (issue #555), reported the very same layout as
  `"unbiased"`. The arm is now per-device on both sides, so the two commands
  agree and `"verified"` means what [`docs/cli/lvs.md`](docs/cli/lvs.md) says it
  means ("**every** MOS body terminal ... resolved to a real drawn/derived
  net"). Affects the `device.body_unverified` warning and the
  `body_verification` block identically — they are rendered from the one
  determination and still cannot disagree. sky130 is unchanged (its
  `well_label`, 64/5, demonstrably names every PMOS body), now verified
  per-device rather than excused by the deck's shape. **Strictly more
  reporting, no verdict change**: the warning's non-blocking severity is
  unchanged, so a matching compare with newly-counted PMOS bodies still reports
  `status: "match"` and the same exit code; a gf180mcu report that previously
  said `"verified"` now says `"unverified"`. No JSON shape change and no
  `schema_version` bump.
- **Fixed**: `klt extract --abstract-cells` no longer binds an abstracted
  cell's **unused tie output** to the design's named supply net (issue #1994).
  The extra pin access points discovered by walking a cell's own pre-erasure
  poly/diffusion connectivity could cross from one declared pin to another —
  for a tie/constant generator such as `sky130_fd_sc_hd__conb_1`, which ties
  `HI` to `VPWR` (and `LO` to `VGND`) through a plain poly strip with no
  diffusion at all, the walk handed the signal pin a candidate sitting on the
  cell's own supply rail. The documented "a named net beats an unnamed one"
  ranking then preferred it over the pin's own (correctly unnamed, because
  genuinely unrouted) island. The effect was backwards: a design extracted
  `match` while its power grid was *missing* and produced dozens of false
  `klt lvs` errors the moment the grid was fixed. Candidate discovery now
  stops at a second declared pin — when the cell-local net it walked onto
  carries another pin's label (scanned across every label layer the deck
  declares), only fragments carrying this pin's own label are kept, so the pin
  falls back to its own declared access point. A tie output the parent
  genuinely routes to the rail still binds to it (that connection is drawn
  outside the cell and survives abstraction). No JSON shape change.
- **Fixed**: `klt equiv --engine yosys-sequential` no longer drops a
  top-level port whose name is a Verilog **escaped identifier** (issue
  #1999) — the leading-`\` spelling a synthesis/P&R flow uses for a name
  containing `.`, `[`, `]` or `/`, e.g. a flattened hierarchical output
  `\q.x` or a bit-blasted bus bit `\q[0]`. Such an identifier is terminated
  by whitespace, so Yosys writes its declaration as `output \q.x ;` (a space
  before the semicolon) and the stage-1 port-list parser did not match it at
  all. The port was therefore misclassified as an internal wire and
  blacklisted by the cut-point refinement loop (issue #1353), deleting the
  one proof obligation that distinguishes the two designs: two netlists
  differing **only** on an escaped top-level port were reported
  `"equivalent"` (verified live on Yosys 0.69). Escaped ports are now
  recognised, never blacklisted, and carried into stage 2's counterexample
  dump and the `iverilog`/`vvp` confirmation testbench under their plain,
  unescaped name (`counterexample.diverging_outputs`, and each cycle's
  `inputs`/`gold_outputs`/`gate_outputs` keys). No JSON shape change — an
  affected run's `status` simply stops being wrong.
- **Fixed**: the curated `sky130` DRC deck now also checks each routing
  metal's **holes-area** minimum — `met1.holes_area.1`, `met2.holes_area.1`,
  `met3.holes_area.1`, `met4.holes_area.1`, `met5.holes_area.1` (issue
  #1976), the holes-area companion of the plain `met{1..5}.area.1` rules
  below. Each is scoped to the checked layer's **holes** — the
  interior voids enclosed by a merged metal region, e.g. a slot in a wide
  plate or a fill pattern — rather than the metal polygon itself, via a new
  `DerivedLayer` `"holes"` mode (`klayout.db.Region.holes()`). Thresholds
  are transcribed from the same pinned `volare` sky130A install cited
  below: `m1.7`/`m2.7`/`m5.7` 0.14 um², `m3.7`/`m4.7` 0.2 um² — carried on
  `DrcRule`'s `area_min_dbu2` field as 140 000 / 200 000 dbu². Before this,
  a too-small slot cut into a wide metal plate or MiM-cap plate — a real
  manufacturability defect — came back `clean` rather than reported. A
  plate with no holes at all correctly stays `clean`, not an error (an
  empty `Region.holes()` result is not a violation). Each rule also
  carries a populated `provenance` citing its own upstream rule id. The
  sky130 deck is now 57 rules (was 52), and `provenance.deck.content_hash`
  changes accordingly. See `docs/cli/drc.md`'s "Coverage" section for the
  full per-kind breakdown.
- **Fixed**: `klt lvs` no longer reports a `reference.form:
  "gate-level-verilog"` reference's `assign`-aliased port as a false
  mismatch (issue #2021, the other half of #1994's 77 false `klt lvs`
  errors on the same design). Gate-level Verilog routinely carries a
  port-to-port `assign` alias -- e.g. `assign dbg_uart_byte[i] =
  rx_byte[i];`, two declared port names for one electrical node -- but the
  conversion to SPICE resolves an `assign` alias transparently for every
  *instance* connection, never for a module's own declared port list: the
  aliased port was emitted as its own `.SUBCKT` pin with nothing inside the
  body ever referencing it, reading back as an isolated, disconnected
  reference net even though the layout has exactly one physical net for
  both names. `klt lvs` now joins the alias port's net onto its canonical
  target's net (following a multi-hop `assign` chain to its ultimate
  target, same as the existing instance-connection resolution) before the
  compare runs -- both port names stay individually declared, now on the
  same net, matching a correctly-wired layout's own "one net, two named
  pins" shape. Disclosed via a new, always-`"warning"`
  `mismatches[].category: "topology.reference_port_alias_joined"` entry
  (never flips `status`) naming the joined net and every alias port folded
  into it -- see [`docs/cli/lvs.md`](docs/cli/lvs.md)'s
  "`topology.reference_port_alias_joined`" section. Only a port whose value
  comes purely from an `assign` is ever joined; a genuinely unconnected or
  differently-wired reference port is untouched and still reports as a
  real mismatch.
- **Fixed**: `klt erc`'s per-gate `antenna_verdict` and `klt power`'s overall
  `em_verdict.status` now report a new `"pass_partial"` value (issue #1997)
  instead of silently reading as a plain `"pass"` when their own coverage
  data shows only part of the relevant surface was actually checked.
  `klt erc`: `antenna_verdict` is `"pass_partial"` when at least one graded
  metal level (`levels[1:]`, excluding the gate role, which is always
  `"unchecked"`) is `"unchecked"` and no graded level violates — the
  canonical case is a full sky130 stack through met5, since sky130's own
  antenna-ratio table has no met3/met4/met5 entries at all. `klt power`:
  the overall `em_verdict.status` is `"pass_partial"` when
  `unchecked_edge_count > 0` and `fail_count == 0` — e.g. only one edge in
  the whole design had both a declared current limit and a solved current.
  Real violations/failures are unaffected: `antenna_verdict` still reports
  `"violate"` and `em_verdict.status` still reports `"fail"` regardless of
  coverage. `klt signoff`'s power-evidence check (`em_verdict.status ==
  "pass"`, exact match) already treats `"pass_partial"` at least as
  strictly as `"pass"` — it does not pass.
- **Fixed**: `klt lvs` now populates `provenance.input.content_hash` (issue
  #1969) with the `sha256:`-prefixed hash of the layout side it compared, for
  both the `klayout` and `netgen` engines. It was always `null` before, on the
  deliberate reasoning (issue #335) that `environment.layout_sha256`/
  `reference_sha256` already covered the same files — but `klt signoff
  --manifest`'s staleness gate reads `provenance.input.content_hash`
  *generically*, across every check kind, and cannot see an LVS-only
  `environment.*` field. The consequence was that a T1 item 4 ("LVS clean")
  citation pinning an expected `content_hash` always rendered
  `unmet`/`stale_evidence` — a pinned hash can never match `None` — whether or
  not the layout had actually moved. `environment.layout_sha256` is unchanged
  (still a bare hex digest, no prefix); `klt lvs --check` gains no new
  `checks[]` entry, since a moved layout is already a hash-integrity failure
  under `environment.layout_sha256`. `klt signoff`'s cross-check now includes
  `klt lvs` in the `input.content_hash` comparison group, so an LVS report
  signed off against a *different* layout than its DRC/extract siblings is
  reported as a provenance mismatch rather than passing unnoticed.
- **Fixed**: the `netgen` engine's report text is now stripped of ANSI escape
  sequences before it is folded into any `klt lvs` report field (issue #1969).
  `klt lvs` emits no colour of its own, but netgen's text is foreign input and
  one channel carries it verbatim (`_describe_netgen_property_delta`'s
  documented "pass any other wording through" fallback lands it in a printed
  `mismatches[].description`), so a colourising netgen build would have put
  raw escape bytes on a non-TTY `--format text` stdout *and* into `--format
  json` string fields. Sanitized at the fold-in boundary rather than with an
  `isatty()` gate in the CLI: a JSON field has no terminal to check, and a
  gate would contradict `klt signoff`'s deliberate always-emit convention.
  Unrecognised netgen wording is still passed through, just without the
  escapes.
- **Fixed**: yosys netlists no longer fail `klt place-and-route` two
  deterministic, recurring ways (issue #1973). Both defects were invisible
  until place-and-route and surfaced as a raw engine error pointing at a
  generated file the caller never wrote.
  - A `signed` port/wire qualifier (`output signed [15:0] sample;`) reached
    OpenSTA's Verilog reader, which rejects the keyword outright: `[ERROR
    STA-0171] <netlist> line N, syntax error`, at the **floorplan** stage.
    Yosys's `write_verilog` preserves the qualifier and has no flag to
    suppress it, and no Yosys pass clears a wire's `is_signed` attribute, so
    `klt synthesize` now applies one post-`write_verilog` text rewrite that
    drops `signed` from every `input`/`output`/`inout`/`wire`/`reg`/`logic`
    declaration. Anchored to a preceding declaration keyword, so an
    expression-level `$signed(...)` cast is never touched; the netlist file
    is rewritten only when something actually changed.
  - A bare `x`-valued constant (`assign \data_len$func$…o = 5'hxx;`, most
    often a Verilog `function`'s dangling argument wire) became an empty
    `GROUND`-typed net (`zero_`) that TritonRoute refuses with `DRT-0305` —
    **after** floorplan, placement and CTS had all already succeeded, so it
    cost a full route attempt to discover. Issue #854's existing `hilomap`
    tie-cell pass only ever mapped concrete `1'b0`/`1'b1` literals; it does
    not recognise an `x` bit as a constant at all. `klt synthesize`'s
    generated script now emits `setundef -zero` immediately **before**
    `hilomap`, so every `x` bit is resolved to a concrete `0` and then
    tie-cell-mapped exactly like an ordinary literal. Scoped to the same
    `_TIE_CELLS` condition as `hilomap` — a `cell_library` with no tie-cell
    entry emits neither pass, exactly as before.
  As a safety net for netlists that did **not** come from `klt synthesize`
  (hand-written, third-party, post-edited), `klt place-and-route` now runs a
  comment-aware pre-flight scan of `request.netlist` before invoking OpenROAD
  at all, and rejects either construct with an `error.message` naming the
  construct, its 1-based netlist line number, the offending line, and the
  fix. `DRT-0305`'s existing diagnosis text is corrected accordingly (it
  previously implied `hilomap` alone covered every constant case).
  RTL-side, `docs/guides/digital-review/rtl-style-guide.md` gains a "Flow
  compatibility" section covering the three constructs behind these failures:
  no `signed` ports/wires, no synthesizable Verilog `function`s, and no
  `integer` loop variables (which survive synthesis as wide tie-cell-driven
  wires — wasted area rather than a hard failure).
- **Fixed**: the curated `sky130` DRC deck now checks every routing metal's
  own **minimum area** — `met1.area.1`, `met2.area.1`, `met3.area.1`,
  `met4.area.1`, `met5.area.1` (issue #1955), the deck's first rules of the
  `"area"` check kind (the primitive itself has existed since issue #812, but
  no shipped deck authored a rule against it). Thresholds are transcribed from
  the same pinned `volare` sky130A install every other sky130 rule cites
  (`fossi-foundation/open-pdks` @ `c6d73a3`,
  `sky130/klayout/sky130A_mr.drc`): `m1.6` 0.083 um², `m2.6` 0.0676 um²,
  `m3.6` 0.240 um², `m4.4a` 0.240 um², `m5.4` 4.0 um² — carried on `DrcRule`'s
  `area_min_dbu2` field as 83 000 / 67 600 / 240 000 / 240 000 / 4 000 000
  dbu². Before this, a metal sliver below its official minimum area — a
  routine output of an automated P&R flow — came back `clean` rather than
  reported. Each rule also carries a populated `provenance` citing its own
  upstream rule id. The sky130 deck is now 52 rules (was 47), `coverage.
  rules_skipped` gains the five new ids on a stream with no geometry on the
  corresponding metal, and `provenance.deck.content_hash` changes accordingly.
  Each layer's *holes*-area sibling (`m1.7`-`m5.7`) is transcribed separately
  by issue #1976 (see above), and the `"density"` check kind stays unused
  (this engine's windowed implementation has no floorplan-boundary concept).
  See `docs/cli/drc.md`'s "Coverage" section for the full per-kind breakdown.
- **Changed**: `klt gen-compose`'s closed guard/collector-ring rejection is now
  **plane-aware** instead of identity-only (issue #1960). Previously the check
  fired from block/port identity alone — *this block reports a
  `TAP_*`/`COLL_*` port and no `GAP_*` opening, therefore no route may touch
  its other ports* — so a backbone flying two via levels **above** the ring
  (e.g. `routing.layer_role: "metal3"` over a `bjt_array` collector ring whose
  taps report the diffusion role) was refused with the same message, and the
  same three remedies, as one laid on the ring's own metal. The only way to
  give an enclosed net an externally reachable pad was to *break* the ring
  (`params.ring_gap_side`/`ring_gap_um`), trading away the substrate isolation
  the ring exists to provide, for what is a routing-layer decision.
  `route_two_pin()` now admits such a leg when **every shape it draws** is
  provably clear of the ring's own conductor: the backbone's plane (and the
  `routing.cross_block_layer_role` plane it could be retried onto) differs from
  every ring conductor layer and is separated from each by at least one via in
  the resolved PDK family's own `ExtractionDeck` `metals`/`vias` stack (through
  the same `_resolve_via_drop_layer` the via-drop check already uses), and
  every via-drop landing pad the leg itself draws on a ring conductor layer
  sits clear of the ring's trace. The ring's conductor layers are the union of
  its ports' reported layers **plus** the deck's `metals[0]`, so a collector
  ring whose `COLL_*` taps name only diffusion is still correctly seen as an
  li1 loop too. Every "cannot be shown to clear" answer — a same-plane leg, a
  layer pair the deck cannot resolve, a ring that does not report where its
  sides run or how wide its trace is, a composition with no resolved routing
  layer/extraction deck — keeps the existing rejection **verbatim**, so this
  only ever *adds* admissible routes. Pre-1.0 value-set caveat per
  [`docs/json-contract.md`](docs/json-contract.md): the set of legs that report
  `routed: true` versus the existing closed-ring `reason` string widens; the
  `reason` text itself is unchanged and no `schema_version` bumps. See
  [`docs/cli/gen-compose.md`](docs/cli/gen-compose.md)'s "Routing *over* a
  closed ring on a higher plane (#1960, fixed)".

- **Added**: `klt signoff` now recognises the two **digital RTL-flow
  evidence kinds** — `klt sta` and `klt functional-verification` — and
  grades T1 item 7 ("Post-layout verification") **per block kind** rather
  than globally (issue #1959). Before this, `_classify` recognised only
  analog/full-custom artifacts, so a digital block's own evidence rendered
  `unrecognized_envelope` on item 5, and item 7 was restricted to `pex`
  alone — an artifact an RTL/synthesis block has no way to produce. **No
  digital RTL-flow block could reach `tier: "T1"`.** Both new kinds are
  detected structurally, like every other native kind: `sta` by a top-level
  `geometry_source` string plus `timing_status` or a `corners` list,
  `functional-verification` by a `tests` list plus `test_count`. `klt sta`
  carries no pass/fail `status` of its own (`status` is always `"ok"`), so
  its verdict is derived from the reported timing: **every** corner it
  reported must be `timing_status: "constrained"` (OpenSTA's unconstrained
  sentinel `1e+39` is positive, so a naive `worst_slack_ns >= 0` rule would
  call an untimed design closed) with non-negative setup and hold slack —
  scoped to the corner set the cited run itself declared, which is why `klt
  place-and-route`'s unrestricted-sweep response stays deliberately
  unrecognised. Item 7 now accepts `pex` for an analog partition, and `pex`
  **or** an SDF-annotated `functional-verification` run for a digital one;
  an unannotated (pre-layout, zero-delay) regression cited there renders a
  new, additive `reason` value, **`"not_post_layout"`**, kept distinct from
  `"wrong_kind"` so the report says whether to cite a different artifact or
  re-run this one against the layout. **Analog and full-custom-digital
  grading is unchanged**: an analog block's item 7 still requires `pex` and
  nothing else, a `kind: "digital"` manifest still accepts `pex` there, and
  item 5 stays unrestricted for every block kind. A `generic` citation still
  renders `wrong_kind` on items 5 and 7 — widening `generic` onto them was
  explicitly rejected as weakening the guarantee issue #1152 preserved.
- **Added**: `klt lvs` now reports a **power/ground connectivity verdict**
  alongside the signal-connectivity one for `reference.form:
  "gate-level-verilog"` compares (issue #1952). That form is signal-only by
  construction — a `klt place-and-route` `verilog_path` is written without
  `-include_pwr_gnd`, so the reference has no power connectivity to
  contradict the layout's, and a cell whose `VGND` pin was wired to the power
  rail still reported `status: "match"`. The new, additive top-level
  `power_connectivity` block closes that: it verifies, per abstracted
  standard-cell instance, that every pin the PDK library declares as
  power/ground reaches the net it should — three finding rules
  (`power.inconsistent_pin_net`, `power.unexpected_pin_net`,
  `power.unconnected_pin`), derived from data `klt lvs` already reads (the
  library's own `.subckt` pin orders for *which* pins are power, the layout
  netlist's `klt extract --abstract-cells` pin resolution for *what they
  connect to*) — no new verb, no new input file, no per-PDK power-pin table.
  It runs **before** issue #1622's filler/tap pruning, so a miswired filler
  (issue #1442's defect class) is still caught. The report's own `status`,
  `mismatch_count`, `error_count`, `category_counts` and
  `category_error_counts` are **unchanged** — they stay exactly
  `NetlistComparer.compare()`'s signal-connectivity result, so **a caller
  wanting full LVS on a digital block gates on both `status` and
  `power_connectivity.status`**. The block is emitted for every reference
  form, carrying `status: "unchecked"` plus a `reason` when the check did not
  run, so "was power connectivity verified by this run?" is answerable from
  any report. Default-on, with `options.power_connectivity: false` as the
  opt-out for a genuinely multi-domain design and
  `options.power_connectivity.expected_nets` to upgrade the cross-instance
  consistency check to an absolute one. No `schema_version` bump (purely
  additive, per [`docs/json-contract.md`](docs/json-contract.md)). See
  [`docs/cli/lvs.md`](docs/cli/lvs.md)'s "Power/ground connectivity" and the
  design record in
  [`docs/design/pg-connectivity-check-decision.md`](docs/design/pg-connectivity-check-decision.md).

- **Fixed**: `klt drc --engine klayout` no longer reports a
  partially-executed deck as `"status": "clean"` (issue #1941). A PDK-native
  deck typically calls `report(...)` near the top and appends rules as they
  run, so a deck that aborts part-way through (an unsupported DRC-DSL
  construct, a rule the installed KLayout build is too old for) still leaves
  a **partial** report file behind — and the engine previously treated that
  file's mere presence as proof the run completed, ignoring both `klayout`'s
  non-zero exit status and the `ERROR:` lines it printed. Either signal now
  fails the run (exit 1, carrying klayout's own output). Only a line
  *starting* with `ERROR` counts, so a deck's own output mentioning the word
  (a rule named `ERROR_CHECK.1`, KLayout's indented backtrace lines) is not
  misread as a failure. **Added**: `--allow-deck-errors` (and the matching
  `allow_deck_errors` request field) as the escape hatch for a caller who has
  deliberately scoped around a known-unrunnable rule — it accepts the partial
  report and records what it tolerated in the additive `engine_deck_errors`
  report field (`{"exit_status": ..., "error_lines": [...]}`, omitted
  entirely on every other run), so such a verdict is never *silently* clean.
  It never tolerates a *missing* report file. See
  [`docs/cli/drc.md`](docs/cli/drc.md)'s "Engine" → `"klayout"`.

- **Added**: `klt lvs` request field `options.compare_parameters` (issue
  #1928) — a request-level escape hatch for scoping *which* device-class
  parameters take part in the compare, reaching
  `klayout.db.DeviceClass.enable_parameter` for the first time: `{
  "<device-class>": [<parameter>, ...] }` enables exactly the named
  parameters per class and disables every other declared parameter on both
  sides before the comparer runs. Closes the gap `options.parameter_tolerance`
  could not (it is a *relative* tolerance and can never call a zero-vs-nonzero
  structural difference equal — the exact shape when one side derives a
  parameter the other side's device cards never carry at all). `"engine":
  "klayout"` only; a device class or parameter name that does not resolve
  against either netlist is a clean application error (exit 1), never a
  silent no-op — the same discipline `hints.same_nets` and
  `options.combine_devices`'s array form already apply. Every excluded
  parameter is echoed in the response and disclosed as a
  `severity: "warning"` `device.parameter_excluded` `mismatches[]` entry, so
  a `"match"` reached this way is never silently indistinguishable from a
  full parameter compare. See
  [`docs/cli/lvs.md`](docs/cli/lvs.md)'s `options.compare_parameters` and
  `device.parameter_excluded` sections.

- **Fixed**: `klt extract`'s written SPICE netlist no longer drops a drawn
  resistor's `L`/`W` geometry (issue #1927). KLayout's own default
  `NetlistSpiceWriter` writes a `DeviceClassResistor`/`WithBulk` device's
  plain `R` card with only its resistance value as the positional token
  (`R$1 a b 120000 res_xhigh_po`) — unlike the MOS path, whose plain `M`
  card natively carries `L=`/`W=` — even though the extractor's own JSON
  report already carries the same device's `l_um`/`w_um` alongside `r_ohm`
  in `devices[].params`. Read back through the documented two-step `klt
  extract` -> `klt lvs` flow (`layout.netlist`), that dropped geometry made
  the layout read as `l_um=0`/`w_um=0` — a false `device.property` finding
  against any reference that carries real geometry (e.g. a `form:
  "subckt-call"` reference, whose own conversion already writes `L=`/`W=`),
  even though the extraction itself was correct. `klt extract`'s SPICE
  writer (`pdk_models.py`'s `create_model_binding_delegate`) now appends
  the same `` L=...U W=...U`` suffix the MOS path and
  `netlist_normalize._convert_geometry_card` (the reference-side
  equivalent) already write, for any *named* (deck-declared) resistor
  device — scoped away from `--parasitics`' own anonymous shunt/leg/DC-tie
  resistors, which never carry a real `L`/`W` measurement. The two
  documented `klt lvs` flows (`layout.file` inline extraction and
  pre-extracted `layout.netlist`) now agree on the same layout. No
  `schema_version` bump — the written SPICE gains an additive suffix on an
  existing card; `devices[].params` (the JSON report) is unchanged.

- **Fixed**: `klt lvs` now recognises a round-tripped `X ... PARAMS:` card
  naming a custom (`kdb.GenericDeviceExtractor`-shaped) device class --
  today, sg13g2's `cap_cmomi`/`cap_cmomf` MoM capacitors (issue #1466) -- as
  a real device instead of degrading into a mangled-name abstract circuit
  (issue #1942). Previously, a pre-extracted `layout.netlist` or
  `reference.netlist` that instantiated this device family via
  `X<name> <net> <net> cap_cmomi PARAMS: W=... L=...` (exactly what `klt
  extract -o <netlist>` writes for it, since this class has no native SPICE
  element letter) was read back through `kdb.NetlistSpiceReader`'s own
  default handling as an *abstract circuit* whose parameters are baked into
  its own mangled name (`CAP_CMOMI(L=...,W=...)`) -- invisible in
  `counts.devices`, unreachable by `options.parameter_tolerance`, and any
  real mismatch degraded to a generic, un-named `topology`/`circuit could
  not be matched to a counterpart` finding. `layout.deck`/`reference.deck`
  now key a `kdb.NetlistSpiceReaderDelegate` (`netlist_capacitor_recovery
  .py`'s `make_capacitor_class_recovery_reader`, extended with a
  `custom_device_classes` parameter) that recognises the card and creates a
  device of the identical `DeviceClass` shape the layout-extraction side
  itself registers (`extract.py`'s new `mom_capacitor_device_class`, shared
  by both sides), so the device now reaches the ordinary device-level
  compare (device census, `parameter_tolerance`, named `device.property`/
  `device.unmatched` mismatches) exactly like an `M`/`R`/`C`/`D` card
  already does. Omitting `layout.deck`/`reference.deck` on a netlist that
  round-trips this device family leaves the pre-#1942 fallback unchanged --
  see `docs/cli/lvs.md`'s "Custom device classes round-tripped through an
  `X ... PARAMS:` card" section. No `schema_version` bump -- an existing
  `X` card is now read differently, but no request/response field changed
  shape; every field this fix newly populates (`counts.devices`,
  `mismatches[].device`/`.property`) already existed.

- **Fixed**: `klt drc`/`klt extract` no longer misclassify a bare layout
  filename starting with `{` (e.g. `{weird}.gds`, run from its own directory)
  as inline JSON (issue #1922). `looks_like_request_document()`
  (`_paths.py`) checked `value.lstrip()[:1] == "{"` before checking
  `os.path.isfile(value)`, so an existing layout whose literal name happened
  to start with `{` produced a "request file is not valid JSON" error
  instead of running DRC/extraction on it. The existing-file check now runs
  first and gates content-sniffing, so an existing file is always routed to
  the (already-correct) content-sniffing branch regardless of its name; a
  non-existent path or a genuine inline JSON string starting with `{` is
  unaffected. No `schema_version` bump — CLI argument disambiguation only,
  no payload shape changed.

- **Fixed**: `klt gen mos_array`/`diff_pair`'s `voltage_flavor` param now
  resolves on the `sky130` PDK family (issue #1912). `_PDK_VOLTAGE_FLAVOR_LAYERS`
  (`gen_layer_params.py`) previously had `sky130` as an empty table — this
  deck's own transcription cited no numbered medium/high-voltage transistor
  marker layer, so any `voltage_flavor` request on `sky130` always resolved to
  no marker layer and silently drew the default thin-oxide device, reported
  only via a `drc_hints.notes` entry a caller could easily miss. Issue #1369
  separately added and verified the missing citation (the curated deck's
  `EXTRACTION_DECK.mos_flavours` `MOSFlavour(marker=(75, 20), flavour="hvi",
  ...)` entry, cross-checked against three independent sources in a fetched
  install), so `sky130` now resolves `voltage_flavor="hvi"` to that same
  `hvi.drawing` (75/20) marker — the marker the deck's own `mos_flavours`
  entry keys its `sky130_fd_pr__nfet_g5v0d10v5`/`__pfet_g5v0d10v5`
  device-class split on, so `voltage_flavor="hvi"` now round-trips through
  `klt extract --pdk` to the real thick-oxide model instead of the thin-oxide
  default. No `schema_version` bump — additive per-family coverage of an
  existing field, no shape changed.

- **Fixed**: `klt gen-compose` no longer reports `nets[].legs[].routed: true`
  for a leg whose approach into its own block violates the resolved deck's own
  same-layer minimum-spacing rule (issue #1904). Every own-block check the
  router ran tested for *contact* — a positive-area overlap (#453/#469,
  #1527) or a `bbox_um` crossing measured against `_port_edge_margin_um`, a
  flat per-block margin allowance rather than a rule lookup (#199's check 5) —
  so an approach that threads a gap in the block's own generator-drawn
  geometry narrower than that rule overlapped nothing, stayed inside its
  allowance, and composed `routed: true` with no warning, leaving a real
  `metal1.space.1`/`li1.space.1` violation for a downstream `klt drc` run to
  discover. The reproduction: a `diff_pair` (`splits: 1`) sandwiches
  `Q1_1_G`'s gate landing pad between the two interleaved rows' own S/D metal
  columns, so an external bundle net dropping onto it ran down that channel
  with ~0.01µm of clearance against gf180mcu's own 0.23µm `metal1.space.1`, on
  a block that is DRC-clean standalone. `compose()`'s own leg-conflict check
  now applies the same `"space"`-rule lookup issue #1386 introduced for the
  route-vs-route case to a leg's whole drawn footprint against the blocks its
  own pins sit on: such a leg is reported (`routed: false`, a `legs[].reason`
  naming the block, the violated rule id and the measured clearance, and — when
  `routing.cross_block_layer_role` is configured — retried on that plane
  first) instead of drawn. Shapes the leg's endpoints land on, and shapes its
  footprint literally overlaps, are both exempt: the former is the merge the
  leg exists to make (a self-notch there is already #1520's check), and the
  latter is a *short* question deliberately left unflagged for a
  `generator_report` block's legitimately-drawn-but-unreported geometry
  (`mos_array`'s dummy matching columns, #1527) — merged metal has no gap left
  to violate a space rule, so only a genuine near miss is reported, which is a
  real `klt drc` finding against dummy metal exactly as against any other
  drawn shape. Nets that were correctly `routed: true` are unaffected;
  `klt drc` remains the rule-compliance authority either way.
- **Fixed**: `klt lvs`'s `reference.form: "subckt-call"` conversion no longer
  makes a resistor/capacitor device class impossible to pair (issue #1907).
  The conversion writes a literal `0` into a converted `R`/`C` card's
  positional value slot — documented, accepted behaviour, since `klt lvs` has
  no PDK sheet-resistance/capacitance-per-area table to compute a real value
  from. But `R`/`C` is the *primary*, compared parameter of KLayout's
  `DeviceClassResistor`/`DeviceClassCapacitor`, and `NetlistComparer` uses
  primary-parameter equality to seed device correspondence: a reference class
  whose every instance read `0` against a layout side carrying real,
  geometry-computed values did not report a per-device parameter difference,
  it failed to pair the class *at all* — a wholesale `device.unmatched` /
  `topology` cascade over every instance and every net touching one, even
  with each instance on its own distinct, unambiguous net pair. That made
  `status: "match"` unreachable for any design using a curated resistor or
  capacitor class against a schematic-style reference — precisely the case
  this conversion mode exists to support. `klt lvs` now excludes that one
  placeholder parameter from the compare (on both sides' class, via
  KLayout's own `EqualDeviceParameters.ignore`) before the comparer is
  constructed, so topology pairs the devices exactly as an equivalent
  hand-written `form: "plain-element"` reference carrying real values already
  did. The exclusion is scoped to the provable placeholder — only classes the
  conversion actually emitted, and only when *every* reference-side instance
  reads exactly `0` — so a genuine `0` on a `form: "plain-element"`
  reference, and a reference mixing converted and real-valued cards on one
  class, are both still compared and still reported.
- **Added**: a `device.placeholder_value` `mismatches[]` category (issue
  #1907, `"engine": "klayout"` only) — one `severity: "warning"`,
  `side: "reference"` entry per device class whose converted placeholder
  value was excluded from the compare, naming the excluded parameter, the
  converted family, each side's instance count, and the distinct layout-side
  values that were *not* compared (`details.layout_values`). Additive and
  never `error`, mirroring `device.bulk_reconciled`/
  `device.parameter_tolerated`: it never changes `status`, it only keeps a
  `"match"` reached with that class's value dimension unverified from being
  indistinguishable from one where the two sides' values actually agreed. See
  [`docs/cli/lvs.md`](docs/cli/lvs.md)'s "`device.placeholder_value`" section.
- **Fixed**: `klt extract --abstract-cells` no longer corrupts net names
  elsewhere in the design when the abstracted cell type has a port that
  resolves only through the deck's synthesized substrate global (issue
  #1911) — e.g. an untapped NMOS body with no drawn substrate tie of its
  own. Erasing the black-boxed cell's `nwell` (and `substrate_isolation`)
  geometry previously also perturbed the whole-layout `tap - nwell`
  body-identity classification those layers double as, so a well tie drawn
  *outside* the abstracted cell could silently flip to a substrate tie and,
  via `connect_global`, merge its net with every other substrate-tied net
  in the design — several physically unrelated nets in unrelated cells
  collapsing into one bogus composite name (`net_a|net_b|...`). The
  matched instances' own pre-erasure `nwell`/`substrate_isolation` cover is
  now unioned back into that classification (never into the erased
  conductor region), so `--abstract-cells` reports the same net names for
  every net that does not touch the abstracted cell as a flat extraction
  does. Separately, a global-net-only port that cannot be resolved from
  in-cell labels (or a LEF `PORT`) and is dropped from the black box's pin
  list is no longer silent — a `warnings[]` entry now names the cell type,
  the unresolved port count, and the resolved pin count. See
  [`docs/cli/extract.md`](docs/cli/extract.md)'s "Global-net-only ports"
  and "Abstraction never changes net identity outside the black box"
  sections.
- **Fixed**: `klt gen-compose`'s `connectivity[]` router no longer lets a
  leg's own `_port_edge_margin_um` allowance fund a crossing on the side its
  port does not face (issue #1895). The obstacle-overlap check's per-block
  allowance is a statement about the approach depth on the side a pin
  *faces* — but it was compared against a leg's *total* crossing of that
  pin's own block, with no account of where in the block the crossing sits,
  so a leg that tunnelled in from the opposite side entirely (e.g. reaching
  a north-facing gate pad from the south, across the source strap
  underneath it) was funded by an allowance describing metal on the other
  side of the block. On a tall block that allowance is large enough to pay
  for crossing straight over another device's terminal: two unrelated nets
  could both report `routed: true` with no `warnings[]` entry while `klt
  extract` recovered them as one shorted node — most visibly when
  `routing.cross_block_layer_role` moved an already-routed self-net's bus
  off the primary plane and onto the very pad the offending leg crossed.
  `route_two_pin()` now measures a leg's crossing of the *far* side of each
  of its own pins' blocks separately and holds it to the check's own
  half-width inflation term alone; an ordinary approach along a port's own
  facing direction never enters that region and is unaffected. See
  [`docs/cli/gen-compose.md`](docs/cli/gen-compose.md)'s "Known limitations"
  section.
- **Fixed**: `klt gen-compose` now draws a real licon/mcon contact when a
  `connectivity[]` net is wired to a port reported on a PDK family's
  diffusion role — e.g. `bjt_array`'s `add_collector_ring` collector-tie
  ports (`COLL_N`/`COLL_S`/`COLL_E`/`COLL_W`) — instead of silently drawing
  no via at all (issue #1894). Before this fix, such a leg fell into
  `_resolve_via_drop_layer`'s generic "unrelated role, nothing to do"
  branch (the one an ordinary guard-ring `TAP_*` port, always reported on
  the metals-stack `"metal"` role itself, correctly still uses) and
  reported `routed: true` with a clean `klt drc` regardless — an open
  collector strap `klt extract` alone could catch (recovering the
  collector as its own unstrapped node), and a route to a second,
  unrelated same-block net could land on/through the ring's own metal
  undetected, since the missing via-drop left that leg's landing point
  invisible to the same-block footprint-collision check (a short `klt
  extract` alone could catch). The ladder now drops through the deck's
  `contact` layer to `metals[0]`, then the ordinary metals-stack ladder up
  to `routing.layer_role`, exactly like any other multi-hop via-drop
  (#1567) — and that leg's landing pad is now visible to the same
  collision check as any other. See `docs/cli/gen-compose.md`'s
  "Diffusion-role ports" (under "Via-drop routing").
- **Changed / Added**: `klt synthesize`'s generated top-level `.ys` script is
  now byte-for-byte scan-clean (issue #1870, completing the half #1844 scoped
  out). The resolved liberty — the one remaining absolute path, and on the
  common PDK layouts (`~/.ciel`, `~/.volare`) a home-directory-shaped one, so
  `klt env-provenance scan` on a freshly generated `.ys` still reported
  `home-path` leaks and exit `3` — is now written relative to the resolved
  PDK install root as `$PDK_ROOT/…` (the same spelling `klt sim`'s
  `request.models.lib` already accepts). Yosys does not expand environment
  variables in a script file, so each run additionally writes the rehydrated
  sibling `.klt/synthesize/synth_<top>.run.ys` — identical apart from its
  header block and the substituted absolute liberty — and it is that sibling
  Yosys is invoked on. The response gains an additive `run_script_path`
  (`{path, scope}`, equal to `script_path` when no token was written); no
  `schema_version` bump, per
  [`docs/json-contract.md`](docs/json-contract.md)'s additive-envelope
  policy. **`script_path` is now the artifact to commit, not the artifact to
  run** — a consumer that previously invoked `yosys -s <script_path>` should
  use `run_script_path`, or rehydrate the committed form itself (one
  `$PDK_ROOT` substitution, also exposed as
  `klayout_tools.synthesize.rehydrate_script_text`). The arithmetic-candidate
  trial scripts, the ABC probe, and the baseline re-derivation script are
  unchanged: fully absolute, directly runnable, no sibling. See
  [`docs/cli/synthesize.md`](docs/cli/synthesize.md)'s "Embedded paths"
  section.
- **Fixed**: `klt functional-verification`'s `options.sdf` now rejects (exit
  1, clear message) an SDF `INTERCONNECT` entry naming an escaped identifier
  that contains a literal occurrence of the file's own `DIVIDER` character
  (e.g. `g\[0\]\.sub\/x.a` with `(DIVIDER .)`) instead of reaching
  `$sdf_annotate` and crashing `vvp` outright with a bare `SIGABRT`/core
  dump (`ERROR: NULL handle passed to vpi_scan.`) — issue #1890. This exact
  shape is what synthesis/place-and-route produces whenever RTL uses a
  `generate` block to instantiate an array of sub-modules and the backend
  flattens that hierarchy into one module (Yosys `flatten`; OpenROAD's
  post-route `write_verilog`/`write_sdf`), so it made the documented SDF
  back-annotation flow unusable end to end on any such design. Root-caused
  (live against Icarus 13.0) to Icarus's own `INTERCONNECT` path splitter
  not honoring a backslash-escaped occurrence of the `DIVIDER` character — a
  genuine upstream limitation no generated shim/wrapper can work around, so
  it is now guarded at request-validation time instead. See
  `docs/cli/functional-verification.md`'s "SDF back-annotation" section.
- **Fixed**: `klt lvs`'s pre-extracted `layout.netlist` shape (and
  `reference.netlist`) now recover a capacitor device's real class name
  when re-reading a `klt extract -o`-written bare `C` card (issue #1558's
  fix), instead of falling back to KLayout's generic/anonymous `CAP`
  device class — issue #1876. #1558 correctly stopped writing the
  capacitor's class name as a trailing token on the `C` card (ngspice's
  native `C`-element parser reads that token as an unresolvable `.model`
  reference), but left `NetlistSpiceReader` nothing to recover the class
  from on read-back, breaking name-based device-class correspondence for
  capacitors specifically whenever the compare's other side names the
  class explicitly (a hand-authored/schematic-derived reference, or a
  layout netlist extracted by a pre-#1558 `klt`). The class name is now
  recovered from the writer's own preceding `* device instance … <class>`
  comment — a missing or malformed comment still degrades gracefully to
  the generic class, exactly as before this fix. The bare, value-only `C`
  card format itself is unchanged; only how `klt lvs` re-reads it changed.
  See `docs/cli/lvs.md`'s "Pre-extracted netlist" and `docs/cli/extract.md`'s
  "Nothing is lost" note.
- **Added**: `klt place-and-route`'s post-route multi-corner sweep
  (`response.corners[]`, issue #1092) gains two additive fields per corner —
  `total_negative_setup_slack_ns` / `total_negative_hold_slack_ns` (issue
  #1866) — from that same corner's own already-running OpenSTA session's
  `report_tns_metric -setup`/`-hold` (the matching TNS pair `klt sta`
  already reports as `total_negative_slack_ns`/`total_negative_hold_slack_ns`).
  Previously each `corners[]` entry reported only worst-slack
  (`setup_slack_ns`/`hold_slack_ns`), which says how bad the single worst
  path is but nothing about how much of the design fails at that corner;
  recovering that meant a separate `klt sta` run per corner against the same
  routed `def_path`, re-reading the same ODB/DEF and liberty files the sweep
  had already loaded. No additional OpenROAD invocation and no
  `schema_version` bump, per
  [`docs/json-contract.md`](docs/json-contract.md)'s additive-envelope
  policy — see
  [`docs/cli/place-and-route.md`](docs/cli/place-and-route.md)'s "Per-corner
  total negative slack" section for the full contract.
- **Added**: `klt signoff` (envelope-aggregation mode and `--manifest`/
  `--fleet` tier reports alike) now mechanically consumes any registered
  `critical: true` metric (`docs/design/metric-namespace.md`, issue #247)
  present in a consumed envelope's own `metrics` block — issue #1850. A
  critical metric whose value fails its own declared `higher_is_better`
  polarity (e.g. a nonzero `drc__error__count`, `higher_is_better: false`)
  forces that check's `passed: false`, independent of the envelope's own
  `status`, and names the offending metric(s) in the check's new
  `detail.critical_metric_blockers` field. Read generically from
  `klayout_tools.metrics`'s registry, never hard-coded per-verb, so a
  future verb's newly-declared critical metric is picked up automatically.
  Purely additive: an envelope with no `metrics` block, or none marked
  `critical`, behaves exactly as before. No `schema_version` bump. See
  [`docs/cli/signoff.md`](docs/cli/signoff.md)'s "Critical-metric
  consumption" section.
- **Added**: `klt place-and-route` and `klt sta` gain I/O timing constraints
  and an explicit unconstrained-sentinel signal (issue #1865). Both commands'
  `request.constraints` now accept `input_delay_ns` / `output_delay_ns` —
  independently optional non-negative scalars emitted as `set_input_delay <ns>
  -clock <clock_port>` over every **non-clock** input port and
  `set_output_delay <ns> -clock <clock_port> [all_outputs]`. Before this, the
  entire constraint surface was a single `create_clock`, which is enough only
  for a design whose paths are all register-to-register: a design whose paths
  are *input port → register* / *register → output port* (a pipeline stage, a
  registered interface adapter, a boundary/IO block) had no constrained
  startpoint or endpoint at all, and every slack field degraded to OpenSTA's
  unconstrained sentinel (`1e+39`/`0`). Omitting both fields leaves each
  command's generated Tcl byte-identical to before. Independently, both
  responses gain **`timing_status`** (`"constrained"` | `"unconstrained"` |
  `null`) — at top level, per `stages[]` entry, per `corners[]` entry, and
  inside `spef_sta` — so a caller can tell "genuinely timed, zero negative
  slack" from "never had a constrained path to measure" without special-casing
  `1e+39` by value. `1e+39` is a *positive* number, so a naive `worst_slack_ns
  >= 0` gate previously reported "timing closed with maximum confidence" on a
  design that was never timed. The slack fields themselves are unchanged and
  still report exactly what OpenROAD/OpenSTA reported, sentinel included — no
  `schema_version` bump on either command, per
  [`docs/json-contract.md`](docs/json-contract.md)'s additive-envelope policy
  (retyping `worst_slack_ns` to `number | null` would have been the breaking
  alternative). Per-port delay maps and a full caller-supplied SDC passthrough
  (`read_sdc`, which would also cover false paths / multicycle paths / clock
  uncertainty) remain out of scope. See
  [`docs/cli/place-and-route.md`](docs/cli/place-and-route.md)'s and
  [`docs/cli/sta.md`](docs/cli/sta.md)'s "I/O timing constraints ... and
  `timing_status`" sections.
- **Added**: `klt drc` and `klt extract` accept an optional **request
  document** (issue #1867) — a path to a JSON file, `-` for stdin, or an
  inline JSON object string, in the same positional slot as `<file>`, with
  the new `klt.drc.request/1` and `klt.extract.request/1` schemas covering
  every existing flag one-for-one. These were the last two physical-flow
  stages without one (`klt synthesize`/`klt place-and-route`/`klt sta`/`klt
  lvs` already take theirs as documents), so a flow that wants each stage
  committed as diffable, content-hashable data no longer has to invent a
  repo-local JSON shape plus an argv translator for exactly these two.
  Purely additive: every existing argv invocation is unchanged, and the two
  forms are **mutually exclusive** (a request document plus one of the
  command's own input flags is a clean exit-1 error, never a silent
  override). Relative paths inside the document resolve against the
  document's own directory (file form) or the cwd (stdin/inline forms),
  matching `klt lvs`. The positional slot tells the two apart by value shape
  — `-`, a value starting with `{`, or an existing file whose first
  non-whitespace byte is `{` is a request document; anything else is a
  layout path. See [`docs/cli/drc.md`](docs/cli/drc.md) and
  [`docs/cli/extract.md`](docs/cli/extract.md), "Request document".
- **Added**: `klt functional-verification` gains an opt-in `options.trace`
  field (Epic #1585 Phase 3, issue #1845) — `true` turns on cocotb's own
  `Runner(waves=True)` on both the build and test steps, so the run's
  engine dumps its own native waveform (Icarus: `<hdl_toplevel>.fst`;
  Verilator: `dump.vcd`), and the response's new `trace` field names it:
  `{"path", "format", "size_bytes"}`, the identical shape [`klt wave
  build`](docs/cli/wave.md)'s own `trace` field already uses, so the object
  drops straight into a `klt wave build` request unchanged — no more
  copy/pasting a path by hand to go from a failed cocotb run to a queryable
  waveform store. `null` on a run that did not request a trace; requesting
  one and getting no waveform file is exit 1, never a silent `null`. Not
  engine-restricted (unlike `options.coverage`/`options.sdf`) and not yet
  combinable with `--mutations` (exit 1, undesigned — each isolated
  per-mutant variant builds in its own throwaway directory this path never
  surfaces). No `schema_version` bump, per
  [`docs/json-contract.md`](docs/json-contract.md)'s additive-envelope
  policy. Retires the "Waveform inspection / interactive debug" out-of-scope
  bullet [`docs/cli/functional-verification.md`](docs/cli/functional-verification.md)
  carried since `klt wave` shipped (Epic #1585 Phase 2) — see that page's
  "Request"/"Response"/"Artifacts"/"Out of scope" sections for the full
  contract.
- **Added**: `klt version --format json` gains `klayout_version` (the
  KLayout engine actually resolved) and `klayout_version_expected` (the
  version this build/commit was tested against, recorded from the
  checkout's own `uv.lock` at build time — issue #1490) — and `klt drc`/`klt
  lvs` reports gain `provenance.klayout_version_mismatch: true|false`, with
  a one-line stderr warning when it's `true`. Closes the gap where pinning
  `klayout-tools` to an exact git commit SHA did not pin the KLayout engine
  version that commit resolves (`pyproject.toml`'s `klayout>=0.30` is
  deliberately an unbounded floor), so a "reproduced" report could silently
  use a different engine than the one that produced a committed baseline.
  Reproduce the exact engine with `uv tool install "klayout-tools @
  git+...@<sha>" --with klayout==<klayout_version_expected>` (already
  supported by `uv`, now documented). See
  [`docs/design/klayout-engine-version-pin.md`](docs/design/klayout-engine-version-pin.md)
  for the full decision record and
  [`docs/json-contract.md`](docs/json-contract.md)'s "Pinning the KLayout
  engine version" section.
- **Added**: `klt mom` PEEC inductance/resistance and full-wave S-parameter
  requests accept a conductor built from more than one box, as long as the
  conductor's own boxes share one current-flow axis and axial extent (issue
  #1841). Previously a multi-box conductor was rejected outright by the
  bar-shaped-conductor MVP restriction; now each box is validated
  individually against that restriction, and (for PEEC) gets its own
  filament grid attributed back to the owning conductor for the pairwise
  solve, or (for full-wave) is combined with its sibling boxes into one
  equivalent wire via summed cross-sectional area and an area-weighted
  centroid. A conductor whose own boxes span more than one axis is still
  rejected, unchanged; conductors still need not share an axis or axial
  extent with each other (issue #1842). `inductance_matrix_nh`/
  `resistance_ohm` still report one row/entry per conductor, not per box.
  See `docs/cli/mom.md`'s "Worked example: multi-box conductor" section.
- **Fixed**: `klt synthesize`'s `schema_version` bumps `1` -> `2` (issue
  #1844, mirroring `klt pex`/`klt sim`/`klt size`/`klt extract`'s own issue
  #1261/#1376 bumps): the top-level `netlist_path`/`script_path` fields --
  plus every nested occurrence, `arithmetic.candidates[].measured.
  netlist_path`/`script_path`, and `restructuring.restructured_netlist_path`
  when a resize was applied -- changed from a raw (often absolute) path
  string to the `{path, scope}` shape `env_provenance.repo_relative_path()`
  already defines. `baseline.ref`'s literal-`response_path`/`netlist_path`
  fallback is also normalized (rendered via `render_path_field()` -- still a
  plain string, since `ref` is a caller-facing label, not one of the
  `{path, scope}` object fields). A committed evidence record wraps a `klt
  synthesize --format json` response unmodified
  (`docs/design/sim-evidence-discipline-spike.md`), so the old shape leaked
  the author's home directory / Loom worktree layout into any such record.
  Independently, the generated `.ys` script no longer embeds an absolute
  path for an RTL source or a `.klt/synthesize/` output path (`tee -o`/
  `write_verilog`) that resolves inside the invocation's repo -- only for
  the top-level production script, run with an explicit `cwd=` so the
  embedded relative paths still resolve correctly (`_run_yosys`'s own
  cwd-independence invariant is otherwise unchanged: every other script this
  module writes, and every call with no repo resolved at all, still embeds
  only absolute paths and runs with no `cwd=`). The resolved liberty stays a
  real absolute path in the script (Yosys must actually open it, and a PDK
  install essentially never lives inside the repo) -- its commit-safe
  identity is already the response's `provenance.deck` (name + content
  hash), so on a machine whose PDK sits under `$HOME` the generated `.ys`
  is still flagged by `klt env-provenance scan` on its liberty lines: a
  known remaining gap, tracked as issue #1870.
  `equivalence.artifacts.{script_path, netlist_path}` is `klt
  equiv`'s own response shape, deliberately left un-normalized (a known,
  documented remaining gap -- changing it needs its own `schema_version`
  bump on `klt equiv`). See `docs/cli/synthesize.md`'s field table and the
  "Equivalence gate" section.
- **Added**: a declared metric namespace registry
  (`src/klayout_tools/metrics.py`, issue #247) — data-only mapping from a
  METRICS2.1-style hierarchical metric name (e.g. `design__instance__count`,
  `drc__error__count`) to `{aggregator, higher_is_better, critical}`,
  modeled on LibreLane/OpenROAD's METRICS2.1 convention. Piloted on `klt
  layout-metrics`, which now emits an additive, parallel `metrics` object
  re-keying `layer_count`/`cell_count`/`instance_count`/
  `drc.violation_count` under their declared name — the existing fields are
  unchanged and unrenamed, and no `schema_version` bump was required. See
  [`docs/design/metric-namespace.md`](docs/design/metric-namespace.md) for
  the design decision and [`docs/json-contract.md`](docs/json-contract.md)'s
  "Declared metric namespace" section for the cross-verb convention. Other
  verbs (`klt drc`, `klt extract`, `klt sim`) adopt the registry via their
  own follow-on issues.
- **Added**: `klt drc` adopts the declared metric namespace registry (issue
  #1847, following #247's `klt layout-metrics` pilot) — `run_drc()` now
  emits an additive, parallel `metrics` object (`{"drc__error__count":
  <violation_count>}`), always present, including in the clean/
  zero-violation case. The existing `violation_count`/`rule_counts` fields
  are unchanged and no `schema_version` bump was required. See
  [`docs/cli/drc.md`](docs/cli/drc.md)'s `metrics` field.
- **Added**: `klt sim` adopts the declared metric namespace registry (issue
  #1849, following #1847's `klt drc` adoption) — `run_sim()` now emits an
  additive, parallel `metrics` object under a new `sim__corner__*`
  namespace (`{"sim__corner__count": <corner_count>,
  "sim__corner__passed_count": <passed>, "sim__corner__failed_count":
  <failed>, "sim__corner__errored_count": <errored>}`), always present. The
  existing `corner_count`/`passed`/`failed`/`errored`/`measurements`/
  `corners` fields are unchanged and no `schema_version` bump was required.
  `measurements[].name` stays explicitly out of scope for this registry —
  those names are caller-supplied via the request spec, not `klt`-declared.
  See [`docs/cli/sim.md`](docs/cli/sim.md)'s `metrics` field.
- **Added**: `klt mom` spec field `stackup_from_pdk` (issue #1617) —
  `{"pdk": "<variant>", "layers": ["<conductor name>", ...], "corner":
  "<optional, default 'nom'>"}` resolves the named conductors from an
  installed PDK via `klayout_tools.pdk_stackup.stackup()` (issue #1609) and
  expands them into the same `stackup[]` entry shape a hand-authored spec
  uses, instead of transcribing elevations/conductivities by hand. Thickness
  mode is always the curated (gap-free) one — `tech-lef` is never exposed
  through this path. `background_permittivity` is derived from the resolved
  dielectric slab every named conductor sits inside when the spec omits it;
  conductors spanning slabs with differing (non-null) permittivity raise a
  clear error instead of averaging or picking one. An explicit `stackup[]`
  or `background_permittivity` in the spec always wins over the derived
  values, so every existing spec's behavior is unaffected. The resolved
  request is echoed back under a new, additive `stackup_from_pdk` field in
  the output (present only when it was actually used) for reproducibility —
  no `schema_version` bump, per
  [`docs/json-contract.md`](docs/json-contract.md)'s additive-envelope
  policy. See `docs/cli/mom.md`'s "Deriving a `stackup` from an installed
  PDK" section.
- **Added**: `klt extract --parasitics-top-cell-only` (issue #1704, the
  implementation half of the design spike in #1702/#1707) — splits each
  net's `--parasitics` ground R/C into the portion drawn directly in the top
  cell versus the portion drawn inside an instantiated sub-block, reported
  as additive `resistance_ohm_top_cell`/`capacitance_ff_top_cell` fields
  (`null` unless the flag is given) on every `parasitics.nets[]` entry. Lets
  a caller who has already extracted a sub-block separately subtract its own
  contribution back out of a composed top-level net's R/C. Ground terms only
  (ignores coupling capacitance); built once per curated layer from the
  layout's own instance tree (`Cell.begin_shapes_rec(layer, min_depth=1)`),
  not once per net or per instance, so the cost is independent of instance
  count. Exact in area, not exactly additive in perimeter for a net whose
  conductor genuinely crosses an instance boundary — see
  `docs/cli/extract.md`'s "Top-cell-only hierarchy split" section and
  `docs/design/parasitics-hierarchy-attribution-spike.md`. Off by default —
  byte-identical to today's behavior.
- **Added**: `klt gen-compose` request fields `connectivity[].layer_role` /
  `connectivity[].width_um` and `connectivity[].legs[].layer_role` /
  `.width_um` (issue #1655) — the metal plane (and width) an individual net,
  or an individual named leg of one, routes on, overriding
  `routing.layer_role`/`routing.width_um` for it alone. Until now
  `routing.layer_role` resolved exactly one plane for the whole composition,
  so a composition whose net-connectivity graph is **non-planar** (a
  K3,3 subdivision) could not be drawn in a single call at all: some pair of
  nets must cross, and two crossing nets on one layer are a short the
  route-vs-route check correctly rejects. Roles resolve through the same
  per-PDK-family table `routing.layer_role` uses; a width is inherited when
  omitted and always floored against the plane the entry actually draws on
  (an inherited width that does not clear a stricter plane's floor is an
  error naming that entry's own `width_um`, never a silent widening of every
  other net's routing — the `routing.cross_block_width_um` rule, #1620,
  applied per net). An explicit plane — `layer_role`, at either tier — wins
  over the automatic `routing.cross_block_layer_role` fallback, which still
  applies unchanged to every net (and every leg) that names no plane of its
  own: a *layer-pinned leg* is neither retried onto the cross-block plane nor
  picked back up by the automatic spanning-tree search if it is rejected on
  the plane it named, so it is reported `routed: false` rather than silently
  redrawn on the net's own plane (the net may still reach those two pins
  indirectly, through other pins). A `width_um` with no `layer_role` takes
  control of no plane and keeps every fallback, at both tiers. Legs of one net
  that span two planes stitch at their shared pin through the existing #454
  via-drop. All
  four fields are optional and additive — a request that omits them composes
  byte-for-byte identically — so there is no `schema_version` bump. See
  `docs/cli/gen-compose.md`'s "Per-net routing planes" section and the
  decision record `docs/design/gen-compose-per-net-layer.md`.
- **Added**: `klt gen mos_array` gains `interior_channel_um` (default `0.0`,
  preserving byte-for-byte existing geometry; issue #1531, Phase 1) — extra
  gap in µm reserved between adjacent rows/columns, on top of the fixed
  inter-device spacing, so an interior unit device's pin has a metal-free
  path out to the array's edge. A positive value widens both `row_pitch` and
  `col_pitch` by exactly that amount; `0.0` adds nothing to either pitch, so
  every request that predates this param keeps identical `x0_um`/`y0_um` per
  cell — the same regression-safety convention `add_guard_ring` and
  `gate_pad_clearance_um` already established. Reserving the channel in the
  generator (rather than teaching `gen-compose` to peek inside an otherwise-
  opaque block) keeps the generator responsible for its own routability the
  same way it already is for its own DRC cleanliness. `res_array`/`diff_pair`
  are expected to grow the same parameter in a later phase, not this one. See
  `docs/cli/gen.md`'s `mos_array` param table.
- **Added**: `klt gen` response field `navigable_regions` (issue #1531,
  Phase 1) — a list of metal-free `{"x0_um", "y0_um", "x1_um", "y1_um"}`
  rectangles inside the emitted cell's own footprint that a router could pass
  through, reported as a sibling of `ports`. Currently populated only by
  `mos_array`'s `interior_channel_um` above (one band per adjacent row pair
  when `rows > 1`, one per adjacent column pair when `cols > 1`); every other
  generator — and `mos_array` itself at the default `interior_channel_um:
  0.0` — reports an empty list. Additive field on `klt gen` only, no
  `schema_version` bump, per [`docs/json-contract.md`](docs/json-contract.md)
  (same precedent as `dbu_um`, issue #1496). `klt gen-compose`'s own
  consumption of this field (subtracting a block's declared
  `navigable_regions` from its obstacle bbox) is issue #1835, below.
- **Added**: `klt gen-compose` subtracts a block's own declared
  `navigable_regions` (issue #1531) from that block's obstacle bbox before
  its obstacle-overlap check runs (issue #1835) — a `waypoints_um` backbone
  that stays inside a block's declared channel (e.g. a `mos_array` placed
  with `interior_channel_um > 0`) is no longer rejected as crossing its own
  block. `navigable_regions` is parsed on `blocks[].generator_report` and
  `blocks[].cell` alongside `ports[]`/`bbox_um`, and run through the same
  `orientation` transform (#1166) every other per-block geometry already
  goes through. Absent or empty `navigable_regions` — every block predating
  this field, and every `mos_array` call with `interior_channel_um: 0.0`
  (the default) — leaves the obstacle-overlap check byte-for-byte
  unchanged. See `docs/cli/gen-compose.md`'s "A block's own declared
  interior routing channel is no longer treated as its own obstacle"
  section.
- **Added**: `klt wave build` / `klt wave query` (Epic #1585 Phase 2c, issue
  #1601) — index a VCD/FST functional-verification waveform trace into a
  compact FST store, then answer `value`/`find`/`count`/`sample`/`stuck`/
  `diff`/`wave` query operations against it, closing the gap `klt
  functional-verification`'s own "Out of scope" section named ("Waveform
  inspection / interactive debug... no waveform artifact is contracted").
  Thin Python wrapper (`klayout_tools/wave.py`) around the standalone
  `klt-wave` binary (`native/wave/`, issues #1599/#1600); `klt-wave-native`
  is not a `pyproject.toml` dependency at all (build it directly with
  `cargo build --release` inside `native/wave/`) — a checkout without it
  gets a clear, actionable error, never an install-time break. See
  [`docs/cli/wave.md`](docs/cli/wave.md) and
  [`docs/design/waveform-query-contract-spike.md`](docs/design/waveform-query-contract-spike.md)
  for the full contract.
- **Added**: `klt gen-compose` — a channel track retry for inter-block
  route-vs-route collisions (issue #1467): a leg whose fixed-shape backbone
  contends with an already-routed net sharing the same single-sided channel
  (same row, ports facing the same direction) is now retried on successive
  y-offset tracks before being reported unroutable, instead of being
  rejected outright on the first collision. Resolved legs report which
  track they landed on in the new `nets[].legs[].channel_track` field (`0`
  for the untracked default). Scoped to nested span pairs — see
  `docs/cli/gen-compose.md`'s "Channel track assignment (#1467)" section for
  the mechanism, the declaration-order requirement, and the capability
  ceiling for genuinely crossing (non-nested) span pairs, which this retry
  cannot resolve. Channel-track exhaustion with a configured
  `routing.cross_block_layer_role` falls back to that cross-layer retry
  (issue #1680) instead of failing outright.
- **Added**: `klt place-and-route` gains three additive, optional
  `request.constraints` design-rule fields (issue #1709) —
  `max_transition_ns` → `set_max_transition`, `max_capacitance_pf` →
  `set_max_capacitance`, and `max_fanout` → `set_max_fanout`, each on
  `[current_design]`, each validated as a positive number when given, each
  independently optional (never gated on `clock_port`/`clock_period_ns`),
  and each emitted right after the clock constraint and before the
  `repair_design`/`repair_timing` pass the generated flow already runs. This
  aims that already-present optimiser at a caller-given target instead of
  only whatever limit the single resolved liberty deck happens to declare —
  the case for a design whose implementation-corner limit is looser than a
  downstream signoff deck's, and the only available case at all for a
  standard-cell library that declares no fanout limit of its own. Omitting
  all three reproduces the prior generated Tcl byte-for-byte.
- **Added**: `klt place-and-route` response fields
  `max_transition_violation_count` / `max_capacitance_violation_count`
  (issue #1709) — the **design-rule-check verdict** at the same
  `pdk.sweep_corners` decks the post-route sweep already re-times at,
  reported at top level and per `corners[]` entry alongside
  `worst_setup_slack_ns`/`worst_hold_slack_ns`, so a caller sees a
  max-transition / max-capacitance violation in the report this run already
  writes rather than in a separate downstream tool three steps later.
  Sourced from `report_check_types -max_slew -violators` /
  `report_check_types -max_capacitance -violators` run inside the sweep's own
  existing OpenROAD invocation (no additional process launch, so the measured
  sweep cost is unchanged), counted by the same marker-delimited
  `"(VIOLATED)"` scrape `setup_violation_count`/`hold_violation_count`
  already use. `0` on a clean run, `null` before the `"route"` stage and when
  `sweep_corners` sweeps zero corners. Reported whether or not the new
  constraint fields above were given. There is deliberately **no**
  `max_fanout_violation_count`: `sta::max_fanout_violation_count` SIGSEGVs
  inside `sta::CheckFanouts::check` on a library that declares no fanout
  limit (reproduced at every corner on `26Q3-1510-g6cb3f2b704`, issue #1709)
  — precisely the library class `constraints.max_fanout` exists to serve. No
  `schema_version` bump (additive on both request and response). See
  [`docs/cli/place-and-route.md`](docs/cli/place-and-route.md).

### Merged since 0.5.0, by commit type

Every commit on `main` in `v0.5.0..v0.6.0` (270 commits), grouped by its
conventional-commit prefix; the trailing `(#N)` is the squash-merged PR.
The narrative entries above cite the originating issue where one exists.
Repeated no-PR sync commits (`chore: resync installed Loom surfaces`,
`bd sync`) are aggregated into single lines.

#### Added

- feat(deck): add `klt deck rules` to expose a deck's rule values (#2308) (#2315)
- feat(sim): honor a host-level KLT_SIM_MAX_WORKERS cap for local-parallel (#2311)
- feat(benchmarks): wire real per-round token usage into the round-mode ledger (#2294) (#2309)
- feat(benchmarks): cache the deterministic reference provider across --rounds (#2305)
- feat(signoff): carry a mixed-signal manifest's declared partition boundary (#2303)
- feat(functional-verification): declared-input evidence manifest v1 (partial-closure pilot) (#2097) (#2301)
- feat(place-and-route): row-rail fallback warning + PDN/stackup docs guidance (#1985) (#2300)
- feat(mom): PEEC increment (i) — multi-box conductors sharing one axis (#1869)
- feat(test): cross-validate klt mom's capacitance matrix against FastCap 2.0 (#2015) (#2064)
- feat(benchmarks): --rounds refinement mode with a per-cell ledger.jsonl (#2253) (#2296)
- feat(design-agent): per-task mutations.json schema + validate mutation-gate enforcement (#2297)
- feat(equiv): stage-scoped --resume + --check/--rerun + remote evidence-runs guide (#2280) (#2289)
- feat(evidence): float-provenance rules + pinned-artifact escape hatch for committed generators (#2279) (#2287)
- feat(extract): add --subcircuit to slice one sub-cell into a standalone .SUBCKT deck (#2283)
- feat(power): carve declared device bodies out of the conductor roles (#2271)
- feat(erc): let a native-substrate block declare its tie via asserted well_boxes (#2273)
- feat(erc): distinguish a tool-limitation tie disclosure from an unexpressible tap (#2264)
- feat(signoff): add --check so a committed report verifies across provisioning routes (#2258)
- feat(erc): declare a tap by assertion or disclose it as unexpressible (#2240)
- feat(signoff): gate --format text colour on isatty, $NO_COLOR, and --no-color (#2235)
- feat(verify): --check/--rerun for synthesize/place-and-route + envelope lint (#2233)
- feat(equiv): optional Verilator fast-path replay backend (iverilog stays canonical) (#2232)
- feat(erc): auto-apply a curated deck's own device-marker layers via --deck (#2217)
- feat(gen-compose): flag legs whose landing pin never touches real block metal (#2221)
- feat(signoff): expose a grading-ruleset identity distinct from git_commit/git_tag (#2222)
- feat(signoff): report how many T1 items this build grades beside the doc's count (#2215)
- feat(erc): locate each island of a multi-island erc.unconnected_net (#2207)
- feat(signoff): verify a citation's input against the artifact, not just the envelope (#2212)
- feat(signoff): report version-skewed envelopes distinguishably (#2198) (#2208)
- feat(erc): subtract declared device bodies from connectivity (#2205)
- feat(signoff): pin the tier report's governing checklist by content hash (#2191)
- feat(erc): add erc_status, a connectivity rollup beside the antenna status (#2188)
- feat(place-and-route): ship platform-default PDN presets for request.power (#2174)
- feat(design-agent): zero-TC composite reference divider for the device oscillator (#2155)
- feat(coverage): define the common partial-success rollup and signoff rule (#2151)
- feat(signoff): kind-restrict T1 items 5, 6 and 8 (#2044) (#2129)
- feat(provenance): discriminate input.content_hash by artifact role (#2058)
- feat(place-and-route): measure and warn about placed power delivery (#2122)
- feat(pdk): enforce explicit capability decisions against live owners (#2137)
- feat(signoff): add T1 item 11, power delivery (structural) (#2057)
- feat(signoff): validate ingested envelopes against a typed shape per kind (#2054)
- feat(gen): add shared provenance block to klt gen/gen-compose reports (#2066)
- feat(sim): add a `batch` backend that submits to 2am's EDA batch fleet (#2080) (#2087)
- feat(coverage): declare the shared nothing_checked convention; klt signoff refuses vacuous evidence (#2067)
- feat(gen-compose): record source_path and a geometry digest per input block (#2069)
- feat(ci): add a cyclomatic-complexity ratchet (C901), not a ceiling (#2041)
- feat(erc): pin the spec file's contents in klt erc's provenance block (#2049)
- feat(lvs,pex,signoff): make device-body tie/bias a gradeable field (#1983) (#2047)
- feat(lvs): disclose applied hints.equivalent_pins groupings in the envelope (#2031)
- feat(signoff): report the cited DRC envelope's coverage block (#2002) (#2017)
- feat(signoff): grade digital RTL-flow blocks (sta/functional-verification, per-kind item 7) (#1967)
- feat(erc): add status and provenance to klt erc's envelope (#1984)
- feat(lvs): add per-instance power/ground connectivity check to gate-level LVS (#1964)
- feat(lvs): add options.compare_parameters to scope device-class parameter compares (#1928) (#1943)
- feat(scripts): add checksum-pinned fetch script for ihp-sg13cmos5l (#1940)
- feat(sta): add pdk.corners for N-corner characterization in one request (#1919)
- feat(drc,extract): add optional request-document input form (issue #1867) (#1918)
- feat(place-and-route,sta): add input/output delay constraints and timing_status (#1915)
- feat(signoff): consume declared critical metrics from the metric-namespace registry (#1910)
- feat(place-and-route): add per-corner TNS to the post-route corner sweep (#1905)
- feat(extract): adopt the declared metric namespace registry (extract__* metrics) (#1899)
- feat(functional-verification): options.trace contracts the waveform artifact (#1862)
- feat(sim): adopt declared metric namespace registry (sim__corner__* metrics) (#1897)
- feat(gen-compose): connectivity[].layer_role routes non-planar net graphs in one call (#1858)
- feat: detect and warn on KLayout engine version drift (issue #1490) (#1872)
- feat(drc): adopt declared metric namespace registry (drc__error__count) (#1877)
- feat(spec-review): promote verdict to EE key of two-key ratification (#1893)
- feat(mom): PEEC increment (ii) -- arbitrary orientation/offset filament pairs (#1879)
- feat(place-and-route): add max_transition_ns/max_capacitance_pf/max_fanout constraints (#1860)
- feat(gen-compose): channel track assignment for inter-block route-vs-route contention (#1467) (#1840)
- feat(extract): implement --parasitics-top-cell-only R/C attribution (#1704) (#1856)
- feat(sim): two-pass fail-fast probe for unmeetable timeout budgets (#1694) (#1859)
- feat(gen-compose): subtract a block's navigable_regions from its obstacle bbox (#1835) (#1855)
- feat(mom): resolve klt mom stackup from an installed PDK (#1617) (#1852)
- feat(metrics): add declared metric namespace registry, pilot on layout-metrics (#1851)
- feat(wave): wire klt wave build/query into the CLI, schemas, docs (#1833)
- feat(gen): mos_array interior_channel_um reserves an interior routing channel (#1531) (#1836)

#### Fixed

- fix(lvs): recover the device.property finding on the inline-extraction path (#2317) (#2319)
- fix(mom): Gauss quadrature over near-field source panels — closes the ~3% centroid-kernel gap (#2061) (#2322)
- fix(gen): draw bjt_array's collector ring on the tap role so extract sees the tie (#2320)
- fix(fastcap): build the capacitance oracle on macOS without manual aids (#2306) (#2314)
- fix(functional-verification): drop zero-delay assign-alias port INTERCONNECTs (#2304)
- fix(native): set debug = 1 on every release profile so macOS can dlopen the output (#2261) (#2290)
- fix(power): attach a via to the polygon it lands on, not the nearest node on the net (#2269)
- fix(build): stop counting untracked build-root files as dirty in git_identity() (#2257)
- fix(lvs): prune power-only masters from the reference side too (#2244) (#2251)
- fix(examples): clear host paths from committed envelopes and gate them in CI (#2241)
- fix(loom): teach sweep-lease-fence.sh check its own sweep id (--sweep-id) (#2239)
- fix(erc): report device-body carve-out area intersected with its role (#2236)
- fix(signoff): rank an ungradeable-by-build item first in the fleet blocker (#2220)
- fix(signoff): resolve klt yield samples path relative to the report (#2211)
- fix(erc): report a degenerate tie as skipped, not a clean missing-tie pass (#2209)
- fix(loom): never write loom:issue over a live loom:operator-only escalation (#2206)
- fix(power): match power_nets by carried label, decompose non-rect rails (#2190)
- fix(signoff): report per-item build-grading coverage and name the build (#2201)
- fix(signoff): distinguish unverifiable provenance from genuine staleness (#2195)
- fix(place-and-route): reject PDN straps that cannot fit the floorplan core (#2189)
- fix(signoff): the fleet roll-up's blocking_item skips structurally-ungradeable items (#2187)
- fix(erc): scope ties[] well conduction to its taps, isolate tie graph (#2186)
- fix(extract): stop double-escaping a merged-label net's synthesized leg/hub name (#2185)
- fix(sim): populate provenance.input with role: netlist (#2168)
- fix(signoff): reject malformed critical metrics (#2094) (#2112)
- fix(signoff): let a critical-metric blocker outrank a partial status token (#2167)
- fix(drc): apply the common coverage rollup to curated results (#2166)
- fix(pex): apply the common coverage rollup (#2165)
- fix(examples): regenerate the signoff worked example and gate it in CI (#2163)
- fix(sim): apply the common coverage rollup to skipped corners and limits (#2162)
- fix(erc): apply the common coverage rollup rule to antenna status (#2109) (#2161)
- fix(power): derive EM status from the common coverage rollup (#2116) (#2160)
- fix(provenance): record the running tool build identity (#2102)
- fix(openroad): retain per-invocation logs across P&R and STA failures (#2127)
- fix(functional-verification): reject unexecuted or inconsistent evidence (#2107)
- fix(extract): rename dot-joined net names so ngspice can address the node (#2153)
- fix(lvs): require cross-master corroboration before calling a pin a supply (#2105)
- fix(signoff): require literal true for SDF annotation credit (#2135)
- fix(extract): preserve abstracted well continuity (#2082) (#2121)
- fix(extract): stop --abstract-cells merging a macro's own declared pins (#2147)
- fix(loom): verify bare file and line citations (#2128)
- fix(place-and-route): floor routed metal against each layer's min-area rule (#2144)
- fix(coverage): refuse zero-check results across audited producers (#2134)
- fix(functional-verification): require readable SDF transcripts (#2138)
- fix(lvs): flag a supply net matched to an unrelated signal net (#2140)
- fix(synthesize): isolate artifacts and input attribution per invocation (#2119)
- fix: isolate routing passes and publish distinct final DRC counts (#2098)
- fix(extract): curate sg13cmos5l nominal metal parasitics (#2126)
- fix(gen-compose): floor via landing pads by enclosure rules (#2103)
- fix(lvs): report supply names spanning disconnected layout nets (#2120)
- fix(extract): resolve a magic tech file for the ext2spice coupling oracle test (#2059)
- fix(synthesize): enable gf180 7t constraints and constant mapping (#2099)
- fix(lvs): make body_verification's PMOS arm per-device, not deck-structural (#2048) (#2068)
- fix(ci): warn not error on queue-only wall-clock breaches (#2056) (#2063)
- fix(decks): register sg13cmos5l in the parasitics registry (#2012)
- fix(gen-compose): floor via-drop landing pads against each layer's own min-area rule (#2075)
- fix: re-snapshot complexity-baseline.json to unblock the C901 ratchet (#2077)
- fix(lvs): join assign-aliased reference ports onto their canonical net (#2038)
- fix(gen): emit the JSON error envelope for self-detected usage errors (#2029) (#2050)
- fix(erc,power): report pass_partial when coverage is incomplete (#1997) (#2022)
- fix(loom): give every worktree its own uv-synced editable install (#2042)
- fix(decks): transcribe sky130's met1-met5 holes-area rules (m1.7-m5.7) (#2023)
- fix(equiv): keep escaped top-level ports out of cut-point blacklisting (#2019)
- fix(extract): stop abstract-pin candidate discovery at a second declared pin (#2024)
- fix(examples): make both worked-example regenerators reproducible (#2018)
- fix(signoff): restrict T1 items 3 and 4 to drc and lvs citations (#2013)
- fix(extract): warn when a declared pin name matches 2+ disconnected nets (#2010)
- fix(lvs): act on power_connectivity first-tester feedback (#1978) (#2009)
- fix(synth,pnr): strip signed declarations, resolve x constants, pre-flight netlists (#2006)
- fix(lvs): populate provenance.input.content_hash, strip ANSI from netgen text (#2005)
- fix(erc): compute gate area from poly ∩ diff via optional active_layer (#2001)
- fix(decks): transcribe sky130's met1-met5 minimum-area rules (#1955) (#1989)
- fix(gen-compose): make the closed-ring rejection plane-aware (#1970)
- fix(signoff): hard-fail klt signoff's lvs check on a power_connectivity mismatch (#1974)
- fix(drc): fail --engine klayout runs on a partially-executed deck (#1951)
- fix(hermit): recognize annotated self-assignment as instance state (#1949)
- fix(extract): carry resistor L/W geometry onto the written R card (#1947)
- fix(gen-compose): accurate ring-gap clearance messages; fund ring-gap reach in own-block routing allowance (#1945)
- fix(lvs): recognise round-tripped custom device classes as devices, not abstract circuits (#1942) (#1944)
- fix(gen-compose): hold a leg's own-block approach to the deck's spacing rule (#1904) (#1936)
- fix(drc,extract): stop bare filename starting with '{' misclassifying as inline JSON (#1939)
- fix(gen-compose): compare a via-drop ladder's intermediate landing pads too (#1937)
- fix(gen): resolve sky130 hvi marker for voltage_flavor (issue #1912) (#1935)
- fix(extract): stop --abstract-cells from corrupting unrelated net names (#1911) (#1934)
- fix(lvs): let a subckt-call reference's placeholder-0 value pair on topology (#1933)
- fix(drc,lvs): populate provenance.pdk when --pdk/reference.pdk resolves (#1901) (#1932)
- fix(gen-compose): reject a leg that reaches a pin from the side it doesn't face (#1931)
- fix(gen-compose): draw a real contact for diffusion-role via-drops (COLL_*) (#1930)
- fix(functional-verification): guard against Icarus's escaped-divider INTERCONNECT crash (issue #1890) (#1926)
- fix(synthesize): write commit-safe $PDK_ROOT-relative liberty, rehydrate a runnable sibling (#1920)
- fix(lvs): recover capacitor device-class name across bare-C-card round trip (#1921)
- fix(place-and-route): surface container-mount hints on openroad file-read failures (#1914)
- fix: restore SIGPIPE race fix and flake-detection test in verify-proposal-refs.sh (#1898) (#1903)
- fix(loom): verify-proposal-refs.sh cache never persists, flaky false MISSING FILE reports (#1881)
- fix(synthesize): report artifact paths as {path, scope}, drop absolute paths from .ys (#1873)
- fix(functional-verification): defer bit-selected top-level-port INTERCONNECT entries to a later $sdf_annotate call (#1857)
- fix(wave): harden gen_fixtures against the fst-writer size-tie defect, draft upstream report (#1606) (#1853)

#### Changed

- refactor(native): extract shared NLDM bilinear-interpolation crate (#2272) (#2282)
- perf(erc): add --findings-only to skip the per-gate antenna accumulation (#2228)
- refactor(size): extract shared method/env tail into _print_method_and_env (#2154)
- refactor(extract): split the netlist-report subsystem into extract_report.py (#2071)
- refactor(pdk): one authoritative variant->family classifier (#2026) (#2053)
- refactor(power): use ir_solver.worst_deviation for island worst droop (#1950)
- refactor(pdk): split standard-cell/liberty subsystem into pdk_cells.py (#1923)
- refactor(functional-verification): split SDF timing-annotation subsystem into functional_verification_sdf.py (#1916)
- refactor(gen): split PDK layer-parameter lookup into gen_layer_params.py (#1891)
- refactor(extract): split RC-parasitics subsystem into extract_parasitics.py (#1882)
- refactor(pdk): dedupe _resolve_liberty into shared resolve_liberty_for_cell_library (#1861)

#### Docs

- docs: tagged remote-compute guide + gitignore per-repo .env (#2277) (#2292)
- docs: codify byte-exact-vs-tolerance convention for numeric fixtures (#2293)
- docs(signoff): document the supply_spec_disclosed_tool_limitation reason (#2268)
- docs(gen): state that generator geometry is not stable across releases (#2256)
- docs(erc): document diffusion/well continuity false-positive risk (#2200)
- docs: make the klt exit-code contract explicitly additive, gate on status (#2184)
- docs: what makes a subagent run slow, and the dispatch skill for it (#2030)
- docs(drc): correct stale coverage_unknown docs for the klayout engine (#2157)
- docs(signoff): document coverage_unknown/malformed_coverage reason values (#2149)
- docs(lvs): document net_correspondence reference/layout as nullable (#2143)
- docs(json): define warnings for skipped requested analyses (#2125)
- docs: document envelope-vs-plain-string split for artifact path fields (#2081)
- doc: add module-ownership map for multi-file klt verbs (#2043)
- docs(tiers): state the power_connectivity condition item 4 is graded on (#2003)
- docs(drc): record the decision to leave metal density uncovered (#1975) (#1992)
- docs(cocotb): require ReadOnly() before sampling a DUT output (#1990)
- docs: add the digital path's staged pipeline contract (D1-D11) (#1963)
- docs(signoff): add a runnable block-manifest worked example (#1961)
- docs(cli): add yield-campaign reference page, link release-lag policy (#1958)
- docs: bring README file trees and PyPI status back in line with the tree
- docs(lvs): clarify counts.{nets,pins}.matched is hierarchy-wide, not top-circuit-scoped (#1887) (#1925)
- docs(lvs): sharpen combine_devices_per_circuit no-op caveat for klt extract composed netlists (#1924)
- docs(synthesize): fix netlist_path.path resolution base in place-and-route bullet (#1896)
- docs(functional-verification): attribute the all-tests-fail SDF shape to timing, not annotation (#1888)
- docs(wave): add waveform-first debugging guide for agents (#1864)
- docs(design): record path-1 decision for klt mom non-bar conductor support (#1843)

#### Chores

- test(lvs): cross-validate the LVS verdict against a magic-extracted netlist (#2318)
- build(deps): bump the github-actions group across 1 directory with 3 updates (#2164)
- build(deps): bump the npm-minor-patch group across 1 directory with 9 updates (#2274)
- test(mom): grep-based no-silent-skip gate + seeded-defect control for PyPEEC (#2310)
- test(lvs): power-grid regression suite + real conb_1 gate-model corpus control (#1986) (#2302)
- test(mom): cross-validate the PEEC spiral against PyPEEC in CI (closes out #1842 AC3) (#2106)
- ci(evidence): flake triage forensics + declared platform-variable regions for golden artifacts (#2275) (#2298)
- ci: dedicated test-numerical job + numpy cross-check suite for dependency-gated numerical surfaces (#2276) (#2291)
- test(gen-compose): guard COLL_* + channel-track-leg net merge (#2008) (#2060)
- ci: byte-compare golden artifacts regenerated under two hash seeds and paths (#2231)
- test: add offset/mismatch start-up sweeps for device-oscillator (#2213)
- test: isolate build identity git probes from OpenROAD stubs (#2159)
- test(corpus): add a gcd P&R fixture with a real power grid (#2114)
- test: isolate optional ngspice and fleet PDK requirements (#2101)
- ci: add a wall-clock budget check to ci.yml (#1993)
- test(oracle): cross-validate klt drc and klt extract against magic (#2014) (#2046)
- test(signoff): bind an LVS check to the DRC'd layout, correct stale prose (#2040)
- ci: move native-techmap off the contended self-hosted pool (#1981)
- test(gen-compose): lock in clean bundle-net routing through a nested composition (#1917) (#1938)
- test(loom): enlarge Fixture 5b's fixture repo to actually trigger the SIGPIPE race it guards (#1906)
- ci: gate native legs on an extension freshness fingerprint (#1889) (#1900)
- chore(examples): regenerate critical-net-mom-fidelity reports from current klt extract (#2270)
- chore(decks): regenerate deck history table for v0.5.0
- chore: resync installed Loom surfaces (35 commits, no PR)
- bd sync: 2026-09-21 17:49:35 (no PR)

## 0.5.0 (2026-09-15)

235 commits on `main` since v0.4.0, cut under the 25-commit backstop in
[`RELEASING.md`](RELEASING.md)'s "Release cadence" (operator ruling 2026-09-15,
issue #1563). Minor bump: the range carries no conventional-commit breaking
marker (`!:` / `BREAKING CHANGE`) and no `schema_version` value under `src/`
changed; compatibility notes, where they matter, are in the entries below.

- **Added**: `klt place-and-route` response field `nominal_hold_slack_ns`
  (issue #1826) — the single, nominal-corner hold WNS
  (`report_worst_slack_metric -hold`), populated from the `"place"` stage
  onward, mirroring the existing setup-side `worst_slack_ns`. Distinct from
  the route-stage-only, corner-swept `worst_hold_slack_ns` aggregate (issue
  #949) — this field exists so a caller can get a hold slack *number*
  before a full route, not just `hold_violation_count`'s pass/fail count.
- **Added**: `klt place-and-route` response field `unrouted_def_path`
  (issue #1826) — a pre-route DEF, populated when `target_stage` itself is
  `"place"` or `"cts"`, reusing (and, for `"cts"`, newly adding) the
  `write_def` call each of those stages' own Tcl generator already runs.
  `klt sta`'s new `def`/`geometry_source: "placement_estimate"` request
  fields (below) accept this path directly, closing the gap where an SDC-
  driven setup/hold slack characterization required a full route.
- **Added**: `klt sta` request field `geometry_source` (`"routed"`, the
  default, or `"placement_estimate"`; issue #1826), echoed back verbatim as
  the response's own `geometry_source` field. Declares whether `def` is a
  fully-routed signoff geometry or a pre-route (`klt place-and-route`
  `"place"`/`"cts"`-stage) estimate — a caller-supplied label, since a bare
  DEF file carries no stage-provenance metadata of its own. See
  `docs/cli/sta.md`'s "Pre-route DEFs" section.
- **Added**: `klt arith-gen` — generate a parallel-prefix adder as Verilog
  RTL from one of five named architectures (`ripple`, `brent-kung`,
  `han-carlson`, `sklansky`, `kogge-stone`) or from an explicit N×N binary
  **cell map** (issue #1722, new module `klayout_tools/arith_gen.py`). Pure
  Python string generation — no Yosys, no PDK, no engine. Writes the
  structural adder, a behavioural `a + b + cin` reference with identical
  ports, a self-checking testbench, a Yosys `techmap -map` rule file
  substituting the adder for same-width `$add` cells, and a ready-to-run
  `klt equiv` request pairing the reference (gold) against the adder (gate).
  Reports `prefix_cells`/`logic_levels`/`max_fanout` so the cell-count vs.
  depth vs. fanout trade is visible without synthesizing. The representation
  (cell map) and the measured-in-loop selection below are reimplemented from
  Lai et al., *Scalable and Effective Arithmetic Tree Generation for Adder
  and Multiplier Designs* (NeurIPS 2024, arXiv:2405.06758); their RL/MCTS
  search is deliberately **not** reimplemented. See
  `docs/cli/arith-gen.md`.
- **Added**: `klt synthesize` request field `arithmetic` (issue #1722) —
  `{"adders": "auto" | "default" | "<architecture>", "min_width": 8,
  "candidates": [...], "verify_adders": true}`. Substitutes a generated
  prefix adder for Yosys's own `$add` expansion via a `techmap -map` rule
  inserted between `hierarchy` and `synth`, guarded by `_TECHMAP_FAIL_` so
  only the probed widths are replaced. `"auto"` runs a full trial synthesis
  per candidate **plus** Yosys's own default expansion, and keeps the
  smallest candidate meeting `constraints.clock_period_ns` (with an explicit
  reported fallback when none does). Every substituted adder is proven
  equivalent to a behavioural `a + b + cin` by `klt equiv` before it is kept
  — scoped to the adder module, so the gate works on sequential designs too.
  Additive: the new response field `arithmetic` is `null` for every request
  without the field, and `schema_version` is unchanged. Measured on the
  `modexp` canary at `WIDTH=16` (gf180mcu, `clock_period_ns: 22`): Yosys's
  own expansion missed the constraint at 23.79 ns while all five prefix
  architectures met it, and the selection kept the smallest of those.
  Multipliers (compressor trees) and search over cell maps are explicit
  non-goals. See `docs/cli/synthesize.md`'s "Arithmetic architecture"
  section and `docs/design/synthesize-qor-improvements-survey.md` §3.8.
- **Added**: `klt sim --plot <dir>` — renders one self-contained,
  dependency-free waveform SVG per non-sweep signal per corner (issue
  #1723), reusing `klt trajectory --plot`'s no-plotting-library, string-
  built SVG approach (new module `klayout_tools/sim_plot.py`). Forces
  `options.waveforms`/`keep_artifacts` on for the run regardless of the
  request's own settings — a waveform can only be plotted once captured.
  `ac` plots use a log10-scaled sweep axis; a `tran` measurement whose
  `.meas` card's own signal matches a rendered signal additionally gets its
  `from=`/`to=` window shaded and its `WHEN <signal>=<value>` threshold
  drawn as a reference line. Every written file is listed in the response's
  new `plots` field and each corner's `artifacts.plots`; a measurement's
  rollup entry additionally gets a `plot` field (the SVG for its own
  `worst_case` corner, or `null`) — so a miss report names the picture to
  open next, without the numeric threshold-crossing extraction that #56
  covers separately. All additive/optional (present only when `--plot` is
  used); `schema_version` unchanged. Also hardens the pre-existing
  `options.waveforms` capture path: a corner whose rawfile
  `parse_ascii_rawfile` cannot parse (currently `ac`/`sp`'s complex
  `real,imag`-encoded values — tracked as issue #1756, out of this issue's
  own scope) now reports a `"warning"` diagnostic and leaves
  `artifacts.waveform`/plots empty instead of raising and aborting the
  whole sweep. See `docs/cli/sim.md`'s "Waveform plots" section.
- **Added**: `klt kb validate --recheck-measured` — re-runs every distinct
  `measured.figures[].testbench` in `kb/entries/*.json` via `klt sim` and
  fails validation when a recorded `figures[].value` has drifted beyond a
  fixed 1% relative tolerance from what that testbench produces now, instead
  of only checking that the testbench path exists (issue #1726). Off by
  default and absent from the every-PR `klt kb validate` CI gate — actually
  simulating every measured entry is expensive — it is what the new nightly
  `.github/workflows/kb-drift-canary.yml` workflow
  (`schedule` + `workflow_dispatch` only, mirroring `equiv-canary.yml`'s
  posture for an expensive non-blocking-on-PRs simulation gate) runs. A
  drifted figure fails that job and is never auto-corrected. `klt kb
  validate`'s JSON payload shape is unchanged (drift shows up as additional
  `entries[].errors[]` strings, so no `schema_version` bump). Also raises
  `examples/kb/pfd-charge-pump-tri-state/request.json`'s
  `options.timeout_s` from `60` to `900`: that transient needs ~4 minutes of
  wall clock, so its own checked-in request could not reproduce the figures
  the KB entry records from it. See `docs/cli/kb.md`'s "Recheck measured
  figures" section.
- **Added**: `klt sim --op-lint` — a per-device operating-point sanity lint
  ("op-sanity") that answers "which MOSFET is not doing its job, and why?"
  instead of running the corner sweep (issue #1718). It takes the *same*
  request document (only `netlist` is required), runs one `.op` at one
  corner (`--op-lint-corner` picks which), and walks every MOS instance in
  the netlist reporting: `off` (`|Vgs| < |Vth|` with `Id` at/below a noise
  floor), `triode` (`|Vds| < |Vdsat|` plus a **corner-aware** margin
  defaulting to 2 kT/q at that corner's temperature), wiring smells
  (`drain_tied_to_rail`, `gate_shorted_to_source`, `bulk_not_tied`), and
  netlist hygiene (`missing_node` for a declared/`.meas`-referenced node the
  netlist never creates, `floating_node` for a single-terminal node). Each
  finding names the device, its four terminal nodes, the model-reported
  values, and a one-line suggestion; per-device `Vth` is read from the model
  (`@m…[vth]`), never a constant. Exit codes reuse `klt sim`'s vocabulary
  against this mode's own verdict (`0` no error-severity finding, `3` at
  least one, `4` the operating point itself is untrustworthy). The same
  entry point is exposed as a new `klt eval` gate kind, `op-sanity` — the
  cheapest useful analog gate, meant to sit *before* a `sim` gate in a
  descriptor. New module `klayout_tools/op_sanity.py`
  (`schema_version: 1`); no new dependency (it reuses the ngspice
  `print @m…[param]` path `klt size` already uses). The corner-sweep path is
  unchanged when `--op-lint` is absent. See `docs/cli/sim.md`'s
  "Operating-point lint" and `docs/cli/eval.md`.
- **Added**: `klt extract --parasitics` now reports a per-net `by_layer[]`
  breakdown alongside each net's scalar `resistance_ohm`/`capacitance_ff`
  totals (issue #1701) — `{"layer", "resistance_ohm", "capacitance_ff"}` per
  contributing conductor role (`"diffusion"`/`"poly"`) or metal level
  (`"metal<i>"`, 0-based), letting a caller composing a hierarchical netlist
  from separately-extracted sub-blocks attribute a net's R/C to a specific
  layer instead of hand-subtracting scalar totals. Purely additive (no
  `schema_version` bump per `docs/json-contract.md`'s additive-field policy)
  and derived entirely from intermediates `_compute_parasitics` already
  computed internally — no new geometry pass. Summing `by_layer[].resistance_
  ohm`/`.capacitance_ff` across one net's list reproduces that net's own
  scalar totals, except for a net named by `--mom-net`/`--mom-rlc-net` (those
  substitute the scalar totals with a value this lumped-RC breakdown did not
  compute). See `docs/cli/extract.md`'s "Per-layer breakdown" section.
- **Fixed**: the curated `gf180mcu` DRC deck now checks Metal4's own minimum
  width and spacing — `metal4.width.1` and `metal4.space.1`, both 0.28 um
  (280 dbu) on layer `(46, 0)` (issue #1688). Every other routing metal
  (`Metal1`-`Metal3`, `Metal5`, `MetalTop`) already carried both, and Metal4
  was otherwise present in the deck (its `Via3`/`Via4` enclosure rules and
  the MiM-cap stack), so a sub-0.28 um Metal4 wire — or two Metal4 wires
  closer than 0.28 um — previously came back `clean` rather than reported.
  Both values are transcribed from the same DRM table the sibling rules cite
  (`google/gf180mcu-pdk`,
  `docs/physical_verification/design_manual/tables_clear/22_Metaln_58.csv`,
  rules `Mn.1`/`Mn.2a`, whose published `0.28 (2 <= n <= 5)` range covers
  `n = 4`), under the already-registered `scope="7.13 Metaln"` — so
  `coverage.deck_scope` is unchanged. The gf180mcu deck is now 46 rules (was
  44), and `coverage.rules_skipped` gains the two new ids on a stream with no
  Metal4 geometry.
- **Fixed**: `klt extract --def-net-names` now recovers the real DEF net names
  of a **composed** layout's block — a DEF-merged P&R macro that `klt
  gen-compose` placed into a larger layout — instead of silently degrading
  every one of that block's nets to a synthesized `$<id>` placeholder (issue
  #1689). Composition never dropped the DEF net-name shape property; the scan
  that reads it looked in the wrong cell. KLayout's LEF/DEF reader stamps the
  property onto the routed geometry it draws in the *merge's own* top cell,
  and `gen-compose`'s composed GDS places that exact cell one level below the
  composed top (`<block_id>__<src_cell_name>`, hierarchy preserved rather
  than flattened) — so the property sat one level below the only cell
  `_def_net_name_probes` scanned. The scan is now depth-1: the extraction top
  cell's own shapes (unchanged) plus exactly one level into each direct child
  cell instance, with each candidate probe point transformed into the
  composed top cell's own coordinate frame before it is resolved. A
  standalone (non-composed) layout's output is unchanged, and the original
  false-positive guard is kept for anything nested deeper — a standard cell's
  own internal annotation several levels down still is not read as a
  top-level net name. **Known limitation**: a block composed *more than one*
  level deep (a macro composed into a macro composed into a macro) is not
  reached; nothing in this codebase currently produces that shape, since
  `gen-compose` always places a source block exactly one level below its own
  composed top. See [`docs/cli/extract.md`](docs/cli/extract.md)'s
  "DEF-derived net names" section.
- **Added**: `klt sim --format json` now reports
  `environment.timeout_preflight_warning` (part of issue #1686) — an
  advisory string emitted before the corner grid starts when the requested
  `options.timeout_s` looks implausible for a `tran` analysis's own declared
  step/window. A per-corner `timeout_s` protects against a *hang*, but
  nothing protected against a budget that can never be met: a real incident
  widened a `tran` window 33× while raising `timeout_s` only 6×, and all 45
  corners came back `timeout` after burning their full budget for ~45
  wall-clock hours with no usable result. The check compares `timeout_s`
  against the `window / step` timepoint count under a deliberately generous
  50 timepoints/second floor (**not** a real per-engine throughput estimate),
  is scoped to `kind: "tran"` only, and is **advisory only — it never blocks
  the sweep**; a budget that looks plausible under this coarse floor can
  still be wildly insufficient in practice. Present only when the heuristic
  has something to say, absent for the common case — purely additive, and
  `klt sim`'s `schema_version` stays `1`. The stronger dispatch-time abort
  scoped by the same issue is **not** implemented (it needs a timed-out
  corner's `reached_s`, which no investigated mechanism can recover safely
  against this module's per-corner deck shape); issue #1694 tracks that open
  design question with the empirical findings. See
  [`docs/cli/sim.md`](docs/cli/sim.md)'s "Timeout-budget preflight" section.
- **Added**: `klt extract --parasitics` gains **`--parasitics-net NET`,
  repeatable** (issue #1700) — it scopes the per-net **ground R/C pass** to
  the named nets instead of measuring every net in the design, and the
  request is echoed back verbatim as the additive
  `parasitics.parasitics_nets` field. The whole-layout default is the
  dominant cost of `--parasitics` on a large block (each net's geometry is
  queried once per conductor role), so a mixed-signal top cell can spend
  tens of minutes producing R/C for thousands of nets when the caller
  wanted it for a handful. Requires `--parasitics` (the flag alone is an
  error, same as `--critical-net`); a name matching no net with
  ground-eligible geometry is reported in `warnings`, not raised, and an
  anonymous net is named in the same backslash-escaped spelling
  `parasitics.nets[].net` reports (issue #1162). This is the only
  `--parasitics` flag that *removes* content from the report, so two
  consequences are deliberate rather than layout findings: an unnamed net
  gets **no `parasitics.nets[]` entry, no injected `R`/`C` SPICE cards, and
  no contribution to `total_resistance_ohm`/`total_capacitance_ff`** — the
  totals of a scoped run are partial by construction; and because both
  coupling passes are computed from the per-net geometry the ground pass
  caches, **only a pair of both-named nets can couple** — naming a
  crosstalk victim without its aggressor reports zero coupling for it. A
  `--critical-net` net left out of `--parasitics-net` gets its own
  `warnings` entry naming the responsible flag combination, instead of the
  generic "matches no net in this layout" wording that would misdirect the
  caller at their layout. The flag changes *which* nets are measured, never
  *how*: a named net's `resistance_ohm`/`capacitance_ff` are bit-for-bit
  what the full-layout pass reports for it. `parasitics.parasitics_nets` is
  `[]` when the flag was never given, and omitting the flag leaves every
  field byte-identical to before it existed — purely additive, and `klt
  extract`'s `schema_version` stays `3`. See
  [`docs/cli/extract.md`](docs/cli/extract.md)'s "Scoping the ground R/C
  pass to named nets" section.
- **Fixed**: `klt gen-compose`'s `connectivity[]` router now retries a leg
  onto `routing.cross_block_layer_role` when it is rejected for crossing a
  *different* net's already-accepted route, instead of failing the whole net
  outright (issue #1680). Two nets sharing one `routing.layer_role` purely
  because it is the request's one primary layer — not because either
  genuinely needs that physical plane — could not resolve a crossing between
  them before this: `route_two_pin()`'s own same-block cross-layer retry
  (#1168/#1393) never fired, since it only ever engages from its own checks
  3/4, one level below where the route-vs-route collision check (#1057/#1386)
  actually runs. The rejected leg is now re-routed once with the primary and
  cross layers swapped and accepted only if it also clears the collision
  check there; a leg that collides on both layers still fails the whole net.
  A genuine same-layer short with no `routing.cross_block_layer_role`
  configured is unaffected. See
  [`docs/cli/gen-compose.md`](docs/cli/gen-compose.md)'s "Known limitations"
  section.
- **Fixed**: `klt gen-compose` now warns when a block's declared `bbox_um`
  understates its own real drawn geometry, and that undeclared excess
  geometry physically overlaps another placed block (issue #1679). A
  `blocks[].generator_report` entry's `bbox_um` is trusted verbatim for
  placement math (never re-derived from its own stream) — but a block whose
  real geometry (e.g. an externally-produced macro's guard/seal ring) extends
  past what it declared could have that excess silently overlap a neighbour
  placed just outside the *declared* bbox but still inside the *real* one.
  `klt drc` reports this clean (a zero-clearance same-layer merge is not an
  illegal shape by any spacing rule); the corruption previously only
  surfaced later as a spurious `klt extract` `merged_net_labels` entry
  joining two of the macro's own unrelated nets, even though extracting the
  same, unmodified macro GDS directly (no composition) was clean.
  `gen-compose` now reads each block's own stream to compare its real placed
  bbox against its declared one, and — only for a block where they disagree —
  checks its real per-layer geometry against every other block's for an
  actual shape overlap, adding one `warnings[]` entry naming both blocks and
  the shared layer when found. Advisory-only (never raises, never blocks
  composition), applies to `"row"`/`"explicit"` placement alike. See
  [`docs/cli/gen-compose.md`](docs/cli/gen-compose.md)'s "A declared
  `bbox_um` is trusted, not verified" section.
- **Fixed**: `klt gen-compose`'s obstacle-overlap routability check is now
  layer-scoped instead of layer-agnostic (issue #1656). The check used to
  test a candidate route's backbone against every other placed block's
  *bbox*, regardless of which layers that block actually draws shapes on —
  so a route on one layer (e.g. `metal3`, a router-only role no `klt gen`
  generator draws pads on) could be rejected, or detoured around (#1167),
  for "crossing" a neighboring block's bbox even when that block draws
  nothing at all on that layer: no physical short is possible, only a bbox
  intersection on paper. `route_two_pin()` now looks up each candidate
  obstacle block's own drawn geometry on the leg's effective route layer
  (reusing the same lazy per-block `read_block_layer_geometry` cache
  `compose()` already builds for #1520/#1527's own-block checks) and skips a
  block as an obstacle when it draws nothing there. A block that does draw
  something on that layer is still rejected/detoured around exactly as
  before — this removes false positives only, it does not weaken the check
  against a genuine obstacle. See
  [`docs/cli/gen-compose.md`](docs/cli/gen-compose.md)'s "Known limitations"
  section.
- **Added**: `klt extract --format json` now reports
  `devices[].instance_path` (issue #1666) — the chain of GDS-level cell
  placements each device's recognition geometry sits inside, outermost
  first: `[{"cell": str, "array_index": [ia, ib] | null}, ...]`, with
  `array_index` giving the 0-based element position within a regular
  `CellInstArray` (`null` for a plain single placement) and `[]` meaning the
  geometry was drawn directly in the top cell. Extraction flattens the whole
  instance tree before device recognition runs, so until now nothing in the
  report distinguished devices coming from different repeated copies of the
  same leaf cell; this is the device-level counterpart to the net-level
  `nets[].net_id`/`pin_index`/`label_positions_um` disclosure added in issue
  #1540, and is resolved positionally after extraction from each device's
  own recorded position against the layout's instance tree. Purely additive
  disclosure — no device, net, parameter, or written netlist line changes,
  and `klt extract`'s `schema_version` stays `3`. See
  [`docs/cli/extract.md`](docs/cli/extract.md)'s "Per-device GDS instance
  attribution" section for the worked example, the rationale for choosing an
  additive field over a non-flattening `--preserve-instances` mode, and the
  caveat that `[ia, ib]`'s axis is the GDS file's rather than the authoring
  order's.
- **Fixed**: `klt lvs`'s internal `_prune_extra_top_circuits` now compares
  circuits by identity (`circuit is not keep`) instead of `cell_index`
  (issue #1657). Every netlist read directly from SPICE/Verilog text — every
  reference netlist, and the pre-extracted `layout.netlist` shape — has no
  backing `kdb.Layout`, so every circuit's `cell_index` previously read back
  as `0`, making the old `cell_index`-based comparison always false and
  silently pruning nothing. This could surface as a spurious `"mismatch"`
  status when a reference or layout netlist declared an extra, unrelated
  top-level `.subckt` alongside the circuit pinned via `top`. Layout-backed
  netlists (distinct `cell_index` values) already pruned correctly and are
  unaffected.
- **Added**: `DrcRule.check` accepts a new single-layer check kind,
  `"isolated"` (issue #1654), dispatching to KLayout's
  `Region.isolated_check` — spacing measured only between edges of
  *different* polygons, unlike `"space"` (`Region.space_check`), which also
  measures the width of a concave notch carved into a single merged polygon.
  `klt drc` can therefore emit `"isolated"` as a `violations[].check` value;
  this is additive value-set growth on an unchanged shape, so `klt drc`'s
  `schema_version` stays `1` (see [`docs/json-contract.md`](docs/json-contract.md)'s
  "Pre-1.0 caveat: value sets within an unchanged shape can grow").
- **Fixed**: the `sky130` curated deck's `nwell.space.1` now uses
  `check="isolated"` instead of `check="space"` (issue #1654), matching the
  real semantics of its source rule — `sky130.lydrc`'s `nwell.2a` is
  `nwell.isolated(1.27, euclidian)`, an inter-polygon spacing check.
  Transcribing it as `"space"` also flagged a concave notch *within* a
  single merged nwell island, which `nwell.2a` does not consider a
  violation; on a production layout roughly a third of the reported
  `nwell.space.1` violations were this same-polygon false-positive class.
  Genuine inter-well spacing violations are still caught unchanged. Two
  consumer-visible consequences: violations of this rule now carry
  `check: "isolated"` rather than `check: "space"`, and `nwell.space.1` no
  longer appears in `tests/golden_deck/sky130/manifest.json` (that
  manifest's sky130 scope is `("width", "space")`). The rule itself is
  unchanged in id, layer, and 1.27 um threshold.
- **Fixed**: `klt lvs`'s `reference.form: "gate-level-verilog"` compare no
  longer reports a false `topology` "circuit could not be matched to a
  counterpart" mismatch for a layout-side power-only cell — a filler cell
  (`sky130_fd_sc_hd__fill_*`, inserted unconditionally whenever issue
  #1442's row-rail fallback fires) or a tap cell
  (`sky130_fd_sc_hd__tapvpwrvgnd_1`, inserted unconditionally by the
  `tapcell` stage) — that a gate-level-Verilog reference never instantiates
  at all (issue #1622). `klt lvs` now removes every layout circuit whose
  entire declared pin list is a power/ground pin of `reference.library`
  (derived per run from that library's own `.subckt` data, never a
  cell-name glob or a hardcoded per-PDK power-pin table), along with every
  instance of it, before the compare runs, disclosing each removal as a new
  `severity: "warning"`, `category: "topology.power_only_pruned"`
  `mismatches[]` entry so a `"match"` reached this way stays auditable from
  the report alone. Scoped strictly to `reference.form:
  "gate-level-verilog"` — the same inference is unsound against a
  `"plain-element"`/`"subckt-call"` reference, whose pin names need not
  overlap the layout's. Additive; `schema_version` stays `1`. See
  [`docs/cli/lvs.md`](docs/cli/lvs.md)'s "`topology.power_only_pruned`"
  section.
- **Fixed**: `klt place-and-route` no longer raises `PlaceAndRouteError` when
  reaching the `"cts"`/`"route"` stage against a
  `gf180mcu_fd_sc_mcu7t5v0` (7-track) netlist (issue #1649). The module's six
  per-cell-library reference tables (`_CTS_BUFFER_CELLS`,
  `_ROUTING_LAYER_RANGE`, `_ANTENNA_DIODE_CELLS`, `_TAPCELL_CELLS`,
  `_FILLER_CELLS`, `_POWER_PIN_PATTERNS`) previously carried only a
  `gf180mcu_fd_sc_mcu9t5v0` (9-track) entry, so any 7-track run's table
  lookup failed before OpenROAD did any real work. Each table now also
  carries a verified `gf180mcu_fd_sc_mcu7t5v0` entry, sourced the same way
  as the existing 9-track ones: `platforms/gf180/config.mk`'s
  `TRACK_OPTION`-templated variables for the CTS buffer/tapcell/filler/power
  pin-pattern cells, and that library's own LEF (`CLASS .../ANTENNACELL`
  and pin data) for the antenna-diode and routing-layer-range rationale.

- **Fixed**: `klt sta`'s `spef_annotation.annotation_complete` (issue #1624)
  no longer reports `true` for a SPEF OpenSTA's `read_spef` then discards.
  The field previously rested on a *name-correlation* check measured
  **before** `read_spef`, with a different name resolver (`get_nets`) than
  the SPEF reader's own — so a SPEF whose flattened names contain the SPEF
  divider character (e.g. a `generate`-block hierarchy flattened to
  `g_slice[0].u_slice/_08_`) could correlate perfectly and still be thrown
  away wholesale by the reader, yielding `annotation_complete: true`
  alongside timing numbers bit-identical to the unannotated run. `klt sta`
  now measures three further pieces of evidence *around and after*
  `read_spef`, each of which independently forces `annotation_complete:
  false`: `reader_warning_count` (+ a capped `reader_warning_sample`) —
  OpenSTA's own `STA-1650`/`STA-1648` "net/instance not found" reader
  diagnostics, matched on both stdout and stderr; `delay_changed` — the
  identical `report_checks -path_delay min_max -digits 6 -unconstrained` report taken
  before and after `read_spef`, `false` when byte-identical; and
  `unannotated_driver_count` — `report_parasitic_annotation`'s own count of
  drivers OpenSTA holds no parasitics for. A companion
  `partially_unannotated_driver_count` is reported but deliberately does
  **not** gate (a complete, correctly-read SPEF routinely reports a non-zero
  partial count). Unknown evidence (an OpenROAD build emitting none of these)
  degrades to `null` and never gates, so the pre-existing name-correlation
  verdict still applies. Additive; `schema_version` stays `1`. See
  [`docs/cli/sta.md`](docs/cli/sta.md)'s "Annotation evidence" section.
- **Added**: `klt extract --deck sky130` now recognises sky130's drawn
  metal-resistor device family (issue #1621): `res_generic_m1`..
  `res_generic_m5` (`metN.drawing` covered by the PDK's own `metN.res`
  resistor-ID mark, `N` = 1..5), extracting a bound `r_ohm` from each
  layer's own sheet resistance (0.120/0.120/0.047/0.047/0.029 Ω/□ for
  met1/met2/met3/met4/met5) instead of leaving the marked segment
  unrecognised. Previously, ingesting a subckt-call-form reference netlist
  that named one of these devices failed outright — there was no device
  class for it at all. `--pdk` leaves them as the bare `R`-card form (no
  curated subcircuit binding): sky130's own device library defines no
  `.subckt` for any metal flavour, only a bare `.model ... r` card, the
  same carve-out `res_metal1`/`res_metal2` already document for sg13g2.
  `klt lvs` matches the new devices through the existing generic
  device-comparison path with no LVS code changes. `klt gen` has no
  generator for this family yet — filed as a follow-up. See
  [`docs/cli/extract.md`](docs/cli/extract.md)'s "Drawn resistors" table.
- **Added**: `klt extract --deck gf180mcu` now recognises gf180mcu's drawn
  metal-resistor family (issue #1640, the gf180mcu counterpart of #1621
  above): `rm1`/`rm2`/`rm3` (`Metal1`/`Metal2`/`Metal3` covered by the PDK's
  own `metal1_res`/`metal2_res`/`metal3_res` resistor-ID marks, 0.09 Ω/□
  each) plus a `tm6k`/`tm9k`/`tm11k`/`tm30k` top-metal thickness flavour set
  (0.06/0.04/0.04/0.0095 Ω/□, caller-selectable via `--deck-option
  metal_top=`, `9K` the PDK's own default — mirroring `poly_res`, issue
  #595). Previously, a marked metal segment extracted as an ordinary short
  and a reference netlist naming one of these devices failed to ingest at
  all. Unlike sky130's `res_generic_mN` carve-out, all seven of these
  gf180mcu classes have a real two-terminal `.subckt` in the vendored
  `sm141064.ngspice` device library, so `--pdk` binds each to a genuine `X`
  subcircuit call rather than a bare `R` card. `klt lvs` matches the new
  devices through the existing generic device-comparison path with no LVS
  code changes. See [`docs/cli/extract.md`](docs/cli/extract.md)'s "Drawn
  resistors" table.
- **Added**: `klt gen-compose`'s `routing.cross_block_layer_role` fallback
  plane (issue #1168) now has its own, independent width knob —
  `routing.cross_block_width_um` (issue #1620). Previously
  `routing.width_um` was validated against *both* `routing.layer_role`'s own
  deck-minimum floor and `routing.cross_block_layer_role`'s (issue #1501),
  so naming a cross-block layer with a stricter minimum silently forced
  every net's *primary*-plane routing wider too. `routing.width_um` is now
  floored against `routing.layer_role` only; a leg that actually falls back
  to `routing.cross_block_layer_role` draws at `routing.cross_block_width_um`
  instead, defaulting to that layer's own deck minimum when omitted and
  floored against it the same way, with the identical error shape. Additive;
  `schema_version` stays `1`. Same behavior reachable from
  `layout_plan_execute`'s `request.routing.cross_block_width_um`. See
  [`docs/cli/gen-compose.md`](docs/cli/gen-compose.md)'s "Cross-block bus
  routing" section.
- **Added**: `klt synthesize` now reports static leakage power (issue
  #1626): `leakage_power_nw` — `sum(cell_leakage_power[cell_type] *
  instance_count[cell_type])` over the response's own
  `instance_counts_by_type`, in nanowatts — plus a per-cell-type
  `leakage_by_type_nw` breakdown, both read from the same resolved liberty
  already loaded for `dfflibmap`/`abc -liberty` (no second liberty fetch,
  no new PDK lookup). Static leakage only — dynamic/switching power needs an
  activity factor this command has no vectors to supply, and stays out of
  scope. Both fields are `null` — never a fabricated or partial-looking
  number — when the resolved liberty reports no `cell_leakage_power` scalar
  for *any* instantiated cell type (some libraries, e.g.
  `gf180mcu_fd_sc_mcu9t5v0`, report leakage only via per-input-state
  `leakage_power()` groups this command deliberately does not average into
  one number). Additive; `schema_version` stays `1`. See
  [`docs/cli/synthesize.md`](docs/cli/synthesize.md)'s
  "`leakage_power_nw`/`leakage_by_type_nw`" section.
- **Fixed**: `klt lef-abstract` (issue #1613) no longer crashes with an
  unhandled `AttributeError: 'SimplePolygon' object has no attribute
  'each_point_hull'` when a routing layer's merged obstruction region
  contains a non-rectangular polygon. The OBS formatter called
  `each_point_hull()` — a method that exists only on `kdb.Polygon`, not on
  the `kdb.SimplePolygon` it was calling it on — so the command always
  succeeded on a purely rectangular layout and always failed on any design
  with L-shaped/T-shaped metal, or whose pin ports only partially overlap a
  metal run. Such shapes now emit as the LEF `POLYGON` they always should
  have; rectangular geometry still emits as `RECT`, and no JSON field
  changed.
- **Fixed**: `klt lef-abstract` (issue #1614) now finds a declared pin's
  real drawn geometry when a PDK puts a routing layer's drawn metal and its
  pin/net text label on two different GDS datatypes of the same LEF layer
  (e.g. gf180mcu's `Metal1`: `(34, 0)` drawn, `(34, 10)` pin label). Pin
  resolution previously searched only the exact datatype the socket
  descriptor's `pins[].layer` declares — the same datatype `klt
  socket-check` reads the pin's text label from — so a pin satisfying
  `socket-check`'s label-layer contract could never see real drawn metal on
  a sibling datatype, and was always synthesized as a placeholder box even
  when metal existed. `_resolve_pins` now searches every GDS datatype that
  shares the pin's resolved LEF layer name, mirroring `_resolve_obs`'s
  existing per-LEF-layer union across datatypes. See
  [`docs/cli/lef-abstract.md`](docs/cli/lef-abstract.md).
- **Fixed**: `klt extract --parasitics --spef` (issue #1627) no longer
  stamps the SPEF header's `*DATE` line with the write's own wall-clock
  time. `*DATE` is optional and informational-only per the IEEE 1481-1999
  SPEF grammar (no downstream reader parses it back), so it is now a fixed
  placeholder — two runs of identical extraction against an unchanged input
  layout now produce byte-identical `.spef` output, matching the
  reproducibility guarantee `klt place-and-route`'s DEF→GDS merge already
  makes for GDS2 output (issue #1367).
- **Added**: `klt sta` now reports `worst_hold_slack_ns`/
  `total_negative_hold_slack_ns` (issue #1625), the hold-side counterparts of
  the existing setup-only `worst_slack_ns`/`total_negative_slack_ns` — from
  `report_worst_slack_metric -hold`/`report_tns_metric -hold`, mirroring
  `klt place-and-route`'s own `worst_hold_slack_ns` field name/pairing
  convention so the two commands' output correlates without a naming
  mismatch. Previously the only hold-side signal in the response was
  `hold_violation_count`, a count that could say a corner *has* a hold
  violation but not rank corners by hold margin or identify the binding hold
  corner in a characterization sweep. `report_tns_metric -hold` ->
  `timing__hold__tns` was verified live against a real `openroad/orfs`
  container (not assumed by analogy) before shipping. Purely additive; no
  `schema_version` bump. See [`docs/cli/sta.md`](docs/cli/sta.md).
- **Added**: new verb `klt clip` (issue #1608): writes a subset of an
  existing GDSII/OASIS stream back out as its own top-cell stream, either a
  micrometre bounding-box region (`--region`, the same inline-JSON shape
  `klt ring-check`/`klt components` already accept) or a named cell's full
  subtree (`--cell`, any cell in the stream, not just a top cell). Fills a
  gap identified by #1603: handing a single device or region to an external
  tool (EM extraction, third-party meshing, or just isolating it for review)
  previously had no `klt` path — `--region` on `klt ring-check`/`klt
  components` only ever *restricted analysis*, it never wrote the clipped
  subset back out. Region-clip mode flattens every layer of the resolved top
  cell (`clip_box()` + `region()`/`texts()`) and intersects it with the
  window; cell-extraction mode copies the named cell's subtree verbatim
  (`kdb.Cell.copy_tree()`), preserving nested hierarchy exactly. Output is
  written via the existing shared `write_layout()` (deterministic, no
  embedded timestamp). Errors clearly on a nonexistent `--cell`, a `--region`
  that rounds to zero width/height at the input's own database unit, and a
  `--region` that matches no geometry. See
  [`docs/cli/clip.md`](docs/cli/clip.md).
- **Added**: `klt pdk stackup` (issue #1609) — the resolved PDK variant's
  process cross-section as structured data: per conductor/via layer its
  elevation (`z0_um`/`z1_um`), thickness, sheet (or per-cut) resistance and
  derived `conductivity_S_per_m`; per dielectric its z-range and relative
  permittivity. This is the z-axis information a GDSII/OASIS file does not
  carry and an external E&M/field solver requires, and it closes the open
  question `docs/design/em-field-sim-spike.md` deferred ("where the sky130
  stackup table itself lives as a `klt`-owned asset"). Unlike `klt pdk
  em-limits`, this is **not** a pure live parse: real tech LEFs carry no
  elevation and no dielectric constant, so those come from a curated
  per-family table (`src/klayout_tools/pdk_stackup.py`) whose every entry
  cites the open, non-NDA'd published source it was transcribed from
  (open_pdks' own `sky130A.tech` `height` stanza / `defaultareacap`
  coefficients, `sky130.xs`, `sky130A.map`), while thickness and resistance
  are derived live from the install's tech LEFs at the selected `--corner`.
  A variant whose family has no curated entry is an explicit exit-1 error,
  not a partial stack. `--thickness curated|tech-lef` selects which of the
  two published film thicknesses drives the emitted geometry (default keeps
  the stack gap-free). Curated today: `gf180mcu`, `sky130`. See
  [`docs/cli/pdk.md`](docs/cli/pdk.md).
- **Added**: `klt pdk stackup --pdk gf180mcuD` (issue #1616) — a second
  curated family, `gf180mcu`, alongside `sky130`
  (`supported_families()` now `["gf180mcu", "sky130"]`). Verified against a
  real `volare`-fetched `gf180mcuD`: elevation/thickness come from
  `libs.tech/magic/gf180mcuD.tech`'s `height` stanza (five metal levels of
  uniform 0.55 µm, `met5` at 1.0025 µm, and **no local-interconnect layer**
  — `met1` is the first conductor, unlike sky130's `li1`/`mcon`), GDS
  layer/datatype from `libs.tech/magic/gf180mcuD-GDS.tech`'s `calma`
  statements, and permittivity (ε_r = 4.0, distinct from sky130's 3.9)
  corroborated the same arithmetic way against this install's own
  `defaultareacap` coefficients. gf180mcu ships no KLayout XSection script
  analogous to sky130's `xsect/sky130.xs`, so dielectric slab boundaries are
  induced purely from the `height` stanza's contiguity and film *material*
  names are a disclosed generic assumption rather than an install-specific
  citation (see `_GF180MCU_STACKUP`'s provenance note in
  `src/klayout_tools/pdk_stackup.py`). `ihp-sg13g2` remains an uncurated
  variant and continues to error clearly, naming both curated families.
- **Added**: `klayout_tools.lef_header.parse_lef_header()` now also reports a
  `CUT` layer's per-cut `RESISTANCE` as `layers[].resistance_ohms` (additive;
  `null` on `ROUTING` layers, which state `RESISTANCE RPERSQ` instead) —
  the only resistance a via layer declares, needed by `klt pdk stackup`.
- **Added**: `klt functional-verification --mutations <proposals>` (issue
  #1592) — mutation testing as a test-quality gate: applies a set of
  hand- or agent-authored byte-exact single-point RTL mutations, one at a
  time, to an isolated from-scratch build+test of the same request, and
  reports how many the testbench actually caught. Ported (Apache-2.0
  attribution header, `src/klayout_tools/_vendor/mutation_variants.py`) from
  [`boldaxolotl/booley`](https://github.com/boldaxolotl/booley)'s
  no-HDL-parsing mutation seam, per
  [`docs/design/mutation-testing-spike.md`](docs/design/mutation-testing-spike.md)
  (issue #1586). The baseline run is reused unchanged from the existing
  request/response contract; a baseline that itself fails aborts the whole
  run before any proposal is attempted (exit 1 — comparing a mutant against
  an already-broken baseline is meaningless). Each isolated per-mutant
  build+test is bounded by a `klt`-level subprocess timeout, independent of
  whatever cycle bound the testbench's own code has — a mutation that
  wedges the DUT's FSM is classified `"killed"` on timeout, per booley's own
  rule, rather than hanging the whole run. Adds an additive
  `mutation_testing` response block (`proposal_count`, `valid_count`,
  `killed_count`, `survived_count`, `rejected_count`, `mutation_score`,
  `results[]`) and extends the exit-code table: exit 3 when the baseline
  passed but at least one valid proposal survived, matching `klt drc`'s
  "ran fine, found violations" convention. See
  [`docs/cli/functional-verification.md`](docs/cli/functional-verification.md)'s
  new "Mutation testing: `--mutations`" section, and
  [`examples/functional-verification/proposals-modexp.json`](examples/functional-verification/proposals-modexp.json)
  for a worked example reproducing the spike's own live finding: one
  comparison-boundary mutation on `modexp.v`'s modular reduction step that
  survives the committed testbench unnoticed, alongside one bit-select
  mutation that is correctly killed.
- **Added**: `klt synthesize`'s response gains three additive fields (issue
  #1588): an always-present `structural` verdict (`{latches,
  expected_latches, unexpected_latches, comb_loops, multi_driven,
  has_critical}`) over inferred latches, combinational loops, and
  multiply-driven nets — all three drawn from the same Yosys `synth`/`stat`
  run this command already performs, no extra invocation; an always-present
  bounded, deterministic `warnings` summary (`{total, by_category,
  representatives}`) of the captured Yosys run log; and an optional
  `baseline` comparison (`{ref, instance_count, area_um2, critical_path_ns,
  delta_pct}`) against a prior run named by the request's new
  `baseline.response_path`/`baseline.netlist_path`. The CLI now exits `3`
  when `structural.has_critical` is `true` (`status` stays `"ok"`),
  reversing this command's own prior "no exit code 3" decision — additive
  per `docs/json-contract.md`'s rule that a command may define codes above
  `2`; no `schema_version` bump (every new field is additive). See
  `docs/cli/synthesize.md`'s "`structural`"/"`warnings`"/"`baseline`"/"Exit
  codes" sections.
- **Fixed**: `klt components`'s `--label-layers` entries can now declare an
  optional `"conductor"` name (e.g. `{"name": "m1pin", "layer": [34, 10],
  "conductor": "m1"}`), scoping that label layer's texts to only that
  conductor's own shapes in a component. Previously every label layer's texts
  were matched against the union of *every* conductor/via layer present in a
  component, so a text on a lower conductor's pin/label layer was wrongly
  attributed to an unrelated, upper-layer component whose geometry merely
  crossed over it in XY with no via joining them — on any real multi-layer
  block with a power stitch or long route, this made `labels` an unreliable
  net-name/short detector (issue #1579). An unknown `"conductor"` name now
  raises `ComponentsError` (exit `1`), matching how an unknown via `"between"`
  conductor is already validated. `"conductor"` is optional and omitting it
  keeps the original any-layer matching for backward compatibility — this is
  an additive JSON field, not a `schema_version` bump.
- **Changed**: `klt gen guard_ring`'s (and, since they compose the same
  ring-drawing code, `mos_array`'s/`diff_pair`'s/`bjt_array`'s/
  `esd_device`'s) `gf180mcu` geometry now draws a second implant ring,
  exactly coincident with the tap/collector ring itself, closing the same
  `DF.12` ("COMP not covered by Nplus/Pplus is forbidden") coverage rule
  #1577 closed for each unit device's own drawn `Comp` body but did not
  reach the ring's `Comp` shape (issue #1580, a follow-up to #1577). The
  doping matches whether the ring encloses a well: a well-tied ring
  (`guard_ring`'s own `add_well` default, or a `flavor="pfet"` well on
  `mos_array`/`diff_pair`) reuses the `"well_tap_implant"` role (Nplus)
  `well_island` already reuses for its own ring (issue #1421's precedent);
  a substrate-tied ring (no enclosing well, including `bjt_array`'s
  collector ring) gets the opposite doping, `"pplus"` (the same p+ role
  #1577 added for a `flavor="pfet"` unit device's own source/drain). This is
  a real, deliberate geometry change on `gf180mcu` only, gated on a
  `ring_implant_present`-style flag — `sky130`/`sg13g2`/`sg13cmos5l`
  geometry is unaffected. **Not verified against gf180mcu's own signoff DRC
  tooling** (this sandbox has no real `run_drc.py` to check it against) —
  see [`docs/cli/gen.md`](docs/cli/gen.md)'s updated gf180mcu note.
- **Changed**: `klt gen mos_array`'s (and, since they compose the same
  unit-device drawing, `diff_pair`'s/`esd_device`'s) `gf180mcu` geometry now
  margins against six real gf180mcu signoff-DRC rule ids
  (`DF.6_LV`/`PL.4_LV`/`PL.5a_LV`/`PL.5b_LV`/`CO.7`/`DF.12`) that this repo's
  own curated `gf180mcu` DRC deck never transcribed, and that #1575 found
  and documented as real violations of the pre-existing default `gf180mcu`
  output (issue #1577). The unit device now draws: a gate-pad clearance off
  the diffusion edge; a symmetric bottom-edge gate endcap (the top edge
  already cleared these rules via the #461 landing pad); an extra
  S/D-contact-to-gate clearance floor; a source/drain implant
  (`Nplus`/`Pplus`, selected by `flavor`) covering every unit device's
  `Comp` body; and (for `voltage_flavor: "medium_voltage"`) a wider
  `Dualgate` marker box. This is a real, deliberate geometry change on
  `gf180mcu` only — the reported `U<i>_G` port and the array/pair's overall
  footprint move — `sky130`/`sg13g2`/`sg13cmos5l` geometry is unaffected.
  **Not verified against gf180mcu's own signoff DRC tooling** (this sandbox
  has no real `run_drc.py` to check it against) — see
  [`docs/cli/gen.md`](docs/cli/gen.md)'s updated gf180mcu note for exactly
  what changed and what remains unverified.
- **Added**: documented and worked-example recipe for drawing a unit MOS
  device narrower than `mos_array`/`diff_pair`'s `UNIT_MIN_W_UM` contact-fit
  floor (`0.42` µm) — issue #1574, the follow-on #322's own triage comment
  flagged as the trigger for revisiting the hard floor. Rather than adding a
  generator-level override, `docs/cli/gen.md` now documents the standard
  "dog-bone terminal" hand-drawn recipe (widen only the source/drain pads to
  the generator's own contact-fit pad size, keep an unwidened shoulder
  between each pad and the gate edge, draw the gate-crossing channel at the
  requested narrow width) directly beneath both generators' `w_um` rows, and
  `examples/dogbone-terminal/` is a new runnable, checked-in worked example
  (`generate.py`) that hand-draws exactly this device with `klayout.db` on
  the same PDK role layers `mos_array` itself resolves to, places it beside
  a real `klt gen mos_array` unit device in one cell, and verifies the
  composed result `klt drc`-clean on both `sky130` and `gf180mcu`
  (`example_sky130.gds`/`example_gf180mcu.gds` with their `.drc.json`
  reports checked in). No generator behavior changes — `mos_array`/
  `diff_pair` still reject `w_um` below `UNIT_MIN_W_UM` outright, exactly as
  before.
- **Added**: `klt gen cap_array` now supports the `gf180mcu` PDK family
  (issue #1555 — the follow-on issue #1117 explicitly deferred when it
  scoped this generator to sky130, and the gf180mcu counterpart of #1455's
  sg13g2 support). It draws the *same* layer/datatype numbers
  `klayout_tools.decks.gf180mcu`'s `EXTRACTION_DECK.capacitors[0]`
  (`cap_mim_2f0_m4m5_noshield`) already declares — `FuseTop` top plate over a
  `Metal4` bottom plate, `Via4`/`Metal5` top-plate via and landing pad — plus
  the `CAP_MK`/`MIM_L_MK` masks that entry's own `top_plate_requires`
  demands, so the output round-trips through `klt extract --deck gf180mcu`
  to that device class (and to its `1f0`/`1f5` density siblings via
  `--deck-option mim_cap=…`, issue #1151) instead of extracting as zero
  capacitors. Three per-family geometry floors keep the default output
  DRC-clean where the generator's generic values are too small for this
  family's MiM rules: the bottom plate is drawn at the DRM's own 1.06µm
  "virtual bottom plate" oversize (`CapacitorDevice.bottom_plate_oversize_um`,
  vs. `mim.enclosing.fusetop.1`'s 0.6µm minimum), the top-plate via at
  `via4.width.1`'s 0.26µm, and a requested `params.spacing_um` below
  `mim.space.1`'s 1.2µm is **widened** to it rather than rejected —
  `drc_hints.min_spacing_um` reports the spacing actually drawn, with a new
  `drc_hints.notes` entry naming the widening. No other PDK family's drawn
  geometry, ports, or hints change (every floor applies as `max(generic,
  floor)` and only gf180mcu sets any). One related fix: `cap_array`'s
  harness-computed `cap_top_via_metal_min_w_um` PCell parameter (added by
  #1455) was missing from `gen.py`'s `_HIDDEN_PARAMS`, so it was advertised
  as a request param by `klt gen --list` despite never being documented as
  one; it and the three new floors are all hidden now.
- **Fixed**: `options.combine_devices_per_circuit` (issue #1552) now carries
  the same two post-combine corrections the whole-netlist
  `options.combine_devices` path already applies (issue #1557): the
  deferred resistor `fixed_offset_ohm` correction (issue #559/#585) and the
  capacitor `C` sum-conservation check (issue #1497). Both were previously
  gated only inside the `if combine_devices_enabled:` block, which never
  runs for a `combine_devices_per_circuit`-only request (the two options
  are mutually exclusive), so a matched circuit's folded `res_high_po`
  resistors never received the deferred offset and a folded capacitor's `C`
  could be left silently inconsistent with its pre-combine sum. Both
  corrections are now applied per circuit, scoped to just the circuit(s)
  `_combine_circuit_devices_safely` actually combined *cleanly* — a circuit
  left uncombined, unmatched, or reported `device.combine_incomplete` is
  never touched. Existing `mismatches[].category` values are reused
  (`device.combine_parameter_corrected`); no new category, no response
  field changes, no `schema_version` bump.
- **Fixed**: `klt extract` no longer writes an unsimulatable `C` card for a
  recognised **capacitor** device class (issue #1558). KLayout's default
  primitive-card writer appends the device class's own name as a trailing
  4th token (`C$1 a b 1.119e-13 cap_mim_1f0_m4m5_noshield`), which ngspice's
  native `C` element parser reads as a required capacitor `.model`
  reference — and no deck's capacitor class name is a real `.model` in any
  PDK model library (gf180mcu's `cap_mim_*_m4m5_noshield`, sky130's
  `sky130_fd_pr__model__cap_mim*`, sg13g2's `cap_cmim`/`rfcmim` are
  `klt`-internal class labels or, at best, *subcircuit* names a `C` card
  cannot reference), so the extracted netlist failed to elaborate at all
  (`unknown parameter (…)` / `could not find a valid modelname`). Unbound
  capacitor classes now write the bare, value-only card (`C$1 a b
  1.119e-13`). Reachable both with `--pdk` (a recognised flavour with no
  curated `pdk_models` table row — e.g. gf180mcu's non-default
  `cap_mim_1f0_m4m5_noshield`/`cap_mim_1f5_m4m5_noshield` densities,
  selected via `--deck-option mim_cap=…`) and without it. The flavour
  identity is unchanged in `devices[].class` and in the netlist's own
  `* device instance …` comment; `--parasitics`' ground/coupling capacitor
  cards, `--pdk`-**bound** capacitor `X` cards, and the `R`/`Q`
  bare-primitive carve-outs (whose trailing token *is* a usable
  consumer-supplied-`.model` reference) are all byte-identical to before.
- **Added**: `klt pex` now accepts `--deck-option <key>=<value>`
  (repeatable) and `--pins <a,b,…>`, passed straight through to the
  `klt extract --parasitics` run it drives internally (issue #1558). Before
  this, a design committed to a non-default deck flavour (gf180mcu's
  `poly_res`/`mim_cap` axes — one drawn geometry, several PDK-offered
  interpretations) was silently extracted against the *deck's* default
  flavour, with no warning, and every `delta[]` row was computed from the
  wrong parasitics; `--pins` is likewise how a caller resolves a
  `pin_count_mismatch` caused by flat extraction promoting more top-level
  pins than the schematic DUT declares. Both are off by default —
  byte-identical to before — and the resolved deck options are echoed in
  the existing `provenance.deck.options` field (no JSON shape change). Both
  flags share `klt extract`'s own parsing/validation helpers, so an
  unrecognised key/value or a malformed entry is the same clean exit-1
  error in either command.
- **Fixed**: `klt extract --check <report> --rerun` (issue #1559) no longer
  reports `status: "drifted"` for extractor-internal bookkeeping that is not
  a stable contract across builds — a permuted `net_id`, an anonymous `$N`
  net-name spelling changing, and/or `parasitics.nets[]` reordering. Both
  the committed and freshly re-run reports are canonicalized before
  diffing: `net_id` is stripped, an anonymous net's spelling is resolved to
  a name derived from its own sorted `<device>.<terminal>` attachment list
  (not the raw counter), and order-sensitive lists are re-sorted by the
  resulting canonical identity. A genuine content change (an R/C value, a
  device/pin count, a connectivity change) still reports `status:
  "drifted"` exactly as before — the fix only normalizes bookkeeping, never
  swallows real drift. `docs/cli/extract.md` gains a "Field classes:
  content, bookkeeping, tool metadata" table naming which report fields
  fall into each class, cross-referenced from `docs/json-contract.md`.
- **Added**: `klt lvs` now accepts `options.combine_devices_per_circuit`
  (issue #1552): a `{"<circuit-name-glob>": <boolean>}` map applied via
  `klayout.db.Circuit.combine_devices()` to each side's own matching
  circuits, before either side's optional `flatten_layout`/
  `flatten_reference` structural flatten runs. It is a per-macro alternative
  to the existing whole-request `options.combine_devices` boolean, for a
  **composed** design (e.g. via `klt gen-compose`) whose own macros were
  each already independently verified under their own — possibly opposite —
  `combine_devices` setting: one already-verified macro needs folding of
  split/interleaved device legs, another needs to stay a literal
  device-for-device compare to avoid issue #1497's silent
  parameter-corruption risk at scale, and no single whole-request boolean
  can satisfy both once composed under a shared top-level rail. Keys are
  `fnmatch`-style glob patterns matched case-sensitively against each side's
  own circuit names (`NetlistSpiceReader` upper-cases names read back from
  SPICE), applied in declaration order (first match wins); a pattern that
  matches no circuit on a side is a new `severity: "warning"`
  `mismatches[].category`, **`combine_devices_per_circuit.unmatched`** —
  never an error, since a pattern legitimately naming a circuit that exists
  on only one side is not itself a mistake. Mutually exclusive with a truthy
  `options.combine_devices` (a clean application error, exit 1); an explicit
  `combine_devices: false` alongside it is a harmless no-op. Echoed under
  the new `options.combine_devices_per_circuit` response field (`null` when
  omitted) and reconstructed by `--check --rerun`. No `schema_version` bump
  — both the request field and the response field are additive. See
  `docs/cli/lvs.md`'s "Composing macros with opposing `combine_devices`
  needs" section for the full worked example.
- **Fixed**: `klt gen res_array` no longer draws a 1dbu-short (219nm) end
  contact when `length_um` puts the contact centre on an exact half-dbu grid
  tie — e.g. `length_um=1.4965`, which tripped gf180mcu's 220nm
  `contact.width.1` minimum with no signal to the caller (issue #1551). The
  unit resistor's two end-contact boxes were built from raw `cx ±
  CONTACT_SIZE_UM / 2` floats whose opposite edges were then rounded to the
  dbu grid *independently*, so floating-point noise could resolve one edge up
  and the other down (the same failure mode diagnosed for `guard_ring` in
  issue #685). They now go through the existing `_snap_square_box_um` helper,
  which snaps the centre to the grid before deriving the edges. No response
  field changes and no `schema_version` bump; drawn geometry is unchanged at
  every `length_um` that is not on a tie.
- **Added**: `klt gen res_array` now exposes gf180mcu's high-sheet-rho
  poly-resistor flavours, not just the base `ppolyf_u` (issue #1550):
  `params.flavor` accepts `"1k"` / `"2k"` / `"3k"` alongside the existing
  `"generic"` default. All three new names draw *identical* geometry — `SAB`
  `(49, 0)` plus the `Resistor` high-sheet-rho marker `(62, 0)` over the
  `RES_MK` body, with no `Pplus` — matching `ppolyf_u_1k`'s (and its `_2k`/
  `_3k` siblings') own `requires` set in
  `klayout_tools.decks.gf180mcu.EXTRACTION_DECK.resistors` exactly; the three
  device classes are distinguished only by which `poly_res` value `klt
  extract --deck-option poly_res=<1k|2k|3k>` selects at extraction time
  (1000/2000/3000 Ω/□ respectively), not by any drawn layer. Before this
  change, a `res_array` cell meant to model a real design's `ppolyf_u_1k`
  resistor extracted as the base `ppolyf_u` class at ~350 Ω/□ instead of
  1000 Ω/□ — a >2.5x resistance-recognition error with no drawing-side
  parameter to fix it. gf180mcu's existing default `"generic"` flavour is
  byte-identical to before. No `schema_version` bump — no field changed
  shape, and no request field changed meaning.
- **Added**: `klt extract`'s `nets[]` entries now carry `net_id`,
  `pin_index`, and `label_positions_um` (issue #1540) — all additive,
  no `schema_version` bump. A flat extraction of a block that instantiates
  the same leaf cell several times in a chain (e.g. a ring of identical
  2-input stages) collides several genuinely distinct nets onto one shared
  `name` (KLayout's own `NetlistSpiceWriter` disambiguates them at write
  time with a `$1`/`$2`/... suffix this tool does not control), and neither
  `name` nor the existing `pin` boolean can tell which collided entry is
  which. `net_id` is the net's own KLayout `cluster_id` (unique per net
  object, the same identity `parasitics.nets[].net_id` already uses for
  issue #765/#811's identical "several distinct nets, one label" shape);
  `pin_index` is that net's exact 0-based position in the written
  `.SUBCKT`/instance-line port order when it is a promoted pin (`null`
  otherwise), letting a caller resolve a specific `.SUBCKT` port index
  straight back to a `nets[]` entry with no separate `klt lvs` run against a
  reference schematic; `label_positions_um` reports every drawn label's own
  `(x_um, y_um)` position naming that net, so a caller with independent
  floorplan knowledge (a known pin location, e.g. from `klt
  place-and-route`'s DEF) can positively identify which collided entry is
  the one it means. Also fixes `_purge_preserving_named_nets` to restore a
  device-free, pinned net by its own `cluster_id` rather than by `name`,
  so two distinct same-named survivors on one circuit no longer silently
  collapse onto a single recreated net (the identical collision shape at a
  smaller scale). See `docs/cli/extract.md`'s "Net-name collisions from
  internally-repeated sub-cells" section for a worked example.

- **Added**: `klt gen --list-pdk-pcells` and `klt gen --pdk-pcell
  <library>/<cell>` reach the PCell library a resolved PDK ships *itself*
  under `libs.tech/klayout/python/`, instead of only klt's own built-in
  generators (issue #1535). `--list-pdk-pcells` enumerates each vendor
  library's registered name, source package, cells, and every cell's
  parameter spec (name/type/default/description/`hidden`/`settable`);
  `--pdk-pcell` instantiates one and writes GDS through the same response
  envelope a built-in generator emits, plus an additive `pdk_pcell`
  `{library, cell, package}` object. klt is a thin passthrough here per
  `docs/ARCHITECTURE.md`'s "wrap the proven engine" rule: it puts the PDK's
  own `python/` directory on `sys.path`, imports the vendor package, lets the
  vendor's own `pya.Library` register itself under the vendor's own name, and
  drives KLayout's own `Layout.add_pcell_variant()` against the vendor's own
  declaration — the same sequence the PDK's own KLayout autoload macro
  performs. A vendor PCell is instantiated as an **opaque** cell
  (`device_count` is always 1, `ports` always `[]`), because interpreting
  vendor geometry to infer devices or pins would be exactly the
  hand-transcription step this feature removes. A package that needs a
  third-party compat layer klt has no dependency on is *reported*, never
  vendored or reimplemented: `--list-pdk-pcells` lists it under `unavailable`
  with the missing module named (and still reports every library that did
  load), while `--pdk-pcell` fails as an application error (exit 1) naming
  the same module, never a traceback. Both real installs available at the
  time this landed hit that path — sky130A's `cells` is a plain-`pya`
  library (not Cadence-DLO/`cni`-based) but reaches `import gdsfactory`,
  and ihp-sg13g2's chain reaches `from cni.tech import Tech`. See
  `docs/cli/gen.md`'s "PDK-shipped PCells" section.

- **Added**: `klt pdk find` and `klt pdk list` report a `has_pcell_library`
  boolean per resolved variant (issue #1535), so a caller can discover that a
  PDK ships its own KLayout PyCell library without invoking `klt gen
  --list-pdk-pcells`. Additive field; it reports only that a Python package is
  present under `libs.tech/klayout/python/`, never that it imports in the
  current environment. See `docs/cli/pdk.md`.

- **Added**: `klt gen-compose`'s `connectivity[].legs[]` field lets a caller
  hand-route individual legs of a bundle (>2-pin) net by name (issue #1529).
  `waypoints_um` steers a 2-pin net's single backbone but was rejected
  outright on any net with more than two pins, since a bundle net's spanning
  tree has no single unambiguous leg for a caller-supplied path to belong
  to — the only workaround was decomposing the net into several 2-pin
  `connectivity[]` entries sharing one `net` name, which then had to stay
  pin-adjacent (each entry sharing a literal pin with the next) to avoid the
  route-vs-route collision check's accepted-leg exemption — keyed on sharing
  a pin, not a `net` name — rejecting two legs of the *same* net as if they
  were a short between different nets. `legs[]` is an array of
  `{from_pin, to_pin, waypoints_um}` objects, each naming one leg of the
  entry's own net (`from_pin`/`to_pin` must match two of that entry's own
  `pins[]`); every named leg is routed through the same
  routability-check path as any other leg (nothing is exempted, only
  steered) and seeded into the spanning tree before the automatic
  nearest-first search runs, so any pin not named in `legs[]` still
  completes automatically. Because every leg in one `legs[]` array belongs
  to the same net, they are never compared against each other by the
  route-vs-route collision check, so non-adjacent legs of one bundle net
  route cleanly with no pin-adjacency ordering required. Mutually exclusive
  with the top-level `waypoints_um` on the same entry (an application
  error, exit 1, if both are supplied). A named leg the router *rejects*
  (its path crosses an unrelated block, or collides with an already-routed
  net) does not fail the net by itself — the automatic search still runs —
  but, unlike a rejected auto-selected candidate, it stays in
  `nets[].legs[]` with `routed: false` and its own `reason` even when the
  net comes back `status: "routed"`, so the automatic search can never
  silently re-route around the caller's own steering. See
  `docs/cli/gen-compose.md`'s "Hand-routing individual legs of a bundle net
  with `legs[]`" section.

- **Fixed**: `klt gen-compose` now rejects an inter-block leg whose escape
  from a coordinate-tapped `blocks[].cell` port would draw a silent short to
  a *different* net already present inside that same block, instead of
  silently drawing it (issue #1527). A `blocks[].cell` block (an existing
  GDS/OASIS stream this command did not generate) publishes no `ports[]` of
  its own — a caller taps a net by coordinate, hand-declaring a port
  directly on one of the block's own internal wires. `route_two_pin()`
  already models the block a leg starts/ends in by its `bbox_um` only, with
  an unavoidable margin (`_port_edge_margin_um`) exempting the port's own
  approach stub from that check — so the one region a coordinate-tapped
  leg is guaranteed to draw metal in (its own approach stub) was the one
  region with no obstacle model, and an escape direction that happened to
  run across a different net's metal already drawn inside the same block
  composed `routed: true` and DRC-clean (two overlapping shapes on one layer
  merge into one polygon — a short, not a spacing violation, so no rule deck
  can see it) while `klt extract` silently merged the two nets. This is now
  caught the same way the existing route-vs-route check (#1057) is: the leg
  is compared against its own block's other drawn shapes on the route layer
  (excluding only the shape its own port lands on), and rejected into
  `unrouted_nets[]` with a `legs[].reason` naming the block instead of being
  drawn. Scoped to `blocks[].cell` endpoints only — a `generator_report`
  block (e.g. `mos_array`'s own unreported `dummy` matching columns,
  suppressed from `klt extract`'s netlist by convention) can legitimately
  draw real geometry this check cannot tell apart from an obstacle, so it is
  left to the existing whole-block bbox check. See `docs/cli/gen-compose.md`'s
  new bullet under "Known limitations", and the corrected "Geometry is
  advisory" note on what `unrouted_nets: []` plus a clean `klt drc` does and
  does not prove for a coordinate-tapped composition.

- **Fixed**: `klt pex` no longer fails every extracted-side corner with
  "Could not find include file" when `-o`/`--output` and/or `--outdir` are
  given as *relative* paths (issue #1525). The extracted-side testbench
  `klt pex` generates re-points its `.include` line at the freshly-extracted
  netlist; that line named the caller's `-o` value verbatim, but each `klt
  sim` corner runs `ngspice -b` from a corner-scoped working directory
  nested under `--outdir`, which resolves a relative `.include` against
  *that* directory, not the invocation's own cwd — so a relative `-o` that
  was perfectly valid from the caller's cwd resolved to nothing there. The
  extracted netlist path is now resolved to absolute internally before it is
  written into the generated testbench, regardless of how `-o`/`--outdir`
  were spelled; this run's own JSON report continues to render `netlist` as
  a repo-relative `{path, scope}` object (issue #1261), so the fix does not
  leak an absolute path into committed evidence. See "The DUT `.include`
  swap" in `docs/cli/pex.md`.

- **Fixed**: `klt gen-compose` now rejects a via-drop landing pad (or
  stub-widen box) that would DRC-violate against its *own* placed block's
  drawn geometry, instead of silently drawing it (issue #1520). A
  `blocks[].cell` block (an existing GDS stream this command did not
  generate) is modelled by its `bbox_um` plus its declared `ports[]` —
  hand-declared, since a pre-existing stream never reports its own ports.
  Declaring a port on an internal wire is legitimate, but the fixed-size
  landing pad `gen-compose` draws there (independent of `width_um`) could
  land close enough to a *different* part of that same wire (e.g. a
  perpendicular leg near a corner) to violate the resolved deck's own
  same-layer minimum-spacing rule — a real `klt drc` finding even though
  both shapes are the same electrical node, since a rule-deck spacing check
  is net-agnostic. This is now caught via `kdb.Region.notch_check` (a
  same-polygon self-space check, as opposed to the existing route-vs-route
  overlap/inflate check, which cannot see it: the pad merges with the wire
  it lands on into one polygon, so there is no separate shape to compare
  against) before anything is drawn — the net is reported **unroutable**
  (`unrouted_nets[]`, `routed: false`, a `legs[].reason` entry naming the
  block and the violated rule id) rather than composing `routed: true`
  with the violation left for a later `klt drc` run to discover. See
  `docs/cli/gen-compose.md`'s new bullet under "Known limitations".

- **Added**: `klt mom --touchstone <path>` writes a standard 2-port
  Touchstone (`.s2p`) file from the de-embedded S-parameters
  `report["full_wave_sweep"][i]["s_parameters"]` already computes (issue
  #1518) — the interchange format SPICE-family tools (ngspice, Xyce, ADS,
  …) expect for N-port network data. Follows Touchstone Rev 1.1 (`# HZ S RI
  R <z0>` option line, `S11 S21 S12 S22`-per-line data order, no lossy
  magnitude/phase round-trip). Raises a clear `MomError` (exit code 1) if
  the report has no S-parameters (`ports` unset in the spec file) or if the
  two ports' `reference_impedance_ohm` differ — Touchstone v1.1's single
  scalar `R <z0>` cannot represent two different real reference
  impedances, and this writer never silently averages them or emits the
  inconsistently-supported Touchstone v2.0 `[Reference]` syntax. See
  `docs/cli/mom.md`'s "Exporting Touchstone (`.s2p`)" section.

- **Fixed**: `klt gen-compose` no longer hard-refuses composing blocks whose
  GDS inputs disagree on `dbu`, as long as every disagreement is an exact
  integer ratio (issue #1514, a regression from #1512). `klt gen`'s output
  dbu is now resolved from the target PDK's own tech LEF `DATABASE MICRONS`
  (#1512) — `0.0005um` for a gf180mcu-family PDK instead of the prior fixed
  `0.001um` — but `klt draw` (deliberately PDK-unaware, #230) and any GDS
  from an older `klt gen` build still always write `0.001um`, so composing
  either against a freshly-generated gf180mcu-family `klt gen` block used to
  hit the same `"which does not match the composed cell's dbu"` error
  #1496 fixed for the opposite pairing. `gen-compose` now resolves the
  composed layout's own dbu as the *finest* dbu among all blocks, and
  rescales every coarser block's geometry (and any internal hierarchy or
  array pitch) losslessly onto that grid via a `kdb.ICplxTrans` integer
  magnification — the same rescale mechanics `klt place-and-route`'s
  `_merge_gds_view` already uses for the analogous DEF/LEF-merge problem
  (#1090). Composing blocks whose dbus differ by a *non*-integer ratio (a
  genuine cross-PDK composition mistake, not a reconcilable grid mismatch)
  still raises `GenComposeError`. Each rescaled block adds one entry to the
  response's `warnings[]` naming the block, its original dbu, and the
  composed dbu it was rescaled onto — additive field usage only, no
  `schema_version` bump.

- **Added**: `klt extract --pin-source-cells CELL[,CELL...]` (issue #1513) is
  a third, *positional* declared-pin mechanism for composing several
  already-independently-verified blocks — at least one of them a
  placed-and-routed macro with generic internal pin labels (`A`, `X`, `Q`,
  `Y`, ...) — into one flat top-level layout via `klt gen-compose` plus
  hand-drawn interconnect, where the composition itself has no single
  governing DEF to anchor `--def-pins` on. `--top-cell-pins` cannot help
  (a composition's own hand-drawn interconnect labels necessarily live in
  an *instanced* sub-cell, not literally in the new top cell, so
  `--top-cell-pins` demotes them right alongside the genuine internal
  noise it targets); `--pins`/`--def-pins` match by string, so two
  independently-labelled macros that coincidentally share a generic pin
  name (e.g. both use `CLK` internally) both stay promoted once that
  string is declared, with no way to keep only the intended one.
  `--pin-source-cells` resolves each drawn pin-name label found anywhere
  under the top cell whose immediate owning cell is one of the named
  cells to its actual extracted net by probing that label's own
  composed-frame position, rather than by matching its text — demoting
  every currently-promoted pin not reached this way, exactly as
  `--pins`/`--def-pins` demote on a miss. Applied after `--pins`'s and
  `--def-pins`'s own reconciliations (when given), so it can only further
  restrict. Off by default (omitting the flag is byte-identical to
  extraction before this feature existed). `klt lvs` exposes the same
  control as the `layout.pin_source_cells` request field, mirroring
  `layout.declared_pins`/`layout.top_cell_pins`. See
  `docs/cli/extract.md`'s "Pin-source cells" section. The same pass also
  documents (in `docs/cli/extract.md`'s "Declared pin set" section) that a
  `--format json`-reported `nets[].name` (`|`-joined by
  `spice_safe_net_name`) must not be pasted directly into `--pins`, whose
  own matching reads the promoted net's `,`-joined internal name instead.

- **Fixed**: `klt extract --parasitics`'s substrate DC-tie (issue #1263) now
  also declares its tied net(s) SPICE-**global** (issue #1503), closing the
  gap where an `X`-instantiated testbench's own `vsubs` (or `vsubs_iso<n>`)
  node was electrically disconnected from the extracted subcircuit's
  internal one -- the tie kept the instance's own node from floating (no
  more singular-matrix error), but the parasitic ground-capacitance model it
  anchors was silently computed against a node no testbench could reach or
  drive. A `.GLOBAL <net> ...` card is now written once, before the first
  `.SUBCKT`, for every synthesized substrate identity the extraction
  produced (the deck-wide `substrate_net` and each `_iso<n>` variant, issue
  #1128); the pin interface, `pin_count`, and every other `parasitics` count
  stay byte-identical (`klt pex`'s `pin_count_mismatch`/`flat_dut_mismatch`
  diagnostics, issue #1258, are unaffected). Additive JSON field:
  `parasitics.substrate_dc_tie.node_scope` (always `"global"`), so a caller
  can detect the node-scoping guarantee without reading the generated SPICE.
  See `docs/cli/extract.md`'s "Substrate DC reference" section.

- **Added**: `klt lvs`'s `options.combine_devices: true` now checks every
  combined capacitor device's `C` parameter against the pre-combine sum of
  the parallel group KLayout's own `Netlist.combine_devices()` folded it
  from, and corrects it in place when the two disagree (issue #1497). A
  parallel capacitor's `C` is mathematically a simple per-device sum — the
  same rule `klt extract`'s `C = area_cap_f_um2 * A + perim_cap_f_um * P`
  deck formula relies on — so the total is a conserved quantity combining can
  only redistribute, never change; a reported observation (10/10 repeat
  calls against one real ~1000-device/~20-group extracted netlist) found
  KLayout's own combine sometimes leaving `C` at a single pre-combine
  instance's own value instead of the group's summed total, while the same
  group's secondary `A`/`P` parameters combined correctly — with no
  exception raised and no existing `device.combine_incomplete` warning to
  catch it (that category only covers the unrelated #1185/#466
  `RuntimeError`-on-partial-match failure mode, which never fires here).
  Neither that report's own reduction attempt nor a follow-up investigation
  here could force a reliable reproduction from a from-scratch synthetic
  netlist at a comparable scale, so this check is applied unconditionally as
  a defensive invariant rather than gated behind a confirmed repro. **New
  `mismatches[].category`**: `device.combine_parameter_corrected`, always
  `severity: "warning"` (never changes `status`), naming every corrected
  device and its before/after `C` values — see `docs/cli/lvs.md`'s
  `device.combine_parameter_corrected` section. Independent of the existing
  `device.combine_incomplete`/`options.combine_devices_max_attempts` retry
  mitigation, which is unaffected.

- **Fixed**: `klt gen compose`'s router now validates `routing.width_um` and
  sizes its via-drop squares against the **resolved PDK deck's own
  minimum-width DRC rules** — the same `ExtractionDeck`/`DrcRule` set `klt
  drc` judges the composed layout with — instead of the documented `0.17um`
  default and the PDK-independent `_VIA_DROP_SIZE_UM` (`0.22um`) constant
  (issue #1501). On a family whose minimums exceed those values (e.g.
  gf180mcu's `metal1.width.1` = `0.23um`, `via1.width.1` = `0.26um`), `compose`
  previously drew guaranteed-illegal geometry with no error or warning at
  generation time — 172 `metal1.width.1` violations on the documented default,
  284 `via1.width.1` violations on every `metal2` route's via-drops, per the
  issue's own measured reproduction. `compose` now rejects a `routing.width_um`
  below the resolved deck's own minimum for the requested layer (and any
  `cross_block_layer_role` fallback layer), naming the violated rule and its
  threshold, and sizes each via-drop's square to `max(_VIA_DROP_SIZE_UM,
  <deck's own via-layer width floor>)` per distinct via layer actually drawn.
  A PDK family whose deck cannot be resolved, or a layer with no matching
  `"width"` rule, keeps the prior constants unchanged. No `schema_version`
  bump — `compose`'s success-path payload shape is unchanged; the new failure
  mode reuses the existing `GenComposeError` application-error path.

- **Fixed**: `klt gen` now writes its output GDS at the **resolved PDK's own
  database unit**, read from that PDK's tech LEF `DATABASE MICRONS`
  declaration, instead of a fixed `0.001um` regardless of PDK (issue #1496).
  For gf180mcu (`DATABASE MICRONS 2000`) a generated cell is now written at
  `0.0005um` — the same dbu `klt place-and-route`'s DEF→GDS merge already
  derived from the same tech LEF — so `klt gen`-family blocks and
  `klt place-and-route`-family macros built for the same PDK can finally be
  placed in one `klt gen-compose` request, which requires a single shared dbu
  across every block and previously refused the mix outright with
  `has dbu=0.0005, which does not match the composed cell's dbu=0.001`. A PDK
  whose tech LEF is missing, unreadable, or declares no `DATABASE MICRONS`
  falls back to `0.001um`, so no request that previously succeeded can now
  fail; sky130 (`DATABASE MICRONS 1000`) output is unchanged. A finer dbu
  changes only the integer grid the shapes are stored on — every generator's
  documented default `params` stays `klt drc --deck gf180mcu` clean at
  `0.0005um` and its drawn micrometre geometry is identical. Composing blocks
  built against genuinely *different* PDKs still raises the dbu-mismatch
  error: that is a real composition mistake, not a tooling artefact.

- **Added**: `klt gen` and `klt gen-compose` responses now carry `dbu_um`, the
  database unit their output stream was written at, so a caller can confirm
  two blocks agree before composing them without a separate `klt stats`
  round-trip (issue #1496). Additive field on both commands — no
  `schema_version` bump, per [`docs/json-contract.md`](docs/json-contract.md).

- **Documented**: `klt gen diff_pair` places both legs at the **same x column
  per terminal** — the common-centroid checkerboard puts one `Q1` and one
  `Q2` sub-instance in every column at the same `x0`, so `Q1_<n>_S`/`_D`/`_G`
  and that column's `Q2_*` ports report identical `x_um` and differ only in
  `y_um`. Previously a caller only discovered this by reading `gen.py` or by
  hitting the routing conflict: a one-column-per-pin floorplan (one
  horizontal track per net, one vertical routing column per pin) cannot give
  the two legs' distinct nets separate columns (issue #1495). The caveat, why
  it is inherent to the interleave rather than a fixable oversight, and the
  routing styles that do work are now in `docs/cli/gen.md`'s `diff_pair`
  section, and surfaced in `klt gen --list` (the `diff_pair` `summary` and its
  `splits` param `description` — both are free-text fields, so this is not a
  schema change). No geometry change: every generated cell is byte-for-byte
  what it was.

- **Fixed**: `klt gen cap_array`'s reported `C<i>_TOP` port now lands on a
  routable escape pad instead of the unit cell's own interior centre (issue
  #1494). The bottom plate spans almost the entire unit footprint (only
  `CAP_BOTTOM_PLATE_MARGIN_UM`, 0.5um, clear on each side), so the old centre
  position sat directly over it -- a caller stepping a via stack down from
  there to route on lower metal had no way to avoid landing on the bottom
  plate's own sheet, silently merging the two plate nets (invisible to DRC,
  since nothing about it violates a same-layer spacing/width rule; only
  extraction would report the merge, with the real top-plate net left
  anonymous). `_cap_unit_layout` now draws a same-layer stub running north
  from the original via-landing pad, clear past the bottom plate's own top
  edge, ending in a second pad whose own bounding box no longer touches --
  let alone overlaps -- the bottom plate; `C<i>_TOP` is reported there
  instead, with a geometrically real `direction_deg` (due north) rather than
  the previous fixed placeholder. Both currently-supported families (sky130,
  sg13g2) draw this escape by default -- no opt-in param, matching this
  repo's existing `ring_gap_side` precedent of not gating correct behavior
  behind a flag. A hypothetical family with plates but no top-plate-via-metal
  layer (not exercised by any currently-supported family) keeps the old,
  unroutable centre position and gains a `drc_hints.notes` warning instead.
  No `schema_version` bump -- no field changed shape, only the `C<i>_TOP`
  port's `x_um`/`y_um`/`direction_deg` *values*.

- **Fixed**: `klt lvs`'s `reference.form: "subckt-call"` conversion no longer
  silently mis-scales a *bare* (unsuffixed, non-exponent) `L=`/`W=` literal
  on a device subcircuit call by `1e6` for a deck whose real schematic-flow
  netlists carry an ambient SPICE `.option scale=1.0u` (issue #1492).
  `normalize_reference_netlist` previously always read a bare literal as SI
  metres, so a real sky130 xschem/ngspice netlist's `L=0.15` (meaning `0.15`
  um under sky130's own `.option scale=1.0u` convention, confirmed against a
  real fetched `open_pdks` install) converted to `L=150000U` — a plausible-
  looking but wildly wrong device geometry that produced an indistinguishable
  `device.unmatched`/`net.unmatched` avalanche with no diagnostic pointing at
  units. A bare literal is now resolved per `reference.deck`'s own
  `klayout_tools.pdk_models.geometry_style_for_family` convention: sky130
  reads it as already-micrometres; `gf180mcu`/`sg13g2`/`sg13cmos5l` (which
  set no ambient scale) are unchanged, still SI metres. An explicit unit
  suffix (`L=0.15u`) or exponent (`L=0.15e-6`) was already unambiguous and is
  unaffected either way. With no `reference.deck` given, a bare literal is
  now a loud `NormalizeError` (surfacing as the usual application-level `klt
  lvs` error) naming the offending device/parameter/value, instead of a
  silent metres assumption.

- **Fixed**: `klt extract --def-net-names` now recovers the real DEF net name
  for an **unrouted single-pin** net — a tie-cell output, or any
  synthesis-inserted constant driver whose net has one instance pin and
  nothing to route to (issue #1488). KLayout's LEF/DEF reader stamps its
  net-name shape property only onto the routed geometry it draws in the top
  cell, so such a net had no name carrier anywhere and extracted as a
  synthesized `$<id>`; several structurally identical ones in a design are
  exactly what makes a downstream `klt lvs` run report an ambiguous-pairing
  `topology` warning per net. `klt place-and-route`'s DEF→GDS merge now
  synthesizes the missing carrier from the net's own pin geometry (the DEF
  `COMPONENTS` instance name KLayout records on each placement, the LEF
  `PIN`/`PORT` rectangle, and the instance's placement transform), as a 2 dbu
  marker shape drawn *inside* the already-drawn conductor — a geometric no-op
  for DRC/LVS/extraction, needing no change to what `--def-net-names` reads.
  Nets a routed-metal shape already names are untouched, so issue #951's
  behaviour is unchanged. **Additive response field** on `klt
  place-and-route`: `def_net_names` (`single_pin_markers`,
  `unresolved_single_pin_nets`), `null` unless `stage_reached` is `"route"`,
  mirroring `layer_map`. A single-pin net whose pin geometry cannot be
  resolved (no open_pdks layer-map file, an undeclared LEF macro/pin, a pin
  centre not covered by drawn conductor) is named in that field and keeps the
  previous `$<id>` fallback rather than failing the merge.
- **Added**: `klt gen mos_array` gains `add_guard_ring` (default `false`,
  preserving byte-for-byte existing geometry) plus `ring_gap_side`/
  `ring_gap_um`/`ring_gap_offset_um`/`ring_padding_um` (issue #1493),
  matching `diff_pair`'s existing param set and composing the same
  `_ring_layout`/ring-gap machinery (#434). This closes the "guard/tap ring
  around a matched device group" idiom `mos_array` — unlike `diff_pair`/
  `esd_device`/`bjt_array` — previously had no way to draw for itself: a
  caller needed a separately generated `guard_ring` block placed inside the
  array's cavity, which `klt gen-compose` could never route to (every
  backbone segment crossing the enclosing block's bbox was rejected as a
  collision, with no way to express "this block encloses that one"). With
  the ring composed directly into the array's own geometry, reporting its
  own `TAP_*`/`GAP_*` ports, a route from the array's own device port to its
  own ring's tap port is an ordinary same-block self-net — already covered
  by the existing pin-block edge-margin allowance, so no `gen-compose`
  change was needed. Deferred on `sg13cmos5l` (no `"tap"` role in that
  family's curated deck) *only* when a request actually sets
  `add_guard_ring: true` — every other `mos_array` request on that family is
  unaffected. A general `gen-compose`-level declared-enclosure relationship
  for a ring around a *separately*-generated block remains a non-goal for
  this issue.

### Merged since 0.4.0, by commit type

Every commit on `main` in `v0.4.0..v0.5.0` (235 commits), grouped by its
conventional-commit prefix; the trailing `(#N)` is the squash-merged PR.
The narrative entries above cite the originating issue where one exists.

#### Added

- feat(sta): from-scratch netlist input mode for klt sta (issue #1825) (#1832)
- feat(place-and-route,sta): pre-route timing checkpoint (nominal hold slack + unrouted DEF) (#1831)
- feat(wave): port VCD ingest and implement klt wave build (#1607)
- feat(wave): port bwave FST query engine, implement klt-wave query (#1602)
- feat(design-agent): device-level sky130 relaxation oscillator, 18/18 corners (#1822)
- feat(synthesize,place-and-route): add sg13g2_stdcell platform-table entries (#1791)
- feat(benchmarks): swap design-agent reference solutions to real sky130 devices (#1782)
- feat(synthesize): generate prefix adders and pick one by measured synthesis (#1772)
- feat(gen): populate res_array metal_level for gf180mcu rm1/rm2/rm3 (#1774)
- feat(gen): populate res_array metal_level for sg13g2 res_metal1/res_metal2 (#1769)
- feat(benchmarks): add charge-pump-pll hard-tier design-agent task (#1767)
- feat(benchmarks): add tool-using interactive design-agent provider (#1763)
- feat(loom): propagate a held PR's loom:operator onto its linked issue(s) (#1762)
- feat(sim): add `klt sim --plot` waveform SVG rendering (issue #1723) (#1757)
- feat(benchmarks): add hard-tier voltage-controlled relaxation oscillator (VCO) design-agent task (#1753)
- feat(benchmarks): add medium-tier cascode, integrator and Schmitt trigger tasks (#1750)
- feat(benchmarks): add medium-tier design-agent tasks (5T OTA + two-stage Miller OTA) (#1746)
- feat(benchmarks): add hard-tier RC relaxation oscillator design-agent task (#1735) (#1745)
- feat(kb): nightly CI drift-check that re-runs measured testbenches (#1742)
- feat(benchmarks): add live-agent candidate provider to design-agent benchmark (#1732) (#1740)
- feat(sim): add per-device operating-point sanity lint (klt sim --op-lint, eval op-sanity gate) (#1730)
- feat(benchmarks): add design-agent benchmark harness (schema + runner + easy-tier tasks) (#1729)
- feat(gen-compose): add gf180mcu metal4/via3 routing roles (#1670) (#1675)
- feat(extract): add devices[].instance_path, attributing each device to its originating GDS instance (#1672)
- feat(extract): recognise gf180mcu's drawn metal-resistor family (issue #1640) (#1662)
- feat(drc): add isolated check kind, fix nwell.space.1 notch false positive (#1661)
- feat(gen): add metal_level param to res_array for sky130 res_generic_mN (#1639) (#1660)
- feat(gen-compose): decouple cross_block_layer_role's width from routing.width_um (#1620) (#1648)
- feat(extract): recognise sky130's drawn metal-resistor family (issue #1621) (#1641)
- feat(pdk): provision SG13G2 ngspice/OSDI sim toolchain (issue #1628) (#1644)
- feat(synthesize): report static leakage power (issue #1626) (#1635)
- feat(sta): add hold-side worst/negative slack fields to `klt sta` (#1625) (#1634)
- feat(pdk): curate a gf180mcu cross-section table for klt pdk stackup (#1629)
- feat(pdk): add `klt pdk stackup` — PDK cross-section for field solvers (#1609) (#1615)
- feat(clip): add `klt clip` to write a bbox region or named cell out as its own stream (#1618)
- feat(pdk): surface PDK PCell importability probe via `klt pdk pcell-check` (#1611)
- feat(functional-verification): add --mutations mutation testing (issue #1592) (#1604)
- feat(docker): add eda-sim overlay image on the loom-worker base (#1584)
- feat(gen-compose): support multi-level via-drop ladders (#1567) (#1570)
- feat(gen): add gf180mcu MiM-capacitor support to cap_array (#1561)
- feat(lvs): add options.combine_devices_per_circuit for composed macros (#1556)
- feat(gen): expose gf180mcu high-sheet-rho poly-resistor flavours in res_array (#1553)
- feat(extract): disclose positional pin/net identity for internally-repeated sub-cell name collisions (#1540) (#1543)
- feat(gen): instantiate a PDK's own shipped PCell library via --list-pdk-pcells/--pdk-pcell (#1538)
- feat(gen-compose): steer individual legs of a bundle net with connectivity[].legs[] (#1529) (#1536)
- feat(mom): add --touchstone (.s2p) export for two-port S-parameters (#1521)
- feat(extract): add --pin-source-cells for gen-compose'd multi-macro pin declaration (#1515)
- feat(gen): add add_guard_ring to mos_array (issue #1493) (#1499)

#### Fixed

- fix(buildgate): skip real ngspice/openroad tiers locally, raise timeout (#1651) (#1659)
- fix(pdk): resolve IHP sg13g2_stdcell's naming convention in synthesize/place-and-route liberty/LEF resolvers (#1796)
- fix(dep-recheck): recognize curator:named-dependency markers in idempotency check (#1788)
- fix(synthesize): bound _run_yosys with a timeout to avoid indefinite hangs (#1777)
- fix(equiv): apply WASI-sandboxed-yosys hint to equiv.py too (#1755) (#1768)
- fix(sim): parse_ascii_rawfile handles ac/sp Flags: complex rawfiles (#1761)
- fix: scope champion-issue-promo tier backlog counts to loom:issue only (#1716)
- fix(extract): recover DEF net names one level below a composed macro's top (#1696)
- fix(decks): add gf180mcu metal4.width.1 / metal4.space.1 (#1688) (#1692)
- fix(extract): --pins matches any component label of a multi-label net (#1690)
- fix(gen-compose): warn when a block's declared bbox_um understates its real geometry (#1679) (#1684)
- fix(gen-compose): retry route-vs-route collision onto cross_block_layer_role (#1683)
- fix(gen-compose): exempt a hollow block's cavity from the obstacle check (#1682)
- fix(gen-compose): scope obstacle-overlap check to layers a block actually draws (#1678)
- fix(loom): guard against inline-code-quoted closing keywords in PR bodies (#1677)
- fix(loom): match #N anywhere on named-dependency checklist lines (#1669) (#1671)
- fix(lvs): prune power-only layout circuits before gate-level-verilog compare (#1663)
- fix(lvs): compare circuits by identity in _prune_extra_top_circuits (#1664)
- fix(ci): make ci-apt-install.sh's mirror rewrite adaptive, not one-directional (#1665) (#1668)
- fix(place-and-route): add gf180mcu_fd_sc_mcu7t5v0 table entries (#1653)
- fix(lef-abstract): search every GDS datatype sharing a pin's LEF layer name (#1636)
- fix(sta): verify SPEF annotation after read_spef, not just net names (#1647)
- fix(lef-abstract): emit non-rectangular OBS polygons instead of crashing (#1642)
- fix(pdk-pcell): reach ihp-sg13g2's cni compat shim via sys.path hint + tkinter workaround (#1638)
- fix(extract): make SPEF *DATE header a fixed placeholder, not wall-clock (#1631)
- fix: unescape DEF identifiers in extract's --def-net-connections (#1632)
- fix(gen): cover gf180mcu guard/tap/collector-ring Comp shapes with implant (#1580) (#1583)
- fix(components): scope --label-layers texts to their own conductor (#1582)
- fix(gen): close mos_array's gf180mcu unit-device signoff-DRC gap (#1577) (#1581)
- fix(dep-recheck): extract Dependencies section by last match, any heading level (#1571)
- fix(extract): normalize bookkeeping fields before --check --rerun diff (#1562)
- fix: write bare C cards and add klt pex --deck-option/--pins passthrough (#1564)
- fix(lvs): carry #559/#1497 resistor-offset and capacitor-C corrections into combine_devices_per_circuit (#1560)
- fix(gen): snap res_array end contacts to the dbu grid before deriving edges (#1554)
- fix(gen-compose): reject unrecognized connectivity[]/legs[]/endpoint keys (#1549)
- fix(loom): restore FORMULA_VERSION guard reverted by resync (#1546) (#1547)
- fix(loom): add FORMULA_VERSION guard to dep-recheck-fingerprint.sh (#1544) (#1545)
- fix(loom): repair curator.md's dep-recheck-fingerprint.sh recipe after CLI resync (#1542)
- fix(lvs): scope net-mismatch merge/split classification per weakly-connected component (#1539)
- fix(curator): derive dep-recheck CONCLUSION_HASH from canonical state, not prose (#1534)
- fix(gen-compose): reject inter-block legs that short to their own block's other metal (#1527) (#1530)
- fix(pex): resolve extracted-side .include path to absolute before writing testbench (#1526)
- fix(curator): add regression guard for dep-recheck idempotency violations (#1523) (#1524)
- fix(gen-compose): reject via-drop pads that self-notch their own block's wire (#1522)
- fix(gen-compose): reconcile integer-ratio dbu mismatches instead of refusing (#1516)
- fix(gen): resolve output dbu from the PDK's tech LEF DATABASE MICRONS (#1512)
- fix(extract): declare substrate DC-tie nets SPICE-global so instantiated testbenches share the node (#1503) (#1511)
- fix(pnr): skip clock_tree_synthesis for a zero-fanout clock instead of segfaulting (#1506) (#1510)
- fix(lvs): correct capacitor C left unmerged by combine_devices() (#1497) (#1508)
- fix(layout-plan): forward routing.cross_block_layer_role instead of silently dropping it (#1509)
- fix(gen): floor routing width and via-drop size at the resolved deck's own DRC minimums (#1507)
- fix(lvs): resolve bare L=/W= geometry literals per deck's .option scale convention (#1505)
- fix(gen): give cap_array's C<i>_TOP port a routable escape pad (#1500)
- fix(extract): recover DEF net names for unrouted single-pin nets (#1498)
- fix(lvs): expand nf>1 MOS subckt calls into parallel plain-element fingers (#1489)

#### Changed

- refactor(decks): split decks/__init__.py into rules/extraction/parasitics submodules (#1824)
- refactor(place-and-route): split DEF->GDS merge subsystem into place_and_route_gds_merge.py (#1823)
- refactor(cli): extract checks-cluster parsers into _add_X_parser() helpers (#1807)
- refactor(place-and-route): split corner-sweep/post-route-SPEF STA into place_and_route_sta.py (#1819)
- refactor(cli): extract misc-cluster parsers into _add_X_parser() helpers (#1811)
- refactor(cli): extract 5 generation-verb parsers into _add_<verb>_parser() helpers (#1818)
- refactor(cli): extract analysis-cluster parsers into _add_<verb>_parser() helpers (#1817)
- refactor(cli): extract 5 inspection-verb parsers into _add_<verb>_parser() helpers (#1802)
- refactor(lvs): split netgen engine subsystem into lvs_netgen.py (#1806)
- refactor(cli): extract 'extraction/verification' cluster parsers into _add_*_parser() helpers (#1809)
- perf(benchmarks): cross-step cache for design-agent-benchmark reference solutions (#1794)
- perf(benchmarks): cache deterministic reference-provider attempts, re-tune CI timeout from measured run (#1792)
- refactor(cli): consolidate duplicated _print_rerun_text into output.render_rerun_drift (#1787)
- refactor(benchmarks): split interactive agent provider into its own module (#1770)
- refactor(sim): split fleet shard/merge + remote dispatch into sim_remote.py (#1765)
- refactor: dedup SPICE + continuation-line fold into _paths.py (#1741) (#1751)
- refactor(lvs): split mismatch-classification subsystem into lvs_mismatch.py (#1724)
- refactor(gen-compose): split routing subsystem into gen_compose_routing.py (#1717)
- refactor: dedupe _tcl_net_list/_count_spef_nets_annotated into _paths.py (#1715)
- refactor(gen): split ten _build_<family>_pcell() factories into gen_pcells/ package (#1713)
- refactor: extract _load_block_cell to dedupe gen_compose GDS-load block (#1711)
- extract: add --parasitics-net to scope the ground R/C pass to named nets (#1705)
- extract: expose per-net R/C breakdown by layer in --parasitics JSON (#1701) (#1706)
- sim: add coarse timeout-budget preflight warning (part of #1686) (#1695)
- refactor: deduplicate _validate_vias between power.py and erc.py (#1693)
- refactor(gen): split _build_pcell_classes into per-family factories (#1650)
- refactor: remove unused build_identity.git_commit()/is_release() accessors (#1646)
- refactor: dedupe _run_openroad/_count_violations/_openroad_version into _openroad_engine.py (#1643)
- klt synthesize: additive structural verdict, warnings summary, baseline QoR delta (#1605)

#### Docs

- docs(design-agent): split-polarity dual comparator clears the sky130A ss/-40C/1.62V headroom ceiling (#1815)
- docs(kb): document sky130 binned nfet L=0.15/nf>=3 BSIM4 fatal footgun (#1812)
- kb: add reference netlist + measured figures for sky130-lc-vco-cross-coupled (#1805)
- kb: add reference netlist + measured figures for multi-ro-xor-entropy-source (#1778)
- kb: add reference netlists + measured figures for ro-puf, metastability-trng, ring-oscillator-jitter-trng (#1776)
- kb: add reference netlists + measured figures for beta-multiplier-bias-cell, cmos-subthreshold-voltage-reference and cmos-ring-vco-current-starved (#1773)
- kb: add reference netlists + measured figures for rx-front-end-termination-buffer and sram-power-up-puf (#1760)
- kb: add reference netlists + measured figures for folded-cascode-ota and cml-tx-line-driver (#1754)
- kb: add reference netlists + measured figures for two-stage-miller-ota and strongarm-latch-comparator (#1747)
- kb: add reference netlists + measured figures for five-transistor-ota and inverter-based-comparator (#1737)
- kb: add measured figures block + numeric search filters (av_db>=40 style) (#1727)
- docs: document daemon dispatch race claiming a reverted promotion (#1710) (#1712)
- docs: design spike — attribute --parasitics R/C to top-cell-drawn vs. instance-inherited geometry (#1707)
- docs(extract): document parasitics star addressing and device-card node order (#1676)
- docs: port per-concern RTL/testbench review guides into docs/guides/digital-review/ (#1612)
- docs(design): survey klt wave query surface and trace formats (#1598)
- docs(guides): port booley's RTL mutation-proposer guide (Apache-2.0) (#1597)
- docs(design): port-vs-build decision record for klt wave (#1596)
- docs(design): propose the klt wave build/query JSON contract (#1595)
- docs(design): survey mutation testing as a test-quality gate for functional-verification (#1594)
- docs(gen,drc): disclose gf180mcu mos_array's real-signoff-DRC gap (#1575) (#1578)
- docs(gen): add dog-bone terminal recipe for W below UNIT_MIN_W_UM (#1576)
- docs(layout_plan): update stale "Phase C not-yet-built" claims (#1566)
- docs(loom): document local Loom test-suite wiring convention (#1532) (#1537)
- docs(gen): state diff_pair's shared per-terminal x column in docs and --list (#1504)

#### Chores

- test(place-and-route): gate a real ihp-sg13g2 GCD integration test (#1784) (#1816)
- chore(deps-dev): bump the npm-minor-patch group across 1 directory with 5 updates (#1573)
- chore(deps): bump flate2 (#1482)
- ci: declare python3-click for the pinned SymbiYosys launcher (#1685)
- ci: run native-techmap on the dedicated heavy runner (2am#29) (#1246)
- chore(decks): regenerate deck history table for v0.4.0
- chore: resync installed Loom surfaces (72 commits, no PR)

## 0.4.0 (2026-09-05)

- **Fixed**: `klt lvs` no longer reports one comparer event twice when a
  `hints.same_nets` pair is refused after the comparer had already associated
  the two nets (issue #1484). Declaring such a pair used to add a
  `hints.rejected` entry *alongside* the pre-existing `topology` "nets were
  paired despite a name/identity conflict" entry for the identical pair, so
  declaring the hint could only raise the mismatch count — the exact opposite
  of what a caller reaching for the hint wants. The narrower, caller-attributed
  `hints.rejected` entry now replaces the `topology` duplicate for that one
  pair; a conflict reported for any other net pair in the same run is
  untouched, and a pair the comparer never associated at all still reports
  `hints.rejected` exactly as before. No category was added or removed. The
  `hints.rejected` `description` now also distinguishes the two refusal shapes
  ("the comparer did not confirm it as a topological match" vs. "the comparer
  associated the two nets and found them not identical topologically"), and
  `docs/cli/lvs.md` states the limitation the issue asked to be written down:
  `NetlistComparer` matches differently-named but topologically identical nets
  with no finding at all, so a `topology` name/identity conflict always means
  the nets genuinely differ — no `hints.same_nets` entry can clear one, and
  the fix is the underlying structural difference.
- **Fixed**: `klt lvs`'s `reference.form: "subckt-call"` converter now
  resolves every resistor and capacitor class a deck's own `ExtractionDeck`
  declares when `reference.deck` names that deck, instead of only the
  hand-curated subcircuit names in `klayout_tools.pdk_models` (issue #1464).
  Before this, a deck could recognise a device for *extraction* and then
  refuse to read that same device back on the LVS reference side, forcing
  the caller to hand-maintain a `reference.device_map` restating what the
  deck already knew — and to discover the need for one from a failed run,
  since the error's first suggestion was `reference.deck`. Closed by this:
  `sg13cmos5l`'s `rsil`/`rppd`/`rhigh` (declared since issue #1415) and
  `sg13g2`'s `cap_cmim` (declared since issue #1456; later moved from this
  derived binding to a curated table entry by issue #1470, with no change in
  behavior for callers). **Not** closed by this at the time: `sg13g2`'s
  other declared MIM class, `rfcmim`. The derived binding takes the declared
  class name as its own subcircuit name, but IHP ships that device as
  `.subckt cap_rfcmim`, so a real reference netlist calling `cap_rfcmim`
  needed an explicit `reference.device_map` entry until issue #1470 (below)
  closed it with a curated table entry instead (see
  `docs/cli/lvs.md` → "Per-deck coverage"). The derived binding only applies
  to a `(deck, family)` pair the
  curated tables do not cover at all, so sky130's genuinely non-identity
  `sky130_fd_pr__model__cap_mim` → `sky130_fd_pr__cap_mim_m3_1` mapping and
  the verified "ships no `.subckt`" carve-outs (`sg13g2`'s
  `res_metal1`/`res_metal2`, gf180mcu's `bjt`) are unaffected — `sky130` and
  `gf180mcu` resolve exactly the same set of names as before. An explicit
  `reference.device_map` entry still overrides a derived binding, and the
  "not a known device for the requested deck" error now enumerates the
  derived classes alongside the MOS names. The *writing* direction (`klt
  extract --pdk`) is untouched: an unbound class still keeps its bare
  primitive card. No `schema_version` bump — no field changed shape.
- **Fixed**: `klt lvs`'s `reference.form: "subckt-call"` converter now
  resolves `sg13g2`'s `rfcmim` MIM capacitor from `reference.deck: "sg13g2"`
  alone (issue #1470) — the one live gap issue #1464/#1468 left open. IHP's
  real subcircuit name for the device is `cap_rfcmim`, not `rfcmim`, so the
  assumed-identity derivation `klayout_tools.pdk_models.build_device_binding_map`
  falls back to (see the entry above) never reached it; a caller had to
  hand-write `"device_map": {"cap_rfcmim": {"kind": "capacitor", "class":
  "rfcmim"}}`. Closed by a new curated `_CAPACITOR_MODEL_TABLE` entry for
  `("sg13g2", "sg13g2")`, verified against a real fetched IHP-Open-PDK v0.3.0
  install's `libs.tech/ngspice/models/capacitors_mod.lib`, mapping `rfcmim ->
  cap_rfcmim` and `cap_cmim -> cap_cmim`. `cap_cmim` had to move into this
  same curated entry alongside it: the derived fallback is gated on the
  whole `(deck, family)` pair, so adding any curated entry for `sg13g2`'s
  capacitor family takes `cap_cmim` out of derivation's reach too — its
  resolution behavior for callers is unchanged, only its internal source.
  Because the curated table also feeds the *writing* direction
  (`resolve_device_bindings`), `klt extract --pdk` now binds sg13g2's
  `cap_cmim`/`rfcmim` devices to their real subcircuits too, instead of
  leaving them as bare `C` cards (see `docs/cli/extract.md` → "Coverage").
  No `schema_version` bump — no field changed shape.
- **Added**: `klt extract --deck sg13g2` now recognises the PDK's two MIM
  capacitors, `cap_cmim` and `rfcmim` (issue #1454) — closing out the
  deferral issue #1233 opened. `EXTRACTION_DECK.capacitors` declares a `MIM`
  (36/0) top plate over a `Metal5` (67/0) bottom plate, with
  `top_plate_via`/`top_plate_via_metal` set on the first pass (`Vmim` 129/0
  → `TopMetal1` 126/0). Both plates therefore land on tracked `metals[]`
  levels, so the recognised device is wired into the rest of the extracted
  graph rather than floating on two isolated nodes — the precise condition
  #1233 deferred on and issue #1243 (PR #1247) removed by extending the
  deck's `metals`/`vias` stack to TopMetal2. The two flavours share
  byte-identical plate geometry and are separated solely by `PWell.block`
  (46/21), the head term of upstream's own `mimcap_exclude`. Area/perimeter
  coefficients (1.5 fF/µm² / 0.04 fF/µm) are transcribed from the PDK's own
  `cmim_core` model card (`capacitors_mod.lib`'s `CJ=cap_carea`/`CJSW=40E-18`
  with `cornerCAP.lib`'s typical-corner `cap_carea = 1.5E-15`) and
  cross-checked against upstream's own worked `C=74.620f` example for a
  7×7 µm plate. `device_classes` for this deck gains `"cap_cmim"`/`"rfcmim"`
  (additive; they trail the MOS roles, ahead of `"resistor"`). Documented
  approximations: `rfcmim` is modelled as its 2-terminal plate-to-plate core
  only (its substrate terminal, `wfeed`, and RF parasitic network are not
  expressible in `CapacitorDevice`), and neither entry is bound to a SPICE
  subcircuit under `--pdk` yet, so both keep KLayout's bare `C`-card form.
  No `schema_version` bump — no field changed shape.
- **Added**: `klt extract --deck sg13g2` and `--deck sg13cmos5l` now
  recognise the PDK's two MoM
  (Metal-oxide-Metal) capacitors, `cap_cmomi` (interdigitated) and
  `cap_cmomf` (metal fringe/finger) — issue #1466, found while investigating
  #1463. This device family cannot be expressed by the existing
  `CapacitorDevice` shape (one marker layer covering the whole device
  footprint, not two independently-drawn plate layers; exactly two per-metal
  port shapes told apart by *position*, not by declared layer; no computed
  capacitance value at all), so this adds a new `MomCapacitorDevice`
  dataclass (`ExtractionDeck.mom_capacitors`) plus a dedicated
  `kdb.GenericDeviceExtractor` subclass (`extract.py`'s
  `_build_mom_capacitor_extractor`, a Python transcription of upstream's own
  `CapMomExtractor`). Recognised devices report only `w_um`/`l_um` (the
  marker's own bounding-box height/width) in `devices[].params` — no
  `c_f`/`area_um2`/`perimeter_um` at all, since the real device's
  capacitance is supplied by its SPICE/Verilog-A model
  (`density[N]*active_area + Cfeed`), not computed by LVS extraction; see
  `docs/json-contract.md`'s "Pre-1.0 caveat" for why this needs no
  `schema_version` bump. `device_classes` gains `"cap_cmomi"`/`"cap_cmomf"`
  on both decks (additive; on `sg13g2` they trail the two MIM capacitor
  roles, on `sg13cmos5l` they follow `"nfet"`/`"pfet"` — either way ahead of
  `"resistor"`). Both decks declare the family because both upstream rule
  decks do: `sg13cmos5l`'s own non-symlinked `sg13cmos5l.lvs` `%include`s the
  same derivation files directly, so these are genuinely cmos5l devices and
  not an sg13g2-only artifact it symlinks in unused. The one per-deck
  difference is reach — cmos5l's stack tops out at `TopMetal1` with no
  `Metal5`, so its port layers stop at `Metal4.pin` and an instance can only
  ever populate `m1p`..`m4p`. For `sg13cmos5l` this is also the deck's only
  capacitor family at all (its forbidden-layer rule blocks the MiM stack
  outright, the observation that opened #1463). No
  `schema_version` bump — no existing field changed shape.
- **Added**: `klt gen mos_array` and `klt gen diff_pair` now support the
  `sg13g2` (IHP-Open-PDK) PDK family (issue #1450), lifting the deliberate
  rejection #1448 shipped. Root cause: the shared unit device's gate-poly
  landing pad (issue #461) is a `CONTACT_SIZE_UM + 2*ENCLOSURE_MARGIN_UM`
  square that overhangs the narrower gate stripe on both sides, and with the
  pad sitting flush on the diffusion those overhangs' undersides faced
  unrelated `Activ` at zero distance — tripping `sg13g2`'s real
  `gatpoly.separation.activ.1` (`Gat.d`, 0.07 µm), the only rule of that
  shape any curated deck in this repo transcribes. The fix is a new
  per-family `_PDK_GATE_PAD_ACTIVE_CLEARANCE_UM` table in
  `klayout_tools.gen`: on a family that declares a clearance (`sg13g2`:
  0.1 µm — the only entry today) the landing pad is stood that far off the
  diffusion edge and the gap is bridged by a poly stem of exactly the gate
  length, so no poly edge faces `Activ` at zero distance. **`sky130` and
  `gf180mcu` declare no clearance and are byte-for-byte unchanged** — same
  drawn geometry, same `U<i>_G`/`*_G` port position and `width_um` (the
  contract issues #461/#492/#781 pinned). On `sg13g2` only, the reported gate
  port's `y_um` includes the clearance; its `width_um` is unchanged. Verified
  DRC-clean with `klt drc --deck sg13g2` across both finger topologies,
  `gate_contact`, `fingers > 1`, `dummy` columns, and `flavor="pfet"`.
  `esd_device`/`bjt_array`/`cap_array`/`bond_pad`/`well_island` remain
  explicitly rejected on `sg13g2`. No `schema_version` bump — no field
  changed shape.
- **Added**: `klt gen res_array` now exposes all three of `sg13g2`'s
  recognised poly-resistor device classes, not just the base one (issue
  #1451): `params.flavor` accepts `"rppd"` (260 Ω/□ — `EXTBlock` + `pSD` +
  `SalBlock`) and `"rhigh"` (1360 Ω/□ — `EXTBlock` + `pSD` + `nSD` +
  `SalBlock`) alongside the existing `"generic"` (`rsil`, 7 Ω/□). Both draw
  their class's own `requires` set from
  `klayout_tools.decks.sg13g2.EXTRACTION_DECK.resistors`, pass `klt drc
  --deck sg13g2` clean, and round-trip through `klt extract --deck sg13g2` to
  the matching device class. Internally, `gen.py`'s `_PDK_RES_FLAVOR_LAYERS`
  widens from a fixed two-slot `res_implant`/`res_block` pair per flavour to
  an ordered, arbitrary-length tuple of `requires` masks (the shape that
  structurally blocked `rppd`/`rhigh` before); `_ResArrayPCell` declares one
  static mask slot per mask the widest flavour in that table needs, so a
  future family's five-mask class needs a table entry only. `sky130`'s
  `"generic"`/`"high"`/`"xhigh"` and `gf180mcu`'s `"generic"` are
  geometry-identical to before, and a flavour name a family does not expose is
  still
  rejected with an error listing that family's own flavours. No
  `schema_version` bump — no field changed shape, and no request field
  changed meaning.
- **Added**: `klt gen res_array`/`klt gen guard_ring` now support the
  `sg13g2` (IHP-Open-PDK) PDK family, alongside `sky130`/`gf180mcu` (issue
  #1448 — the concrete follow-through to #1266's "Adding a third PDK family"
  contribution guide). `res_array`'s `"generic"` flavour round-trips through
  `klt extract --deck sg13g2` to the `rsil` poly-resistor device class;
  `guard_ring`'s tap ring is drawn on sg13g2's `Activ` layer (no distinct tap
  mask on this family, mirroring gf180mcu's own `Comp` reuse). Both pass `klt
  drc --deck sg13g2` clean on their documented default `params`. `mos_array`/
  `diff_pair` were **not** supported on `sg13g2` by this issue: the shared
  unit-device gate-poly landing pad tripped this family's real
  `gatpoly.separation.activ.1` DRC rule (`Gat.d`), a genuine geometry gap
  neither `sky130` nor `gf180mcu` happens to check for, so requesting either
  generator against `sg13g2` failed with a specific error citing that rule
  rather than silently emitting DRC-dirty output. (Superseded before release
  by issue #1450 above, which fixes the geometry and lifts that rejection.)
  `bjt_array`/`cap_array` (no bipolar/MiM-capacitor device
  recognition in this deck's curated `EXTRACTION_DECK`) and `esd_device`/
  `bond_pad`/`well_island` (not attempted by this issue) are also explicitly
  rejected for `sg13g2` rather than left to crash or silently succeed with
  untested geometry. `_pdk_family()` now delegates variant -> family
  classification to `klayout_tools.pdk_models._pdk_variant_family()` (the
  same helper `sim.py` already imports directly) instead of its own
  narrower literal-prefix scan, so IHP-Open-PDK's real resolved variant name
  (`"ihp-sg13g2"`, which is not itself prefixed by `"sg13g2"`) resolves
  correctly. No `schema_version` bump — no field changed shape.
- **Fixed**: `klt pdk cells`: `_DEVICE_MODEL_RE` gains a leading `\b`
  token-boundary anchor, so a flavor-shaped substring embedded inside an
  unrelated identifier can no longer accidentally match. Each library's
  `device_flavors` also gains an additive `device_flavors_status`
  (`"ok"`/`"unknown"`) field: a library whose `spice/` view has SPICE
  instance lines (`X<n> ...`) but none matched the device-flavor parser now
  reports `"unknown"` (a loud signal that this library's device-naming
  convention isn't recognised) instead of a silent `[]` indistinguishable
  from "this library genuinely has no devices" — addresses issue #537
  acceptance criterion 4, which #556's fix for the same issue didn't cover.
  The `klt pdk cells` text table renders `unknown` (not `-`) in the
  `devices` column for this case. No `schema_version` bump — both changes
  are additive (`supplies_v`, `device_flavors_status`); no existing field
  was renamed, removed, or had its documented type changed.
- **Fixed**: `klt place-and-route`'s `"route"` stage now always draws a real,
  `request.power`-independent `sky130_fd_sc_hd` row-rail obstruction
  (`add_pdn_stripe ... -followpins` on the library's own `VPWR`/`VGND` pins)
  immediately before `global_route` runs, even when `request.power` is
  entirely omitted (issue #1442). Without it, a `request.power`-less run had
  no continuous per-row power-rail shape on `met1` for the router to avoid —
  the same layer `_ROUTING_LAYER_RANGE` opens up to ordinary signal
  routing — so a signal net could legally route straight across an
  unfilled standard-cell row gap; a filler cell dropped into that same gap
  later (this stage's own `filler_placement` call, previously gated on
  `request.power` alone) could then physically short its own VPWR/VGND
  strap onto that pre-existing signal route, invisible to `klt drc`
  (spacing/width checks never fire on touching/overlapping same-layer
  shapes) but a real electrical short in `klt extract`'s
  `merged_net_labels[]`. `filler_placement`/`global_connect` now also runs
  whenever this row-rail fallback is active, safely, because the
  obstruction it draws precedes routing — live-verified against a real
  `openroad` toolchain to produce zero power/signal `merged_net_labels[]`
  merges. The fallback's own `pdngen` call carries `-dont_add_pins`
  (discovered live while validating this fix): without it, `pdngen`
  promotes `VPWR`/`VGND` into the design's own top-level **Verilog** port
  list even with no `-pins` on `define_pdn_grid`, breaking
  `tests/test_equiv.py`'s golden (`klt synthesize`) vs. gate (`klt
  place-and-route`) port-list comparison for every default,
  `request.power`-omitted `sky130_fd_sc_hd` route — `-dont_add_pins`
  suppresses exactly that promotion while leaving the physical
  `SPECIALNETS` row-rail shapes untouched. New additive response field
  `power.row_rail`
  (`emitted`/`layer`/`power_net`/`ground_net`/`filler_masters`); no
  `schema_version` bump. `tests/corpus/place_and_route/regenerate.sh` gains
  a matching `merged_net_labels[]` connectivity check (alongside its
  existing `klt drc` check) so this class of defect cannot silently ship
  again; the committed `gcd.gds.gz`/`mult8.gds.gz` corpus fixtures
  themselves are intentionally not regenerated in this change (see
  `docs/cli/place-and-route.md`'s "Row-rail fallback" section for why) —
  tracked as a follow-up.
- **Added**: `klt gen well_island` — a named-net well/tap island isolated
  from a caller-specified set of other wells (issue #1421). It draws
  `guard_ring`'s geometry with the two properties a *well island* needs and
  a guard ring cannot express: the tie is bound to a caller-supplied
  `params.net` (reported as every `TAP_*` port's `net` and as
  `drc_hints.well_net`, and drawn as a label on the ring's own **metal**),
  and `params.isolate_from` + `params.separation_um` state which other well
  regions it must stay clear of. It either trims its own well enclosure to
  clear them or raises a `GenError` naming the offending rectangle — never a
  silently merged well. Separation defaults to the *resolved family's own*
  different-potential well rule (sky130 `nwell.2`, 1.27µm euclidian;
  gf180mcu `NW.2b`, 1.4µm), neither of which the curated DRC decks check
  today (sky130 transcribes no well rule at all — issue #1420 — and
  gf180mcu deliberately transcribes only `NW.2a`'s equipotential 0.6µm), so
  the generator is the source of truth for the geometry it draws. The net
  name is deliberately **not** drawn on the deck's well-label layer: a text
  there names the `nwell` polygon directly and would make
  `klt extract`'s `devices[].nets["b"]` report the intended body net even
  with the tie broken. Naming the ring metal instead means the name only
  reaches the well through the physical tie, so a per-device body net is
  real evidence the bias works. New reporting fields
  `drc_hints.well_net`/`well_box_um`/`well_separation_um`/
  `well_keepout_box_um` (additive; no `schema_version` bump), and
  `ports[].net` is now non-`null` for this generator. `klt gen --list`
  gains a fifth `params[].type`, `list` (a JSON array), for
  `isolate_from`.
- **Fixed**: the `sg13cmos5l` curated deck now models SG13CMOS5L's **whole
  BEOL stack** — `Metal1`/`Via1`/`Metal2`/`Via2`/`Metal3`/`Via3`/`Metal4`/
  `TopVia1`/`TopMetal1` — instead of a single `Metal1` level with no via at
  all (issue #1417). This is a correctness fix, not just added coverage:
  with `EXTRACTION_DECK.metals` capped at `Metal1` and `.vias` empty, every
  shape on a higher level sat *outside* the deck's connectivity graph, so a
  net routed off `Metal1` extracted as several disconnected nets instead of
  one and silently mismatched a downstream `klt lvs` reference netlist —
  which in practice meant any block verified against this deck had to be
  floorplanned planar and single-metal. `klt drc --deck sg13cmos5l` gains
  the matching 21 width/space/enclosure rules (6 rules before, 27 now),
  each with a golden violate/clean fixture pair in
  `tests/golden_deck/sg13cmos5l/manifest.json`. Contrary to the rest of this
  deck (whose rule *text* is symlinked into a pinned sibling `ihp-sg13g2`
  checkout), cmos5l's `5_17_metaln.drc`/`5_19_via1.drc`/`5_20_vian.drc`/
  `5_21_topvia1.drc` are its **own**, non-symlinked files scoped to its
  real, shorter stack (`TopVia1` lands on `Metal4`, not sg13g2's `Metal5`),
  so those rules cite `IHP-GmbH/ihp-sg13cmos5l` provenance directly. No
  `Metal5`/`Via4`/`TopVia2`/`TopMetal2`: all four are on cmos5l's own LVS
  and DRC forbidden-layer lists, so the stack tops at `TopMetal1` and those
  four stay unrecognised. Metal resistors (`res_metal1`..`res_topmetal1`)
  are now reachable in principle but remain untranscribed — a dedicated
  follow-on, not a side effect of this connectivity fix.
- **Fixed**: the `sky130` curated DRC deck now checks the `nwell` (64/20)
  well layer — `nwell.width.1` (min width, 0.84 um) and `nwell.space.1`
  (min inter-well spacing/isolation, 1.27 um), transcribed from
  `sky130.lydrc`'s `nwell.1a`/`nwell.2a` (issue #1420). Before this, `DECK`
  had zero rules referencing `nwell`, even though `EXTRACTION_DECK` has
  always used that exact layer for PMOS device recognition and body-net
  derivation — a layout with illegally-close, effectively-merged well
  islands (e.g. two n-wells spaced well under the real 1.27 um minimum)
  reported a vacuous `status: "clean"` on the very layer its own
  extraction correctness depends on. Two more official well rules — the
  `nwell` enclosure of opposite-type diffusion/tap, scoped to a
  `psdm`/`nsdm` implant-layer boolean expression no `klt gen` generator
  draws today — are deliberately left uncovered; see the "nwell (well-layer)
  rule coverage" note in `src/klayout_tools/decks/sky130.py`'s module
  docstring for the full reasoning.
- **Fixed**: the `sg13cmos5l` curated deck now recognises SG13CMOS5L's three
  **drawn poly resistors** — `rsil` (7.0 Ω/sq), `rppd` (260.0 Ω/sq) and
  `rhigh` (1360.0 Ω/sq) — instead of leaving them undeclared (issue #1415).
  This is a correctness fix, not just added coverage: a drawn `rppd` body is
  a `GatPoly` strip contacted at both ends, so with no `ResistorDevice`
  entry the body was absorbed into ordinary interconnect and the resistor's
  **two terminals were shorted together**, collapsing two schematic nets
  into one and cascading into `net.unmatched`/`device.unmatched` for every
  other device in the block. Recognition is transcribed from cmos5l's own
  (symlink-resolved) `lvs/rule_decks/res_derivations.lvs`/
  `res_extraction.lvs` — `polyres_mk = polyres_drw.and(extblock_drw)
  .interacting(gatpoly).not(polyres_exclude)`, then each flavour's own
  `pSD`/`nSD`/`SalBlock`/`RES` terms — with every layer number read from
  cmos5l's own `sg13cmos5l.lyp` and every sheet rho from cmos5l's own,
  non-symlinked `sg13cmos5l_tech.json` (its `techName`, `SG13G2_CMOS5L`,
  selects the `G2`-suffixed keys; there is no cmos5l-specific `*G2C_rspec`),
  corroborated by `cornerRES.lib`'s typical (`res_typ`) corner. Metal
  resistors (`res_metal1`..`res_topmetal2`) remain unrecognised, deferred
  behind this deck's Metal2-TopMetal1 stack extension (issue #1417); two of
  them sit on layers cmos5l forbids outright. See
  `src/klayout_tools/decks/sg13cmos5l.py`'s resistor note for the
  transcription, the documented approximations, and the deferral rationale.
- **Fixed**: `klt sta`'s `spef_annotation` net-name correlation
  (`design_nets_annotated`/`design_nets_total`) no longer silently drops
  every design net whose name contains a SPEF-escaped bus-index bracket
  (`\[`/`\]`) or hierarchical-path separator (`\/`) (issue #1422). The
  correlation check compared a still-*escaped* name recovered from the
  SPEF's own `*D_NET` lines against the design's real, unescaped net names
  (`get_full_name`'s own spelling), so any name containing a SPEF-reserved
  character never matched — measured as low as ~51% correlation on a real
  routed design where the true structural agreement was ~99.5%.
  `_spef_net_names()` now un-escapes each recovered name
  (`_unescape_spef_name()`, the exact inverse of `extract_spef.py`'s
  `_spef_name()`) before it is used as a Tcl `get_nets`/array-key value.
  Also adds `spef_annotation.design_nets_missing_sample` — a capped sample
  (at most 20) of the design's own net names that still fail to correlate,
  for self-diagnosing a future correlation gap without a separate DEF/SPEF
  cross-check. Additive — no `schema_version` bump.
- **Added**: `klt lvs` now accepts `options.combine_devices_max_attempts`
  (issue #1412), a caller-configurable retry budget for
  `options.combine_devices`'s bounded-retry mitigation of KLayout's own
  `Netlist.combine_devices()` run-to-run nondeterminism (issue #1185) —
  previously a private, hardcoded `5`. A caller working against a
  large/complex netlist that observes retry exhaustion
  (`device.combine_incomplete` / `status: "inconclusive"`) more often than
  the default budget's small-fixture derivation predicts can now raise it
  without a fork; a caller iterating on a small netlist can lower it for
  faster feedback. Defaults to `5`, unchanged. `docs/cli/lvs.md`'s
  `options.combine_devices` / `device.combine_incomplete` sections also now
  document a reported (not independently re-measured) retry-exhaustion rate
  against a large (~8,600-device, multi-class) netlist, and correct the
  non-determinism guidance to note that every `category_counts` key can be
  volatile under `combine_devices`, not only `device.unmatched`. Additive —
  no `schema_version` bump.
- **Added**: `klt pdk find`/`env`'s JSON payload now reports `broken_symlinks`
  — every dangling symlink found under any resolved `assets` directory
  (issue #1406). Some upstream PDKs (e.g. IHP-Open-PDK's `ihp-sg13cmos5l`
  standalone, without its documented sibling `ihp-sg13g2` checkout) ship
  most of their device-symbol/model library as symlinks into a sibling PDK
  checkout; a standalone install leaves those dangling, and the install
  otherwise looks complete (`ls` shows every filename) until a downstream
  tool actually tries to open one. Also adds `klt pdk check`, a thin wrapper
  around `find_pdk()` that exits `4` when `broken_symlinks` is non-empty
  (`0` when clean) so this condition can gate CI, mirroring `cells
  --supply`'s dedicated exit code. Additive — no `schema_version` bump.
- `klt gen-compose` now routes **more than one** same-block self-net that
  needs `routing.cross_block_layer_role`'s fallback (issue #1393). Before
  this, `#1168`'s fallback only fired from the self-net pad-crossing /
  drawn-metal checks and had no visibility into a route-vs-route collision
  (#1057/#1386) with a *different* self-net on the same block that also
  needed the cross layer, so whichever net `connectivity[]` declared first
  claimed the lane and the other was reported unroutable
  (`"crosses already-routed net '<first net>'"`) — reordering the entries
  just moved the failure to the other net. `route_two_pin()` now retries
  such a leg on a bounded set of alternate lanes looped clear of the block's
  own bbox (the same *pattern* the #1167 detour search already applies to a
  third, unrelated block), re-running every routability check plus the
  route-vs-route check against each drawn lane and keeping the first that
  passes. Purely additive and narrowly armed: the retry is reachable only
  from a same-block self-net's own fixed-shape attempt that resolved onto
  `routing.cross_block_layer_role`, so every other leg reaches the
  route-vs-route check exactly as before, a leg whose every lane still
  conflicts is still reported `routed: false`, and nothing is waived to make
  a lane fit. Note an accepted lane is not pinned to the cross layer — it
  commonly resolves back onto the primary `routing.layer_role` once it no
  longer crosses the pads that forced the fallback. See
  `docs/cli/gen-compose.md`'s "More than one same-block self-net per block"
  section, which replaces that document's previous "at most one same-block
  self-net crossing per block" known-limitation callout.
- `klt pex`'s extracted-side leg no longer mis-resolves a PDK-relative
  `models.lib` path (issue #1395). When the request's `models` block uses the
  documented PDK-resolution shape (`{"pdk": "<variant>", "lib": "<relative
  path>"}`, optionally `"pdk_root"` — see `docs/cli/sim.md`'s "Model library
  resolution"), the generated extracted-side request copy now carries that
  pair through unmodified so `klt sim` resolves it against the PDK-variant
  directory, exactly as the schematic-side leg and a standalone `klt sim` run
  already did. Previously the relative `lib` was eagerly joined against the
  *original request file's* directory, producing a bogus absolute path and a
  `model library not found` failure on the extracted-side leg alone. The
  no-`pdk` shape (a plain relative or `$PDK_ROOT/…` literal `lib`) keeps its
  existing eager request-dir resolution, which the copy in `--outdir` still
  needs. No JSON shape change.
- **Fixed**: `klt extract --pdk sky130*` now writes the geometry on every
  PDK-bound `X` card as **bare micrometre numbers** (`L=0.15 W=0.65 AS=0.234
  AD=0.234 PS=1.6 PD=1.6`) instead of explicit SPICE unit suffixes (`L=0.15U
  ... AS=0.234P ...`) (issue #1396). sky130's own vendor deck
  (`libs.tech/combined/corners/all.spice`) sets `.option scale=1.0u` and
  documents the convention outright — *"1 micron width is W=1, not W=1u"* —
  and ngspice applies `scale` **on top of** the parsed literal (`×scale` for
  `l`/`w`/`ps`/`pd`, `×scale²` for `as`/`ad`). The suffixed form therefore
  reached the model 10⁶× too small, matched no model bin, and was rejected
  with the generic `could not find a valid modelname`; the same 1e6 error
  silently mis-sized sky130's bound resistor and MiM-capacitor cards, which
  are geometry-parameterized under the same ambient scale, so the convention
  is now resolved once per PDK family and applied to every bound device kind.
  **gf180mcu is deliberately unchanged** — its model library sets no `.option
  scale` and its subcircuits declare raw-metre defaults, so the absolute,
  suffix-carrying literal is correct there; `sg13g2` also stays on the
  unit-suffix default (no real IHP install was available to verify against).
  The **unbound** `M`-card form (no `--pdk`) keeps KLayout's own unit-suffixed
  spelling exactly as before, and `klt lvs` reference-netlist parsing is
  unchanged. See `docs/cli/extract.md`'s "Geometry units follow the resolved
  PDK's own convention".
- `klt pdk find`/`list`/`env` now confirmed to resolve IHP-Open-PDK's
  SG13CMOS5L (`ihp-sg13cmos5l`) via the same flat, single-PDK layout logic
  SG13G2 already used — no code change needed there (issue #1399).
  Verifying the generic engines against a real fleet-host install did
  surface a real gap in `pdk.drc_deck_file()`/`pdk.lvs_deck_file()`:
  IHP-Open-PDK nests its native `drc/`/`lvs/` directories one level deeper
  than open_pdks does (`libs.tech/klayout/tech/drc/`/`.../tech/lvs/`, not
  `libs.tech/klayout/drc/`/`.../lvs/` directly), so neither resolver ever
  found a deck on a real SG13G2/SG13CMOS5L install before this fix — both
  now fall back to that nested shape when the open_pdks-shaped directory
  does not exist, generically rather than as a per-PDK special case, so a
  real open_pdks install (sky130/gf180mcu) never pays for the extra probe
  and its resolution is unchanged. `lvs_deck_file()` also gained a second,
  independent fallback for the deck's filename itself: a real
  `ihp-sg13cmos5l` install's LVS deck is `sg13cmos5l.lvs`, dropping the
  variant's `ihp-` vendor-prefix segment entirely — a different mismatch
  shape from sky130/gf180mcu's existing trailing-suite-letter fallback
  (`sky130A` → `sky130`), generalized as "also try the variant name with
  its leading, hyphen-delimited prefix segment stripped." `klt drc --engine
  klayout` and `klt lvs`'s `"netgen"` engine both now run cleanly against a
  real (or SG13CMOS5L-shaped synthetic) install on a trivial cell — see
  `docs/cli/pdk.md`'s "Scope" and "PDK layouts: what resolves and what
  doesn't", `docs/cli/drc.md`'s "Deck resolution", and
  `tests/test_pdk.py`/`tests/test_drc_klayout_engine.py`/`tests/test_lvs.py`'s
  SG13CMOS5L fixtures. No curated deck exists for SG13CMOS5L yet (a
  separate, larger follow-up) — only the two generic-engine paths are
  proven here.
- `klt gen-compose`'s route-vs-route collision check (#1057) is now
  spacing-aware, not just overlap-aware (issue #1386): two accepted
  `connectivity[]` legs on the same effective drawing layer that never
  literally overlap, but sit closer together than the resolved deck's own
  same-layer minimum-spacing rule (e.g. sky130's `li1.space.1`/
  `met1.space.1`), are now rejected (`unrouted_nets[]`, a `legs[].reason`
  naming both the other net and the violated rule id) instead of both
  silently composing `routed: true` over a real `klt drc` violation. The
  same check is also now layer-aware: two accepted legs on genuinely
  different physical layers (e.g. one fell back to
  `routing.cross_block_layer_role` while another stayed on the primary
  `routing.layer_role`) are no longer compared against each other at all. A
  layer with no matching `"space"` rule in the resolved deck keeps the
  pre-#1386 overlap-only behavior unchanged. Purely additive: a
  request/response that was never affected by this check is unchanged; see
  `docs/cli/gen-compose.md`'s "Route-vs-route collision is spacing-aware"
  section.
- `klt extract` (and `klt lvs`, which shares the same extraction path) now
  emits two additive, cause-agnostic `warnings[]` entries when top-level pin
  promotion silently produces nothing to anchor `klt lvs`'s net/device
  correspondence against (issue #1385): one when the layout carries zero
  text on any of the target `--deck`'s own label layers anywhere in the cell
  tree (the observed real-world trigger is a `klt place-and-route` request
  whose `io.layer_h`/`io.layer_v` choice lands on a GDS layer the deck never
  scans for pin-name text), and one after every promotion/demotion pass
  (`make_top_level_pins()`, `--top-cell-pins`, `--pins`/`declared_pins`) has
  run, whenever the top circuit ends up with zero top-level pins for any
  reason. Purely additive (new `warnings[]` strings only) — no change to
  `nets[]`, `pin_count`, or any other field, and a layout that already
  promotes at least one top-level pin is unaffected.
- `klt lvs`'s `reference.form: "gate-level-verilog"` conversion now reads a
  Verilog **escaped identifier** the way Verilog defines it — one atomic
  token from the backslash to the next whitespace (issue #1371). A
  backend-emitted name that embeds a flattened `generate`/`genvar` hierarchy
  path, e.g. `\my_array[0].u_inst/_01_`, previously failed the whole run
  with `connection '...' is not a plain net reference, a single-index
  bit-select, or a '1'b0'/'1'b1' constant`, because the embedded `[0]` was
  re-read against the plain-identifier bit-select grammar; it now resolves
  to the literal net `my_array[0].u_inst/_01_`. A genuine bit-select of an
  escaped net (`\bus [3]`, with Verilog's terminating whitespace explicit)
  is accepted too. Purely additive: unescaped plain names, unescaped
  bit-selects (`bus[3]`), and the loud rejection of range slices
  (`bus[7:0]`) and general expressions are unchanged.
- `klt extract`'s `schema_version` bumps `2` -> `3` (issue #1376): the
  top-level `pdk.root` field's shape changed from a raw (often absolute)
  filesystem path string -- the literal `--pdk-root` argument, echoed
  verbatim -- to the `{path, scope}` shape `env_provenance.repo_relative_path`
  already defines (mirroring `klt pex`/`klt sim`/`klt size`'s own issue
  #1261 bump). A PDK install is inherently external to whatever repo invokes
  `klt extract`, so the old shape baked a host-specific absolute path --
  possibly a username, e.g. `/home/<user>/.volare/gf180mcuD` -- into any
  committed `--format json` evidence report, duplicating (without
  sanitizing) the already-path-free `provenance.pdk` block
  (`{name, source, version}`) in the same document. `root` is now
  `{"path": null, "scope": "external"}` for the common case of a PDK
  installed outside the invoking repo, or `{"path": "<repo-relative path>",
  "scope": "repo"}` for one that happens to live inside it.
- `klt lvs`'s `options.combine_devices` gained three caller-side escape
  hatches for the case where KLayout's own `Netlist.combine_devices()` trips
  its internal-consistency error *deterministically* rather than at the ~20%
  per-attempt rate #1185's retry mitigation is sized for (issue #1370). All
  three are additive — a request that does not set `options.combine_devices`
  behaves exactly as before, and `combine_devices: true`/`false` still mean
  what they meant.
  1. **The field now also accepts an array of device-class names**
     (`["nfet_01v8"]`) alongside `true`/`false`, combining only the named
     classes. Names match case-insensitively against both netlists' registered
     device classes. An empty array, a non-string entry, a bare string, or a
     class name present in *neither* netlist is a clean request error (exit
     `1`) rather than a silent no-op. The resolved value is echoed back
     verbatim under `options.combine_devices`, so `--check --rerun` reproduces
     the restricted compare.
  2. **New `status: "inconclusive"` and exit code `4`**, plus a symmetric
     degrade. When the retry budget is exhausted on *either* side, both
     netlists are now rolled back to their pre-combine state (previously a
     partially-folded layout was compared against a fully-folded reference,
     and every resulting `device.property`/`device.unmatched` finding was
     cascade). Because the compare the caller asked for never ran, a resulting
     engine `"mismatch"` is reported as `"inconclusive"` — a `"match"` is not
     downgraded. Exit `4` is the same numeric value `klt equiv` already uses
     for its own unreachable-verdict outcome. This changes the reported
     verdict for a `combine_devices` run that previously exited `3` with a
     `device.combine_incomplete` warning; every other run is unaffected.
  3. **`device.combine_incomplete` entries now carry
     `circuit`/`device`/`net` identifiers** parsed from KLayout's own error
     text and resolved against the failing netlist, plus a
     `details.terminal` / `details.klayout_error` block — instead of only the
     raw exception string embedded in `description`.

  A `klt eval` `"lvs"` gate whose report is `"inconclusive"` still gates as
  `status: "fail"` (the candidate has not cleared the gate) but now reports
  `exit_code: 4` rather than `3`, mirroring how a `"sim"` gate already
  distinguishes a broken corner from a failing one — `exit_code` is
  documented as the code the cited check would itself have returned, and `3`
  there would claim the design differs when the compare never ran.
- `klt extract --abstract-cells` no longer mis-binds abstracted-cell pins on a
  dense, fully-routed digital block (issue #1366, a regression introduced
  between 0.2.0 and 0.3.0 by the multi-candidate pin probing added in #1182 /
  #1184). When one pin resolved several candidate access points,
  `_abstract_pin_net_score` broke ties on *how connected* each candidate net
  was (`terminal_count + subcircuit_pin_count + pin_count`).
  `subcircuit_pin_count()` is incremented by the wiring pass itself, once per
  pin it wires, so on a fully abstracted block (no devices, so the other two
  terms are 0) the score measured nothing but how many pins had already been
  resolved onto each candidate — a rich-get-richer feedback loop whose winners
  are exactly the two impossible bindings reported: an output pin bound to one
  of its own instance's input nets (that input sorts first, so its net banked a
  pin before the output was scored) and a ground-role pin bound to the power
  net (`VDD` sorts before `VSS`, so the power rail was always one pin ahead of
  the ground rail) — 126 impossible bindings across 1339 instances in the
  reported repro, on a byte-identical input that 0.2.0 extracted correctly.
  Two named candidates are now never ranked against each other, and among
  unnamed candidates only `terminal_count()` (a fixed property of the drawn
  layout) breaks the tie; anything still tied resolves in favour of the pin's
  own primary access point — its drawn label or first-declared LEF `PORT` —
  rather than a fragment discovered by walking the cell's internal
  connectivity. `.SUBCKT` headers, pin names/order, instance names and cell
  types are unaffected; only net assignment changes.
- `klt extract --abstract-cells` gained a post-wiring self-check (issue
  #1366): any abstracted instance that resolves two or more of its separately
  declared pins onto one net now produces an aggregated `warnings[]` entry
  naming the instance, pins and net. It is a warning, not a failure — a
  deliberately tied-off pin is legal — but it is the single observable
  signature both impossible bindings above collapse to, and previously such a
  netlist reported a completely healthy run right up until a downstream
  `klt lvs` read the fault as a design error.
- `klt equiv --engine yosys-sequential` now converges on real post-route
  netlists (issue #1353). `equiv_make` pairs `gold`/`gate` wires by name, and
  OpenROAD's resizing/repair-buffer/cloning passes legitimately leave
  same-named *internal* wires meaning different things; each such pair became
  an unprovable `$equiv` obligation *and* a false cut point poisoning every
  proof downstream of it, so a real 50-flop GCD pre/post-route pair reported
  `"inconclusive"` (and, before #1349, a false `"counterexample"`). Stage 1
  now runs as a bounded counterexample-guided refinement loop: the wires
  `equiv_status` reports unproven are fed back through `equiv_make
  -blacklist` -- **never a top-level port** -- and the recipe re-runs, up to
  three passes sharing the one `timeout_s` budget. Blacklisting only removes
  cut points (assumptions), so this is strictly stronger than not doing it,
  and the output obligations that define equivalence are always retained.
  Measured live against `openroad` 26Q3-1510-g6cb3f2b704 + a pinned volare
  sky130A + Yosys `0.67+post`: the GCD canary goes from 93 unproven cells /
  `"inconclusive"` to 1428/1428 proven / `"equivalent"` in one refinement
  pass (~28 s); a single-cell mutation of the same post-route netlist still
  does *not* converge. The engine's script also gained an `opt -noff`
  normalization pass (98 -> 93 unproven before refinement; `-noff` keeps
  every flip-flop untouched). Additive JSON: `artifacts.stage1_blacklist_path`
  (the auditable list of dropped pairings, `null` when unused) and an
  `equiv_cutpoint_refinement` info diagnostic. The GCD real-P&R canary test
  and `.github/workflows/equiv-canary.yml` no longer carry an `xfail`.
- Ran the full `klt yield-campaign` -> `klt yield-sensitivity` -> yield-aware
  `klt size` pipeline end to end against the PLL charge-pump canary (issue
  #1327, epic #705 Phase 2), validating that `klt size`'s design-centering
  objective (#1326, below) actually improves measured yield/margin, not
  just that it runs. A real sky130 `tt_mm` mismatch-corner Monte Carlo
  (`n=32`) on the as-authored charge pump measured 40.6% empirical UP/DOWN
  current-mismatch yield (stddev 4.89%, `target_yield=0.9`); a bespoke
  sensitivity harness (`klt sim`'s mismatch MC does not expose per-device
  parameter draws, so this is a caller-side harness per
  `docs/cli/yield-sensitivity.md`'s own documented fallback) ranked the
  DOWN leg's Vth mismatch as dominant; the yield-aware `klt size` request
  grew that device's `mult` to 48x; re-measuring the re-centered circuit
  raised empirical yield to 56.25% and cut stddev by ~37% to 3.08% -- a
  real, measured improvement, though neither run clears the 90% target
  (the remaining gap is a systematic Vds-mismatch bias
  `charge_pump.sizing.json` already documents, which `mult` growth cannot
  address -- see `examples/kb/pfd-charge-pump-tri-state/
  design-centering-validation/README.md`'s "Result" section for the full,
  honest writeup including confidence-interval overlap at this sample
  size). No code change to `klt size`/`klt yield-campaign`/`klt
  yield-sensitivity` themselves (already complete as of #1326/#906/#923);
  the new evidence bundle plus `tests/test_design_centering_validation.py`
  guard the committed numbers.

- `klt size` gains a design-centering/yield-aware objective mode (issue
  #1326, epic #705 Phase 2), wiring `klt design-centering`'s
  `ranking[].parameter`/`ranking[].contribution` contract (#924/#710)
  directly into the sizing loop: setting `request.design_centering`
  ({`ranking`, `parameter_map`}, additive to `corners.objective` #769)
  grows whichever instance(s) the ranking flags as the dominant mismatch
  contributor(s) (by `mult`, rounded up to the next whole device, never
  `w_um`) and re-solves at the grown geometry -- all inside one `klt size`
  call, in both single-device and topology mode, instead of requiring a
  manual `klt design-centering` re-run. See `docs/cli/size.md`'s
  "Design-centering objective" section.

- `klt place-and-route`'s post-merge DIEAREA bounding-box guard (issue
  #1090) no longer rejects every gf180mcu `request.power` run (issue
  #1335). open_pdks' gf180mcu standard-cell library deliberately draws GDS
  geometry beyond its own LEF abutment box so abutting cells' wells/
  implants merge across a row, and `tapcell`'s row-end `__endcap`
  placements reach the die boundary once `request.power` is set — a
  legitimate overhang the guard's previous fixed 0.01 um tolerance could
  not distinguish from a genuine DBU doubling/halving defect. The guard's
  tolerance is now the merged cell library's own measured GDS-vs-LEF
  abutment-box overhang, capped at the tech LEF's own row (`SITE`) height
  so it can never mask a real doubling/halving-scale defect; the existing
  fixed 0.01 um tolerance remains the fallback when the tech LEF declares
  no `SITE`. sky130hd is unaffected (its cells have no such overhang). See
  `docs/cli/place-and-route.md`'s "DEF→GDS merge" section.

- `klt lvs` gains a new `reference.form` value, `"gate-level-verilog"`
  (issue #1336), closing the gap that left `klt lvs` with no code path to
  run against a `klt place-and-route` result: `reference.netlist` is read
  as a gate-level Verilog netlist (the shape `verilog_path`, issue #996,
  writes) instead of SPICE, and converted in-process to plain-element-
  shaped SPICE before comparing — each standard-cell instance becomes an
  `X<inst>`/pin-only-`.SUBCKT` black-box call, mirroring `klt extract
  --abstract-cells`'s own layout-side shape (issue #620). The new
  `reference.library` field (required with this form) names the standard-
  cell library whose real `libs.ref/<library>/{spice,cdl}/<library>.
  {spice,cdl}` file resolves each cell's pin order — via `klt pdk`'s
  existing `libs_ref` resolution (`reference.pdk`/`reference.pdk_root`
  mirror `klt extract`'s `--pdk`/`--pdk-root`), never a hardcoded table. See
  `docs/cli/lvs.md`'s "Digital gate-level LVS" section for the full
  contract. `scripts/place-and-route-smoke.sh` (the `workflow_dispatch`
  place-and-route smoke job) gains a matching second LVS stage per design —
  routed GDS via `klt extract --abstract-cells` against that same run's own
  `verilog_path` — which its earlier revisions documented as impossible.
  **Scope of the verdict:** a layout side carrying `VPWR`/`VGND` pins (what
  `klt extract --abstract-cells` normally produces) compares cleanly, but
  because `verilog_path` carries no power connectivity at all, a power-net
  defect is *invisible* to this form — it establishes signal connectivity
  only, never power-grid correctness.

- `klt signoff` recognizes a new `"power"` evidence kind (issue #1321, Phase
  2 of epic #712): a `klt power --format json` envelope, detected
  structurally (`power_nets` + `networks`) since it carries no top-level
  `status` field and no `provenance` block, unlike every other recognized
  kind. In envelope-aggregation mode (`klt signoff drc.json ... power.json`)
  it passes on `em_verdict.status == "pass"` — a `null` `em_verdict` (no
  IR-drop solve requested) or a rolled-up `"fail"`/`"not_checked"` does not
  pass — with `worst_case_droop_mv` surfaced informationally in
  `checks[].detail`, not itself compared (the envelope declares no droop
  limit to check it against). `docs/design-evidence-tiers.md`'s T1
  checklist has no item for power-grid IR-drop/EM evidence yet, so a
  `"power"`-kind citation is never graded `"met"` by `--manifest`/`--fleet`
  tier-verdict mode, for any item. See `docs/cli/signoff.md`'s "`klt power`
  evidence (envelope aggregation only)" and "No T1 item accepts `power`
  evidence" sections.

- `klt place-and-route`'s gf180mcu path is now **live-verified end to end**
  (issue #1331, closing Epic #700's acceptance criterion 4 with a real tool
  run rather than the corrected configuration data #637/PR #639 delivered).
  Run 2026-08-22 with `openroad 26Q3-1080-gab6fd26351` from
  `openroad/orfs:latest` against a real volare `gf180mcuC` install
  (`open_pdks c6d73a35f524070e85faff4a6a9eef49553ebc2b`): the GCD worked
  example goes `klt synthesize` → `klt place-and-route` floorplan through
  full detailed route with `route__drc_errors: 0`, `flow__errors__count: 0`,
  and 0 antenna-violating nets/pins, producing a valid merged GDS. The
  previously skip-only gated test
  `test_integration_real_openroad_gcd_worked_example_gf180mcu` ran to
  completion and passed. No behavior change to any command; results and
  their two caveats are recorded in `docs/cli/place-and-route.md`'s new
  "Live verification" section.

  That run surfaced two things stubbed tests could not:

  - **The gated test's own PDK-variant gate was wrong** and is fixed here.
    A stock volare gf180mcu install ships four variants differing only in
    metal-stack depth (`gf180mcuA` 3LM, `gf180mcuB` 4LM, `gf180mcuC`/
    `gf180mcuD` 5LM), all shipping a complete `gf180mcu_fd_sc_mcu9t5v0`
    asset set, so asset presence alone selected `gf180mcuA` — which cannot
    serve this command's `Metal2-Metal5` routing range and dies in the
    `place` stage with `[ERROR PPL-0051] Layer Metal4 not found.`
    `_find_real_pnr_variant` now additionally requires the variant's tech
    LEF to declare each routing layer the request references. Test-only
    change; no `src/` change was needed, so #637's shipped
    `_ROUTING_LAYER_RANGE`/`_CTS_BUFFER_CELLS` values are confirmed correct
    against a real install.
  - **DRC-clean requires `request.power`, and at the time of this run that
    configuration could not emit a GDS.** Without `request.power` the routed
    GDS carries 19 `nwell.space.1` violations under `klt drc --deck
    gf180mcu` (unfilled standard-cell row gaps — the documented consequence
    of omitting power delivery). With `request.power` the same design is
    **DRC-clean, 0 violations**, but `klt place-and-route` raised instead of
    writing the GDS: its DEF→GDS merge DIEAREA bbox guard did not account
    for gf180mcu cells drawing geometry outside their LEF abutment box
    (`__endcap` by 2.93 um). That was issue #1335, **fixed by #1342** — see
    the DIEAREA guard-tolerance entry above in this `## Unreleased` section.
    The run's 0-violation number required a manual guard bypass then; it
    reproduces without one now.

  **LVS remains unverified** for this path: nothing in `klt` converts the
  flow's as-built `verilog_path` into the SPICE reference `klt lvs`
  requires, so there is no LVS code path to run rather than a failing one.
  Tracked in #1336.

- `klt layout-metrics` gains an optional **`--pdk`** flag and a matching
  optional **`pdk`** field in `layout.json` — issue #1285, the deferred
  follow-up from #943/#1060. The value is one of `klt`'s own PDK-family
  names (`sky130`, `gf180mcu`, `sg13g2`, the same vocabulary as
  `drc.deck`), recorded verbatim and **never inferred**: a block directory
  carries nothing that identifies its PDK. Additive, so `schema_version`
  stays `1` and a `layout.json` written before this field remains valid.
  Unlike `--deck` (best-effort — an unknown name just omits `drc`), an
  unknown `--pdk` exits `1` rather than writing a wrong identifier into the
  contract. The gallery content pipelines populate it where they know it:
  `scripts/bootstrap-gallery-blocks.py` from the `tests/corpus/<pdk>/`
  directory it walks, `scripts/ingest-canary.py` from a new `--pdk` flag or
  the conservative `<pdk>-<name>` slug guess it already uses for render
  labels (omitted when neither yields a PDK). The site's block detail page
  now prefers this field over its slug-prefix heuristic when picking the
  embedded GDS viewer's `pdk=` identifier, keeping the heuristic as the
  fallback for blocks that predate the field. See
  `docs/cli/layout-metrics.md`.

- `klt pex`'s `pin_count_mismatch` diagnostic (issue #1030, PR #1039) is now
  detected **pre-flight and engine-independent** instead of only reactively
  from ngspice's own refusal to elaborate the extracted-side deck (issue
  #1041). Immediately after extraction, `run_pex` statically compares the
  schematic DUT's and the extracted netlist's `.SUBCKT` headers for the
  specific subcircuit `klt extract` wrapped its output in — scoped to that
  one instantiated subcircuit, so an unrelated helper `.SUBCKT` of a
  different pin count elsewhere in either netlist cannot false-positive it —
  before the extracted-side `run_sim` call for any testbench. A mismatch
  found this way skips the extracted-side simulation entirely for every
  testbench (it cannot produce a trustworthy value either way) and reports
  the same `pin_count_mismatch` shape as before, with `ngspice_message:
  null`. This matters because ngspice 42 — what `apt`/this repo's CI
  installs — is measurably laxer than ngspice 46 and silently accepts many
  pin-count mismatches (an extra pin, or fewer pins than declared) instead
  of rejecting them, so the previous purely-reactive detection could report
  a plausible-looking `status: "pass"` with `pin_count_mismatch: null` and
  silently wrong extracted values on exactly the ngspice version most users
  and CI actually run. The original reactive paths
  (`_pin_count_mismatch_from_report` / `_pin_count_mismatch_from_message`)
  remain as a fallback for mismatches the static header comparison cannot
  see — e.g. a subcircuit name only one side declares, or an unreadable
  header — so `ngspice_message` is still populated (non-`null`) when the
  fallback is what actually caught it. See `docs/cli/pex.md`'s "The pin
  lists must match" section (the "Detection depends on your ngspice
  version" caveat it previously documented no longer applies). No
  response-shape break — `pin_count_mismatch`'s own shape and
  `schema_version` are both unaffected, only *when* the block is populated
  changed.

- `klt power` gains the **per-net EM (electromigration) current-density
  verdict** — issue #846, Phase 1c of the power/IR-drop + EM signoff epic
  #712. Each `stackup`/`vias` role gains two new optional inputs,
  `current_limit_a_per_um` (a metal role's own EM limit, per-width, the same
  way a real PDK's tech LEF already expresses `DCCURRENTDENSITY`) and
  `current_limit_a` (a via role's flat per-shape limit), plus a free-text
  `current_limit_source` citing where each came from; the response gains a
  new `em_verdict` field comparing every solved edge's branch current
  (Phase 1b's `ir_drop_map`) against its own cited limit and rolling the
  result up per net (`"pass"`/`"fail"`/`"not_checked"`), plus
  `current_limit_a`/`current_limit_source` on every base extraction edge.
  All additive — every field an earlier phase promised is unchanged, so
  `schema_version` stays `1`. An edge with no declared limit or no solved
  current is `unchecked`, never guessed at. Validated with golden pass/fail
  segments (a rail engineered just under, and just over, a declared limit,
  on both a metal edge and a via edge) plus an end-to-end pass on the real
  OpenROAD-produced `gcd` corpus fixture against sky130's own real
  `DCCURRENTDENSITY AVERAGE 2.8` met1/met2 limit (verified against a real
  sky130A install). See `docs/cli/power.md`.

- **Fixed**: `klt gen mos_array`/`diff_pair`'s `voltage_flavor` param now
  resolves on the `ihp-sg13g2`/`ihp-sg13cmos5l` PDK families (issue #1472).
  `_PDK_VOLTAGE_FLAVOR_LAYERS` (`gen.py`) previously had entries only for
  `gf180mcu`/`sky130` — both IHP families were added to the sibling
  `_PDK_ROLE_LAYERS` table by #1448/#1462 without a matching
  `_PDK_VOLTAGE_FLAVOR_LAYERS` entry, so a `voltage_flavor` request on either
  family always resolved to no marker layer and silently drew the default
  thin-oxide device, reported only via a `drc_hints.notes` entry a caller
  could easily miss. Both families now share the flavour name `"hv"`,
  transcribed from the curated decks' own
  `EXTRACTION_DECK.mos_flavours[0].flavour` value, drawing their shared
  `ThickGateOx` (44/0) marker — the same marker each deck's own
  `mos_flavours` entry keys its `sg13_hv_nmos`/`sg13_hv_pmos` device-class
  split on, so `voltage_flavor="hv"` now round-trips through `klt extract
  --pdk` to the real thick-oxide model instead of the thin-oxide default. No
  `schema_version` bump — additive per-family coverage of an existing field,
  no shape changed.

## 0.3.0 (2026-08-21)

### Fixed since release

- 2026-08-21 — **Breaking (`klt sim` `schema_version` 2 -> 3, `klt size`
  `schema_version` 1 -> 2):** `environment.models_lib` no longer echoes the
  resolved **absolute** model-library path (issue #1274, found while
  implementing #1264). This is the same leak class the entry below fixed for
  the other `klt sim`/`klt pex` path fields — `environment.models_lib` was
  simply missing from that PR's field list, and `klt size` had the identical
  defect in all three of its `environment` writers (default `sizing_corner`
  objective, `worst_case_margin` objective, and coupled-topology mode). On a
  normal install the PDK lives under the user's home directory, so every
  committed report carried something like
  `/home/<user>/.volare/sky130A/libs.tech/ngspice/sky130.lib.spice` and
  `klt env-provenance scan` flagged it — and unlike the other fields, this
  one could not be cleaned by regenerating the report, since a fresh run
  just substitutes the regenerating machine's home path. Both commands now
  report the `{path, scope}` shape `env_provenance.repo_relative_path()`
  defines: `{"path": "<repo-relative>", "scope": "repo"}` for a library
  inside the invocation's repo, `{"path": null, "scope": "external"}` for
  one outside it (the absolute path is never echoed), and — for `klt sim`
  only, which resolves a library only when the request declares a
  `corners.process` axis — `{"path": null, "scope": "absent"}` when none was
  resolved at all. Chose `{path, scope}` over keeping a `string | null`
  because it matches the shape every other normalized path field in these
  reports already uses, and keeps "outside the repo" distinguishable from
  "not resolved". Nothing is lost: the library stays pinned **by identity**
  via `environment.models_lib_sha256` (`klt sim`) and
  `provenance.deck.name`/`content_hash`/`provenance.pdk` (both), which is
  `env_provenance.py`'s own documented rule ("pin such an input by identity
  … not by location"). `--format text` renders the field through
  `env_provenance.render_path_field()`, printing `<outside repo>` rather
  than a raw dict or an absolute path. The four committed example reports
  that carried the leak (`examples/design-pipeline/sim-ac.result.json`,
  `sim-op.result.json`, `examples/sim-remote/matrix-local.report.json`,
  `matrix-remote.report.json`) are regenerated in the same change. See
  [`docs/cli/sim.md`](docs/cli/sim.md) and
  [`docs/cli/size.md`](docs/cli/size.md)'s "JSON schema (the contract)"
  sections.
- 2026-08-21 — `klt extract --parasitics`: `sg13g2`'s `PARASITICS.metals`
  table was an all-defaults-empty `ParasiticsDeck()` (issue #1277), so
  `--parasitics` on this deck always reported `r_count: 0, c_count: 0` for
  every net regardless of the input layout's real Metal1/Metal2 routing —
  a caller who didn't specifically read `warnings[]` could easily miss that
  every "parasitic" result for `sg13g2` was silently zero, the deck's own
  analogue of #547's gf180mcu gap. `PARASITICS.metals[0]`/`[1]` (Metal1,
  Metal2) and `metal_overlaps[0]` (the Metal1/Metal2 vertical-overlap
  coefficient) are now curated, sourced from
  `libs.tech/magic/ihp-sg13g2-extract.tech`'s public nominal (`variants
  ()`) corner in a real IHP-Open-PDK v0.3.0 install (Apache-2.0) — *not*
  `sg13g2_typ.itf` as originally suggested, since that file carries only a
  raw process-stack description with no directly-transcribable area/
  perimeter-capacitance table; see
  `src/klayout_tools/decks/sg13g2.py`'s `PARASITICS` module comment for the
  full rationale. `sheet_res_ohm_sq` reuses `EXTRACTION_DECK.resistors`'
  already-curated `res_metal1`/`res_metal2` values (0.110/0.088 ohm/sq)
  rather than re-deriving them; both independently cross-check against this
  same `.tech` file's own `resist (allm1)/metal1 110` /
  `resist (allm2)/metal2 88` nominal-corner entries. Metal3 through
  TopMetal2 remain **deliberately uncurated** — out of scope for this issue
  — and continue to report as gaps in `metals_without_coefficient`/
  `overlap_pairs_without_coefficient`, which is correct, expected
  post-fix behavior, not a regression. **Behavior change**: `klt extract
  --deck sg13g2 --parasitics` on a layout routed on Metal1/Metal2 now
  reports nonzero `r_count`/`c_count`/`total_resistance_ohm`/
  `total_capacitance_ff` where it previously reported zero. See
  [`docs/cli/extract.md`](docs/cli/extract.md) → "Parasitic (RC)
  extraction".
- 2026-08-21 — `klt extract --parasitics`: `sg13g2`'s `PARASITICS.metals`/
  `metal_overlaps` tables now cover the deck's full seven-level metal stack
  (Metal1-TopMetal2), extending #1277's initial Metal1/Metal2-only slice
  (issue #1281). `metals[2:7]` (Metal3, Metal4, Metal5, TopMetal1,
  TopMetal2) and `metal_overlaps[1:6]` (the five remaining adjacent-pair
  vertical-overlap coefficients) are now curated, sourced from the same
  `libs.tech/magic/ihp-sg13g2-extract.tech` public nominal (`variants ()`)
  corner #1277 used, in the same real IHP-Open-PDK v0.3.0 install
  (Apache-2.0). Unlike Metal1/Metal2, `sheet_res_ohm_sq` for Metal3-
  TopMetal2 is transcribed **directly** from this file's nominal
  `resist (),(lvs)` block — `EXTRACTION_DECK.resistors` does not (yet)
  declare `res_metal3`..`res_topmetal2` sheet rhos to reuse — and
  independently cross-checked against
  `libs.tech/klayout/tech/lvs/rule_decks/res_extraction.lvs`'s
  `RSH_RES_METAL3`..`RSH_RES_TOPMETAL2`, which agree exactly. See
  `src/klayout_tools/decks/sg13g2.py`'s `PARASITICS` module comment for the
  full per-value citations. **Behavior change**: `klt extract --deck sg13g2
  --parasitics` on a layout routed through Metal3-TopMetal2 now reports
  those levels' contribution to `r_count`/`c_count`/`total_resistance_ohm`/
  `total_capacitance_ff` instead of silently omitting it, and no longer
  lists any metal level in `metals_without_coefficient` /
  `overlap_pairs_without_coefficient` regardless of routing. See
  [`docs/cli/extract.md`](docs/cli/extract.md) → "Parasitic (RC)
  extraction".
- 2026-08-21 — **Breaking (per-command `schema_version` bump 1 -> 2, both
  commands):** `klt pex` and `klt sim` JSON reports no longer embed absolute
  input paths (issue #1261, found while implementing #1254: `klt
  env-provenance scan` flagged the author's home directory and a Loom
  worktree number in this repo's own committed evidence records, since a
  record wraps a `klt sim`/`klt pex` response unmodified —
  `docs/design/sim-evidence-discipline-spike.md`). `klt pex`'s
  `layout`/`netlist`/`reference_netlist` and each `testbenches[]` entry's
  `request`/`schematic_netlist`, and `klt sim`'s `netlist` and
  `environment.resume.checkpoint_path`, now report the `{path, scope}` shape
  `env_provenance.repo_relative_path()` already defines (issue #1254/PR
  #1260) rather than a raw path string: `scope: "repo"` with a
  repo-relative `path` when the input sits inside the invocation's repo,
  else `{"path": null, "scope": "external"}` — the absolute path is never
  echoed, in either case. Chose this (repo-relative normalization at the
  verb level) over the issue's other option (leaving the response
  unmodified and pushing normalization onto the evidence-record wrapper)
  because the wrapper convention's own "`result` is the untouched `klt
  sim`/`klt pex` output" guarantee stays true either way: fixing the writer
  means the wrapper needs no new step, while pushing the fix onto the
  wrapper would have required revising that documented guarantee for no
  added benefit. `pin_count_mismatch`/`flat_dut_mismatch`'s own nested
  `netlist` fields are unaffected (out of scope) and stay raw strings.
  Existing committed evidence records under `evidence/` are left
  unmodified, as documented in #1254 — this only changes what a *future*
  run emits. See [`docs/cli/pex.md`](docs/cli/pex.md) and
  [`docs/cli/sim.md`](docs/cli/sim.md)'s "JSON schema (the contract)"
  sections. **`klt yield`/`klt yield-campaign` are deliberately unaffected,
  and do not bump**: `klt yield` echoes a sim report's `netlist` into its own
  `source.netlist` — and `klt yield-campaign` feeds a live `klt sim` report
  straight into it by default — so `yield_analysis._read_samples` now unwraps
  the object to the resolved path string (`null` when the netlist is outside
  the repo), keeping `source.netlist` the plain string
  [`docs/cli/yield.md`](docs/cli/yield.md) has always documented. Schema-v1
  sim reports (a raw string) still read through unchanged.
- 2026-08-21 — `klt extract --parasitics` (and therefore `klt pex`, which
  always extracts with `parasitics=True`) now writes a DC reference for the
  deck's *synthesized* substrate net(s) instead of leaving them floating
  (issue #1263). Every ground capacitor the parasitics pass injects hangs
  off `substrate_net` (`vsubs`), a net `connect_global` mints out of nothing
  — no layout can label it, and nothing in the written netlist gave it a
  defined DC value. A testbench that `.include`d the extracted file and
  `X`-instantiated its `.SUBCKT` (the convention
  [`docs/cli/extract.md`](docs/cli/extract.md) documents) therefore hit
  `Warning: singular matrix:  check node x<dut>.vsubs` on the `.op`/
  transient solve, and ngspice's gmin/source-stepping recovery could return
  a *non-reproducible* operating point rather than failing loudly — a
  silently untrustworthy post-layout result. Pin exposure did not help: the
  substrate net is normally promoted to a pin, but a pin wired to an
  equally-undriven testbench node floats just the same. `--parasitics` now
  emits one `R<net>_dctie <net> 0 1e+12` shunt per synthesized substrate
  identity — the deck's `substrate_net` **and** every
  `<substrate_net>_iso<n>` isolated-region variant (issue #1128) — **inside**
  the extracted `.SUBCKT` body, against SPICE's global ground node `0` (which
  needs no `.global` card and no cooperation from the including testbench),
  so the tie travels with the artifact whether `klt extract` or `klt pex`
  produced it. Additive behavior change, no `schema_version` bump: the
  `.SUBCKT` header, pin list, `pin_count`, `net_count`, `device_count`,
  `devices[]` and `nets[]` are all unchanged (so `klt pex`'s
  `pin_count_mismatch`/`flat_dut_mismatch` diagnostics read the same
  interfaces as before), `r_count`/`total_resistance_ohm` still count
  *extracted* resistance only, and the new `parasitics.substrate_dc_tie`
  block names the tied nets and their card names. Idempotent with a
  hand-authored `.global vsubs`/`Vsubs`/`.options rshunt=1e12` (1 Tohm in
  parallel with an ideal source draws ~1.8 pA at a 1.8 V rail — existing
  testbenches need no edit), and measurably harmless on a leg that was
  already DC-well-formed. It is a DC anchor, **not** an AC ground: a
  measurement that needs the substrate held at a real 0 V reference (any
  coupling-capacitance delta) must still tie it explicitly. Documented in
  [`docs/cli/extract.md`](docs/cli/extract.md) → "Substrate DC reference" and
  [`docs/cli/pex.md`](docs/cli/pex.md) → "The extracted side carries its own
  substrate DC reference".
- 2026-08-21 — `klt pex`'s DUT `.include` swap no longer silently accepts a
  non-DUT single-`.include` testbench, or a flat-schematic-DUT-vs-
  `.SUBCKT`-wrapped-extraction mismatch (issue #1255, two gaps left by
  #1030/#1039). **Gap 1**: a testbench whose sole `.include`/`.inc` target
  is not plausibly the DUT (e.g. a shared corner/parameter file with no
  `.SUBCKT` and no circuit element of its own — the testbench's own DUT
  devices being inlined directly instead) used to be silently swapped in
  as if it were the DUT, producing a vacuous `status: "pass"` with
  `reference_netlist` naming the wrong file. This is now a hard, up-front
  `PexError` (exit `1`), before either side ever simulates. **Gap 2**: a
  schematic DUT netlisted flat (no `.SUBCKT`/`.ENDS` wrapper — the
  testbench references its internal nodes directly as top-level nets)
  against `klt extract`'s always-`.SUBCKT`-wrapped output used to make
  `_pin_count_mismatch` silently return `None` (its comparison requires
  both sides to declare a `.SUBCKT`), so the run either failed deep in a
  per-corner `ngspice.log` or, worse, reported a spurious passing number
  for a disconnected extracted-side fixture. This is now detected
  proactively — before either side simulates, unlike the reactive
  `pin_count_mismatch` check — and reported as a new, named
  `flat_dut_mismatch` block (same shape as `pin_count_mismatch`:
  `subcircuit`, `schematic`/`extracted` per-side detail, `ngspice_message`
  — always `null` here — and `detail`), with per-corner `delta[]` rows
  carrying `extracted_value: null`/`status: "error"` and exit code `4`
  (mutually exclusive with `pin_count_mismatch`: the extracted side is
  never even attempted). Purely additive — no `schema_version` bump (`klt
  pex` stays at `1`); every existing `.include`-swap and
  `pin_count_mismatch` behavior is unchanged. Documented in
  [`docs/cli/pex.md`](docs/cli/pex.md) → "That one line must plausibly
  name the DUT" and "Flat schematic DUT vs `.SUBCKT`-wrapped extraction".
- 2026-08-19 — `klt lvs --check <report> --rerun` can now reconstruct the
  compare a committed report describes instead of guessing at it (issue
  #1205). The report echoed a single `top` and no `options`, so full mode
  applied that one top to *both* sides and dropped every compare-shaping
  option: a report from an LVS negative control (a `<cell>_shorted` layout
  compared against the intact `<cell>`'s reference netlist, whose two tops
  differ by construction) exited `1` with "top cell/subcircuit not found in
  reference netlist", and a clean `status: "match"` report from a
  `combine_devices: true` request re-ran *un-folded* and reported the
  resulting fresh mismatch list as `status: "drifted"` — a spurious "your
  evidence drifted" verdict about a compare the consumer never asked for.
  The report now additionally carries `reference_top` (the reference side's
  own resolved top circuit) and an `options` block echoing
  `combine_devices`/`flatten_layout`/`flatten_reference`/`netgen_setup`/
  `parameter_tolerance` as resolved, and `--rerun` reconstructs from both.
  Purely additive — no `schema_version` bump (`klt lvs` stays at `1`), the
  pre-existing `top`/`parameter_tolerance` fields are unchanged, and a report
  committed before these fields existed re-runs exactly as it did before
  (each missing field falls back to the old behavior and is excluded from the
  drift diff, since a field a report never carried cannot have drifted).
  Cheap mode (`--check` without `--rerun`) reconstructs no request at all and
  is unaffected. Documented in [`docs/cli/lvs.md`](docs/cli/lvs.md) →
  "Response" and "`--check` / `--rerun`".
- 2026-08-19 — `klt extract` no longer silently substitutes a deck's
  synthesized `substrate_net` name for a real drawn one on a diode's
  substrate-formed terminal (issue #1196). A `diode_nd2ps_06v0`-class device's
  anode is tied to that global (`vsubs` by default) because gf180mcu draws no
  p-substrate mask; when a layout draws a substrate tie the deck's own tap
  mechanism claims (`Pplus`-covered `Comp` outside every `Nwell`, contacted —
  issue #1084), `connect_global` merges the two and the drawn label names the
  terminal, as it already did. When the drawn, contacted, **labelled** tie
  sits inside the diode's own device-mark footprint but the deck's tap
  derivation does *not* claim it (e.g. no `Pplus` implant), the two stay
  apart: the terminal keeps `vsubs` while the drawn net exists beside it. That
  case now emits one aggregate `warnings[]` entry per (diode class, terminal)
  naming the class, the terminal, the drawn label(s) that did not name it, and
  the synthesized net substituted for them. Connectivity is unchanged (a
  disclosure, not a re-wiring); no warning fires for a diode with no drawn tie
  at all, for an unlabelled tie, or when the tie's label is spelled exactly
  like the deck's own `substrate_net`. Documented in
  [`docs/cli/extract.md`](docs/cli/extract.md) → "Junction diodes".
- 2026-08-19 — `klt gen mos_array`, `diff_pair`, and `esd_device` can now draw
  a minimum-gate-length unit device DRC-clean (issue #1187). Each unit
  device's S/D local-metal pads abutted the poly gate directly, so the
  S-pad-to-D-pad gap was exactly the requested `l_um` — below a curated deck's
  own same-layer metal-spacing rule that made a short-gate device impossible
  to draw cleanly through any combination of params (on sky130A every `l_um`
  in the 0.15–0.17um band, *including* the PDK's own `poly.width.1` minimum,
  failed `li1.space.1` on every unit device). `_mos_finger_positions` now
  inserts `max(0, (SD_PAD_GATE_GAP_MIN_UM - l_um) / 2)` of clearance on each
  side of every gate stripe, where the new `SD_PAD_GATE_GAP_MIN_UM` (0.25um)
  exceeds both curated decks' binding rule (gf180mcu `metal1.space.1`: 0.23um;
  sky130 `li1.space.1`: 0.17um) with margin. That padding grows the unit
  device's own **pitch**, never the gate — `params.l_um` is still drawn
  exactly as requested — so the target PDK's poly minimum-width rule is
  neither helped nor hurt by it, and remains unchecked here. **Geometry
  changes only below `l_um` 0.28um**: at or above `GATE_LENGTH_SAFE_MIN_UM`
  (0.28um, all three generators' own default) the offset is exactly zero and
  the generated GDS is byte-for-byte identical to before, so a caller who
  never lowers `l_um` sees no change at all. `klt gen mos_array`'s
  `drc_hints.notes` advisory for a below-default `l_um` is reworded to match:
  it no longer warns about S/D metal spacing (now handled automatically), only
  about the still-real, still-unchecked poly minimum-width risk. No
  `schema_version` bump — `drc_hints.notes[]` is an existing free-text field
  and no payload changes shape. See [`docs/cli/gen.md`](docs/cli/gen.md)'s
  `mos_array` and `diff_pair` `l_um` parameter rows.
- 2026-08-19 — `klt gen-compose` no longer requires `routing.layer_role`/
  `routing.width_um` whenever `connectivity[]` is non-empty (issue #1188).
  Omitting `routing` entirely (or passing `routing: {}`) is now a
  **declare-only** request: every `connectivity[]` net's `{block, port}`
  pins are still validated against the referenced blocks' own reported
  ports (an unknown block/port is still an application error, exit 1), but
  no metal is drawn. Every net comes back in `nets[]` with
  `status: "unrouted"`, `route_length_um: null`, and each leg's
  `reason: "routing not requested"` — distinct from a geometry-based
  rejection reason — and its label lands in `unrouted_nets[]` (the existing
  partial-success exit code `3`, not `0` or `1`). Supplying **any** key of
  `routing` still opts into routing exactly as before, and both
  `layer_role`/`width_um` remain required at that point — no behavior
  change to the routed path. No `schema_version` bump — additive use of
  existing `nets[].status`/`legs[].reason`/`unrouted_nets[]` fields, not a
  new shape.
- 2026-08-19 — `klt lvs`'s `options.combine_devices: true` path no longer
  flakes between `status: "match"` and `status: "mismatch"` across repeated
  runs against byte-identical inputs anywhere near as often as before (issue
  #1185). Root cause: KLayout's own `Netlist.combine_devices()` groups
  combination candidates using an ordering keyed on process heap addresses,
  not netlist content — not controllable from Python — so a partial-match
  device group (issue #466) could combine cleanly on one invocation and hit
  KLayout's internal-consistency `RuntimeError` on the next, for the exact
  same layout GDS + reference netlist. `_combine_devices_safely` now retries
  the combine, per side, against up to 5 independent netlist copies before
  falling back to the existing graceful-degrade `device.combine_incomplete`
  warning, cutting the ~1-in-5 single-attempt flake rate this issue was
  filed against down to roughly 1-in-30,000. Not provably deterministic —
  the underlying KLayout ordering still cannot be forced — so a residual,
  much rarer flake is still possible; see
  [`docs/cli/lvs.md`](docs/cli/lvs.md)'s `options.combine_devices` and
  `device.combine_incomplete` sections for the full explanation. No
  `schema_version` bump — `device.combine_incomplete`'s `description` text
  now discloses the retry count, an existing free-text field.
- 2026-08-18 — `klt gen-compose`'s bundle-net router (`gen_compose.route_bundle()`,
  #1073) no longer discards a net's already-routable geometry when the net as
  a whole cannot be fully connected. Before, a spanning-tree failure reset
  *every* leg back to `routed: false`, even legs that individually passed
  every routability check — a net with `N` legs where `M < N` were routable
  drew nothing at all. Now the `M` routable legs are drawn and only the
  remaining `N - M` are left unrouted, each with its own `legs[].reason`. A
  new `nets[].status` field (`"routed"` / `"partial"` / `"unrouted"`)
  distinguishes a partially-drawn net from a fully-undrawn one — both still
  report `nets[].routed: false` and appear in `unrouted_nets[]`, so a caller
  reading `status` (rather than counting `legs[].routed`) is required to tell
  them apart. Fully-routable and fully-unroutable nets are unaffected (issue
  #1169). No `schema_version` bump — `nets[].status` is a new, additive
  field; every existing field keeps its prior meaning. See
  [`docs/cli/gen-compose.md`](docs/cli/gen-compose.md)'s "Bundle (>2-pin)
  routing" section and its `nets[]`/`nets[].legs[]`/`unrouted_nets[]` field
  descriptions.
- 2026-08-18 — `klt gen-compose` now **routes around** a placed block sitting
  between a net's two pins instead of reporting the net unroutable (issue
  #1167). `route_two_pin()`'s obstacle-overlap check rejected any backbone
  crossing an unrelated block's bbox, so in a placement row only
  *immediately adjacent* blocks could be wired at all — a real block's
  netlist is not a Hamiltonian path over its devices, so most nets never had
  a chance (issue #1164 measured 0/8, 0/9 and 0/9 nets routed across three
  real gf180mcu blocks). When a backbone is rejected **solely** for crossing
  blocks neither pin sits on, the router now retries the pair on up to two
  alternate *lanes* — a straight run over/under (or left/right of) every
  block in the way, shortest detour first — each routed exactly as a
  caller-supplied `waypoints_um` path is, so all six routability checks
  apply to it unchanged and nothing is waived to make a detour fit. The
  search is bounded on both sides: a lane clears every block it spans (two
  obstacles between the pins cost one lane, not two nested detours) and a
  lane is never itself detoured (one level of recursion, at most two extra
  attempts per net). A backbone crossing one of its own two pins' blocks
  (the same-facing port pair) is still rejected rather than detoured — its
  remedy stays `waypoints_um` (#634) — and a caller-supplied `waypoints_um`
  path is never replaced by a detour. When neither lane is clear the net is
  still reported in `unrouted_nets[]`, with the same reason as before plus a
  note that a detour was tried. No `schema_version` bump: no field changes,
  only which nets come back `routed: true` (with a longer
  `route_length_um`). See `docs/cli/gen-compose.md`'s "Routing around an
  unrelated block" section.
- 2026-08-18 — An anonymous (unlabelled, KLayout-synthesized `$N`
  placeholder) net's name is now backslash-escaped (`\$N`) everywhere `klt
  extract`/`klt lvs` report it — `nets[].name`, `devices[].nets[...]`,
  `parasitics.nets[].net`/`.hub_net`/`.terminals[].leg_net`, and `klt lvs`'s
  `net_correspondence[]`/`mismatches[].net` — matching the escaped spelling
  KLayout's own `NetlistSpiceWriter` already used for that net's *node*
  references in the written `.spice` file (issue #1162). Before this, these
  JSON fields carried the *bare*, unescaped `$N` form while the written
  netlist used `\$N`; ngspice (and the wider SPICE3/HSPICE-descended dialect
  family) treats a token that starts with `$` as an inline-comment marker,
  so a caller who copied one of these fields verbatim into a hand-authored
  SPICE card (a testbench, a `.save`/probe directive, a manual
  instantiation) reproduced a silent per-card truncation `NetlistSpiceWriter`
  itself already avoids internally. A `--critical-net`/`--distributed-rc`/
  `--mom-rlc-net`/`--mom-net` argument naming an anonymous net must now use
  this same escaped `\$N` spelling. No `schema_version` bump on either
  command — field names and types are unchanged; only the string *value* of
  an already-documented field changes, and only for the narrow case of an
  anonymous net, the identical precedent issue #696/PR set for the
  merged-label-net spelling fix. See `docs/cli/extract.md`'s "Anonymous nets
  are backslash-escaped" section.
- 2026-08-18 — `klt functional-verification`'s `options.sdf` transcript gate no
  longer fails a run on a *corrupted* transcript line (issue #1136). Icarus's
  C-level SDF diagnostic output and cocotb's Python logging share one stdout
  file descriptor, so under a non-tty capture (a caller piping `klt`'s output
  without `PYTHONUNBUFFERED=1`) a flush from one can land mid-line inside a
  not-yet-flushed line from the other. Observed live: the tail of a benign
  `SDF WARNING: …: TIMINGCHECK not supported.` line was overwritten by an
  unrelated cocotb log line, taking the `TIMINGCHECK` substring the benign
  exemption keys on with it — so a fully-correct SDF annotation was reported
  as `did not fully apply: 1 diagnostic(s)`. `_scan_sdf_diagnostics` now
  classifies a marker-bearing line only once it also matches Icarus's own
  diagnostic shape (`SDF WARNING:`/`SDF ERROR:` plus a `<file>:<line>:`
  locator); a line carrying the marker without that shape is a splice, not
  evidence, and is counted as neither actionable nor benign. No JSON shape
  change. This cannot mask a real failure: Icarus emits one diagnostic per
  failing SDF entry, so a genuine failure arrives in volume while a splice
  corrupts only the single line the interleaved flush landed in.
- 2026-08-18 — `klt extract`'s NMOS body ("W") terminal is no longer always
  tied to one hardcoded, deck-wide `substrate_net` global regardless of
  physical isolation (issue #1128). `ExtractionDeck.substrate_isolation`
  (unset/`None` by default — every deck as of this field's introduction) is
  an optional isolation/deep-well layer; gf180mcu now declares its `DNWELL`
  (12/0). When set, an NMOS device's active-diffusion island — or a
  substrate-tie tap's slice of geometry (drawn or derived, issue #1084) —
  inside a connected component of that layer resolves to a *per-island*
  synthesized identity instead of the single deck-wide global, letting two
  physically separate, DNWELL-isolated NMOS domains, each with its own real
  substrate tap tied to a distinct net, extract as two genuinely distinct
  nets. Geometry outside every isolation island keeps today's single shared
  identity. Additive: gf180mcu's own existing (non-isolated) corpus fixtures
  are unaffected, since none of them draw `DNWELL`, and every other deck
  leaves the new field unset.
- 2026-08-17 — `klt lvs`'s unmatched-circuit/unmatched-subcircuit-instance
  `mismatches[]` entries (`category: "topology"`) now name what failed to
  pair (issue #1132). Previously both carried neither `net` nor `device` —
  the only two identifier slots the shape had — so an anonymous `topology`
  finding could not be attributed to a circuit or instance without a
  side-channel netlist diff, especially at macro scale (thousands of
  instances across dozens of cell types). Two new optional-object fields,
  additive and `null` off their own categories, mirror `net`/`device`'s own
  convention: `circuit` (`{"layout": <name|null>, "reference": <name|null>}`
  — the circuit itself for an unmatched-circuit entry, or the circuit
  *containing* the instance for an unmatched-subcircuit-instance entry) and,
  for the instance case only, `instance` (the subcircuit instance's own
  name) and `subcircuit` (the name of the circuit it instantiates, its "cell
  type"). All three keys are present on *every* `mismatches[]` entry
  (`null` off their own categories, never omitted) — including the generic
  safety-net entry emitted when the comparer reports a mismatch this module
  could not classify, which is now built through the same entry constructor
  as every other finding rather than as its own literal. Also adds
  `error_count` (total `severity: "error"` entries) and
  `category_error_counts` (`category_counts`, split to `error`-only) so a
  caller can gate on "any real defect present" without re-reading and
  re-filtering `mismatches[]` itself — a nonzero `category_counts` entry can
  otherwise be entirely `severity: "warning"`.
- 2026-08-17 — `klt functional-verification`'s `environment.sdf` (`options.sdf`
  runs) gains `partial` and `dropped` fields alongside the existing
  `file`/`corner`/`annotated` (issue #1102). Previously `annotated: true`
  reported two materially different outcomes identically: "every delay and
  every timing check in the SDF applied" and "every `IOPATH` applied and
  every `TIMINGCHECK` section was dropped" — the normal case on Icarus,
  which implements SDF delay annotation but no SDF `TIMINGCHECK` support at
  all, so `$setup`/`$hold`/`$width` checks that ran during the regression
  used the cell library's own placeholder timing, not the characterised
  limits in the SDF. `_scan_sdf_diagnostics` already read both transcripts
  and silently discarded the benign `TIMINGCHECK` lines it filtered out of
  the hard-failure gate; it now also counts them per class. `partial` is
  `true` whenever any benign diagnostic class was filtered, and `dropped`
  names each such class with `{count, reason}` — `false`/`{}` on a run
  where every class in the SDF applied cleanly. Additive; `annotated`'s own
  meaning is unchanged.
- 2026-08-17 — `klt equiv`'s multi-source `provenance.input.content_hash`
  (both `gold`/`gate` sides combined pass more than one file total) now uses
  the same path-independent digest scheme `klt synthesize` already used for
  its own multi-source case, instead of an independently-copied
  implementation that mixed each file's path into the hash (issue #1112).
  The two had silently diverged: given identical file contents, `klt equiv`
  and `klt synthesize` previously reported *different* `sha256:` values for
  the same documented `provenance.input` field
  (`docs/json-contract.md`'s "Reproducibility provenance" section) — a
  committed report's hash was not comparable across the two commands. Both
  now share one `_combined_content_hash` implementation in
  `klayout_tools/_provenance.py`; the single-source case (`build_provenance`'s
  `input_path`) is unaffected.
- 2026-08-17 — `klt extract --deck gf180mcu --pdk ...` now binds a MOS
  device drawn (fully or partially) inside `Dualgate` (55/0) to the real
  **5V/6V models** (`nfet_06v0`/`pfet_06v0`) instead of the default 3.3V
  ones (`nfet_03v3`/`pfet_03v3`), closing the MOS-extraction half of #552
  (issue #1111, option 2). `ExtractionDeck` gains an optional
  `mos_flavours` field (a tuple of `MOSFlavour` entries, empty for every
  other deck) declaring a marker-scoped MOS voltage flavour; gf180mcu
  declares one keyed on `Dualgate`. `devices[].class`/`device_counts` are
  **unaffected** — every MOS device still reports the deck's ordinary
  `"nfet"`/`"pfet"` class regardless of flavour, so LVS device-class
  matching, `klt lvs`'s reference-netlist `subckt-call` conversion
  (`known_mos_subckt_names`/`build_subckt_to_class_map`, both now also
  resolve `nfet_06v0`/`pfet_06v0` back to the base `nfet`/`pfet` class),
  and every other structural-netlist consumer are unchanged; only the
  `--pdk`-bound SPICE model name differs. `voltage_domain_warnings` no
  longer fires for MOS device geometry inside `Dualgate` (the gap it
  flagged for MOS is now closed) — the registry entry itself stays
  (gf180mcu's DRC-rule residue beyond `DF.1a`/`DF.3a`, e.g. `DF.6`,
  `PL.5a`/`PL.5b`, is unaffected and still flagged both by `klt drc` and by
  this field's description text). A transistor whose active geometry only
  partially overlaps `Dualgate` is claimed entirely by the flavour (a
  documented policy — the DRM does not contemplate a transistor legally
  straddling a voltage-domain boundary).

- 2026-08-17 — `klt drc --deck gf180mcu` now enforces the **5V/6V (`_MV`)
  thresholds** on `Comp` geometry drawn inside `Dualgate` (55/0) instead of
  checking every `Comp` shape against the 3.3V (`_LV`) column (issue #1110,
  closing the DRC half of #552). `DF.1a` and `DF.3a` ship as rule *pairs*:
  `comp.width.1` (0.22 um) / `comp.width.mv.1` (0.30 um) and
  `comp.space.1` (0.28 um) / `comp.space.mv.1` (0.36 um), each half scoped
  to whole `Comp` polygons that do/do not touch `Dualgate` — the PDK's own
  `comp_3p3v`/`comp_56v` derivation, transcribed from
  `rule_decks/comp.drc`. #552's reproducer (a 0.25 um `Comp` stripe fully
  inside `Dualgate`) now reports a `comp.width.mv.1` violation instead of
  `status: "clean"`. Geometry outside `Dualgate`, and every layout that
  draws no `Dualgate` at all, is checked exactly as before. **New JSON
  values, not new fields**: two new `rule_counts`/`violations[].rule` ids
  (`comp.width.mv.1`, `comp.space.mv.1`), `55/0` now appears in
  `coverage.deck_layers`/`layers_checked` (and no longer in
  `layers_in_stream_without_rules`) for gf180mcu, and
  `coverage.voltage_domain_warnings`'s gate is now per rule — it no longer
  fires for geometry the two split pairs checked, still fires for every
  rule that ignores the marker, and its `Dualgate` description was rewritten
  to name what remains unmodelled. Deck-internal: `DerivedLayer` gains a
  `mode` field (`"sized_intersection"`, the previous and default behavior,
  plus `"overlapping"`/`"not_interacting"`).

- 2026-08-17 — `klt lvs` can now compare a `klt extract`-derived (always
  flat) layout netlist against a **hierarchical** reference netlist — one
  leaf `.subckt` plus N instance calls of it, the shape a macro built by
  tiling one verified leaf cell naturally takes (issue #1085). Previously
  this was an unconditional `topology` "circuit could not be matched to a
  counterpart" mismatch on both sides: `NetlistComparer` pairs circuits
  one-for-one, and the flat layout side has no subcircuit-call circuit to
  pair against the reference's, so the compare declined to even attempt a
  per-net/per-device verdict. New opt-in `options.flatten_reference` /
  `options.flatten_layout` booleans (default `false`, unchanged behavior)
  call KLayout's own `Netlist.flatten()` in-process on the named side
  before comparing, collapsing its subcircuit-call hierarchy into its top
  circuit(s) so it becomes directly comparable against an already-flat
  netlist on the other side. Each side actually flattened is disclosed as a
  new `severity: "warning"`, `category: "topology.flattened"`
  `mismatches[]` entry, so a `"match"` reached after an opted-in flatten is
  never silently indistinguishable from one reached against the netlist's
  original hierarchy. See `docs/cli/lvs.md`'s `options.flatten_reference`
  and `"topology.flattened"` sections.
- 2026-08-17 — `klt yield`'s spec `limits` (`min`/`max`) were inclusive-only,
  so a spec row ratified as a strict inequality ("must be strictly positive")
  had to be transcribed as `min: <epsilon>` — no longer a literal copy of the
  spec, and silently wrong for anyone who forgot the epsilon (issue #1083).
  `limits` gains optional `exclusive_min`/`exclusive_max` booleans (default
  `false`) that make the corresponding bound a *strict* comparison (`>`/`<`
  instead of `>=`/`<=`); declaring one with no matching `min`/`max` value is a
  validation error rather than silently ignored. Additive and backwards
  compatible — no `schema_version` bump. The human-readable report now shows
  the comparison operator (`min(>)=0` vs `min(>=)=0`) so the strictness is
  visible, not just present in the JSON; see `docs/cli/yield.md`'s `limits`
  shape table for the worked example.
- 2026-08-17 — `klt yield` no longer reports a conditional yield as if it were
  unconditional (issue #1082). An errored sample never entered the yield
  denominator, so a campaign whose failure mode is *"no measurement"* — an
  extraction only defined in-regime, a search that reports "no operating point
  in range", a `.meas` that does not trigger — could report a perfect yield
  with a high `errored` count sitting next to it, unremarked. Any non-zero
  `errored` (no threshold to cross) now adds a per-measurement warning naming
  the errored fraction, stating the estimate is conditional on the `n` samples
  that produced a value, and giving the whole-draw yield that counting every
  errored sample as a failure would produce; a matching run-level warning
  points a reader at it. A measurement where *every* sample errored still
  fails the `min_samples` floor, but the error now names the errored count
  instead of reporting the draw as merely small. Additive to the existing
  `warnings` arrays — **no `schema_version` bump** (still `1`). See
  `docs/cli/yield.md`'s "Errored samples and conditional yield"; counting
  errored samples as failures (an `errored_policy`, or a second yield block
  over the whole draw) remains out of scope and separately tracked.
- 2026-08-16 — `klt functional-verification`'s `options.sdf` no longer fails
  with `SDF ERROR: ... Could not find intermodpath!`/`Could not find net` on
  a post-route SDF whose `INTERCONNECT` entries touch a bare top-level port
  (issue #1056) — the common case, since every primary input/output net
  produces exactly this shape. Icarus cannot resolve a bare port identifier
  against a module elaborated as its own `-s` root at all, only against one
  nested as a child instance of another root; a generated transparent
  pass-through wrapper now instantiates the DUT as that nested child (see
  `docs/cli/functional-verification.md`'s "How the annotation is wired").
  `request.parameters` combined with `options.sdf` is now rejected up front
  (exit 1) rather than silently targeting the wrapper instead of the real
  DUT, since cocotb's Icarus parameter-override syntax always addresses
  `hdl_toplevel`, which the wrapper now is.
- 2026-08-16 — `klt signoff --manifest`/`--fleet` now work from a packaged
  install (issue #1050). Both modes parse
  `docs/design-evidence-tiers.md` at runtime, but resolved it three
  directories above the installed module — a path that only exists in a
  source checkout, so a `pip install`/`uv tool install` failed outright with
  `could not read design-evidence-tiers doc at '<prefix>/lib/python3.X/docs/…'`.
  Built wheels now bundle the doc as package data
  (`klayout_tools/data/design-evidence-tiers.md`, force-included from the
  canonical `docs/` copy) and the default resolves against the installed
  package, falling back to the checkout's `docs/` for editable installs.
  Two overrides were added for consumers vendoring their own copy: the new
  `klt signoff --tiers-doc PATH` flag and the `KLT_TIERS_DOC` environment
  variable (flag wins). `source_doc` in both reports still reads
  `"docs/design-evidence-tiers.md"` for the shipped doc regardless of
  install layout, and names the override path when one is used.
  Envelope-aggregation mode (`klt signoff <file>...`) never read the doc and
  is unchanged.
- 2026-08-16 — `klt drc`'s `gf180mcu` deck no longer reports `mim.space.1`
  against ordinary `Metal4` routing or PDN power-stripe geometry (issue
  #1033). The rule transcribes the DRM's `MIMTM.1`, whose own scope is the
  MiM capacitor's "virtual bottom plate" (`FuseTop` oversized by 1.06um,
  intersected with `Metal4`), but it was implemented as a general
  whole-layer `Metal4`-to-`Metal4` `"space"` check — so any two `Metal4`
  shapes closer than 1.2um tripped it even on a design with zero MiM
  capacitors (confirmed in practice on an OpenROAD-routed digital block
  with a `Metal4` PDN grid: 188 violations, none MiM-related). It is now
  scoped to that derived virtual bottom plate via the same `DerivedLayer`
  primitive `mim.enclosing.via4.1` has used since #345, expressed as a
  `"separation"` check between the plate and the rest of `Metal4` (`"space"`
  is a single-region primitive and could not express it). `run_drc()`
  additionally excludes, wholesale, any `other_layer` polygon that overlaps
  the raw unsized `base` region, so a plate straddling the sizing cutoff
  does not report a spurious zero-gap violation against its own leftover
  fragment — and, since that exclusion also removes a *neighbouring* MiM
  cap's plate metal, measures `MIMTM.1`'s "whether adjacent MiM" half
  separately as a peer-to-peer `isolated_check` among those excluded
  plate-bearing polygons, reported under the same rule id (so the fix
  narrows false positives without dropping DRM coverage).
  A design with no `FuseTop` shapes now skips the rule entirely
  (`coverage.rules_skipped`) instead of emitting false positives; genuine
  `MIMTM.1` violations against a real MiM bottom plate are still reported.
  The rule's `check` field changes from `"space"` to `"separation"` and its
  `description` is restated (both are per-violation payload *values*, not
  response-shape changes; `schema_version` unaffected). See
  `docs/cli/drc.md` and `tests/test_drc.py`'s two new negative controls.
- 2026-08-16 — `klt drc`'s `gf180mcu` deck does not false-positive on a
  correctly-abutted standard-cell row (issue #1028); the false positives
  originally reported were verified to be an artifact of the reproduction
  script's own placement pitch, not an engine defect. Reproducing the
  issue against a row of the real `gf180mcu_fd_sc_mcu9t5v0__and2_1` corpus
  cell (`tests/corpus/gf180mcu/`) abutted at the cell's own true 4.48um
  cell-outline pitch reports `status: "clean"` for all four rules the
  issue named (`comp.enclosing.contact.1`, `contact.space.1`,
  `metal1.space.1`, `poly2.space.1`) — `comp.enclosing.contact.1` via the
  same `_run_check` `.merged()` fix #995/#998 already made, and the three
  `*.space.1` rules (a different dispatch path, `_SINGLE_LAYER_CHECKS` /
  `Region.space_check`, not touched by #995/#998) via `space_check`'s own
  `merged_semantics` already tolerating an exact zero-gap abutment seam —
  no `_run_check` code change was needed. Reproducing the issue's own
  literal reproduction script instead (a flat `2.8` um assumed pitch,
  narrower than `and2_1`'s real 4.48um width) reproduces its exact reported
  rule counts — real, correctly-detected violations from a corrupted
  (overlapping) placement, confirming the original script's pitch was
  itself the bug. A genuine sub-dbu residual gap between otherwise
  correctly-pitched instances still reports a real `metal1.space.1`
  violation (the shared power/ground rail's real facing-edge spacing), by
  design — not loosened, since a real place-and-route flow leaving such a
  gap has a genuine rail discontinuity. See `docs/cli/drc.md`'s "gf180mcu
  standard-cell row abutment (#1028)" for the full writeup and
  `tests/test_drc.py`'s three new `row_abutment_*` regression tests. No
  response-shape change; `schema_version` unaffected.

- 2026-08-16 — `_merge_def_to_gds` (the DEF/GDS merge step behind place-and-
  route flows) now resolves the tech LEF's own declared `DATABASE MICRONS`
  and configures KLayout's DEF reader to match, instead of silently
  inheriting KLayout's compiled-in default DBU (issue #1032). Left unset,
  `kdb.LoadLayoutOptions().lefdef_config.dbu` defaulted to `0.001`
  (`DATABASE MICRONS 1000`), which happens to match sky130's tech LEF but is
  wrong for any PDK declaring a different value (e.g. gf180mcu's `2000`).
  KLayout's DEF reader never raises on this mismatch — it only logs a `DEF
  UNITS does not match reader DBU` warning — so merged via-cut geometry was
  silently dropped or misplaced. The fix reads `database_microns` via the
  existing `read_lef_header()` helper and sets `dbu = 1.0 /
  database_microns` before `main_layout.read(def_path, opts)`; when the tech
  LEF doesn't declare a `DATABASE MICRONS` value, the merge falls back to
  KLayout's prior default DBU behavior unchanged. sky130 output is
  bit-identical (the resolved default is the same `0.001`); no response
  shape changed, `schema_version` unaffected.

- 2026-08-16 — `klt pex` no longer silently swaps the wrong `.include` line,
  and now names a schematic/extracted pin-list mismatch instead of burying it
  in a per-corner log (issue #1030). Two fixes to the DUT `.include` swap
  (`docs/cli/pex.md`'s "The DUT `.include` swap"): (1) a testbench body with
  **more than one** `.include`/`.inc` directive is now a hard `PexError`
  naming every matched line and its 1-based line number, raised in the same
  up-front pass that already rejects a testbench with none — previously
  `_find_dut_include` returned the *first* match, so a testbench that also
  includes a PDK's global switch-parameter file (gf180mcu's
  `design.ngspice`) ahead of its DUT had that file re-pointed at the
  extracted netlist, left the real schematic DUT in place on both sides, and
  reported the wrong `reference_netlist`, with no error or warning. (2) When
  the extracted-side deck is refused by ngspice for a top-level pin-count
  mismatch (`Too few/many parameters for subcircuit type "<name>"` — the
  routine outcome whenever a deck's extraction promotes a
  device-body/substrate-tap net the hand-written schematic subcircuit does
  not declare), `klt pex` now reports it as a new **additive** top-level
  `pin_count_mismatch` field (both sides' `.SUBCKT` pin lists and counts,
  ngspice's own line when a per-corner log was kept, and a `detail`
  sentence) alongside per-corner `delta[]` rows with a `null`
  `extracted_value` — exit `4`, a full JSON report — rather than only a
  generic `status: "error"` whose real cause lived in the per-corner
  `ngspice.log` artifact. `pin_count_mismatch` is `null` on every run whose
  extracted side simulated, and the detection only runs when the extracted
  side produced no measured value at all, so a passing run cannot pick up a
  false positive. Bridging a genuine interface mismatch (a caller-supplied
  pin map, a wrapper subcircuit) remains deliberately out of scope. At the
  time this landed, detection was only as good as the engine's own refusal
  to elaborate the deck: ngspice 46 rejects any pin-count difference, but
  ngspice 42 (what `apt` ships, and what CI installs) silently accepts every
  "too many parameters" case *and* a one-pin "too few" shortfall, simulating
  on with dangling terminals — so on ngspice < 46 such a run still reported
  `status: "pass"` with `pin_count_mismatch: null`. **Superseded by issue
  #1041 (below):** detection is now a static, engine-independent pre-flight
  check that no longer depends on which ngspice version is installed; the
  version-dependent reactive detection this entry originally described
  remains only as a fallback for what the pre-flight check cannot see. No
  response-shape break; `schema_version` unaffected.

- 2026-08-14 — `klt drc` no longer reports false-positive `"enclosing"` /
  `"enclosed"` violations when a checked layer is drawn as several abutting
  (touching, non-overlapping) shapes rather than one merged polygon (issue
  #995). `run_drc` built each checked `Region` straight from the raw shape
  iterator, and `Region.enclosing_check`/`enclosed_check` measure the
  *primary* region's raw polygon edges — so a cut sitting close to an
  internal seam between two touching shapes was measured against that seam
  instead of the merged region's real outer edge. `_run_check` now merges
  both regions before dispatching to any check primitive. This only removes
  false positives: a merged region covers the same area with weakly fewer
  edges, so a genuine shortfall still reports. Measured on the committed
  `tests/corpus/place_and_route/gcd.gds.gz`, whose four
  `diff.enclosing.licon.1` violations were previously documented as real
  row-gap geometry: all four were this false positive (every reported edge
  pair exactly 25 dbu wide, inside a single `sky130_fd_sc_hd__and3_1`
  instance whose own `diff` is drawn as two abutting rectangles, enclosed by
  ~925 dbu of the merged region), and both machine-generated corpus fixtures
  (`gcd`, `mult8`) now report `"status": "clean"`. No response-shape change;
  `schema_version` unaffected.
- 2026-08-14 — `klt place-and-route`'s `post_route_spef` SPEF no longer
  declares every routed net a top-level design port (issue #961 defect 1,
  Epic #700 Phase 3). `--def-net-names` (#951) gives every routed net a real
  name, and flat extraction's `Netlist.make_top_level_pins()` promotes every
  *named* net to a top-level circuit pin — so the written SPEF's
  `*PORTS`/`*P` list announced ordinary internal nets (`*P _019_ B`) to
  `read_spef` as design-boundary ports. `_post_route_spef_metrics` now scans
  the routed DEF's own `PINS` section — the DEF's own statement of which nets
  are genuine design I/O, unaffected by the net-name renaming — and passes it
  to `klt extract` as `declared_pins` (the pre-existing `--pins` mechanism,
  issue #514), demoting every promoted pin outside that set. Measured on the
  committed `tests/corpus/place_and_route/gcd.gds.gz`: `*PORTS` entries drop
  from **463 to 54** (that design's 52 I/O plus `VPWR`/`VGND`) while the
  `*D_NET` set stays bit-for-bit identical at 1392 blocks, so #951's
  `537 / 537` net-name annotation ratio is untouched. A DEF whose `PINS`
  section is absent or unparseable leaves `declared_pins` unset (`klt
  extract`'s pre-#961 behaviour) rather than declaring the design portless.
  No response-shape change; `schema_version` unaffected. **This does not
  close the larger gap** — `read_spef` still discards every RC network,
  because device-terminal (`*I <inst>:<pin>`) connectivity is still not
  emitted at all and the `*CAP`/`*RES` node names still resolve to no pin in
  the linked design, so `worst_slack` remains bit-identical across
  `read_spef`; that remainder (plus coupling-`*CAP` node naming) stays
  tracked by #961 itself, which this entry only partially closes. See
  `docs/cli/place-and-route.md`'s "`*PORTS` lists only real design ports"
  subsection.

- 2026-08-12 — `klt synthesize` now maps constant drivers onto real
  tie-high/tie-low standard cells, unblocking `klt place-and-route` for
  designs that need constant ties (issue #854). Yosys's `synth`/`abc` passes
  leave `1'b0`/`1'b1` constants as bare Verilog literals (`assign q[5] =
  1'h0;`, `.D(1'h1)`); OpenSTA's Verilog reader materialises one net per
  constant value when OpenROAD reads such a netlist (conventionally `zero_`
  and `one_`), OpenROAD types those nets `GROUND`/`POWER`, and TritonRoute
  then aborts the whole `route` stage with `[ERROR DRT-0305] Net zero_ of
  signal type GROUND is not routable by TritonRoute.` The generated `.ys`
  script now runs Yosys's `hilomap` pass — between `clean` and `stat`, so
  the inserted tie cells are counted in `instance_count`/`area_um2` — using
  the resolved `pdk.cell_library`'s own verified tie cells
  (`sky130_fd_sc_hd__conb_1` HI/LO; `gf180mcu_fd_sc_mcu9t5v0__tieh` Z /
  `__tiel` ZN — ORFS's own `TIEHI_CELL_AND_PORT`/`TIELO_CELL_AND_PORT` for
  each platform, cross-checked against the installed liberty). A
  `cell_library` with no tie-cell table entry emits no `hilomap` line at
  all, keeping its script unchanged, and a design that needs no constant
  tie (the repo's own `gcd.v`) synthesizes to a byte-identical netlist. No
  response-shape change; `schema_version` is unaffected. Additionally, `klt
  place-and-route` now *diagnoses* `DRT-0305` instead of passing it through:
  the error message names the offending net and explains the tie-cell fix,
  rather than surfacing only OpenROAD's own Tcl line-number summary
  (`Error: pnr_<top>_route.tcl, 6 DRT-0305`) — which is what a caller saw,
  because the informative line goes to stdout while the useless one goes to
  stderr.
- 2026-08-12 — `klt mom`'s PEEC partial self-inductance
  (`inductance_matrix_nh[j][j]`) no longer over-predicts by a systematic
  ~0.3% (issue #836). Each filament's self term previously substituted an
  equal-area circle's self geometric mean distance into the thin-wire mutual
  -inductance formula; it is now computed exactly, via Hoer & Love's closed
  form for the partial inductance of a rectangular bar against itself
  (`native/mom/src/peec.rs`'s `self_partial_inductance_nh`). Measured against
  the mean-distance asymptote for a square cross-section, the five bar
  geometries this issue cited went from a +0.24–0.37% systematic
  over-prediction to `< 0.05%` residual (dominated by the mutual-term
  bundle-averaging approximation and the asymptote's own remainder, not the
  self term). No response-shape change — `inductance_matrix_nh`'s field
  shape and `schema_version` are unaffected, only the numeric values it
  reports. See `docs/design/mom-validation.md`'s "Inductance/resistance"
  section for the full measured validation.
- 2026-08-12 — `klt synthesize` now reports `instance_count` /
  `instance_counts_by_type` as a **recursive rollup over the whole design
  hierarchy** (issue #821). Both fields were read straight out of
  `stat -liberty … -json`'s `modules["\<top>"]` block, which describes only
  the top module itself: for any design whose top instantiates sub-modules
  Yosys's default `synth` leaves un-flattened, `instance_count` was the top
  module's own direct-cell count (`0` for a pure wrapper — the reported
  `mac8` case, whose true total is 302) while `area_um2` in the same block
  was already the correct hierarchical rollup, so the two silently disagreed
  about what "the design" meant. `instance_counts_by_type` was worse than
  empty: `stat` lists each un-flattened sub-module *instance* as a pseudo
  cell type in its parent's block, so the map reported sub-module names
  (`adder16`, `mult8`) as if they were standard cells. The counts are now
  aggregated by walking the full `modules` dict, expanding every sub-module
  entry into the real leaf standard cells it instantiates and scaling each
  level by that level's own instance count — correct for 3+ levels of
  hierarchy and for a sub-module instantiated more than once under one
  parent, and verified to reproduce `stat`'s own `design` rollup exactly on
  live Yosys output. The aggregation reads instance multiplicity from
  `num_cells_by_type` rather than a parent's own `num_cells`, because
  whether `num_cells` counts sub-module instances is Yosys-version-dependent
  (0.33 counts them, 0.68 reports them separately as `num_submodules`).
  `area_um2` / `sequential_area_um2` are unchanged (already recursive), and a
  single-module design — every existing fixture, `gcd` included — reports
  exactly the same numbers as before. Consumers that treated
  `instance_count` as a design-size proxy (`klt eval`'s `synthesize`
  threshold gate, which a wrong `0` trivially satisfied; `digital_fleet`'s
  instance-sizing ladder) now see the true gate count.

- 2026-08-11 — `klt extract --mom-net <net>` now resolves the net whose
  capacitance it overwrites by **net id**, not by name (issue #811). The
  solver picked its net object by walking `Circuit.each_net()` and taking the
  first name match, while the `parasitics.nets[]` entry to overwrite was
  looked up by name a second time against a `(net, net_id)`-sorted list —
  two independent lookups that are only guaranteed to select the same net
  island as long as KLayout's (undocumented, and demonstrably non-monotone —
  a net rescued by the purge pass is recreated at the end of the circuit's
  net list) iteration order happens to agree with net-id order. For a
  `--mom-net` label shared by several genuinely distinct, un-strapped islands
  (the `gcd` corpus block has 105 same-labelled `VGND` islands, 88 `VPWR`)
  that could have written one island's field-solved capacitance onto a
  different island's SPICE `C` card, with `lumped_rc_capacitance_ff` /
  `delta_ff` comparing two different pieces of geometry. The solve now
  deterministically picks the **lowest-`net_id`** island among the matches
  (the first entry carrying that name in `parasitics.nets[]`), reports which
  one it solved in the new additive `parasitics.mom_crosscheck.net_id` field,
  warns that the name matched several islands, and threads that id through to
  the swap. A `--mom-net` name matching a single net island — including
  #798's own `Y`/`sky130_fd_sc_hd__inv_1` acceptance case — reports exactly
  the same numbers as before.

- 2026-08-11 — `klt gen mos_array`'s `finger_topology="series"` (issue #777's
  deferred option 1, follow-up to #780's `"parallel"` default) now pads
  *every* gate finger and reports *every* terminal instead of only the two
  end S/D segments and the first finger's gate: for `fingers > 1` the unit
  reports the `fingers + 1` S/D segments as `U<i>_S<j>`/`U<i>_D<j>`
  (alternating — segment `2j` is `S<j>`, segment `2j + 1` is `D<j>`) and each
  finger's gate as `U<i>_G<j>`, with interior segments reporting
  `direction_deg: 270` and the gate pad's own width instead of `w_um`.
  `device_count` is now `rows * cols * fingers` in this mode, matching what
  `klt extract` reports back (`"parallel"` mode's `device_count` is
  unaffected). Previously the interior segments and gates had no reported
  port and no landing pad, so they extracted as permanently floating gates
  — `"series"` still surfaces an informational `drc_hints.notes` entry
  describing the chained-transistor shape, but no longer a `warnings[]`
  entry, since every terminal is now reachable. `fingers=1` output is
  byte-for-byte unchanged under either topology.

- 2026-08-11 — `klt gen mos_array`'s `fingers > 1` now draws the
  conventional **parallel** multi-finger device instead of an unstrapped
  series chain (issue #777). Previously the generator drew N gate stripes
  over a shared diffusion and stopped there: `klt extract` read that back as
  N transistors chained source-to-drain on N *floating* gate nets, and the
  shape could not be repaired from outside — the interior S/D segments had
  no reported ports and the interior gates had no landing pad, so nothing
  could contact them. It now straps the alternating S/D segments to a source
  rail below the diffusion and a drain rail above it, and runs every gate
  stripe up into a shared poly comb, so a `fingers=N` unit is one device of
  width `N * w_um` (N transistors between the same two S/D nets on one gate
  net, which `klt lvs`'s `options.combine_devices` folds into that single
  device). A new `params.finger_topology` (`"parallel"`, the default, or
  `"series"`) keeps the old unstrapped shape available for a caller that
  intends to strap the stripes itself — and `"series"` now emits a
  `warnings[]` entry stating that the interior terminals are unreported and
  uncontactable, rather than letting that surface in an LVS diff. This moves
  the reported `U<i>_S`/`U<i>_D`/`U<i>_G` port coordinates (they sit on the
  rails and the comb) and grows the unit device's height for `fingers > 1`;
  `fingers=1` — the default, and every existing consumer of it — is
  byte-for-byte unchanged, as are `diff_pair` and `esd_device`, which keep
  the unstrapped unit-device helper.

- 2026-08-11 — sky130's two curated MiM-capacitor entries (`klt extract`'s
  `sky130_fd_pr__model__cap_mim`/`..._cap_mim_m4`) now declare
  `top_plate_via`/`top_plate_via_metal` (`via3`→`met4` for the met3/`capm`
  stack, `via4`→`met5` for the met4/`capm2` stack, issue #775) — the same
  mechanism gf180mcu's MiM stack already used (issues #314/#364). Before
  this, a MiM cap's top plate drawn with a real, DRM-legal landing via was
  either read as a false short between the two plates (the via's overlap
  with the bottom plate merged into the deck's generic per-layer
  connectivity) or, with no via declared at all, left the top plate an
  orphaned, single-terminal net — neither is a usable two-terminal
  capacitor for `klt lvs`. Fixed alongside a latent bug in the shared
  `#364` false-short exclusion this change surfaced: for a deck whose
  `bottom_plate` is not clipped to the top plate's own footprint
  (`bottom_plate_oversize_um == 0`, sky130's case), the exclusion now also
  skips capacitors whose `top_plate` marker is not drawn anywhere on the
  layout — without this, declaring `top_plate_via` on a widely-used via
  layer (`via3`/`via4`, ordinary sky130 signal routing) excluded nearly
  every legitimate via on that layer from connectivity in any digital/macro
  design that draws no MiM cap at all, a false disconnect across the whole
  design. gf180mcu's stack never hit this because its nonzero
  `bottom_plate_oversize_um` derivation already gates `bottom_region` on
  `top_region`'s presence.

- 2026-08-11 — A merged-label net (two drawn text labels shorted onto one
  electrical net, issue #470) now has exactly one spelling everywhere `klt
  extract`/`klt lvs` name it (#696). KLayout's own `Net.expanded_name()`
  joins the labels with a comma (`Y,Y2`) — but a SPICE node token cannot
  carry a comma, so the *written netlist*'s `.SUBCKT`/instance lines already
  used the `|`-joined escape (`Y|Y2`) `NetlistSpiceWriter` writes instead.
  Every net name this repo put into JSON (`klt extract`'s `nets[].name`,
  `devices[].nets[...]`, `merged_net_labels[].net`, `parasitics.nets[].net`;
  `klt lvs`'s `net_correspondence[]`/`mismatches[].net`) used the raw,
  un-escaped comma form, so the same net was spelled two different ways
  depending which artifact you read it from — a caller joining `klt
  extract`'s JSON to its own written netlist by net name hit a `KeyError` on
  exactly the nets where two labels legitimately named one node. Every net
  name this repo reports is now rewritten through a shared
  `spice_safe_net_name` helper to the netlist's own `|`-joined spelling
  before it reaches the response, so it is a usable key into the netlist
  rather than a separately-spelled alias of it. No `schema_version` bump on
  either command — field names and types are unchanged; only the string
  *value* of an already-documented field changes, and only for the narrow
  case of a label-merged net. See docs/cli/extract.md's "Merged net labels"
  and docs/cli/lvs.md's `net_correspondence[]` entries for the documented
  spelling.

- 2026-08-11 — `klt gen-compose`'s `placement.strategy: "explicit"` path now
  warns when a block is placed closer to a neighbour than that neighbour's
  own declared `generator_report.drc_hints.min_spacing_um` (#692). Before
  this, `"explicit"` performed no clearance check at all: a block placed
  flush against (0um clearance from) a `guard_ring`-generated neighbour
  composed without complaint, and `klt drc` reported the result clean —
  two same-layer shapes placed with zero clearance merge into one polygon,
  which is not a spacing violation by any rule (there's no gap left to
  measure once they're unioned), so the resulting short surfaced only
  downstream via `klt extract`'s `merged_net_labels` diagnostic. `compose()`
  now appends one `warnings[]` entry per offending ordered block pair naming
  both blocks, the declared minimum, and the actual clearance found; this is
  advisory only (composition still succeeds, geometry stays advisory
  exactly as documented) and scoped to `"explicit"` placement only — `"row"`
  placement's own uniform `spacing_um` does not have the same silently-flush
  ergonomics trap and gains no new warnings. See `docs/cli/gen-compose.md`'s
  "Explicit placement" section.

- 2026-08-11 — `klt gen guard_ring` (and every generator that composes its
  ring drawing: `diff_pair`, `bjt_array`, `esd_device`): a `contacts_per_side`
  value `_guard_ring_validate` accepted as geometrically fine could still
  draw a DRC-violating ring, with no signal in the response (#685). Root
  cause: `_ring_layout` built each tap contact's box from an un-snapped float
  centre (`cx - half`, `cx + half`); `_insert_boxes`'s later, independent
  per-edge `int(round(x / dbu))` conversion could then round one edge up and
  the other down when the un-rounded edge fell within float noise of a
  half-dbu grid boundary, silently drawing a contact 1 dbu narrower/shorter
  than `CONTACT_SIZE_UM` and tripping gf180mcu's `contact.width.1` (verified:
  `contacts_per_side=3` and `=7` on the issue's `78.91 x 4.75um` repro
  region, while `=2` and `=4` stayed clean). Contact boxes are now built by
  snapping the centre to the manufacturing grid first (`_snap_square_box_um`),
  so both edges derive from the same rounded integer and can never drift
  apart — every `contacts_per_side` `_guard_ring_validate` now accepts draws
  a full-size, DRC-clean contact. `_guard_ring_describe`'s existing
  `CONTACT_GAP_SAFE_UM` advisory note (the mechanism that already caught the
  issue's other failure mode, a genuine near-limit `contact.space.1` case) is
  now applied per-axis too, so it still flags a tight resolved contact count
  on either axis independently. See `docs/cli/gen.md`'s `guard_ring` section.

- 2026-08-10 — `klt extract`: a material, non-empty `ignored_layers` result
  (any entry with `shapes > 0` — which is every entry, since empty layers
  are filtered before they reach this field) now also appends a single
  aggregate `warnings[]` entry naming the affected layer(s) and their total
  shape count (#666). Before this, `ignored_layers` was diagnostic-only: a
  net routed on a metal level the deck's connectivity graph does not read
  extracted "successfully" with no signal in `warnings[]`, the one field a
  caller checking only the minimal self-check every `klt` command output
  documents would see. **Behavior change**: a layout that previously
  extracted "clean" (no `warnings[]` entries) with a non-empty
  `ignored_layers` now also emits a `warnings[]` entry for it — any test or
  tooling that asserted an empty `warnings[]` purely because it never
  inspected `ignored_layers` should be re-checked. `device_recognition_only_
  layers` (#619) is unaffected — it intentionally still does not mirror into
  `warnings[]`, unchanged from the 2026-08-08 entry below.
- 2026-08-09 — `klt place-and-route`: `_ROUTING_LAYER_RANGE`'s
  `gf180mcu_fd_sc_mcu9t5v0` entry was `"Metal1-Metal5"`, one layer wider at
  the bottom than the platform allows — OpenROAD was told it could route
  signals on `Metal1`, which is where that library's standard cells put
  their own pins (`buf_4`'s `I`/`Z` are both `LAYER Metal1`). Corrected to
  `"Metal2-Metal5"`, matching `MIN_ROUTING_LAYER ?= Metal2` /
  `MAX_ROUTING_LAYER ?= Metal5` in OpenROAD-flow-scripts' own
  `platforms/gf180/config.mk` — the same primary source the
  `sky130_fd_sc_hd` entry's `"met1-met5"` comes from (sky130hd legitimately
  starts at `met1`: its cells pin out on `li1`, below `met1` entirely).
  This narrows the range, so it does not reintroduce #619's
  routing-range-wider-than-extraction-connectivity failure mode:
  gf180mcu's `EXTRACTION_DECK` already covers the full Metal1-Metal5 stack
  (#220), a superset. The provenance comments on both tables were also
  wrong and are corrected: OpenROAD-flow-scripts **does** ship a `gf180`
  platform whose defaults (`TRACK_OPTION ?= 9t`, `POWER_OPTION ?= 5v0`)
  resolve to exactly `gf180mcu_fd_sc_mcu9t5v0`, and no `CTS_BUF_CELL`
  variable exists anywhere in that repo — neither platform pins a CTS
  buffer at all (`CTS_BUF_LIST` is optional and unset in both), so each
  `_CTS_BUFFER_CELLS` entry now records its actual source. No behavior
  change for `sky130_fd_sc_hd` (#637).
- 2026-08-08 — `klt extract`: sky130's `EXTRACTION_DECK.metals`/`.vias`
  connectivity stack stopped at met2, one full level short of
  `place_and_route.py`'s `_ROUTING_LAYER_RANGE["sky130_fd_sc_hd"]`
  (`"met1-met5"`) — so a net `klt place-and-route` told OpenROAD it could
  route through met3-or-higher silently split into two disconnected nets on
  extraction, with `ignored_layers: []` (#619, since met3/met4 were already
  read as `capacitors[].bottom_plate`, just never merged). `metals`/
  `metal_labels`/`vias` now cover the full li1-through-met5 stack
  (met3/met4/met5's `70/20`/`71/20`/`72/20`, connecting `via2`/`via3`/`via4`
  at `69/44`/`70/44`/`71/44`), following #508's met1→met2 extension pattern
  and re-verified against the same real sky130A `.lyt`/`.lydrc` sources.
  `PARASITICS.metals` gained matching met3/met4/met5 `LayerRC` entries
  (sourced from the same nominal `sky130.tech` corner as met1/met2) so this
  extension does not regress `--parasitics` into gf180mcu's pre-#547
  zero-R/C-above-the-covered-stack gap. Additive `ExtractionDeck`
  `device_recognition_only_layers`/`merge_layers`/`device_recognition_layers`
  properties and the response's new `device_recognition_only_layers[]` field
  distinguish "read for device recognition, never a `metals`/`vias` level"
  from `ignored_layers`'s "never read at all" — the gap that let met3/met4
  hide this routing-connectivity ceiling behind a clean `ignored_layers`
  report before this fix. It intentionally does **not** mirror into
  `warnings[]`: a deck's own marker/mask geometry (a resistor's marker
  layer, a bipolar's ID mark, a MiM cap's top-plate mark) is expected to be
  device-recognition-only by PDK design, not a coverage gap, so warning on
  every occurrence would fire on nearly any layout using one of these device
  classes. **Behavior change**: a sky130 layout with real routing on
  met3/met4/met5 now merges nets it previously extracted as disconnected;
  see `docs/cli/extract.md`'s "Device-recognition-only layers" section.

- 2026-08-06 — `klt extract`: `warnings[]` no longer duplicates
  `unbiased_pmos_body_nets[]`/`single_terminal_nets[]` one line per instance
  (#599) — a design with many affected devices/nets (e.g. 148 floating PMOS
  bodies on one real top cell) inflated `warnings[]` from a handful of
  entries to 150+ near-identical lines, defeating literal `warnings[]`
  pinning for any caller (a golden-file/regression test, or a human skimming
  CLI output for anything unexpected) at scale. Each finding class now
  mirrors `unmodelled_poly[]`'s existing aggregate pattern: one `warnings[]`
  line per class with the count baked in (e.g. `"148 PMOS devices tie their
  body to an anonymous net..."`), pointing at the structured array for the
  full per-instance list — `single_terminal_nets` emits two such lines (one
  for the `terminal_kind == "gate"` bucket, one for every other terminal
  kind combined), matching its existing two-message-class split.
  `unbiased_pmos_body_nets[]`/`single_terminal_nets[]` themselves are
  unchanged — still one entry per device/net; only their `warnings[]`
  mirror is aggregated. `merged_net_labels[]`/`voltage_domain_warnings[]`
  keep their existing one-line-per-entry `warnings[]` mirror unchanged —
  their cardinality is bounded by label-collision/marker-registry size
  rather than raw device/net count, so they don't reproduce this scaling
  problem; left as a lower-priority follow-up rather than blocking this fix.
  **Behavior change**: a caller matching a specific per-instance net/device
  name inside a `warnings[]` string for either of the two changed fields
  must now read the structured array instead — see `docs/cli/extract.md`'s
  "JSON schema" table.

- 2026-08-05 — `klt drc`: both curated decks checked the layers *below* a
  contact/via but never the conductor *above* it, so a cut whose landing
  metal missed one of its edges reported `status: clean` (#551) — a DRC
  false negative on real, script-placeable geometry. gf180mcu gains nine
  conductor-over-cut enclosure rules: `metal1.enclosing.contact.1` (`CO.6`,
  0.005 um) and a below-cut/above-cut pair for every via level
  (`metal1`/`metal2` around `Via1` at `V1.3a`'s literal 0.0 um and `V1.4a`'s
  0.01 um; `metal2`/`metal3` around `Via2`, `metal3`/`metal4` around `Via3`,
  `metal4`/`metal5` around `Via4`, all 0.01 um). Values are re-derived from a
  real gf180mcuD install's own executable
  `rule_decks/{contact,via1..via4}.drc`, whose
  `cut.enclosed(metal, d) OR cut.not(metal)` form maps exactly onto this
  engine's `"enclosing"` check, so none of the nine is an approximation.
  sky130 gains `li1.enclosing.licon1.1`, closing the analogous
  `li1`-over-`licon1` gap. The end-of-line variants (`CO.6a`/`CO.6b`,
  `Vn.3c`/`Vn.3d`, `Vn.4b`/`Vn.4c`) remain untranscribed — their narrow-metal
  conditional is outside `DrcRule`'s vocabulary. **Behavior change**: a
  layout with a genuinely under-enclosed cut that previously reported
  `clean` now correctly reports `violations` (exit `3`). See
  `docs/cli/drc.md`'s "Coverage" section, including why
  `li1.enclosing.licon1.1` is transcribed at `li.5`'s zero-margin floor
  rather than its published 0.08 um.

- 2026-08-05 — `klt extract --parasitics`: gf180mcu's `PARASITICS.metals`
  table carried only one `LayerRC` (Metal1) against the deck's 5-level
  `EXTRACTION_DECK.metals` stack, so Metal2 through Metal5 silently
  contributed zero resistance and capacitance to every net's reported
  parasitics (#547) — a driver sized against `--parasitics` output on
  gf180mcu was sizing against roughly the wrong, optimistically-understated
  answer. `PARASITICS.metals` is now curated to all 5 entries, sourced from
  gf180mcu.tech's public nominal (`variants ()`) corner (Metal5 uses the
  THICKMET1P1 / 11 kA top-metal row, matching the reported gf180mcuD build).
  Additive `parasitics.metals_without_coefficient[]` (mirrored in
  `warnings[]` when non-empty) now also surfaces any future gap of this
  shape loudly instead of silently, for either shipped deck.

- 2026-08-05 — `klt pdk cells`: two silent-wrong-answer bugs on gf180mcu
  (#537). `device_flavors` was always `[]` for a gf180mcu standard-cell
  library — the device-model regex required a `<family>_fd_pr__` prefix
  (sky130's `sky130_fd_pr__nfet_01v8` shape), but gf180mcu's SPICE instance
  lines name the bare flavor with no prefix at all (`nfet_06v0`); the regex
  now recognizes both shapes. Separately, a library fully, separately
  characterised at more than one voltage for the same typical-process/
  room-temperature corner (e.g. `gf180mcu_fd_sc_mcu9t5v0`'s real
  1.8V/3.3V/5.0V split) reported only its lowest supply — the other
  characterised voltages were silently dropped from the payload, so `--supply
  3.3` false-negatived (exit `3`) even though the library IS characterised at
  3.3V. `libraries[]` gains a `supplies_v` array reporting every
  characterised supply; `--supply` now matches against the full set.
  `nominal_supply_v`/`nominal_corner` keep reporting the lowest as a
  documented, backward-compatible single-value pick. Purely additive JSON
  shape change — no `schema_version` bump (`list_cell_libraries` stays `1`).
  See `docs/cli/pdk.md`'s "`klt pdk cells`" section.

- 2026-08-04 — `klt extract`/`klt lvs`/`klt sim`: the deck's two-term
  device-parameter corrections (`CapacitorDevice.perim_cap_f_um`, #512, and
  `ResistorDevice.fixed_offset_ohm`, #518) now reach the extracted netlist
  itself, not only `klt extract`'s JSON `devices[]` report (#521). Both
  corrections used to be computed while building the response array, so the
  `kdb.Netlist` handed to `NetlistSpiceWriter` (and therefore the `.spice`
  file `klt sim` consumes) and to `klt lvs`'s `NetlistComparer` still
  carried KLayout's raw single-term value — e.g. a 10µm × 1µm sky130
  `res_high_po` reported `r_ohm: 3627.977587` in JSON while writing
  `R$1 ... 3248.27244` to disk, and comparing it against a reference netlist
  built from the PDK's real two-term model reported a spurious
  `device.property` mismatch. The correction is now applied once to the
  `kdb.Device` during extraction, and the JSON report reads the corrected
  value back — `devices[].params` output is unchanged, and a deck that
  leaves both coefficients at their `0.0` default is unaffected everywhere.

- 2026-08-04 — `klt extract`: sky130's `res_high_po` precision poly
  resistor now includes a fixed per-instance head/end-effect resistance
  term, correcting a systematic, one-sided undercount (previously
  `R = L / W * sheet_rho_ohm_sq` only) against the PDK's own two-term
  `sky130_fd_pr__res_high_po` simulation model (#518). `ResistorDevice`
  gains an optional `fixed_offset_ohm` coefficient (default `0.0`,
  non-breaking for any deck entry that does not set it); when set, `klt
  extract` reads back KLayout's own already-computed `L`/`W` and corrects
  `r_ohm` to `L / W * sheet_rho_ohm_sq + fixed_offset_ohm`. sky130's
  `res_high_po` entry is updated with coefficients measured via ngspice
  against the real PDK model card at the `tt` corner
  (`sheet_rho_ohm_sq=324.827244`, `fixed_offset_ohm=379.705147`) — for a
  short resistor this had been undercounting extracted resistance by up to
  ~120%. See `docs/cli/extract.md` -> "Drawn resistors".

- 2026-08-04 — `klt extract`: MiM capacitor devices now include a
  perimeter/fringe capacitance term, correcting a systematic, one-sided
  undercount (previously area-only: `C = A * area_cap_f_um2`) against the
  gf180mcu and sky130 PDKs' own two-term simulation models (#512).
  `CapacitorDevice` gains an optional `perim_cap_f_um` coefficient (default
  `0.0`, non-breaking for any deck that does not set it); when set, `klt
  extract` reads back KLayout's own already-computed plate-overlap
  perimeter (`P`, now also reported as `devices[].params.perimeter_um`) and
  corrects `c_f` to `area_cap_f_um2 * A + perim_cap_f_um * P`. gf180mcu's
  `cap_mim_2f0_m4m5_noshield` and sky130's two `cap_mim`/`cap_mim_m4`
  entries are updated with coefficients transcribed from each PDK's own
  SPICE model card (`sm141064.ngspice`'s `c_cox`/`c_capsw`,
  `sky130_fd_pr__cap_mim_m3_*`'s `camimc`/`cpmimc`) — for a small unit
  capacitor this had been undercounting extracted capacitance by up to
  ~15%. See `docs/cli/extract.md` -> "MiM capacitor device recognition".

- 2026-08-04 — `klt gen-compose`: a route to a north/south-facing port whose
  drawn pad is wider than `routing.width_um` no longer leaves a sub-spacing
  gap beside the pad (#496). `route_two_pin()`'s stub -- the segment leaving
  a port before the backbone's perpendicular jog -- was always drawn at
  `routing.width_um`, even when the port's own reported `width_um` (its
  pad's extent) was wider; the pad's edge outside the narrow stub's own
  footprint could then sit closer to the jog above/below it than the target
  deck's same-layer spacing rule allows -- a real DRC violation in otherwise
  clean composed output (e.g. a `mos_array`/`diff_pair` gate contact's
  landing pad, a `guard_ring`/`bjt_array` ring tap, an S/D pad reached from
  above). The router now widens just that stub segment to the *port's own*
  reported `width_um` when it exceeds the route's, purely geometrically (no
  port-name special-casing, so it generalizes past gate contacts); an
  east/west-facing port's horizontal stub, or an endpoint that needs a
  via-drop instead (its real pad lives on a different layer), is unaffected.
  See `docs/cli/gen-compose.md`.

### Added since release

- 2026-08-21 — `reference.device_map` (`klt lvs`'s `form: "subckt-call"`
  converter) and the identical `netlist.device_map` (`klt layout-plan`/`klt
  digest`) now accept an explicit non-MOS override, not just MOS (issue
  #1271). The original bare-string shape (`{ "<subckt-name>": "<nfet\|pfet>"
  }`) is unchanged — every entry still means a 4-terminal MOS `l`/`w`
  binding. A `device_map` entry may now also be an object, `{ "kind":
  "mos"\|"resistor"\|"capacitor"\|"bipolar", "class": "<device-class>",
  "length_param": "<param>", "width_param": "<param>" }`, letting a caller
  name a custom (uncurated) resistor/capacitor/bipolar subcircuit and get a
  correctly-terminal-counted `R`/`C`/`Q` conversion instead of the
  `device_map only supports MOS-shaped ...` error issue #1163 added for
  exactly this previously-unsupported case (that error still fires for a
  *bare-string* entry naming a non-MOS device, since that shape cannot
  express a non-MOS kind). See
  [`docs/cli/lvs.md`](docs/cli/lvs.md)'s `reference.device_map` field
  description.
- 2026-08-21 — New `klt env-provenance` verb (issue #1254) and the
  importable `klayout_tools.env_provenance` module behind it: the reference
  **environment-provenance emitter** for evidence-record harnesses.
  `emit` reports `{schema_version, host_id, os, python_version, klt_version,
  klayout_version, paths}` where every path is repo-relative (a path outside
  the repo is `{"path": null, "scope": "external"}` — the absolute location
  is never emitted) and the host is a pseudonymous `host-<8hex>` salted hash
  of the normalised hostname, never the hostname itself; there is no
  login/author field. `scan` reports home-directory-shaped absolute paths
  (`/Users/<name>/…`, `/home/<name>/…`, `C:\Users\<name>\…`; a `~/`-rooted
  path is not flagged) in files a change adds, exiting `3` when it finds
  them — a successful run with findings, mirroring `klt drc`'s exit `3`.
  `emit` runs its own finished payload through that same scan and **refuses
  to emit** (exit `1`) rather than mint a record carrying this machine's
  hostname, login, or home directory, which is what makes the rule a
  mechanism rather than a convention. Motivated by a 2026-08 disclosure
  read-audit that found ~3,937 committed evidence records in the public
  canary repos carrying all three identifier classes, written by design by
  each canary's own harness. Existing records are **not** rewritten — a
  record id embeds a commit SHA, so rewriting one breaks the verifiability
  the evidence is published for; the rule binds the writer from now on. The
  rule itself is stated in
  [`docs/design-evidence-tiers.md`](docs/design-evidence-tiers.md) →
  "Provenance hygiene in evidence records" (a new section — the tier ladder
  and T1 checklist `klt signoff` parses are unchanged) and in
  [`docs/design/sim-evidence-discipline-spike.md`](docs/design/sim-evidence-discipline-spike.md)'s
  amendment. See [`docs/cli/env-provenance.md`](docs/cli/env-provenance.md).
- 2026-08-20 — The `sg13g2` extraction deck now recognises SG13G2's third
  **drawn poly resistor**, `rhigh`, and two **drawn metal resistors**,
  `res_metal1`/`res_metal2` (issue #1235, a follow-on to #1231). `rhigh` was
  left unrecognised by #1231 because `sg13g2_tech.json` itself disagrees with
  itself on its sheet rho (`rhigh_rspec` 1300 Ω/sq vs `rhighG2_rspec` 1360
  Ω/sq); a third, independent, citable source breaks the tie —
  `libs.tech/ngspice/models/cornerRES.lib`'s typical-corner (`res_typ`)
  section, the coefficients ngspice itself simulates a `rhigh` instance
  with, sets `rsh_rhigh = 1360`, and that same section's `rsh_rsil`/
  `rsh_rppd` independently reproduce this deck's own already-curated `rsil`/
  `rppd` values exactly, corroborating it over `rhigh_code.py`'s isolated,
  evidently-stale non-`G2` read. `rhigh` now extracts with
  `sheet_rho_ohm_sq=1360.0`, distinguished from `rppd` by requiring both
  `pSD`/`nSD` implants present together (upstream: `rhigh_res = polyres_mk
  .and(psd_drw).and(nsd_drw).and(salblock_drw)`) rather than by an `excludes`
  mirror of `rppd`'s own. `res_metal1` (0.110 Ω/sq)/`res_metal2` (0.088
  Ω/sq) fit inside this deck's already-curated Metal1/Metal2 stack, so no
  stack extension was needed for these two; `res_metal3`..`res_topmetal2`
  remain unrecognised, blocked on the same Metal3+ stack extension (issue
  #1243) MIM capacitors (#1233) are deferred behind. See
  `src/klayout_tools/decks/sg13g2.py`'s resistor note and
  [`docs/cli/pdk.md`](docs/cli/pdk.md)'s SG13G2 coverage table.
- 2026-08-20 — The `sg13g2` curated deck's `metals`/`vias` connectivity
  stack (and the companion DRC rule deck) now reaches **Metal5/TopMetal1/
  TopMetal2**, extended from its original Metal1/Via1/Metal2 ceiling (issue
  #1243, sg13g2's own equivalent of sky130's #619). Before this, a net
  routed above Metal2 — or a device recognised on a level above it —
  resolved to an isolated, unmerged node: `EXTRACTION_DECK.metals`/`.vias`
  had no entries past Metal2, so a design routing through Metal3 or higher
  silently split into disconnected nets on extraction. `DECK` gains 24 new
  `DrcRule` entries (Metal3–Metal5/TopMetal1/TopMetal2 width/space, plus
  each new via level's own enclosure rule(s) — TopVia1/TopVia2 each carry
  *two*, since the upstream source defines both the below- and above-metal
  enclosure), every one transcribed with a `RuleProvenance` citation against
  the fetched IHP-Open-PDK v0.3.0 install, following the same
  `beol/5_17_metaln.drc`/`5_19_via1.drc`/`5_20_vian.drc`/`5_21_topvia1.drc`/
  `5_22_topmetal1.drc`/`5_24_topvia2.drc`/`5_25_topmetal2.drc` sources the
  issue names. This is the prerequisite issue #1233 (MIM capacitors, whose
  `cap_cmim`/`rfcmim` plates land on Metal5/TopMetal1) and issue #1235
  (metal resistors, whose `res_metal1`..`res_topmetal2` flavours span up to
  TopMetal2) both independently blocked on — neither device class is
  recognised by this change itself; each remains its own standalone
  follow-on now that the connectivity stack they would land on actually
  reaches that far. See `src/klayout_tools/decks/sg13g2.py`'s "Scope guard"
  and "MIM capacitors" docstring sections and
  [`docs/cli/pdk.md`](docs/cli/pdk.md)'s SG13G2 coverage table.
- 2026-08-20 — Investigated (and declined) recognising SG13G2's **SiGe HBTs**
  (`npn13G2`/`npn13G2l`/`npn13G2v`/`pnpMPA`) in the `sg13g2` extraction deck
  (issue #1232, a follow-on to #1231). Against a real fetched IHP-Open-PDK
  v0.3.0 install, SG13G2's own LVS deck extracts these through a custom Ruby
  `CustomBJTExtractor` (`custom_bjt_extractor.lvs`) — not KLayout's stock
  `DeviceExtractorBJT3Transistor` that `EXTRACTION_DECK.bipolars`
  (`BipolarDevice`) wires up — with a device marker that is itself a 3-layer
  compound boolean (and one of those layers is not even a literal drawn
  layer in this PDK) and terminal pins distinguished by drawn
  bounding-box/area filters this engine's device-recognition primitives have
  no equivalent for. Forcing a same-shaped `BipolarDevice` approximation
  would have produced a self-consistent golden pair that still does not
  match the PDK's real connectivity, so `bipolars` stays empty — a drawn
  SiGe HBT continues to extract as ordinary interconnect (a `klt lvs`
  `device.unmatched`/short) rather than a wrong device. See
  `src/klayout_tools/decks/sg13g2.py`'s "SiGe HBTs — investigated, declined"
  docstring section for the full finding, and
  [`docs/cli/pdk.md`](docs/cli/pdk.md)'s SG13G2 coverage table.
- 2026-08-20 — The `sg13g2` extraction deck now recognises SG13G2's
  **thick-oxide ("-HV") MOS** flavour and two of its **drawn poly resistors**
  (issue #1231). Before this, geometry drawn inside `ThickGateOx` (44/0)
  extracted as the thin-oxide device — an actively *wrong* device identity,
  not merely an unrecognised one: under `--pdk` it bound
  `sg13_lv_nmos`/`sg13_lv_pmos` for a transistor the PDK's own LVS deck
  extracts as `sg13_hv_nmos`/`sg13_hv_pmos`. `EXTRACTION_DECK` now declares a
  `mos_flavours` entry keyed on that marker (the mechanism issue #1111 added
  for gf180mcu's `Dualgate`), so such a transistor binds the real thick-oxide
  models and `ThickGateOx` stops firing `voltage_domain_warnings` for MOS
  geometry (its DRC-rule residue keeps the registry entry). `sg13g2` also
  gains its first `--pdk` MOS model-binding table entry, resolved from the
  `ihp-sg13g2` variant name IHP-Open-PDK installs use. Separately, `rsil`
  (7 Ω/sq) and `rppd` (260 Ω/sq) poly resistors now extract as real
  two-terminal devices with a bulk terminal instead of shorting through the
  poly bar; `rhigh` is deliberately still unrecognised (its sheet rho is
  ambiguous in the PDK's own data — 1300 vs 1360 Ω/sq — and a known-unmodelled
  short beats a silently wrong value). `devices[].class` labels for MOS are
  unchanged (`nfet`/`pfet`, flavoured or not); `device_classes` for `sg13g2`
  gains `"resistor"`. HBT/MIM-capacitor/diode recognition remain unbuilt —
  see [`docs/cli/pdk.md`](docs/cli/pdk.md)'s SG13G2 coverage table and
  [`docs/cli/extract.md`](docs/cli/extract.md).
- 2026-08-19 — `klt --version` now reports a **build identity**, and the new
  `klt version` verb exposes it as JSON (issue #1202). A build made from a
  commit after a release tag used to report that tag's version verbatim, so a
  PyPI `0.2.0` install and a later `pip install git+…@<sha>` — shipping
  different rule decks — were indistinguishable on the CLI surface. A build
  that is not a confirmed tagged release now reports a PEP 440 local-version
  segment: `0.2.0+g<sha>`, `0.2.0+g<sha>.dirty`, or `0.2.0+unknown` when the
  identity is unrecoverable. **A real tagged release still reports the bare
  `klt X.Y.Z` it always did**, so consumers parsing that string are
  unaffected. `klt version --format json` carries
  `{schema_version, version, package_version, git_commit, git_tag, dirty,
  is_release}`, with `is_release` tri-state (`true`/`false`/`null`) exactly
  like `provenance.deck.released`. The identity is captured at build time by
  a new `hatch_build.py` hatchling hook (an installed wheel has no `.git` to
  probe); the package version in `pyproject.toml` stays static. Reports'
  `provenance.klt_version` is unchanged — still the plain package version.
  See [`docs/cli/version.md`](docs/cli/version.md).
- 2026-08-19 — New `klt deck hash --deck <name>` reports the
  `provenance.deck.content_hash` the running build resolves for a built-in
  deck, with **no layout file and no check run** (issue #1202). Obtaining
  that hash previously meant running a throwaway `klt drc` on whatever layout
  happened to be around purely as a version probe — and was impossible for a
  consumer with no layout at all (a CI preflight, a container smoke test).
  The payload is `{schema_version, deck, content_hash, released}`, computed
  through the same code path a real run records, so it cannot drift from what
  a report carries; an unknown deck name is a clean exit-1 error envelope
  naming the available decks. Pairs with the existing
  `klt deck resolve --content-hash <hash>` (which release shipped this?).
  See [`docs/cli/deck.md`](docs/cli/deck.md).
- 2026-08-19 — New verb `klt pdk em-limits` (issue #1215): parses every tech
  LEF a resolved PDK variant ships and reports, per `ROUTING`/`CUT` layer,
  the `DCCURRENTDENSITY`/`ACCURRENTDENSITY` electromigration limits declared
  — the only machine-readable EM data an open_pdks-layout install ships at
  all (no dedicated EM section exists in the DRC decks or ngspice model
  files). Flags any layer where the shipped tech LEFs disagree (a real
  gf180mcuD install's own `_fd_sc_mcu9t5v0`/`mcu7t5v0` vs.
  `_osu_sc_gp9t3v3`/`gp12t3v3` families report 1.19µm/1.5/2.2 mA/µm vs.
  0.99µm/1.21/1.82 mA/µm for the same top-metal layer, despite identical
  sheet resistance) and returns the conservative (lower) value by default;
  a cut layer with no declared current density at all (the diffusion/poly
  contact layer, `CON`, usually the tightest EM constraint in a
  power-device stack) reports an explicit "not shipped" result rather than
  omitting the layer or fabricating a number. Backed by four new fields on
  `lef_header.parse_lef_header()`'s `layers[]` entries — `thickness_um`,
  `dc_current_density`, `ac_current_density`, `resistance_rpersq` — and the
  new `pdk.em_limits()` library function. Scans every `libs_ref` entry that
  ships a `techlef/` subdirectory, independent of naming convention
  (deliberately not the `_fd_sc_`-name-marker scan `klt pdk cells` uses),
  so it does not miss the `_osu_sc_`-named half of the disagreement it
  exists to surface. Documented in [`docs/cli/pdk.md`](docs/cli/pdk.md) →
  "`klt pdk em-limits`".
- 2026-08-19 — New `klt deck info [--deck <name>]` subcommand (issue #1209):
  reports this *install's own* deck content hash, structural device-class
  coverage (`ExtractionDeck.device_classes` — e.g. whether
  `diode_nd2ps_06v0`/`diode_pd2nw_06v0` are wired up at all), and release
  status (the same tri-state `released` signal as `provenance.deck.released`,
  plus the matching release's `git_tag`/`git_commit`/`package_version` when
  `released` is `true`) — with **no input layout needed**. Complements `klt
  deck resolve` (issue #623), which requires a content hash already in hand
  (normally obtained by running some verb against real input and reading it
  out of a JSON report's `provenance` block); `klt deck info` answers "what
  does *this* install actually recognise" directly. Motivated by a real
  instance of the "same `klt --version` string, different deck content" gap
  (issue #1209): PyPI's `klayout-tools==0.2.0` shipped a `gf180mcu` deck
  built before diode-device recognition was added, silently — both the PyPI
  build and a from-source checkout reported `klt --version` as `0.2.0`, and
  nothing short of a live extraction diff or hand-inspecting `provenance`
  distinguished them. See `docs/cli/deck.md`.
- 2026-08-19 — `klt sim` now classifies ngspice's own uninformative
  `Error: could not find a valid modelname` failure as a new
  `model_bin_range` diagnostic code (issue #1214). Some PDKs' BSIM model
  cards enforce an undocumented per-instance width bin range — gf180mcu's
  `nfet_06v0`/`pfet_06v0` fail this way at roughly 100-110 um of total
  instance width, identically whether the width comes from a raw `w=` or
  from fingering it via `nf=` (only `m=`, which parallels outside the
  check, reliably works around it) — and the raw ngspice message names
  neither the width nor even the offending instance. `klt sim` re-scans the
  corner's own netlist for the MOS instance with the largest total width
  (`w`, or `w * nf` when fingered) not already using `m=`, and reports it by
  name in the diagnostic's `message`; when no such instance is found it
  falls back to ngspice's raw log line, same as every other code. See
  [`docs/cli/sim.md`](docs/cli/sim.md) → "Model bin-range diagnostic".
- 2026-08-19 — The shared `provenance.deck` block (`klt drc`, `klt extract`,
  `klt lvs`, and every other verb whose report carries a `provenance.deck`)
  now includes a `released` field (issue #1193): a non-fatal, generation-time
  answer to whether `provenance.deck.content_hash` matches a deck that has
  actually shipped in a released `klayout-tools` version, per
  `klt deck resolve`'s own generated history table
  (`src/klayout_tools/decks/_history.json`, issue #623). `true` when the hash
  matches a release; `false` (still `status: "clean"`/`"match"` — this never
  fails the run) when the table loaded fine but confirms no release ships
  this hash — an unreleased dev checkout, an uncommitted deck edit, or a deck
  added after the last tag (the `sg13g2` deck is a live example of this
  today); `null` when the answer can't be determined at all (no
  `content_hash` to check, or the history table itself is missing,
  unreadable, or malformed — deliberately never conflated with a confirmed
  `false`). Closes the gap where a project could accumulate months of
  committed DRC/LVS evidence against a deck revision no release ever shipped,
  with nothing surfacing that fact until someone later tried
  `klt deck resolve` on the orphaned hash. No `schema_version` bump —
  additive to `provenance.deck`, built once in
  `klayout_tools._provenance._deck_block` /
  `klayout_tools.decks.history.is_deck_hash_released`. See
  `docs/json-contract.md`'s "Shared `provenance` block" section.
- 2026-08-19 — `klt gen-compose` can now consume a cell it did not generate
  (issue #1189), in both of the two ways that were previously impossible
  without hand-forging a `generator_report` object with a fake `generator`
  field. **(1) Hierarchical composition**: the response now reports
  `generator: "gen-compose"` plus a `ports[]` array promoted from the
  request's own `pins[]` entries (in the composed, post-placement coordinate
  frame, using `klt gen`'s own port shape, named after each pin's `net` so
  the port name, its drawn `kdb.Text` label, and the name `klt extract`
  recovers all agree). A composition therefore feeds straight back into a
  further composition as a `blocks[].generator_report`, and its top-level
  nets are addressable by name from the level above — where before there was
  only one flat level. **(2) `blocks[].cell`**, a new block kind naming an
  **existing** cell in a GDS/OASIS stream — a PDK standard cell, a vendor
  macro, any pre-drawn library cell — as
  `{gds_path, cell_name, ports[], bbox_um}`. `ports[]` (validated, since
  these are hand-declared rather than tool-produced) is what lets a library
  cell participate in `connectivity[]`/`pins[]`; `bbox_um` is optional and,
  when omitted, is read straight off the cell (`kdb.Cell.dbbox()`), so a
  caller no longer has to re-key `klt cells`' `{left, bottom, right, top}`
  report into this command's `{x0, y0, x1, y1}` convention. Exactly one of
  `generator_report`/`cell` per `blocks[]` entry; a `generator_report`
  missing its `generator` now fails with a message that points at
  `blocks[].cell` instead. A `cell` block is otherwise ordinary — it takes
  `blocks[].orientation` (#1166), works under every `placement.strategy`,
  and mixes freely with generated blocks in one request. Response
  `blocks[]` entries gain `source` (`"generator_report"`/`"cell"`) and
  `cell_name`, and `blocks[].generator` is `null` for a `cell` block. No
  `schema_version` bump — the request additions are optional and the
  response additions are new fields (`generator`, `ports[]`,
  `blocks[].source`, `blocks[].cell_name`); `blocks[].generator` widens to
  `string | null` only for the new block kind, which no pre-existing request
  can produce. Per-block *rotation* beyond `orientation`'s four
  mirror/rotate values (what a mirrored standard-cell row pair needs) stays
  out of scope — tracked by #1166. See `docs/cli/gen-compose.md`'s
  "Hierarchical composition and library cells (#1189)" section.
- 2026-08-18 — `klt gen-compose`'s `routing` gains an optional
  `cross_block_layer_role` (issue #1168): a second, higher metal role a
  same-block self-net leg falls back to when it would otherwise draw a
  silent short across another of that block's own pads on the primary
  `routing.layer_role` — exactly the case an existing rejection reason
  already named as the fix ("route to a layer_role with a metal2/via stack
  instead") without a way to configure it. Before this, escaping that short
  required moving the *whole* composition's `routing.layer_role` to the
  second metal (`"metal2"`, issue #454), even for nets that never needed it.
  `gen_compose._resolve_cross_block_route_layer` resolves and via-hop
  validates the new role against the same per-PDK-family table
  `routing.layer_role` uses (an application error, exit 1, when it is not
  exactly one via hop from the primary role); `route_two_pin()` retries only
  the same-drawing-layer-short checks on the cross layer for a leg that
  trips them on the primary layer, redrawing that leg's entire backbone
  there with an endpoint via-drop wherever needed — every other net in the
  same request, and any leg that never crosses another same-layer pad,
  is unaffected. No `schema_version` bump — purely additive to the
  `routing` request object and the `nets[].legs[]`/`routed_geometry`
  internals; the response shape is unchanged. See `docs/cli/gen-compose.md`'s
  "Cross-block bus routing (`routing.cross_block_layer_role`, #1168)"
  section.
- 2026-08-18 — `klt gen-compose`'s `blocks[]` (and
  `klt.layout_plan.request/1`'s `device_groups[]`) accept an optional
  `orientation` field -- `"none"` (default), `"mirror_x"`, `"mirror_y"`, or
  `"rotate_180"` (issue #1166, root cause #1 of #1164's friction report) --
  a per-block mirror/rotation applied about that block's own local origin
  before placement translates it, orthogonal to (composes with) every
  `placement.strategy`. Unblocks the minimal "CMOS inverter" case a
  translation-only placement model could not route: two same-facing ports
  (e.g. two `mos_array` blocks' drains, both drawn on their own right edge)
  cannot be wired without one being mirrored to face the other.
  `bbox_um`/`ports[]` (position and `direction_deg`) and the actual drawn
  GDS geometry are all transformed identically -- see `docs/cli/gen-compose.md`'s
  new "Block orientation (mirror/rotate, #1166)" section for the exact
  transform table. Omitting `orientation` leaves every existing request's
  behavior unchanged (defaults to `"none"`, today's translation-only
  placement). No `schema_version` bump -- additive on both contracts.
- 2026-08-18 — `klt signoff --manifest` gains an opt-in **generic evidence
  envelope** (`"kind": "generic"`, issue #1152) as the ingestion path for T1
  item 8 ("Characterization report"), the one T1 checklist item naming no
  specific `klt` verb -- e.g. a project's own hand-rolled record backing a
  committed Markdown characterization report. A minimal JSON wrapper
  (`schema_version`, `"kind": "generic"`, `status: "pass"|"fail"`, plus
  optional `summary`/`source`/`provenance`) is recognized by `_classify` via
  its explicit `"kind"` self-declaration, checked ahead of every native
  structural check so an incidental field collision can never misclassify
  it; graded in envelope-aggregation mode exactly like the six native
  kinds. A new, independent `_ITEMS_ACCEPTING_GENERIC_EVIDENCE` set (today
  `{8}`) gates which `--manifest` tier items a passing `"generic"` citation
  may satisfy -- items 3-7 (DRC/LVS/corner/Monte-Carlo/post-layout) and the
  otherwise-unrestricted items 1, 2, 9, 10 all continue to render
  `"unmet"`/`"wrong_kind"` for a `"generic"` citation, never a borrowed
  pass. `provenance` is optional, unlike every native kind: when given, it
  participates in the existing staleness/consistency machinery for free; when
  omitted, a manifest-pinned `content_hash` against that entry always
  renders `"unmet"`/`"stale_evidence"`, never a false pass. No
  `SCHEMA_VERSION`/`TIER_REPORT_SCHEMA_VERSION` bump -- additive, matching
  the precedent set when `yield`/`pex` were added. See
  `docs/cli/signoff.md`'s "Generic evidence (opt-in, non-`klt`-native)"
  section.
- 2026-08-18 — new **`klt.layout_plan.request/1`** contract plus a
  library-level reference validator (issue #1131, Phase B of
  `docs/design/netlist-driven-layout-spike.md`): a declarative plan
  document describing how a netlist's devices are grouped, matched,
  ordered (`rows[]`), and abutted (`abutment[]`) into one layout —
  placement *intent*, sitting between an ingested netlist digest (issue
  #1130) and existing `klt gen`/`klt gen-compose` calls.
  `klayout_tools.layout_plan.validate_layout_plan()` is a pure function
  with no generation side effects: it checks structure, resolves every
  `device_groups[].devices` reference against the netlist digest, checks
  `device_groups[].generator`/`topology` against what `docs/cli/gen.md`'s
  generators actually support, and checks every intra-plan
  `encloses`/`rows[].order`/`abutment[]` group-id reference. Notably, a
  device reference resolves on `(name, device_class)`, never `name` alone
  — a digest device name is per-class (`M1`/`R1` both digest to `"1"`), so
  a bare name carried by two classes is an ambiguity **error** naming the
  classes to choose between, and `{"name", "device_class"}` is accepted to
  disambiguate. Exit-code trichotomy: `0` valid, `1` application error
  (unresolvable reference; includes `topology: "interdigitated"`/`"single"`,
  which no generator supports yet and which are flagged rather than
  silently accepted), `2` usage error (malformed document). **No `klt`
  subcommand is added** — Phase B is contract + validation only; plan
  *execution* is Phase C, not yet built. See the new
  `docs/cli/layout-plan.md` and
  `docs/schemas/layout-plan-request.schema.json`.
- 2026-08-18 — new **plan compiler/executor**,
  `klayout_tools.layout_plan_execute` (issue #1155, Phase C of
  `docs/design/netlist-driven-layout-spike.md`): turns an already-validated
  `klt.layout_plan.request/1` document (Phase B, issue #1131) into a real
  generated/placed/routed layout by calling only already-shipped machinery
  — `klt gen`'s `generate()` per `device_groups[]` entry (netlist-derived
  `L`/`W` sizing, with `params` overrides layered on top and every
  divergence surfaced as a `warnings[]` entry), `rows[]`/`abutment[]`
  compiled onto `klt gen-compose`'s existing `"explicit"` placement
  strategy (reusing `compute_row_offsets` per row, then stacking rows
  vertically), the netlist digest's device-to-net map compiled into
  `gen-compose`'s `connectivity[]` (a 3+-pin net becomes an ordinary bundle
  entry — `compose()` already routes every entry through `route_bundle()`
  regardless of pin count since issue #1073, so this module never calls
  the router itself), and one final `gen_compose.compose()` call for the
  actual generation/placement/routing pass. No new placement/routing
  primitive anywhere in this phase. Response extends `klt gen-compose`'s
  shape with `device_groups[]` (`resolved_params`/`offset_um`/`bbox_um`)
  and the one new field, `unmapped_netlist_nets[]` (every digest net that
  resolved to zero device-group ports — including a deliberately-unrouted
  supply/bulk net, always reported unconditionally, no
  `netlist.ignore_nets[]`-style opt-out added). Exit-code trichotomy: `0`
  full success, `1` application error (`LayoutPlanExecuteError` — an
  unresolvable PDK, a generation failure, an empty `device_groups[]`, or a
  group with no `rows[]`/`abutment[]` path to a placed position), `2`
  reserved for a future CLI subcommand (none added this phase — library-
  only, mirroring Phase B), `3` partial success
  (`unrouted_nets[]`/`unmapped_netlist_nets[]` non-empty, via
  `exit_code_for()`/`partial_success()`). See the new
  `docs/cli/layout-plan-execute.md`.
- 2026-08-18 — `klt extract --deck-option mim_cap=<value>` selects which of
  gf180mcu's three PDK-offered MiM-capacitor densities
  (`cap_mim_1f0_m4m5_noshield`/`cap_mim_1f5_m4m5_noshield`/
  `cap_mim_2f0_m4m5_noshield`, 1.0/1.5/2.0 fF/µm² respectively) a marked
  `FuseTop`-over-`Metal4` overlap extracts as (issue #1151), mirroring the
  `poly_res` resistor-flavour mechanism issue #595 already shipped. The
  three densities share byte-identical drawn mask geometry, so before this
  option the deck could only ever recognize the default 2.0 fF/µm² density
  — a design drawn against the 1.0/1.5 fF/µm² options had no way to get an
  accurate extraction, and (worse) its DRM-legal top-plate via was silently
  read as an ordinary routing via, shorting the two plate nets together in
  the extracted netlist with no diagnostic. `CapacitorDevice` gains
  `flavour_option`/`flavours` fields (a new `CapacitorFlavour` dataclass)
  analogous to `ResistorDevice`'s own; `get_extraction_deck`'s
  `deck_options` resolution now applies to both device families through one
  shared, generalized resolver. Omitting `--deck-option mim_cap=...` keeps
  today's `cap_mim_2f0_m4m5_noshield` default, byte-for-byte. `--pdk`
  model-binding for the two new flavours is not yet curated (their
  extracted `C` card is written unbound, the same documented carve-out an
  unbound device class elsewhere in this deck already gets) — see
  `docs/cli/extract.md`'s "Selecting a shared-geometry MiM capacitor density
  flavour" section.
- 2026-08-18 — `klt extract` gains **`--check <report.json>`** (issue
  #1149), closing the one gap issue #1106 left open: `klt drc`/`klt lvs`
  already let a caller verify a previously committed report still
  reproduces, but `klt extract` -- the verb that actually contains a
  curated deck's device-recognition logic (e.g. gf180mcu's substrate/
  well-tap derivation, issue #1084) -- had no such check, so a caller
  re-running an extraction against an updated deck build got a silently
  different result with no signal that the deck itself had changed
  underneath them. Wires `klt extract` up to the same shared
  `_report_verify.py` machinery `drc`/`lvs` already use -- no new hashing
  scheme. Cheap mode (default) re-hashes the input layout and deck the
  committed report names (`provenance.input.content_hash`/
  `provenance.deck.content_hash`) and compares against the values the
  report already recorded -- no extraction engine re-run. Combine with
  `--rerun` for full mode: re-run the extraction (reconstructing `file`/
  `deck`/`top`/`provenance.deck.options` from the committed report) and
  diff every verdict-bearing field, excluding `provenance.klt_version`/
  `klayout_version`/`pdk.version`. Both modes report `status: "match"`
  (exit `0`) or `"drifted"` (exit `3`, naming which field(s) moved)
  through the standard envelope. `--check` is mutually exclusive with the
  positional `<file>` argument -- a required, mutually exclusive argparse
  group, so omitting or combining both is a usage error (exit `2`);
  `--deck` is consequently no longer `required=True` at the argparse level
  (an omitted `--deck` alongside `<file>` is now a clean application-level
  error, exit `1`, not a usage error). Purely additive otherwise: no
  existing field or exit code changes meaning for a normal (non-`--check`)
  run. See `docs/cli/extract.md`, "`--check` / `--rerun`".
- 2026-08-18 — `klt gen` gains a new **`cap_array`** generator (issue
  #1117): a row of matched unit MiM (Metal-Insulator-Metal) capacitor
  cells, each a top-plate-metal-over-bottom-plate-metal stack (sky130's
  `capm` top-plate mark over a `met3` bottom-plate conductor) with a
  `via3`/`met4` top-plate via and local-metal landing pad, the capacitor
  sibling of `res_array` (analogous to how `mos_array`/`res_array`/
  `bjt_array` already cover the MOS/resistor/bipolar device classes `klt
  extract` recognizes). Draws the *same* layer/datatype numbers
  `klayout_tools.decks.sky130`'s `EXTRACTION_DECK.capacitors[0]` entry
  (`sky130_fd_pr__model__cap_mim`) already declares, so its output
  round-trips through `klt extract` to that exact device class. Two ports
  per unit (`C<i>_BOT`/`C<i>_TOP`); `drc_hints.matched_group_id` is
  `"cap_array:<num>"`. Only `sky130` is supported for now — gf180mcu's own
  MiM stack needs an additional "virtual bottom plate" oversize derivation
  that is out of this generator's initial scope; requesting `cap_array` for
  any other PDK family raises a clear application error. See
  `docs/cli/gen.md`'s new "`cap_array`" section.
- 2026-08-18 — `klt drc`/`klt lvs` gain **`--check <report.json>`** (issue
  #1106): verify a previously committed `--format json` report still
  reproduces, instead of hand-rolling a normalize-and-diff. Cheap mode
  (default) re-hashes the input(s)/deck the committed report names
  (`provenance.input.content_hash`/`provenance.deck.content_hash` for
  `drc`; `environment.layout_sha256`/`environment.reference_sha256`/
  `provenance.deck.content_hash` for `lvs`) and compares against the
  values the report already recorded — no engine re-run. Combine with
  `--rerun` for full mode: actually re-run the analysis and diff every
  verdict-bearing field between the fresh and committed reports, excluding
  `provenance.klt_version`/`klayout_version`/`pdk.version` (fields that
  legitimately vary between runs of identical inputs). Both modes report
  `status: "match"` (exit `0`) or `"drifted"` (exit `3`, naming which
  field(s) moved) through the standard envelope. `--check` is mutually
  exclusive with the normal positional input (`klt drc`'s `<file>`, `klt
  lvs`'s `<request>`) — a required, mutually exclusive argparse group, so
  omitting or combining both remains a usage error (exit `2`), unchanged
  from the pre-#1106 contract. Purely additive: no existing field or exit
  code changes meaning for a normal (non-`--check`) run. See
  `docs/cli/drc.md`/`docs/cli/lvs.md`, "`--check` / `--rerun`".
- 2026-08-17 — New verb **`klt sta`** (issue #1099): standalone timing/power
  analysis of an already-implemented (placed & routed) design, independent
  of `klt place-and-route`'s own in-flow STA. Previously the only way to
  get a timing number for an existing routed DEF was to re-run the entire
  `place-and-route` flow — which makes correct multi-corner
  characterization impossible (re-running place-and-route per corner
  produces N *different* placements/routings, since global placement and
  detailed routing are seeded but not corner-invariant) and is expensive (a
  full flow per corner where the analysis itself is seconds). `klt sta
  <request.json>` instead runs a single, fresh OpenSTA session over a
  caller-supplied routed `def` (`read_lef` x2, `read_def` — no
  `-floorplan_initialize` — `read_liberty`, `create_clock`, optionally
  `read_spef`), with no placement/routing/CTS of its own, and reports the
  same timing/power fields `place-and-route`'s response already carries
  (`worst_slack_ns`, `total_negative_slack_ns`, `fmax_mhz`,
  `setup_violation_count`, `hold_violation_count`, `clock_skew_ns`,
  `estimated_power_mw`), plus a `provenance` block built via the shared
  `build_provenance` helper. An optional `spef` request field feeds a
  caller-supplied SPEF in via `read_spef`, with the same net-name-
  correlation sanity check (`spef_annotation`) `klt place-and-route`'s own
  `post_route_spef` in-flow pass already runs. Backed by a new
  `klayout_tools/post_route_sta.py` module (named to avoid clobbering the
  pre-existing, unrelated `klayout_tools/sta.py` — the `klt_statime_native`
  Rust boundary backing `klt synthesize`'s integrated, pre-layout,
  gate-level `sta` report). See `docs/cli/sta.md` for the full
  request/response contract.
- 2026-08-17 — `klt place-and-route`'s post-route multi-corner sweep (issue
  #949) now reports a per-corner breakdown and can be scoped to a named
  subset of the shipped corners (issue #1092). A new response field,
  `corners: [{"name": ..., "setup_slack_ns": ..., "hold_slack_ns": ...},
  ...]`, names which swept corner produced `worst_setup_slack_ns`/
  `worst_hold_slack_ns` — previously only the two aggregate numbers were
  reported, with no way to tell which of a cell library's shipped corners
  decided either one. A new optional request field,
  `pdk.sweep_corners: [...]`, lets a caller scope the sweep to the corners
  a design actually operates at instead of always sweeping every corner the
  cell library ships (a multi-supply library's sweep is otherwise dominated
  by decks a single-supply design never runs at); an unresolvable corner
  name raises `PlaceAndRouteError`. Both fields are additive — omitting
  `sweep_corners` reproduces today's "sweep everything" behavior exactly,
  and the two existing aggregate fields are unchanged in meaning. See
  `docs/cli/place-and-route.md`'s "Multi-corner setup/hold sweep" section.
- 2026-08-17 — `klt place-and-route` gains an optional **`request.power`
  power-delivery stage** (issue #1091): previously the generated Tcl never
  called `global_connect`/`pdngen` and never inserted tapcells or filler
  cells, so a routed DEF had no `SPECIALNETS` section, every standard
  cell's `VDD`/`VSS` LEF pin belonged to no net, and cell rows were
  discontinuous wherever placement left a gap — `target_stage: "route"`
  meant "signals route," not "the block is implemented." The new optional
  `power` request block (`power_net`/`ground_net`, default `"VDD"`/`"VSS"`,
  plus `straps[]` naming PDN layer/width/pitch geometry) drives real
  `tapcell`/`add_global_connection`/`global_connect`/`pdngen` Tcl at the end
  of the `"floorplan"` stage and `filler_placement`/`global_connect` at the
  end of the `"route"` stage — verified live end-to-end against a real
  `openroad` binary and a real sky130A install (non-empty `SPECIALNETS`,
  every standard cell's PG pins wired, 105 tapcells + 807 fillers placed on
  the `gcd` worked example). Per-library tapcell/filler-cell/pin-pattern
  reference data lives in three new tables (`_TAPCELL_CELLS`,
  `_FILLER_CELLS`, `_POWER_PIN_PATTERNS`), sourced and dated the same
  verified-not-guessed way as the existing CTS-buffer/routing-layer-range/
  antenna-diode tables. The response gains an additive `power` field
  (`pdn`/`global_connect` booleans, `power_net`/`ground_net`,
  `tapcell_master`/`endcap_master`, `filler_masters`) so a caller can tell a
  signal-only "route" result from a power-complete one without parsing the
  DEF for a missing `SPECIALNETS` section — always present, `false`/`null`/
  `[]` shaped when `request.power` is omitted (the default, byte-for-byte
  unchanged prior behavior). See `docs/cli/place-and-route.md`'s "Power
  delivery" section for the full contract.
- 2026-08-17 — `klt yield` gains a **`failed_unmeasurable` count**, additive
  alongside `errored` on a measurement's request (both the sample-set and
  `klt sim` report input paths, plus `negative_control`) and echoed back in
  the response (issue #1095). `errored` (a tooling failure -- the simulator
  crashed, the log was unparseable) stays excluded from every statistic,
  including the empirical yield's own denominator, exactly as before.
  `failed_unmeasurable` is the other kind of "no value": a *design* failure,
  where the measurement's functional failure mode **is** the absence of a
  value (an extraction only defined in-regime, a search reporting no
  operating point in range, a `.meas` that does not trigger). Those draws
  now **enter the empirical yield's numerator's complement and denominator
  as failures** -- `yield.empirical.n == n + failed_unmeasurable` -- while
  staying excluded from `distribution`/`capability` like `errored`, since
  there is still no value to fit a mean, stddev, or Cpk to. Critically, this
  also fixes `negative_control`: before this change, a deliberate defect
  effective enough to drive *every* negative-control draw out of the
  measurable regime reported `not_detected`, because every failing draw
  vanished into `errored` and the control's surviving samples were only the
  ones that happened to still produce a value -- the self-check reported the
  opposite of what actually happened. A negative control seeded entirely
  with `failed_unmeasurable` (no numeric samples at all) now correctly
  reports `detected`. PR #1101's existing `errored`-only conditional-yield
  warnings are unchanged; `failed_unmeasurable` gets its own distinct
  per-measurement and run-level warnings. See
  `docs/cli/yield.md#errored-samples-and-conditional-yield` and
  `docs/cli/yield.md#negative-control`.
- 2026-08-17 — `klt economy` gains an **AREA-EFF bounds-check mode** (issue
  #1086): a block's absolute area bound (`Area`) can't tell area a block
  *needs* from area it *wastes* — this adds the companion `Area-Eff` spec
  row's machine-checkable side. Four independently-optional flags each add
  one check to a new `area_eff` block (present only when at least one is
  given, mirroring `--budget-um2`/`--reference-area-um2`'s optional-block
  convention): `--area-eff-max-dead-margin-um` (hard bound — caps every
  edge of `dead_margins_um`), `--area-eff-max-empty-region-fraction` (hard
  bound — caps the largest `largest_empty_regions[]` entry's share of the
  bbox), `--area-eff-require-bbox-tightness` (hard bound — `bbox_tightness`
  must equal `1.0`), and `--area-eff-min-utilization` (calibrated bound — a
  per-block-kind floor, deliberately the only non-hard check since a floor
  set too high pressures cramming against matching/DRC margin). `area_eff`
  reports a `checks` sub-object (only the requested checks) and an overall
  `status`, `"pass"` only when every requested check passes. See
  `docs/design-evidence-tiers.md`'s new "Area-efficiency spec convention
  (AREA-EFF)" section for the full convention (including seeded
  per-block-kind utilization floors, cross-checked against real `klt
  economy` output on both existing `evidence/economy-review/` canaries) and
  `docs/cli/economy.md`'s "AREA-EFF bounds-check block" for the JSON shape.
- 2026-08-17 — `klt gen-compose` routes **bundle (>2-pin) `connectivity[]`
  nets** (issue #1073). A shared supply/ground rail, a bias line, a clock, or
  any fanout node touches one port on every block it spans, so a two-pin-only
  router left the majority of a real circuit's connectivity in
  `unrouted_nets[]` (exit 3) with nothing drawn — the verb degraded to
  "placement only" for anything past a two-block pair. An N-pin net is now
  routed as a **spanning tree of two-pin legs**: every unordered pin pair is a
  candidate leg, candidates are tried nearest-first (Manhattan distance
  between the ports' composed-frame positions, ties broken by declaration
  order so output stays byte-reproducible), and a leg is accepted when it
  joins two so-far-disconnected parts of the net. For a rail across a
  placement row that yields exactly a trunk: a chain of adjacent-block legs.
  Every leg goes through the same `route_two_pin` as before, so all of its
  routability checks (channel width, guard/collector ring, self-net pad
  crossing, self-net drawn-metal short, obstacle overlap, via-drop
  resolution) apply **per leg** — and a leg one of them rejects is skipped in
  favour of the next candidate joining the same two parts, so a net routes
  around an individually unroutable pair whenever another spanning tree
  exists. `pins[]` order is not a routing order. Additive response field:
  `nets[].legs[]` (per-leg `pins`/`routed`/`route_length_um`/`reason`), where
  `routed: true` means that leg's metal is in the output. A net whose pins
  cannot all be joined stays in `unrouted_nets[]` and draws **nothing** (a
  half-wired net would leave the caller building the rest of its interconnect
  around the router's own geometry), with the per-leg rejection reasons in
  `legs[]` and a `drc_hints.notes[]` entry naming the pins that could not be
  reached. A bundle net gets one net label, not one per leg. `waypoints_um`
  (#634) steers a single backbone and is now rejected at request-parse time
  (exit 1) on a >2-pin net instead of being silently ignored. Two-pin
  behavior is unchanged in every respect (it is the degenerate one-leg case
  of the same path). This is the increment
  `docs/design/gen-composition-spike.md` section 5 item 2 reserved for "once
  two-pin routing is proven against a real block".
- 2026-08-16 — `klt gen-compose` gains a third `placement.strategy`:
  **`"array"`** (issue #1053), for composing a repeated-block regular R
  rows x C columns tiling (a matched-device array, a memory bitcell array, a
  pad ring — anything built from one cell on a uniform X/Y pitch) directly,
  instead of emitting `rows * cols` individual `"explicit"` entries. Takes
  exactly one `blocks[]` entry plus `placement.rows`/`cols`/
  `row_pitch_um`/`col_pitch_um` and an optional `placement.origin_um`
  (defaults to `{0, 0}`) — mirroring `klayout.db.CellInstArray`'s own
  row-vector/column-vector/row-count/column-count parameterization — and
  composes it as a **single hierarchical `kdb.CellInstArray` instance**
  rather than flattening into `rows * cols` placements, keeping a
  several-thousand-instance array's request document (and the composed
  layout's own instance count) at O(1) instead of O(rows * cols). Per-tile
  `connectivity[]`/`pins[]` routing (wiring a shared net to every instance in
  the array) is out of scope — `connectivity[]`/`pins[]` still address only
  the array's base (row 0, col 0) tile. See "Array placement (a
  repeated-block regular tiling, #1053)" in `docs/cli/gen-compose.md`.
- 2026-08-16 — **`klt economy`** (issue #1012): quantitative layout-density
  report — the numbers backend for judging silicon economy (agent-produced
  layouts are correct-but-sprawling by default, and area is unit cost)
  before a layout-economy review can gate anything. Reports utilization
  (merged, non-overlap-double-counted drawn area over bbox area, both for
  the whole design and per library cell), a whitespace map (a configurable
  `--grid-cols`/`--grid-rows` coarse view plus the exact largest disjoint
  empty regions via `kdb.Region` boolean subtraction), bounding-box
  tightness (`tight_bbox` vs. `bbox`, aspect ratio) and grid-band-walked
  dead margins per edge, best-effort std-cell-row utilization for
  digital-looking blocks (inferred from instance placement geometry, no DEF
  `ROW` records needed), and an optional `--budget-um2` PASS/FAIL /
  `--reference-area-um2` ratio check. Builds on the `economy-review` skill's
  placeholder script (issue #1013, PR #1024) for the core utilization/
  margin math (already verified against the same two real canary GDS files
  used here — `blocks/sky130-bandgap`, `blocks/sky130_fd_sc_hd__buf_4`) but
  ships as a first-class, JSON-contracted `klt` verb under `src/`, with
  exact (non-rasterized) empty-region geometry, per-cell/row utilization,
  and the budget/reference blocks the placeholder didn't have. See
  `docs/cli/economy.md`.
- 2026-08-16 — `klt place-and-route`'s DEF→GDS merge now resolves a
  family-level KLayout LEF/DEF layer-map file (e.g. `gf180mcu.map`) when no
  exact `<variant>.map` exists, instead of silently proceeding without a
  layer map at all (issue #1029). Some open_pdks families — gf180mcu,
  confirmed against both `gf180mcuC` and `gf180mcuD` — ship this file as a
  single `libs.tech/klayout/tech/<family>.map` shared across every variant,
  unlike sky130's one-file-per-variant convention (`sky130A.map`,
  `sky130B.map`, …), which the resolver previously assumed unconditionally.
  The response gains the additive **`layer_map`** field (`{path,
  resolution}`, `null` unless `stage_reached` is `"route"`, mirroring
  `gds_path`) so a caller can see whether a map was applied and how it was
  resolved (`"exact"` / `"family"` / `"none"`) without reading source.
  `klayout_tools.lef_abstract`'s own duplicate `_resolve_layer_map` gets the
  identical fallback. No `schema_version` bump — purely additive to the
  response envelope; sky130's existing exact-variant-match behavior is
  unchanged.
- 2026-08-16 — `klt size` requests gain an optional **`target.vds_v`**
  (issue #1015): when set, sizes the device at a fixed `Vds` (the classical
  gm/Id lookup-table methodology) instead of the default diode-connected
  bias (`Vds=Vgs`). The generated ngspice deck holds `Vds` at exactly the
  requested value via an ideal voltage source, and a feedback-regulated
  gate bias (an ngspice behavioral source, resolved by ngspice's own DC
  Newton-Raphson solver — no extra invocation or outer search loop) servos
  `Vgs` to hit `target.id_a` at that `Vds`; the same bracket-and-interpolate
  width search, then a fresh confirmation run, reports the confirmed
  operating point exactly as before. The confirmed `Vds` is additionally
  echoed as the additive **`operating_point.vds_v`** (`null` in
  diode-connected mode, which has no independently-declared `Vds`).
  `target.vds_v` absent (the default) reproduces the original
  diode-connected deck byte-for-byte — no regression for existing callers.
  See `docs/cli/size.md`'s "Fixed-Vds bias mode" and the new
  `examples/size/cascode_request.json` worked example. No `schema_version`
  bump — purely additive to `request.target`/`response.operating_point`.
- 2026-08-15 — `klt functional-verification` requests gain
  **`options.defines`/`options.build_args`/`options.includes`** (issue
  #1001), forwarded to cocotb's own `Runner.build(defines=..., includes=...)`
  and the accumulated `build_args` list — the compile-time-defines mechanism
  an `ifdef`-gated Verilog cell library (e.g. a standard-cell PDK's own
  behavioural model, gated on `USE_POWER_PINS`/`FUNCTIONAL`) needs, and that
  the request schema had no path for before. `options.build_args` **composes
  with**, rather than replaces, the fixed `--coverage --trace` args a
  `options.coverage: true` run already adds — the effective build args are
  `["--coverage", "--trace"] + options.build_args` when both are given, so a
  user-supplied flag is appended last and can still override a coverage
  default. `options.includes` resolves each entry relative to the request
  file's own directory, the same convention `sources`/`testbench.module`
  already use. All three default to empty when omitted, so an existing
  request with none of them produces a byte-identical `Runner.build()` call.
  No `schema_version` bump — purely additive to `request.options`, same
  precedent as `random_seed`. See `docs/cli/functional-verification.md`'s
  "Compile-time defines, build args, and includes".
- 2026-08-15 — **SDF-annotated gate-level re-simulation**, both halves
  (issue #1002, Epic #700 Phase 3, `docs/design/post-route-sta-survey.md`
  §4.3). `klt place-and-route` gains the optional boolean
  **`post_route_sdf`** request field: one `write_sdf -divider .
  -include_typ` call inside the existing `post_route_spef` OpenSTA session,
  immediately after its `read_spef`, so the emitted IEEE-1497 delays are the
  real ones computed from the resolved liberty plus the extracted routed
  parasitics — never a synthetic or uniform model. The file is reported as
  the additive **`spef_sta.sdf_path`** (`null` otherwise). It requires
  `post_route_spef: true` (exit 1 otherwise): an SDF written from an
  `estimate_parasitics -global_routing`-fed session would carry the coarse
  estimate's delays while *looking* like a post-route measurement to
  everything downstream. `klt functional-verification` gains the matching
  optional **`options.sdf: {"file": ..., "corner": "min"|"typ"|"max"}`**
  block, implementing the recipe
  `docs/design/sdf-annotate-feasibility-spike.md` (#962) verified live: the
  `$sdf_annotate` call rides in a generated `klt_sdf_annotate` elaboration
  root (a cocotb regression's `hdl_toplevel` *is* the DUT, so there is
  nowhere else to put an `initial` block) carrying an **absolute** SDF path;
  the build gains `-gspecify -ginterconnect -s klt_sdf_annotate -T <corner>`;
  and `options.sdf` on `engine: "verilator"` — or against an `iverilog`
  older than 13.0, which has no `-ginterconnect` flag at all and would fail
  the build with a message naming neither SDF nor this request field, or
  alongside a `FUNCTIONAL` entry in `options.defines`, whose zero-delay cell
  models carry none of the `specify` blocks an SDF annotates (both
  issue #1004) — is exit 1, never a silent no-op.
  Because **every** Icarus SDF failure mode is non-fatal — an unopenable
  file, an unmatched instance, an unmatched `IOPATH` all leave `vvp` exiting
  `0` and cocotb reporting a clean zero-delay pass — both engine transcripts
  are scanned for `SDF WARNING`/`SDF ERROR` and any hit fails the run (exit
  1), with the benign `TIMINGCHECK not supported` class exempted so a real
  `write_sdf` output is not rejected wholesale. The response's
  `environment.sdf` (`null`, or `{file, corner, annotated: true}`) makes an
  annotated verdict distinguishable from a zero-delay one from the JSON
  alone. Verification status: the Icarus half is exercised end to end
  against real `iverilog` 13.0 by checked-in integration tests (the same
  testbench passes at zero delay and fails once a >sampling-window delay is
  annotated — survey §4.3's own coverage metric; a deliberately broken SDF
  is proven to produce a green `results.xml` that only the transcript gate
  catches); the OpenSTA half's generated Tcl is unit-asserted but **not**
  re-measured against a real `openroad` session (none available). Purely
  additive on both verbs — `schema_version` stays `1` per
  `docs/json-contract.md`. See `docs/cli/place-and-route.md`'s "SDF export"
  and `docs/cli/functional-verification.md`'s "SDF back-annotation".

- 2026-08-14 — `klt place-and-route`'s `"route"` stage now exports the
  **as-built gate-level netlist** and surfaces it as a new
  **`verilog_path`** response field (issue #996). The generated route-stage
  Tcl calls OpenROAD's own `write_verilog` immediately after its existing
  `write_def`, from the same linked design, writing
  `<output_dir>/<hdl_toplevel>.v` — so the netlist reflects the clock-tree
  buffers `clock_tree_synthesis` built, the gates `repair_design`/
  `repair_timing` resized, and the diodes `repair_antennas` inserted, all of
  which the routed `def_path`/`gds_path` contain but `klt synthesize`'s
  (pre-CTS) netlist cannot. Before this, a gate-level LVS run against a
  routed digital macro had no reference netlist that matched the layout: in
  one real run 40 of ~720 instances diverged (35 CTS/timing-repair cells,
  5 drive-strength resizes), every one an ordinary P&R optimization that
  `klt lvs` had no way to attribute. `write_verilog` is a top-level command
  of OpenROAD's always-loaded `dbSta` module
  (`src/dbSta/src/dbReadVerilog.tcl` → `sta::write_verilog_cmd`, verified
  against `The-OpenROAD-Project/OpenROAD@master` fetched 2026-08-14; ORFS
  calls the same command in `flow/scripts/final_outputs.tcl`) — unrelated to
  Yosys's identically-named command `klt synthesize` drives. Written at
  `"route"` only, never `"cts"`: the artifact is the netlist counterpart of
  a routed layout, so `verilog_path` is `null` before that stage exactly as
  `def_path`/`gds_path` are. Flags deliberately not passed:
  `-include_pwr_gnd` (matching ORFS's own `6_final.v`, and keeping the
  artifact directly diffable against `klt synthesize`'s netlist),
  `-remove_cells [find_physical_only_masters]` (this flow inserts no
  fill/tap/endcap cells, so it would only risk dropping a real one), and
  `-sort` (ignored by OpenROAD itself). Purely additive —
  `schema_version` stays `1` per `docs/json-contract.md`, and
  `def_path`/`gds_path` are unchanged. See `docs/cli/place-and-route.md`'s
  "As-built netlist (`verilog_path`)".

- 2026-08-14 — `klt extract --parasitics` (and `klt pex`) gain
  **`--mom-rlc-net`/`--mom-rlc-resistance-ohm`/`--mom-rlc-capacitance-ff`/
  `--mom-rlc-inductance-nh`** — substitute a caller-supplied, directly-solved
  R/L/C for one named net (e.g. the output of a separate `klt mom`
  Method-of-Moments run against that net's real geometry, Epic #701) in
  place of this extraction's own Phase 1/2 lumped-RC/coupling-C value for
  that net (issue #988, Epic #709 Phase 3a — the mechanical half of closing
  the loop between the MoM epic and the PEX epic). Unlike `--mom-net`
  (issue #798), which drives its own internal, idealised-ground-plate MoM
  solve and reports the comparison, this command never calls `klt mom`
  itself — the three values are opaque caller input, applied verbatim.
  `--mom-rlc-net` requires `--parasitics` and at least one of the other
  three (each independently optional); a name matching no net with
  ground-eligible parasitics geometry is a clean error, matching
  `--mom-net`'s "an explicit request should never silently fall back"
  convention. `--mom-rlc-resistance-ohm`/`--mom-rlc-capacitance-ff`
  genuinely replace the named net's series resistance/ground capacitance in
  both the written SPICE `R`/`C` card(s) and its `parasitics.nets[]` entry
  — net-scoped, every other net's parasitics (JSON and SPICE) untouched.
  `--mom-rlc-inductance-nh` is purely additive (there is no inductance term
  in the default RC-only model to replace): one series inductor
  (`kdb.DeviceClassInductor`, henries) is spliced between the named net's
  hub and its ground capacitor. Mutually exclusive with `--distributed-rc`
  naming the same net. New fields: `parasitics.l_count`,
  `parasitics.total_inductance_nh`, `parasitics.nets[].inductance_nh`, and
  `parasitics.mom_rlc_override` (echoes the applied substitution); `klt
  pex`'s `extraction.mom_rlc_override` mirrors the latter. Omitted (the
  default) leaves every field byte-identical to before this feature
  existed. See `docs/cli/extract.md`'s "Substitute a caller-supplied `klt
  mom` R/L/C for a critical net" section.

- 2026-08-14 — `klt extract --parasitics` (and `klt pex`) gain
  **`--critical-net`, repeatable** — lateral (same-layer, sidewall)
  coupling capacitance for caller-declared "nets that matter" (issue #976,
  Epic #709 Phase 2a: "high-impedance nodes, the SAR ADC's CDAC top plate,
  the PLL loop filter"). Issue #760 already models *vertical* (adjacent-
  metal-level) coupling unconditionally across the whole layout; this
  closes part of the remaining gap `PARASITIC_MODEL_SCOPE["coupling"]`
  names -- same-layer neighbours -- but only for a net pair naming one of
  the caller's declared `--critical-net` nets, not the whole layout: a
  full-layout lateral search is `docs/design/extract-fidelity-roadmap.md`'s
  own "medium cost" Stage 2b (real neighbour-search cost across every
  same-layer pair on a routed block), so this increment scopes the search
  instead of paying that cost unconditionally. Geometry via KLayout's own
  `Region.separation_check` (the same primitive `klt drc`'s `separation`
  checks use) within that metal level's own minimum-spacing DRC rule;
  coefficient from the PDK's public `defaultsidewall` value (sky130 only,
  citation-transcribed the same way `metal_overlaps` was). Additive, not
  deducted from either net's substrate fringe term (a documented
  simplification -- magic's own fringe-shielding model needs its
  `defaultsidewall` second parameter's semantics resolved first, an
  explicitly open roadmap question). New fields: `parasitics.critical_nets`
  (the request, echoed back) and `parasitics.nets[].coupled[].
  lateral_levels`; `parasitics.model.coupling`'s text changes again (same
  "additive behavior change" treatment issue #760's own change got).
  `--critical-net` omitted (the default) leaves every field byte-identical
  to before this feature existed. See `docs/cli/extract.md`'s "Lateral
  (same-layer, sidewall) coupling capacitance for critical nets" section.
  `klt pex` proof (issue #976's own "measurable, explainable delta"
  acceptance bar): `tests/test_pex.py`'s
  `test_run_pex_critical_net_lateral_coupling_canary` is a real,
  ngspice-driven `klt pex` run on a purpose-built two-net high-impedance-node
  fixture — the same `06-layout.gds`/`sky130-ota-5t` worked example Phase 1
  proved on (#973) has no current/charge-carrying high-impedance node for a
  coupling-capacitance delta to show up on (every measurement there is an
  ideal-voltage-source-driven hub, `delta_pct: 0.0` by construction), so
  this canary supplies one instead: `--critical-net` off reports
  `extracted_value: 0.0` on the victim net (Phase 1 baseline, unchanged);
  `--critical-net VIC` on the identical layout/testbench reports a real,
  nonzero, reproducible coupling voltage — re-derived from a real simulation
  on every CI run, not a one-off hand-captured evidence blob.

- 2026-08-14 — `klt extract --parasitics` (and `klt pex`) gain
  **`--distributed-rc`** (requires `--critical-net`) — replaces the
  single-lumped-element star R/C model with a distributed, multi-segment RC
  ladder for the same caller-declared "nets that matter" `--critical-net`
  already scopes lateral coupling onto (issue #977, Epic #709 Phase 2b,
  building on Phase 2a's #976). A single lumped hub overstates a net's own
  Elmore delay by roughly 2x versus a genuinely distributed line
  (`docs/design/extract-fidelity-roadmap.md`'s Stage 3); for a
  `--critical-net`-named net with 2 or more device terminals, its terminals
  are ordered along their approximate physical spread and its total
  resistance/capacitance is broken into `N - 1` series segment resistors
  (proportional to inter-terminal distance) and `N` per-terminal ground
  capacitors (the standard "half the capacitance of each adjoining segment"
  discretization) — both conserving the net's own totals exactly, so this
  changes *where* the R/C sits, not *how much* exists. A named net with
  fewer than 2 device terminals (nothing to chain) keeps the star model,
  reported in `warnings`, not an error. New fields: `parasitics.
  distributed_rc` (the flag, echoed back), `parasitics.nets[].rc_model`
  (`"lumped"` or `"distributed"`), and `parasitics.nets[].segments[]` (the
  ladder's per-segment resistors); a distributed net's own `nets[].
  terminals[]` entries carry `order`/`capacitance_ff` instead of the star's
  `resistance_ohm`. `parasitics.model.resistance`'s text changes again (same
  "additive behavior change" treatment issue #976's own `model.coupling`
  change got). `--distributed-rc` omitted (the default) leaves every field
  byte-identical to before this feature existed. See
  `docs/cli/extract.md`'s "Distributed (multi-segment) RC ladder for
  critical nets" section. `klt pex` proof (issue #977's own "measurable,
  more-explainable delta vs. Phase 2a's baseline" acceptance bar):
  `tests/test_pex.py`'s `test_run_pex_distributed_rc_canary` is a real,
  ngspice-driven `klt pex` run on a purpose-built high-impedance-node
  fixture (two poly resistors joined by a long li1 run) — with
  `--critical-net` alone (Phase 2a's own baseline model), a fast step
  reaches the internal node in one resistor hop with its full capacitance at
  the hub; with `--distributed-rc` added, the identical step now reads a
  measurably smaller early-sample-point voltage, reflecting the ladder's
  genuine extra propagation pole — re-derived from a real simulation on
  every CI run.

- 2026-08-14 — `klt power` gains the **static (DC) IR-drop solve** — issue
  #845, Phase 1b of the power/IR-drop + EM signoff epic #712. The spec file
  takes two new optional inputs: `pads` (where each net's supply is
  delivered, each held at a fixed `voltage_v`) and `current_model` (what
  each instance draws, as a `current_a` magnitude off a `supply_net` and
  back into a `ground_net`); the response gains two new fields,
  `ir_drop_map` (per-net/per-island/per-node voltages and droop, plus the
  per-branch currents Phase 1c's EM verdict will consume) and
  `worst_case_droop_mv`. Both are **additive** — every field Phase 1a
  promised is unchanged, so `schema_version` stays `1` per
  `docs/json-contract.md`'s envelope design — and a spec declaring neither
  `pads` nor `current_model` still runs extraction-only, with both fields
  `null`. The numerics live in a new, deliberately **geometry-free**
  `klayout_tools/ir_solver.py`: modified nodal analysis with pad nodes held
  as a Dirichlet boundary, solved per island with Jacobi-preconditioned
  conjugate gradients (no numpy/scipy — the runtime dependency set stays
  `klayout`/`jsonschema`), with `resistance_ohm: 0` edges merged as ideal
  shorts rather than divided by, and every island's iteration count and
  achieved residual reported rather than hidden. An island with no pad has
  no DC operating point and is reported `unsolved_reason: "no_pad"` — never
  guessed at — with any current stranded there totalled into
  `unsolved_current_a` and named in `warnings`. Validated two independent
  ways, per the epic's own reality-grounding requirement: against
  **canonical closed-form networks** (`tests/test_ir_solver.py` — a series
  ladder's `I*N*R`, a uniformly loaded rail's triangular sum
  `I*R*N*(N+1)/2`, a double-fed rail's textbook factor of 4, a current
  divider, a balanced Wheatstone bridge's zero bridge current, and the
  infinite-square-lattice Green's function at `R/2` adjacent and `2R/pi`
  diagonal — to `1e-9` relative on the exact cases, 0.5 %/1 % on the two
  lattice cases where finite-grid truncation of the *analytic* answer
  dominates), and against an **independent implementation** —
  ngspice's own `.op` DC operating point, the same engine `klt sim` already
  uses (`tests/test_power_ir_cross_check.py`), node for node and branch for
  branch, on a synthetic 2-D mesh and end to end on the real
  OpenROAD-produced `gcd` corpus fixture, agreeing to `1e-9` V and `1e-12`
  A. On that fixture (0.2 mA per `VPWR` rail, 17.6 mA total across 193
  islands) all 386 nodes solve, no current is stranded, per-island
  conservation holds exactly, and the worst droop is 4.84 mV. The per-net
  EM current-density verdict (#846, Phase 1c) remains a later phase. See
  `docs/cli/power.md`.
- 2026-08-13 — **new verb `klt pex`** (issue #801, Epic #709 Phase 1a):
  extract a lumped-RC parasitic-annotated netlist from a routed layout,
  re-run one or more existing `klt sim` testbench requests against it per
  corner, and report a **per-corner, per-spec-row schematic-vs-extracted
  delta** — a measured, explainable degradation on every spec row, not a
  bare "post-layout OK". It productizes the two-step manual workflow
  `docs/cli/sim.md`'s "Post-layout verification" and
  `.claude/skills/design-extraction/SKILL.md` already documented: it drives
  `klt extract --parasitics` itself, re-runs each testbench **completely
  unmodified** for the schematic leg, and for the extracted leg rewrites
  *only* the testbench's `.include`/`.inc` DUT reference to point at the
  freshly-extracted netlist (tagging `netlist_source: "extracted"`, reusing
  `klt sim`'s existing field rather than inventing a parallel testbench
  format). A testbench that inlines its DUT devices instead of `.include`ing
  them has no single swap point and is refused up front, not partially run.
  Each `delta[]` row's own `status` mirrors the **extracted-side**
  measurement's `klt sim` verdict against the `measurements[].limits` the
  caller already declared — never a second, undocumented delta-magnitude
  tolerance. The report pins the extraction method (`extraction.model`,
  `klt extract`'s `PARASITIC_MODEL_SCOPE` verbatim) and deck version
  (`provenance.deck`'s `sha256:` content hash) so Epic #709's later phases
  can store it as evidence without re-deriving provenance. The response
  shape (`delta[]` + `reference_netlist`) matches the provisional envelope
  issue #871 wired into `klt signoff`'s kind detector ahead of this command
  existing, so `klt signoff`'s T1 item-7 ("Post-layout verification")
  binding needed no change — the provisional-shape notes in
  `docs/cli/signoff.md` / `docs/design-evidence-tiers.md` are updated to
  point at the now-ratified contract. Note one deliberate divergence from
  those pre-existing notes' worked examples, which showed `klt pex
  extracted.spice schematic.spice` (two already-produced netlists): the
  shipped command takes a **routed layout plus a testbench set**, since it
  runs extraction itself. Exit codes `0`/`3`/`4` mirror `klt sim`'s
  precedent. `schema_version` starts at `1`. See
  [`docs/cli/pex.md`](docs/cli/pex.md).
- 2026-08-14 — `klt extract` gains `--def-net-names`, and `klt
  place-and-route`'s `post_route_spef` path uses it to take post-route SPEF
  annotation from **0% to 100%** of the design's nets (issue #951, Epic #700
  Phase 3). Extraction names nets from GDS text labels, and the `"route"`
  stage's DEF→GDS merge emits labels for top-level pins only, so every
  internal routed net reached the SPEF under a KLayout-synthesized `$<id>`
  name no OpenSTA net is called — measured live at `0` of `981`/`1904`/`449`
  annotated on `gcd`/`modexp`/`mult8` when `--spef` shipped (#948). The real
  names were in the routed GDS all along, as a **shape property** rather than
  a label: KLayout's LEF/DEF reader records each routed-net shape's DEF net
  name under `net_property_name` (default property `1`), and GDS
  `PROPATTR`/`PROPVALUE` round-trips it. `--def-net-names` reads that
  property and renames each extracted net accordingly; opt-in, because
  property `1` carries no guaranteed meaning in a GDS that did not come from
  a LEF/DEF merge, so every other layout's output is byte-identical to
  before. A run that opts in and finds no such property (or finds names that
  resolve to no extracted net) says so in `warnings` rather than silently
  changing nothing. **No routed-GDS artifact change and no corpus fixture
  regeneration were required** — the committed
  `tests/corpus/place_and_route/gcd.gds.gz` already carried all 458 of that
  design's DEF net names. Re-measured live against `openroad/orfs:latest`
  (OpenROAD `26Q3-1080-gab6fd26351`), sky130A, `seed: 1`: `537/537` (`gcd`),
  `760/764` (`modexp` — the 4 unmatched are tie-cell outputs with no routed
  geometry at all, so no wire and no parasitics either), `276/276`
  (`mult8`). Two additive `spef_sta` fields record the correlation from both
  directions — `design_nets_annotated`/`design_nets_total` (of the design's
  own nets, how many the SPEF names; the ratio `annotation_complete` is now
  keyed on) alongside the existing SPEF-side
  `nets_annotated`/`nets_total`, which cannot reach 1 by construction since
  flat extraction also emits intra-standard-cell nodes a gate-level design
  never had. Also fixes the `get_nets` net-name embedding, which
  backslash-escaped glob metacharacters inside a Tcl brace-quoted word —
  where no backslash substitution happens, so an escaped `a\[10\]` reached
  OpenSTA still carrying its backslashes and matched nothing (invisible
  while correlation was 0%). No `schema_version` bump. **Known remaining
  gap, now measured rather than assumed**: `read_spef` matches every net but
  still discards their RC networks, because `klt extract`'s SPEF omits
  device-terminal (`*I <instance>:<pin>`) connectivity by design (#948) and
  its `*CAP`/`*RES` node names therefore resolve to no pin in the linked
  design — `worst_slack` is bit-identical before and after `read_spef`
  (tracked as issue #961). See `docs/cli/place-and-route.md`'s "Still
  missing for real routed-RC timing" and `docs/cli/extract.md`'s
  `--def-net-names` section.

- 2026-08-13 — `klt place-and-route`'s `"route"` stage gains two new
  additive response fields, `worst_setup_slack_ns`/`worst_hold_slack_ns`
  (issue #949, Epic #700 Phase 3, `docs/design/post-route-sta-survey.md`
  §4.2), closing two gaps that survey's §1.2 documented: the `"route"`
  stage's OpenSTA session resolved exactly one PDK corner, and there was no
  hold-slack **value** anywhere (only `hold_violation_count`, a count). A
  new `klayout_tools.pdk.list_lib_corners()` helper enumerates every `.lib`
  timing corner a resolved `cell_library` ships (a small additive
  generalisation of the existing `_nominal_supply` per-file walk, excluding
  sky130's `_ccsnoise`-suffixed views — a same-PVT-point noise-model
  sibling, not a distinct corner, confirmed live against a real
  `openroad/orfs:latest` container to collide with `[WARNING STA-1140]` if
  loaded alongside its non-suffixed sibling). After the `"route"` stage's
  own single-corner script writes its checkpoint, a **second** OpenROAD
  invocation reads it back, `define_corners`s every enumerated corner,
  `read_liberty -corner`s each, and reports
  `report_worst_slack_metric -setup`/`-hold` — live-verified that OpenSTA
  automatically worst-cases setup at the slowest loaded corner and hold at
  the fastest (the standard sign-off convention), with no manual slow/fast
  classification needed. Deliberately a separate OpenROAD session, not more
  Tcl folded into the route stage's own: `report_worst_slack_metric` cannot
  scope its result back to one corner once more than one is loaded, so
  folding the sweep in would have silently turned the existing
  `worst_slack_ns`/`total_negative_slack_ns`/`setup_violation_count`/
  `hold_violation_count` fields from nominal-corner-only into swept-worst-
  case — the existing fields are confirmed unchanged (a strict
  backward-compatibility regression test). `null` before the `"route"`
  stage. The sweep is a real, non-zero wall-clock addition (a second full
  OpenROAD subprocess launch), not assumed free. No `schema_version` bump.
  See `docs/cli/place-and-route.md`.

- 2026-08-13 — `klt extract --parasitics` can now additionally write its
  per-net R/C model as a Standard Parasitic Exchange Format file (`--spef
  PATH`, issue #948, Epic #700 Phase 3, `docs/design/post-route-sta-survey.md`
  §4.1) — a pure format translation of the same data already reported in the
  JSON `parasitics` block and injected into the written SPICE, for
  `read_spef`-style STA consumption. Requires `--parasitics`. Every SPEF
  identifier is backslash-escaped for the characters SPEF's own (IEEE
  1481-1999) grammar reserves (`$`, `|`, `[`, `]`) — load-bearing, not
  cosmetic: KLayout's extracted net names routinely carry them, and a real
  OpenSTA `read_spef` aborts on the first unescaped occurrence
  (`[ERROR STA-1670]`), reproduced live and fixed here. New additive
  response field `spef_path` (`null` unless `--spef` was given). `klt
  place-and-route`'s `"route"` stage wires this in behind an opt-in
  `post_route_spef` request boolean (default `false`): after the DEF→GDS
  merge, `klt extract --parasitics --spef` runs against the merged routed
  GDS, and a second OpenSTA session (seeded from the `"route"` stage's own
  checkpoint) loads that SPEF via `read_spef`, reporting slack/violation
  metrics into the new additive `spef_sta` response field alongside — never
  replacing — the existing `estimate_parasitics -global_routing`-derived
  top-level fields. Net-name correlation between `klt extract`'s
  GDS-label-derived names and OpenSTA's own linked-design net list is
  checked explicitly (`get_nets -quiet`, before `read_spef` runs) and
  reported as `spef_sta.nets_annotated`/`nets_total` plus a plain-language
  `spef_sta.annotation_warning` when incomplete — measured live at 0%
  correlation on the `gcd`/`modexp`/`mult8` corpus (internal routed nets
  reach the GDS under KLayout-synthesized names, not OpenSTA's own; see
  `docs/cli/place-and-route.md`'s "Net-name correlation" subsection and
  follow-on issue #951), so `spef_sta`'s numbers must not be read as a
  real-parasitics measurement until that gap closes. Off by default — a
  real wall-clock addition (one more `klt extract --parasitics` pass plus
  one more `openroad` invocation), not assumed free. `null` before the
  `"route"` stage. No `schema_version` bump. See `docs/cli/extract.md` and
  `docs/cli/place-and-route.md`.

- 2026-08-13 — `klt place-and-route`'s `"route"` stage gains a new additive
  `route_drc_violation_count` response field (issue #938, Epic #700 Phase 2,
  `docs/design/native-routing-survey.md` §4.5), mirroring the existing
  `antenna_violation_count` pattern: TritonRoute's `detailed_route
  -output_drc <rpt>` already writes a real DRC report to disk on every
  `"route"`-stage run, but its violation count was never parsed into the
  JSON response until now. `_count_route_drc_violations()` counts the
  report's own literal per-violation `"violation type: "` header lines —
  the same literal TritonRoute uses both to write the report and to
  re-parse it internally, confirmed live via `strings` against a real
  `openroad/orfs:latest` build's `openroad` binary. `null` before the
  `"route"` stage, `int` (including a confirmed `0` on a DRC-clean run)
  from `"route"` onward. No `schema_version` bump. See
  `docs/cli/place-and-route.md`.

- 2026-08-13 — `klt place-and-route`'s `"route"` stage gains two additive,
  off-by-default request fields from issue #939's routing-flag audit (Epic
  #700 Phase 2, `docs/design/native-routing-survey.md` §4.1):
  `route_critical_nets_percentage` (0–100, default `0`) passes
  `global_route -critical_nets_percentage <percent>`, a real timing-aware
  congestion knob confirmed against OpenROAD's own upstream
  `src/grt/src/GlobalRouter.tcl`/`README.md` source; and
  `max_antenna_repair_iterations` (1–8, default `1`) generalises the
  existing single `repair_antennas`/`detailed_route` reroute pass (#759)
  into a bounded, flow-level multi-pass loop, mirroring
  OpenROAD-flow-scripts' own `MAX_REPAIR_ANTENNAS_ITER_DRT` shape —
  `repair_antennas`'s own `-iterations` flag was evaluated and deliberately
  **not** used directly, since OpenROAD's own source warns against it once
  `detailed_route` has already run (this stage's exact call pattern). A
  genuine negative result was also found: `detailed_route` itself carries
  no timing-driven or congestion-tuning flag at all. Both defaults
  reproduce the prior generated Tcl byte-for-byte; **not evaluated with a
  real OpenROAD A/B run** — no `openroad`/`openroad/orfs` container was
  reachable in this task's environment, unlike issue #783's live-container
  audit of `clock_tree_synthesis` — see `place_and_route.py`'s own module
  docstring for the full citations, methodology, and limitations. No
  `schema_version` bump. See `docs/cli/place-and-route.md`.

- 2026-08-12 — New verb `klt design-centering` closes the loop between the
  statistical/yield epic #710 and the analog-sizing epic #705: it turns a
  `klt yield-sensitivity` parameter ranking (issue #923) into re-centering
  candidates against a `klt size` sized device's own geometry — issue #924,
  Phase 3 of epic #710. Reads a request document naming a `sensitivity`
  report, a `sized_device` (single-device or coupled topology mode), and a
  caller-supplied `parameter_map` bridging the two commands' different
  naming conventions (`klt yield-sensitivity` ranks mismatch/process
  parameters like `vth_mismatch_m1`; `klt size` reports geometry keyed by
  device instance, e.g. `input_a`) — this command does not guess that
  mapping. Each mapped, ranked parameter gets a suggested area-growth
  multiplier via the standard Pelgrom mismatch-scaling law (`sigma ~ 1 /
  sqrt(area)`), a stated first-pass heuristic rather than a rigorous
  re-optimization. #705's own analog-sizing engine is still Phase 1 with no
  design-centering stage of its own, so this ships as the reference consumer
  the `klt yield-sensitivity` contract already reserved for it (no new field
  was added to that contract); if #705 grows a real design-centering stage
  later, it should wire into `ranking[].parameter`/`ranking[].contribution`
  directly. Validated end-to-end against #923's own known-dominant-parameter
  fixture (`examples/design-centering/`): the injected 10x-dominant mismatch
  term correctly surfaces as re-centering candidate #1, mapped to its sized
  device instance. See `docs/cli/design-centering.md`.

- 2026-08-12 — `klt synthesize` now reports a real gate-level critical path
  in a new, additive `sta` response field, computed by
  `klt-statime-native` (`native/statime/`) over the mapped netlist the run
  just produced — issue #925, Epic #704 Phase 3. The engine issue #809
  shipped as a go/no-go spike (native-Rust NLDM liberty parsing +
  rise/fall-aware timing graph, verified within 1.34% of an OpenSTA oracle
  on a 3-design corpus) is promoted here to a `pyo3`/`maturin` extension
  module (`klt_statime_native`, via a single `critical_path_json`
  `#[pyfunction]`) and called in process by `klayout_tools/sta.py`; the
  standalone `klt-statime` CLI binary still builds from the same crate, and
  the crate's numerics are unchanged. `sta` carries `worst_path` — the
  globally worst path, whatever its endpoints, with a full per-hop cell
  breakdown (`point`/`cell`/`edge`/`arrival_ns`/`slew_ns`) — plus
  `worst_reg_to_reg_path` (`null` for a purely combinational design), and
  echoes the uniform boundary condition it ran with. **Additive, not a
  replacement**: the existing `timing` field (ABC's `stime -p` pre-layout,
  wire-free, combinational-cone-only estimate, `source: "abc_stime"`,
  issue #807) is untouched, as is `--verify-equivalence`'s `klt equiv`
  acceptance gate (#808), so no `schema_version` bump is needed per
  `docs/json-contract.md`. **Still not signoff STA**, and issue #925
  resolved none of the spike's documented simplifications: no SDC and no
  `create_clock` (every primary input, *including the clock net*, gets a
  uniform 0.05 ns transition; every primary output a uniform 0.03 pF load —
  the same values the accuracy comparison ran with, and deliberately **not**
  derived from `constraints.clock_period_ns` or from ABC's own
  `-constr` driving-cell/load table, a different knob in different units),
  still wire-free/parasitic-free, register data pins identified by the
  literal pin name `D`, and 3 corpus designs is the whole verified sample.
  `delay_ns` is therefore a path delay, never slack. `sta` is `null` — never
  a fabricated number — when the optional `klt_statime_native` extension is
  not installed (it needs a Rust toolchain; `uv sync --group statime` from a
  checkout) or when the engine cannot analyze the netlist/liberty pair, and
  a missing extension never fails the run. Accuracy is re-verified through
  the *integrated* path, not just the standalone binary
  (`tests/test_sta_corpus.py`: `gcd` 1.34%, `mult8` 0.36%, `modexp` 0.45%
  vs. the checked-in OpenSTA oracle). See `docs/cli/synthesize.md`'s `sta`
  section and `native/statime/README.md`. Timing-*driven* restructuring —
  optimizing against this number rather than only reporting it — is #926's
  scope, not this change's.

- 2026-08-12 — `klt synthesize` gains an opt-in `--restructure-timing` flag
  (new `restructuring` response field, `null` unless given) that closes or
  reduces a setup violation on the `sta` stage's `worst_path`, via a bounded
  cell-resizing loop — issue #926, Epic #704 Phase 3, the last open phase of
  that epic. Each iteration finds the highest-contribution cell on the
  current worst path, looks up a same-family, higher-drive-strength,
  pin-compatible variant in the resolved liberty (the
  `<family>_<drive-strength-integer>` naming convention
  `_ABC_CONSTR_INPUTS`/`_TIE_CELLS` already document), swaps the one
  matching instantiation line, and re-measures via the same
  `compute_critical_path` call `sta` itself uses -- keeping the resize only
  if it strictly reduces the delay. Bounded: the loop stops -- reporting
  `converged` plus a `gave_up_reason` -- once it meets
  `constraints.clock_period_ns` (now required, along with a working `sta`
  stage, whenever this flag is given -- either missing is a hard failure,
  since the flag was explicitly requested), exhausts resizable candidates,
  produces a non-improving candidate, or reaches
  `--restructure-max-iterations` (default `8`); it never loops unboundedly.
  Any resize actually applied is validated by `klt equiv` against the
  source RTL before the run returns -- reusing `--verify-equivalence`'s own
  combinational-only scope and hard "non-equivalent verdict fails the run"
  discipline, via a separate `equiv_request_<top>_restructured.json` request
  file so both gates can be requested together without clobbering each
  other's artifact. The restructured netlist's path
  (`restructuring.restructured_netlist_path`, `null` when no resize was
  applied) is the documented netlist-handoff contract for #700
  (`klt place-and-route`) to prefer once that epic reaches its own timing
  phase -- #700 does not consume it yet, and this change does not require
  #700 to be unblocked. Only cell resizing is implemented in this
  increment; buffer insertion and re-mapping (the acceptance criteria's two
  named extensions) are not, and hold-time closure is not modeled at all
  (the `sta` stage reports only the worst/longest path, never a
  minimum-delay check) -- see `docs/cli/synthesize.md`'s "Timing-driven
  restructuring" section for the full scope and known limitations. Additive:
  `sta`/`timing`/`equivalence` are unaffected, no `schema_version` bump.

- 2026-08-12 — New verb `klt yield-sensitivity` ranks a completed Monte
  Carlo campaign's device/process parameters by their contribution to an
  output metric's variance — issue #923, Phase 3 of the statistical/yield
  epic #710. Reads a sensitivity sample document (per-sample parameter
  draws paired with a resulting output value) and emits a ranking, sorted
  descending by `|contribution|`, using a standardized regression
  coefficient (solved from the parameter-parameter correlation matrix) as
  the primary metric when the sample count supports it, with a
  Pearson-correlation fallback otherwise; Pearson `r` and Spearman `rho` are
  reported per parameter as corroborating measures, and every response
  states the method's own limitations (linear/monotonic effects only, no
  confidence interval on the ranking itself) rather than leaving them
  implicit. Deliberately correlation/regression-based, not a full
  Sobol/variance-based decomposition — a stated simplification, not an
  oversight. Ships in the same `klt_yield_native` Rust crate `klt yield`
  already uses (`native/yield/src/sensitivity.rs`), with a new
  `SENSITIVITY_SCHEMA_VERSION` independent of `klt yield`'s own
  `schema_version`. Validated against a synthetic campaign
  (`examples/yield-sensitivity/`) where one injected mismatch term is
  scaled 10x the others — the ranking correctly surfaces it first, an order
  of magnitude above every other parameter's contribution. See
  `docs/cli/yield-sensitivity.md`. Independent of #924 (wiring this ranking
  into the analog-sizing engine for design centering), which is not
  required for this ranking to be useful on its own.

- 2026-08-12 — `klt signoff --fleet`'s fleet-wide roll-up now resolves a
  canary's real verdict on the statistical (item 6, `klt yield`, #870) and
  post-layout (item 7, `klt pex`, #871) T1 items, instead of always naming
  them "blocked on statistical/post-layout evidence" — issue #872, Phase 2c
  of epic #706. `build_fleet_report()` needed no functional change: it has
  always reduced whichever `items[]` `build_tier_report()` renders, and that
  render has included items 6/7 since Phase 0 — what changed is that those
  two items can now be *satisfied* by real `klt yield`/`klt pex` evidence
  (#870/#871), so a manifest citing that evidence resolves its
  `blocking_item` to the item's actual reason (`met`, `check_failed`,
  `wrong_kind`, ...) rather than the pre-#870/#871 `unrecognized_envelope`.
  New regression coverage in `tests/test_signoff.py` walks a block through
  all four states (item 6 missing -> item 6 bound, item 7 missing -> item 7
  bound with a wrong-kind citation -> item 7 bound with real `pex`
  evidence -> `tier: "T1"`), plus a "measured, not asserted" test that runs a
  real `klt yield` subprocess against `examples/yield/`'s Monte Carlo
  campaign and confirms the block's `tier` changes from `null` to `"T1"`
  once that evidence is bound. No response-shape change; both
  `TIER_REPORT_SCHEMA_VERSION` and `FLEET_REPORT_SCHEMA_VERSION` are
  unaffected. See `docs/cli/signoff.md`'s "Fleet roll-up" section and its
  "Worked example: fleet roll-up across four canaries", which now carries a
  canary blocked on item 6 and shows that block reaching T1 once real
  `klt yield` evidence is bound.

- 2026-08-12 — `klt drc`/`klt extract`/`klt lvs` gain a third built-in deck,
  `sg13g2` (IHP-Open-PDK's SG13G2) — issue #905, Phase 3b of the DRC/LVS deck
  compiler epic #711, "the second PDK-generality proof" alongside sky130
  (Phases 0-2) and gf180mcu (Phase 3a). Unlike sky130/gf180mcu (both of which
  had a hand-curated deck *before* Epic #711 existed, so earlier phases
  backfilled `RuleProvenance` citations onto an already-shipping deck),
  `decks/sg13g2.py` did not exist before this issue — issue #524 (the
  traditional hand-written route) remains open, repeatedly rejected by
  Champion for an oversized single-PR scope, so this deck is transcribed
  with `RuleProvenance` citations from the start. 19 curated `DrcRule`
  entries (Activ, GatPoly, Cont, Metal1, Via1, Metal2, Via2 — width/space/
  enclosing/separation checks) transcribed from a real, pinned IHP-Open-PDK
  v0.3.0 install (`scripts/fetch-ihp-sg13g2.sh`, commit
  `5cccb161f7492697cfa52eb14dc03beb00bdca9e`)'s own `.drc` rule-deck files
  and `sg13g2_tech_default.json` threshold table, each cross-verified
  against both sources; every rule ships a golden violate/clean pair (14
  width/space rules via the existing `tests/golden_deck/` manifest
  mechanism, extended to a third deck; the 5 enclosing/separation rules via
  hand-written pairs in `tests/test_drc.py`, mirroring sky130/gf180mcu's own
  pattern). `EXTRACTION_DECK` recognises thin-oxide ("-LV") NMOS/PMOS
  (`sg13_lv_nmos`/`sg13_lv_pmos`, transcribed from `mos_extraction.lvs`),
  each with a golden layout→netlist pair in the new
  `tests/test_sg13g2_deck.py`; the thick-oxide ("-HV") voltage domain is
  registered as an unmodeled-marker gap (`ThickGateOx`, 44/0), the same
  mechanism gf180mcu's `Dualgate` already uses. Issue #905's own acceptance
  criteria ask for a cross-check against #524's hand-written deck if it has
  landed — it has not (still open, unmerged), so this deck ships with
  golden-pair validation only, stated explicitly rather than silently
  skipped (see `sg13g2.py`'s own "No #524 cross-check" docstring section).
  Resistor/capacitor/bipolar/diode device recognition and RC parasitics are
  not curated in this increment (`ParasiticsDeck()` registers empty, so
  `--parasitics` reports an honest uncalibrated gap rather than an "unknown
  deck" error) — a follow-on scope, the same incremental-growth pattern
  sky130/gf180mcu's own coverage already followed.

- 2026-08-12 — `klt yield` gains variance-reduction sampling strategies —
  issue #907, Phase 2b of the statistical/yield epic #710. A measurement's
  input can now declare `sampling: {"strategy": "latin_hypercube",
  "replicates": N}` (a replicated Latin-hypercube design — McKay, Conover &
  Beckman 1979) or `sampling: {"strategy": "importance", "weights": [...]}`
  (a Horvitz-Thompson importance-weighted estimate with a delta-method
  interval — Hesterberg 1995) as alternatives to Phase 1's plain random
  Monte Carlo, plus Kish's effective sample size for importance sampling.
  Both add a strategy-aware `yield.variance_reduced` estimate and a
  `sample_size.variance_reduced` precision verdict alongside the existing
  `yield.empirical`/`yield.normal` fields, and every measurement now always
  carries a `sampling` report (`"plain_random"` by default) naming which
  strategy produced its estimate. Validated against known analytic
  distributions, not just run: replicated LHS reaches the analytic yield
  with a measurably tighter confidence interval than plain MC at a matched
  total sample count, and importance sampling resolves a one-sided
  4-sigma tail (`Phi(4) = 0.99996833...`) that plain MC cannot at a
  smaller sample budget (`native/yield/src/estimate.rs`'s own tests).
  Purely additive — `schema_version` stays at `1`. See
  `docs/cli/yield.md`'s "Sampling strategies (variance reduction)" section.
- 2026-08-12 — New command `klt yield-campaign` launches and manages a
  Monte Carlo yield campaign directly, rather than requiring a pre-run `klt
  sim` report — issue #906, Phase 2a of the statistical/yield epic #710.
  A campaign spec is a `klt sim` request document with a mandatory
  `monte_carlo` block; a `monte_carlo.seed` omitted from the spec is
  derived deterministically from the spec's own sampling-relevant content
  (netlist/analysis/measurements/corners/`monte_carlo.n`/`vary`), so the
  same spec re-run — on one host or sharded across a fleet — always
  reproduces the same sample set. Dispatch is handed straight to `klt
  sim`'s own `--backend`/`--hosts`, reusing Epic #375's shard/merge engine
  for the corner x Monte-Carlo-sample grid unchanged, which this issue also
  finishes wiring into `klt sim` itself: `backend: "remote"` with `hosts >
  1` now provisions a real, guarded K-instance EC2 fleet
  (`remote_fleet.run_fleet`, Epic #375 Phase 1B/#377) instead of raising
  "not yet supported" — each shard is pushed its own already-expanded,
  already-seeded slice of the unit list (`request._explicit_points`, an
  internal wire field) rather than re-deriving from `corners`/`monte_carlo`
  ranges on the remote box, so sharding never changes a unit's own value or
  its derived seed. `klt yield-campaign`'s response is `klt yield`'s own
  Phase 1 yield-report JSON, produced by that exact pipeline unmodified
  against the resulting sample set, plus one added `campaign` provenance
  block (resolved seed/source, requested samples, backend/hosts, sim
  status). See `docs/cli/yield.md`'s "Campaign orchestration" section and
  `docs/cli/sim.md`'s "Fleet sharding" section.

- 2026-08-12 — `klt mom` gains port definition + de-embedding, reporting
  **de-embedded S-parameters** for the full-wave sweep's canonical two-port
  transmission-line case — issue #894, Phase 2b of the Method-of-Moments
  epic #701. A two-entry `ports` array in the spec file (each entry a
  `position_um` reference-plane location plus an optional
  `reference_impedance_ohm`, default 50 ohm) turns the raw partial-impedance/
  characteristic-impedance output Phase 2a (#893) added into the standard
  RF two-port network representation: the full modeled structure is treated
  as three cascaded uniform-line segments (feed/DUT/feed) at the line's own
  `Z0(omega)`/`gamma(omega)`, and the DUT's ABCD matrix is recovered by
  cascading the inverse of each feed segment's ABCD matrix around the
  total, then converted to S-parameters at each port's reference impedance
  (the standard ABCD-to-S conversion). Validated against the classical
  matched-transmission-line closed form (`S11 == S22 == 0`, `S21 == S12 ==
  exp(-gamma*L)`) and, for the de-embedding step specifically, against
  modeling the device-under-test segment alone with no feed stubs at all
  (`tests/test_mom_ports_validation.py`,
  `native/mom/src/fullwave.rs`'s own Rust-level tests). Purely additive —
  new response fields (`ports`, and each `full_wave_sweep` entry's
  `s_parameters`) present only when requested; `schema_version` stays at
  `2`. See `docs/cli/mom.md`'s "Port definition and de-embedding" section.

- 2026-08-12 — `klt mom` gains an optional frequency-domain, full-wave
  partial-impedance sweep — issue #893, Phase 2a of the Method-of-Moments
  epic #701 (the entry point for RF/EM blocks and S-parameter extraction).
  Setting a non-empty `frequencies_hz` in the spec file (plus the optional
  `segment_size_um` mesh knob) solves each conductor pair's complex partial
  impedance at every requested frequency via a retarded free-space Green's
  function (`exp(-jkR)/(4*pi*R)`), reusing PEEC's bar-shaped-conductor
  restriction but refining the mesh axially (one equivalent thin wire per
  conductor) rather than across the cross-section. For the canonical
  two-conductor transmission-line case, it additionally derives the
  characteristic impedance and propagation constant from the per-unit-length
  series impedance and a differential-mode line capacitance. Validated
  against the classical two-wire-line closed form and the lossless-TEM
  propagation identity, and shown to converge under axial mesh refinement
  (`tests/test_mom_fullwave_validation.py`,
  `native/mom/src/fullwave.rs`'s own Rust-level tests). Purely additive —
  new response fields (`full_wave_sweep`, `full_wave_segment_count`,
  `segment_size_um`) present only when requested; `schema_version` stays at
  `2`. See `docs/cli/mom.md`'s "Full-wave frequency sweep" section.

- 2026-08-12 — Ran `klt yield` end to end against a real canary's existing
  Monte Carlo campaign — issue #818, Phase 1c of the statistical/yield epic
  #710. `2AMLogic/gf180-sar-adc`'s `sim/mc-cdac-mismatch/` (CDAC unit-cap
  mismatch → INL/DNL, N=20000, ratified spec row) was chosen over the two
  bandgap canaries as the more complete existing MC record. Produced a
  complete T1-row statistical record — yield estimate, CI, Cpk/sigma-to-spec,
  sample-size verdict, a real seeded negative control (mismatch sigma forced
  to 3x nominal, N=2000), and an analytic cross-check against the
  experiment's own closed-form Pelgrom-law prediction — recorded in the
  canary's `sim/` directory per its append-only evidence convention
  ([2AMLogic/gf180-sar-adc#149](https://github.com/2AMLogic/gf180-sar-adc/pull/149)).
  `klt yield`'s independently-computed `sigma_to_spec` agreed with the
  canary's own bespoke-script `sigma_at_spec` to 3 significant figures. No
  code change to `klt yield` itself (already complete as of #816/#837 and
  #817/#902) — `docs/cli/yield.md`'s new "Real canary evidence" section has
  the full result table, the negative-control's honest
  detected-on-3-of-4-rows finding, the manifest snippet showing zero-bespoke
  -parsing consumption by `klt signoff`'s Phase 2a binding (#870/#880), and
  an explicit check against `docs/design-evidence-tiers.md` item 6's bar
  (satisfied except the process-corner-combination leg, stated as a known
  gap, not hidden).

- 2026-08-12 — Cross-checked the compiled sky130 LVS device-extraction rules
  (`EXTRACTION_DECK`, issues #868/#867) against the real, hand-written
  upstream `sky130.lvs` deck they were transcribed from — issue #869, Phase
  2c of the DRC/LVS deck compiler epic #711. A new
  `klayout_tools.extract.run_extract_klayout_engine` helper drives sky130's
  native LVS/device-extraction deck (resolved via the new
  `klayout_tools.pdk.lvs_deck_file`) through the standalone `klayout`
  binary and reads back an extracted device netlist comparable to
  `run_extract`'s own — the LVS-device-extraction counterpart of
  `run_drc_klayout_engine`/`--engine klayout` (issue #565), the same
  native-deck cross-check mechanism Phase 1 (#747) used for the DRC side.
  Issue #520's Tiny Tapeout corpus is still an open, unbuilt epic with no
  vendored layouts in this repo, so — mirroring Phase 1's own and issue
  #860's own precedent — `tests/test_lvs_native_extraction_cross_check.py`
  reuses the golden-layout-per-rule convention as the corpus stand-in.
  Verified against a real `volare`-fetched sky130A install and a real
  KLayout 0.28.16 binary: 7 of 8 provenance-cited device rules were run (the
  `pnp` bipolar is investigated and explicitly deferred, not silently
  dropped); 4 agree with the native deck exactly, and 3 disagree for a
  documented, already-known reason (`res_high_po`'s issue #518 refinement,
  `cap_mim`/`cap_mim_m4`'s issue #512 perimeter term) — no undocumented
  disagreement was found. No response-shape change (`run_extract`/`klt
  extract`'s own output is untouched; the new engine is a Python helper, not
  yet a CLI flag — see `docs/cli/extract.md`'s new "sky130 native-deck
  (`sky130.lvs`) LVS device-extraction cross-check" section for the full
  writeup and scope note).

- 2026-08-12 — `klt erc` gains a per-gate antenna-ratio verdict — issue
  #860, Phase 1b of the antenna + ERC signoff epic #713, built on Phase
  1a's (#859) layer-by-layer connectivity model. Every non-gate `stackup`
  level's `antenna_ratio` (`cumulative_area_um2 / gate_area_um2`) is now
  reported, and, when the new `--pdk` flag names a supported PDK
  (currently `sky130`), compared against that PDK's real antenna-ratio
  limit for the role, adding `antenna_ratio_max`/`antenna_ratio_source`/
  `verdict` (`"pass"`/`"violate"`/`"unchecked"`) per level and an aggregate
  `antenna_verdict` per gate. sky130's limits (`li1`: 75, `met1`: 400,
  `met2`: 400) are transcribed from the official SkyWater PDK repository's
  own published antenna-rule table
  (`google/skywater-pdk`, `docs/rules/antenna/table-Ia-antenna-rules-s8d.csv`,
  "Max EA/A w/o diode" column), verified stack-invariant across every
  sky130 metal-stack option table checked; the gate role itself is never
  PDK-checked (its ratio is trivially `1.0`, and the source table's poly
  rule measures perimeter, not cumulative area). Validated against golden
  violate/pass layout pairs for every checked layer, cross-checked against
  klayout's own independently-implemented `LayoutToNetlist.antenna_check`
  engine on the same fixtures (the Tiny Tapeout corpus named in epic
  #713's own acceptance criteria, issue #520, has no ingestion harness or
  cached GDS yet, so is not yet usable for this cross-check — see
  `docs/cli/erc.md`'s "Cross-checked against klayout's own built-in
  antenna engine" section). Purely additive: `schema_version` stays `1`,
  and every field Phase 1a shipped is unchanged. See `docs/cli/erc.md`.

- 2026-08-12 — `klt yield` now enforces the self-checking discipline issue
  #817 requires (Phase 1b of the statistical/yield epic #710, building on
  #816/#837): a per-measurement `negative_control` and `analytic_cross_check`
  block, both reported alongside the yield estimate rather than as a
  separate command. A **negative control** is a seeded, known-bad variant's
  own samples, analysed against the same limits as the nominal draw; the
  self-check verdict (`"detected"` / `"not_detected"`) requires not just a
  lower point estimate but two **non-overlapping** exact (Clopper-Pearson)
  confidence intervals — a difference too large to be sampling noise, mirroring
  the never-a-bare-point-estimate discipline the rest of the command already
  enforces. A campaign where no measurement declares a `negative_control` at
  all, or whose negative control fails to show the expected degradation, is
  flagged with a run-level warning rather than silently accepted. An
  **analytic cross-check** compares a measurement's empirical mean/stddev
  against a closed-form prediction: `ktc_noise` (`sigma = sqrt(kB*T/C)` for
  a given sampling capacitance and temperature) or `mismatch_offset` (a
  caller-supplied sigma, e.g. from a Pelgrom-model prediction already
  evaluated), each with its own confidence interval via the same asymptotic
  approximation the `normal` yield estimator's delta method already uses;
  `"consistent"` requires **both** the analytic mean and stddev to fall
  inside their empirical intervals. Both blocks are optional per measurement
  and additive to the JSON payload — `schema_version` is unaffected. See
  `docs/cli/yield.md`'s "Negative control" and "Analytic cross-check"
  sections.

- 2026-08-12 — Every sky130 LVS device-extraction rule that carries a
  `RuleProvenance` citation (issue #868, Phase 2a) now also ships a golden
  layout→netlist pair validating it end-to-end — issue #867, Phase 2b of
  the DRC/LVS deck compiler epic #711. Phase 2a validated one entry per
  named device *class* as its model-definition pilot (MOSFET: `nfet` only;
  resistor: `res_generic_po` only; capacitor: `sky130_fd_pr__model__cap_mim`
  only); this closes the remaining five: `pfet`, `res_high_po`,
  `res_xhigh_po`, `sky130_fd_pr__model__cap_mim_m4`, and the `pnp` bipolar
  — all 8 of `EXTRACTION_DECK`'s provenance-cited device rules now have a
  golden pair, mirroring Phase 1's own width/spacing golden-pair discipline
  applied to the LVS side. A new coverage test,
  `test_golden_pairs_cover_every_provenanced_sky130_device_rule`
  (`tests/test_lvs_device_provenance.py`), asserts this 1:1 coverage stays
  true — a future provenance-backfilled device rule that ships without a
  matching golden pair fails it, the same discipline
  `tests/golden_deck/`'s own coverage test already enforces for `klt drc`'s
  width/space rules. No response-shape change (pure test/doc addition); see
  `docs/cli/extract.md`'s "Device rule provenance" section.

- 2026-08-12 — LVS device-extraction rules now carry the same machine-readable
  `RuleProvenance` citation `DrcRule.provenance` introduced for DRC rules in
  issue #747 — issue #868, Phase 2a of the DRC/LVS deck compiler epic #711.
  `ResistorDevice`, `CapacitorDevice`, `BipolarDevice`, and `DiodeDevice` each
  gain an optional `provenance` field, and `ExtractionDeck` gains
  `nfet_provenance`/`pfet_provenance` for MOS recognition (which has no
  per-entry list the way the others do — a deck declares exactly one NMOS and
  one PMOS rule). sky130's curated `EXTRACTION_DECK` backfills all five: MOS
  (`nfet`/`pfet`), all three resistor entries, both MiM-capacitor entries, and
  the one bipolar (PNP) entry, each citing the real upstream `sky130.lvs`
  device-class name (e.g. `"sky130_fd_pr__nfet_01v8"`,
  `"sky130_fd_pr__res_generic_po"`) from `efabless/sky130_klayout_pdk` — a
  different upstream repo than the DRC-side `.lydrc`/`.drc` scripts
  `DrcRule.provenance` cites. gf180mcu's entries are left unpopulated
  (`None`) for this pass. Validated against golden layout→netlist pairs for
  all three of the issue's named device classes (MOSFET, resistor,
  capacitor) — see `tests/test_lvs_device_provenance.py` and
  `docs/cli/extract.md`'s "Device rule provenance" section. Like
  `DrcRule.provenance`, not yet surfaced in `klt extract`'s JSON output.

- 2026-08-12 — `klt signoff`'s tier-verdict mode now binds the T1
  checklist's post-layout item (item 7, "Post-layout verification") to a
  `klt pex` schematic-vs-extracted-netlist delta report, and makes item 7
  the first — and so far only — **kind-restricted** item: it renders
  `"met"` only on a `pex`-kind citation. Issue #871, Phase 2b of epic
  #706. Previously `_build_tier_item` was kind-agnostic per item, so a
  manifest could satisfy item 7 with *any* recognised, passing envelope —
  a clean `klt drc` report, or a pre-layout schematic `klt sim` — none of
  which prove anything about post-layout behaviour. A passing citation of
  a kind the item does not accept now renders `"unmet"` with a new
  machine-readable `reason: "wrong_kind"`, grouped with the other "no
  runnable check exists for this item" reasons rather than with
  `"check_failed"` (the cited check did not fail on its own terms; it
  simply does not prove what this item requires). Items 1-6 and 8-10 are
  unaffected — they pass `allowed_kinds=None` and keep the original
  unrestricted behaviour. **The `pex` envelope shape is provisional.**
  `klt pex` (Epic #709) does not exist in this codebase yet, and its
  defining issue #801 ("Define `klt pex`") is stalled with an empty body
  under `loom:operator-only`/`loom:operator-decision` — there is no
  ratified JSON shape to build against. `_classify` therefore recognises a
  Curator-proposed, provisional shape (a top-level `delta` list plus a
  `reference_netlist` field, mirroring how `sim` is detected by
  `measurements`+`corner_count` and `extract` by `device_count`+`nets`),
  chosen narrowly enough that #801's eventual real shape is very likely
  additive to it rather than a rewrite. Reconcile against #801 once it
  lands. See `docs/cli/signoff.md`'s "Item 7 is kind-restricted: `klt
  pex`" section.

- 2026-08-12 — `klt signoff`'s tier-verdict mode now grades the T1
  checklist's statistical-evidence item (item 6, "Statistical claims carry
  Monte Carlo evidence") against a `klt yield` report — issue #870, Phase 2a
  of epic #706, extending the gate binding Phase 1 (#825) shipped for the
  four deterministic gates (DRC/LVS/netlist regeneration/corner sim) to the
  statistical item. `_classify`/`_check_passed`/`_detail` now also recognise
  a `klt yield` (issue #816, Phase 1a of epic #710) JSON envelope, reachable
  via item 6's `evidence` entry exactly like every other kind — file-backed
  or command-backed, no new evidence shape. A `"met"` verdict passes on
  `status: "pass"` or `status: "reported"` (no measurement declared a
  `target_yield`, so nothing could fail); `status: "fail"` renders
  `"unmet"` with `reason: "check_failed"`, same as every other kind. `klt
  yield`'s current JSON shape carries no `provenance` block of its own —
  unlike drc/lvs/extract/sim, its envelope names no content hash for the
  Monte Carlo sample document it analysed — so the citation's `content_hash`
  is instead computed by hashing the referenced samples document directly
  (`_yield_samples_content_hash`), preserving the same input-hash staleness
  discipline Phase 1 established rather than leaving a `"met"` yield
  citation with no input hash at all. An item with no backing Monte Carlo
  campaign evidence renders `"unmet"` via the same `no_evidence` reason
  every other item uses — there is no separate code path that could
  fabricate a `"met"` for it. See `docs/cli/signoff.md`'s "`klt yield`
  evidence and content hashing" section.

- 2026-08-12 — New verb `klt erc` builds a per-gate, layer-by-layer
  connectivity model — issue #859, Phase 1a of the antenna + ERC signoff
  epic #713. A JSON spec declares a `stackup` (fabrication order from the
  gate/poly layer up through the metal stack; `stackup[0]` sets `"role":
  "gate"`) and optional `vias` (bridging two stackup roles). Connectivity is
  traced with `klayout.db.LayoutToNetlist` used purely for wire/via
  connectivity (no device recognition) — the same API `klt power` already
  uses for its own resistive-network extraction. For every net whose
  geometry includes the gate-role layer, `klt erc` reports that net's own
  merged area at each fabrication step plus a running cumulative total
  (`gates[].levels[].step_area_um2`/`cumulative_area_um2`) — "per gate"
  means "per electrically distinct gate net", matching real
  process-antenna-area-ratio (PAAR) methodology directly, and needs no net
  labelling to discover (unlike `klt power`'s caller-named `power_nets`).
  This phase delivers the `klt erc` interface and the connectivity model
  only — the per-gate antenna-ratio verdict against a PDK limit (#860,
  Phase 1b) and the core ERC finding list (#861, Phase 1c) are later phases
  that add response fields additively (no `schema_version` bump needed for
  either). See `docs/cli/erc.md`.

- 2026-08-12 — `klt erc` additively reports `erc_findings`: the four core
  ERC rules — floating gate, unconnected/multiply-driven net, missing
  substrate/well tie, supply short — issue #861, Phase 1c of the antenna +
  ERC signoff epic #713, building on #859's Phase 1a connectivity model.
  `erc.floating_gate` needs no additional spec input — any gate net whose
  accumulation stops immediately after the gate role (`step_area_um2 ==
  0.0` at every level above it) is flagged directly from the existing
  `gates[]` model. Two new optional spec sections drive the rest:
  `nets` (named nets to check, mirroring `klt power`'s `power_nets` but
  with an added `"kind": "signal" | "supply"`) and `ties` (substrate/well
  tie declarations: a well/tub layer, a tap layer, the `stackup` role the
  tap connects up to, and the net it must reach). A declared net matching
  zero or more than one disconnected island is `erc.unconnected_net`; two
  different declared names that resolve to the same electrical island are
  `erc.multiply_driven_net`, or `erc.supply_short` when both are declared
  `"kind": "supply"` — recovered from `klayout.db.Net.expanded_name()`'s
  own comma-joined name for a shorted net (verified empirically for this
  issue), not a second connectivity pass. Every well/tub shape without a
  connected tap to its declared net is `erc.missing_tie`. Each finding
  reports `rule`/`description` plus the specific `net`/`other_net`/
  `gate_id`/`layer`/`bbox` implicated, mirroring `klt drc`'s
  `violations[]` shape. No `schema_version` bump (purely additive fields).
  See `docs/cli/erc.md`.

- 2026-08-12 — `klt signoff --fleet <file>` adds a third mode to `klt
  signoff`: a fleet-wide tier roll-up (issue #827, Phase 1c of epic #706).
  Given a **fleet manifest** naming several blocks (each a per-block
  manifest, inline or by path), it calls the existing tier-verdict machinery
  (`klt signoff --manifest`, #722/#825/#826) once per block and reports
  each block's current tier plus, for any block not yet at T1, the single
  T1 item still blocking it — turning "which canaries are at which tier,
  and what's blocking each not-yet-T1 block" into one query instead of a
  per-block survey. No evidence is read or graded independently: a block's
  roll-up row is a pure reduction of its own tier report, so the two can
  never disagree about *why* it isn't T1. New top-level JSON shape
  (`schema_version: 1`, independent of the other two modes'
  `schema_version`s) — `block_count`/`t1_count`/`not_t1_count` plus one
  `blocks[]` entry per fleet member (`block`, `source`, `kind`, `tier`,
  `t1_item_count`, `t1_met_count`, `blocking_item`). Exits `0` only when
  every block in the fleet is `tier: "T1"`, `3` otherwise, `1` on a
  malformed fleet/block manifest. See `docs/cli/signoff.md`'s "Fleet
  roll-up" section.

- 2026-08-12 — New verb `klt power` extracts a routed layout's named
  power/ground nets into a resistive network (nodes + segment resistances) —
  issue #844, Phase 1a of the power/IR-drop + EM signoff epic #712. A JSON
  spec declares `power_nets` (names to extract), a `stackup` (metal roles:
  drawing layer, optional pin/label layer, sheet resistance), and optional
  `vias` (bridging two stackup roles, with a resistance). Connectivity is
  traced with `klayout.db.LayoutToNetlist` used purely for wire/via
  connectivity (no device recognition) — the same API `klt extract` uses
  internally, scoped down to only the declared power-grid layers. A named
  net commonly resolves to several electrically disconnected **islands**
  (reported separately, each its own `island_id`) rather than one connected
  mesh: `klt place-and-route` deliberately runs no PDN generation, so a
  routed design's power/ground geometry is whatever the standard-cell rows
  themselves contribute. Validated against the real, OpenROAD-produced
  `gcd` corpus fixture (`tests/corpus/place_and_route/gcd.gds.gz`): 88
  separate `VPWR` islands and 105 separate `VGND` islands, 386 nodes, 193
  edges, zero warnings. This phase delivers the `klt power` interface and
  the resistive-network extraction only — the static IR-drop solve (#845)
  and the per-net EM verdict (#846) are later phases that add response
  fields additively (no `schema_version` bump needed for either). See
  `docs/cli/power.md`.

- 2026-08-12 — **new verb `klt yield`** (#816, Phase 1a of the
  statistical/yield epic #710): a Monte Carlo sample set plus spec limits in,
  a yield estimate with confidence intervals, a distribution fit,
  Cpk/sigma-to-spec, and a sample-size verdict out. Both input shapes are
  accepted and auto-detected — a `klt sim --format json` Monte Carlo report
  is consumed **directly** (samples from `corners[]` entries carrying a
  `monte_carlo` block, limits from the report's own
  `measurements[].limits`), so the record format the canary MC harnesses
  already produce needs no intermediate format; a plain sample-set document
  (`{"measurements": [{"name", "samples", "limits"}]}`) covers a draw that
  came from somewhere else. A `--limits` spec file supplies or overrides the
  limits and can carry run-level `confidence`/`target_ci_halfwidth`/
  `min_samples`/`target_yield` defaults.

  **The tool never emits a bare point estimate, and enforces that rather
  than advising it.** Every yield number is an object carrying `estimate`,
  `confidence`, `confidence_interval`, and `n` — a shape the Rust response
  types make impossible to bypass, with a final guard rejecting a
  non-finite or inverted interval before serialisation. A request that could
  only produce a bare number is an **error** (exit `1`), not a warning:
  fewer than 2 usable samples, `--min-samples` below the hard floor of 2,
  `--confidence 0` or `1`, or a measurement with neither `min` nor `max`. A
  zero-failure run is reported as `[low, 1]` with a warning spelling out the
  honest statement ("at least 98.78% at 95% confidence, N = 300", not "100%
  yield"). A declared `target_yield` passes only if the **lower** confidence
  bound reaches it, and the sample-size block answers two separate questions
  — `required_n` (samples for the requested interval half-width) and
  `required_n_for_target` (samples for the *claim*, searched against the
  same exact interval the report publishes, `null` when the observed pass
  rate makes the target unreachable at any N).

  Two estimators ship side by side: `empirical` (exact Clopper-Pearson,
  assumption-free) and `normal` (delta-method interval over the fitted
  normal), with an Anderson-Darling normality verdict and a warning when the
  normal fit is rejected. Statistics run in the new `klt_yield_native` Rust
  extension (`native/yield/`, this repo's third Rust component after
  `native/mom/` and `native/congestion/`) — dependency-free numerics
  (Cody `erfc`, Acklam inverse normal CDF, Lanczos log-gamma, modified-Lentz
  incomplete beta) each with its own closed-form unit test. Like `klt mom`,
  it is an **optional** PEP 735 dependency group (`uv sync --group yield`):
  every other `klt` verb still installs with no Rust toolchain in sight, and
  an unbuilt extension is a clean error pointing at the build instructions.
  Exit codes: `0` pass, `1` failed to run, `3` a yield claim was not
  supported at the stated confidence. Worked example in `examples/yield/`
  (both input shapes over one seeded 300-sample draw); see
  `docs/cli/yield.md`.

- 2026-08-12 — `klt extract --parasitics --mom-net <net>` cross-checks (and
  replaces) one net's lumped-RC ground capacitance with `klt mom`'s
  Method-of-Moments field solver (#798, Phase 1b of the MoM epic #701 —
  "prove the shipped MoM solver improves real extraction fidelity, not just
  canonical benchmarks"). The named net's geometry on each of the deck's
  `metals` roles is read via the same `LayoutToNetlist.polygons_of_net` call
  `--parasitics` itself uses; each shape's bbox becomes a `klt mom` conductor
  panel, paired with a synthesized ground plate directly beneath it (z-gap
  inverted from the deck's own `cap_area_ff_um2` coefficient at a fixed 3.9
  relative permittivity, padded 3× that gap in every direction — a factor
  chosen from a convergence sweep during implementation, not tuned to any one
  net; see docs/cli/extract.md's "`klt mom` cross-check for one net" for the
  full derivation). Both the written SPICE `C` card and the
  `parasitics.nets[]` entry for that one net now carry the MoM value; every
  other net is untouched. On `tests/corpus/sky130/sky130_fd_sc_hd__inv_1.gds`
  net `Y` — the exact canary net `docs/design/extract-fidelity-roadmap.md`
  section 4 already cites as this repo's committed schematic-vs-extracted
  sensitivity-floor example, and a clean single-role (li1-only) case —
  the measured delta is **-27.66%** (lumped RC 0.23966 fF vs. MoM 0.17338 fF):
  MoM's ab-initio solve reports meaningfully less capacitance than the
  lumped model's area+fringe sum for this net's actual isolated geometry.
  **No `schema_version` bump** — the new `parasitics.mom_crosscheck` field is
  additive (`null` unless `--mom-net` was given), and every other documented
  field keeps its meaning; `--mom-net` omitted (the default) is
  byte-identical to before this feature existed. Requires the
  `klt_mom_native` extension to be built (docs/cli/mom.md); an unbuilt
  extension, an unresolvable net name, or a solver-level failure is a clean
  `ExtractError`. Only the deck's `metals` roles are modelled — a net whose
  ground capacitance also draws on a `poly`/`diffusion` role is honestly
  flagged as out of scope in `mom_crosscheck.warnings` rather than silently
  compared apples-to-oranges. See docs/cli/extract.md's "`klt mom` cross-check
  for one net" section.

- 2026-08-12 — `klt mom`'s matrix solve step is now **preconditioned
  Conjugate Gradient** (Jacobi/diagonal preconditioner) instead of a direct
  LU factorisation (#799, Phase 1c of the Method-of-Moments epic #701). The
  potential-coefficient matrix is symmetric positive definite by
  construction (`q^T P q` is twice the charge distribution's electrostatic
  energy), which is exactly the case CG — not the more general GMRES the
  epic also named — is the standard, provably-optimal Krylov method for. On
  an 8-conductor/1-shared-ground "finger" geometry discretised to 6 912
  panels (well past the MVP's 1-2 conductor fixtures), CG converges in a
  mean of 68.8 iterations (~1% of `n`) per right-hand side and the solve is
  **3.83x faster** than the direct factorisation it replaces (112.9 s vs.
  29.5 s), with the solved capacitance matrix agreeing with the direct
  solve to 1e-7 relative. Wiring this in also surfaced (and fixed) a latent
  fill bug: a multi-box conductor whose boxes abut at a right-angle corner
  (e.g. the coax fixtures' four-wall shield) could discretise two panels at
  the exact same location, giving the potential-coefficient matrix a
  literal `Inf` entry that the old direct solve happened not to trip over —
  `geometry::discretize` now deduplicates exact-coincident panels within a
  conductor. **No `schema_version` bump and no behavior change to `klt
  mom`'s JSON contract or reported numbers** — every existing closed-form
  and convergence-under-refinement check (`tests/test_mom_validation.py`,
  #757) passes unchanged against the new solve path. Full rationale, the
  preconditioner choice, and the measured convergence/scaling numbers:
  `docs/design/mom-iterative-solver.md`.

- 2026-08-12 — `klt synthesize --verify-equivalence` (#808, Phase 1 of the
  RTL-synthesis epic #704) wires the already-shipped `klt equiv` (#726) in
  as `klt synthesize`'s own **acceptance gate**: a synthesized netlist is
  not considered done until `klt equiv` reports it `"equivalent"` to the
  source RTL that was fed into Yosys. Off by default (additive/opt-in —
  every pre-existing invocation is unaffected). When given, the just-
  produced netlist is proven against the same `sources`/`hdl_toplevel` this
  run synthesized (reusing `klt equiv`'s own request contract — `gate`'s
  `liberty` is set to the same resolved liberty synthesis used); a
  `"counterexample"` or `"inconclusive"` (timeout) verdict is a hard
  `SynthesizeError`/exit `1`, never a silent warning folded into a
  `status: "ok"` response. Combinational designs only, matching `klt
  equiv`'s own Phase 0 MVP scope (#707) — a sequential design (e.g. the GCD
  worked example) makes the gate itself fail with a clear scope error. New
  response field `equivalence` (`null` unless the flag is given;
  `{status, engine, engine_version, timeout_s, elapsed_s, artifacts}` on a
  pass) — **no `schema_version` bump**, purely additive. New
  `--equiv-timeout-s` flag overrides `klt equiv`'s own default proof
  timeout. See `docs/cli/synthesize.md`'s "Equivalence gate" section.

- 2026-08-12 — `klt synthesize` now drives ABC's constraint-gated half of
  the mapping flow (#807, Epic #704 Phase 1a). Three additive changes, all
  unlocked by one flag: (1) `constraints.clock_period_ns` is **consumed**
  rather than carried — it becomes `abc -D <picoseconds>`, and the command
  always writes a `<top>_abc.constr` file (`set_driving_cell`/`set_load`,
  per-`cell_library` values sourced from ORFS's own
  `ABC_DRIVER_CELL`/`ABC_LOAD_IN_FF` and cross-checked against the installed
  liberty) passed as `abc -constr`, which is what turns on ABC's
  `buffer`/`upsize`/`dnsize` sizing and buffering steps at all; (2) non-logic
  cells are excluded via `abc -dont_use` — for `sky130_fd_sc_hd` the
  `lpflow_*` power-isolation and `probe*` test-probe classes ORFS's own
  `DONT_USE_CELLS` names, so a plain `gcd` netlist no longer contains 19
  power-domain isolation cells it has no use for; (3) the reserved `timing`
  field is now populated with ABC's own `stime -p` critical path, as a
  self-labelling object (`{"source": "abc_stime", "wire_load": null,
  "critical_path_ps": …, "delay_target_ps": …}`) — **a pre-layout,
  wire-free estimate, never signoff STA**, which remains Phase 4's
  OpenROAD/OpenSTA step. Measured on Yosys 0.68+48 against a volare
  `sky130A` install: `gcd` goes from 335 cells / 2951.5808 µm² / no delay
  number to 347 cells / 3238.1056 µm² / 2485.93 ps with zero excluded cells,
  and supplying `clock_period_ns: 10` maps it to 3001.6288 µm² at 3072.40 ps
  — the area/delay trade-off is now caller-controlled instead of pinned to
  one unlabelled point. `klt equiv` reports `"equivalent"` before and after
  on every combinational design measured. `cell_library` values in neither
  new table (and Yosys builds whose `abc` predates `-dont_use`, e.g. Ubuntu
  24.04's 0.33) keep the previous behaviour exactly rather than failing.

- 2026-08-11 — `klt extract --parasitics` now models **vertical-overlap
  (crossover) net-to-net coupling capacitance** (#760, Stage 2a of
  `docs/design/extract-fidelity-roadmap.md`). Where one net's conductor on
  metal level `i` sits directly under a *different* net's conductor on level
  `i+1`, that overlap area is charged between the two nets at the PDK's own
  `defaultoverlap` coefficient and **removed** from both nets' ground-area
  term — charge moves rather than duplicating. The geometry is a plain
  `Region &` on the per-net regions the extraction already has: no halo
  search, no new geometry structure, no new dependency. This is the first
  element `klt extract` has ever emitted that connects two signal nets, so
  it is the first extracted netlist that can show crosstalk in `klt sim` at
  all — a disturbance that was previously zero *by construction*, not by
  measurement. On the `gcd` corpus block it emits 5 638 net-to-net
  capacitors totalling 68.39 fF, taking 27.81 fF off the ground term
  (2 617.23 → 2 589.42 fF) — the same geometry the PDK's own coefficients
  price 2.46× higher between nets than to substrate, which is the
  misattribution this stage exists to correct. The same measurement
  decomposes that block's
  full 146.33 fF of crossover charge as 77.6 fF same-net (a net's own via
  stacks and its own li1-under-met1 routing, correctly left on ground),
  0.3 fF between distinct nets that share one layout label (coupling is
  aggregated per net *name*, so both terminals of such a pair would land on
  the same hub node — a self-loop; also left on ground), and 68.4 fF
  genuinely inter-net.
  **No `schema_version` bump** — new fields are additive
  (`parasitics.nets[].coupled[]`, `cc_count`,
  `total_coupling_capacitance_ff`, `overlap_pairs_without_coefficient[]`),
  `c_count` keeps its documented "one per `nets[]` entry, ground capacitors
  only" meaning, and no documented field is renamed or retyped. Two
  behavior changes land here rather than in a version, per this file's
  preamble: `parasitics.model.coupling` no longer reads `"not modelled"`
  (the field issue #728 added for exactly this moment — a consumer asserting
  on that literal string starts failing **by design**), and
  `nets[].capacitance_ff` / `total_capacitance_ff` values move as crossover
  charge relocates, with their definitions unchanged. `--parasitics` stays
  default-off and fixed-model; `devices[]`/`nets[]` and the
  `--parasitics`-off netlist are byte-identical to before. Coefficients are
  transcribed per adjacent level pair with per-value citations from each
  PDK's public magic tech file (sky130A's nominal `variants (),(orig),(si)`
  block, gf180mcuD's `variants ()` block, open_pdks
  `c6d73a35f524070e85faff4a6a9eef49553ebc2b`), and any declared adjacent
  pair without a curated coefficient is reported in
  `overlap_pairs_without_coefficient[]` plus `warnings[]` rather than
  silently contributing zero (#547's pattern). Lateral (same-layer,
  sidewall) coupling and fringe shielding remain unmodelled — Stage 2b/2c.
  See docs/cli/extract.md's "Vertical-overlap coupling capacitance".

- 2026-08-11 — `klt mom`'s numbers are now checked against analytic ground
  truth (#719, Phase 1 of the Method-of-Moments epic #701), closing the gap
  #718's entry below left open. `tests/test_mom_validation.py` asserts the
  solver against four closed forms — the ideal parallel plate
  (`C = εr ε0 A/d`, a strict lower bound for finite plates), Kirchhoff's
  1877 fringing-corrected disk asymptotic, the coaxial closed form with the
  exact conformal-mapping equivalent radii of a square cross-section, and
  full-enclosure `C00 = −C01` — and demonstrates convergence under mesh
  refinement two ways: Richardson extrapolation of the solver's own sequence
  (observed order 3.47 on the parallel plate) and error against the coax
  closed form directly (4.48% → 0.75% under one halving, observed order
  2.58). Convergence is **gated, not merely reported**: a sequence whose
  successive refinements stop shrinking fails the suite, demonstrated
  against the solver's own documented breakdown regime as well as synthetic
  sequences. No behavior change to `klt mom` itself — the solver was found
  accurate to 0.75% of the coax closed form and 1.8% of the fringing-
  corrected plate oracle at the stated operating points. Oracles, measured
  values, and the reasoning behind each tolerance:
  `docs/design/mom-validation.md`.
- 2026-08-11 — new `klt mom` verb (#718): Phase 0/1 of the Method-of-Moments
  epic (#701) — quasi-static **capacitance** extraction. Given a GDSII/OASIS
  layout plus a JSON spec file mapping GDS layers to conductors and z-extents,
  it discretises conductor surfaces into panels, fills the
  potential-coefficient matrix with a constant-panel point-collocation
  boundary-element method, and solves one right-hand side per conductor for
  the Maxwell capacitance matrix (femtofarads, `capacitance_matrix_ff`).
  Unlike `klt extract --parasitics`'s net-to-ground lumped model, this one
  **does** produce inter-conductor coupling terms.
  **This is the first Rust component in klayout-tools.** The numerics live in
  a pyo3/maturin extension crate at `native/mom/` (establishing the
  `native/<engine>/` convention for future Rust engines per
  `docs/ARCHITECTURE.md`'s "Rewrite rule"); the Python layer does the
  `klayout.db` geometry read and the JSON envelope. Because building it needs
  a Rust toolchain, it is an **optional** `mom` dependency group (`uv sync --group mom`)
  — every other `klt` verb still installs with no Rust in sight, and `klt mom`
  fails cleanly with a build pointer when the extension is absent, never an
  `ImportError` traceback. Numeric-accuracy validation against closed-form
  references is deliberately out of scope here and owned by #719; the solve
  does self-check the two properties any physical capacitance matrix has
  (positive diagonal, non-positive off-diagonal) and reports violations in a
  `warnings` array rather than silently returning a sign-flipped mutual
  capacitance. See `docs/cli/mom.md`.
- 2026-08-11 — `klt extract --parasitics` now declares its own model scope
  machine-readably (#728): a new `parasitics.model` object (`capacitance`,
  `coupling`, `resistance`, `frequency` — static text, unchanged across every
  extraction) states plainly that the model's capacitance is net-to-ground
  only, coupling between neighboring nets is not modelled, resistance is a
  single lumped series element distributed as a star per net (issue #592),
  and the model is quasi-static (no frequency dependence). The same text is
  also written as `*`-commented header lines at the top of the netlist
  whenever `--parasitics` was given, so the model's scope is visible from the
  raw SPICE alone, not only the JSON. Before this, the only way to learn the
  model omits inter-net coupling was to notice that every emitted `C` card's
  second terminal is the ground net — a silent omission indistinguishable
  from "no coupling found." Purely additive, no `schema_version` bump. See
  docs/cli/extract.md's "Parasitic model scope (`parasitics.model`)".
- 2026-08-11 — new `klt size` verb (#721): Phase 0 of the analog-sizing
  epic (#705) — solve a single device's channel width from a gm/Id target
  and a current budget, with `ngspice` on the real PDK models as the
  in-loop evaluator (never a closed-form surrogate as the final word).
  Uses a diode-connected bias sweep (an ideal current source fixes `Id`
  exactly; ngspice's own DC operating-point solver finds the resulting
  `Vgs`/`gm` for each candidate width), pays a real PDK's model-library
  parse cost once per invocation via `alterparam`/`reset` iteration rather
  than re-invoking `ngspice` per candidate, and always confirms the
  interpolated answer with a fresh, independent ngspice run before
  reporting it. Reuses `klt sim`'s model-library resolution and
  exit-code trichotomy (0/1/3/4). Every response — pass, fail, or error —
  carries a populated `method` field stating the sizing rationale and
  inversion-level derivation; a result with no stated method is never
  produced. Validated against this repo's own sky130 5T OTA canary
  (`examples/design-pipeline/`): given the gm/Id its committed AC
  simulation implies for the hand-sized input pair, `klt size` returns
  5.44 um against the hand-sized 8 um on the real sky130A models. See
  `docs/cli/size.md`.
- 2026-08-11 — `klt size` now accepts a PDK **corner set**, not just a
  single corner (#729): `request.corners` reuses `klt sim`'s own
  `corners.process`/`corners.temperature_c` axis semantics verbatim (bare
  `.lib` section names or `{name, sections}` bundles) rather than a third
  spelling, plus a single scalar `corners.vdd_v` (this command biases one
  fixed supply, never sweeps it). The width search still runs at exactly
  **one** sizing corner (`corners.sizing`, defaulting to the first declared
  point on each axis) — re-solving per corner would return a different
  width per corner, which is not a device. The solved width is then
  verified with a fresh single-point confirmation at every other declared
  corner, reporting each corner's own operating point and margins in the
  new `corners` response block. Declaring several corners does not by
  itself fail a good sizing — a non-sizing corner's `gm/Id` genuinely
  drifts with process/temperature, and that spread is reported, not
  aggregate-failing, unless the request opts in via
  `targets.hold_across_corners: true`. An evaluator error at *any* declared
  corner still forces the aggregate `status` to `"error"` (exit `4`),
  mirroring `klt sim`'s own error > fail > pass precedence. The pre-#729
  single-corner request/response shape (`request.corner`, top-level
  `corner`/`operating_point`/`margins`) is unchanged and still works — the
  `corners` block is purely additive, no `schema_version` bump. See
  `docs/cli/size.md`'s "Corner sets" section.
- 2026-08-11 — `klt size` now offers a worst-corner margin objective (#769,
  Phase 1 of the analog-sizing epic #705): `request.corners.objective:
  "worst_case_margin"` searches for the single width that maximizes the
  *worst* per-corner gm/Id margin across every declared corner, instead of
  #729's default (`"sizing_corner"`) which solves at one nominal corner and
  only reports the spread elsewhere. It sweeps the full width grid at
  *every* declared corner, then finds the width that minimizes the
  worst-case relative gm/Id error via a bisection on ln(W) against the
  already-swept per-corner curves (no extra ngspice invocation per
  bisection step), and finally confirms that width with a fresh
  single-point ngspice run at every declared corner (`2N` invocations for
  `N` corners, vs. `"sizing_corner"`'s `N+1`). The top-level
  `corner`/`operating_point`/`margins` fields mirror the worst-margin
  corner among those confirmations under this objective; the new
  `corners.worst_case` field names which declared corner that was. A
  single declared corner makes the two objectives mathematically
  equivalent, and the legacy `request.corner` shape has no `objective`
  field at all, so a single-corner request's result is unaffected by this
  feature existing. Purely additive (`corners.objective` defaults to
  `"sizing_corner"`), no `schema_version` bump. See `docs/cli/size.md`'s
  "Worst-corner margin objective" section.
- 2026-08-11 — `klt size` now solves a **coupled multi-device topology
  jointly**, not just one device at a time (#768, Phase 1a of epic #705): a
  request declares `topology: "diff_pair_mirror_tail"` (an NMOS
  differential input pair, a PMOS current-mirror load, and an NMOS tail
  current source with its diode-connected bias replica — six device
  instances, three solved widths, the bias structure of this repo's own
  hand-sized 5T OTA canary) plus a shared `budget` (`tail_current_a`,
  `tail_mirror_ratio`) instead of a single `device`/`target.id_a`. The
  solve runs *on the assembled circuit*: one diode-connected width sweep
  per role seeds the search and supplies its local slope, then each
  candidate `(W_tail, W_input, W_mirror)` is evaluated by a fresh ngspice
  DC operating-point run of the **whole** topology, reading back every
  instance's real in-circuit `gm`/`Id`/`Vgs` at its actual coupled `Vds`
  and correcting all three widths at once from that single coupled
  measurement (each role's own curve, shifted by its measured offset).
  That is what makes it one joint solve rather than three independent
  single-device solves glued together — `joint_solve.iterations[0]` is
  exactly the independent-solve answer, and the trajectory shows what the
  coupling correction bought. The response reports all six instances, each
  with its own gm/Id, inversion level, margins and rationale, the solved
  width per role, and `budget.measured` — the coupled quantities the
  circuit actually delivered (real tail current, the two legs' split, the
  realized mirror ratio). Against the real sky130A models it reproduces the
  5T OTA canary's hand-sized 10/8/6 um at 9.83/8.74/6.41 um from the
  canary's own measured gm/Id spec. Corner sets (`corners`) are not yet
  supported in topology mode. Purely a new request/response shape
  (`request.topology`, `request.devices`, `request.budget`, `request.bias`)
  alongside the unchanged single-device one — no `schema_version` bump. See
  `docs/cli/size.md`'s "Coupled multi-device topology sizing" section.
- 2026-08-11 — `klt size` now reports a PMOS's overdrive `Vov` with the
  correct polarity, so a PMOS is no longer misclassified as weakly inverted
  regardless of its actual bias (found while building #768, whose coupled
  topology has a PMOS mirror load and whose acceptance criteria require a
  per-device inversion-level rationale). The polarity is taken from the
  sign of the *reported* `Vth` rather than from `device.kind`, because the
  two model conventions this command meets disagree — a bare SPICE
  `level=1` PMOS reports negative `Vgs`/`Vth`, sky130's `__pfet_01v8`
  subcircuit reports positive magnitudes. Affects
  `operating_point.vov_v`/`operating_point.inversion_level` for PMOS
  devices only (NMOS results are bit-for-bit unchanged); no field added or
  removed, no `schema_version` bump. See `docs/cli/size.md`'s
  "Inversion-level classification" section.
- 2026-08-11 — `klt signoff --manifest <file>` (#722, Phase 0 of epic
  #706): a second, additive mode alongside the existing envelope-
  aggregation mode. Renders the full T1-T4 evidence-tier item skeleton,
  mechanically parsed from `docs/design-evidence-tiers.md` (never
  duplicated in code, so the doc and this command can never drift), graded
  against a block manifest's declared kind (`analog`/`digital`/
  `mixed-signal`) and per-item evidence citations. An item is `"met"` only
  when its evidence resolves to a passing, fresh `klt drc`/`lvs`/`extract`/
  `sim` JSON envelope (a stale input-hash pairing or missing check renders
  `"unmet"`, never assumed met); every `"met"` item's citation carries the
  evidence file, check status, input content hash, and exit status. `T2`-
  `T4` render as single always-`"unmet"` ladder-row items (this toolkit's
  closed loop targets T1 only). This phase is the item model, doc parser,
  and interface only — wiring the actual DRC/LVS/sim gates is a follow-on
  phase. `--manifest` is mutually exclusive with the existing positional
  `<file>...` envelope-aggregation arguments. See `docs/cli/signoff.md`'s
  "Tier-verdict report" section.

- 2026-08-11 — new `klt equiv` verb (#723): proves or refutes
  **combinational** equivalence between two RTL/gate-level netlists
  (`gold`/`gate`) — Phase 0 of the formal-equivalence epic #707, the
  correctness loop-closer #704 (RTL synthesis) and #700 (place-and-route)
  both depend on. Orchestrates Yosys's own built-in `miter -equiv` /
  `sat -prove-asserts` flow as a subprocess (built-in MiniSat, no
  `sby`/SymbiYosys dependency — see `docs/cli/equiv.md`'s "Engine" section
  for why, given this repo's CI installs `yosys` but not SymbiYosys). On a
  refutation, the concrete counterexample vector is independently re-run
  through both netlists via `iverilog`/`vvp` (`confirmed_by_simulation` in
  the response) rather than trusted from the solver alone. A design
  containing flip-flops, latches, or memories on either side is rejected up
  front with a clear scope error — this MVP is combinational-only. A
  solver/process timeout is always `status: "inconclusive"`, **never**
  `"equivalent"` (new exit code `4`, extending `klt sim`'s 0/1/2/3/4
  precedent rather than `klt lvs`'s 0/1/2/3). An optional `gate.liberty` /
  `gold.liberty` request field (read via `read_liberty -ignore_miss_func`,
  no `-lib`) lets a post-synthesis gate-level netlist — e.g. `klt
  synthesize`'s own `netlist_path` output — be compared directly against
  its source RTL. See `docs/cli/equiv.md`.

- 2026-08-11 — `klt gen guard_ring`: new additive `contacts_per_side_ns`/
  `contacts_per_side_ew` int params (#685), each defaulting to `0` (inherit
  `contacts_per_side`, so existing single-scalar callers are unaffected).
  `contacts_per_side_ns` sets the tap contact count on the N/S (top/bottom)
  sides, spaced along `inner_width_um`; `contacts_per_side_ew` sets it on the
  E/W (left/right) sides, spaced along `inner_height_um` — letting a caller
  sizing a ring around a strongly non-square inner region target an
  independent pitch on each axis instead of being capped by whichever side
  is shorter. See `docs/cli/gen.md`'s `guard_ring` section.

- 2026-08-11 — `klt extract`: new additive `dead_metal[]` field (#676)
  reporting every connected cluster of routing-stack geometry (the deck's
  `metals`/`vias` levels) that joins **no** extracted net — `{"role",
  "layer", "datatype", "bbox_um", "shapes", "area_um2"}` per cluster, sorted
  by `(layer, datatype, left, bottom)`. Deliberate dead metal (artwork, fill,
  a bond-pad blank) and accidental dead metal (a routing stub that never
  reached its via) were both invisible to every `klt` report before this:
  nothing in `nets[]` mentions the geometry, so the only way to find it was
  to render the raw stream and eyeball it. XY overlap between adjacent metal
  levels is **not** treated as connection — the netted side of the
  comparison comes from the extracted connectivity graph, which joins two
  levels only through a declared via layer. A labelled floating cluster
  (power strap, seal ring, bond pad) survives extraction as a real named net
  and is never reported. **Behavior change**: a non-empty `dead_metal[]`
  also appends one aggregate `warnings[]` entry (count baked in, #599's
  pattern), so a layout with untied dummy-device routing or leftover
  floating islands that previously extracted with an empty `warnings[]` now
  emits one. See `docs/cli/extract.md`'s "Dead metal" section.
- 2026-08-11 — new `klt components` verb (#674): reports which shapes form
  one electrically connected geometric component across a caller-supplied
  conductor/via stack — no PDK deck, no device recognition. Wraps the same
  `kdb.LayoutToNetlist` connect/via-join idiom `klt extract`'s internal
  extraction already uses (`l2n.connect(metal, via)` / `l2n.connect(via)` /
  `l2n.connect(via, next_metal)`) behind an ad-hoc `--conductors`/`--vias`
  mapping instead of a hardcoded `ExtractionDeck`, so two shapes that merely
  overlap in XY on different layers are never treated as connected — only a
  same-layer touch or an explicit, landed via joins two components. Never
  calls a device extractor or purges floating nets, so an isolated,
  unnamed, device-free island still appears as its own component. Optional
  `--label-layers` reports which GDS text touches each component; optional
  `--region` crops to a window and flags components that touch its edge.
  See `docs/cli/components.md`.
- 2026-08-10 — `klt layers`: new opt-in `--flattened`/`--include-text` flags
  (#675). Without `--flattened`, `layers[]` entries report only per-cell-
  **definition** shape counts, unchanged since before this flag existed.
  With `--flattened`, each entry additionally reports `flattened_shapes`
  (every physical placement of a shape on that layer, hierarchy- and
  transform-flattened via `Cell.begin_shapes_rec`), `bbox_um` (the union of
  every placement's transformed physical extents), and `contributors`
  (every contributing cell definition, instance-weighted, sorted by name).
  `--include-text` (requires `--flattened`) additionally reports
  `text_count` and `texts` (distinct text strings with flattened occurrence
  counts, sorted by string). All new fields are purely additive —
  `schema_version` stays `1`. See `docs/cli/layers.md`.
- 2026-08-09 — new `klt signoff` verb (#309): aggregates one or more `klt
  drc`/`klt lvs`/`klt extract`/`klt sim` `--format json` envelopes into a
  single pass/fail signoff verdict. Combines each input's own status
  (`drc`'s `clean`, `lvs`'s `match`, `sim`'s `pass`; `klt extract` has no
  independent pass/fail and always counts as passed) into an overall
  `status: "pass"|"fail"`, and separately checks every input's shared
  `provenance` block (#251) for consistency — mismatched `pdk.name`/
  `pdk.version`, a same-named deck's `content_hash`, or `input.content_hash`
  across the given envelopes **refuses** to produce a verdict at all
  (`status: "refused"`, exit code `4`) rather than silently combining a
  DRC pass against one layout revision with an LVS pass against another.
  The mechanical building block underneath the `design-signoff` skill's T1
  checklist walk (`docs/design-evidence-tiers.md`); does not yet diff
  against a block's declared spec (no machine-readable S3 schema exists —
  `docs/design/design-pipeline.md` §4) or grade the checklist's non-JSON
  items (design-source hygiene, testbenches shipped). See
  `docs/cli/signoff.md`.
- 2026-08-09 — `klt drc`: new opt-in `--engine klayout` (#565), a subprocess
  wrapper around the standalone `klayout` application binary that runs a
  PDK-native DRC-DSL script (`.lydrc`/`.drc`) instead of this repo's own
  curated `Region`-primitive decks — mirroring `klt lvs`'s `"netgen"`
  engine (#343): no `shutil.which` precheck (a missing binary raises a
  clean, actionable error), the subprocess's exit code is never trusted
  (only the report file's own presence/content is), and the report is
  parsed from KLayout's own `.lyrdb` (RDB XML) output into the same
  `violations[]`/`rule_counts` shape the curated engine produces. The deck
  script is resolved via a new PDK-asset helper,
  `klayout_tools.pdk.drc_deck_file()` (mirroring `netgen_setup_file()`), or
  given directly via `--deck-file`. By default `klt drc` still has no
  dependency on the standalone binary at all — this is opt-in, and does not
  change the curated engine's own JSON schema (the new `engine`/one-field
  is additive and only present on `--engine klayout` output). See
  `docs/cli/drc.md` → "Engine" → `"klayout"`.
- 2026-08-08 — new `klt deck resolve` verb (#623): resolves one of klt's
  built-in DRC/LVS rule decks (sky130, gf180mcu) by content hash
  (`--content-hash sha256:<hex>`, matching `provenance.deck.content_hash`'s
  shape) or by name + package version (`--deck <name> --version <X.Y.Z>`)
  against a generated hash/version -> release lookup table
  (`src/klayout_tools/decks/_history.json`), returning the klayout-tools git
  tag/commit and PyPI version that shipped that exact deck revision. Fixes
  the friction of needing to hand-bisect this repo's own git history to
  reproduce a pinned `content_hash` once a newer `klt` build shadows an
  older, pinned one on `PATH`. Resolve-only by design: it never fetches,
  checks out, or builds the historical revision in-process — the caller
  still installs the reported version themselves. The table is generated
  (never hand-maintained) by the new `scripts/generate_deck_history.py`,
  which walks this repo's own `v*` git tags and records one entry per
  `(deck, release)` pair; an unresolvable hash or a deck/version combo that
  never shipped is a clean error-envelope response, never a silent
  empty/null result. See `docs/cli/deck.md`.
- 2026-08-08 — `klt extract`: new `--abstract-cells <glob>` flag
  (repeatable, #620), a **cell-level black-box + pins** abstraction mode,
  additive to (and independent of) the existing region-based
  `black_box_regions` exclusion. Every instantiated cell whose name matches
  an `fnmatch` glob is extracted as an opaque, pinned subcircuit instead of
  being flattened to its own devices — everything not matched extracts
  exactly as today, in the same run. Pins are resolved once per distinct
  matched cell type: from that type's own `metal_labels`/`well_label`/
  `poly_label` text drawn directly in its own definition when present, else
  from a `MACRO`/`PIN`/`PORT` block of the same name in one of the new
  `--abstract-cell-lef <path>` files/directories (repeatable, first match
  wins), read via two new pure-text functions in the same `lef_header.py`
  module `klt lef-abstract` already relies on for tech-LEF header parsing
  (`parse_lef_macro_pin_ports`/`read_lef_macro_pin_ports` — a `PIN`'s `PORT`
  bounding box only, deliberately not the full mask-aware geometry engine).
  A matched cell type with neither pin source is a clean `ExtractError`, never
  a silently dropped pin or an unconnected instance. Per-instance
  mirror/rotation transforms are applied to the resolved pin footprint
  before probing, so differently-oriented instances of the same abstracted
  cell type wire up correctly. The written SPICE gains one `.SUBCKT <cell
  type> <pins...> ... .ENDS` block per distinct matched cell type (empty
  body) and one `X<instance>` card per matched instance, wired via the same
  layout-derived net names the un-abstracted portion already uses — a
  purely additive extension of the existing `kdb.NetlistSpiceWriter`
  machinery, not a new SPICE-emission code path. New `abstracted_cells[]`
  JSON response field reports, per distinct matched cell type: instance
  count, resolved pin count, and resolution source (`"in_cell_labels"` |
  `"lef_abstract"`, plus the specific LEF path for the latter) — mirroring
  `black_box_regions[]`/`ignored_layers[]`'s audit-coverage style. Always a
  list, empty (byte-identical to before this feature existed) unless
  `--abstract-cells` matched at least one instantiated cell. Scoped to a
  hierarchical **SPICE subcircuit** netlist only for this first delivery — a
  gate-level Verilog output is a deliberately deferred follow-up. See
  `docs/cli/extract.md`'s "Cell-level (black-box + pins) abstraction".
  Purely additive JSON shape change — no `schema_version` bump.
- 2026-08-06 — `klt extract`: new `--deck-option <key>=<value>` flag
  (repeatable, #595), the "other half" of #299's base-vs-high-rho poly
  resistor split. `Resistor`-marked poly on gf180mcu recognises one shared
  region three different ways in the official PDK LVS deck, selected by a
  build-time `POLY_RES` variable (`1k`/`2k`/`3k` -> 1000/2000/3000 Ω/□) that
  no drawn layer distinguishes; #299 wired only the PDK's own `'1k'` default,
  leaving a design actually drawn against the `2k`/`3k` interpretation with
  no way to select it — its resistor extracted at the wrong value (or, if the
  matching entry were narrowed away, an unmodelled short). `--deck-option
  poly_res=2k`/`=3k` now selects the caller's intended flavour explicitly; an
  unrecognised key or value is a clean exit-1 error, never a silently-kept
  default. New `ResistorDevice.flavour_option`/`flavours` fields
  (`decks/__init__.py`) declare a resistor family's selectable flavours;
  `get_extraction_deck(name, deck_options)` resolves them, raising the new
  `InvalidDeckOptionError` for an unrecognised entry. Default behavior
  (`--deck-option` omitted) is byte-for-byte unchanged. The resolved mapping
  is echoed as the new `provenance.deck.options` key (present only when
  non-empty) so a record can pin exactly which flavour a run selected — see
  `docs/cli/extract.md`'s "Selecting a shared-geometry resistor flavour".
  Under `--pdk`, the *selected* flavour binds its own real simulation
  subcircuit — `pdk_models.py`'s resistor table now carries `ppolyf_u_2k` and
  `ppolyf_u_3k` alongside `ppolyf_u_1k` (all three confirmed in
  `sm141064.ngspice`), so `--pdk` + `--deck-option poly_res=2k` emits `X …
  ppolyf_u_2k r_length=… r_width=…` rather than falling back to a bare `R`
  card. `klt lvs`'s equivalent request field (`layout.deck_options`) landed
  separately — see #600 below. Purely additive JSON shape change — no
  `schema_version` bump.
- 2026-08-07 — `klt lvs`: new `layout.deck_options` request field (#600),
  the JSON-request-document counterpart of `klt extract --deck-option`
  (#595) — `klt lvs` takes a request document rather than per-flag CLI args,
  so there was no way to select a shared-geometry resistor flavour (e.g.
  gf180mcu's `poly_res`) for the layout-side extraction: a design drawn
  against the `2k`/`3k` `POLY_RES` flavour had its layout netlist extracted
  at the deck's `1k` default regardless, producing a spurious resistance
  mismatch (or, worse, a silent pass under a loose
  `options.parameter_tolerance`) against a schematic reference sized for the
  actual flavour. `layout.deck_options: {"poly_res": "2k"}` resolves the
  same `get_extraction_deck(name, deck_options)` mapping `klt extract`
  already uses, forwarded to both `_resolve_layout`'s inline-extraction call
  and `run_lvs`'s own `get_extraction_deck` call (the latter also drives
  `device_classes` and the deferred resistor `fixed_offset_ohm` correction
  for the pre-extracted `layout.netlist` + `layout.deck` shape, #585). An
  unrecognised key/value raises a clean `LvsError` (wrapping
  `InvalidDeckOptionError`), never a traceback or a silently-kept default;
  giving `deck_options` without `layout.deck` is likewise a clean request
  error. The resolved mapping is echoed as the new `provenance.deck.options`
  key (present only when non-empty), matching `klt extract`'s shape exactly.
  Omitting `layout.deck_options` entirely is byte-for-byte unchanged from
  before this field existed. See `docs/cli/lvs.md`'s `layout.deck_options`
  field and `docs/cli/extract.md`'s "Selecting a shared-geometry resistor
  flavour" (its `klt lvs` caveat is now resolved). Purely additive JSON
  shape change — no `schema_version` bump.
- 2026-08-06 — **Breaking (per-command `schema_version` bump 1 -> 2):** `klt
  extract --parasitics`: each net's extracted resistance is now distributed
  as a **star topology** from the net (the star's hub) to each of its device
  terminals, instead of one shunt resistor into a dead-end internal node
  (#592). The pre-#592 Γ-section topology (`net --R--> net__par --C-->
  <substrate_net>`) left every device terminal on the original net, so the
  emitted resistor carried no DC current and never sat in series between two
  terminals on the same net — #338 documented this precisely and closed it
  as a doc-only fix, deferring the model change to a follow-up; #592 is that
  follow-up (scoped to Option 1, star-topology, per its curator enhancement
  — the full distributed per-segment RC ladder, Option 2, remains
  deliberately out of scope). Now, every device terminal that was on the net
  is moved onto its own fresh "leg" net with a series resistor back to the
  net (the net's pins/subcircuit connections stay directly on it, at zero
  resistance), so two terminals on the same net sit in series through two
  resistors — non-zero, in-path resistance that responds to real layout
  geometry, where before it was always exactly zero. Each leg's share of the
  net's total resistance is weighted by that terminal's approximate distance
  (via `Device.trans`, a coarse per-device rather than per-terminal
  location) from the net's terminal centroid; a single-terminal net
  degenerates to exactly the old Γ-section's one resistor. `parasitics.
  nets[].internal_node` is replaced by `hub_net` (usually the net itself now)
  and a new `terminals[]` array, and `parasitics.r_count` now counts every
  emitted resistor rather than always equalling `c_count`. Only `klt
  extract`'s own `schema_version` moved to `2`; every other command's is
  unaffected. See `docs/cli/extract.md`'s "Parasitic (RC) extraction"
  section and `docs/design/lvs-extraction-spike.md`'s new #592 addendum.
- 2026-08-06 — `klt lvs`: new `options.parameter_tolerance` (#589), an opt-in
  relative tolerance for numeric device parameters, expressed as a fraction
  (`0.001` is 0.1%). Extraction is geometrically exact against the curated
  deck's own device model while a schematic reference's values routinely come
  from a rounded design-level model, so the two differ by well under 0.1% —
  far inside any manufacturing tolerance, not a design error — yet the
  `"engine": "klayout"` path compared parameters against a fixed float-noise
  epsilon (`1e-6` relative) with no request-level knob, so a physically clean
  compare could never report `match`. Implemented as **snap-and-recompare**,
  not as a widened epsilon: `status` is always `NetlistComparer.compare()`'s
  own boolean and `compare()` decides parameter equality with its own,
  tighter, non-configurable tolerance *before* this command classifies
  anything, so a wider epsilon could only ever suppress a `device.property`
  entry, never move the verdict. Instead each in-tolerance reference-side value
  is snapped to its layout-side counterpart and a second, real `compare()` is
  run — covering both the clean device pairing and the minimal-cell degraded
  pairing (#282) that real extracted layouts hit whenever a body/well net is
  not shorted to a rail. New `severity: "warning"`,
  `category: "device.parameter_tolerated"` mismatch entries disclose every
  absorbed difference with **both original values**, the observed relative
  delta and the effective tolerance, and the new top-level
  `parameter_tolerance` field echoes it — a tolerance-assisted `"match"` is
  never indistinguishable from one where the numbers actually agreed.
  All-or-nothing per device pair (a pair with any out-of-tolerance parameter
  is left completely alone), never applied to structural findings, and
  **default unchanged**: omitting the option is byte-identical to before.
  `"engine": "klayout"` only — netgen's own per-property tolerances are
  absolute per-device-class values in its setup file, which a single relative
  tolerance has no faithful translation into, so the combination is an
  application error (exit 1) pointing at `options.netgen_setup` rather than a
  silent no-op. Purely additive JSON shape change — no `schema_version` bump.
  See `docs/cli/lvs.md`'s "`device.parameter_tolerated`" section.
- 2026-08-05 — `klt stats`/`klt layers`/`klt drc`/`klt precheck`/`klt render`/
  `klt socket-check`: new `--top <cell>` flag (#554), extending the
  cell-selector `klt extract`/`klt ring-check`/`klt lef-abstract` already had
  to the rest of the read-side verbs, so a multi-top-cell library stream
  (the normal shape of a vendor-supplied I/O or standard-cell library GDS)
  can be read one cell at a time instead of only as a whole-stream union.
  `klt stats --top <cell>` now succeeds on a multi-top-cell stream instead of
  hard-erroring (its documented "ambiguous bounding-box reference" error is
  unchanged when `--top` is omitted); the other five verbs default to
  today's whole-stream behaviour (`klt layers`' shape-count union, every top
  cell checked for `drc`/`precheck`/`socket-check`, every top cell rendered)
  when `--top` is omitted, and restrict to one cell's own hierarchy — itself
  plus every cell it calls, directly or indirectly, not just the whole
  stream's bounding box/top-cell label — when given. `--top <name>` naming a
  cell absent from the stream exits `1` with a clean error, matching
  `klt ring-check --top`'s existing message style. `klt lef-abstract`
  (already had `--top`) and `klt cells --top` (an unrelated, pre-existing
  boolean display filter) are unmodified. See each verb's own `docs/cli/*.md`
  for the field-by-field scoping.
- 2026-08-05 — `klt extract`: surface gf180mcu's anonymous PMOS body net as
  a structured JSON signal, not only discoverable by grepping the written
  SPICE body (#555). gf180mcu's curated deck has no distinct well-tie/tap
  layer separate from transistor active and no well-label layer, so a
  PMOS device's body terminal already extracted onto a floating,
  KLayout-synthesized `"$5"`-style net (a documented Coverage-section
  limitation) -- but that net has **no DC bias path at all**, unlike the
  NMOS body (tied to the deck's `vsubs` global via `connect_global`),
  which silently corrupts a direct resimulation of the extracted netlist:
  a PMOS body node that should sit at the real supply rail instead floats
  to whatever its source/drain-body junction diodes balance to. New
  top-level `unbiased_pmos_body_nets[]` array, one `{"device", "net"}`
  entry per affected PMOS device, plus a matching prose `warnings[]`
  entry; present (and populated when applicable) regardless of
  `--parasitics`/`--pdk`. No device-physics change -- the anonymous net's
  connectivity is unchanged, this is a reporting fix. An opt-in flag to
  actually re-bias the net at extraction time (e.g.
  `--tie-well-to=<net>`) is a documented, deliberately deferred follow-up,
  not implemented here. Purely additive: the array is always present,
  empty for any layout/deck (e.g. sky130) whose PMOS body resolves to a
  real, named net -- no `schema_version` bump (`extract` stays `1`). See
  `docs/cli/extract.md`'s "Parasitic (RC) extraction" section, "Known gap:
  gf180mcu's anonymous PMOS body net has no DC bias path".
- 2026-08-05 — `klt drc`/`klt extract`: fail-loudly coverage for unmodelled
  voltage-domain marker geometry (#552). A gf180mcu layout can carry a
  `Dualgate` (55/0) region that promotes the enclosed devices to the PDK's
  5V/6V domain — changing both the DRC width/spacing thresholds
  (`DF.1a`/`DF.3a`/`DF.6`/`PL.5a`/`PL.5b`) and the MOS model binding
  (`nfet_03v3`/`pfet_03v3`) — yet neither curated deck modelled the marker,
  so `klt drc` could report `clean` and `klt extract` could bind the
  low-voltage model on geometry the marker actually governs, both silently.
  Rather than change any threshold or model binding (issue #552's Option 3
  scoping — Options 1/2 are explicitly out of scope), both commands now
  surface an additive diagnostic when a registered marker's geometry
  interacts with checked/extracted geometry: `klt drc` gains
  `coverage.voltage_domain_warnings[]` (each
  `{"marker": "<layer>/<datatype>", "description": str}`, sorted by marker,
  gated on the marker overlapping a layer in `layers_checked` — not bare
  stream presence), and `klt extract` gains a top-level
  `voltage_domain_warnings[]` of the same shape (gated on overlap with
  extracted MOS geometry, `deck.active`), mirrored into `warnings[]`. New
  `UNMODELED_VOLTAGE_MARKERS` deck registry with a
  `get_unmodeled_voltage_markers()` accessor; gf180mcu registers `Dualgate`,
  sky130 registers an explicit empty map (no named `hvi`-equivalent layer
  yet — the gap is visible in the diff, not omitted). Purely additive: both
  arrays are always present and empty for a deck that registers no marker or
  a layout that draws none overlapping, so no `schema_version` bump (`drc`
  and `extract` both stay `1`). See `docs/cli/drc.md`'s
  "`coverage.voltage_domain_warnings`" section and `docs/cli/extract.md`'s
  "Voltage-domain markers" section.
- 2026-08-05 — `klt draw`: new optional `array` field on a shape entry (#553),
  a `{"pitch_um": [dx, dy], "count": [nx, ny]}` repetition primitive so a
  regular via/contact farm no longer needs one JSON shape entry per instance —
  one real fixture needed 17134 shape entries / 1.4 MB of request JSON for
  geometry parameterizable as origin+pitch+count. `count` is the number of
  instances (not gaps); stepping is computed as `unit_geometry + i * pitch` in
  **integer database units after both the unit shape and the pitch are
  snapped to `dbu_um`**, so an array lands exactly on pitch by construction
  rather than by the caller's float luck. Purely additive: no `array` key (or
  `count: [1, 1]`) is byte-identical to today's output, and `shape_count` /
  `layers[].shapes` in the response now count every expanded instance so they
  stay meaningful for reviewing what was actually written — no
  `schema_version` bump (`draw` stays `1`). See `docs/cli/draw.md`'s new
  "array" subsection.
- 2026-08-05 — `klt gen`: new `bond_pad` generator (#568), the first
  generator in this family covering the chip *boundary* rather than a core
  analog device — a passivation opening enclosed by the resolved PDK
  family's own topmost routing metal (a new `top_metal` layer role: sky130's
  `met5.drawing` `(72, 20)`, gf180mcu's `Metal5` `(81, 0)`), overlapping it
  by `enclosure_um` on every side. `enclosure_um` defaults to `2.0` and is
  hard-floored there, transcribed from gf180mcu's only DRC-coded bond-pad
  rule (DRM 9.1 "PAD.4", `decks/gf180mcu.py`'s `pad.enclosing.metal5.1`);
  `bond_type` (`"wedge"`/`"ball_cup"`/`"bump"`) selects which of that same
  DRM's 9.2 "PAD.1" *guideline* minimum opening sizes `opening_um` is
  checked against (40/40/4 µm), flagged via `drc_hints.notes` rather than
  rejected when smaller. Reports one `PAD` port on `top_metal`, sized to
  `opening_um`. gf180mcu output always assumes the 5LM metal stack
  (`pdk.variant` never distinguishes 5LM from 6LM) and `down_to` supports
  only its default `"top_metal"` (no via-stack-down-to-a-lower-level yet) —
  both documented, tested limitations, not silent gaps. See
  `docs/cli/gen.md`'s new "`bond_pad`" section.
- 2026-08-05 — `klt extract`: junction-diode device recognition (#542). Neither
  curated deck recognised a diode as a device class, so a discrete PN/ESD-clamp
  diode — the standard pad-ring clamp primitive, and what gf180mcu's own
  `gf180mcu_fd_io__asig_5p0` pad cell is built from — extracted as **no device
  at all**, leaving `klt lvs` unable to verify any diode-based clamp. New
  optional `ExtractionDeck.diodes` field (a tuple of the new `DiodeDevice`
  dataclass: anode/cathode layer roles, a device-mark `marker` layer, and
  per-terminal `requires`/`excludes` narrowing), wired through KLayout's native
  `DeviceExtractorDiode`. A terminal the PDK draws no mask for — the
  p-substrate side of an n+/p-substrate diode — is declared `None` and tied to
  the deck's `substrate_net` global, mirroring the collector-less bipolar
  collector. First deck wiring: gf180mcu's `diode_nd2ps_06v0` and
  `diode_pd2nw_06v0`, transcribed from that PDK's own official KLayout LVS
  deck. Recognised diodes appear in `devices[]` with `"a"`/`"c"` terminal keys
  and `area_um2`/`perimeter_um` params, are appended to `device_classes`, and
  are written as SPICE `D` cards whose model token is the deck entry's name
  (schematic-equivalent, no I-V model — the same fidelity the MOS/BJT
  recognisers provide). Purely additive: `diodes` defaults to `()`, so sky130
  and any deck that declares none extracts bit-for-bit as before — no
  `schema_version` bump (`extract` stays `1`). See `docs/cli/extract.md`'s new
  "Junction diodes" section.
- 2026-08-05 — `klt pdk macros`: new sibling command to `klt pdk cells`
  (#535) that enumerates hard-macro IP libraries — `libs_ref` entries named
  `*_fd_ip_*` (e.g. an SRAM/ROM compiler output) — which `klt pdk cells`
  deliberately excludes (its own scope is `*_fd_sc_*` standard-cell digital
  libraries only). Previously there was no CLI-surfaced way to discover
  these even when fully present on disk; `klt pdk cells` silently skipped
  them with no indication anything was omitted. Reports each library's name
  and which views it ships (`gds`/`lef`/`lib`/`spice`/`cdl`/`verilog`, a
  presence check under the library directory, not content parsing). New
  `list_hard_macro_libraries()` in the `klayout_tools.pdk` library API.
  `klt pdk cells`'s own JSON schema and behavior are unchanged. See
  `docs/cli/pdk.md`'s new "`klt pdk macros`" section.
- 2026-08-04 — `klt pdk find`/`list`/`env`: resolve IHP-Open-PDK's SG13G2
  install (#522), the first non-open_pdks-shaped layout this resolver
  handles. `_probe_root` now falls back to treating an install root as its
  *own* single, flat variant (named after the root's basename) whenever the
  existing nested open_pdks-style scan finds nothing — covering both
  real-world `$PDK_ROOT` conventions for a single-PDK repo like
  IHP-Open-PDK (pointed at the clone root, already resolved as an ordinary
  nested variant named `ihp-sg13g2`; or pointed directly at the
  `ihp-sg13g2/` directory itself, the new flat case). No public JSON schema
  change — `find`/`list`/`env`'s payload shapes are unchanged, and the
  `PdkNotFoundError` message wording was only reworded to stop naming
  "open_pdks-layout" as the sole supported shape. New
  `scripts/fetch-ihp-sg13g2.sh` fetches a pinned, checksum-verified
  IHP-Open-PDK release (Apache-2.0) into `pdks/ihp-open-pdk/`, mirroring
  `scripts/fetch-pdks.sh`'s pattern; `pdks/README.md` documents the explicit
  `ihp130` (lambdapdk)-vs-SG13G2 (IHP-Open-PDK) distinction. `klt drc`/`klt
  lvs`'s default engine still need a curated SG13G2 rule/extraction deck
  that does not exist yet (tracked separately as #524); `klt lvs`'s `"netgen"`
  engine, which needs no curated deck, resolves and can run against a real
  SG13G2 install's own netgen setup script today. See `docs/cli/pdk.md`'s
  new "PDK layouts: what resolves and what doesn't" table.
- 2026-08-04 — `klt lvs`: new `request.reference.device_bulk` field and
  `device.bulk_reconciled` mismatch category (#506, `"engine": "klayout"`
  only), which *reconcile* the device-class arity gap `device.class_arity`
  (#504/#505) could only diagnose. `{"<model>": "<reference net>"}` declares
  that the reference netlist's device class of that name carries an implicit
  bulk/well/collector terminal on the named net — the terminal its layout-side
  namesake declares explicitly (e.g. the `W` of a `bulk_to_substrate` resistor
  flavour's three-terminal `RES_X`). `klt lvs` adds that one terminal to the
  reference class and ties it to the named net on every reference-side
  instance before `NetlistComparer.compare()` runs, so a circuit that
  legitimately mixes a bulk-terminal device flavour on the layout side with a
  schematic reference that does not model that terminal can now report
  `status: "match"` at all (previously a permanent `device.class_arity`
  mismatch). The net is resolved per instantiating circuit and created there
  when the reference does not model that node; it composes with
  `hints.same_nets` for the deck-synthesized-substrate case. Every reconciled
  class emits a `severity: "warning"`, `side: "reference"`
  `device.bulk_reconciled` entry naming the terminal, the net, whether the net
  was created, and both terminal lists — so a match reached through the hook
  is never silently indistinguishable from a fully independent one, the same
  disclosure discipline `device.body_unverified` applies to an unverified MOS
  body. A class the request does not name still reports `device.class_arity`;
  an unresolvable model name, a reference class that is not actually missing a
  terminal, a class two or more terminals apart, and use with
  `"engine": "netgen"` are each an application error (exit 1). See
  `docs/cli/lvs.md`'s `reference.device_bulk` and `device.bulk_reconciled`
  entries.
- 2026-08-04 — `klt lvs`: new `device.class_arity` mismatch category (#504,
  `"engine": "klayout"` only), diagnosing a layout-side and reference-side
  device class that share a name but declare a different terminal list —
  e.g. a deck's `bulk_to_substrate` resistor flavour extracted via
  `DeviceExtractorResistorWithBulk` (a three-terminal `A`/`B`/`W`
  `DeviceClassResistorWithBulk`) compared against a schematic-derived
  reference's plain two-node `R` card (a two-terminal `DeviceClassResistor`
  of the same model name). Previously silent: `NetlistComparer` cannot pair
  any instance of such a class at all, and since the class names agree the
  event fits neither `device.class` (a matched-but-differently-classed pair)
  nor the `topology` device-class-mismatch case (a class registered on only
  one side) — it degraded into an unattributable `device.unmatched`/
  `net.unmatched` cascade naming neither class's terminal list. The new
  `severity: "error"`, `side: "both"` entry names both classes' terminal
  lists in `details` (`{"layout_terminals": [...], "reference_terminals":
  [...]}`); diagnostic only — it does not itself make the two sides match,
  so `status` still reports `"mismatch"`. See `docs/cli/lvs.md`'s
  `device.class_arity` entry.
- 2026-08-04 — `klt gen`: `mos_array` and `diff_pair` accept `gate_contact`
  (#492), which finishes the gate stack the #461 poly landing pad only made
  legal — a contact plus a local-metal pad drawn on the pad, and the `_G`
  port reported on the `metal` role, symmetric with `_S`/`_D`. Enabling it
  raises the pad's contact region `0.4um` clear of the diffusion edge (a
  contact-enclosure metal square centred on the bare pad shares an edge with
  the S/D local-metal pads and merges into one polygon, shorting the gate to
  source/drain), so the unit device — and `diff_pair`'s automatically-sized
  guard ring — grows taller and `_G`'s `y_um` moves. Defaults to `false`,
  reproducing the bare-poly gate byte-for-byte. See `docs/cli/gen.md`.
- 2026-08-04 — `klt gen-compose`: a `connectivity[]` net whose pin sits on
  the extraction deck's bare `poly` layer (a gate drawn without
  `params.gate_contact`) is now reported in `unrouted_nets[]` with a
  `drc_hints.notes[]` reason naming the port's layer and the fix (#492).
  Previously such a net was drawn anyway — `"routed": true`, no note — as a
  metal stub sitting *over* the gate with no contact joining the two, an
  open net only a later `klt drc`/`klt extract`/`klt lvs` run would surface.
  Behavior for every other port layer is unchanged. See
  `docs/cli/gen-compose.md`.
- 2026-08-04 — `klt gen`: `diff_pair` accepts `ring_padding_um` and
  `row_spacing_um` (#484), the ring-to-core padding and inter-row device
  spacing its automatically-sized guard ring previously hardwired to
  `0.5um`/`0.4um`. Both default to those values, so omitting them reproduces
  prior geometry byte-for-byte; a caller that needs room to bring both
  matched devices' gate nets out of the block (previously nowhere to land a
  second gate contact stack beside the row/ring boundary) can widen either
  param and pay the extra area. Validated `>= 0`; `col_pitch_um` (the
  within-row gap between interleaved splits) is unchanged. See
  `docs/cli/gen.md`.

### Changed since release

- 2026-08-13 — `klt draw` now has a **written** unknown-key policy for its
  request JSON, and enforces it (issue #950): an unrecognised key is an
  application error (exit 1) naming the offending key and listing the allowed
  set, **except** a key beginning with `_`, which is reserved for caller
  annotations and is accepted and ignored. Enforced at every level of the
  request — the top-level object, `params`, `options`, each `shapes[]` entry,
  each `labels[]` entry, and a shape's `array`. Previously every unrecognised
  key at every level was silently dropped, but only incidentally: no code
  anywhere diffed the request's key set against an allow-list, so the tolerance
  was undeclared behavior that `docs/cli/draw.md` never promised, and a caller
  carrying `_purpose`/`_rule` sidecar keys to document a known-bad DRC fixture
  was relying on an accident. Rejecting is what makes a typo in a *real* key
  visible (`rect_nm` for `rect_um`, `counts` for `array.count`) instead of
  yielding a successfully-written stream missing the geometry that was asked
  for; the `_` prefix keeps the motivating self-documenting-fixture use case
  working, and is guaranteed never to be given meaning by a future version.
  Same posture as `klt gen-compose`'s `request.pdk`
  (`gen_compose._ALLOWED_PDK_KEYS`). **Potentially breaking** for a request
  that carried a non-`_`-prefixed extra key: rename it to `_<key>`. No
  response-shape change; `schema_version` is unaffected, and the existing
  `shape[N] must have exactly one geometry key` path is unchanged for a shape
  whose keys are all recognised. This policy is scoped to `klt draw` — the
  other request-JSON verbs are unchanged and still document their own. See
  `docs/cli/draw.md` -> "Unrecognised keys".

## 0.2.0 (2026-08-04)

The first release since `0.1.0`. `klt` grew from **5 verbs to 24**; the 19
net-new verbs below all existed on `main` but had never shipped in a
numbered release, so anyone installing from PyPI has until now been getting
a tool bearing little resemblance to the documented one.

Additive at the CLI surface — no existing verb was renamed or removed. One
per-command breaking change is included (`klt precheck`'s own
`schema_version` moved `1` -> `2`, detailed below); per
[`docs/json-contract.md`](docs/json-contract.md) that is scoped to that
command and does not affect any other verb.

### Added — new verbs

- `klt precheck` — pre-flight layout checks before an expensive run
- `klt socket-check` — socket/pin-level connectivity checks
- `klt ring-check` — guard/collector ring integrity checks
- `klt layout-metrics` — quantitative layout measurements
- `klt render` — raster/vector rendering of layout views
- `klt extract` — parasitic/device extraction with provenance hashing
- `klt lvs` — layout-versus-schematic comparison
- `klt gen` — parametric analog cell generation
- `klt gen-compose` — composition and routing of generated blocks
- `klt draw` — primitive layout drawing
- `klt sim` — circuit simulation, including the remote AWS backend
- `klt report` — aggregated run reporting
- `klt kb` — knowledge-base query/update
- `klt trajectory` — append-only optimization-run logs (`--plot`)
- `klt synthesize` — RTL synthesis (Yosys engine class)
- `klt place-and-route` — physical implementation (OpenROAD engine class)
- `klt functional-verification` — cocotb testbenches via Icarus/Verilator
- `klt eval` — single `valid`/`objective`/`metrics` verdict over gate checks
- `klt lef-abstract` — LEF abstract generation

Each is documented in [`docs/cli/`](docs/cli/).

### Changed — behavior detail

The user-visible, additive behavior changes worth calling out explicitly,
because they affect a verb's output. Not an exhaustive commit-by-commit log.

- 2026-08-04 — `klt sim`: wall-clock budget, orphan safety, and resume for
  corner sweeps (#482). `options.wall_clock_budget_s`/`--budget-s` bounds
  the overall sweep (distinct from the existing per-corner
  `options.timeout_s`); an always-on parent-PID liveness check stops the
  local/local-parallel dispatch loop the instant the launching process
  exits; and `options.resume`/`--resume` checkpoints completed corners to
  `checkpoint.json` and skips them on a later matching invocation. Skipped
  corners are reported as `status: "error"` with
  `budget_exceeded`/`orphaned` diagnostics, and `environment.budget`/
  `environment.orphaned`/`environment.resume` summarise the outcome —
  purely additive.
- 2026-08-04 — `klt extract`: comma-joined multi-label net collision
  detection (#481, closes #470). Any net whose KLayout-assigned name is a
  comma-joined merge of 2+ distinct labels (e.g. `Y,Y2`) — a silent signal
  that two differently-named nets were shorted together in the layout, most
  commonly a `gen-compose` `pins[]` entry naming a port other connectivity
  already reaches — is now flagged in a new top-level
  `merged_net_labels[]` array (`{"net", "labels"}` per affected net) plus a
  matching `warnings[]` entry. Purely additive:
  `net_count`/`nets[]`/`pin_count` are unchanged.
- 2026-08-04 — `klt gen-compose`: self-net short detection now reads drawn
  pad geometry, not just port adjacency (#472, closes #469).
  `route_two_pin()` gains a fourth self-net check that intersects the
  route's own drawn metal against the block's actual drawn shapes on the
  route layer, generalising #467's same-row/same-direction heuristic
  (still kept) to same-facing pin pairs on different rows and wider routes
  that reach an adjacent row's pad — cases missed because a port's
  reported `width_um` systematically under-states the pad metal drawn
  around it.
- 2026-08-04 — `klt gen`: `res_array` can draw sky130's higher-sheet-rho
  precision-resistor flavours (#475, closes #463). A validated `flavor`
  request param ("generic"/"high"/"xhigh", mirroring `mos_array`'s enum
  pattern) resolves a per-family implant/block layer pair sourced from the
  extraction deck's own `requires` layers (sky130 `res_high_po` = psdm +
  rpm; `res_xhigh_po` = psdm + urpm), so the generated array extracts as
  the requested device class instead of always `res_generic_po`. Default
  "generic" reproduces prior geometry exactly on both families; an
  unsupported flavour raises a clear `GenError`.
- 2026-08-04 — `klt gen`: MOS generators draw a poly gate landing pad past
  the diffusion so gate contacts land legally (#474). `mos_array` and
  `diff_pair` previously drew gate poly sharing the diffusion's exact
  extent, leaving no DRC-legal spot for a gate contact. The shared unit
  layout now adds a landing pad extending past the diffusion's gate-side
  edge on the first finger and reports the gate port at the pad centre —
  a JSON-contract-visible change: the gate port's `y_um` moves into the
  extension and its `width_um` becomes the pad width, and
  `bbox_height_um` now spans diffusion plus pad while `height_um` stays
  the bare diffusion height. A contact placed at the reported gate port is
  now DRC-clean on the curated sky130 deck.
- 2026-08-04 — `klt extract`: `dummy` marker-layer suppression (#295)
  extended from MOS gates to resistors and bipolars (#471, closes #462).
  The `dummy` region is now subtracted from each candidate resistor body
  before device recognition and from `bipolar_base` before
  emitter/collector are derived, with fully-covered components counted
  into the shared `dummy_devices_dropped` — so matched resistor/bipolar
  arrays built with dummy edge units no longer inflate `device.unmatched`
  under `klt lvs`, and a fully dummy-suppressed poly resistor no longer
  trips a false `marked_unrecognised` warning.
- 2026-08-04 — `klt lvs`: new `device.combine_incomplete` mismatch category
  (#466). Only possible with `options.combine_devices: true`. KLayout's own
  `klayout.db.Netlist.combine_devices()` can raise an unhandled
  internal-consistency `RuntimeError` on a *partial-match* device group — N
  real (matching-relevant) instances plus M dummy instances that all share
  two of three terminals (e.g. a bipolar device's base and collector, tied
  to a matched array's common well and substrate), but only the N real
  instances additionally share the third (e.g. an emitter bussed to one
  signal net). `klt lvs` now catches that one error shape per netlist
  (narrowly — only a `RuntimeError` carrying KLayout's own `"...in
  Netlist.combine_devices"` marker text; any other `RuntimeError` still
  propagates as an application error) instead of letting it abort the whole
  run: whatever `combine_devices()` already merged stays merged, that
  netlist's remaining devices are left as individual devices, and a
  `severity: "warning"` entry (never changes `status`, never breaks
  `mismatch_count`'s error semantics) records that combine did not fully
  apply on that side. Purely additive (no `schema_version` bump), but adds a
  `category_counts["device.combine_incomplete"]` entry for any
  `combine_devices` run that trips the KLayout error — see
  `docs/cli/lvs.md`'s `device.combine_incomplete` subsection for the full
  trigger conditions.
- 2026-08-04 — `klt gen` + `klt gen-compose`: ring routing openings (#434).
  `guard_ring`, `diff_pair` (`add_guard_ring`) and `bjt_array`
  (`add_collector_ring`) accept `ring_gap_side` (`""`/`"N"`/`"S"`/`"E"`/
  `"W"`), `ring_gap_um` and `ring_gap_offset_um`, cutting **one** opening
  through the ring's band — on every layer the ring is drawn on, dropping
  any contact it would clip — so the ring stays a single connected C-shaped
  conductor rather than splitting into two arcs. The opening is reported as
  a new `GAP_<side>` entry in `ports[]` (`width_um` = the opening's length
  along that side, `direction_deg` = the side's outward normal), and
  `klt gen-compose` uses it to admit a route to a ringed block's non-tap
  port — but only when the drawn backbone actually passes through the
  opening with half the route width plus the block's own
  `drc_hints.min_spacing_um` of clearance from either cut end; a crossing on
  any other side, a crossing that misses the opening, and a segment laid
  along a ring side are all still reported in `unrouted_nets[]`. With no
  opening declared, the #199 closed-ring rejection is unchanged (its message
  now also names the new remedy). Wiring or labelling a `GAP_*` port is an
  application error (exit `1`). Before this, a matched analog group could
  keep its default guard/collector ring **or** be wired into the rest of a
  composed circuit, not both. See `docs/cli/gen.md` ("Ring routing
  openings") and `docs/cli/gen-compose.md`.
- 2026-08-03 — **Breaking (per-command `schema_version` bump 1 -> 2):** `klt
  precheck`: `layer_whitelist` violations' `shapes` count is now weighted by
  placement multiplicity across the full cell hierarchy, not summed once per
  cell *definition* (issue #452). Previously, `layout.each_cell()` counted
  each cell definition's own (non-recursive) shapes exactly once regardless
  of how many times that cell was actually placed, under-reporting true
  placed-shape prevalence by roughly two orders of magnitude on real
  hierarchical, macro-scale input — e.g. a layer drawn 800 times across 320
  placed `sky130_fd_sc_hd` instances reported `"shapes": 10`. The fix
  multiplies each cell definition's own-shape count by its total placement
  multiplicity across all instantiation paths (multiplicities compose
  multiplicatively for nested placement — a cell placed inside a cell that
  is itself placed multiple times gets the product, not the sum), via a
  top-down hierarchy walk (`Layout.each_cell_top_down()`/`Instance.size()`)
  rather than a full recursive shape flatten. Only `klt precheck`'s own
  `schema_version` moved to `2`; every other command's `schema_version` is
  unaffected, and a per-command bump never forces one on the package version
  — per-command versioning per `docs/json-contract.md`. See `docs/cli/precheck.md`.
- 2026-08-03 — `klt eval`: `synthesize` and `place-and-route` are now
  first-class gate `check`s (#437, Phase 5 of Epic #391), joining
  `drc`/`lvs`/`sim`/`layout-metrics`/`functional-verification`. Both always
  report `"status": "ok"` (see `docs/cli/synthesize.md`/
  `docs/cli/place-and-route.md` — a run either produces its output or
  raises), so — like `layout-metrics` — a gate naming either one must
  declare an explicit `threshold` to derive pass/fail; `request` resolves
  as a path only (never inline JSON/`-`), matching each verb's own
  `load_request`. This is what lets a digital candidate's descriptor chain
  `synthesize` -> `functional-verification` -> `place-and-route` ->
  `drc`/`layout-metrics` (over the P&R-produced GDS) into the same single
  `valid`/`objective`/`metrics` verdict an analog descriptor already
  produces, and record it as one `klt trajectory` log entry via the same
  `gates`/`objective` shape. No schema change to `klt eval`'s own response
  envelope. See `docs/cli/eval.md`.
- 2026-08-03 — `klt trajectory` + `klt eval`: a scored evaluation can now be
  recorded to a trajectory log without hand-rolling the record (#437, Phase 5
  of Epic #391 — the trajectory-log half of that issue, completing the gate
  wiring above). New `klayout_tools.trajectory.record_from_eval`/
  `append_record` build one trajectory record straight from a `klt eval`
  envelope's own `objective`/`gates` (collapsing `gates[]` to the record
  schema's lighter `gate_results`) and append it to an append-only JSONL log,
  creating the file and any missing parent directories on first write. Both
  are check-name-agnostic — they read only `objective`/`gates`, so an analog
  envelope (`drc`/`lvs`/`sim`/`layout-metrics`) and a digital one
  (`synthesize`/`functional-verification`/`place-and-route`) log identically.
  `klt eval` gains `--trajectory-log`/`--turn`/`--candidate-ref`/
  `--description`/`--wall-clock-s`, which call that pair as a side effect on
  top of the unchanged response envelope — so one invocation can score a
  candidate *and* log the turn, matching `klt trajectory --plot`'s existing
  "write a file as a courtesy, independent of `--format`" precedent.
  `--trajectory-log` without both `--turn` and `--candidate-ref`, and a
  log-append failure, are both application errors (exit `1`). See
  `docs/cli/eval.md`'s "Trajectory logging" section and
  `docs/cli/trajectory.md`'s "Building a record from `klt eval`" section.
- 2026-08-03 — `klt functional-verification`: new verb (#422, Phase 3 of
  Epic #391). Runs a cocotb testbench against RTL sources through Icarus
  Verilog (default) or Verilator, reporting `status`
  (`"pass"`/`"fail"`), `test_count`/`passed_count`/`failed_count`/
  `skipped_count`, a per-test `tests[]` array (with `error_type`/
  `error_message` on failures), optional Verilator `coverage`
  (`line_pct`/`toggle_pct`/`branch_pct`/`expr_pct` plus an lcov `info_path`),
  and an `environment` reproducibility block. Invoked exclusively through
  cocotb 2.0's first-party Python `Runner` API — never a generated
  Makefile — and the verdict is always derived from the run's own
  `results.xml`, never from a simulator's exit code (which the Phase 1
  survey observed varying between `0`, `1`, and `2` for the *same* failing
  regression). Exit codes reuse `klt lvs`'s `0`/`1`/`2`/`3` trichotomy, so
  `status: "pass"` → `valid: true` and `status: "fail"` → `valid: false` at
  the `klt eval` boundary; `functional-verification` is also now a
  first-class `klt eval` gate `check`. cocotb is an optional runtime
  dependency (not pinned in `pyproject.toml` — cocotb 2.0 caps at Python
  3.13 while `klt` supports 3.10+). See
  `docs/cli/functional-verification.md` and
  `docs/design/cocotb-verification-spike.md` section 7 for the full
  contract.
- 2026-08-03 — `klt functional-verification`: `options.random_seed` (issue
  #423). Pinned to `Runner.test()`'s own `seed` parameter
  (`COCOTB_RANDOM_SEED`) when given; the effective seed cocotb actually used
  (pinned or its own generated value) is always echoed back in
  `environment.random_seed`, read from `results.xml`'s own
  `<property name="random_seed">` — the same reproducibility bar `klt sim`'s
  Monte Carlo seeding and `klt lvs`'s `environment` hashes already set. CI
  now also provisions pinned, checksum-verified Icarus Verilog/Verilator
  builds and the pinned cocotb extra (`scripts/install-icarus-verilog.sh`,
  `scripts/install-verilator.sh`, `pyproject.toml`'s
  `functional-verification` extra), so the GCD worked example
  (`docs/design/cocotb-verification-spike.md` section 6) runs for real in CI
  against both `engine: "icarus"` and `engine: "verilator"` rather than only
  locally.
- 2026-08-03 — `klt place-and-route`: new verb (#425, Phase 4 of Epic #391).
  Places and routes a gate-level netlist (`klt synthesize`'s own
  `netlist_path` output) against a resolved sky130 standard-cell LEF/liberty
  deck via OpenROAD's native Tcl API, one subprocess per stage
  (`floorplan` -> `place` -> `cts` -> `route`), chained via `write_db`/
  `read_db` ODB checkpoints, with `-metrics <file>.json` as the structured
  per-stage metrics channel (confirmed end-to-end against a real
  `openroad/orfs` run for this issue's own worked example). Supports all
  three non-padframe floorplan methods (`utilization`/`explicit`/`def`);
  `target_stage` makes a partial run (e.g. `"place"`) a normal, successful
  outcome with `def_path`/`gds_path` both `null` by design. `def_path` is
  populated once `write_def` has run; `gds_path` only once the DEF is
  merged with the resolved standard-cell GDS view via KLayout's `pya`, in
  -process (never a `klayout` subprocess) — ported from ORFS's own
  `def2stream.py`. Adds a LEF resolver (`klayout_tools.pdk.lef_files()`)
  alongside the existing liberty resolution. `seed` is a required request
  field, echoed unchanged in the response. See `docs/cli/place-and-route.md`
  and `docs/design/digital-flow-contracts-spike.md` section 5 for the full
  contract.
- 2026-08-03 — `klt synthesize`: new verb (#416, Phase 2 of Epic #391).
  Synthesizes RTL sources against a resolved sky130 standard-cell liberty
  via Yosys + bundled ABC (`read_verilog` -> `hierarchy` -> `synth` ->
  `dfflibmap` -> `abc -liberty` -> `clean` -> `stat`/`write_verilog`,
  generated into a debuggable `.ys` script kept alongside the mapped
  netlist), reporting `instance_count`/`area_um2`/`sequential_area_um2`/
  `instance_counts_by_type` parsed from Yosys's own `stat -liberty ...
  -json` output. `pdk.cell_library`/`corner` resolve to a liberty file via
  the same `find_pdk()`/`libs_ref` discovery `klt pdk`/`klt cells` already
  use — no new PDK-fetch mechanism. `timing` is always `null` in this
  contract, deferred to a future OpenROAD/OpenSTA place-and-route phase.
  See `docs/cli/synthesize.md` and
  `docs/design/digital-flow-contracts-spike.md` section 4 for the full
  contract.
- 2026-08-03 — `klt trajectory`: new verb (#388). Renders an append-only
  JSONL optimization-trajectory log (one record per evaluation: `turn`,
  `candidate_ref`, `objective` `{name, value, polarity}`, optional
  `gate_results`/`wall_clock_s`) into a markdown milestone table plus a
  self-contained objective-vs-turn SVG plot for a block repo's README.
  Milestones are the turns where the objective improves on the best-prior
  record by more than a configurable `--threshold`. Operates purely on the
  JSONL file — no live optimizer required — so a hand-written or
  human-curated log renders identically. The record schema mirrors the
  planned `klt eval` envelope's `objective`/`gate_results` shape (#387). See
  `docs/cli/trajectory.md`.
- 2026-08-02 — `klt lvs`: new `device.body_unverified` mismatch category
  (`a483ed0`, #281/#285). Warns (`severity: "warning"`, never changes
  `status`) when a MOS body terminal was extracted onto a deck-synthesized
  net rather than a real drawn tap/well-label net — an NMOS entry fires on
  every inline-extraction LVS run with one or more NMOS devices (no curated
  deck draws a distinct NMOS substrate/tap layer), and a PMOS entry
  additionally fires for decks with no distinct well-tap layer (gf180mcu
  today). This is purely additive (no `schema_version` bump) but changes
  `category_counts` for any gf180mcu (and, for the NMOS case, sky130)
  inline-extraction fixture that previously reported an empty
  `category_counts: {}` — see `docs/cli/lvs.md`'s `device.body_unverified`
  subsection for the full trigger conditions.
- 2026-08-02 — `klt lvs`: new top-level `net_correspondence[]` response
  field (#311). Lists every layout↔reference net pairing the comparer
  matched — unambiguous and ambiguously-resolved alike — as `{layout,
  reference, pin}` entries, sorted and deduplicated per circuit scope so
  `len(net_correspondence) == counts.nets.matched` holds even across a
  hierarchy with cross-circuit net-name collisions. Purely additive (no
  `schema_version` bump) — see `docs/cli/lvs.md`'s `net_correspondence[]
  entries` subsection.
- 2026-08-02 — `klt extract`: the "unmodelled device geometry" diagnostic
  (#288/#299) no longer flags a recognised drawn resistor's own terminal
  head (#324) — a poly component abutting a body region `_resolve_resistors`
  already recognised is now excluded outright, the same way a real MOS gate
  already was, removing a false positive that previously fired on any
  resistor whose wide terminal head carries an ordinary (2+) contact array.
  New top-level `unmodelled_poly[]` response field lists the bounding
  box + `reason` (`"unmarked"` / `"marked_unrecognised"`) of every shape the
  diagnostic still flags, alongside the existing prose `warnings[]` strings.
  Purely additive (no `schema_version` bump) — see `docs/cli/extract.md`'s
  "Known limitation: unmodelled device geometry" subsection. Ordinary poly
  routing tracks sharing the same resistor-body signature remain a known,
  documented false-positive class with a client-side filtering workaround
  via `unmodelled_poly[]`.
- 2026-08-02 — `klt sim`: new optional request block `monte_carlo` (#348,
  phase 1 of #344's decomposition) — re-runs each expanded corner point
  `n` times with a reproducible seed (`monte_carlo.seed`), standing in for
  the per-instance device variation a mismatch-aware model library's
  behavioral parameters draw on. Adds `environment.monte_carlo` (echoes
  `n`/`seed`/`vary`) and a per-sample `corners[].monte_carlo` field
  (`{sample_index, seed, process_seed, mismatch_seed}`, `null` for a
  non-sampled corner); a sample's `corner_id`/artifact path gets a
  `/mc<sample_index>` suffix so per-sample logs never collide. Ships the
  deterministic negative control two public canary repos already rely on
  in their own hand-rolled orchestration: the seed component for whichever
  axis `monte_carlo.vary` does *not* request stays identical across every
  sample of a corner (sigma=0), proving the sampler isn't silently
  injecting or dropping variation. Purely additive (no `schema_version`
  bump), reuses the existing `local`/`local-parallel`/`remote` backends
  unchanged — see `docs/cli/sim.md`'s "Monte Carlo sampling" section.
  Statistics rollup and limit-window evaluation across a sample set landed
  separately as phase 2 (#349, below).
- 2026-08-02 — `klt lvs`: new accepted `request.engine` value `"netgen"`
  (#343) — a second, independent comparator behind the same request/response
  contract, wrapping the open-flow standard
  [`netgen`](https://github.com/RTimothyEdwards/netgen) as a subprocess
  (`netgen -batch lvs`). **Netlist-vs-netlist only** — no `magic` extraction
  backend; it compares the same layout/reference SPICE netlists the
  `"klayout"` engine already resolves, so it validates comparator/contract
  independence, not extraction independence (see `docs/cli/lvs.md` →
  "Engine"). Adds a new `mismatches[].details` field (object | `null`) for
  engine-specific data that does not map onto
  `category`/`net`/`device`/`property` — present and `null`-valued on every
  `"klayout"`-engine entry too, so no entry shape changed — plus two
  netgen-only request options (`options.netgen_setup`,
  `options.netgen_timeout_s`). `environment.engine_version` is netgen's own
  banner-reported version for this engine. Known, documented gap:
  `counts.*.matched` is exact on a `"match"` verdict and `0` on a
  `"mismatch"` verdict, and `net_correspondence` is always `[]`, for
  `"engine": "netgen"` only. Purely additive (no `schema_version` bump); the
  default engine is still `"klayout"` and its output is unchanged apart from
  the new `null` `details` key. Findings (netgen invocation quirks, report-
  format stability) are written up in
  `docs/design/lvs-extraction-spike.md`'s 2026-08-02 addendum.
- 2026-08-02 — `klt sim`: Monte Carlo statistics rollup (#349, phase 2 of
  #344's decomposition). A measurement that ran under `monte_carlo` now
  carries an additive `measurements[].monte_carlo` block —
  `{n, errored, mean, stddev, min, max, quantiles, sigma_window,
  by_corner}` — so callers no longer reduce the raw per-sample corner list
  themselves. `stddev` is the sample (n-1) standard deviation and is `null`
  for `n < 2` rather than a fabricated `0.0`; `quantiles` defaults to
  `[5, 50, 95]` and is configurable via the new `monte_carlo.quantiles`
  request field. The new `monte_carlo.k_sigma` (overridable per measurement
  with `measurements[].k_sigma`) opts into a `mean ± k*stddev`
  limit-window check; both endpoints are scored through the existing
  `_evaluate_limits`, so min/max handling and the margin sign convention
  are shared with the path a single deterministic value takes —
  **a failing window makes the run `fail` (exit `3`) even when every
  individual sample passed its limits**. Without a declared `k_sigma`,
  pass/fail behavior is unchanged; without `monte_carlo`, the response
  shape is unchanged (no `schema_version` bump) — see `docs/cli/sim.md`'s
  "Monte Carlo statistics" section.
- 2026-08-02 — `klt sim`: new `environment.monte_carlo.family_mismatch[]`
  response field (`85faf9d`, #365) — per-device-family mismatch-section
  availability for the selected model library, so a Monte Carlo consumer
  can detect which families actually sampled variation. Purely additive
  (no `schema_version` bump), but changes `environment` contents for every
  gf180mcu Monte Carlo run — see `docs/cli/sim.md`'s
  `environment.monte_carlo` subsection.
- 2026-08-03 — `klt lef-abstract`: new verb (#438, Epic #393 Phase 2
  Capability A). Emits a LEF abstract (`MACRO` block with `PIN`/`OBS`
  sections) from a block's GDSII/OASIS layout plus its `klt socket-check`
  descriptor, so OpenROAD can place it as a hard macro. Pin geometry is
  real drawn metal when present, else a synthesized placeholder box
  (reported per pin as `geometry_source`); obstruction geometry is every
  routing-layer shape not already claimed by a declared pin. Backed by
  `klayout_tools.lef_header`, a new dependency-free tech-LEF header reader
  (`SITE`, routing-layer `PITCH`/`OFFSET`/`DIRECTION`, macro `PIN`
  `DIRECTION`/`USE`) — the deferred-trigger resolution
  `docs/design/sc-leflib-evaluation.md` called out. `docs/schemas/socket
  .schema.json` gains two new optional, additive `pins[]` fields
  (`direction`/`use`, LEF's own vocabulary) this command consumes when
  present. See `docs/cli/lef-abstract.md` and
  `docs/cli/socket-check.md`'s new "LEF translation" section.
- 2026-08-03 — `klt place-and-route`: new `request.macros` field (#438),
  closing the "macro placement" gap the verb's own v1 docstring originally
  scoped out. Each entry fixes one hard-macro instance (e.g. a `klt
  lef-abstract` LEF) at a caller-given location during the floorplan stage
  via OpenROAD's own `place_macro -location ... -orientation ... -exact`
  (never the automatic macro placer). The DEF→GDS merge tolerates an
  abstract-only macro instance (no declared `gds`) staying empty instead of
  raising; a declared `gds` merges the macro's own view in, same as the
  standard-cell GDS merge. Purely additive (`[]`/omitted unchanged; no
  `schema_version` bump) — see `docs/cli/place-and-route.md`'s new
  "Hard-macro placement" section.
- 2026-08-04 — Epic #393 Phase 3 (cross-domain signoff, #456): `klt drc`,
  `klt lvs`, and `klt extract` verified against a real mixed
  sky130_fd_sc_hd + analog-macro layout (a `klt gen diff_pair` block placed
  via `klt place-and-route`'s `request.macros` alongside real standard
  cells, real Yosys + real OpenROAD). No code changes were needed in any of
  the three verbs — each already spans both domains by construction (`klt
  drc`/`klt extract`'s whole-layout flattening has no region concept to
  scope by; `klt lvs`'s comparator has no macro-boundary special case).
  Findings and the injected-violation/corrupted-reference verification
  methodology are recorded in `docs/cli/drc.md`, `docs/cli/lvs.md`, and
  `docs/cli/extract.md`'s new "Mixed sky130_fd_sc_hd + analog-macro
  layout"/"...netlist" sections. One real domain-boundary gap was found and
  filed separately (not fixed here, per this phase's own decomposition
  precedent, #451/#452): #464, a `klt lef-abstract` macro pin with no
  `PORT` geometry that, if wired into a real net, fails `klt
  place-and-route` opaquely (OpenROAD `GRT-0029`) rather than with a clear
  `klt`-level error. This is a documentation/verification-only change — no
  `schema_version` bump for any of the three verbs.
- 2026-08-04 — Fixed #464 (per the #451/#452 precedent of fixing rather
  than only documenting a verb-boundary gap). `klt lef-abstract` gains a
  new top-level `unroutable_pins` response field (`[{name, layer:
  [gds_layer, gds_datatype]}, ...]`) that promotes the existing per-pin
  `geometry_source: "none"`/`warnings[]` signal into a structured,
  programmatically-checkable echo — purely additive, `[]` when every pin
  resolved a routing-type LEF layer, no `schema_version` bump. `klt
  place-and-route`'s `_validate_macros` now cross-checks each macro's LEF
  `PIN` blocks (via a new `has_port` field on `klayout_tools.lef_header`'s
  parsed `pins[]`) against the netlist's own named port connections for
  that instance, and rejects the request (exit 1, before OpenROAD is ever
  invoked) when a `PORT`-less pin is actually wired to a real net — turning
  the opaque `GRT-0029` global-routing failure into a specific, actionable
  `klt`-level error. A `PORT`-less pin the netlist leaves unconnected is
  not rejected; the netlist scan is a best-effort structural-Verilog lookup
  (paren-depth-matched, comment-stripped) that is skipped — never a
  spurious reject — when the macro's own instance can't be confidently
  located in the netlist text. See `docs/cli/lef-abstract.md`'s "Pins" and
  `docs/cli/place-and-route.md`'s "Hard-macro placement" sections.

## 0.1.0 (2026-07-31)

Initial release — the agent-native IC layout toolkit, first cut.

### Added

- `klt` CLI with five headless, JSON-contracted verbs:
  - `klt layers` — layer/datatype enumeration for GDSII/OASIS streams
  - `klt stats` — bounding box, drawn area, density, polygon/vertex counts (`--per-layer`)
  - `klt cells` — cell hierarchy: top cells, shape/instance counts, bboxes (`--top`)
  - `klt drc` — headless DRC via KLayout's native Region check primitives, with
    curated width/space/enclosure decks for sky130 and gf180mcu (`--deck`)
  - `klt pdk find|list|env` — discovery/resolution of open_pdks-layout PDK installs
- Shared JSON output envelope (`schema_version`, error shape, exit codes) across
  all verbs — `docs/json-contract.md` is the API
- `scripts/fetch-pdks.sh` — pinned fetch of lambdapdk open PDK data
- `kb/` knowledge-base scaffold with JSON Schema and seed entries
- sky130/gf180mcu test corpus with golden fixtures; CI (ruff + pytest, Python 3.10–3.13)
- Docs: architecture, JSON contract, per-verb CLI references, macOS KLayout
  source-build guide; site at klayout-tools.org
