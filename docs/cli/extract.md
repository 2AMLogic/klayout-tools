# `klt extract`

Extract a **schematic-equivalent** netlist (devices + connectivity) from a
GDSII or OASIS layout stream, headless, and write it as a SPICE circuit body
plus a structured summary. An opt-in `--parasitics` flag additionally extracts
first-order lumped RC interconnect parasitics (see "Parasitic (RC)
extraction" below).

```
klt extract <file> --deck sky130|gf180mcu [-o|--output <netlist.spice>] [--top <cell>] [--pdk <variant>] [--pdk-root <root>] [--parasitics] [--format text|json]
```

This is phase 2 of Epic #153 (`klt lvs`/`klt extract`), the build carried by
the accepted spike,
[`docs/design/lvs-extraction-spike.md`](../design/lvs-extraction-spike.md)
(section 2a) — read it first for the engine survey and the reasoning behind
the contract shape below. This document is the shipped contract; where the
two disagree, this document (and the code) win.

- `<file>` — path to a GDSII (`.gds`) or OASIS (`.oas`) file. KLayout
  auto-detects the stream format on read (same as `klt drc`); the extension
  is not authoritative.
- `--deck` — required. The connectivity + device-extraction deck to run.
  Currently: `sky130`, `gf180mcu`.
- `--output` / `-o` — path to write the extracted SPICE netlist. Defaults to
  `<file>` with its extension replaced by `.spice`, next to the input (the
  "next to the input" convention `klt render`/`klt sim` already use).
- `--top` — top cell to extract when the stream has more than one (required
  in that case; optional otherwise, and must name the sole top cell if
  given).
- `--pdk` / `--pdk-root` — optional. See "PDK resolution" below.
- `--parasitics` — optional, off by default. Additionally extract first-order
  lumped RC interconnect parasitics (one series R + one ground C per net) as
  extra `R`/`C` cards in the written netlist and a `parasitics` block in the
  JSON. When omitted, the output is byte-identical to a schematic-equivalent
  extraction. See "Parasitic (RC) extraction" below.
- `--format` — `text` (default, a human-readable summary) or `json`. The
  extracted **netlist** always goes to `--output`; `--format` governs only
  the summary report.

## Engine

`klt extract` runs fully headless via the pip `klayout` package's native
`klayout.db.LayoutToNetlist` (connectivity + device extraction) and
`klayout.db.NetlistSpiceWriter` (SPICE serialisation) — the same wrapped
dependency `klt drc` and `klt render` already use, verified live in the phase
1 spike. There is no dependency on the standalone `klayout` application
binary or any second geometry engine — only `pip install klayout` (already
this repo's sole runtime dependency).

Extraction is **flat**, not hierarchical: every deck layer is a single
flattened `Region`/`Texts` collection over the selected top cell (via
`Cell.begin_shapes_rec`), the same whole-layout flattening idiom `klt drc`
uses. Device recognition splits NMOS (`active - nwell`) from PMOS
(`active & nwell`) and runs KLayout's native `DeviceExtractorMOS4Transistor`
for each — one generic `nfet`/`pfet` device class per deck (no
voltage-flavor distinction). A deck may additionally declare one or more
vertical-BJT device-recognition entries, run through KLayout's native
`DeviceExtractorBJT3Transistor` — see "Bipolar (BJT) device recognition"
below — and one or more drawn MiM-capacitor device-recognition entries, run
through KLayout's native `DeviceExtractorCapacitor` — see "MiM capacitor
device recognition" below.

## Deviation from the spike

The spike's proposed invocation is flag-only (`klt extract <file> --deck
sky130|gf180mcu`), with no PDK-resolver involvement. This command keeps
`--deck` as the required selector of the curated deck (self-contained,
exactly like `klt drc`'s decks — no PDK install is required to run it), and
additionally accepts optional `--pdk`/`--pdk-root` flags resolved through
the one shared resolver every other PDK-aware verb uses (`klt pdk find`'s
resolver, [`docs/cli/pdk.md`](pdk.md)), mirroring `klt sim`'s optional
`models.pdk`/`models.pdk_root` resolution. See "PDK resolution" below.

## Coverage

The `sky130` and `gf180mcu` decks are **curated starter subsets**, the
extraction analogue of `klt drc`'s curated rule decks (see
[`docs/cli/drc.md`](drc.md) → "Coverage"): a two-terminal-well CMOS stack
(one drawn well layer splitting NMOS/PMOS, contact/local-interconnect up
through one or two metal levels), not a full PDK's device zoo. Both decks
are defined in `src/klayout_tools/decks/sky130.py` /
`src/klayout_tools/decks/gf180mcu.py` as an `ExtractionDeck` (layer roles:
`active`, `poly`, `nwell`, optional `tap`, `contact`, an ordered `metals`
stack with matching `metal_labels`/`vias`, and an optional `well_label`) —
each field's exact layer numbers and provenance are documented in the deck
module's own docstring, verified against this repo's real corpus fixtures
(`tests/corpus/sky130/`, `tests/corpus/gf180mcu/`).

Two known connectivity-fidelity limitations, both documented in the deck
modules and deliberate (not oversights):

- **NMOS body.** Neither curated deck draws a separate substrate/pwell
  layer, so there is no drawn tap geometry to derive a real net name from.
  The NMOS body terminal is tied to a global net (`vsubs` by default) via
  KLayout's `connect_global` instead of a real substrate-tap extraction.
- **PMOS body (gf180mcu only).** sky130's curated deck draws well taps on a
  *distinct* layer from transistor active (`tap.drawing`), so the well body
  net picks up its real name via that tap + an `nwell` pin label (verified
  against the sky130 corpus: the PMOS body of a real inverter cell extracts
  to the correct `VPB` pin). gf180mcu's curated layer set has no distinct
  tap layer (`Comp` is shared with the transistor active layer) and no
  well-label layer, so its PMOS body terminal is a floating, anonymous net.

Connecting a well region to *every* contact inside it (rather than only a
genuinely distinct tap region) is deliberately **not** done — the well is a
background region spanning the whole PMOS area, so a blanket rule like that
shorts every transistor terminal inside the well together. See
`ExtractionDeck`'s docstring in `src/klayout_tools/decks/__init__.py` for
the full reasoning.

### Bipolar (BJT) device recognition

Both curated decks additionally declare one vertical-BJT device-recognition
entry (`ExtractionDeck.bipolars`, a tuple of `BipolarDevice` — see its
docstring in `src/klayout_tools/decks/__init__.py`), run through KLayout's
native `DeviceExtractorBJT3Transistor`:

- **sky130**: one `pnp` entry — base = `nwell.drawing` (64/20), emitter =
  `diff.drawing` (65/20), marker = `pnp.drawing` (82/44, the layer
  `sky130_fd_pr__pnp_05v5`-style device-cell instances draw over themselves
  for recognition).
- **gf180mcu**: one generic `bjt` entry (the DRM's `DRC_BJT` mark layer
  covers both NPN and PNP polarities with no single named device cell to
  attribute one to, unlike sky130's `pnp_05v5`) — base = `Nwell` (21/0),
  emitter = `Comp` (22/0), marker = `DRC_BJT` (127/5).

Both entries reuse the deck's own MOS-recognition `nwell`/`active` layers for
`base`/`emitter` rather than introducing dedicated bipolar-only masks — the
`marker` layer is what scopes recognition to genuine device-cell instances:
`extract.py` intersects `base` with `marker` before extraction, so an
ordinary PMOS-only `nwell` drawn elsewhere in the layout is never
misrecognised as a bipolar base. Neither curated deck draws a distinct
collector layer (the vertical bipolar's collector is the substrate itself),
so the collector terminal is tied to the deck's global substrate net
(`vsubs` by default), mirroring the NMOS-body wiring above.

`devices[].nets` for a bipolar device uses `"c"`/`"b"`/`"e"` terminal keys
(collector/base/emitter) instead of MOS's `"s"`/`"g"`/`"d"`/`"b"`, and
`devices[].params` is empty (KLayout's `DeviceClassBJT3Transistor` reports
area/perimeter parameters named `AE`/`AB`/`AC`/`PE`/`PB`/`PC`/`NE`, none of
which match the `W`/`L` parameter names `devices[].params` extracts — see
"JSON response" below).

### MiM capacitor device recognition

Both curated decks additionally declare one or more drawn MiM
(Metal-Insulator-Metal) capacitor device-recognition entries
(`ExtractionDeck.capacitors`, a tuple of `CapacitorDevice` — see its
docstring in `src/klayout_tools/decks/__init__.py`), run through KLayout's
native `DeviceExtractorCapacitor`. A MiM cap is two conductor plates
separated by a thin dielectric, drawn as **two independent layers** — a
purpose-drawn top plate and a bottom plate on an ordinary conductor — rather
than one marked-up conductor the way a drawn resistor or the bipolar entries
above reuse existing layers:

| Deck | `devices[].class` | Top plate | Bottom plate | Capacitance density |
| ---- | ------------------ | --------- | ------------ | -------------------- |
| `gf180mcu` | `cap_mim_2f0_m4m5_noshield` | `FuseTop` (75/0), requires `CAP_MK` (117/5) + `MIM_L_MK` (117/10), excludes `efuse_mk`/`plfuse` | "Virtual bottom plate": `Metal4` (46/0) clipped to `FuseTop` sized (oversized) by 1.06µm | **2.0 fF/µm²** |
| `sky130` | `sky130_fd_pr__model__cap_mim` | `capm.drawing` (89/44) | `met3.drawing` (70/20), unfiltered | **2.0 fF/µm²** |
| `sky130` | `sky130_fd_pr__model__cap_mim_m4` | `capm2.drawing` (97/44) | `met4.drawing` (71/20), unfiltered | **2.0 fF/µm²** |

KLayout computes `C = A * area_cap` from the two plates' actual geometric
overlap area, so the capacitance-density number *is* the accuracy of the
extracted value — the capacitor analogue of a drawn resistor's sheet
resistance. Each is transcribed from that PDK's own official KLayout **LVS**
deck (a different source than the DRC rule tables the rest of each deck
module cites — sky130's `sky130.lvs`, gf180mcu's `mimcap_derivations.lvs` /
`mimcap_extraction.lvs`) and cross-checked against independent sources in
the same PDK install — see the per-deck module docstrings
(`src/klayout_tools/decks/sky130.py`, `.../gf180mcu.py`) for the exact
source files, the derivation each is transcribed from, and every
approximation taken relative to it. gf180mcu's stack additionally needs a
"virtual bottom plate" derivation (its bottom plate is ordinary `Metal4`
routing, not a purpose-drawn cap layer the way sky130's `capm`/`capm2`
are) — clipping the bottom conductor to the sized top-plate outline so an
unrelated `Metal4` trace 1.06µm away from a real MiM cap is not swept in.

Two consequences worth knowing:

- **Unmarked conductor is never reclassified.** An ordinary
  `Metal4`-`Metal3` (or, on sky130, plain `met3`/`met4`) overlap with no
  `MIM_L_MK`/`capm`/`capm2` marker drawn stays ordinary connectivity, never
  a capacitor — mirroring the "unmarked conductor is never reclassified"
  guarantee drawn resistors give.
- **Capacitor plate nets are not wired into the rest of this deck's metal
  stack.** Neither deck's `metals` field (see "Coverage" above) reaches as
  high as a MiM cap's plate layers (both stop at metal1); a recognised
  capacitor's two terminal nets are therefore only as large as the plate
  shapes `klt extract` itself recognises — multiple plate polygons that
  touch merge into one net, but neither plate's net extends into whatever
  real upper-metal routing a full metal-stack extraction would connect it
  to. The *device* (a capacitor of the correct value between two
  correctly-shaped plates) is still correctly recognised; only the net
  names/connectivity of its two terminals carries this documented
  approximation. A layout with no MiM-cap marker anywhere extracts
  bit-for-bit as it did before this feature existed — the capacitor
  extractor is never even invoked for an entry whose plate regions come out
  empty.
- **gf180mcu's stack models only the default of three selectable
  densities.** The DRM/LVS deck's MiM cap supports 1.0/1.5/2.0 fF/µm²
  dielectric-thickness options selected as a foundry-side runset option, not
  something a drawn layout's own geometry can distinguish; this deck models
  only the LVS runset's own default (2.0 fF/µm²) — see
  `decks/gf180mcu.py`'s provenance note.

**sky130 gap**: neither MoM (Metal-on-Metal, interdigitated-finger)
capacitors nor any voltage-flavor/size variant beyond the two curated
`capm`/`capm2` stacks above are modelled — out of scope for this curated
starter subset, not a silent omission.

## PDK resolution

`--pdk`/`--pdk-root` are **optional** and resolved through
`klayout_tools.pdk.find_pdk` — the same resolver `klt pdk find`/`klt pdk
env` use ([`docs/cli/pdk.md`](pdk.md)):

- Omit both — the default — and extraction runs entirely from the curated
  `--deck` table; no PDK install is touched or required. This matches `klt
  drc`'s CI posture: both commands are runnable with nothing installed
  beyond `pip install klayout`.
- Give either — an explicit `--pdk <variant>` and/or `--pdk-root <root>` —
  and the PDK is resolved before extraction runs; an unresolvable PDK
  (nothing found, or the named variant is absent) is an application error
  (exit 1), and the resolved `variant`/`root`/`version` are echoed in the
  response's `pdk` field for provenance.

### SPICE model binding (`--pdk` given + resolvable)

Before this behavior existed, `--pdk`/`--pdk-root` only affected the JSON
response's `pdk` provenance field — the *written netlist* was identical
either way, using the curated deck's bare device-class label
(`nfet`/`pfet`) as an `M`-card model name:

```
M$1 Y A VGND vsubs nfet L=0.15U W=0.65U AS=0.234P AD=0.234P PS=1.6U PD=1.6U
```

`nfet` is the deck's own class label, not a model any real PDK ships —
sky130 and gf180mcu both ship their primitive MOS device as a SPICE
`.subckt` (taking `d g s b` terminals plus `l`/`w` geometry), never a
built-in `nmos`/`pmos` model — so this `M` card cannot bind a real PDK model
library at all.

**When a PDK resolves now**, each extracted MOS device is written as an `X`
subcircuit call against the resolved PDK's real device library instead:

```
X$1 Y A VGND vsubs sky130_fd_pr__nfet_01v8 L=0.15U W=0.65U
```

The device is bound via a small curated
`(deck_name, pdk_variant_family) -> {"nfet": <subckt>, "pfet": <subckt>}`
table (`src/klayout_tools/pdk_models.py`; see that module's docstring for
the exact provenance of each bound subcircuit name, verified against a real
fetched PDK install rather than assumed) and a
`kdb.NetlistSpiceWriterDelegate` subclass that overrides KLayout's default
`M`-card device writer only for classes present in the resolved table.

**Scope limits** (deliberately narrower than the general PDK-device-metadata
resolver `docs/design/pdk-device-corner-metadata-spike.md` proposes as a
future epic):

- **MOS family only**, one voltage flavor per PDK family — the only flavor
  the curated extraction decks distinguish (see this module's own docstring):
  sky130's `01v8` core devices (`sky130_fd_pr__nfet_01v8` /
  `sky130_fd_pr__pfet_01v8`) and gf180mcu's `03v3` core devices (`nfet_03v3`
  / `pfet_03v3` — gf180mcu has no `gf180mcu_fd_pr__`-prefixed naming
  convention the way sky130 does).
- **Two curated decks only** (`sky130`, `gf180mcu`); a resolved PDK whose
  family has no curated table entry for the requesting `--deck` (e.g. the
  `sky130` deck against a resolved `gf180mcuA` install, or a variant name
  matching no known PDK family at all) is an application error (exit 1)
  naming what was tried — **never** a silent fallback to the bare `M`-card
  form.
- The written `X` card carries only `L`/`W` (both with an explicit
  micrometre unit suffix, e.g. `L=0.15U` — the same convention `klt
  extract`'s `M`-card form already uses, unambiguous regardless of any
  `.option scale` a caller's testbench may or may not set) and relies on
  the resolved subcircuit's own defaults for everything else (`nf`/`mult`/
  `par`, all confirmed `1`-equivalent in the fetched real installs this
  table was verified against — this deck's device extractor never models
  multi-finger/multiplied devices either). Source/drain area+perimeter
  (`AS`/`AD`/`PS`/`PD`, present on the bare `M`-card form) are **not**
  carried onto the `X` card — consistent with this command's documented
  schematic-equivalent, no-parasitics scope (see "Out of scope" below).
- **The JSON response is unaffected**: `device_counts`/`devices[].class`
  always report the deck's own class label (`nfet`/`pfet`), regardless of
  `--pdk`. Model binding is a SPICE-serialization concern only.

## Parasitic (RC) extraction (`--parasitics`)

`--parasitics` is an **opt-in, additive** mode implementing the interface
decision recorded in
[`docs/design/lvs-extraction-spike.md`](../design/lvs-extraction-spike.md) →
"Addendum (#216): parasitic (RC) extraction interface decision" (the
implementation is issue #217). When the flag is omitted the parasitics path is
skipped entirely and the written SPICE and JSON are byte-identical to a
schematic-equivalent extraction.

### Engine: a first-order lumped reduction (not a wrapped PEX engine)

The addendum deferred the engine choice to this implementation. KLayout's pip
`db` module — this repo's sole runtime dependency — has **no built-in
interconnect-mesh RC extraction call**: its `DeviceExtractorResistor` /
`...Capacitor` recognize *explicitly drawn* R/C devices (a precision poly
resistor, a MiM cap), not the parasitic R/C of ordinary wiring. Surveying the
open-source alternative (wrapping an external PEX layer) found no
suitably-licensed (MIT/Apache/BSD), headless, KLayout-scriptable
interconnect-PEX engine that works from a GDS/OASIS stream without pulling in a
**second geometry backend** and a non-pip toolchain — the same "two backends"
cost the spike's §1/§3 rejected for the LVS engine choice. So, matching the
addendum's lean and the DRC-deck curation pattern, `--parasitics` builds a
**first-order, lumped reduction** on top of the per-net/per-layer geometry
KLayout's `LayoutToNetlist` already tracks (`polygons_of_net`), using curated
per-PDK sheet-resistance / area-perimeter-capacitance coefficient tables.

### The model

For each net (except the deck's substrate/ground net, and nets with no
eligible interconnect geometry) the pass emits a single lumped-RC **Γ-section**:

```
net --R--> net__par --C--> <substrate_net>
```

- **C to ground** (femtofarads) = Σ over conductor roles of
  `area_um2 * cap_area + perimeter_um * cap_perim`, the net's lumped ground
  capacitance. The reference node is the deck's `substrate_net` (`vsubs`),
  which a `klt sim` testbench ties to the AC ground.
- **series R** (ohms) = Σ over conductor roles of `sheet_res * n_squares`,
  where `n_squares` is estimated per layer from the net's area `A` and
  perimeter `P` by modelling its copper as one equivalent rectangle
  (`L`,`W` = roots of `t² − (P/2)t + A = 0`; squares = `L/W`, clamped ≥ 1;
  exact for a single rectangular wire, → 1 for a square). This biases series
  resistance conservatively high for L-shaped or fragmented nets.

Conductor roles map to the deck's geometry layers: `diffusion` (the NMOS+PMOS
source/drain regions), `poly`, and each `metals[i]` metal-stack layer. The
coefficients are curated per-PDK-family in each deck module's `PARASITICS`
table (`src/klayout_tools/decks/sky130.py` / `gf180mcu.py`), sourced from each
PDK's **public** process/DRM data — never NDA'd — the same curation pattern as
the DRC decks and the SPICE model-binding table (#214).

The R/C values are **representative, uncalibrated, order-of-magnitude starter
values**: parasitic-extraction accuracy tuning/calibration against silicon is
an explicit non-goal of this first cut (#216 "Non-goals"). The model is fixed,
not tunable — there is no `fast`/`accurate` mode selector.

### What it does *not* do

- **Net-to-net coupling capacitance** is explicitly out of scope for this
  first increment (ground capacitance only). Coupling needs spacing-aware
  neighbor geometry the lumped-to-ground model does not capture; it is a
  credible second increment once a friction log demands it.
- **Device connectivity is untouched.** The parasitic elements are additive:
  no existing device instance, net name, or pin is modified, and the internal
  `net__par` parasitic nodes never surface in `devices[]`/`nets[]` (see below)
  or on the `.SUBCKT` pin interface. The netlist stays a drop-in `klt sim`
  `netlist`.

### JSON `parasitics` block

`--parasitics` adds a top-level `parasitics` field (an additive, independently
optional field — `null` when the flag is omitted). The existing
`device_count` / `net_count` / `devices[]` / `nets[]` keep their exact
schematic-equivalent meaning whether or not the flag is given; the parasitic
R/C are **not** counted as devices and the internal nodes are **not** counted
as nets.

```json
"parasitics": {
  "r_count": 4,
  "c_count": 4,
  "total_resistance_ohm": 3050.7818,
  "total_capacitance_ff": 5.400116,
  "nets": [
    {
      "net": "Y",
      "resistance_ohm": 1169.7827,
      "capacitance_ff": 1.910204,
      "internal_node": "Y__par"
    }
  ]
}
```

| Field                  | Type            | Description                                                                                     |
| ---------------------- | --------------- | ------------------------------------------------------------------------------------------------- |
| `r_count` / `c_count`  | integer         | Number of parasitic resistors / capacitors emitted (one Γ-section per net, so these are equal).   |
| `total_resistance_ohm` | number          | Sum of the emitted series resistances (ohms).                                                     |
| `total_capacitance_ff` | number          | Sum of the emitted ground capacitances (femtofarads).                                             |
| `nets`                 | array\<object\> | One entry per net carrying parasitics, sorted by `net` for deterministic output. See below.       |

Each `nets[]` entry: `net` (the schematic-equivalent net name), `resistance_ohm`,
`capacitance_ff`, and `internal_node` (the injected internal parasitic node's
name, `<net>__par`, or a collision-suffixed variant).

## Verified compatible with `klt sim`'s netlist convention

Hard acceptance bar (Epic #153: "`klt extract` output feeds `klt sim`
unmodified"), verified directly against
[`docs/cli/sim.md`](sim.md) → "Netlist convention: a circuit body, not a
full deck" rather than asserted: the written SPICE file is a
`.SUBCKT <top> <pins…> … .ENDS <top>` circuit body with **no top-level
`.control`/`.end` card** — confirmed directly against KLayout's
`NetlistSpiceWriter` output (it never emits a top-level `.END` for a
single-circuit netlist) and exercised by `tests/test_extract.py`.

An extracted netlist is a *DUT* with no stimulus (nothing in a layout says
"this rail is 1.8 V"); it is consumed by `klt sim` the way any DUT is — a
thin testbench `.include`s the extracted file, instantiates the `.subckt`,
and adds the sources, and *that* testbench is the `klt sim` `netlist`.
`tests/test_extract.py`'s `test_extracted_netlist_feeds_klt_sim_unmodified`
exercises exactly this against a real sky130 corpus cell end to end (skipped
when `ngspice` is not installed).

## JSON schema (the contract)

**JSON is the API.** Human-readable text output is a courtesy; the JSON
schema below is the stable contract. Per the project's rules, **breaking
(renaming, removing, or retyping) a field is a breaking change**. New fields
may be added without breaking the contract, so consumers should ignore
unknown fields. See [`docs/json-contract.md`](../json-contract.md) for the
envelope shared across all `klt` commands (`schema_version`, error shape,
exit codes).

```json
{
  "schema_version": 1,
  "file": "design.gds",
  "deck": "sky130",
  "top": "ota_5t",
  "dbu_um": 0.001,
  "netlist_path": "design.spice",
  "netlist_sha256": "4f1c...",
  "status": "extracted",
  "device_count": 2,
  "net_count": 6,
  "pin_count": 6,
  "device_counts": { "nfet": 1, "pfet": 1 },
  "device_classes": [
    "nfet",
    "pfet",
    "pnp",
    "sky130_fd_pr__model__cap_mim",
    "sky130_fd_pr__model__cap_mim_m4"
  ],
  "devices": [
    {
      "name": "$1",
      "class": "nfet",
      "nets": { "s": "VGND", "g": "A", "d": "Y", "b": "vsubs" },
      "params": { "w_um": 0.65, "l_um": 0.15 }
    }
  ],
  "nets": [{ "name": "A", "pin": true, "device_count": 2 }],
  "warnings": [],
  "pdk": null,
  "parasitics": null
}
```

### Top-level fields

| Field              | Type                       | Description                                                                                          |
| ------------------ | -------------------------- | ------------------------------------------------------------------------------------------------------ |
| `schema_version`   | integer                    | Version of this command's JSON shape (starts at `1`; per-command, per `docs/json-contract.md`).        |
| `file`             | string                     | The input path exactly as provided on the command line.                                                |
| `deck`             | string                     | Extraction deck used (`"sky130"` / `"gf180mcu"`).                                                      |
| `top`              | string                     | Top cell the netlist was extracted from.                                                               |
| `dbu_um`           | number (float)             | Database unit in micrometres, same semantics as `klt layers`/`klt drc`.                                |
| `netlist_path`     | string                     | Resolved path of the written SPICE netlist (echoes `--output` or the computed default).                |
| `netlist_sha256`   | string                     | SHA-256 hex digest of the written netlist file.                                                        |
| `status`           | `"extracted"`              | Never `"error"` — a failed run does not emit this envelope at all (see Exit codes).                    |
| `device_count`     | integer                    | `len(devices)`.                                                                                        |
| `net_count`        | integer                    | `len(nets)`.                                                                                           |
| `pin_count`        | integer                    | Number of `nets[]` entries with `pin: true`.                                                           |
| `device_counts`    | object\<string, int\>      | Per-device-class counts, keys sorted for determinism (`"nfet"`/`"pfet"`, and, on decks/layouts that have one, a bipolar class like `"pnp"`/`"bjt"` and/or a MiM-capacitor class like `"sky130_fd_pr__model__cap_mim"`/`"cap_mim_2f0_m4m5_noshield"`). What was actually **found**.  |
| `device_classes`   | array\<string\>            | The device-class roles this `--deck` is structurally capable of recognising (`["nfet", "pfet"]` for a MOS-only deck; a deck that also declares a bipolar entry appends its class name, e.g. sky130's `[..., "pnp", ...]` or gf180mcu's `[..., "bjt", ...]` — see "Bipolar (BJT) device recognition" above — and a deck that declares one or more MiM-capacitor entries likewise appends each one's class name, e.g. sky130's two `"sky130_fd_pr__model__cap_mim"`/`"sky130_fd_pr__model__cap_mim_m4"` or gf180mcu's one `"cap_mim_2f0_m4m5_noshield"` — see "MiM capacitor device recognition" above), independent of what this layout happens to contain. What the deck **can find**, not what it found — see `device_counts` for that. A consumer that needs to know ahead of time whether a deck supports a given device class (e.g. before pairing it with a reference netlist for `klt lvs`) reads this instead of inferring "unsupported" from a zero count. |
| `devices`          | array\<object\>            | One entry per extracted device, see below.                                                             |
| `nets`             | array\<object\>            | One entry per extracted net, see below.                                                                |
| `warnings`         | array\<string\>            | Non-fatal extraction notes (e.g. a gate shape touching no diffusion). Always present, empty when clean. |
| `pdk`              | object \| `null`           | `{"variant", "root", "version"}` when `--pdk`/`--pdk-root` were given and resolved; `null` otherwise.   |
| `parasitics`       | object \| `null`           | Lumped RC summary when `--parasitics` was given; `null` otherwise. See "Parasitic (RC) extraction".     |

The `devices[]`/`nets[]` report is a *convenience view* for agents that want
structure without re-parsing SPICE; the **netlist file at `netlist_path` is
the authoritative artifact**, and it is what a future `klt lvs` and `klt sim`
consume.

### `devices[]` entries

| Field    | Type                        | Description                                                                                    |
| -------- | --------------------------- | ------------------------------------------------------------------------------------------------ |
| `name`   | string                      | The device's instance name in the written netlist (e.g. `"$1"`, matching the `M$1 ...` line).    |
| `class`  | string                      | The deck's device-class name (`"nfet"` / `"pfet"`, a declared bipolar class like `"pnp"` / `"bjt"`, or a declared MiM-capacitor class like `"sky130_fd_pr__model__cap_mim"` / `"cap_mim_2f0_m4m5_noshield"`). |
| `nets`   | object\<string, string\|null\> | Terminal → net-name map. MOS: `"s"`, `"g"`, `"d"`, `"b"`. Bipolar: `"c"`, `"b"`, `"e"` (collector/base/emitter — see "Bipolar (BJT) device recognition" above). MiM capacitor: `"a"`, `"b"` (the two plates — see "MiM capacitor device recognition" above). `null` only if a terminal has no connected net at all (never observed for `s`/`g`/`d` in this deck's extraction; MOS `b` and bipolar `b` can be `null`-free but anonymous, see "Coverage"). |
| `params` | object\<string, number\>    | MOS: `"w_um"` / `"l_um"`, the extracted gate width/length in micrometres. Bipolar: empty (KLayout's `DeviceClassBJT3Transistor` reports area/perimeter parameters this field does not extract). MiM capacitor: `"c_f"` (extracted capacitance, in **Farads**) and `"area_um2"` (the plates' overlap area, in square micrometres — `c_f = area_um2 * area_cap`, see "MiM capacitor device recognition" above). |

`devices` is sorted by `name` for deterministic, diff-clean output.

### `nets[]` entries

| Field          | Type    | Description                                                                 |
| -------------- | ------- | ------------------------------------------------------------------------------ |
| `name`         | string  | The net's name in the written netlist (a labelled name, or an anonymous `$N`). |
| `pin`          | boolean | Whether this net is promoted to a top-cell pin (a named net at the top level). |
| `device_count` | integer | Number of device terminals connected to this net.                             |

`nets` is sorted by `name` for deterministic, diff-clean output.

## Exit codes

| Code | Meaning                                                                                          |
| ---- | --------------------------------------------------------------------------------------------------- |
| `0`  | Extraction succeeded, netlist written.                                                              |
| `1`  | Failed to run — bad file, unknown `--deck`, unresolvable PDK (when `--pdk`/`--pdk-root` given), a resolved PDK with no curated model-binding table entry for `--deck` (see "SPICE model binding" above), missing/ambiguous top cell, or an engine error. |
| `2`  | Usage error (missing argument, bad `--format` value) — from argparse.                               |

There is no exit code `3` — unlike `klt drc`/`klt lvs`, there is no "ran but
found problems" outcome for extraction; it either produces a netlist or it
fails (matching `klt gen`'s reasoning for omitting a `3`).

On error (exit 1), a concise message is written to **stderr** and nothing is
written to stdout. No Python traceback is printed.

- `--format text` (default): a plain-text line prefixed `klt extract:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "extract", "message": "unknown deck 'nope' (available: gf180mcu, sky130)" } }
  ```

## Out of scope

First-order lumped RC parasitics are available behind `--parasitics` (see
above, issue #217). The following remain out of scope:

- **Net-to-net coupling capacitance.** `--parasitics` models lumped
  capacitance to ground only; coupling between neighboring nets is
  deliberately deferred (it needs spacing-aware neighbor geometry the
  lumped-to-ground model does not capture) and is a credible second
  increment. Without `--parasitics`, no interconnect R/C is extracted at all.
- **Parasitic-extraction accuracy calibration.** The `--parasitics`
  coefficients are representative, uncalibrated, order-of-magnitude starter
  values (see "Parasitic (RC) extraction"); calibrating them against silicon
  is an explicit non-goal (#216 "Non-goals").
- **Drawn resistor device class.** Bipolar and MiM-capacitor devices are
  recognised (see "Bipolar (BJT) device recognition" and "MiM capacitor
  device recognition"), but neither deck declares a precision-resistor
  extractor, so a resistor-marked conductor still extracts as ordinary
  connectivity. Tracked as a sibling sub-issue of #219
  ([#222](https://github.com/2AMLogic/klayout-tools/issues/222)).

Netlist comparison (`klt lvs`) is a separate command; this command only
produces the layout-side netlist half of that comparison.
