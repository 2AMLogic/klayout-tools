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
