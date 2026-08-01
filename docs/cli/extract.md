# `klt extract`

Extract a schematic-equivalent SPICE netlist — devices and connectivity, no
parasitics — from a GDSII or OASIS layout stream, and report it as structured
data.

```
klt extract <file> --deck sky130|gf180mcu [-o|--output <netlist.spice>]
                   [--top <cell>] [--pdk <variant>] [--pdk-root <path>]
                   [--format text|json]
```

- `<file>` — path to a GDSII (`.gds`) or OASIS (`.oas`) file. KLayout
  auto-detects the stream format on read; the extension is not authoritative.
- `--deck` — the extraction deck: the connectivity + device-extraction rule
  set. Currently: `sky130`, `gf180mcu`. Required unless `--pdk`/`--pdk-root`
  resolve an install whose variant names one (see "PDK resolution").
- `--output` / `-o` — where to write the extracted SPICE netlist. Defaults to
  the input path with its extension replaced by `.spice` (`design.gds` →
  `design.spice`), the "next to the input" convention `klt render`/`klt sim`
  already use.
- `--top` — the top cell to extract. Required when the stream has more than
  one top cell; otherwise the single top cell is used.
- `--pdk` / `--pdk-root` — optional; resolve a PDK install for provenance and
  to derive `--deck` (see "PDK resolution").
- `--format` — `text` (default, a human-readable summary) or `json`. This
  governs only the **report**; the extracted netlist always goes to
  `--output`.

This is phase 2 of Epic #153. The engine choice and the contract below come
from the accepted spike,
[`docs/design/lvs-extraction-spike.md`](../design/lvs-extraction-spike.md)
§2a. `klt lvs` (compare an extracted netlist against a reference) is phase 3
and does not exist yet.

## Engine

`klt extract` runs fully headless via the pip `klayout` package's own
`klayout.db.LayoutToNetlist` (connectivity model + device recognition) and
`klayout.db.NetlistSpiceWriter` (SPICE serialisation) — the same wrapped
dependency `klt drc` and `klt render` already use, with **no dependency on
the standalone `klayout` application binary or its LVS-DSL script runner**,
no GUI, and no Qt. Only `pip install klayout` is needed, so the command runs
anywhere that already runs in CI.

The spike's §1 survey scored netgen (the open flow's LVS comparator) and
magic (its extractor) and set them aside as an *oracle*, not a runtime: they
would add a Tcl runtime and a second geometry backend for capability the
already-wrapped KLayout engine has. Because the contract is the API and the
engine is an implementation detail, that choice stays reversible.

## Extraction decks

An **extraction deck** is this repo's own declarative connectivity + device
recipe (`klayout_tools.decks.ExtractionDeck`): which drawn layers exist,
which derived layers are computed from them (`and`/`not` region operations),
which device extractors run over those derived layers, what connects to what,
which layer carries the substrate global net, and which text layers name
nets. It drives `LayoutToNetlist` directly, the same way `klt drc`'s
`DrcRule` table drives `Region`'s check primitives.

