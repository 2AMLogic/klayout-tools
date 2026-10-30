# `klt pex`

Extract a lumped-RC **parasitic-annotated** netlist from a routed layout,
re-run one or more schematic testbenches against it per corner, and report a
**per-corner, per-spec-row schematic-vs-extracted delta** — a measured,
explainable degradation on every spec row, not a bare "post-layout OK".
Phase 1a of [Epic #709](https://github.com/2AMLogic/klayout-tools/issues/709)
("PEX-aware post-layout sim flow for klt").

```
klt pex <layout> <testbench>... --deck sky130|gf180mcu|sg13g2 [-o|--output <netlist.spice>] [--top <cell>] [--pdk <variant>] [--pdk-root <root>] [--outdir <dir>] [--backend <backend>] [--max-units-per-job <n>] [--max-concurrent-jobs <n>] [--deck-option <key>=<value>]... [--pins <a,b,...>] [--rename-declared-pins] [--tie-declared-pins] [--label-layer <role>=<layer>/<datatype>]... [--device-model <class>=<subckt>]... [--tie-net <extracted-net>=<schematic-net>]... [--critical-net <net>]... [--critical-net-pattern <glob>]... [--critical-net-all] [--critical-net-exclude <glob>]... [--distributed-rc] [--resistance-graph [--resistance-graph-r-cut <ohms>]] [--parasitic-variant <variant>]... [--substrate-tie-ohms <ohms>] [--mom-rlc-net <net>] [--mom-rlc-resistance-ohm <r>] [--mom-rlc-capacitance-ff <c>] [--mom-rlc-inductance-nh <l>] [--flatten-schematic] [--device-only-leg] [--extract-only] [--format text|json]
klt pex <layout> --deck <deck> --measure-command <command> --reference-netlist <schematic.spice> [--measure-timeout-s <seconds>] [<every flag above>]
```

- `<layout>` — path to a GDSII (`.gds`) or OASIS (`.oas`) routed layout
  stream. Same auto-detection as `klt extract`/`klt drc`.
- `<testbench>...` — one or more `klt sim` request JSON files (see
  [`sim.md`](sim.md)), each already converged against the schematic-level
  design (e.g. an S5 sizing testbench). Re-run **completely unmodified** for
  the schematic-side leg of each delta row; re-pointed at the extracted
  netlist (see "The DUT `.include` swap" below) for the extracted-side leg.
  Every testbench body must carry **exactly one** `.include`/`.inc` directive
  (none and more than one are both hard errors), and every testbench must
  include the *same* schematic DUT file — `klt pex` reports one
  `reference_netlist` for the whole run. **Required unless
  `--measure-command` (or `--extract-only`) is given**; the three are
  mutually exclusive.
- `--measure-command` — measure both legs with a caller-supplied command
  instead of with `klt sim` testbenches (issue #2478). For a spec row no
  single `.meas` card can express. Requires `--reference-netlist`; mutually
  exclusive with `<testbench>`. See "Measuring with a caller-supplied
  command" below for the full contract.
- `--reference-netlist` — the schematic DUT netlist the schematic-side
  measurement is taken on, and the `reference_netlist` this run reports.
  **Required with `--measure-command`, and rejected without it**: in
  testbench mode the schematic DUT is whatever every testbench `.include`s,
  which is an observation rather than a caller's claim.
- `--measure-timeout-s` — wall-clock cap on **one** `--measure-command`
  invocation (there are two per run, one per side). Default `1800`, the same
  cap [`signoff.md`](signoff.md) puts on a command-backed evidence entry.
  Requires `--measure-command`.
- `--deck` — required, passed through to `klt extract --parasitics`.
- `--output` / `-o` — path to write the extracted SPICE netlist. Same default
  as `klt extract`: `<layout>` with its extension replaced by `.spice`, next
  to the input.
- `--top` / `--pdk` / `--pdk-root` — passed through to `klt extract`; see
  [`extract.md`](extract.md).
- `--outdir` — overrides where this command writes its own generated
  artifacts: the extracted-side testbench body/request copies (see below),
  and each `klt sim` call's own `options.keep_artifacts` output, namespaced
  per testbench and per side as `<outdir>/<NN-testbench-stem>/schematic/` /
  `.../extracted/`. Defaults to a `.klt/pex/` directory next to `<layout>`
  (the same "next to the input" convention `klt render`/`klt sim` already
  use).
- `--backend` — execution backend passed through to every `klt sim` call
  (schematic and extracted side, every testbench). See
  [`sim.md`](sim.md#execution-backends). Defaults to `local`, or each
  testbench's own `backend` field.
- `--max-units-per-job`, `--max-concurrent-jobs` — backend `batch` sharding,
  same semantics as the `klt sim` flags of the same name
  ([`sim.md`](sim.md)), applied to each `klt sim` call separately (the
  schematic side and the extracted side of every testbench are two jobs, each
  of which must fit the fleet's whole-job ceiling on its own). Omitted, each
  testbench request's own `batch` block applies, unchanged
  ([#4616](https://github.com/2AMLogic/klayout-tools/issues/4616)).
- `--deck-option` — repeatable `<key>=<value>`, passed through verbatim to
  `klt extract --deck-option` (issue #1558); see
  [`extract.md`](extract.md)'s "Selecting a shared-geometry resistor
  flavour" / "Selecting a shared-geometry MiM capacitor density flavour"
  sections. Selects a caller-visible flavour of a deck's
  shared-geometry device family — today gf180mcu's `poly_res` (`1k`
  (default) / `2k` / `3k`) and `mim_cap` (`cap_mim_1f0_m4m5_noshield` /
  `cap_mim_1f5_m4m5_noshield` / `cap_mim_2f0_m4m5_noshield` (default)),
  where one drawn geometry has several PDK-offered interpretations the
  layout alone cannot distinguish. **Without it, a design that commits to a
  non-default flavour is silently extracted against the deck's default
  one** — no warning, and every `delta[]` row is then computed from the
  wrong parasitics. An unrecognised key or value is a clean error (exit 1),
  not a silently-kept default. Off by default — byte-identical to before
  this flag existed. The fully resolved mapping — including the options the
  deck applied by default, flagged as such in
  `provenance.deck.options_explicit` (issue #2394) — is recorded in
  `provenance.deck.options`, so a committed record pins exactly which
  flavour a run resolved, not only which one it was told to.
- `--pins` — comma-separated declared pin set (e.g. `'A,B,VDD,VSS'`),
  passed through to `klt extract --pins` (issue #1558); see
  [`extract.md`](extract.md). Every
  named net not in this set keeps its name but is demoted to an internal
  node instead of being promoted to a top-level pin. This is the flag that
  resolves a `pin_count_mismatch` (below) caused by flat extraction
  promoting more top-level pins — a body/substrate tap, say — than the
  schematic DUT declares. Off by default — every named net still promotes
  to a pin, byte-identical to before this flag existed.
- `--rename-declared-pins` — passed through to `klt extract
  --rename-declared-pins` (issue #2854); see
  [`extract.md`](extract.md)'s "Renaming promoted pins to their declared
  name" section. Renames each surviving promoted pin net whose component
  labels contain exactly one `--pins` name to that name, so the extracted
  DUT's `.SUBCKT` header carries the canonical interface names the
  schematic DUT declares — required whenever a boundary net carries extra
  drawn labels and its declared name is only one component of KLayout's
  joined net name. Requires `--pins`. Off by default — byte-identical to
  before this flag existed. The effective value is echoed as
  `extraction.rename_declared_pins`.
- `--tie-declared-pins` — passed through to `klt extract
  --tie-declared-pins` (issue #4383); see [`extract.md`](extract.md). Declared
  pins that extraction joins into one net (a ground and a body pin, say) are
  tied back to every declared name, so the extracted `.SUBCKT` keeps the
  schematic's pin count instead of every delta row failing with
  `pin_count_mismatch`. Requires `--pins`. Off by default — byte-identical.
  Echoed as `extraction.tie_declared_pins`. `extraction.pdk` records the PDK
  install the extraction read (`variant`, `root` as `{path, scope}`,
  `version`; `null` without `--pdk`), so a run with two installs of one
  variant shows which was used.
- `--flatten-schematic` — flatten the schematic DUT to PDK leaf devices
  before the schematic-side run (issue #2889) — the `klt pex` counterpart of
  `klt lvs`'s `options.flatten_reference`. Off by default — byte-identical
  to before this flag existed. See "Flattening a hierarchical schematic DUT
  (`--flatten-schematic`)" below.
- `--device-only-leg` — measure a parasitic-free third leg (issue #3298) and
  split each `delta[]` row into a device-representation part and a parasitic
  part. Off by default — byte-identical to before this flag existed. With
  `--measure-command` the command is run a third time on the parasitic-free
  netlist (`$KLT_PEX_SIDE=device-only`, issue #4293). See "Separating
  device representation from parasitics (`--device-only-leg`)" below.
- `--extract-only` — extract and report, with no measurement leg (issue
  #3739). No testbench, `--measure-command` or placeholder measurement is
  needed, so nothing fake lands in `delta[]`. The response carries
  `extraction`, `body_bias` and `provenance` exactly as a graded run does,
  plus `testbenches: []`, `delta: []`, `measurement.mode: "extract-only"`
  (`command`/`timeout_s`/`sides` null) and `status: "not_checked"`
  (`coverage.nothing_checked_reasons: ["no_delta_rows"]`, exit 4 — see
  "`coverage`" below), so it is distinguishable from a graded run and `klt
  signoff` cannot cite it as comparison evidence. `reference_netlist` is
  `{"path": null, "scope": "absent"}` and `reference_netlist_sha256`,
  `pin_count_mismatch`, `flat_dut_mismatch` and `model_mismatch` are `null`:
  there is no schematic side. Combining it with a testbench,
  `--measure-command`, `--reference-netlist`, `--measure-timeout-s`,
  `--device-only-leg` or `--flatten-schematic` is exit 1 (error envelope).
  Off by default — without it, naming no testbench still fails with "at
  least one testbench request is required".
- `--tie-net` — repeatable `<extracted-net>=<schematic-net>` (issue #4245).
  Merges one reported-unbiased well/body net of the extracted netlist into a
  schematic net before the extracted leg is simulated, so the leg is
  physically meaningful without a hand-rolled `--measure-command`. See
  "Tying an unbiased body net (`--tie-net`)" below.
- `--label-layer` — repeatable `<role>=<layer>/<datatype>`, passed through
  to `klt extract --label-layer` (issue #2686); see
  [`extract.md`](extract.md)'s "Overriding a deck's label layers" section.
  Re-points one label role (`well`, `poly`, `metal<i>` — 0-based,
  index-aligned with the deck's metal stack) at the GDS purpose this layout
  actually wrote its net/pin names on, *replacing* the deck's own pair for
  that role; `<role>=none` reads no label layer for that role at all, and a
  later entry for the same role wins. **Without it, a layout whose names sit
  on a text purpose the deck does not read extracts with no promoted
  top-level pins**, so the extracted-side DUT cannot bind to the
  testbench's `X` line. For example, an IHP stream labelled on `Metal1.text`
  (8/25) under `sg13cmos5l` (whose default is `Metal1.pin`, 8/2):

  ```bash
  klt pex routed.gds tb.json --deck sg13cmos5l \
    --label-layer metal0=8/25 --label-layer metal1=10/25
  ```

  A malformed entry (no `=`, a blank role, a value that is neither `none`
  nor `LAYER/DATATYPE`) or a role the deck has no slot for is a clean error
  (exit 1). Only name text moves — connectivity and device recognition are
  untouched. Off by default — byte-identical to before this flag existed.
  The applied mapping is echoed as `extraction.label_layers` (`null` when
  the flag was never given; a cleared role is `null` inside the object).
- `--device-model` — repeatable `<class>=<subckt>`, passed through to `klt
  extract --device-model` (issue #4234); see [`extract.md`](extract.md).
  Under `--pdk`, writes a bipolar class with no curated PDK binding (gf180mcu's
  `bjt`) as an `X` call to the named subcircuit, so the extracted netlist
  loads in ngspice instead of failing with `could not find a valid
  modelname`. An unknown or already-curated class is a clean error (exit 1).
  Off by default — byte-identical to before this flag existed. Echoed as
  `extraction.device_models` (`null` when not given).
- `--critical-net` — repeatable, passed through to `klt extract
  --critical-net` (issue #976, Epic #709 Phase 2a). Scopes the lateral
  (same-layer, sidewall) coupling-capacitance pass onto these net names, on
  top of Phase 1's always-on vertical-overlap coupling (issue #760) -- so
  the re-simulated extracted-side testbench (and the resulting `delta[]`
  rows) reflect it. Off by default -- byte-identical to before this feature
  existed. See [`extract.md`](extract.md)'s "Lateral (same-layer, sidewall)
  coupling capacitance for critical nets" section.
- `--critical-net-pattern GLOB` / `--critical-net-all` /
  `--critical-net-exclude GLOB` — glob and all-nets forms of
  `--critical-net`, passed through to `klt extract` (issue #2974; see
  [`extract.md`](extract.md)'s "Many nets" paragraph).
  `extraction.critical_nets` echoes the resolved concrete names.
- `--resistance-graph` / `--resistance-graph-r-cut OHM` — passed through to
  `klt extract --resistance-graph` / `--resistance-graph-r-cut` (issue #4037),
  with the same validation errors (r-cut requires the graph flag; r-cut must be
  finite and `>= 0`, `0` = ideal via), surfaced as `PexError`. Unlike `klt
  extract`, which keeps the legacy per-level series sum in its netlist, `klt
  pex` writes each net's terminal-aware `selected_resistance_ohm` as that net's
  lumped R wherever `resistance_source` is `graph`; `legacy` nets (no per-cut
  resistance given, three or more terminals, ...) keep the legacy value, as do
  nets overridden with `--mom-rlc-resistance-ohm`. This removes the
  double-counting of strapped / via-stitched conductors from the re-simulated
  extracted side (#2458). Cannot be combined with `--distributed-rc` (the
  ladder stays on the legacy scalar, so a critical net would mix sources):
  `PexError`. Off by default -- the netlist and JSON report are byte-identical
  to a run without the flags. Echoed under `extraction.resistance_graph`.
- `--distributed-rc` — passed through to `klt extract --distributed-rc`
  (issue #977, Epic #709 Phase 2b); requires `--critical-net`. Replaces the
  single-lumped-star R/C model with a distributed, multi-segment ladder for
  every `--critical-net`-named net that has 2 or more device terminals -- so
  the re-simulated extracted-side testbench (and the resulting `delta[]`
  rows) reflect the finer-grained model. Off by default -- byte-identical to
  before this feature existed. See [`extract.md`](extract.md)'s "Distributed
  (multi-segment) RC ladder for critical nets" section.
- `--parasitic-variant` (issue #3056) — passed through to `klt extract
  --parasitic-variant` (`drop-r:NET|*`, `drop-c-ground:NET|*`,
  `drop-c-coupling`; repeatable) to drop parasitic elements and attribute a
  failing delta row. Echoed as `extraction.parasitic_variant` (present only
  when given). See [`extract.md`](extract.md)'s `--parasitic-variant`
  section.
- `--substrate-tie-ohms` — passed through to `klt extract
  --substrate-tie-ohms` (issue #3089): the substrate DC-tie resistance (default
  `1e12`, substrate capacitively floating in transient; `0` = hard tie). See
  [`extract.md`](extract.md)'s "Substrate tie value" section.
- `--mom-rlc-net` / `--mom-rlc-resistance-ohm` / `--mom-rlc-capacitance-ff` /
  `--mom-rlc-inductance-nh` — passed through to `klt extract --mom-rlc-net`
  (issue #988, Epic #709 Phase 3a). Substitutes a caller-supplied,
  directly-solved R/L/C for one named net -- e.g. the output of a separate
  [`klt mom`](mom.md) run against that net's real geometry -- in place of
  this extraction's own Phase 1/2 lumped-RC/coupling-C value for that net,
  so the re-simulated extracted-side testbench (and the resulting `delta[]`
  rows) reflect a MoM-grade parasitic on exactly the net a caller has
  singled out as critical. `--mom-rlc-net` requires at least one of the
  other three; `--mom-rlc-inductance-nh` is purely additive (there is no
  inductance term in the default model to replace). Off by default --
  byte-identical to before this feature existed. See
  [`extract.md`](extract.md)'s "Substitute a caller-supplied `klt mom` R/L/C
  for a critical net" section.
- `--format` — `text` (default, a human-readable pass/fail summary) or
  `json` (this command's own JSON envelope, see below).

## What it does

1. **Extract.** Runs `klt extract <layout> --deck <deck> --parasitics`
   (`src/klayout_tools/extract.py`'s `run_extract`, `parasitics=True`
   always) — the same first-order lumped-RC extraction `klt extract
   --parasitics` already ships (one series R per net terminal plus one
   ground C per net, from the deck's curated sheet-resistance/capacitance
   table; issue #760's vertical-overlap coupling capacitance included). `klt
   pex` has no schematic-equivalent-only mode — use `klt extract` directly
   for that.
2. **Re-simulate, per testbench, both sides.** For each `<testbench>`:
   - **Schematic side** — `klt sim <testbench>` exactly as authored, no
     modification at all (AC #2: "reusing the existing testbench
     conventions, no parallel testbench format").
   - **Extracted side** — a generated copy of the testbench's `netlist` body
     with its `.include`/`.inc` DUT reference re-pointed at the freshly
     extracted netlist (see "The DUT `.include` swap" below), run via `klt
     sim` with `netlist_source: "extracted"` set — the same field
     [`sim.md`](sim.md#post-layout-verification-netlist_source) already
     defines for exactly this purpose, not a parallel mechanism.
3. **Diff.** Every `(corner_id, measurement name)` pair present in the
   extracted-side response's `corners[]` is matched against the
   schematic-side response's corresponding corner/measurement and emitted as
   one `delta[]` row — see "Response" below.

## The DUT `.include` swap

A `klt sim` request's `netlist` field is one file with no built-in
distinction between "the DUT" and "the stimulus" (see
[`sim.md`](sim.md#netlist-convention-a-circuit-body-not-a-full-deck)).
[`extract.md`](extract.md#verified-compatible-with-klt-sims-netlist-convention)
documents the resolution already in use for a `klt extract`-produced
netlist: *"a thin testbench `.include`s the extracted file, instantiates the
`.subckt`, and adds the sources, and that testbench is the `klt sim`
`netlist`."*

`klt pex` leans on that same convention for the schematic side too — each
`<testbench>`'s `netlist` file is expected to `.include`/`.inc` a separate
schematic-equivalent DUT file (hand-written, or generator-produced, sharing
the extracted netlist's `.SUBCKT <top> <pins...> ... .ENDS <top>` interface)
rather than inlining the DUT's own devices directly:

```spice
* testbench.spice
.include "schematic_dut.spice"
.model res_generic_po r
Vdd RA 0 DC 1.8
Xres RA RB RES
Rload RB 0 1k
```

For the extracted-side run, `klt pex` rewrites *only* that one
`.include`/`.inc` line to point at the extraction's own written netlist —
every source, load, `.model` card, and measurement in the testbench is
byte-identical between the two runs. That rewritten line always names an
**absolute path** to the extracted netlist (issue #1525), regardless of
whether `-o`/`--outdir` were themselves given as relative or absolute paths:
each `klt sim` corner runs `ngspice -b` from a corner-scoped working
directory nested under `--outdir`, which resolves a relative `.include`
against *that* directory rather than the original invocation's `cwd` — an
absolute extracted-netlist path sidesteps that mismatch entirely. A
testbench with no `.include`/`.inc`
directive at all has no single swap point this command can use without also
parsing/rewriting device cards, which is out of scope — it is refused up
front with a clear error, not partially run.

### Exactly one `.include`/`.inc` line is required

A testbench body carrying **two or more** `.include`/`.inc` directives is
likewise refused up front (exit `1`), with an error naming every matched
line and its 1-based line number:

```
testbench netlist has 2 `.include`/`.inc` directives (line 2: `.include
"design.ngspice"`; line 3: `.include "schematic_dut.spice"`) -- klt pex swaps
exactly one of them for the extracted netlist and cannot tell which one is
the DUT ...
```

Which of several included files is "the DUT" is not inferable from the
directive alone. A testbench that also includes, say, a PDK's global
switch-parameter file (gf180mcu's `design.ngspice`) ahead of its DUT is
entirely legitimate ngspice — but before this was an error, `klt pex`
swapped the **first** matching line, silently re-pointing that file at the
extracted netlist, leaving the real schematic DUT in place on both sides,
and reporting the wrong `reference_netlist` (issue #1030).

Keep exactly one `.include`/`.inc` line: it is the swap point. Do **not**
fold another included file into that DUT netlist. The line is replaced by
the extracted netlist for the extracted-side run, so anything folded into
the schematic DUT file is present on the schematic side only — a PDK
switch-parameter file (needed by the corner `.lib` sections on both sides)
would then be missing from the extracted run, which fails with errors such
as `Undefined parameter`. Content both legs need must instead be inlined
into the testbench body (for example as a `.param` card). If that is not
possible, the testbench cannot be represented by the current request shape.

### That one line must plausibly name the DUT

Even with exactly one `.include`/`.inc` line, that file must itself
plausibly *be* a DUT (issue #1255). A testbench whose sole `.include`
happens to name a shared corner/parameter file — for example, because the
testbench's own DUT is netlisted as discrete device-level primitives
written directly in the testbench body (to break a feedback loop a
black-box DUT doesn't expose a port for), leaving only an unrelated
`.include` in the file — used to be silently swapped in as if it were the
DUT: the run completed with `status: "pass"`, `reference_netlist` naming a
file that was not the DUT at all, and an empty (or spurious) `delta[]` — a
vacuous pass, not an error. This is now a hard, up-front error (exit `1`),
before either side ever simulates:

```
testbench 'testbench.json': its single `.include`/`.inc` directive names
'/abs/path/design.ngspice', but that file declares no `.SUBCKT` and no
circuit element of its own -- it parses as parameter/model/corner-file
content only, so it cannot be the DUT klt pex is meant to swap for the
extracted netlist (issue #1255). ...
```

The check is deliberately permissive about *how* the DUT is written: both a
`.SUBCKT`-wrapped file and a flat file (bare device cards, no wrapper — see
"Flat schematic DUT vs `.SUBCKT`-wrapped extraction" below) pass it; only a
file with no circuit content at all — pure `.param`/`.model`/`.lib`/comment
lines — is rejected. If the DUT's own devices are written directly in the
testbench body instead of `.include`d as a separate file, `klt pex` cannot
re-point at the extracted netlist without also rewriting those device
cards, which is out of scope: rewrite the testbench so its single
`.include`/`.inc` line names the schematic DUT file itself.

### The pin lists must match: `pin_count_mismatch`

Because the testbench's own `X...` instantiation line is reused
byte-identically on both sides, the schematic DUT's `.SUBCKT <top>
<pins...>` header and the extracted netlist's must declare the **same number
of pins**. When they do not — the common case being a deck whose extraction
promotes a device-body/substrate-tap net (or any other physical net a
hand-written schematic subcircuit never models) to a top-level pin — a
strict-enough ngspice would refuse the extracted-side deck outright:

```
Too few parameters for subcircuit type "res" (instance: xxres)
    Simulation interrupted due to error!
```

`klt pex` never actually needs ngspice to say so, though: it detects the
mismatch **statically and pre-flight**, comparing the schematic DUT's and
the extracted netlist's `.SUBCKT` headers for the specific subcircuit `klt
extract` wrapped its output in, *before* either side ever simulates (issue
#1041) — the same comparison ngspice's own elaboration would otherwise make,
done in Python instead of relying on the engine to make and report it. This
is **engine-independent**: it fires identically whatever ngspice version is
installed, rather than depending on the engine actually rejecting the
mismatched deck (see "Detection is engine-independent" below for why that
distinction matters). It reports the mismatch **by name** rather than
leaving it buried in a per-corner `ngspice.log` artifact (issue #1030). The
run is not aborted: the JSON report is still emitted, with

- one `delta[]` row per schematic-side `(corner, measurement)` carrying
  `extracted_value: null`, `delta_pct: null`, `status: "error"` — so a
  caller still gets a per-corner report of exactly which rows have no
  trustworthy extracted value, and the extracted-side simulation is skipped
  entirely for every testbench (it cannot produce a trustworthy value
  either way), and
- a top-level `pin_count_mismatch` block naming both sides' netlists, their
  `.SUBCKT` pin lists and counts, and a `detail` sentence:

```json
"pin_count_mismatch": {
  "subcircuit": "RES",
  "schematic": {
    "netlist": "/abs/path/schematic_dut.spice",
    "subcircuit": "RES",
    "pin_count": 2,
    "pins": ["RA", "RB"]
  },
  "extracted": {
    "netlist": "/abs/path/top.spice",
    "subcircuit": "RES",
    "pin_count": 3,
    "pins": ["RA", "RB", "VSUBS"]
  },
  "ngspice_message": null,
  "detail": "the extracted netlist declares `.SUBCKT RES` with 3 pins (RA, RB, VSUBS) but the schematic DUT declares 2 (RA, RB) -- ..."
}
```

The exit code is `4` (a `delta[]` row errored), not `1` — the command ran,
it just has no trustworthy extracted-side value to report. `--format text`
prints the same block. On every run whose extracted side actually
simulated, `pin_count_mismatch` is `null`.

**A reactive fallback remains for what the static check cannot see.** The
pre-flight comparison above is scoped to the *instantiated* subcircuit
(`klt extract`'s own top-cell name) specifically, so it never false-positives
on an unrelated helper `.SUBCKT` of a different pin count elsewhere in
either netlist — but it also cannot catch a mismatch it has no name to
compare, e.g. the schematic DUT declaring its subcircuit under a name the
extracted netlist does not share, or an unreadable header. For those cases,
`klt pex` still falls back to inferring the mismatch reactively, from
ngspice's own refusal once an extracted-side run has already come back with
no measured value at all (issue #1030's original detection) — carrying
ngspice's own line (`ngspice_message`, non-`null` in that case) when the
corner kept its log artifact via `options.keep_artifacts`. Either path
populates the same `pin_count_mismatch` shape, so a caller does not need to
know which one fired; only `ngspice_message` tells you (`null` when the
pre-flight check caught it, ngspice's own text when the reactive fallback
did). The reactive fallback is gated on "nothing at all came back", so a
passing (or merely limit-failing) run can never pick up a false-positive
diagnostic from it.

**Detection is engine-independent.** Because the pre-flight check compares
the two `.SUBCKT` headers directly rather than waiting for ngspice to reject
the deck, behaviour is identical on every ngspice version — including
ngspice 42 (what Debian/Ubuntu `apt` currently ships, `42+ds-3build1`,
including in this repo's CI), which is measurably laxer than ngspice 46 and
silently accepts many pin-count mismatches instead of rejecting them (an
extra pin, or fewer pins than declared), simulating to completion with the
extra/missing terminals left dangling. Before this pre-flight check existed,
that meant a `status: "pass"` with `pin_count_mismatch: null` and **silently
wrong** extracted values on exactly the ngspice version most users and CI
actually run — the pre-flight check closes that gap by never depending on
ngspice's own strictness in the first place.

**Fixing a real mismatch is the caller's job, for now.** Give the schematic
subcircuit the same pin list as the extracted one, or extract with a
`--top` cell whose interface matches. A general bridging mechanism (a
caller-supplied `--pin-map`, or an explicitly-named wrapper subcircuit —
compare `klt lvs`'s [`hints.equivalent_pins`](lvs.md)) is deliberately out
of scope here: this reports the mismatch, it does not paper over it.

### Flat schematic DUT vs `.SUBCKT`-wrapped extraction: `flat_dut_mismatch`

`pin_count_mismatch` above assumes the schematic DUT itself declares a
`.SUBCKT` header — it compares two interfaces. A schematic DUT netlisted
**flat** (no `.SUBCKT`/`.ENDS` wrapper at all — e.g. because an EDA tool
netlisted it as a standalone top-level schematic, with the testbench
relying on the DUT's own internal nodes being ordinary top-level nets) is a
different, adjacent condition (issue #1255, Gap 2): `klt extract`'s output
is **always** `.SUBCKT`-wrapped, so swapping a flat schematic `.include`
for the wrapped extracted netlist silently disconnects every testbench
source/probe from the DUT — `klt pex` only re-points the `.include`/`.inc`
line, it never adds an `X...` instantiation of the newly-wrapped
subcircuit.

Like `pin_count_mismatch`'s own pre-flight check, this runs once, from the
two netlists' own text, *before* either side ever simulates — unlike
`pin_count_mismatch`'s *reactive fallback* (only ever consulted once an
extracted-side simulation attempt has already come back with nothing
measured), which cannot apply here at all: the flat schematic DUT declares
no `.SUBCKT` interface for the pre-flight check (or the fallback) to compare
in the first place, so a flat-vs-wrapped mismatch needs its own proactive
check. That matters because a flat-vs-wrapped mismatch does not reliably
fail loudly — a testbench with no `.ic`/similar card referencing the
now-disconnected nodes may simply simulate the disconnected fixture to
completion and report a plausible-looking but physically meaningless number
for a measurement that happens to be well-defined on the stimulus side
alone, exactly the failure mode `pin_count_mismatch`'s reactive fallback's
"nothing measured came back" gate cannot catch.

When it fires, the extracted side is never even attempted (so
`pin_count_mismatch` stays `null` — the two diagnostics are mutually
exclusive), and the report carries the same per-corner `delta[]` shape as a
`pin_count_mismatch` run (`extracted_value: null`, `status: "error"`) plus
a top-level `flat_dut_mismatch` block, in the same shape as
`pin_count_mismatch` (`subcircuit`, `schematic`/`extracted` per-side detail,
`ngspice_message` — always `null` here, since no engine is ever invoked —
and `detail`):

```json
"flat_dut_mismatch": {
  "subcircuit": "RES",
  "schematic": {
    "netlist": "/abs/path/flat_dut.spice",
    "subcircuit": null,
    "pin_count": null,
    "pins": null
  },
  "extracted": {
    "netlist": "/abs/path/top.spice",
    "subcircuit": "RES",
    "pin_count": 2,
    "pins": ["RA", "RB"]
  },
  "ngspice_message": null,
  "detail": "the schematic DUT (...) declares no `.SUBCKT` at all -- ..."
}
```

Exit code `4`, same as `pin_count_mismatch`. Fix by wrapping the schematic
DUT in a matching `.SUBCKT ... .ENDS` (with the same top-level pin list
`klt extract` produces), so the testbench's own instantiation line can be
reused unmodified on both sides — the same convention `pin_count_mismatch`
already requires.

### Pin order must match: `pin_order_mismatch`

The testbench's positional `X...` line is reused on both legs, so the
*order* of the pins matters as well as their count. `klt extract` writes its
`.SUBCKT <top>` header deterministically (real ports first, then parasitic
references, each alphabetical); a schematic DUT declares pins in symbol
order. When both headers declare the top subcircuit with the same number of
pins and the same pin set (compared case-insensitively) but a different
sequence, `pin_count_mismatch` and `flat_dut_mismatch` are both `null` and
the extracted leg would otherwise simulate with signals on the wrong pins and
report meaningless deltas.

`klt pex` detects this statically, before either leg simulates, and reports
a top-level `pin_order_mismatch` block in the same shape as
`pin_count_mismatch` (`subcircuit`, `schematic`/`extracted` per-side detail
with `pins` in declared order, `ngspice_message` — always `null`, no engine
is invoked — and `detail`). It is mutually exclusive with `pin_count_mismatch`
and `flat_dut_mismatch`, and always `null` in `--measure-command` mode.

**Exit behaviour.** Identical to its siblings: the extracted leg is skipped,
`delta[]` rows carry `extracted_value: null` and `status: "error"`, the
report `status` is `"error"`, and the exit code is `4`. Fix by wrapping the
schematic DUT in a `.SUBCKT` that declares the pins in the extracted order
(shown in the block), so one instantiation line is valid for both legs.
Rewriting the testbench instance by pin name, or reordering the extracted
header, is not done by `klt pex`.

## The extracted side carries its own substrate DC reference (issue #1263)

The extraction `klt pex` runs is `--parasitics`, so every net's ground
capacitance hangs off the deck's *synthesized* substrate net (`vsubs`, plus
any `<substrate_net>_iso<n>` isolated-region variant). Nothing in a layout
can label that net, so nothing used to give it a defined DC value: an
extracted-side leg whose testbench did not hand-author a tie hit ngspice's
`Warning: singular matrix:  check node x<dut>.vsubs`, and the
gmin/source-stepping recovery that followed could return a non-reproducible
operating point rather than failing loudly.

`klt extract --parasitics` now emits one `R<net>_dctie <net> 0 1e+12` shunt
per synthesized substrate net **inside the extracted `.SUBCKT` body** — see
[`extract.md`](extract.md#substrate-dc-reference-issue-1263). Because the
tie lives in the extracted artifact rather than in `klt pex`'s
testbench-assembly step, both legs behave predictably and nothing in this
command's own contract changes:

- **The `.include` swap is still the only edit `klt pex` makes.** Every
  source, load, `.model` card, and measurement in the testbench remains
  byte-identical between the two runs; `klt pex` adds no card of its own.
- **Both `pin_count_mismatch` and `flat_dut_mismatch` are unaffected.** The
  tie is a body card, not a pin: the extracted `.SUBCKT` header, its pin
  list, and its pin count are exactly what they were before, so both
  diagnostics compare the same interfaces they always did.
- **The schematic side never sees it.** The tie exists only in the
  extraction, so the schematic leg's `delta[].schematic_value` is unchanged
  by definition — and on the extracted side a 1 Tohm leak to ground is below
  every simulator tolerance for a node that already has a DC path, so an
  extracted leg that converged before converges to the same numbers now.
- **A testbench that already ties the substrate keeps working.** The default
  is idempotent with a hand-authored `.global vsubs` / `Vsubs vsubs 0 DC 0`
  / `.options rshunt=1e12`; there is nothing to remove. Keep an explicit
  `Vvsubs vsubs 0 DC 0` where the *measurement* needs the substrate held at a
  real 0 V **AC** reference (any coupling-capacitance delta does) — 1 Tohm
  anchors the DC solve, it is not a low impedance. This repo's own
  coupling-canary fixtures in `tests/test_pex.py` keep theirs for exactly
  that reason.

## Unbiased device bodies invalidate the comparison (issue #1983)

A PMOS device's body terminal extracts onto a **floating, anonymous net** (a
KLayout-synthesized `$5`-style name) whenever no well tie — drawn or derived
— reaches that device's `nwell` island. Per
[`extract.md`](extract.md#coverage), that net has **no DC bias path at all**,
which makes a full-circuit resimulation of the extracted netlist
"physically wrong, not merely imprecise": ngspice converges and produces
numbers, and those numbers are not comparable to a schematic-level
netlist's.

`klt pex`'s entire output *is* such a resimulation, compared row-by-row
against a schematic leg — so this condition does not merely reduce a
`delta[]` row's precision, it invalidates the comparison the row reports. And
a `klt pex` report is the artifact
[`design-evidence-tiers.md`](../design-evidence-tiers.md) item 7 (post-layout
verification — the item with the strictest citation rule in the checklist) is
cited from, while never carrying the extraction's own JSON. A reader of a
committed `klt pex` record therefore had no way to tell a post-layout number
measured on a properly-biased netlist from one measured on a floating-body
netlist.

The `body_bias` block states it:

```json
"body_bias": {
  "status": "unbiased",
  "unbiased_device_count": 3,
  "unbiased_nets": ["\\$5", "\\$7"],
  "unbiased_pmos_body_nets": [
    {"device": "$1", "net": "\\$5"},
    {"device": "$2", "net": "\\$5"},
    {"device": "$3", "net": "\\$7"}
  ]
}
```

It is reduced from the extraction this command drove itself (`klt extract`'s
`unbiased_pmos_body_nets[]`, issue #555) — nothing here re-derives the
condition, and the entries are carried through verbatim, same field name and
same `{"device", "net"}` shape, so a reader who knows one artifact already
knows the other.

**Reported, not enforced.** `status` is untouched: a run whose deltas all met
tolerance still reports `status: "pass"` with `body_bias.status: "unbiased"`,
and the exit code is unchanged. Whether an unbiased body should invalidate a
*claim* is a policy question for the consumer of the evidence — `klt signoff`
surfaces it on item 7's citation (`citation.body_bias`, see
[`signoff.md`](signoff.md)) without grading on it, and
[`design-evidence-tiers.md`](../design-evidence-tiers.md) item 7 states the
condition a `pex` citation is only valid under. Grading on it here would
retroactively fail every design on a PDK whose deck has no well-tap
mechanism, which is a separate decision from making the condition visible.

### Tying an unbiased body net (`--tie-net`)

Issue #4245. For a layout that cannot be relabelled, `--tie-net
'<extracted-net>=<schematic-net>'` (repeatable) merges the named
reported-unbiased net into a schematic net in the extracted netlist every
simulated leg reads:

```bash
klt pex amp.gds tb.json --deck sky130 --tie-net '\$5=VDD'
```

- `<extracted-net>` must be one of an untied run's `body_bias.unbiased_nets`.
  Any other name, or a layout with no unbiased net, is an error in the usual
  envelope error shape (`error.code: "invalid_tie_net"`) that lists the valid
  names; a malformed pair (no `=`, an empty half, one extracted net tied to two
  different schematic nets) is rejected the same way. This confines the flag
  to floating bodies — it cannot rewrite arbitrary nets.
- Several extracted nets may be tied to one schematic net.
- Anonymous names (`\$5`) are synthesized by KLayout and can change when the
  layout changes, so a declared tie is **per extraction**: re-read
  `unbiased_nets` after re-laying-out.
- `extraction.netlist_sha256` is the digest of the netlist as rewritten by the
  tie, so it always matches the file `netlist.path` points at.
- Without the flag the output is byte-identical to a run that never had it.

The tie is disclosed, not hidden. `body_bias` gains an additive `tied[]`:

```json
"body_bias": {
  "status": "tied",
  "unbiased_device_count": 3,
  "unbiased_nets": ["\\$5", "\\$7"],
  "unbiased_pmos_body_nets": [ ... ],
  "tied": [
    {"extracted_net": "\\$5", "schematic_net": "VDD", "source": "declared"},
    {"extracted_net": "\\$7", "schematic_net": "VDD", "source": "declared"}
  ]
}
```

`unbiased_pmos_body_nets` keeps reporting what the layout drew; ties are
additive. `status` stays `"unbiased"` while any unbiased net is left untied,
and is `"tied"` — never `"biased"` — once every one is tied, so an item-7
citation can tell "tied by declaration" (`source: "declared"`) from a drawn
tap (`"biased"`). The *reported, not enforced* policy above is unchanged:
`status` and the exit code do not depend on `body_bias`.

**Check first: a well label may be enough.** If the layout can be relabelled,
a text label on the deck's well label layer (`--label-layer well=<l>/<d>`; on
sky130 `nwell.pin`, 64/5) names the well net, which then drops out of
`unbiased_pmos_body_nets[]` with no `--tie-net` at all. Verified (issue
#4245) on the sky130 deck with a tap-less inverter: without a label the PMOS
body is the anonymous `\$8` and is reported unbiased; with an `nwell.pin`
label it is the named net and `unbiased_pmos_body_nets` is empty. Decks with no
well label layer (gf180mcu, per `extract.md`) cannot use this route, which is
what `--tie-net` is for.

**A clean run says so positively.** `status: "biased"` with a zero count is
emitted for a run that checked and found every body biased — distinct from a
pre-#1983 record that never reported it at all, where the field is simply
absent.

## A device-model divergence invalidates the comparison too: `model_mismatch`

`pin_count_mismatch`/`flat_dut_mismatch` (above) both compare the top-level
`.SUBCKT` **interface** each side's testbench-facing wrapper declares — its
pin list. Neither one looks at what is *inside* that interface: which
concrete device model each side's `M`/`X`/`Q`/`D` device cards actually
instantiate. A layout drawn with **no voltage-domain marker geometry**
always extracts onto a PDK family's *default* MOS flavour (`klt extract
--pdk`'s documented fallback — e.g. sky130's thin-oxide
`sky130_fd_pr__nfet_01v8`), while a reference (schematic) netlist naming a
different flavour — a thick-oxide/high-voltage variant such as
`sky130_fd_pr__nfet_g5v0` — is *topologically* identical: same device count,
same connectivity, same top-level pin list. `pin_count_mismatch` and
`flat_dut_mismatch` are both `null` for exactly this reason — yet the two
netlists are electrically unrelated, so every `delta[]` row this run
produces compares two different physical devices, not a parasitic effect.
Before this diagnostic existed, the only way to notice was reading the
extracted `.spice` snapshot by hand and comparing model tokens (issue
#2402).

`model_mismatch` compares the **multiset** of device model/`.subckt` names
the reference and extracted netlists' `M`/`X`/`Q`/`D` cards instantiate —
the same instance count per model matters too, not just which names appear,
so a device silently dropped or duplicated on one side is caught the same
way a substituted flavour is:

```json
"model_mismatch": {
  "kind": "model_set",
  "reference_only": ["sky130_fd_pr__nfet_g5v0"],
  "extracted_only": ["sky130_fd_pr__nfet_01v8"],
  "counts": {
    "reference": {"sky130_fd_pr__nfet_g5v0": 2},
    "extracted": {"sky130_fd_pr__nfet_01v8": 2}
  },
  "detail": "the reference (schematic) and extracted netlists instantiate different device models/`.subckt`s ..."
}
```

`reference_only`/`extracted_only` name every model that appears on one side
and not the other (by instance count, case-insensitively — ngspice itself is
case-insensitive about model/subcircuit names); `counts` echoes each side's
full per-model instance count so a caller can see a same-name-different-count
divergence too, even though that case leaves both list fields empty. `null`
when the two multisets agree exactly.

`kind` (issue #3054, additive) tells the two non-`null` cases apart without
re-deriving the set difference: `"model_set"` when `reference_only` or
`extracted_only` is non-empty (a wrong model flavour — the case above, and a
real reason to distrust `delta[]`), `"count_only"` when the model sets are
identical and only the instance counts differ. A `count_only` `detail` is
neutral: the usual cause is finger/multiplier decomposition (the layout draws
several fingers, the schematic has one instance with `nf`/`m` or none), which
`klt lvs` has already reconciled. It does not by itself make `delta[]`
unrelated, though representation effects (fingering, AS/AD) are folded into
`delta[]` alongside the parasitics. A hierarchical reference keeps the
hierarchy-artifact wording for either kind.

**Reported, not enforced — the same precedent as `body_bias`.** `status` is
untouched: a run whose deltas all met tolerance still reports `status:
"pass"` with a non-`null` `model_mismatch`, and the exit code is unchanged.
Unlike `pin_count_mismatch`/`flat_dut_mismatch`, this diagnostic **never
skips the extracted-side simulation** — the two `.SUBCKT` interfaces still
match, so the simulation itself runs and converges; it is the *result* that
is not comparable to the schematic leg, which is a policy question for the
consumer of the evidence (see [`signoff.md`](signoff.md) and
[`design-evidence-tiers.md`](../design-evidence-tiers.md) item 7), not one
this command answers on the caller's behalf.

**Caveat: a hierarchical reference netlist is a known false-positive
shape.** `klt extract`'s output is always flat, by design
([`extract.md`](extract.md)'s "Extraction is flat, not hierarchical" note,
issue #1085 — the same mismatch `klt lvs`'s `options.flatten_reference`
exists to fix). If the reference (schematic) netlist is instead
hierarchical — a leaf `.subckt` plus one or more `X`-card instance calls of
it — a device nested inside that leaf is counted once per *definition* by
this comparison, not once per physical *instance*, while the flat extracted
side always counts once per instance. That can produce the same divergent
multiset a genuine device-flavour swap would, without the two sides
actually naming different devices. `model_mismatch`'s `detail` names this
explicitly (rather than asserting the two sides are electrically unrelated)
whenever the reference netlist is hierarchical; treat a hierarchical-shaped
`detail` as inconclusive on its own and re-run with `--flatten-schematic`
(below), which removes the hierarchy from the comparison, before discarding
the run's `delta[]` evidence.

## Separating device representation from parasitics (`--device-only-leg`)

**`delta[]` against a literal schematic is not a pure parasitic delta.** The
extraction carries the *drawn* devices — units and fingers as separate
instances, each with its own AS/AD/PS/PD diffusion geometry, plus dummies —
while a schematic commonly writes one lumped instance (`ng=1`, full width).
Compact models do not treat those as the same device (narrow-width effects,
diffusion geometry, current-mirror ratios set by fingers of different unit
widths), so `delta[]` mixes representation effects with parasitic ones.
Whenever [`model_mismatch`](#a-device-model-divergence-invalidates-the-comparison-too-model_mismatch)
reports instance-count differences, read `delta[]` with that in mind; in one
observed use the representation step alone moved quiescent current by about
25 % and DC gain by about 8 %.

`--device-only-leg` (issue #3298) isolates the two. It re-runs the same
extraction with parasitics off — the same extracted devices and diffusion
geometry, no `R`/`C` cards and no `<net>__tN` star nodes; nothing is parsed
or stripped from the parasitic netlist, so no internal node naming is
depended on — writes it beside the extracted netlist as
`<name>.device-only<ext>`, and re-runs every testbench against it as a third
`klt sim` leg. Each `delta[]` row then adds:

- `device_only_value` — the measurement on the parasitic-free leg;
- `representation_delta_pct` — device-only vs schematic (representation and
  geometry effects), by the `delta_pct` rule;
- `parasitic_delta_pct` — extracted vs device-only (the parasitics alone), by
  the `delta_pct` rule with `device_only_value` as the base;
- `device_only_status` — that leg's own measurement status, `"error"` when it
  produced nothing for the row.

For positive-valued measurements the parts reconcile:
`(1 + representation/100) * (1 + parasitic/100) == 1 + delta_pct/100`, within
rounding. A part is `null` when a value it needs is missing or its base is
exactly `0`. A device-only leg that fails to simulate nulls only these
fields for that testbench; `status` and `delta_pct` are unchanged.

The top level adds `device_only_netlist` (`{path, scope}`),
`device_only_netlist_sha256`, `device_card_count` and `device_cards_match` —
whether the device cards (everything but `R`/`C`) equal the parasitic
extraction's once its star nodes are mapped back to their nets. `false` means
the leg is not purely device-only; treat the split as unreliable and inspect
both netlists. Without the flag none of these fields exist.

## Flattening a hierarchical schematic DUT (`--flatten-schematic`)

`klt pex` swaps only the testbench's single DUT `.include`, so the
schematic leg runs whatever that file contains — normally a hierarchical
netlist (user subcircuits instantiated from the top subcircuit), while the
extracted leg is always flat. Besides the `model_mismatch` false positive
above, that leaves every caller with a hierarchical design writing its own
flattener. `--flatten-schematic` (issue #2889) does it once, up front:

1. The one resolved schematic DUT is flattened by `klayout_tools.spice_flatten`
   (SPICE text in, SPICE text out — not KLayout's in-process
   `Netlist.flatten()`, so the result stays a simulable deck) and written to
   `<outdir>/schematic-dut.flat.spice`. A relative `.include`/`.inc`/`.lib
   <path>` inside the DUT file is rewritten to an absolute path against the
   DUT's own directory, since the copy no longer sits next to it.
2. Each testbench's schematic-side run uses a DUT-swapped copy
   (`<outdir>/<label>.schematic-request.json` +
   `<label>.schematic-dut-testbench.spice`) whose `.include` names that file
   by absolute path; every other request field, including its own
   `netlist_source`, is unchanged. In `--measure-command` mode the command is
   handed the flattened netlist for the schematic side instead.
3. `flat_dut_mismatch`, `pin_count_mismatch` and `model_mismatch` compare
   the flattened netlist against the extraction.
   `reference_netlist`/`reference_netlist_sha256` and
   `testbenches[].schematic_netlist*` still name the caller's own DUT file.

Flattening rules:

- **Inlined:** every `X` card whose target is a `.subckt` defined in the DUT
  file, recursively. **Kept:** each root `.subckt` (one nothing else
  instantiates) plus the extracted top cell, with its header verbatim;
  file-level cards outside any `.subckt` stay where they are.
- **Leaf devices:** an `X` target with no definition in the file (a PDK
  primitive such as `sky130_fd_pr__nfet_01v8`, defined by the model library
  the testbench loads) is emitted as-is. SPICE text alone cannot tell a PDK
  primitive from a missing user subcircuit, so every such name is listed in
  `schematic_flatten.external_subckts` — check it for anything that is not a
  PDK device. The flattener only sees subcircuits defined in the DUT file
  itself: one pulled in through the DUT's own `.include`/`.lib` is not read,
  so it stays an unflattened `X` call, appears in `external_subckts`, and
  (if nothing else was inlined) the run reports `already_flat: true`.
  (Departure from the issue's acceptance criteria, accepted in review: an
  undefined `X` target is kept as an external leaf rather than failing with
  `undefined_subckt`, because sky130 MOS devices are themselves `X` calls to
  PDK-library subcircuits.)
- **Names:** an inlined device `R1` reached through instances `X1` → `X2`
  becomes `R.X1.X2.R1` (the leading element letter is kept); an internal node
  `mid` becomes `X1.X2.mid`; pins map to the caller's nodes. Ground (`0`,
  `gnd`) and `.global` nets are never renamed.
- **Parameters:** header defaults, overridden by the instance's
  `name=value` arguments (evaluated in the caller's scope), then body
  `.param` cards, are substituted into the inlined cards' `name=value`
  values and `{...}`/`'...'` expressions. As in ngspice, scoping is
  hierarchical: a nested subcircuit also sees every parameter resolved on
  its enclosing instances (its own defaults and arguments shadow them). A
  root subcircuit's own parameters stay for ngspice to bind. An instance `m=` (when the subcircuit declares no
  `m` parameter) is pushed down onto every inlined device.
- **`.model` cards** inside an inlined subcircuit are hoisted to the
  enclosing root, de-duplicated.
- **Errors, never a partial flatten.** A recursion cycle, an `X` call whose
  node count differs from the subcircuit's pin count, a card the flattener
  cannot rename safely (a `B` source, polynomial `E`/`G`, transmission
  line, `.ic`/`.nodeset`/`.include` inside a subcircuit, an `m=` multiplier
  over a source), or unbalanced `.subckt`/`.ends` fail the run before
  anything simulates, with `error.code` `schematic_flatten_cycle`,
  `schematic_flatten_pin_count_mismatch`, `schematic_flatten_unsupported_card`
  or `schematic_flatten_malformed`.

The response reports it: `flatten_schematic: true` and

```json
"schematic_flatten": {
  "netlist": { "path": ".klt/pex/schematic-dut.flat.spice", "scope": "repo" },
  "netlist_sha256": "sha256:<64 hex>",
  "subckts": ["BUF"],
  "inlined_instances": 2,
  "inlined_subckts": ["INV"],
  "external_subckts": ["sky130_fd_pr__nfet_01v8", "sky130_fd_pr__pfet_01v8"],
  "already_flat": false
}
```

An already-flat DUT is still copied, run and reported (`already_flat:
true`, `inlined_instances: 0`). Without the flag, `flatten_schematic` is
`false` and `schematic_flatten` is `null`.

## The extracted network's own integrity report (issue #2764)

`klt pex` re-simulates the netlist the extraction wrote, so a defect in the
*emitted* RC network — a distributed ladder split into islands, a near-zero
resistor bridging two nets, a NaN-valued R, a ground capacitor that no
longer sums back to its net's declared total — would otherwise surface only
as a confusing `delta[]` row. `klt extract --parasitics` already validates
the network it emits; this command carries those diagnostics through as
`extraction.parasitic_integrity`, so a committed pex record is
self-describing about the health of the network its numbers were measured
on.

The block is `null` only for an extraction report predating issue #2764.
Otherwise it is `klt extract`'s own integrity summary verbatim (check
settings and aggregate counts — see [`extract.md`](extract.md)'s "Parasitic
network integrity" for the field list and the model limits), plus a `nets`
array re-attached from the extraction's `parasitics.nets[]` entries in that
same order: one `{"net", "net_id", "rc_model", "integrity"}` entry per net,
each `integrity` payload byte-identical to what `klt extract` inlines on
the corresponding `parasitics.nets[]` entry. The same `open` /
`islands` / `orphan_nodes` / `suspicious_resistors` / `short_candidates` /
`capacitance` statuses, the same threshold and tolerances — extract and
pex show the same diagnostics.

**These checks validate the generated RC model and cannot certify geometric
opens or shorts.** A `pass` says the emitted network is well-formed, not
that the layout is; see `extract.md`'s model-limits note. And like
`body_bias`/`model_mismatch`, the block is **reported, never folded into
`status`** — whether a finding should invalidate a comparison is a policy
decision for the evidence consumer, not one this command makes on the
caller's behalf.

## Measuring with a caller-supplied command (`--measure-command`)

Everything above describes this command's original (and still default) input
contract: a routed layout plus one or more `klt sim` request JSON
testbenches. That contract composes badly with one specific, real class of
block ([issue #2478](https://github.com/2AMLogic/klayout-tools/issues/2478)).
A `klt sim` request's `measurements[]` entries are *verbatim `.meas` cards*
([`sim.md`](sim.md)) — there is no caller-side post-processing hook — so a
spec row whose figure is not expressible as one `.meas` card cannot be driven
through `klt pex` at all.

Measurement shapes that are generic to analog blocks, not to any one design,
and that `.meas` cannot express:

- a **threshold crossing** that has to be *located* by a rule the caller
  applies across sample points or across separate runs (first crossing after
  a named edge, sign-corrected per sweep point, "unresolved" as a
  first-class outcome) rather than by one `.meas ... WHEN` card;
- a statistic computed **across a Monte Carlo family** and then rescaled by a
  gain fitted from a calibration sweep run alongside it;
- a measurement taken on a **derived netlist** — e.g. a loop-broken
  sub-model assembled from a subset of the DUT's own device lines to make a
  small-signal analysis well-posed. This one is doubly blocked by the
  testbench contract: the derived file does not exist until the harness
  builds it, so no `.include` swap can re-point at it;
- an **injection-based** measurement whose stimulus sources are placed at
  nodes derived from the DUT's own device lines, then inverted by an
  estimator the caller owns.

Because [`signoff.md`](signoff.md#item-7-is-kind-restricted-per-block-kind)'s
T1 item 7 ("Post-layout verification") accepts only a `pex`-kind envelope for
an analog partition, such a block could not cite item 7 **at all** — however
much real, committed, reproducible post-layout evidence it had. The invariant
issue #871 established is *"item 7 requires a real, disclosed
schematic-vs-extracted comparison"*, not *"item 7 requires that the
comparison was measured by `klt sim`"*.

`--measure-command` closes that without weakening the invariant. Instead of a
testbench set you supply **one command**, and `klt pex` runs it **twice**:

```sh
klt pex 06-layout.gds --deck sky130 \
  --measure-command 'uv run python3 threshold_crossing_probe.py' \
  --reference-netlist 07-reference.spice \
  --format json
```

1. **Extract** — unchanged: `klt extract --parasitics` on `<layout>`, driven
   by this command, with every `--deck-option`/`--pins`/`--critical-net`/
   `--distributed-rc`/`--mom-rlc-*` flag working exactly as above.
2. **Measure the schematic leg** — run the command with
   `--reference-netlist`'s path.
3. **Measure the extracted leg** — run the *same* command with the
   freshly-extracted netlist's path.
4. **Diff** — every `(corner_id, name)` pair the extracted leg reported is
   matched against the schematic leg's and emitted as one `delta[]` row, by
   exactly the same rules testbench mode uses.

### How the command is invoked

- **Argv, never a shell.** The string is split with shell *quoting* rules
  (`shlex.split`) once, up front, and then executed directly — no shell, no
  glob expansion, no metacharacter interpretation. An unbalanced quote is a
  clean exit-1 error, not a surprise at run time.
- **The netlist is the final argument.** `klt pex` appends the path of the
  netlist under measurement to the argv you gave. Your own words are never
  rewritten (there is no placeholder token to remember).
- **…and is in the environment too**, so a harness can read it whichever way
  suits it:

  | Variable | Value |
  |---|---|
  | `KLT_PEX_SIDE` | `schematic` or `extracted`; `device-only` for the third run `--device-only-leg` adds (issue #4293) |
  | `KLT_PEX_NETLIST` | the same path passed as the final argument |
  | `KLT_PEX_ARTIFACTS_DIR` | a per-side directory under this run's `--outdir` (`<outdir>/measure/<side>/`), created before the command runs — for whatever decks/logs/raw files the harness wants to keep |
  | `KLT_PEX_BACKEND` | the `--backend` value (`local`/`batch`/…), set **only** when `klt pex` was given `--backend`; the same value is also exported as `KLT_SIM_BACKEND` so a child `klt sim` obeys it. Absent otherwise — the host's inherited `$KLT_SIM_BACKEND` default applies unchanged |
  | `KLT_OUTDIR_LOCK_INHERIT` | the canonical path of `$KLT_PEX_ARTIFACTS_DIR`; a nested `klt` (e.g. `klt sim` on a request under that directory, default `-o`) treats that directory and its descendants as already owned by this run instead of failing with `outdir_locked` (additive, #3291) |

- **A non-zero exit, an unrunnable command, a timeout, or stdout that is not
  valid JSON is a hard error** (exit `1`), with a bounded tail of the
  command's own stderr in the message. An unmeasured leg is never quietly
  turned into `"error"` delta rows that would read like a real, failed
  comparison.

### The measurement document (the command's stdout)

Each invocation prints one JSON object on stdout:

```json
{
  "corners": [
    {
      "corner_id": "tt/1.800V/27C",
      "measurements": [
        { "name": "t_cross", "value": 1.24e-9, "status": "pass" }
      ]
    }
  ]
}
```

That is deliberately a **subset of `klt sim`'s own response shape**, not a
new vocabulary: every other key, at any level, is ignored, so a real `klt
sim` response (`schema_version`, `provenance`, per-corner `artifacts` and
all) validates unchanged and can be piped straight through by a harness that
only needs post-processing on *some* of its rows.

| Field | Type | Rule |
|---|---|---|
| `corners` | array\<object\> | Required, non-empty. |
| `corners[].corner_id` | string | Required, non-empty, unique in the document. Any string — it is matched between the two legs, never parsed. |
| `corners[].measurements` | array\<object\> | Required, non-empty. |
| `…measurements[].name` | string | Required, non-empty, unique within its corner. Becomes the `delta[]` row's `spec_row`. |
| `…measurements[].value` | number \| null | Required. A finite number, or `null` for the caller's own **"unresolved"** outcome. `null` renders an `"error"` delta row — never a fabricated pass. `NaN`/`Infinity`, booleans and strings are refused. |
| `…measurements[].status` | string | Optional, one of `"pass"`/`"fail"`/`"error"`. Omitted ⇒ `"error"` for a `null` value, `"pass"` otherwise. A caller that owns its own limits states them; a caller with none gets the same "measured, therefore comparable" default `klt sim` gives an unlimited measurement. |
| `…measurements[].message` | string | Optional, non-empty (issue #4555). Why the measurement has no value (e.g. a `.meas` threshold never crossed). Copied into `delta[].error.message` when the row errors on this side, with `stage: "execute"`; ignored on `pass`/`fail` measurements. A non-string or blank value is a hard error. |

Any violation of the table is a hard error (exit `1`) naming the offending
path (e.g. `corners[0].measurements[2].value`), for the same reason a failed
invocation is: a malformed document from one leg would otherwise be
indistinguishable from a real measurement failure.

### What this mode does *not* loosen

- **The caller supplies the measurement, never the comparison.** `klt pex`
  picks both netlists, runs the command against each, and computes every
  `delta[]` row itself. There is no way to hand it a finished delta set.
- **Extraction is still this command's own**, so `extraction` (including
  `extraction.model`), `body_bias` and `provenance` are derived from a real
  `--parasitics` extraction of your real layout, exactly as in testbench
  mode. The disclosures that give item 7 its teeth
  ([issue #1983](https://github.com/2AMLogic/klayout-tools/issues/1983)) are
  not weakened by a byte.
- **The mode is disclosed**, in the `measurement` block of the response (see
  the schema below): `mode`, the argv verbatim, the timeout in force, and —
  per side — the exact netlist measured, the command's exit status there, and
  how many corners/rows it reported. A reader of a committed record can tell
  an externally-measured post-layout comparison from a `klt sim`-measured
  one, and re-run it.
- **A hand-rolled envelope is still not evidence.** The opt-in `generic`
  envelope remains item 8 only ([`signoff.md`](signoff.md)); a `"kind":
  "generic"` document asserting that a post-layout comparison happened still
  renders `"unmet"`/`"wrong_kind"` for item 7.

### Two diagnostics are inapplicable here

`pin_count_mismatch`, `flat_dut_mismatch` and `pin_order_mismatch` are always `null` in this mode.
Both exist because testbench mode re-runs the testbench's **own unmodified
`X...` instantiation line** against both netlists, so a pin-list or
flat-vs-wrapped difference silently breaks the extracted-side deck. A
caller-supplied command builds each side's deck itself from the path it is
handed, so neither the premise nor the consequence holds — reporting them
anyway would be a false alarm, and (worse) would skip an extracted-side
measurement the harness is perfectly able to take. This is the flip side of
the contract: in this mode, keeping the two sides' decks comparable is the
harness's responsibility, not this command's.

`model_mismatch` **is** still computed: it compares what the two netlists
*contain* (their `M`/`X`/`Q`/`D` device cards), which is independent of how
either side was measured.

The `--reference-netlist` file must itself plausibly *be* a DUT — the same
"declares a `.SUBCKT` or a circuit element of its own" check testbench mode
applies to its `.include` target
([issue #1255](https://github.com/2AMLogic/klayout-tools/issues/1255), Gap 1)
— so a parameter/corner file named by mistake is a clean error rather than a
vacuous pass.

### Running a corner grid from the command

The command inherits `klt pex`'s environment, so on a host that sets
`$KLT_SIM_BACKEND=batch` a child `klt sim` already routes a multi-unit request
(corners / Monte Carlo) to the batch fleet. A harness that measures a PVT grid
should therefore express it as a `klt sim` request and call `klt sim` — not
hand-roll `ngspice` loops on the invoking host. An explicit
`klt pex --backend X` reaches the child as `$KLT_PEX_BACKEND` and
`$KLT_SIM_BACKEND` (the flag wins over a pre-set `$KLT_SIM_BACKEND`, matching
`klt sim`'s `--backend` > env precedence); a harness that must refuse or route
differently can read `$KLT_PEX_BACKEND`.

- The two legs run **sequentially**, so batch wall time is two grid
  submissions back to back.
- Pass `klt sim -o` a path **outside** `$KLT_PEX_ARTIFACTS_DIR`: a child
  `klt sim` writing under that tree hits `outdir_locked`
  ([#3291](https://github.com/2AMLogic/klayout-tools/issues/3291)).

### Portability note: `.include`d files travel off-host

Because deck construction is the harness's job in this mode, one
portability property is worth stating outright: if your harness generates a
`klt sim` request of its own, an `.include`/`.inc` in that request's netlist
**does** survive the trip to an off-host backend. `klt sim`'s `remote`/`batch`
backends — which a multi-unit request reaches by default on any host that sets
`$KLT_SIM_BACKEND` — stage the netlist's whole resolved include closure
alongside it and rewrite each directive to the staged, job-relative name
([issue #2485](https://github.com/2AMLogic/klayout-tools/issues/2485); see
[`sim.md`](sim.md) → "Off-host backends stage the netlist's `.include`
closure" for the exact rules, including which includes are deliberately left
for the executing host to resolve).

Before that fix, only the request's *own* netlist file was staged, so an
`.include` naming a path on the submitting host resolved nowhere on the
executing one — and failed in the quietest possible way: every corner came
back `"unavailable_measurement"`, the harness still exited `0`, and `klt pex`
rendered a full set of `"error"` delta rows that read like a real, failed
comparison. The same trap caught **testbench mode**, whose contract mandates
an `.include`d DUT, on any off-host-defaulted host. Both are fixed at the
transport, so neither mode needs a workaround now; an include that resolves
nowhere on the submitting host is refused at submit time with a named error
instead. (`examples/design-pipeline/12-pex-external.probe.py` inlines the
netlist it is handed — that still works, and is still the simplest thing a
harness can do.)

## Scope-mismatch note (resolved by this issue, #801)

Issue #871 (Phase 2b of epic #706, merged before this command existed) taught
`klt signoff`'s envelope-kind detector a **provisional, Curator-proposed**
`pex` shape ahead of this command's own definition (see
[`signoff.md`](signoff.md#item-7-is-kind-restricted-klt-pex)) — its own
worked examples show `klt pex extracted.spice schematic.spice --format json`,
i.e. **two already-produced netlists** as input. This command's actual
Acceptance Criteria (and Epic #709's Phase 1a goal) instead take a **routed
layout plus a testbench set** as input, since `klt pex` must run extraction
itself, not merely diff two netlists someone already produced and simulated.
This document (and the shipped command) follow that AC; the response
*shape* nonetheless matches the provisional envelope exactly (`delta[]` +
`reference_netlist` + the fields below), so `klt signoff`'s existing
recognition logic needed no change.

## JSON schema (the contract)

**JSON is the API.** Human-readable text output is a courtesy; the schema
below is the stable contract, subject to the same rules as every other `klt`
verb — see [`json-contract.md`](../json-contract.md) for the envelope
(`schema_version`, error shape, exit codes) shared across all commands.

**Schematic-side content identity (issue #2875, additive; `schema_version`
stays `2`).** `provenance.input.content_hash` and `extraction.netlist_sha256`
identify the layout and the extracted netlist only. Every `delta[]` row also
depends on the schematic side, so each is content-addressed separately — one
digest per role, never a combined hash — as `sha256:<64 lowercase hex>` of the
file's exact bytes, computed at the existing resolution points before either
side simulates:

| Field | Bytes covered |
| ----- | ------------- |
| `testbenches[].request_sha256` | The request JSON file given as `<testbench>` (stimulus corner list, `.meas` cards, models). |
| `testbenches[].netlist_sha256` | The simulation body file the request's `netlist` names (resolved request-dir-relative). |
| `testbenches[].schematic_netlist_sha256` | The single DUT file that body `.include`s — the file the extracted-side run swaps out. Two testbenches sharing a DUT carry the same value. |
| `reference_netlist_sha256` | `reference_netlist`: the shared DUT, or `--reference-netlist` in `--measure-command` mode (where `testbenches` is empty and the extracted leg stays bound by `extraction.netlist_sha256`). |

Digests stay present when the matching path is redacted as `scope:
"external"`. Each `testbenches[]` entry has the same keys whether or not its
extracted side ran (diagnostic/partial runs included). Paths resolve as
before (request-dir-relative body, body-dir-relative DUT include). Legacy
envelopes lack these fields and remain readable; `klt signoff` item 7
verifies them when present ([`signoff.md`](signoff.md)).

**Per-side execution evidence (issue #2872 / #2876, additive; `schema_version`
stays `2`).** `delta[]` keeps only values, so a row with `null` on both sides
cannot say *why*. Each `testbenches[]` entry therefore also carries:

| Field | Meaning |
| ----- | ------- |
| `testbenches[].sides.schematic` / `.extracted` | The side's `klt sim` evidence, or `null` when that side was never simulated (e.g. the extracted side skipped by a pin-count/flat-DUT mismatch). |
| `sides.<side>.environment` | The subset of that run's `klt sim` `environment` that identifies where it ran: `engine`, `engine_version`, `ngspice_binary`, `engine_host`, `client_ngspice_binary` and `remote`, each copied only when `klt sim` reported it. `remote` is present only when the off-host backend that ran (`remote` or `batch`) filled `environment.remote`, and carries whatever that backend reports there (for `batch`, its `job_id`; see [`sim.md`](sim.md)). A local run has no `remote` key; nothing is fabricated. |
| `sides.<side>.corners[]` | One record per corner, keyed by `corner_id` within its testbench: `corner_id`, `status` (the sim corner status), `diagnostics[]` and `produced_value`. |
| `corners[].diagnostics[]` | The `klt sim` diagnostic objects, copied unchanged (e.g. `batch_job_failed` with `runner_code`). `klt pex` keeps no allow-list of codes; unknown codes and optional keys pass through. |
| `corners[].produced_value` | `true` when any measurement of that corner on that side has a non-null `value`. |
| `testbenches[].nothing_simulated_corners` | `corner_id`s where **neither** side produced a non-null measurement, derived from the two sides' corner records (not from `delta[]`). Such a corner is an execution failure: nothing was simulated, so it says nothing about the extracted netlist. `[]` when the extracted side did not run. |

A one-sided failure (schematic `produced_value: true`, extracted `false`) is
**not** listed in `nothing_simulated_corners`: the schematic side worked and
the extracted side did not, which is a genuine pex finding. These fields do
not change `delta[]`, `coverage`, `status` or the exit code. Side data is
per testbench entry and per `corner_id`, so repeated corner or measurement
names across testbenches never collide. `--measure-command` runs have no
`testbenches[]` and so no `sides`.

**Input-path fields report `{path, scope}`, never a raw path string
(`schema_version` `2`, issue #1261).** `layout`/`netlist`/`reference_netlist`
and each `testbenches[]` entry's `request`/`schematic_netlist` are
normalised via `env_provenance.repo_relative_path` — the same primitive and
shape [`env-provenance.md`](env-provenance.md) documents. A committed
evidence record wraps a `klt pex` response unmodified
(`docs/design/sim-evidence-discipline-spike.md`), so an absolute path in one
of these fields used to leak the run's own filesystem layout (author's home
directory, a Loom worktree number) into every such record; this shape makes
that structurally impossible instead of relying on every caller to remember
to scrub it. `pin_count_mismatch`/`flat_dut_mismatch`'s own nested `netlist`
fields are **not** normalised (out of this issue's scope) and stay raw
strings — see `docs/cli/env-provenance.md`'s `{path, scope}` table for the
full `repo`/`external`/`absent` scope meanings.

```json
{
  "schema_version": 2,
  "status": "pass",
  "layout": { "path": "top.gds", "scope": "repo" },
  "netlist": { "path": "top.spice", "scope": "repo" },
  "reference_netlist": { "path": null, "scope": "absent" },
  "extraction": {
    "deck": "sky130",
    "device_count": 1,
    "net_count": 2,
    "netlist_sha256": "71d273ab...",
    "model": {
      "capacitance": "net-to-ground for every net's own ...",
      "coupling": "vertical overlap (crossover) unconditionally, plus lateral (same-layer, sidewall) coupling for `--critical-net`-named nets ...",
      "resistance": "single lumped series resistance per net ...",
      "frequency": "quasi-static -- one frequency-independent R and C per net ..."
    },
    "critical_nets": [],
    "distributed_rc": false,
    "mom_rlc_override": null,
    "label_layers": null,
    "device_models": null,
    "parasitic_integrity": {
      "short_threshold_ohm": 1.0,
      "cap_tolerance": { "absolute_ff": 1e-06, "relative": 1e-09 },
      "max_detail_items": 20,
      "nets_checked": 2,
      "nets_with_findings": 0,
      "open_nets": 0,
      "island_components": 0,
      "orphan_nodes": 0,
      "suspicious_resistors": 0,
      "short_candidates": 0,
      "capacitance_mismatches": 0,
      "nets": [
        {
          "net": "RB",
          "net_id": 3,
          "rc_model": "lumped",
          "integrity": {
            "open": {
              "status": "not_applicable",
              "reason": "lumped model: terminal legs attach to the hub by construction; no distributed segments to check connectivity over",
              "disconnected_count": 0,
              "disconnected_terminals": [],
              "details_truncated": false
            },
            "islands": { "count": 0, "details": [], "details_truncated": false, "reason": null },
            "orphan_nodes": { "count": 0, "details": [], "details_truncated": false, "reason": null },
            "suspicious_resistors": { "count": 0, "details": [], "details_truncated": false, "reason": null },
            "short_candidates": { "count": 0, "details": [], "details_truncated": false, "reason": null },
            "capacitance": {
              "status": "pass",
              "declared_ff": 2.45871,
              "computed_ff": 2.45871,
              "delta_ff": 0.0,
              "reason": null,
              "tolerance": { "absolute_ff": 1e-06, "relative": 1e-09 }
            }
          }
        }
      ]
    }
  },
  "measurement": {
    "mode": "testbench",
    "command": null,
    "timeout_s": null,
    "sides": null
  },
  "reference_netlist_sha256": "sha256:77d0...",
  "testbenches": [
    {
      "request": { "path": "gain-tb.json", "scope": "repo" },
      "request_sha256": "sha256:9f2c...",
      "netlist": { "path": "gain-tb.spice", "scope": "repo" },
      "netlist_sha256": "sha256:41ab...",
      "schematic_netlist": { "path": null, "scope": "external" },
      "schematic_netlist_sha256": "sha256:77d0...",
      "corner_count": 3,
      "measurement_names": ["gain_db"]
    }
  ],
  "corner_count": 3,
  "delta": [
    {
      "spec_row": "gain_db",
      "corner_id": "tt/1.800V/27C",
      "schematic_value": 42.1,
      "extracted_value": 41.6,
      "delta_pct": -1.19,
      "status": "pass"
    }
  ],
  "passed": 3,
  "failed": 0,
  "errored": 0,
  "coverage": {
    "testbenches": 1,
    "delta_rows": 3,
    "corners_compared": 3,
    "nothing_checked": false,
    "nothing_checked_reasons": []
  },
  "pin_count_mismatch": null,
  "pin_order_mismatch": null,
  "backend_failure": null,
  "flat_dut_mismatch": null,
  "model_mismatch": null,
  "flatten_schematic": false,
  "schematic_flatten": null,
  "provenance": {
    "klt_version": "0.4.2",
    "klayout_version": "0.30.10",
    "pdk": { "name": "sky130A", "source": "volare", "version": "<stamp>" },
    "deck": { "name": "sky130", "content_hash": "sha256:<hex>", "released": true },
    "input": { "content_hash": "sha256:<hex>", "role": "layout" }
  }
}
```

### Top-level fields

| Field               | Type              | Description                                                                          |
| ------------------- | ----------------- | -------------------------------------------------------------------------------------- |
| `schema_version`    | integer           | Version of this command's JSON shape (`2` as of issue #1261's `{path, scope}` path-normalization change, per `docs/json-contract.md`).      |
| `status`             | string            | Aggregate: `"pass"`, `"fail"`, `"error"`, or `"not_checked"`. Precedence: `error` > `fail` > `not_checked` > `pass` — mirrors `klt sim`. |
| `layout`             | object            | `{path, scope}` — `<layout>` normalised via `env_provenance.repo_relative_path` (issue #1261): `scope: "repo"` with a repo-relative `path` when `<layout>` sits inside the invocation's repo, else `{"path": null, "scope": "external"}`. The absolute path is never echoed. |
| `netlist`            | object            | `{path, scope}` — the extracted (parasitic-annotated) netlist `klt extract` wrote, normalised the same way as `layout`. |
| `reference_netlist`  | object            | `{path, scope}` — the schematic DUT file every testbench `.include`d (see "The DUT `.include` swap" above), normalised the same way as `layout`. `{path: null, scope: "absent"}` for `--extract-only` (issue #3739), which has no schematic side. |
| `extraction`         | object            | `deck`, `device_count`, `net_count`, `netlist_sha256` (echoed from `klt extract`'s own report), `model` (`extract.py`'s `PARASITIC_MODEL_SCOPE`, verbatim — what the extracted side's R/C model does and does not account for), `critical_nets` (issue #976 — the `--critical-net` request echoed back, `[]` when the flag was never given), `distributed_rc` (issue #977 — `true` only when `--distributed-rc` was given, `false` otherwise), `parasitic_variant` (issue #3056 — the `--parasitic-variant` request echoed back; present only when the flag was given), `mom_rlc_override` (issue #988 — `null` unless `--mom-rlc-net` was given, in which case `klt extract`'s own substitution report; see [`extract.md`](extract.md)'s "Substitute a caller-supplied `klt mom` R/L/C for a critical net" section for the field list), `label_layers` (issue #2686 — `null` unless `--label-layer` was given, in which case `klt extract`'s own `label_layers` echo: `{"<role>": [layer, datatype] | null}`, sorted by role, `null` for a role cleared with `none`), `device_models` (issue #4234 — `null` unless `--device-model` was given, in which case the applied `{"<class>": "<subckt>"}` mapping), `resistance_graph` (issue #4037 — present only with `--resistance-graph`: `{r_cut_ohm, nets[]}` where each `nets[]` entry is `{net, net_id, resistance_source, selected_resistance_ohm, legacy_resistance_ohm}`; `resistance_source` says which value the extracted netlist's lumped R used), and `parasitic_integrity` (issue #2764 — the extraction's own per-net parasitic RC network integrity diagnostics; see below). Pins the extraction method alongside `provenance.deck`'s content-hash version pin. |
| `body_bias`          | object            | Issue #1983 (additive field). Whether the extracted netlist this run re-simulated actually had a **DC bias path for every device body**: `status` (`"biased"`/`"unbiased"`, or `"tied"` when every unbiased net was declared tied with `--tie-net`), `unbiased_device_count`, `unbiased_nets` (the distinct synthesized net names, sorted), and `unbiased_pmos_body_nets` (`klt extract`'s own `{"device", "net"}` entries, verbatim), and — only with `--tie-net` (issue #4245) — `tied[]` (`{extracted_net, schematic_net, source: "declared"}`). See "Unbiased device bodies invalidate the comparison" above. Reduced from the extraction this command drove itself; it never changes `status`. |
| `measurement`        | object            | Issue #2478 (additive field, always present). How this run's two legs were measured, so a reader of a committed record never has to infer it from the presence/absence of `testbenches[]`. `mode` is `"testbench"` (the default path — `command`/`timeout_s`/`sides` are then all `null`) `"command"` (a caller-supplied `--measure-command` produced both legs) or `"extract-only"` (issue #3739: `--extract-only` measured nothing — `testbenches` and `delta` are empty, `status` is `not_checked`, and `command`/`timeout_s`/`sides` are `null`). For `"command"`: `command` is the argv **list** verbatim (never a shell string — it is never run through a shell), `timeout_s` the per-invocation cap in force, and `sides` an object keyed `schematic`/`extracted`, each carrying `netlist` (`{path, scope}`, the exact netlist that side was measured on), `exit_status`, `corner_count` and `measurement_names`. See "Measuring with a caller-supplied command" above. |
| `testbenches`        | array\<object\>   | One entry per `<testbench>`: `request` (`{path, scope}` — the `<testbench>` argument, normalised the same way as `layout`; issue #1261), `request_sha256`, `netlist` (`{path, scope}` — the simulation body the request names), `netlist_sha256`, `schematic_netlist` (`{path, scope}` — the resolved DUT path it `.include`d, normalised the same way), `schematic_netlist_sha256`, `corner_count`, `measurement_names`, and the issue #2872 `sides` / `nothing_simulated_corners` execution evidence (see "Per-side execution evidence" below). The `*_sha256` fields are the issue #2875 content identities, see "Schematic-side content identity" below. Informational — the full per-corner detail lives in `delta[]`. **Empty (`[]`) for a `--measure-command` run**, which has no testbenches at all; `measurement.sides` carries the equivalent per-leg detail there. |
| `reference_netlist_sha256` | string | Issue #2875 (additive). `sha256:<64 lowercase hex>` of the exact bytes of `reference_netlist` — the schematic DUT (testbench mode) or `--reference-netlist` (`--measure-command` mode). `null` only if the file was unreadable. |
| `corner_count`       | integer           | Number of distinct `corner_id` values across every `delta[]` row.                       |
| `delta`              | array\<object\>   | One entry per `(testbench, corner, spec row)` — see "`delta[]` entries" below.          |
| `passed`/`failed`/`errored` | integer    | `delta[]` row counts by `status`.                                                       |
| `schematic_failed`   | integer           | Issue #2989 (additive). Count of `delta[]` rows whose `schematic_status` is `"fail"`. Reported, not enforced: it never feeds `status`, `coverage` or the exit code, so a response can read `pass` while this is non-zero — check it when the limits are a contract both sides must meet. |
| `coverage`           | object            | Issue #1996 (additive field). What this `status` was actually compared over: `testbenches`, `delta_rows`, `corners_compared`, plus the shared `nothing_checked`/`nothing_checked_reasons` roll-up. See "`coverage`: an empty `delta[]` is not a pass" below. |
| `pin_count_mismatch` | object \| null    | `null` on every run whose extracted side simulated (issue #1030 — additive field). Non-`null` names the schematic/extracted top-level pin-list mismatch that made the extracted-side deck unrunnable: `subcircuit`, a `schematic` and an `extracted` object (each `netlist`, `subcircuit`, `pin_count`, `pins` — `null` pin data when the header could not be read), `ngspice_message` (ngspice's own line, `null` when no per-corner log was kept), and a human-readable `detail`. See "The pin lists must match" above. |
| `backend_failure`    | object \| null    | `null` unless an extracted-side `klt sim` call raised something other than a pin-count mismatch (issue #2990 — additive field), e.g. a batch-fleet capacity refusal. Non-`null` is `{testbench, side, message, code}` for the first such failure (`side` is always `"extracted"`; `code` is the underlying `SimError.code`, e.g. `batch_no_capacity`, or `null`). The run does **not** abort: that testbench's `delta[]` rows are `status: "error"` with the schematic value intact and `extracted_value: null`, and its `testbenches[].sides.schematic` evidence (including a remote job id) is kept, so only the extracted side needs re-running. A caller can retry mechanically on `code`. A schematic-side failure still aborts (nothing completed), but its error envelope now carries `error.code` too. |
| `flat_dut_mismatch`  | object \| null    | `null` unless the schematic DUT is netlisted flat (no `.SUBCKT` wrapper) against a `.SUBCKT`-wrapped extraction (issue #1255, Gap 2 — additive field). Same shape as `pin_count_mismatch` (`subcircuit`, `schematic`/`extracted` objects, `ngspice_message` — always `null` here, no engine is ever invoked — and `detail`); mutually exclusive with `pin_count_mismatch` (the extracted side is never attempted when this fires). See "Flat schematic DUT vs `.SUBCKT`-wrapped extraction" above. |
| `pin_order_mismatch` | object \| null    | `null` unless both sides declare the top subcircuit with the same pin set in a different order (issue #2890 — additive field). Same shape as `flat_dut_mismatch` (`ngspice_message` always `null`); mutually exclusive with `pin_count_mismatch`/`flat_dut_mismatch`; `null` in `--measure-command` mode. The extracted leg is skipped and the run exits `4`. See "Pin order must match" above. |
| `model_mismatch`     | object \| null    | `null` when the reference (schematic) and extracted netlists' `M`/`X`/`Q`/`D` device cards instantiate the same models/`.subckt`s the same number of times (issue #2402 — additive field). Non-`null` names the divergence: `kind` (`model_set` or `count_only`, issue #3054), `reference_only`/`extracted_only` (model names unique to one side, by instance count), `counts` (each side's full per-model instance count), and a human-readable `detail`. Unlike `pin_count_mismatch`/`flat_dut_mismatch`, computed unconditionally (independent of whether the top-level pin lists match) and never skips the extracted-side simulation — it never changes `status`. See "A device-model divergence invalidates the comparison too" above. |
| `flatten_schematic`  | boolean           | Issue #2889 (additive, always present). Echo of `--flatten-schematic` — `false` when not given. |
| `schematic_flatten`  | object \| null    | Issue #2889 (additive). `null` unless `--flatten-schematic` was given. Otherwise the flattened schematic DUT the schematic leg ran: `netlist` (`{path, scope}`), `netlist_sha256` (`sha256:<hex>` of its exact bytes), `subckts` (root `.subckt` names kept), `inlined_instances`, `inlined_subckts`, `external_subckts` (undefined `X` targets kept as leaf devices — audit these) and `already_flat`. See "Flattening a hierarchical schematic DUT" above. |
| `device_only_netlist` | object            | Issue #3298 (additive, `--device-only-leg` only — absent otherwise). `{path, scope}` of the parasitic-free extraction the third leg ran. |
| `device_only_netlist_sha256` | string \| null | Issue #3298 (additive, `--device-only-leg` only). `sha256:<hex>` of its exact bytes. |
| `device_card_count`  | integer           | Issue #3298 (additive, `--device-only-leg` only). Device cards (non-`R`/`C`) in the device-only netlist. |
| `device_cards_match` | boolean           | Issue #3298 (additive, `--device-only-leg` only). `true` when those cards equal the parasitic extraction's with star nodes mapped back to their nets; `false` means the leg is not purely device-only. |
| `provenance`         | object            | The extraction's own shared reproducibility block (`klt_version`, `klayout_version`, `pdk`, `deck`, `input`) — see [`json-contract.md`](../json-contract.md#shared-provenance-block). `deck` pins the extraction deck (name + `sha256:` content hash — the "deck version" this report pins); `input` pins `<layout>`. |

### `delta[]` entries

| Field             | Type            | Description                                                                          |
| ----------------- | --------------- | --------------------------------------------------------------------------------------- |
| `spec_row`        | string          | The measurement name, from the testbench's own `measurements[].name` (see [`sim.md`](sim.md)) — or, for a `--measure-command` run, from the measurement document's own `measurements[].name`. When more than one `<testbench>` is given, prefixed `<testbench file stem>.<name>` to disambiguate reused measurement names across testbenches; bare `<name>` for a single testbench, and for a `--measure-command` run (there is exactly one measurement pair). |
| `corner_id`       | string          | `klt sim`'s own corner id (`<process>/<supply>V/<temp>C`, see [`sim.md`](sim.md#corners-entries)) — identical on both sides, since the extracted-side request differs from the schematic-side one *only* in its `.include`d DUT file. |
| `schematic_value` | number \| null  | The measurement's value on the schematic-side run, or `null` if that corner/measurement was missing or itself errored. |
| `extracted_value` | number \| null  | The measurement's value on the extracted-side run, or `null` if it was unextractable (a `klt sim` `"error"`-status measurement). |
| `delta_pct`       | number \| null  | `100 * (extracted_value - schematic_value) / abs(schematic_value)`, rounded to 3 decimals; `null` when either value is missing or `schematic_value` is exactly `0`. |
| `status`          | string          | `"pass"`, `"fail"`, or `"error"` — mirrors the **extracted-side** measurement's own `klt sim` status (the side item 7 actually grades) against its declared `measurements[].limits`, except `"error"` also covers a missing/errored schematic-side value (no trustworthy delta to report). Issue #3620: an optional measurement that is `"absent"` on exactly one side (a figure lost — or newly appearing — after parasitics) is also `"error"` (`delta_pct: null`, `error` blames the absent side); `"absent"` on both sides is `"pass"`. Never a delta-magnitude threshold of its own — the same measurement `limits` a caller already declared, not a second, undocumented tolerance. |
| `schematic_status` | string         | Issue #2989 (additive). The schematic-side measurement's own `klt sim` limit status: `"pass"`, `"fail"`, `"error"` (also when the schematic measurement is missing), or `"absent"` (issue #3620: an optional measurement with no value). Does not affect `status`. |
| `extracted_status` | string         | Issue #2989 (additive). The extracted-side measurement's own `klt sim` limit status (may be `"absent"`, see `schematic_status`); `"error"` for rows whose extracted side never produced a response. |
| `error`           | object          | Issue #3062 (additive). Present **only** on `status: "error"` rows (absent on `pass`/`fail`), naming why the row has no trustworthy value: `side` (`"schematic"` or `"extracted"`), `stage` (`"submit"`, `"execute"`, `"collect"`, `"parse"` or `"unknown"` — `"unknown"` when the source code maps to no stage, never guessed), `message`, and `code` when the source has one (e.g. `batch_job_failed`, `batch_no_capacity`). Taken from that side's corner `diagnostics[]` (first error-severity entry) or, when that side never returned a report, from the recorded failure; `remote` carries the side's `environment.remote` (the batch job id) when present. In `--measure-command` mode, when the blamed side has no diagnostics, `message` is that side's measurement-document `message` and `stage` is `"execute"` (issue #4555). The side's full evidence stays under `testbenches[].sides.<side>.corners[]` for the same `corner_id`. |
| `device_only_value` | number \| null | Issue #3298 (additive, `--device-only-leg` only). The measurement on the parasitic-free third leg; `null` when that leg produced nothing for this row. |
| `representation_delta_pct` | number \| null | Issue #3298 (additive, `--device-only-leg` only). `delta_pct`'s rule applied to `schematic_value` → `device_only_value`: the device-representation/geometry part of `delta_pct`. |
| `parasitic_delta_pct` | number \| null | Issue #3298 (additive, `--device-only-leg` only). `delta_pct`'s rule applied to `device_only_value` → `extracted_value`: the parasitic part of `delta_pct`. |
| `device_only_status` | string | Issue #3298 (additive, `--device-only-leg` only). The device-only measurement's own `klt sim` status, or `"error"` when the leg produced nothing for this row. |

When the exit code is `4` because rows errored and at least one row carries an
`error`, one line is also printed to **stderr** (both `--format json` and
`--format text`; stdout is unchanged): the errored-row count, and the first
cause's side, stage, code and message. A `SimError` that aborts the run
(exit `1`) now keeps its `code` (e.g. `batch_no_capacity`) as the error
envelope's `error.code`.

### `coverage`: an empty `delta[]` is not a pass

The [common v1 coverage contract](../coverage-contract.md) accompanies the
existing `testbenches`, `delta_rows` and `corners_compared` coverage counters.
`checked` names actual `(corner, spec_row)` comparisons with pass/fail
results. Error rows are `unavailable_comparison` skips; a testbench with no
corners or measurements is a `no_comparison_pairs` skip.

A comparison with identical values still produces a checked row and can
pass. An empty `delta` is known zero (`no_delta_rows`): without another
failure/error it now returns `status: "not_checked"`, exit 4. Error rows can
also leave zero actual comparisons, but the aggregate remains `error` with
exit 4. The legacy counters continue to include all delta rows, so a row
count alone must not be treated as proof of comparison.

`klt signoff` refuses zero, unknown or malformed common coverage. Partial
comparisons remain disclosed for the Phase 2 success policy.

## Output-directory ownership

**Output-directory ownership (issue #2770).** `klt pex` owns its artifact
root (`--outdir`, default `.klt/pex/` next to the layout) for the whole run.
A second cooperating run on the same directory (any relative or symlink
alias of it) -- or on an ancestor or descendant of it -- fails immediately
with error code `outdir_locked`: exit `1`, empty stdout, the standard error
envelope on stderr, and nothing under the directory modified. The
per-testbench `klt sim` runs on `<outdir>/<testbench>/schematic|extracted`
inherit that ownership, and an independent `klt sim`/`klt pex` aimed at one
of those child directories is refused while the run is active. A killed or
crashed run releases ownership automatically; a leftover owner record never
blocks the next run. A `--measure-command` harness is the one child process
that inherits ownership: `klt pex` exports `KLT_OUTDIR_LOCK_INHERIT` (the
canonical `$KLT_PEX_ARTIFACTS_DIR`), so a nested `klt sim` whose output
directory is that directory or a descendant -- including the default
`.klt/sim/` beside a request written there -- runs without `outdir_locked`.
Only descendants are covered: an ancestor or sibling of it is still refused,
and a harness that fans out parallel `klt sim`s into the same directory loses
the guard among themselves. Pass `klt sim -o <dir>` to a distinct directory
per parallel run. `outdir_lock_unavailable` (same exit/streams) means
locking itself is not possible here. Full policy, library exceptions and
platform scope: [json-contract.md](../json-contract.md) "Output-directory
ownership".

## Exit codes

| Exit code | Meaning                                                                                     |
| --------- | -------------------------------------------------------------------------------------------- |
| `0`       | At least one actual comparison ran and every `delta[]` row passed.                                                                   |
| `1`       | Failed to run at all — bad layout/testbench, unresolvable deck/PDK, a testbench with no `.include`/`.inc` DUT reference **or more than one** or one that does not plausibly name the DUT (issue #1255, Gap 1), testbenches disagreeing on their schematic DUT, or an extraction/simulation failure. For a `--measure-command` run (issue #2478): also a mis-combined invocation (`--measure-command` with a `<testbench>`, or without `--reference-netlist`; `--reference-netlist`/`--measure-timeout-s` without `--measure-command`), a `--reference-netlist` that is missing or is not a DUT, and any measurement-command failure — unrunnable, non-zero exit, timeout, non-JSON stdout, or a malformed measurement document. With `--flatten-schematic` (issue #2889): a schematic DUT the flattener refuses (cycle, `X`-call pin-count mismatch, unsupported card, unbalanced `.subckt`), classified as `error.code` `schematic_flatten_*`. Documented error shape on stderr (`--format json`). |
| `3`       | Ran successfully; at least one `delta[]` row's `status` is `"fail"`.                           |
| `4`       | No actual comparison ran (`status: "not_checked"`), or at least one `delta[]` row's `status` is `"error"` — including a schematic/extracted pin-list mismatch, which reports a named `pin_count_mismatch` block here rather than aborting with exit `1`, and a flat-schematic-DUT-vs-`.SUBCKT`-wrapped-extraction mismatch, which reports a named `flat_dut_mismatch` block the same way (issue #1255, Gap 2), and a same-pins-different-order mismatch, which reports a named `pin_order_mismatch` block the same way (issue #2890), and an extracted-side backend failure, which reports a named `backend_failure` field (issue #2990). |

(`2` is reserved for argparse usage errors, as with every other `klt`
subcommand. Exit codes `3`/`4` mirror `klt sim`'s own precedent — see
[`sim.md`](sim.md#exit-codes).)

**Gate on `status`, not the exit code.** These codes are additive — a
future release may add a new one above `4` — and the exit code is only a
shortcut derived from the payload's own `status` field, which is
authoritative. See [`docs/json-contract.md`](../json-contract.md#exit-codes)'s
"Exit codes" section.

## Relationship to `klt signoff`'s post-layout item

`klt signoff`'s tier-verdict mode (`--manifest`, Epic #706) grades T1 item 7
("Post-layout verification") only against a citation that classifies as kind
`"pex"` — see [`signoff.md`](signoff.md#item-7-is-kind-restricted-klt-pex).
This command's `--format json` output classifies as that kind (a top-level
`delta[]` list plus `reference_netlist`) and passes item 7 on `status:
"pass"`, exactly as that document describes.

**A `--measure-command` run is graded identically** (issue #2478). It emits
the same envelope — same `delta[]`, same `reference_netlist`, same
`extraction`/`body_bias`/`provenance` — so it classifies as `pex` and grades
`"met"` with no change to item 7's kind restriction, and no new envelope
kind. That is the point: the restriction exists so a clean DRC cannot
masquerade as a post-layout re-simulation, not so that a block with a genuine
post-layout re-simulation is locked out for using a measurement `klt sim`'s
request schema cannot express.

## Relationship to Epic #709's later phases

This command's report pins its extraction method (`extraction.model`) and
deck version (`provenance.deck`) so [#802](https://github.com/2AMLogic/klayout-tools/issues/802)
(Phase 1b) can store it under the append-only `sim/` evidence convention
without re-deriving provenance, and so
[#803](https://github.com/2AMLogic/klayout-tools/issues/803) (Phase 1c) can
cite it as the T1 improvement proof on one canary. Coupling capacitance
beyond issue #760's vertical-overlap case is Phase 2a scope
([#976](https://github.com/2AMLogic/klayout-tools/issues/976)) — landed as
the `--critical-net` flag above, opt-in and scoped to a caller-declared net
set (not run unconditionally across the whole layout, the way vertical
coupling is). Distributed RC is Phase 2b scope
([#977](https://github.com/2AMLogic/klayout-tools/issues/977)) — landed as
the `--distributed-rc` flag above, opt-in and scoped to the same
`--critical-net` set. Feeding a real `klt mom` (Method-of-Moments, Epic
#701) R/L/C for a chosen critical net into this command's own extracted
netlist is Phase 3a scope
([#988](https://github.com/2AMLogic/klayout-tools/issues/988)) — landed as
the `--mom-rlc-net`/`--mom-rlc-resistance-ohm`/`--mom-rlc-capacitance-ff`/
`--mom-rlc-inductance-nh` flags above, opt-in and scoped to one named net at
a time, substituting into (rather than merely reporting alongside) that
net's Phase 1/2 value — so the `delta[]` report can be produced against a
MoM-grade parasitic for that net. See [`extract.md`](extract.md)'s
"Substitute a caller-supplied `klt mom` R/L/C for a critical net" section
for the full substitution mechanics.

**Phase 2a's own measurable-delta proof.** The `sky130-ota-5t` canary above
is DC-only by construction (every measurement point is an ideal-voltage-
source-driven net hub, `delta_pct: 0.0` "measured, not assumed" per its own
[README](../../examples/design-pipeline/README.md#s10-pex-delta-proof-epic-709-phase-1c-803)),
so it has no current/charge-carrying, high-impedance node for a coupling-
capacitance delta to show up on — extending it is not this issue's proof
vehicle. Instead, `tests/test_pex.py`'s
`test_run_pex_critical_net_lateral_coupling_canary` is a real, ngspice-driven
`klt pex` run on a purpose-built two-net high-impedance-node fixture (the
exact net class Epic #709 Phase 2's own text names): `--critical-net` off
reports `extracted_value: 0.0` on the victim net (Phase 1's baseline,
unchanged by this issue); `--critical-net VIC` on the identical layout and
testbench reports a real, nonzero, reproducible coupling voltage — a
measurable, explainable delta re-derived from a real simulation on every CI
run, rather than a one-off hand-captured evidence blob.

**Phase 2b's own measurable-delta proof
([#977](https://github.com/2AMLogic/klayout-tools/issues/977)).**
`tests/test_pex.py`'s `test_run_pex_distributed_rc_canary` is a real,
ngspice-driven `klt pex` run on a purpose-built fixture: two sky130 poly
resistors joined head-to-tail by a long (500 um) li1 run, so the internal
node between them (`MID`) is a genuine high-impedance routed net with real
interconnect R/C of its own (~11 kohm / ~53 fF) — exactly the net class
Epic #709 Phase 2's own text names ("high-impedance nodes", "a PLL loop
filter"). A fast (10 ps rise) step reaches `MID` through either 1 resistor
hop (`--critical-net MID` alone, Phase 2a's own baseline model: `MID`'s
entire capacitance sits at one star hub) or 2 (`--critical-net MID
--distributed-rc`: `MID`'s capacitance splits across its own ladder node and
its neighbour's, an extra pole the star cannot represent). Same testbench,
geometry, and schematic reference, same net-level R/C totals either way —
the identical early-sample-point measurement moves by a real, reproducible
amount purely because `--distributed-rc` changes *where* that R/C sits
along the net, not *how much* of it exists.

## Evidence discipline: records, supersession, and pinning are repo-owned

Like `klt sim`, `klt pex` is a stateless, single-invocation command:
`provenance` and `extraction` pin the inputs and method a *given* run used
(layout content hash, extraction deck + content hash, the R/C model scope
note) but nothing persists across runs, chains one run to the next, or
enforces a policy from a previous run's result. That scope decision — and
how a caller should fold a `klt pex` delta report into the same append-only
`sim/` evidence convention `klt sim` already uses (record wrapper,
supersession chains, pinning, subset-reason, spread checks) — is decided in
[`docs/design/sim-evidence-discipline-spike.md`](../design/sim-evidence-discipline-spike.md).
Short version: a `klt pex` record sits alongside the schematic-only `klt
sim` record for the same `<block>/<corner-scope-slug>`, distinguished
structurally by the presence of `result.delta` rather than a new
wrapper-level field, and pins `extraction.model` +
`provenance.deck` as its extraction-method/deck-version pin
(`extraction_pin` in the wrapper) — so a later extraction-method change
(e.g. a Phase 2 lumped-RC → coupling-C upgrade) mints a new record via the
convention's existing `supersedes` chain rather than overwriting history.
