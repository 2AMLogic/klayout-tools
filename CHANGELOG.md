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

### Fixed since release

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
  pin map, a wrapper subcircuit) remains deliberately out of scope. Note that
  detection is only as good as the engine's own refusal to elaborate the
  deck: ngspice 46 rejects any pin-count difference, but ngspice 42 (what
  `apt` ships, and what CI installs) silently accepts every "too many
  parameters" case *and* a one-pin "too few" shortfall, simulating on with
  dangling terminals — so on ngspice < 46 such a run still reports `status:
  "pass"` with `pin_count_mismatch: null`. See `docs/cli/pex.md`, "Detection
  depends on your ngspice version". No response-shape break;
  `schema_version` unaffected.

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