These decks are **ours to curate**. Neither open PDK ships a KLayout-native
LVS deck — sky130's LVS flow lives in
[`fossi-foundation/open-pdks`](https://github.com/fossi-foundation/open-pdks)
as magic extraction rules plus a netgen setup, and
[`google/globalfoundries-pdk-libs-gf180mcu_fd_pv`](https://github.com/google/globalfoundries-pdk-libs-gf180mcu_fd_pv)
(archived) ships KLayout **DRC** decks only. That is exactly the gap the
spike diagnosed, and the reason this deck data is transcribed from each PDK's
published layer map rather than sourced from a shipped LVS setup.

### Coverage

Both decks are **curated starter subsets**, the same scope posture the DRC
decks take, and are expected to grow incrementally in follow-on issues.

| | `sky130` | `gf180mcu` |
| - | - | - |
| Devices | `nfet_01v8`, `pfet_01v8` (four-terminal MOS) | `nfet_03v3`, `pfet_03v3` (four-terminal MOS) |
| Interconnect | `li1` → `met5` (`licon1`/`mcon`/`via`…`via4`) | `Metal1` → `Metal2` (`Contact`/`Via1`) |
| Label layers | `nwell` 64/5, `pwell` 64/59, `li1` 67/5, `met1`–`met5` 68/5…72/5 | `Metal1` 34/10, `Metal2` 36/10 |
| Substrate net | `VSUBS` | `VSUBS` |

Deliberately **not** covered yet, for either deck:

- **Non-MOS devices** — resistors, capacitors, diodes, and bipolars are not
  recognised. Their geometry still participates in connectivity; it simply
  produces no device.
- **Device flavour discrimination** — the two extracted MOS classes are each
  PDK's core-voltage flavour. Thick-oxide (sky130 5 V, gf180mcu's `Dualgate`
  5 V/6 V) and hvt/lvt variants are extracted as the core flavour, because
  separating them needs implant/marker-layer booleans a follow-on increment
  adds. A layout mixing flavours will therefore extract *correct topology*
  with an *understated* device-class distinction — treat `device_counts` as
  "MOS by polarity" until that increment lands.
- **gf180mcu `Metal3` and above** — the interconnect stack stops at `Metal2`,
  the highest pair this repo has verified against the published deck.

Device class names (`nfet_01v8`, …) are a **stable public contract** once
shipped, exactly like a DRC rule id: they appear verbatim in the emitted
SPICE and in `device_counts`/`devices[].class`, and are never renamed or
repurposed.

### Substrate handling

Neither PDK draws an explicit p-well/substrate layer here, so both decks
follow KLayout's own LVS idiom: the n-channel device's body terminal binds to
a synthetic, always-empty `bulk` region, which is tied to the p-substrate
taps and given the global net name **`VSUBS`**. Both decks use the same
global name on purpose, so a downstream testbench or a future `klt lvs` hint
does not have to special-case the PDK.

## PDK resolution

Extraction reads **no files from a PDK install** — the deck is
self-contained — so `--pdk`/`--pdk-root` are optional and `klt extract` runs
in a CI job with no PDK installed. When either is given, the install is
resolved through the one shared resolver behind
[`klt pdk find`/`klt pdk env`](pdk.md) (never a hand-rolled lookup), and:

- the resolved variant/root/version are echoed in the response's `pdk` block,
  so a stored extract records which install the caller was working against;
- when `--deck` is omitted, the deck is derived from the variant by prefix
  (`sky130A` → `sky130`, `gf180mcuD` → `gf180mcu`), the same family mapping
  `klt gen` uses.

An explicitly requested PDK that cannot be resolved is a hard error (exit
`1`), never a silent downgrade. With neither flag, `pdk` is `null`.

## Netlist convention: a circuit body `klt sim` can consume

The written netlist is a **circuit body**: one `.SUBCKT <cell> … .ENDS` per
extracted circuit, with **no top-level `.control` and no top-level `.end`
card**. That is the shape [`klt sim`](sim.md) requires of its `netlist`
input, and it is enforced at this boundary — `klt extract` strips any
top-level `.end` card or `.control`/`.endc` block from the writer's output
and reports the fact in `warnings[]`, so a future KLayout release cannot
silently break a downstream `klt sim` run. (`.ENDS`, which closes a
subcircuit, is of course kept.)

Nets that carry no label get a generated, deterministic `net_<cluster id>`
name rather than the escaped `\$<id>` node the SPICE writer would otherwise
emit, so the file stays readable by a plain SPICE parser. This is reported in
`warnings[]` too — an unlabelled net usually means the layout is missing pin
labels on the PDK's label layers.

### The known asymmetry: a DUT has no stimulus

An extracted netlist is a **device under test**, not a testbench: nothing in
a layout says "this rail is 1.8 V". `klt sim`'s netlist convention lists
sources as part of a circuit body because its worked examples are
self-contained testbenches. An extracted `.subckt` is consumed the way any
DUT is — a thin testbench `.include`s the extracted file, instantiates the
subcircuit, and adds the sources, and *that testbench* is the `klt sim`
`netlist`:

```spice
* testbench body -- no .control/.end (klt sim wraps this)
.param vdd=1.8
.include inv_1.spice
Vdd  VPWR 0 DC {vdd}
Vgnd VGND 0 DC 0
Vin  A    0 PULSE(0 {vdd} 1n 100p 100p 5n 10n)
X1 A VGND VPB VPWR VSUBS Y sky130_fd_sc_hd__inv_1
```

The extracted body satisfies the load-bearing half of the convention (no
`.control`/`.end`, valid to `.include`); the stimulus half is the
testbench's job, exactly as it is for a schematic-side netlist.

### Not yet: binding device classes to PDK SPICE primitives

The emitted devices are SPICE **device elements** referencing the deck's
device-class name (`M$1 Y A VGND VSUBS nfet_01v8 L=0.15U W=0.65U …`) — the
form KLayout's extractor produces and the form a topological comparer
(`klt lvs`, phase 3) consumes. Open PDKs ship their primitives as
*subcircuits* instead (`sky130_fd_pr__nfet_01v8`, instantiated with `X`), so
simulating an extracted netlist against the PDK's model library needs those
classes bound to the PDK's primitive names.

Choosing between the two representations — device element (what LVS wants)
and subcircuit call (what a simulator wants) — is a contract decision the
spike did not settle, and it belongs to **phase 4, loop closure through
`klt sim`**, together with the worked example that exercises it end to end.
Until then, an extracted netlist is `.include`-able and parses cleanly, and
binding it to models is the caller's step.

## Scope: schematic-equivalent, no parasitics

Extraction is **schematic-equivalent**: devices and connectivity only, with
no parasitic R/C on the interconnect. That deferral is the spike's own
recorded decision, and a future parasitic mode is an *additive* extension (a
flag, extra elements in the emitted netlist) that does not break this
contract.

Geometric matching verification (the generator spike's `matched_group_id`) is
also out of scope, and for a structural reason: schematic-equivalent
extraction and LVS are *topological*, while common-centroid/symmetry intent
is *geometric*. See the spike's §4.

## JSON schema (the contract)

**JSON is the API.** Human-readable text output is a courtesy; the JSON
schema below is the stable contract. Per the project's rules, **breaking
(renaming, removing, or retyping) a field is a breaking change**, and device
class names are part of that contract. New fields may be added without
breaking it, so consumers should ignore unknown fields. See
[`docs/json-contract.md`](../json-contract.md) for the envelope shared across
all `klt` commands (`schema_version`, error shape, exit codes).

The **netlist file at `netlist_path` is the authoritative artifact** — it is
what `klt sim` (and, later, `klt lvs`) consumes. The `devices[]`/`nets[]`
report is a convenience view for agents that want structure without
re-parsing SPICE. The netlist is a **file reference, never inlined**, the
same discipline `klt sim` applies to its waveform/log artifacts.

```json
{
  "schema_version": 1,
  "file": "tests/corpus/sky130/sky130_fd_sc_hd__inv_1.gds",
  "deck": "sky130",
  "top": "sky130_fd_sc_hd__inv_1",
  "dbu_um": 0.001,
  "pdk": null,
  "netlist_path": "tests/corpus/sky130/sky130_fd_sc_hd__inv_1.spice",
  "netlist_sha256": "7920b21aa2e88afb50caaad4b82946511e075539649b68738eb90adc1e884ede",
  "status": "extracted",
  "device_count": 2,
  "net_count": 6,
  "pin_count": 6,
  "device_counts": { "nfet_01v8": 1, "pfet_01v8": 1 },
  "devices": [
    {
      "circuit": "sky130_fd_sc_hd__inv_1",
      "name": "$1",
      "class": "nfet_01v8",
      "nets": { "S": "VGND", "G": "A", "D": "Y", "B": "VSUBS" },
      "params": {
        "ad_um2": 0.169,
        "as_um2": 0.169,
        "l_um": 0.15,
        "pd_um": 1.82,
        "ps_um": 1.82,
        "w_um": 0.65
      }
    }
  ],
  "nets": [
    {
      "circuit": "sky130_fd_sc_hd__inv_1",
      "name": "Y",
      "pin": true,
      "device_count": 2
    }
  ],
  "warnings": []
}
```

### Top-level fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema_version` | integer | Version of this command's JSON shape (starts at `1`; per-command). |
| `file` | string | The input path exactly as provided on the command line. |
| `deck` | string | The extraction deck used (`"sky130"` or `"gf180mcu"`). |
| `top` | string | Name of the top cell the netlist was extracted from. |
| `dbu_um` | number (float) | Database unit in micrometres, same semantics as `klt layers`/`klt drc`. |
| `pdk` | object \| null | `{"variant", "root", "version"}` when `--pdk`/`--pdk-root` resolved an install; `null` when neither was given. |
| `netlist_path` | string | Resolved path of the written SPICE netlist (echoes `--output` or the computed default). |
| `netlist_sha256` | string | SHA-256 of the written netlist, so a stored extract can be checked against the file it produced — and so a later `klt sim`/`klt lvs` can record *which* extracted netlist it consumed. |
| `status` | `"extracted"` | Never `"error"` — a failed run does not emit this envelope at all (see Exit codes). |
| `device_count` | integer | `len(devices)` — every extracted circuit, not just the top one. |
| `net_count` | integer | `len(nets)` — likewise. |
| `pin_count` | integer | Pins of the **top** circuit: the ports of the `.SUBCKT` a caller instantiates. |
| `device_counts` | object\<string,int\> | Per-device-class counts; keys sorted for determinism (same shape as `klt drc`'s `rule_counts`). |
| `devices` | array\<object\> | One entry per extracted device, see below. |
| `nets` | array\<object\> | One entry per extracted net, see below. |
| `warnings` | array\<string\> | Non-fatal extraction notes. Always present, empty when clean — the same "always report the array" discipline as `klt drc`'s `violations`. |

### `devices[]` entries

| Field | Type | Description |
| ----- | ---- | ----------- |
| `circuit` | string | Name of the circuit (extracted cell) the device belongs to. |
| `name` | string | Device name, identical to the instance name in the emitted SPICE (`$1` here, written as `M$1`). |
| `class` | string | The deck's device-class name (e.g. `"nfet_01v8"`) — a stable contract, like a DRC rule id. |
| `nets` | object\<string, string\|null\> | Terminal → net name. Keys are the device class's own terminal ids (`S`/`G`/`D`/`B` for a four-terminal MOS), kept verbatim rather than case-folded: they are shared with the reference netlist `klt lvs` will compare against. `null` for an unconnected terminal. |
| `params` | object\<string, number\> | Extracted geometry, unit-suffixed from the engine's own scaling exponent: `l_um`, `w_um`, `ps_um`, `pd_um` (micrometres) and `as_um2`, `ad_um2` (square micrometres). Keys sorted. |

### `nets[]` entries

| Field | Type | Description |
| ----- | ---- | ----------- |
| `circuit` | string | Name of the circuit the net belongs to. |
| `name` | string | Net name — a label string where the layout carries one, a global net name (`VSUBS`), or a generated `net_<cluster id>` where it does not. |
| `pin` | boolean | Whether the net surfaces as a pin of its circuit. |
| `device_count` | integer | Number of **distinct** devices attached (a device with two terminals on the same net counts once). |

### Ordering

`devices` is sorted by `(circuit, class, name)` and `nets` by
`(circuit, name)`, so repeated runs against the same input produce identical,
diff-clean output — the same canonical-ordering guarantee `klt drc` makes
about `violations`.

Both arrays enumerate **every** extracted circuit, not just the top one: a
hierarchical layout puts its devices in the subcircuits, and a top-only
report would claim "0 devices" about a netlist full of them. `circuit` is
what disambiguates them.

## Exit codes

| Code | Meaning |
| ---- | ------- |
| `0`  | Extracted a netlist. |
| `1`  | Failed to run — bad file, unknown `--deck`, ambiguous or unknown `--top`, unresolvable `--pdk`, unwritable output, or an engine error. |
| `2`  | Usage error (missing argument, bad `--format` value) — from argparse. |

There is deliberately **no exit `3`** here, unlike `klt drc`: extraction has
no "ran successfully but found problems" outcome — it either produces a
netlist or it fails. (Findings-style results arrive with `klt lvs` in phase
3, which does use `3`.) Non-fatal observations go in `warnings[]` and still
exit `0`.

On error (exit 1), a concise message is written to **stderr** and nothing is
written to stdout. No Python traceback is printed — including for an unknown
`--deck` name.

- `--format text` (default): a plain-text line prefixed `klt extract:`.
- `--format json`: the shared error envelope from
  [`docs/json-contract.md`](../json-contract.md), on stderr.

## Examples

```bash
# sky130 standard cell -> netlist next to the input
klt extract sky130_fd_sc_hd__inv_1.gds --deck sky130

# gf180mcu, explicit output path, JSON report
klt extract clkinv_1.gds --deck gf180mcu -o build/clkinv.spice --format json

# derive the deck from a resolved PDK install, and record its provenance
klt extract design.gds --pdk sky130A --format json

# a stream with several top cells
klt extract lib.gds --deck sky130 --top my_block
```
