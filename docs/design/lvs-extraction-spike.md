# Spike: netlist extraction + LVS (`klt extract` / `klt lvs`)

**Status:** spike / proposal. Nothing here is scheduled, and nothing here
authorises implementation. Per
[docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "How capabilities arrive," a
major capability arrives by spiking a design epic first — candidate-engine
survey, proposed JSON contract, wrap/build decision — and this document is
that spike for the layout→verification half of the closed loop. It is
Phase 1 of Epic #153; Phases 2–4 (the `klt extract` build, the `klt lvs`
build, and loop closure through `klt sim`) carry the implementation and are
gated on the findings here. It follows the structure the two prior accepted
spikes set:
[docs/design/spice-corner-runner-spike.md](spice-corner-runner-spike.md)
(for `klt sim`) and
[docs/design/layout-generator-spike.md](layout-generator-spike.md)
(#104, for `klt gen`).

**Demand signal:** the vision statement (ARCHITECTURE.md → "Vision") names
the loop as spec → schematic/generator → sized circuit → layout →
**DRC/LVS clean → extracted netlist** → simulation-verified. Two of those
arrows are missing. `klt drc` (shipped) closes "DRC clean"; nothing in `klt`
closes "LVS clean" or produces an "extracted netlist," so a finished layout
cannot be checked against its schematic and cannot be re-simulated as built.
This is not a hypothetical: it is confirmed live from two independent
bring-ups recorded on #54.

**The friction is documented twice, on two open PDKs.** #54's own comments
record it:

- **sky130** — driving the Epic #105 Phase 3 worked example (a sky130 5T
  OTA, `examples/design-pipeline/`), Loop A (sizing ↔ simulation at the
  schematic level) closed cleanly — `klt sim` reported 20/20 corners passing
  — but the run stopped at stage S8. With no `klt` verb to extract a netlist
  from the layout or compare it to the schematic, the post-extraction
  simulation pass (the design doc's S9/S11) was structurally unreachable. A
  schematic-level pass "is real evidence, not a substitute for the
  post-extraction pass this issue blocks."
- **gf180mcu** — an independent DRC/LVS bring-up on a second open PDK hit
  the same wall from the other side: `klt` has DRC (`klt drc --deck
  gf180mcu`) but "nothing to reach for on the LVS side," so the
  layout-vs-schematic check had to be driven **entirely outside `klt`**, via
  the PDK's own native LVS deck. A live data point that the gap is not
  sky130-specific.

**What is being driven outside the tool is not a solver.** As with the
SPICE corner runner, the copy-pasted / out-of-band part is *orchestration*:
building a connectivity model from a layout, emitting an extracted netlist
in a form a downstream simulator can actually consume, running a topological
compare against a reference netlist, and turning the comparer's verdict into
a structured, machine-readable mismatch report. That observation drives both
the contract below and the wrap/build call in §3.

## 1. Candidate-engine survey

Licenses, headless posture, and maintenance status below were fetched **live**
during this spike (GitHub REST API for repo license / `archived` / `pushed_at`
stamps, the projects' own `LICENSE`/`Copying`/source-header text, and — for
KLayout — direct capability probing of the exact pip package this repo already
pins), not recalled from memory. Same verification discipline as
[docs/design/layout-generator-spike.md](layout-generator-spike.md) §1 and
[docs/design/lambdalib-survey.md](lambdalib-survey.md).

### KLayout's own `LayoutToNetlist` + `NetlistComparer` (the wrapped dependency)

| Property | Finding |
| -------- | ------- |
| Upstream | Ships inside KLayout itself — the pip [`klayout`](https://github.com/KLayout/klayout) package this repo already pins (`pyproject.toml`: `klayout>=0.29`), used today by `klt drc` and `klt render`. Repo is active (`pushed_at: 2026-07-28`, not archived) and GPL-3.0. |
| License | KLayout core is GPL-3.0 — already the posture this repo accepts for every existing verb. Importing the `klayout` package as a wrapped dependency (the same relationship `klt drc` already has to KLayout's DRC engine, per [docs/cli/drc.md](../cli/drc.md) → "Engine") is precedent, not a new decision this spike introduces. |
| Headless posture | Already proven headless in this repo — every existing `klt` command runs `klayout.db` in CI with no GUI. Both the extractor and the comparer are plain `klayout.db` classes, not GUI features. |
| Extraction API | **Present and verified live.** Probing this repo's own `.venv` (`klayout` 0.30.10): `klayout.db.LayoutToNetlist` exists — it builds a connectivity model (`connect(l1, l2)` for same-layer/inter-layer connectivity, `connect_global(...)` for substrate/well, device extractors like `DeviceExtractorMOS4Transistor`, `...Resistor`, `...Capacitor`, `...BJT3Transistor` for device recognition) and produces a `pya.Netlist`. `NetlistSpiceWriter` (also verified present) serialises that `Netlist` to SPICE. |
| LVS / compare API | **Present and verified live.** `klayout.db.NetlistComparer` exists — a graph-isomorphism netlist comparer with net/device/pin matching, hints for ambiguous cases (`same_nets`, `same_circuits`, `equivalent_pins`), and a structured pass/fail with per-object mismatch enumeration. `NetlistSpiceReader` (verified present) parses a reference SPICE netlist into a `pya.Netlist` for the comparer's other input. |
| One dependency, both halves | Decisive: KLayout does **both** extraction *and* comparison inside the single pip dependency the repo already ships. No new engine, no new install, no second geometry backend — the same "one geometry backend" argument that governed the `klt gen` spike's engine choice (layout-generator-spike.md §1, cross-cutting takeaways) applies here verbatim. |
| Structured results | No JSON at the engine layer (same as every engine this repo wraps) — the comparer exposes results through its C++/Python object model (matched/unmatched nets, devices, pins), which is exactly the raw material §2's contract turns into the structured report. |

### netgen (contrast candidate — the open-flow LVS default)

| Property | Finding |
| -------- | ------- |
| Upstream | [`RTimothyEdwards/netgen`](https://github.com/RTimothyEdwards/netgen) — the LVS comparator the open-PDK flow (magic + netgen + open_pdks) defaults to. Active (`pushed_at: 2026-07-07`, not archived), `VERSION` 1.5.323 at survey time. |
| License | GPL. The GitHub API reports `NOASSERTION` (its top-level `Copying` file carries legacy FSF General-Public-License boilerplate that the detector doesn't map to an SPDX id), but the source headers are explicit — e.g. `base/netcmp.c`: *"This program is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation."* Fine to invoke as a separate process; like Xyce in the SPICE spike, it forecloses in-process embedding in an MIT-licensed surface, which permanently constrains the implementation options if it were the engine. |
| Headless posture | Batch-capable and genuinely headless — `netgen -batch lvs "<layout.spice> <subckt>" "<schematic.spice> <subckt>" <setup.tcl>`. But it is a **Tcl-driven** tool: standing it up in CI means a netgen install *plus* its Tcl runtime, on top of whatever produced the layout-side netlist. |
| Critical gap: comparator only | **netgen does not extract.** It compares two SPICE netlists; it has no layout front-end. In the open flow the layout-side netlist comes from **magic** (`extract` → `ext2spice`) — so "use netgen" is really "use magic *and* netgen," two more non-pip, non-KLayout tool installs, a second geometry/extraction engine distinct from `klayout.db`, and the exact two-backends sprawl "wrap the proven engine" exists to prevent (layout-generator-spike.md §1, gdsfactory/laygo2 entries reached the same verdict about a second geometry engine). |
| Verdict | **Oracle, not runtime.** netgen (+ magic, + the PDK's netgen setup) is an independent, battle-tested implementation of the same compare — ideal as a differential oracle for validating a KLayout-based `klt lvs` (see §3's oracle row), but the wrong thing to make `klt`'s runtime dependency when the already-wrapped KLayout engine does extraction *and* comparison in one pip package the repo already has. |

### PDK-native LVS decks (the gap #54 actually hit)

| Property | Finding |
| -------- | ------- |
| sky130 | The sky130 LVS flow lives in [`fossi-foundation/open-pdks`](https://github.com/fossi-foundation/open-pdks) (Apache-2.0, active — `pushed_at: 2026-07-17`): magic extraction rules + a netgen setup/`.tcl` per the entry above. It is a **magic + netgen** pipeline — no KLayout-native LVS deck — so adopting "the PDK-native deck" as `klt`'s path means adopting the magic+netgen+Tcl stack, not a single deck file. |
| gf180mcu | Verified live: the physical-verification repo [`google/globalfoundries-pdk-libs-gf180mcu_fd_pv`](https://github.com/google/globalfoundries-pdk-libs-gf180mcu_fd_pv) is **archived** (Apache-2.0, last `pushed_at: 2023-06-12`) and ships KLayout decks under `klayout/drc/` **only** — a full recursive tree walk finds `klayout/drc/rule_decks/*.drc` (including `lvs_bjt.drc`, a *DRC*-style BJT-marking deck) but **no standalone KLayout LVS deck**. gf180mcu's real LVS path, like sky130's, is the PDK's **netgen** setup. This is exactly the wall the #54 gf180mcu bring-up hit: `klt drc --deck gf180mcu` exists, its LVS counterpart does not, so LVS ran entirely outside `klt` via the PDK's own native (netgen) deck. |
| What the gap *is* | Both open PDKs route LVS through the same out-of-tool stack (magic-or-KLayout extraction feeding netgen), and `klt` wraps none of it. The friction is not "a solver is missing" — the solvers exist and are proven — it is that there is **no JSON-contracted `klt` verb** over extraction+compare, so every consumer re-drives the PDK's native flow by hand and gets an unstructured, PDK-specific text verdict back. That is the same shape of friction the SPICE corner runner and layout-generator spikes each diagnosed: the reusable, ours-to-own layer is the contract and the orchestration, not the engine. |

### Also considered and set aside

- **magic** (`RTimothyEdwards/magic`) — the extraction half of the open flow's LVS. Genuinely headless and scriptable, but a second geometry/extraction engine alongside `klayout.db` (the two-backends problem again), Tcl-driven, and only *half* of LVS on its own. Relevant as an oracle-side extractor paired with netgen, not as `klt`'s runtime.
- **Proprietary LVS (Calibre nmLVS, PVS, ICV)** — inseparable from proprietary-PDK workflows; out by the open-PDK rule, exactly as HSPICE/Spectre were in the SPICE spike.

### Recommendation

**Wrap KLayout's `LayoutToNetlist` + `NetlistComparer` for both verbs, behind
a contract that does not name them.** The reasoning mirrors the SPICE spike's
"the PDK's native simulator wins by default" and the generator spike's "keep
one geometry backend" almost exactly:

- KLayout is **already** this repo's wrapped, proven, headless, single-pip
  dependency, and — verified live — it does *both* halves this epic needs
  (extraction via `LayoutToNetlist`/`NetlistSpiceWriter`, comparison via
  `NetlistComparer`/`NetlistSpiceReader`) in one package. `klt drc` already
  set the "wrap KLayout's own engine" precedent for the DRC half of physical
  verification; LVS is the same move for the other half.
- netgen is the technically canonical open-flow *comparator*, and the honest
  reason it loses is the same as Xyce's in the SPICE spike: ecosystem cost,
  not capability. Choosing it means adopting magic-for-extraction, a Tcl
  runtime, and a second geometry backend — a larger project than wrapping the
  extractor+comparer the repo already ships.

Because the contract is the API and the engine is an implementation detail,
this is a reversible choice. The request/response shapes in §2 carry an
explicit `engine` selector (`"klayout"` for v1), and netgen-behind-the-same-
contract is the intended second implementation and the differential oracle —
the right first test of whether the contract really is engine-agnostic.

### Invocation strategy (in-process, unlike `klt sim`)

Unlike the SPICE corner runner (subprocess-per-corner, because ngspice can
hang on nonconvergence and must be killable), extraction and netlist
comparison are **in-process `klayout.db` calls**, the same as `klt drc` and
`klt render` already make. There is no external process to time out or kill:
`LayoutToNetlist` and `NetlistComparer` are deterministic graph operations on
in-memory data structures, run in `klt`'s own process against the one wrapped
engine. This matches the `klt gen` spike's in-process posture, not the SPICE
spike's fan-out posture, and needs no per-run timeout/kill machinery.

## 2. Proposed JSON contract

Two verbs, two contracts. Documented in the field-table style already
established for [`klt render`](../cli/render.md) and [`klt drc`](../cli/drc.md)
→ "JSON schema (the contract)." Same house rules apply: **JSON is the API**,
text output is a courtesy, renaming/removing/retyping a field is a breaking
change, new fields may be added, consumers ignore unknown fields, units are
carried in field names (`_um`) the way `klt layers`/`klt drc` carry `dbu_um`.
Both responses conform to the shared envelope in
[docs/json-contract.md](../json-contract.md) (`schema_version` + flat
top-level fields), the convention that postdates the SPICE spike and every
shipped `klt` verb already follows.

These are **proposed** shapes for review, not shipped contracts. No `klt`
subcommand, dependency, or code is added by this spike.

### 2a. `klt extract` — layout → netlist

```
klt extract <file> --deck sky130|gf180mcu [-o|--output <netlist.spice>] [--format text|json]
```

Mirrors `klt drc`'s flag-driven invocation (`<file>` + `--deck` + `--format`):
extraction, like DRC, is a whole-layout operation with a PDK-selected rule
set, not a rich request-JSON operation like `klt sim`/`klt gen`.

#### Request (flags)

| Flag | Type | Description |
| ---- | ---- | ----------- |
| `<file>` | string, required | Path to a GDSII (`.gds`) or OASIS (`.oas`) stream. KLayout auto-detects the format on read (same as `klt drc`); the extension is not authoritative. |
| `--deck` | string, required | Connectivity + device-extraction rule set: `sky130` or `gf180mcu`. Selects which layers connect, which globals (well/substrate) exist, and which device extractors run — the extraction-side analogue of `klt drc`'s rule deck. |
| `--output` / `-o` | string | Path to write the extracted SPICE netlist. Defaults to `<file>.spice` next to the input (the "next to the input" convention `klt render`/`klt sim` already use). |
| `--top` | string | Optional top-cell name when the stream has more than one; defaults to the single top cell (same limitation posture as `klt drc`'s whole-layout note). |
| `--format` | string | `text` (default) or `json` for the report below. The extracted **netlist** always goes to `--output`; `--format` governs only the summary report. |

#### Response (`--format json`)

```json
{
  "schema_version": 1,
  "file": "design.gds",
  "deck": "sky130",
  "top": "ota_5t",
  "dbu_um": 0.001,
  "netlist_path": "design.spice",
  "netlist_sha256": "4f1c…",
  "status": "extracted",
  "device_count": 5,
  "net_count": 7,
  "pin_count": 4,
  "device_counts": { "nfet_01v8": 3, "pfet_01v8": 2 },
  "devices": [
    { "name": "M0", "class": "nfet_01v8", "nets": { "d": "vout", "g": "vin_p", "s": "tail", "b": "vsubs" }, "params": { "w_um": 2.0, "l_um": 0.5 } }
  ],
  "nets": [
    { "name": "vout", "pin": true, "device_count": 3 }
  ],
  "warnings": []
}
```

##### Top-level fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema_version` | integer | Version of this command's JSON shape (starts at `1`; per-command, per `docs/json-contract.md`). |
| `file` | string | The input path exactly as provided (matches `klt drc`'s `file` convention). |
| `deck` | string | Extraction deck used (`"sky130"` / `"gf180mcu"`). |
| `top` | string | Top cell the netlist was extracted from. |
| `dbu_um` | number | Database unit in micrometres, same semantics as `klt layers`/`klt drc`. |
| `netlist_path` | string | Resolved path of the written SPICE netlist (echoes `--output` or the computed default). The netlist is a **file reference, never inlined** — same discipline as `klt sim`'s waveform/log artifacts. |
| `netlist_sha256` | string | SHA-256 of the written netlist — so a stored extract can be checked against the file it produced, and so `klt lvs`/`klt sim` can record *which* extracted netlist they consumed (reproducibility, per the SPICE spike's `environment` block). |
| `status` | string | `"extracted"` on success. A failed run does not emit this envelope (see Exit codes) — the same "never `error` in-band" discipline as `klt drc`'s `status`. |
| `device_count` / `net_count` / `pin_count` | integer | Rollup counts of the extracted netlist. |
| `device_counts` | object\<string,int\> | Per-device-class counts, keys sorted for determinism (same shape as `klt drc`'s `rule_counts`). |
| `devices` | array\<object\> | One entry per extracted device: `name`, `class` (the deck's device-class name), `nets` (terminal → net-name map), `params` (extracted geometry, `_um`-suffixed). |
| `nets` | array\<object\> | One entry per extracted net: `name`, `pin` (whether it surfaces as a top-cell pin/label), `device_count`. |
| `warnings` | array\<string\> | Non-fatal extraction notes (e.g. an unconnected geometry island, a device whose params were clamped). Always present, empty when clean — same "always report the array" discipline as `klt drc`'s `violations` and the SPICE contract's `corners`. |

The `devices[]`/`nets[]` report is a *convenience view* for agents that want
structure without re-parsing SPICE; the **netlist file at `netlist_path` is
the authoritative artifact**, and it is what `klt lvs` and `klt sim` consume.

#### Verified compatible with `klt sim`'s `netlist` convention

This is a hard acceptance bar (Epic #153 success criterion: "`klt extract`
output feeds `klt sim` unmodified"), so it is verified explicitly against
[docs/cli/sim.md](../cli/sim.md) → "Netlist convention: a circuit body, not a
full deck" rather than asserted.

`klt sim` requires its `netlist` to be a **circuit body** — device and
subcircuit definitions (and sources) with **no `.control`/`.end` cards of its
own** — because `klt sim` generates a corner-specific wrapper deck that
`.include`s the file and appends the `.lib`/`.temp`/`.meas`/`.control` cards
itself. A file carrying its own top-level `.end` is explicitly *not*
supported.

`klt extract` therefore emits, and this contract requires, a netlist that is
a circuit body with **no top-level `.control` and no top-level `.end` card**.
Concretely:

- The extracted top cell is written as a `.subckt <top> <pin…> … .ends`
  definition — a self-contained circuit body, not a top-level flat deck with
  a trailing `.end`. `klt`'s wrapper (not `klt extract`) is responsible for
  stripping/omitting the `.end` that KLayout's `NetlistSpiceWriter` would
  otherwise terminate a full deck with; this contract fixes that requirement
  at the `klt extract` boundary so the output is a drop-in `klt sim` `netlist`.
- **The known asymmetry, stated plainly:** an extracted netlist is a *DUT*
  and has **no stimulus** — no supply sources, no input drivers (nothing in a
  layout says "this rail is 1.8 V"). `klt sim`'s convention lists "sources" as
  part of a circuit body because its bandgap/divider examples are
  self-contained testbenches. An extracted `.subckt` is consumed by `klt sim`
  the way any DUT is: a thin testbench `.include`s the extracted file,
  instantiates the `.subckt`, and adds the sources — and *that* testbench is
  the `klt sim` `netlist`. The extracted body satisfies the load-bearing half
  of the convention (**no `.control`/`.end`**, valid to `.include`); the
  stimulus half is the testbench's job, exactly as it is for a schematic-side
  netlist. This is the Phase-4 loop-closure path (`examples/design-pipeline/`
  S9/S11) and it needs no schema change — the extracted `.subckt` and a
  hand/generator-written schematic `.subckt` are interchangeable `.include`
  targets, which is the whole point of matching the convention now.

### 2b. `klt lvs` — extracted vs. reference netlist compare

```
klt lvs <request.json> [--format text|json]
```

Unlike `klt extract`, `klt lvs` takes a **request document** (like `klt sim`
and `klt gen`): it binds two netlist inputs plus matching hints, which is
richer than a flag line carries cleanly.

#### Request

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
  "options": { "keep_extracted": true }
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema` | string | Request contract identifier + major version (same request-side convention as the SPICE and generator spikes; the *response* uses the shared `schema_version` envelope). |
| `engine` | string | Engine selector — `"klayout"` for v1. Present from day one so engine choice is data, not a code path (the reversibility argument in §1). |
| `layout.file` / `layout.deck` / `layout.top` | string | The layout side. Extraction runs inline with the named deck (§2a), i.e. `klt lvs` extracts *then* compares — or `layout` may instead carry `{"netlist": "design.spice"}` to compare a **pre-extracted** netlist (from a prior `klt extract`), so extraction and compare are separable steps that also compose in one call. |
| `reference.netlist` | string | Path to the reference (schematic / golden) SPICE netlist, parsed via `NetlistSpiceReader`. |
| `reference.top` | string | The subcircuit in the reference to compare against `layout.top`. |
| `hints.same_nets` | array\<[string,string]\> | Optional net-equivalence hints for the comparer (e.g. tie an extracted substrate net to the schematic's `GND`) — passed through to `NetlistComparer`'s same-net hinting. |
| `hints.equivalent_pins` | object | Optional per-subcircuit swappable-pin groups (e.g. a symmetric differential input), passed to the comparer's equivalent-pin hinting. |
| `options.keep_extracted` | boolean | When `layout` is a stream, retain the intermediate extracted netlist on disk and reference it from the response (default `false`). |

#### Response

Deliberately mirrors [`klt drc`](../cli/drc.md)'s `status` + `*_count` +
`violations[]` structured-report shape rather than inventing a new vocabulary
(Epic #153 requirement). `klt drc` reports a clean/violations verdict with a
sorted array of structured findings; `klt lvs` reports a match/mismatch
verdict with a sorted array of structured mismatches.

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
  "environment": {
    "engine": "klayout",
    "engine_version": "0.30.10",
    "layout_sha256": "1ab7…",
    "reference_sha256": "c93e…",
    "extracted_netlist": ".klt/lvs/design.spice"
  },
  "mismatches": []
}
```

On a run with findings:

```json
{
  "schema_version": 1,
  "engine": "klayout",
  "layout": "design.gds",
  "reference": "ota_5t.schematic.spice",
  "top": "ota_5t",
  "status": "mismatch",
  "mismatch_count": 2,
  "category_counts": { "net.unmatched": 1, "device.property": 1 },
  "counts": {
    "nets": { "layout": 8, "reference": 7, "matched": 6 },
    "devices": { "layout": 5, "reference": 5, "matched": 4 },
    "pins": { "layout": 4, "reference": 4, "matched": 4 }
  },
  "environment": {
    "engine": "klayout",
    "engine_version": "0.30.10",
    "layout_sha256": "1ab7…",
    "reference_sha256": "c93e…",
    "extracted_netlist": ".klt/lvs/design.spice"
  },
  "mismatches": [
    {
      "category": "net.unmatched",
      "severity": "error",
      "description": "layout net has no reference counterpart",
      "side": "layout",
      "net": { "layout": "vout_split", "reference": null },
      "device": null,
      "property": null
    },
    {
      "category": "device.property",
      "severity": "error",
      "description": "matched device parameter differs beyond tolerance",
      "side": "both",
      "net": null,
      "device": { "layout": "M3", "reference": "M3", "class": "nfet_01v8" },
      "property": { "name": "w_um", "layout": 1.0, "reference": 2.0 }
    }
  ]
}
```

##### Top-level fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema_version` | integer | Version of this command's JSON shape (per-command, per `docs/json-contract.md`). |
| `engine` | string | Echo of the request's `engine`. |
| `layout` | string | Echo of `layout.file` (or `layout.netlist`), as provided. |
| `reference` | string | Echo of `reference.netlist`, as provided. |
| `top` | string | The compared top cell / subcircuit. |
| `status` | string | `"match"` or `"mismatch"` — the LVS analogue of `klt drc`'s `"clean"`/`"violations"`. Never `"error"` in-band; a failed run does not emit this envelope (see Exit codes). |
| `mismatch_count` | integer | `len(mismatches)`. |
| `category_counts` | object\<string,int\> | Per-category mismatch counts, keys sorted for determinism — the LVS analogue of `klt drc`'s `rule_counts`. |
| `counts` | object | Side-by-side `layout`/`reference`/`matched` tallies for `nets`, `devices`, `pins` — the at-a-glance "how close" summary a bare pass/fail can't give. |
| `environment` | object | Reproducibility block (mirrors the SPICE spike's `environment`): engine name/version, SHA-256 of the layout and reference inputs, and the path to the intermediate extracted netlist when retained. So a stored LVS verdict is checkable against the exact inputs that produced it. |
| `mismatches` | array\<object\> | One entry per structured mismatch — see below. Empty on a clean match; always present. |

##### `mismatches[]` entries

Field-for-field the LVS counterpart of `klt drc`'s `violations[]`: a stable
`category` id (never renumbered once shipped, exactly like a DRC rule id), a
human `description`, and the objects involved.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `category` | string | Stable mismatch-category id — proposed set: `net.unmatched`, `net.merged`, `net.split`, `device.unmatched`, `device.class`, `device.property`, `pin.unmatched`, `topology`. Never renumbered/repurposed once shipped (same contract guarantee as a DRC `rule` id). |
| `severity` | string | `"error"` (breaks equivalence) or `"warning"` (e.g. a property difference inside tolerance, or an ambiguity the comparer resolved via a hint). Lets a caller distinguish "not LVS-clean" from "clean but noteworthy." |
| `description` | string | Human-readable explanation of this category of mismatch. |
| `side` | string | `"layout"`, `"reference"`, or `"both"` — which netlist the offending object(s) live on. |
| `net` | object \| null | `{ "layout": <name|null>, "reference": <name|null> }` when a net is involved. |
| `device` | object \| null | `{ "layout": <name|null>, "reference": <name|null>, "class": <string> }` when a device is involved. |
| `property` | object \| null | `{ "name": <string>, "layout": <value>, "reference": <value> }` for a `device.property` mismatch. |

`mismatches` is sorted by `(category, side, device.layout, device.reference,
net.layout, net.reference)` so repeated runs against the same inputs produce
identical, diff-clean output — the same canonical-ordering guarantee `klt drc`
makes about `violations`.

#### Exit codes

Following the trichotomy `klt drc` and `klt sim` both use, and resolving it
against the convention `klt sim` already settled (sim.md → "Exit codes"):

| Code | Meaning |
| ---- | ------- |
| `0` | LVS clean — layout matches reference (`status: "match"`). |
| `1` | Failed to run — bad file, unknown `--deck`, unreadable/unparseable reference netlist, extraction or engine error. |
| `2` | Usage error (missing argument, bad `--format`/request) — from argparse. |
| `3` | Ran successfully; mismatches found (`status: "mismatch"`), the documented payload is on stdout. |

`3` is `klt lvs`'s direct analogue of `klt drc`'s `3` ("ran clean but found
findings") — LVS is a clean/dirty verdict like DRC, so it needs `drc`'s
two-outcome success split, **not** `sim`'s three-outcome `3`/`4` split (which
exists only because a corner sweep has a "broken run" outcome distinct from
"design failed a limit" — a comparer has no such third success state). On
error (exit `1`) a concise message goes to **stderr**, nothing to stdout, no
Python traceback — identical to `klt drc`/`klt sim`.

`klt extract` uses the plain `0`/`1`/`2` set (there is no "ran but found
problems" success outcome for extraction — it either produces a netlist or it
fails), matching `klt gen`'s reasoning for omitting a `3` (layout-generator-
spike.md §2, exit codes).

### Semantics and guarantees

- **`match`/`mismatch` and `error` are always different**, the same
  discipline the SPICE contract draws between `fail` and `error`: a
  `mismatch` means the comparer produced a trustworthy verdict and the
  layout does not match the schematic; an `error` (exit 1, no envelope) means
  no trustworthy verdict exists (extraction failed, reference unparseable).
  An agent must tell "the layout is wrong" from "the run is broken" because
  the corrective action differs.
- **Deterministic, canonical output.** Both `devices[]`/`nets[]` (extract) and
  `mismatches[]` (lvs) are emitted in a fixed sort order, so output is
  byte-stable across runs and platforms given the same inputs and KLayout
  version — the same guarantee `klt drc`'s `violations` and the SPICE
  contract's `corners` make.
- **Netlists are referenced, never inlined.** `klt extract` writes the SPICE
  to `netlist_path`; `klt lvs` references the reference and the intermediate
  extracted netlist by path. The JSON carries structure and verdicts, not
  netlist text — same "artifacts are paths" rule as `klt sim`.
- **Reproducibility is in-band.** `klt extract`'s `netlist_sha256` and
  `klt lvs`'s `environment` hashes let a stored result be re-checked against
  the exact inputs that produced it — a verdict that cannot be re-derived is
  not evidence (the SPICE spike's `environment` rationale, applied here).
- **Additive envelope, same rule as every verb.** New fields may be added
  without a bump; renaming/removing/retyping requires bumping `schema`
  (request) or `schema_version` (response). Category ids and device-class
  names are part of the contract, like DRC rule ids.

## 3. Wrap or build?

[docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "Rewrite rule" permits a rewrite
only when **all three** hold — bottleneck/ceiling, oracle, unlock — and it
names the geometry layer (parsing, polygon ops, hierarchy traversal,
eventually a DRC engine) as a *good* target. Netlist extraction and
graph-isomorphism comparison sit adjacent to that. Scored honestly for the
KLayout `LayoutToNetlist`/`NetlistComparer` engine choice from §1, the same
way the `klt gen` spike scored its hypothetical Rust engine (layout-generator-
spike.md §3):

1. **Bottleneck or ceiling — fails, and, as in the `klt gen` spike, the
   *reason* it fails is the finding.** There is no `klt`-native extraction or
   LVS running in any agent's edit loop today — Phases 2–4 have not started.
   Scoring "is KLayout's extractor the ceiling" before anything exists to be
   a ceiling of would be inventing a number, exactly the failure mode the
   rewrite rule exists to prevent ("sequenced by a decision rule, not by
   ambition"). And what friction *is* recorded on #54 is not the engine being
   slow or capability-limited — it is that **no wrapped verb exists at all**,
   so the work is driven out-of-tool. That is a "build the wrapper," not a
   "rewrite the engine," signal. The honest position: wrap first, run it in
   real block work (the #105 OTA and the canary cells), and revisit this test
   only when there is a friction-log entry pointing at the *engine*.
2. **Oracle exists — holds, and is buildable now.** This is the row that
   passes, and §1 already identified the oracle: **netgen (+ magic, + the
   PDK's own netgen setup)** is a second, independent, battle-tested
   implementation of the same layout-vs-schematic compare. A KLayout-based
   `klt lvs` can be differential-tested against it through the very contract
   in §2 — same verdict on a known-good cell, same *category* of mismatch on
   a deliberately broken one. This is the exact oracle strategy the `klt gen`
   spike found for generators (an independent implementation to diff against),
   and it is the strongest reason a rewrite *could* be made safe later, if the
   other two rows ever flip.
3. **Unlock — fails today.** Nothing this contract requires is structurally
   impossible through the wrapped `LayoutToNetlist`/`NetlistComparer`. The
   honest future counter-case is **incremental, in-edit-loop LVS** — the
   direct analogue of the "incremental DRC inside an agent's edit loop" the
   architecture doc names as a legitimate unlock — where an agent mutates a
   layout and wants an instant "did I just break connectivity" answer without
   a full re-extraction. That would plausibly need engine internals a
   batch-oriented wrapper doesn't expose. But it is a *different capability*
   than batch LVS, unmeasured today, and — like the `klt gen` spike's Rust
   verdict — the leverage move if it ever proves out is to take/extend the
   modern engine, not to reimplement decades of device-recognition and
   graph-matching edge cases from scratch.

One of three (oracle only). **Recommendation: wrap KLayout's
`LayoutToNetlist` + `NetlistComparer`.**

**But "wrap" is the wrong word for the whole answer** — the same substantive
point the SPICE and generator spikes both made. Two different things are in
play:

- **Wrap the engine.** Connectivity/device extraction and graph-isomorphism
  comparison — KLayout's `LayoutToNetlist`/`NetlistComparer`, unmodified,
  called in-process. Device recognition and netlist isomorphism are a poor
  rewrite target for the same reason SPICE device models are: they encode
  years of PDK-specific edge cases, and the outside world already ships proven
  implementations (KLayout's, and netgen's as the oracle).
- **Build the orchestration.** Deck-driven connectivity setup, extracting a
  SPICE **circuit body** shaped to drop into `klt sim` (§2a — the load-bearing
  compatibility work no engine does for us), the structured `mismatches[]`
  report modelled on `klt drc`, the `environment`/hash reproducibility block,
  deterministic canonical ordering, the engine-neutral contract, and the
  `--deck` abstraction spanning sky130 *and* gf180mcu. **No engine ships
  this** — which is precisely why #54's two bring-ups each rebuilt it by hand
  outside `klt`. This layer is ours, first-class, written from scratch.

Reading this as "wrap KLayout" alone would reproduce the friction: a thin
shell that hands back KLayout's native compare result leaves every consumer to
rebuild the contracted part. The engine is a dependency behind the contract;
the extraction+LVS *contract* is the deliverable.

## 4. Resolving the `matched_group_id` open question

[docs/design/layout-generator-spike.md](layout-generator-spike.md) proposed a
`drc_hints.matched_group_id` field on `klt gen`'s output (its §2, `drc_hints`
table) — "an identifier tying together instances that must remain
electrically/geometrically matched (e.g. a common-centroid array's unit
devices) … the hook a future LVS/extraction step (`klt lvs`, Phase 4) can use
to verify matching intent survived downstream edits" — and left, as an
explicit open question (its "Open questions" section), *"How `matched_group_id`
is actually consumed once `klt lvs`/extraction exists."* Resolving it is an
acceptance criterion of this spike. This document resolves it by citation, and
**does not edit** the generator spike.

**Resolution: `klt lvs` (as scoped here — schematic-equivalent LVS) does *not*
read `matched_group_id`, and deliberately so.** The reasoning is a scope
boundary, not an oversight:

- **Topological LVS and matching verification are different checks.**
  `NetlistComparer` (and netgen, and every classical LVS) answers one
  question: is the extracted *connectivity graph* isomorphic to the reference,
  with matching device classes and in-tolerance parameters? That question is
  **purely topological** — it has no notion of, and no need for, "these two
  devices were meant to be a common-centroid pair." A diff pair laid out
  common-centroid and the same diff pair laid out as two plain adjacent
  devices are **LVS-identical**; they differ only in *geometric matching
  quality*, which is exactly what `matched_group_id` encodes and exactly what
  a connectivity comparer neither sees nor should.
- **Therefore `matched_group_id` is out of `klt lvs`'s scope for the same
  structural reason parasitics are** (see "Out of scope" below): it is a
  geometry/matching concern, not a schematic-equivalence concern. Consuming it
  in `klt lvs` would conflate two checks that the industry keeps separate for
  good reason (LVS vs. a distinct matching/symmetry check, sometimes called
  "matching DRC" or a placement-symmetry check).
- **How it *would* be consumed — recorded, not built.** A future
  matching-verification capability (a `klt match`-class check, or a mode of a
  later `klt lvs` epic) would consume `matched_group_id` **from the layout
  side, not the netlist side**: read the hint off the generated GDS (a cell
  property or text label the generator writes alongside the geometry), group
  the devices sharing an id, and check a *geometric* predicate — common
  centroid, orientation match, dummy fencing, symmetric placement about the
  group axis — using `klt`'s existing `klayout.db` geometry access (the same
  backend `klt drc`/`klt render` use). That is a real capability with a real
  home; it is simply **not this epic**, because Epic #153 is scoped to
  schematic-equivalent LVS (its Overview: "schematic-equivalent first").
- **The generator's hook is still correct and still worth keeping.** The field
  remains a valid forward-compatible annotation — a stable, in-band matching
  intent that survives downstream edits and needs no second annotation format
  when the matching check is eventually spiked. This spike confirms the
  *field* is well-placed and resolves only the *consumption* question: it is
  consumed by a future geometric matching check reading the GDS, **not** by
  the topological `klt lvs` this epic delivers.

**Net: deferred, with a stated reason** — schematic-equivalent LVS is
topology; `matched_group_id` is geometry; they are different checks, and only
the topological one is in Epic #153's scope. The field stays as the
generator spike proposed it.

## Out of scope for this spike

No dependency was added to `pyproject.toml`, no `klt` subcommand was added, no
extraction/LVS code was written, and no MCP surface was touched. Phases 2–4 of
Epic #153 carry the build, gated on these findings.

Two capability boundaries are recorded explicitly rather than silently
dropped:

- **Parasitic (RC) extraction is explicitly deferred.** Epic #153's own
  Overview defers it — "schematic-equivalent first; parasitics as a follow-on
  decision recorded in the spike" — and this is that record. Phase 2 targets
  **schematic-equivalent** extraction only: devices and connectivity, no
  parasitic R/C on the interconnect. The `klt extract` contract in §2a is
  built for this — its `devices[]`/`nets[]` report and its `.subckt` output
  carry device parameters and topology, not extracted parasitics. A future
  parasitic mode is an *additive* extension (an `--parasitics` flag, a
  `parasitics` request block, extra elements in the emitted netlist) that does
  not break the schematic-equivalent contract — the same "room to grow without
  breaking" the SPICE spike reserved for Monte-Carlo. KLayout's
  `LayoutToNetlist` does have parasitic-adjacent capability, but committing to
  an RC-extraction contract (coupling model, reduction strategy, accuracy vs.
  runtime) is a decision this spike deliberately does **not** make; it belongs
  to its own follow-on decision once schematic-equivalent extraction is real
  and a friction-log entry demands parasitics.
- **Geometric matching verification is out of scope** (the
  `matched_group_id` boundary, §4): schematic-equivalent LVS is topological;
  checking common-centroid / symmetry / dummy-fencing intent is a distinct
  geometric check for a distinct future epic.

## Open questions for a follow-up epic

- **Where the connectivity/extraction rules live.** `klt drc`'s decks are
  hand-transcribed curated subsets (`src/klayout_tools/decks/`). Extraction
  needs a connectivity + device-extractor definition per PDK — is that a
  parallel curated deck, or is it sourced from the PDK's own KLayout LVS setup
  where one exists? (sky130/gf180mcu both route real LVS through netgen, not a
  KLayout LVS deck — §1 — so a KLayout-native connectivity deck is likely
  ours to curate, like the DRC decks.)
- **Device-parameter tolerance for `device.property` mismatches.** How close
  must an extracted `w_um`/`l_um` be to the reference before it is a match vs.
  a `device.property` mismatch? A per-deck default, a request field, or both —
  and does it interact with `snapped_to_grid` from a generator-produced layout?
- **How the reference netlist's `top`/subcircuit is selected** when the
  reference is a multi-subcircuit deck, and how port/pin correspondence is
  established when schematic and layout name pins differently (beyond the
  `equivalent_pins` hint).
- **Netlist provenance in `klt sim`'s `environment`.** The SPICE spike's own
  open questions flag distinguishing "an extracted netlist" from "a schematic
  netlist" in a corner-sweep's `environment` block — `klt extract`'s
  `netlist_sha256` is the hook, but how `klt sim` records *which side of the
  loop* it verified (schematic vs. extracted) is a Phase-4 loop-closure detail.
- **The geometric matching check** that actually consumes `matched_group_id`
  (§4) — a `klt match`-class capability — is un-spiked; this document only
  fixes the boundary that it, not `klt lvs`, is its consumer.
- **Incremental / in-edit-loop LVS** (§3, unlock row) — if a measured friction
  log ever shows batch re-extraction is the ceiling for an agent's edit loop,
  that is the trigger to re-score the rewrite rule for an incremental engine;
  it is explicitly not scored as present today.
- **Caching.** An extract/LVS result is a pure function of (layout, deck,
  reference, hints) hashes — the same caching opportunity the SPICE spike
  flagged for corners, so a re-run after a one-net fix need not re-extract the
  whole layout.

## Addendum (#216): parasitic (RC) extraction interface decision

**Status:** design decision only. Like the rest of this spike, nothing here
authorises implementation — it resolves the interface/contract questions
this document's own "Out of scope" section deferred ("committing to an
RC-extraction contract... belongs to its own follow-on decision once
schematic-equivalent extraction is real and a friction-log entry demands
parasitics"). #216 is that friction-log entry. Schematic-equivalent `klt
extract`/`klt lvs` are shipped (Epic #153, phases 2–4); this addendum is the
follow-on decision the original text promised, scoped — per #216's own
"Ask" and its curator enhancement — to the decision, not the build. No code
in `src/klayout_tools/` changes as part of this addendum.

### Interface shape: stays inside `klt extract`, behind `--parasitics`

Parasitics extraction is **not** a distinct verb. It stays inside `klt
extract` behind an explicit, opt-in `--parasitics` flag, for the same
reasons `--pdk`/`--pdk-root` (#214) were added as flags rather than a
sibling command:

- A parasitics pass still needs the exact same flattened `LayoutToNetlist`
  connectivity/device extraction `klt extract` already performs (§2a) — a
  separate verb would duplicate `--deck`/`--top`/`--pdk` resolution and the
  device-recognition pass for no benefit, and would risk the two outputs
  (schematic-equivalent vs. parasitic-aware) silently diverging in what they
  consider a "device" or a "net."
- `--parasitics` is additive and off by default: the current fast,
  parasitics-free path (`klt extract`'s existing contract, `docs/cli/
  extract.md`) is completely unaffected when the flag is omitted — matching
  this spike's original framing of a future parasitic mode as "an *additive*
  extension... that does not break the schematic-equivalent contract."
- This mirrors the project's general precedent for optional deeper analysis
  behind a flag rather than a new verb (e.g. `klt drc`'s coverage reporting,
  #193) — reserve a new verb for a capability with a genuinely different
  input/output shape, which parasitics is not: it is still "layout in,
  netlist out."

### Coupling model and reduction strategy: what KLayout offers vs. what we'd build

Verified live against this repo's actual runtime dependency (pip `klayout`
0.30.10, the same package `klt extract`/`klt drc` already use — no second
engine):

- `db.LayoutToNetlist` itself carries **no interconnect-mesh parasitic
  extraction call.** Its full method surface (audited directly via
  `dir(LayoutToNetlist)`) has no `extract_rc`/`extract_parasitics`-shaped
  API — only the connectivity/device extraction (`extract_netlist`,
  `extract_devices`) this spike already scoped for schematic-equivalence.
- KLayout **does** ship `db.DeviceExtractorResistor` /
  `db.DeviceClassResistor` (and `...Capacitor`/`...CapacitorWithBulk`
  counterparts). These recognize an **explicit, drawn resistor or capacitor
  device** — e.g. a PDK's precision poly resistor or MiM cap structure, the
  same device-recognition idiom `klt extract` already uses for
  `DeviceExtractorMOS4Transistor` (`R = L/W * sheet_rho` from the shape
  geometry). This is the "parasitic-adjacent capability" the original
  "Out of scope" section gestured at — but it recognizes a *device the
  designer intentionally drew as a resistor/capacitor*, not the parasitic
  resistance/capacitance of ordinary interconnect wiring. It does not
  generalize to "compute R/C for every net's routing."

  > **Decision, since built (#222).** `DeviceExtractorResistor` /
  > `...WithBulk` is now wired into `klt extract` for **drawn** resistors,
  > as a separate capability from `--parasitics` — confirming the
  > distinction this bullet draws rather than blurring it. The two never
  > interact: a drawn resistor is a *device* (it appears in `device_count`/
  > `devices[]`/`device_counts`, with `params.r_ohm`), while `--parasitics`
  > R/C elements are injected after the schematic-equivalent view is built
  > and appear only in the written SPICE and the `parasitics` block. The
  > per-PDK declaration lives in the same curated-deck table this addendum
  > proposes for the RC coefficients (`ExtractionDeck.resistors`, one
  > `ResistorDevice` per class), and its sheet-rho values come from each
  > PDK's *own* official KLayout LVS deck rather than being curated by
  > hand — a drawn resistor's whole value is that number, so an
  > order-of-magnitude starter value (acceptable for `--parasitics`) is not
  > acceptable here. Capacitor/bipolar recognition remains unbuilt (#219's
  > sibling sub-issues #223/#225).
- There is therefore **no built-in call to wrap** for genuine interconnect
  parasitics; a real implementation needs one of:
  1. **Build a first-order, lumped reduction ourselves** on top of the
     shapes `LayoutToNetlist` already tracks per net (`shapes_of_net`/
     `polygons_of_net`): one equivalent series resistance per net segment
     from a curated per-layer sheet resistance (`rho`), and one lumped
     capacitance-to-ground per net from curated per-layer area/perimeter
     capacitance coefficients — the same "curate what the PDK doesn't hand
     us as a native KLayout deck" pattern already used for the DRC decks
     (`src/klayout_tools/decks/`) and this spike's own connectivity decks.
     Net-to-net coupling capacitance (as opposed to ground capacitance) is
     explicitly **not** part of this first-order model — it requires
     spacing-aware neighbor geometry the lumped-to-ground model does not
     capture, and is a credible second increment once a friction log
     demands it.
  2. **Wrap an external, already-built open-source PEX layer on top of
     KLayout**, if a suitably licensed (MIT/Apache/BSD, matching this
     project's open-PDK-and-open-tooling posture) and headless-scriptable
     one exists — evaluated the same way this spike evaluated `klayout`
     vs. `magic`/`netgen` for LVS (§3), i.e. "wrap the proven engine" when
     one credibly exists rather than building from scratch.
  - **Recommendation:** this addendum does **not** pick between (1) and
    (2) — that survey (candidate engines, license/headless fitness,
    accuracy-vs-effort) is exactly the kind of judgment call #216's own
    "Non-goals" section rules out doing here ("parasitic extraction
    accuracy tuning/calibration... is out of scope"). It is scoped work
    for the follow-on implementation issue, matching how this spike's §3
    itself was the dedicated venue for the LVS engine survey rather than
    folding it into the Overview.

### Accuracy vs. runtime tradeoff: fixed, not tunable, in a first cut

A single, fixed, first-order lumped model (§ above) — no `--parasitics
fast|accurate` mode selector. Exposing an accuracy/runtime knob is only
worth the added contract surface once real friction demands a tradeoff be
made visible to a caller; until then a fixed, documented, conservative
approximation is simpler to reason about and to test. This matches the
project's demand-driven capability-arrival rule (`docs/ARCHITECTURE.md` →
"How capabilities arrive").

### Output format: additive, still one flat SPICE netlist

Confirmed unchanged from this spike's original framing — the follow-on
implementation must keep:

- **One SPICE file**, still a `.SUBCKT ... .ENDS` circuit body with no
  top-level `.control`/`.end` card, still directly consumable by `klt sim`'s
  `netlist` field with no manual reformatting (`docs/cli/sim.md` →
  "Netlist convention"). Parasitic elements are additional `R`/`C`
  primitive instances alongside the existing `M`-card (or `X`-card, when
  `--pdk` model-binding is active) device instances — never a second file
  and never a different netlist shape.
- **`devices[]`/`nets[]` JSON stays additive, not breaking.** The
  currently-documented fields (`docs/cli/extract.md` → "JSON schema") keep
  their exact meaning whether or not `--parasitics` is given. The follow-on
  implementation issue should scope whatever new summary fields it needs
  (e.g. a `parasitics` block with R/C element counts) as new,
  independently-optional additions — never a retype/rename of an existing
  field, per this project's JSON-contract rule
  (`docs/json-contract.md`).

### PDK coverage: sky130 and gf180mcu, same resolution path

No new PDK-selection mechanism. `--parasitics` reuses `klt extract`'s
existing `--deck sky130|gf180mcu` plus its existing optional `--pdk`/
`--pdk-root` resolution (`docs/cli/extract.md` → "PDK resolution"). The
per-layer sheet-resistance and area/perimeter capacitance coefficients the
lumped model needs (see "Coupling model" above) are exactly the kind of
per-PDK numeric table this repo already curates by hand for the DRC decks
and the SPICE model-binding table (#214) — sourced from each PDK's own
public device/process data, never from an NDA'd source, matching
`docs/cli/extract.md` → "SPICE model binding" as the closest existing
precedent for a curated, per-PDK-variant numeric table.

### Follow-on

This addendum recommends eventually building the first-order lumped model
above (§ "Coupling model," option 1, unless the follow-on issue's own
engine survey finds a wrap candidate under option 2 that is clearly
preferable) — per #216's acceptance criteria, that implementation work is
filed as a separate tracking issue, #217, rather than done inline here, the
same way Epic #153's phase-1 spike (#161) preceded its phase-2
implementation issue (#162). See #216 (this decision) and #217
(implementation) for status.

## Addendum (#223): bipolar (BJT) device-class recognition, realized

**Status:** implementation note, not a fresh design decision — this spike's
own survey (§ "KLayout's own `LayoutToNetlist` + `NetlistComparer`" →
"Extraction API" above) already verified `db.DeviceExtractorBJT3Transistor`
present live alongside `DeviceExtractorMOS4Transistor`; #219 (device-class
recognition friction) and its sub-issue #223 are what schedule wiring it in,
not a new engine-choice question. `klt extract` now recognises one curated
vertical-BJT device per deck (sky130's `pnp`, gf180mcu's generic `bjt`) via
that same extractor, mirroring the MOS device-recognition pattern §2a
already established: an optional `ExtractionDeck.bipolars` field (a tuple of
`BipolarDevice` — base/emitter/marker/collector layer roles) feeds
`DeviceExtractorBJT3Transistor`, with the PDK's dedicated device-mark layer
(sky130's `pnp.drawing`, gf180mcu's `DRC_BJT`) scoping recognition to
genuine device-cell instances rather than every drawn well region. See
`docs/cli/extract.md` → "Bipolar (BJT) device recognition" for the shipped
contract (the authoritative source once code and this survey disagree, per
this document's own opening note) and
`src/klayout_tools/decks/__init__.py`'s `BipolarDevice` docstring for the
layer-role contract. Resistor and capacitor device classes (#219's
remaining sibling sub-issues, e.g. #222 for resistors) are unaffected by
this addendum and remain separately tracked.

## Addendum (#225): MiM-capacitor device-class recognition, realized

**Status:** implementation note, not a fresh design decision -- the
"Coupling model" section above already identified `db.DeviceExtractorCapacitor`
/ `...CapacitorWithBulk` live alongside `DeviceExtractorResistor` and
`DeviceExtractorMOS4Transistor`, and already drew the line this
implementation confirms: it recognizes a *device the designer intentionally
drew as a capacitor* (a MiM stack), not the parasitic capacitance of
ordinary interconnect wiring -- the two never interact (a drawn capacitor
appears in `device_count`/`devices[]`/`device_counts` with `params.c_f`;
`--parasitics`' injected `C` elements are unnamed, model-token-free cards in
the written SPICE and the separate `parasitics` block only, per the #216
addendum above).

`klt extract` now recognises one or more curated MiM-capacitor devices per
deck (gf180mcu's one `cap_mim_2f0_m4m5_noshield`, sky130's two --
`sky130_fd_pr__model__cap_mim`/`_m4`, one per metal level its official LVS
deck draws a purpose-built top-plate mark layer on) via that extractor,
mirroring the bipolar device-recognition pattern the #223 addendum above
established: an optional `ExtractionDeck.capacitors` field (a tuple of
`CapacitorDevice` -- top-plate/bottom-plate layer roles, each independently
narrowable by `requires`/`excludes`, plus an optional "virtual bottom
plate" oversize derivation) feeds `DeviceExtractorCapacitor`.

One structural difference from both `BipolarDevice` and the (still
unmerged, #222) resistor pattern: a MiM cap's two plates are **two
independent layers**, not one deck conductor narrowed by a marker -- so
unlike a bipolar entry's `base`/`emitter` (which reuse the deck's own
`nwell`/`active` MOS-recognition layers) or a resistor's `body` (which must
be one of the deck's own `metals`), a `CapacitorDevice`'s `top_plate`/
`bottom_plate` are **not** required to be part of `ExtractionDeck.metals` --
both curated decks' tracked metal stack stops at metal1, while a MiM cap's
plates live on a higher metal level (gf180mcu's Metal4/FuseTop, sky130's
met3-met4/capm/capm2) neither deck otherwise tracks. The two plate regions
are therefore recognised as their own new, self-connected connectivity
nodes: the *device* (a capacitor of the correct value between two
correctly-shaped plates) is still correctly recognised, but its two
terminal nets do not extend into whatever real upper-metal routing a full
metal-stack extraction would connect them to -- a documented, narrower-scope
approximation than the bipolar/MOS terminals' well-tie handling, called out
explicitly in `CapacitorDevice`'s own docstring rather than glossed over.

See `docs/cli/extract.md` -> "MiM capacitor device recognition" for the
shipped contract (the authoritative source once code and this survey
disagree, per this document's own opening note) and
`src/klayout_tools/decks/__init__.py`'s `CapacitorDevice` docstring for the
layer-role contract. The resistor device class (#219's one remaining
sibling sub-issue, #222) is unaffected by this addendum and remains
separately tracked.

## Addendum (2026-08-02, #343): netgen realized as the intended second engine

**Status:** implementation note, closing the loop this spike's §1 opened.
§1's "netgen (contrast candidate)" survey reached "Oracle, not runtime" --
netgen has no layout front-end, so wiring it up meant wiring up `magic` too,
the "two-backends sprawl" `klt sim`'s "wrap the proven engine" precedent
exists to prevent -- and the Recommendation section named
"netgen-behind-the-same-contract" as "the intended second implementation
and the differential oracle." Issue #343 built exactly that: `klt lvs`'s
`engine` selector now accepts `"netgen"` alongside `"klayout"`, invoked
headlessly (`netgen -batch lvs`) in **netlist-vs-netlist mode only** against
the same layout/reference SPICE netlists the `"klayout"` engine already
resolves -- no `magic` extraction backend. See `docs/cli/lvs.md` -> "Engine"
for the shipped contract; this addendum records what the implementation
found, which the acceptance criteria asked to be written up separately from
the contract itself.

### netgen was built from source for this issue, not just read about

No `netgen` binary or packaging (Homebrew, conda, apt) was available in the
sandbox this issue was implemented in, and the `RTimothyEdwards/netgen`
GitHub repository's Tcl/Tk-based build has a real portability trap worth
recording: it links against `Tcl_Main`/`Tcl_SetVar`/`Tcl_StaticPackage`,
none of which exist in Tcl/Tk **9.x** (removed as part of Tcl 9's deprecated
string-API cleanup) -- `./configure --with-tcl=<tcl 9 prefix>` succeeds, but
`make` fails at the final `netgenexec` link step with "symbol(s) not found
for architecture". **netgen (as of 1.5.323, this issue's survey date)
requires Tcl/Tk 8.6**, not 9 -- `brew install tcl-tk@8` (or the equivalent
on another platform) then `./configure --with-tcl=<tcl 8.6 prefix>
--with-tk=<tcl 8.6 prefix>` builds and installs cleanly. A CI image that
provisions `netgen` for this engine (e.g. to validate #343's own tests
against the real binary, not just the stubbed-subprocess unit tests) needs
Tcl/Tk 8.6 explicitly, not whatever "tcl" resolves to on a modern base
image.

### The `netgen::lvs` Tcl proc's invocation syntax (verified from source)

`netgen -batch lvs <name1> <name2> [setupfile] [logfile]` is not a
`netgen`-native CLI flag set -- `-batch` only strips itself and sets a Tcl
`batchmode` flag; the remaining argv is `eval`-ed as a Tcl command in the
`netgen` namespace (`tcltk/netgen.tcl.in`), where `lvs` is a proc
(`netgen::lvs`, same file) that expects `name1`/`name2` as either a bare
filename (cell name == file basename) or a **single argv token containing a
space**, `"<file> <cell>"`, which Tcl's list semantics re-split into the
2-element form the proc parses. This is exactly what
`subprocess.run([..., f"{layout_path} {layout_circuit.name}", ...])`
produces (each Python list element is one argv token, unsplit by a shell) --
verified by invoking the real binary this way and reading a produced
`comp.out` report back. An empty string (`""`) for `setupfile` is netgen's
own documented "no setup file" path (`netgen::lvs`'s own log line: "No
setup file specified. Using trivial default setup."), not an
undocumented convention this implementation invented.

### netgen's exit code is never trustworthy alone (confirmed, matching the ngspice precedent)

`netgen -batch lvs` exits `0` for a clean match, a topology mismatch, a
pin-count mismatch, *and* for at least one failure mode short of a
completed compare (a missing input netlist file -- netgen prints a Tcl
traceback to stdout and returns from the `lvs` proc without ever writing a
report file, but the process itself still exits `0`). This is the same
"never trust a subprocess's raw exit code" precedent `sim.py`'s own
docstring establishes for `ngspice`, now confirmed for netgen too: this
issue's implementation (`lvs._run_netgen_lvs`) treats "no report file was
written at all" as its own distinct failure (a clear `LvsError` quoting
netgen's stdout, since netgen's errors go there, not to the log file), and
otherwise defers entirely to the log file's own `"Final result:"` text --
never the process return code.

### Report-format stability: verified against four real scenarios, not guessed

Rather than infer netgen's `comp.out` format from documentation or hearsay,
this issue captured the actual, byte-for-byte report text from a
from-source netgen 1.5.323 build for four hand-built SPICE-netlist
scenarios (a clean match; a single device parameter changed with
connectivity untouched; a shorted/split net; an extra device changing the
port list) and grepped the C/Tcl source (`base/netcmp.c`,
`tcltk/tclnetgen.c`, `tcltk/netgen.tcl.in`) for every `Fprintf`/`Printf`
call that could produce the terminal verdict line, to confirm the four
observed strings are the complete, current set rather than a lucky sample:

- `"Final result: Circuits match uniquely."` -- unique topological match
  (optionally followed by `"Property errors were found."` when a matched
  device pair's parameter differs by more than netgen's own configured
  cutoff -- `klt lvs` downgrades this to `status: "mismatch"` with a
  `device.property` entry, since a real parameter defect must never read as
  a clean match, exactly as issue #282 already established for the
  `"klayout"` engine's own minimal-cell degradation).
- `"Final result: Netlists do not match."` (also seen as `"Circuits do not
  match."` in `netcmp.c`'s non-Tcl code path, not reachable via
  `netgen::lvs`'s own Tcl invocation but included in the parser's
  substring match defensively) -- topology mismatch, with a fixed-width,
  pipe-delimited `NET mismatches:`/`DEVICE mismatches:` side-by-side report
  preceding it.
- `"Final result: Top level cell failed pin matching."` -- a port-list
  mismatch (`netgen::lvs`'s own pre-`verify` short-circuit, not a
  `verify`-command message at all).
- `"Final result: Subcell(s) failed matching."` -- a black-boxed
  subcircuit mismatch (the sibling short-circuit; not exercised against the
  real binary for this issue, since provoking it needs a deliberately
  unmatchable nested subcircuit, but confirmed present verbatim in
  `tcltk/netgen.tcl.in`).
- `"Circuits match uniquely with port errors."` (`tclnetgen.c`'s
  `_netcmp_verify`) -- topologically unique but with a port disagreement;
  present in the source for completeness but not confirmed reachable from
  `netgen::lvs`'s own call sequence (its pin-matching step forces the more
  specific `"Top level cell failed pin matching."` path first in every
  scenario this issue could construct) -- `klt lvs`'s parser still handles
  it defensively (never as a clean match) in case a future netgen version's
  proc reaches it.

The `NET mismatches:`/`DEVICE mismatches:` side-by-side tables are real and
stable in shape (fixed-width columns, a `"(no matching net)"`/`"(no
matching instance)"` filler for a one-sided row, a full-width `---` rule
opening and closing the table), but this issue deliberately does **not**
parse them into per-net/per-device `mismatches[]` entries the way the
`"klayout"` engine's own event stream does -- trusting exact column
alignment across netgen versions/platforms is not a documented, versioned
part of netgen's report contract the way the verdict lines above are (grep
hits in the source, not just observed output). `klt lvs` instead buckets
each whole section into one generic `net.unmatched`/`device.unmatched`
entry with the raw block preserved in `details.raw` (see `docs/cli/lvs.md`)
-- an explicit, documented precision gap rather than a per-net claim that
could silently be wrong. The one report fragment this issue *does* parse
precisely is the parameter-difference block (`"<class>:<idx> vs.
<class>:<idx>:"` followed by indented `"<PARAM> circuit1: <v1>
circuit2: <v2> (delta=<pct>%, cutoff=<pct>%)"` lines, from
`netcmp.c`'s `PrintPropertyResults`) -- a fixed, `printf`-templated format
that maps directly onto `device.property`'s `property: {name, layout,
reference}` shape with no ambiguity.

### Contract gap: `counts.*.matched`/`net_correspondence` do not generalize

The `"klayout"` engine's `counts.nets/devices/pins.matched` and
`net_correspondence[]` are built directly from `NetlistComparer`'s own
per-pairing callbacks (`match_nets`, `match_devices`, ...) -- a `netgen`
subprocess exposes no equivalent structured event stream, only the text
report above. Reconstructing an equivalent per-item correspondence from
netgen's fixed-width tables would mean trusting exactly the column
alignment the section above declined to trust for `mismatches[]` -- so
`klt lvs` does not attempt it for `"engine": "netgen"`: `matched` is exact
on a `"match"` verdict (guaranteed by construction -- a unique match
requires equal net/device/pin cardinality on both sides) and `0` on a
`"mismatch"` verdict (the conservative floor, never a fabricated estimate);
`net_correspondence` is always `[]`. This is a genuine, documented contract
gap between the two engines (see `docs/cli/lvs.md` -> "Engine" ->
`"netgen"`), not an oversight -- closing it would require either trusting
netgen's table alignment or a second, netgen-specific compare pass this
issue's netlist-vs-netlist scope does not otherwise need.

### Schema/contract verdict: no `schema_version` bump

Per `docs/json-contract.md` ("adding a field is not [a breaking change]"),
this issue's two payload changes are both additive: `"netgen"` as a second
accepted `engine` value (an enum-like value addition, the same category the
contract's own "Pre-1.0 caveat" section already covers for a new
`mismatches[].category`) and the new `mismatches[].details` field (present,
`null`-valued, on every existing `"klayout"`-engine entry too -- no field
was renamed, removed, or retyped). `klt lvs`'s `SCHEMA_VERSION` stays `1`.

### Validation: categorically consistent verdicts on the real sky130 corpus fixture

Both engines were run (with a real, from-source netgen 1.5.323 binary, not
just the stubbed-subprocess unit tests) against `tests/corpus/sky130/
sky130_fd_sc_hd__inv_1.gds` -- the same real corpus cell
`tests/test_lvs.py`'s own `test_corpus_known_good_cell_matches_cleanly`
uses -- through both `klt lvs`'s inline-extraction path (`layout.file` +
`layout.deck: "sky130"`) and its negative-control counterpart (the same
curator NMOS-body short, `" VGND vsubs"` -> `" VGND VGND"`, `test_lvs.py`'s
own corpus round-trip tier applies):

- **Known-good self-compare**: both engines report `status: "match"`.
- **Deliberately-shorted reference**: both engines report `status:
  "mismatch"`, and both surface a connectivity finding on the shorted net
  (`"klayout"`: `device.unmatched`/`net.unmatched`/`topology`; `"netgen"`:
  `net.unmatched`, its coarser section-bucketed granularity -- see the
  report-format section above).

Both engines additionally agree on the deck-structural
`device.body_unverified` warning in both cases (issue #281 -- unaffected by
the engine choice, since it is derived from the layout-side deck, not the
compare engine). Categorically consistent per this issue's acceptance
criteria, without either engine's exact `mismatches[]` shape needing to be
identical -- the `"netgen"` engine's coarser bucketing on a topology-level
defect is the one documented, expected divergence (see the contract-gap
section above).
