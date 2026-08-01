# `klt extract`

Extract a **schematic-equivalent** netlist (devices + connectivity) from a
GDSII or OASIS layout stream, headless, and write it as a SPICE circuit body
plus a structured summary. Opt in to first-order interconnect parasitics with
`--parasitics`.

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
- `--parasitics` — optional, off by default. Also extract first-order lumped
  interconnect parasitics. See "Parasitic extraction" below.
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
voltage-flavor distinction).

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
  carried onto the `X` card — the resolved subcircuit models the device's own
  junction capacitance itself. Interconnect parasitics are a separate,
  opt-in concern (see "Parasitic extraction" below), and `--parasitics`
  composes with model binding.
- **The JSON response is unaffected**: `device_counts`/`devices[].class`
  always report the deck's own class label (`nfet`/`pfet`), regardless of
  `--pdk`. Model binding is a SPICE-serialization concern only.

## Parasitic extraction (`--parasitics`)

Off by default. When given, `klt extract` additionally models the
**interconnect** parasitics of every extracted net and folds them into the
same netlist. Implemented per the interface decision in
[`docs/design/lvs-extraction-spike.md`](../design/lvs-extraction-spike.md) →
"Addendum (#216)"; the engine survey that decision deferred is written up in
`src/klayout_tools/parasitics.py`'s module docstring (short version: KLayout's
`db` module has no interconnect-PEX call to wrap, its newer `klayout.pex`
module solves only the R half and only in *distributed* form, and the one
credible external wrapper — `iic-jku/klayout-pex` — is GPL-3.0 and depends on
non-pip external binaries, so the first-order reduction is built here over a
curated coefficient table).

### The model

Per net, per conductor layer (poly and each metal level — diffusion is
excluded because the device models already carry it, following both PDKs'
own extraction rules):

- **Capacitance to ground**: `area_um2 × area_cap + perimeter_um ×
  perimeter_cap`, from the net's own merged shapes.
- **Resistance**: `squares × sheet_rho`, where each merged polygon's square
  count comes from its *equivalent rectangle* (the `L`/`W` with the polygon's
  own area and perimeter, `squares = L/W`; a shape too compact for that
  system to have a real solution counts as one square). Polygons on a layer
  sum in series.

Both are attached as **one lumped RC load per net** — a resistor from the net
to a generated internal node, and a capacitor from that node to the deck's
substrate net:

```
RY   Y      Y_par  106.146
CY   Y_par  vsubs  2.39653e-16
```

Nothing existing is disturbed: no net is split and no device terminal is
re-parented, so with `--parasitics` on, `devices[]`, `device_counts`,
`net_count`, `pin_count`, every net name, and every pin are **identical** to
the same run without it.

One documented exception, reported as a `warnings[]` entry when it happens:
every generated capacitor returns to the deck's substrate net, which must be
tieable from a testbench. That net is normally already a pin (any layout with
an NMOS has it), but a layout that never creates it — a PMOS-only cell, say —
gets it promoted to a pin so the capacitance is not stranded on an
unreachable internal node. That is the only case in which `--parasitics`
changes the `.SUBCKT` interface, and it never happens silently.

### What this does *not* model

Stated plainly, so no caller mistakes this for sign-off-grade PEX:

