# Changelog

`klt` has not yet reached `1.0`; per [`docs/json-contract.md`](docs/json-contract.md),
`schema_version` only bumps for non-additive (breaking) shape changes to a
command's own payload, and is versioned **per command** — so one command's
breaking change never forces a bump on another, nor on the package version.
Additive behavior changes — including new `mismatches[].category` values
`klt lvs` can emit — land within a package version and are recorded here
instead. This file is the source of truth for which categories exist as of a
given date; pin `provenance.deck` (sha256) and `provenance.klayout_version`,
not `klt --version`, if you need to detect this kind of drift.

## Unreleased

### Fixed since release

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
