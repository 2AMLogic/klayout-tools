# `klt pex`

Extract a lumped-RC **parasitic-annotated** netlist from a routed layout,
re-run one or more schematic testbenches against it per corner, and report a
**per-corner, per-spec-row schematic-vs-extracted delta** — a measured,
explainable degradation on every spec row, not a bare "post-layout OK".
Phase 1a of [Epic #709](https://github.com/2AMLogic/klayout-tools/issues/709)
("PEX-aware post-layout sim flow for klt").

```
klt pex <layout> <testbench>... --deck sky130|gf180mcu|sg13g2 [-o|--output <netlist.spice>] [--top <cell>] [--pdk <variant>] [--pdk-root <root>] [--outdir <dir>] [--backend <backend>] [--critical-net <net>]... [--distributed-rc] [--format text|json]
```

- `<layout>` — path to a GDSII (`.gds`) or OASIS (`.oas`) routed layout
  stream. Same auto-detection as `klt extract`/`klt drc`.
- `<testbench>...` — one or more `klt sim` request JSON files (see
  [`sim.md`](sim.md)), each already converged against the schematic-level
  design (e.g. an S5 sizing testbench). Re-run **completely unmodified** for
  the schematic-side leg of each delta row; re-pointed at the extracted
  netlist (see "The DUT `.include` swap" below) for the extracted-side leg.
  Every testbench must `.include`/`.inc` the *same* schematic DUT file — `klt
  pex` reports one `reference_netlist` for the whole run.
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
- `--critical-net` — repeatable, passed through to `klt extract
  --critical-net` (issue #976, Epic #709 Phase 2a). Scopes the lateral
  (same-layer, sidewall) coupling-capacitance pass onto these net names, on
  top of Phase 1's always-on vertical-overlap coupling (issue #760) -- so
  the re-simulated extracted-side testbench (and the resulting `delta[]`
  rows) reflect it. Off by default -- byte-identical to before this feature
  existed. See [`extract.md`](extract.md)'s "Lateral (same-layer, sidewall)
  coupling capacitance for critical nets" section.
- `--distributed-rc` — passed through to `klt extract --distributed-rc`
  (issue #977, Epic #709 Phase 2b); requires `--critical-net`. Replaces the
  single-lumped-star R/C model with a distributed, multi-segment ladder for
  every `--critical-net`-named net that has 2 or more device terminals -- so
  the re-simulated extracted-side testbench (and the resulting `delta[]`
  rows) reflect the finer-grained model. Off by default -- byte-identical to
  before this feature existed. See [`extract.md`](extract.md)'s "Distributed
  (multi-segment) RC ladder for critical nets" section.
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
byte-identical between the two runs. A testbench with no `.include`/`.inc`
directive at all has no single swap point this command can use without also
parsing/rewriting device cards, which is out of scope — it is refused up
front with a clear error, not partially run.

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

```json
{
  "schema_version": 1,
  "status": "pass",
  "layout": "top.gds",
  "netlist": "top.spice",
  "reference_netlist": "/abs/path/schematic_dut.spice",
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
    "distributed_rc": false
  },
  "testbenches": [
    {
      "request": "gain-tb.json",
      "schematic_netlist": "/abs/path/schematic_dut.spice",
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
  "provenance": {
    "klt_version": "0.4.2",
    "klayout_version": "0.30.10",
    "pdk": { "name": "sky130A", "source": "volare", "version": "<stamp>" },
    "deck": { "name": "sky130", "content_hash": "sha256:<hex>" },
    "input": { "content_hash": "sha256:<hex>" }
  }
}
```

### Top-level fields

| Field               | Type              | Description                                                                          |
| ------------------- | ----------------- | -------------------------------------------------------------------------------------- |
| `schema_version`    | integer           | Version of this command's JSON shape (starts at `1`, per `docs/json-contract.md`).      |
| `status`             | string            | Aggregate: `"pass"`, `"fail"`, or `"error"`. Precedence: `error` > `fail` > `pass` — mirrors `klt sim`. |
| `layout`             | string            | Echo of `<layout>`, exactly as provided.                                                |
| `netlist`            | string            | Path to the extracted (parasitic-annotated) netlist `klt extract` wrote.                |
| `reference_netlist`  | string            | Absolute path of the schematic DUT file every testbench `.include`d (see "The DUT `.include` swap" above). |
| `extraction`         | object            | `deck`, `device_count`, `net_count`, `netlist_sha256` (echoed from `klt extract`'s own report), `model` (`extract.py`'s `PARASITIC_MODEL_SCOPE`, verbatim — what the extracted side's R/C model does and does not account for), `critical_nets` (issue #976 — the `--critical-net` request echoed back, `[]` when the flag was never given), and `distributed_rc` (issue #977 — `true` only when `--distributed-rc` was given, `false` otherwise). Pins the extraction method alongside `provenance.deck`'s content-hash version pin. |
| `testbenches`        | array\<object\>   | One entry per `<testbench>`: `request` (the file path as given), `schematic_netlist` (the resolved DUT path it `.include`d), `corner_count`, and `measurement_names`. Informational — the full per-corner detail lives in `delta[]`. |
| `corner_count`       | integer           | Number of distinct `corner_id` values across every `delta[]` row.                       |
| `delta`              | array\<object\>   | One entry per `(testbench, corner, spec row)` — see "`delta[]` entries" below.          |
| `passed`/`failed`/`errored` | integer    | `delta[]` row counts by `status`.                                                       |
| `provenance`         | object            | The extraction's own shared reproducibility block (`klt_version`, `klayout_version`, `pdk`, `deck`, `input`) — see [`json-contract.md`](../json-contract.md#shared-provenance-block). `deck` pins the extraction deck (name + `sha256:` content hash — the "deck version" this report pins); `input` pins `<layout>`. |

### `delta[]` entries

| Field             | Type            | Description                                                                          |
| ----------------- | --------------- | --------------------------------------------------------------------------------------- |
| `spec_row`        | string          | The measurement name, from the testbench's own `measurements[].name` (see [`sim.md`](sim.md)). When more than one `<testbench>` is given, prefixed `<testbench file stem>.<name>` to disambiguate reused measurement names across testbenches; bare `<name>` for a single testbench. |
| `corner_id`       | string          | `klt sim`'s own corner id (`<process>/<supply>V/<temp>C`, see [`sim.md`](sim.md#corners-entries)) — identical on both sides, since the extracted-side request differs from the schematic-side one *only* in its `.include`d DUT file. |
| `schematic_value` | number \| null  | The measurement's value on the schematic-side run, or `null` if that corner/measurement was missing or itself errored. |
| `extracted_value` | number \| null  | The measurement's value on the extracted-side run, or `null` if it was unextractable (a `klt sim` `"error"`-status measurement). |
| `delta_pct`       | number \| null  | `100 * (extracted_value - schematic_value) / abs(schematic_value)`, rounded to 3 decimals; `null` when either value is missing or `schematic_value` is exactly `0`. |
| `status`          | string          | `"pass"`, `"fail"`, or `"error"` — mirrors the **extracted-side** measurement's own `klt sim` status (the side item 7 actually grades) against its declared `measurements[].limits`, except `"error"` also covers a missing/errored schematic-side value (no trustworthy delta to report). Never a delta-magnitude threshold of its own — the same measurement `limits` a caller already declared, not a second, undocumented tolerance. |

## Exit codes

| Exit code | Meaning                                                                                     |
| --------- | -------------------------------------------------------------------------------------------- |
| `0`       | Every `delta[]` row passed.                                                                   |
| `1`       | Failed to run at all — bad layout/testbench, unresolvable deck/PDK, a testbench with no `.include`/`.inc` DUT reference, testbenches disagreeing on their schematic DUT, or an extraction/simulation failure. Documented error shape on stderr (`--format json`). |
| `3`       | Ran successfully; at least one `delta[]` row's `status` is `"fail"`.                           |
| `4`       | Ran successfully; at least one `delta[]` row's `status` is `"error"`.                          |

(`2` is reserved for argparse usage errors, as with every other `klt`
subcommand. Exit codes `3`/`4` mirror `klt sim`'s own precedent — see
[`sim.md`](sim.md#exit-codes).)

## Relationship to `klt signoff`'s post-layout item

`klt signoff`'s tier-verdict mode (`--manifest`, Epic #706) grades T1 item 7
("Post-layout verification") only against a citation that classifies as kind
`"pex"` — see [`signoff.md`](signoff.md#item-7-is-kind-restricted-klt-pex).
This command's `--format json` output classifies as that kind (a top-level
`delta[]` list plus `reference_netlist`) and passes item 7 on `status:
"pass"`, exactly as that document describes.

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
`--critical-net` set. MoM-grade extraction remains later-phase scope (not
this command) — see Epic #709.

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