- **Net-to-net coupling capacitance** — not modelled at all (explicitly
  deferred by #216 to a second increment). Only capacitance to substrate.
- **IR drop / driver-to-receiver wire delay** — the series R is in series
  with the net's own capacitance, not in the signal path between a driver and
  its receivers. Placing it in the path requires partitioning a net's
  terminals, which a single-R lumped model has no non-arbitrary basis for; a
  distributed increment (KLayout's `klayout.pex` `RNetExtractor` is the
  natural engine) is the honest way to get it.
- **Contact/via resistance** — not modelled.
- **Accuracy tuning** — one fixed operating point, no `fast`/`accurate`
  selector (#216), and no calibration against silicon.

### Coefficients

Curated per PDK family in `src/klayout_tools/decks/sky130.py` /
`decks/gf180mcu.py` as a `ParasiticTech` (`sheet_ohm_per_sq`,
`area_cap_af_per_um2`, `perimeter_cap_af_per_um` per layer), positionally
aligned with the same module's `ExtractionDeck` so the two cannot drift apart
silently. Every value is transcribed from that PDK's **public** process data
— the `resist` and `defaultareacap`/`defaultperimeter` entries of its magic
technology file in
[fossi-foundation/open-pdks](https://github.com/fossi-foundation/open-pdks)
(`sky130/magic/sky130.tech`, `gf180mcu/magic/gf180mcu.tech`), nominal corner
— with the exact file, section, and unit conversion cited inline, the same
provenance discipline the DRC decks follow. No PDK install is required to use
`--parasitics`: the coefficients ship with the deck, exactly like the DRC
thresholds.

`--parasitics` reuses `--deck`/`--pdk`/`--pdk-root` unchanged; there is no
new PDK-selection mechanism. It composes with `--pdk` model binding (MOS
devices still become `X` subcircuit calls; the parasitic elements are still
plain `R`/`C` cards).

### Parasitics and LVS

Do **not** feed a `--parasitics` netlist to `klt lvs`. LVS is a
schematic-equivalence compare, and a parasitic-annotated netlist is
isomorphic to no schematic reference — it has extra R/C devices and extra
nodes by construction. `klt lvs`'s own inline extraction never enables
parasitics.

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

With `--parasitics`, `parasitics` is a block and each `nets[]` entry gains
two fields (everything else is unchanged):

```json
{
  "nets": [
    {
      "name": "Y",
      "pin": true,
      "device_count": 2,
      "resistance_ohm": 106.146295,
      "capacitance_ff": 0.239653
    }
  ],
  "parasitics": {
    "model": "lumped_rc_load",
    "ground_net": "vsubs",
    "net_count": 4,
    "resistor_count": 4,
    "capacitor_count": 4,
    "total_resistance_ohm": 717.979281,
    "total_capacitance_ff": 1.269301
  }
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
| `device_counts`    | object\<string, int\>      | Per-device-class counts (`"nfet"`/`"pfet"`), keys sorted for determinism.                              |
| `devices`          | array\<object\>            | One entry per extracted device, see below.                                                             |
| `nets`             | array\<object\>            | One entry per extracted net, see below.                                                                |
| `warnings`         | array\<string\>            | Non-fatal extraction notes (e.g. a gate shape touching no diffusion). Always present, empty when clean. |
| `pdk`              | object \| `null`           | `{"variant", "root", "version"}` when `--pdk`/`--pdk-root` were given and resolved; `null` otherwise.   |
| `parasitics`       | object \| `null`           | The lumped RC model's rollup when `--parasitics` was given; `null` otherwise. See below.                |

### `parasitics` block (only with `--parasitics`)

| Field                  | Type    | Description                                                                                             |
| ---------------------- | ------- | ---------------------------------------------------------------------------------------------------------- |
| `model`                | string  | Identifier of the reduction that produced these numbers — `"lumped_rc_load"` (see "Parasitic extraction"). |
| `ground_net`           | string  | The net every generated capacitor returns to (the deck's substrate net, e.g. `"vsubs"`).                 |
| `net_count`            | integer | Number of nets that got parasitic elements (a net with no geometry on any curated layer gets none).      |
| `resistor_count`       | integer | Number of generated `R` elements (one per modelled net).                                                 |
| `capacitor_count`      | integer | Number of generated `C` elements (one per modelled net).                                                 |
| `total_resistance_ohm` | number  | Sum of every net's modelled series resistance, in ohms.                                                  |
| `total_capacitance_ff` | number  | Sum of every net's modelled capacitance to ground, in femtofarads.                                       |

The `devices[]`/`nets[]` report is a *convenience view* for agents that want
structure without re-parsing SPICE; the **netlist file at `netlist_path` is
the authoritative artifact**, and it is what a future `klt lvs` and `klt sim`
consume.

### `devices[]` entries

| Field    | Type                        | Description                                                                                    |
| -------- | --------------------------- | ------------------------------------------------------------------------------------------------ |
| `name`   | string                      | The device's instance name in the written netlist (e.g. `"$1"`, matching the `M$1 ...` line).    |
| `class`  | string                      | The deck's device-class name (`"nfet"` / `"pfet"`).                                              |
| `nets`   | object\<string, string\|null\> | Terminal → net-name map: `"s"`, `"g"`, `"d"`, `"b"`. `null` only if a terminal has no connected net at all (never observed for `s`/`g`/`d` in this deck's extraction; `b` can be `null`-free but anonymous, see "Coverage"). |
| `params` | object\<string, number\>    | `"w_um"` / `"l_um"`, the extracted gate width/length in micrometres.                             |

`devices` is sorted by `name` for deterministic, diff-clean output.

### `nets[]` entries

| Field          | Type    | Description                                                                 |
| -------------- | ------- | ------------------------------------------------------------------------------ |
| `name`         | string  | The net's name in the written netlist (a labelled name, or an anonymous `$N`). |
| `pin`          | boolean | Whether this net is promoted to a top-cell pin (a named net at the top level). |
| `device_count` | integer | Number of device terminals connected to this net (never counts generated parasitic elements). |
| `resistance_ohm` | number | **Only with `--parasitics`.** The net's modelled lumped series resistance, in ohms (`0.0` for a net with no geometry on any curated parasitic layer). |
| `capacitance_ff` | number | **Only with `--parasitics`.** The net's modelled lumped capacitance to `parasitics.ground_net`, in femtofarads (`0.0`, as above). |

`nets` is sorted by `name` for deterministic, diff-clean output. The internal
RC nodes `--parasitics` generates are **not** reported as nets — they are an
implementation detail of the written netlist, and `nets[]` keeps its
documented meaning with or without the flag.

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

**Net-to-net coupling capacitance** is not modelled — `--parasitics` extracts
capacitance to substrate only, and resistance only along a net's own
conductors. This is a deliberate first-increment boundary set by
[#216](https://github.com/2AMLogic/klayout-tools/issues/216) (recorded in
[`docs/design/lvs-extraction-spike.md`](../design/lvs-extraction-spike.md) →
"Addendum (#216)") and delivered as scoped by
[#217](https://github.com/2AMLogic/klayout-tools/issues/217); coupling
requires spacing-aware neighbour geometry a lumped-to-ground model cannot
carry. The full not-modelled list is in "Parasitic extraction" above.
Accuracy calibration against silicon is likewise out of scope.

Netlist comparison (`klt lvs`) is a separate command; this command only
produces the layout-side netlist half of that comparison.
