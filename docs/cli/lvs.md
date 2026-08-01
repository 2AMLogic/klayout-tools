# `klt lvs`

Compare a layout-derived netlist against a reference (schematic/golden) SPICE
netlist and report structured, categorised mismatches — the LVS half of
Epic #153, following the layout-vs-schematic pattern
[`klt extract`](extract.md) established for extraction.

```
klt lvs <request.json> [--format text|json]
```

This is phase 3 of Epic #153 (`klt lvs`/`klt extract`), the build carried by
the accepted spike,
[`docs/design/lvs-extraction-spike.md`](../design/lvs-extraction-spike.md)
(section 2b) — read it first for the engine survey and the reasoning behind
the contract shape below. This document is the shipped contract; where the
two disagree, this document (and the code) win.

Unlike `klt extract`/`klt drc`, `klt lvs` takes a **request document** (like
`klt sim`/`klt gen`), not positional netlist file args — it binds two
netlist inputs plus optional matching hints, richer than a flag line carries
cleanly.

- `<request.json>` — path to a request document (see "Request" below). A
  *reference*, not inline JSON on the command line.
- `--format` — `text` (default, a human-readable summary) or `json`.

## Engine

`klt lvs` runs fully headless via the pip `klayout` package's native
`klayout.db.NetlistComparer` (graph-isomorphism netlist comparison, net/
device/pin matching with hint support) and `klayout.db.NetlistSpiceReader`
(parsing the reference netlist) — the same wrapped dependency `klt extract`,
`klt drc`, and `klt render` already use. There is no dependency on the
standalone `klayout` application binary, netgen, or magic — only `pip install
klayout` (already this repo's sole runtime dependency).

Comparison is **in-process**, not a subprocess — unlike `klt sim`'s
per-corner `ngspice` fan-out, `NetlistComparer` is a deterministic graph
operation on in-memory data structures with no external process to time out
or kill (spike section 1, "Invocation strategy").

`request.engine` is a data field, not a code path — only `"klayout"` is
implemented in this version; an unsupported value is an application error
(exit 1).

## Scope: schematic-equivalent, topological compare only

Per the phase 1 spike's resolution (section 4, "Resolving the
`matched_group_id` open question"), `klt lvs` does **not** read
`matched_group_id` — a geometric-matching check deferred to a follow-up
epic — and does no layout-vs-layout geometric diffing. This command compares
device/net/pin topology only, exactly as `NetlistComparer` does natively.

## Netlist form: the schematic-equivalent, plain-element form

The reference (and any pre-extracted layout) netlist must use the same
**schematic-equivalent** device form `klt extract` writes: a plain element
line whose leading letter names the device class and whose parameters are
geometric literals —

```
M1 d g s b nfet L=0.15U W=0.65U
```

— **not** a SPICE simulation deck's subcircuit-call form for a PDK whose
models are subcircuits (`XM1 d g s b sky130_fd_pr__nfet_01v8 L=... W=...`).
Handing `NetlistSpiceReader` the subcircuit-call form does not error: it
reads the call as an instance of an undefined subcircuit, the circuit
collapses toward a single merged net, and the compare reports a confusing
`net.merged`/`topology` mismatch that reads like a layout bug but is actually
a netlist-form mismatch. If a caller's schematic tool can only emit the
simulation form, convert it (element letter + model name + the geometric
parameter subset) before pointing a `klt lvs` request at it — this is a
small, mechanical, PDK-parameterized transform, not something this command
attempts to detect or normalise in this version.

**Unit suffixes matter.** `NetlistSpiceReader` interprets a bare numeric
literal for a MOS `W`/`L` parameter as plain SI (metres) per the SPICE
standard, but an explicit `U` (or `UM`) suffix as micrometres — the
convention `klt extract`'s own writer always uses (`W=0.65U`). A reference
netlist authored without unit suffixes will parse with a `1e6`-scaled
device parameter that only ever mismatches or matches *relative to itself*
consistently, but produces a nonsensical absolute value in a
`device.property` mismatch's reported numbers. Always write reference
netlists with explicit unit suffixes on `W`/`L`.

## Negative controls: two independent corruptions

Per this issue's field notes, "LVS clean" alone is not evidence — a
mis-wired invocation that silently compares nothing also passes. A negative
control needs **two independent corruptions**, because they fail
independently:

- **topology** — short two nets that should be separate (`net.merged`), or
  split one net into two (`net.split`).
- **device parameters** — change one device's width without touching
  connectivity (`device.property`). This is the one that catches a compare
  that checks the connection graph and ignores parameters entirely.

`tests/test_lvs.py` exercises both independently, per this guidance.

**Also worth knowing**: `klayout.db.NetlistComparer`'s default net matching
does not lock onto layout net *labels* to constrain the compare — a pure
pin/device-topology isomorphism is accepted even when net names differ
structurally (only a top circuit's own declared *pin order* is a hard
anchor). A clean LVS run therefore does not by itself establish that a
top-level pinout is correct; something else (e.g. comparing `pins[]` names
directly) has to check pin order.

## Request

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
| `schema` | string | Request contract identifier (not required — `load_request` does not validate it, matching `klt sim`'s convention for user-authored input). |
| `engine` | string | Engine selector. Only `"klayout"` is supported; omit to use the default. |
| `layout` | object | The layout side — see "`layout` shapes" below. Exactly one of `file`/`netlist` is required. |
| `reference.netlist` | string, required | Path to the reference (schematic/golden) SPICE netlist, parsed via `NetlistSpiceReader`. Relative paths resolve against the request file's directory. |
| `reference.top` | string | The subcircuit in the reference netlist to compare. Omit when the reference file has exactly one top-level circuit (auto-selected, same convention as `layout.top`/`klt extract`'s `--top`). |
| `hints.same_nets` | array\<[string, string]\> | Optional `[layout_net_name, reference_net_name]` pairs — ties a named net in the layout's top circuit to a named net in the reference's top circuit. A name that does not resolve on the stated side is an application error (exit 1), not a silent no-op. |
| `hints.equivalent_pins` | object\<string, array\<[string, string]\>\> | Optional per-subcircuit swappable-pin groups, keyed by **reference**-side subcircuit name (`NetlistComparer.equivalent_pins` only accepts circuits from the netlist passed as `compare()`'s second argument, which is always the reference netlist in this command's `compare(layout, reference)` call order). |
| `options.keep_extracted` | boolean | When `layout.file` is given (inline extraction), retain the intermediate extracted netlist on disk at `<request-dir>/.klt/lvs/<top>.spice` and echo its path in `environment.extracted_netlist`. Default `false` (nothing is written to disk). |

### `layout` shapes

Extraction and compare are separable steps that also compose in one call:

- **Inline extraction** — `{"file": "design.gds", "deck": "sky130", "top": "ota_5t"}`. Runs `klt extract`'s core extraction (the same `extract_netlist_from_layout` function `klt extract` itself calls) against the named curated deck (`sky130`/`gf180mcu`), then compares the resulting in-memory netlist directly — no SPICE round-trip is required unless `options.keep_extracted` is set. `top` is optional (defaults to the layout's sole top cell, same as `klt extract --top`); `deck` is required in this shape.
- **Pre-extracted netlist** — `{"netlist": "design.spice", "top": "ota_5t"}`. Reads an existing extracted (or hand-written) SPICE netlist directly via `NetlistSpiceReader`, skipping extraction entirely. `top` is optional (defaults to the sole top circuit).

## Response

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
  "device_classes": ["nfet", "pfet"],
  "environment": {
    "engine": "klayout",
    "engine_version": "0.30.10",
    "layout_sha256": "1ab7...",
    "reference_sha256": "c93e...",
    "extracted_netlist": null
  },
  "mismatches": []
}
```

### Top-level fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema_version` | integer | Version of this command's JSON shape (starts at `1`; per-command, per [`docs/json-contract.md`](../json-contract.md)). |
| `engine` | string | Echo of the request's `engine` (or the default, `"klayout"`). |
| `layout` | string | Echo of `layout.file` or `layout.netlist`, exactly as provided. |
| `reference` | string | Echo of `reference.netlist`, exactly as provided. |
| `top` | string | The compared top circuit's name (the layout side's resolved top cell/circuit name). |
| `status` | `"match"` \| `"mismatch"` | `"match"` when `NetlistComparer.compare()` reports the netlists equivalent; `"mismatch"` otherwise. Never `"error"` in-band — a failed run does not emit this envelope at all (see "Exit codes"). |
| `mismatch_count` | integer | `len(mismatches)`. Can be nonzero even when `status` is `"match"` — a `severity: "warning"` entry (e.g. an ambiguity the comparer resolved on its own) does not change the verdict. |
| `category_counts` | object\<string, int\> | Per-category mismatch counts, keys sorted for determinism — the LVS analogue of `klt drc`'s `rule_counts`. |
| `counts` | object | Side-by-side `layout`/`reference`/`matched` tallies for `nets`, `devices`, `pins`. `matched` counts only a **strictly successful** pairing (e.g. a device paired with identical parameters and class) — a device paired despite a `device.property`/`device.class` mismatch is *not* counted as matched. |
| `device_classes` | array\<string\> \| `null` | The layout-side deck's `ExtractionDeck.device_classes` (see `klt extract`'s own field of the same name) — what that deck is structurally capable of recognising, not what this compare found. Present (currently always `["nfet", "pfet"]`) when `layout.file` + `layout.deck` (inline extraction) was given; `null` when `layout.netlist` (pre-extracted, no deck involved) was given instead. |
| `environment` | object | Reproducibility block: `engine`, `engine_version` (the installed `klayout` package version), `layout_sha256` (of `layout.file`, or of `layout.netlist` when no extraction ran), `reference_sha256` (of `reference.netlist`), `extracted_netlist` (path to the retained intermediate netlist when `options.keep_extracted` is set and `layout.file` was given; `null` otherwise). |
| `mismatches` | array\<object\> | One entry per structured mismatch — see below. Empty on a clean match; always present. |

### `mismatches[]` entries

Field-for-field the LVS counterpart of `klt drc`'s `violations[]`: a stable
`category` id (never renumbered/repurposed once shipped, exactly like a DRC
rule id), a curated human `description` (not raw engine log text), and the
objects involved.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `category` | string | One of `net.unmatched`, `net.merged`, `net.split`, `device.unmatched`, `device.class`, `device.property`, `pin.unmatched`, `topology`. |
| `severity` | `"error"` \| `"warning"` | `"error"` breaks equivalence; `"warning"` is informational and never changes `status`. Informational cases include an ambiguous net pairing the comparer resolved on its own (see `hints.same_nets` above), and a `topology` device-class-mismatch entry for a device class with zero actual instances on the side that registered it (e.g. an all-`nfet` layout compared against an all-`nfet` reference netlist that never mentions `pfet` — `klt extract` always registers both polarities' device classes even when only one is instantiated). A device-class mismatch where the class has one or more real instances still reports `"error"`. |
| `description` | string | Curated, human-readable explanation of this mismatch — never raw `NetlistComparer` log text (which is version-dependent and, per this repo's own testing, sometimes empty). |
| `side` | `"layout"` \| `"reference"` \| `"both"` | Which netlist the offending object(s) live on. |
| `net` | object \| `null` | `{"layout": <name\|null>, "reference": <name\|null>}` when a net is involved. |
| `device` | object \| `null` | `{"layout": <name\|null>, "reference": <name\|null>, "class": <string\|null>}` when a device is involved. |
| `property` | object \| `null` | `{"name": <string>, "layout": <value>, "reference": <value>}` for a `device.property` mismatch. `name` is `w_um`/`l_um` for the width/length parameters (matching `klt extract`'s own convention); every other declared device-class parameter is reported under its own lower-cased name. |

`mismatches` is sorted by `(category, side, device.layout, device.reference,
net.layout, net.reference)` (missing fields sort first) so repeated runs
against the same inputs produce identical, diff-clean output — the same
canonical-ordering guarantee `klt drc` makes about `violations`.

#### Net-merge/net-split classification (a documented simplification)

`NetlistComparer`'s own event stream does not label a net mismatch as
"merged" or "split" — it only reports individual net-pairing events. This
command distinguishes the three net categories from the *pattern* of events
in one compare run: an isolated, one-sided unmatched net (no counterpart on
the other side, and nothing else nearby) is `net.unmatched`. When a
one-sided leftover net on the **layout** side co-occurs with a differently-
named net pairing elsewhere in the same circuit, it is classified
`net.split` (a reference net's role divided across more layout nets than
expected); the mirror case (a leftover on the **reference** side) is
`net.merged`. This heuristic is verified against synthetic single-defect
merge/split fixtures in `tests/test_lvs.py`, but — like `klt extract`'s
documented curated-deck connectivity limits — is not a formal proof for
every possible multi-defect input; a compare run with several independent
net defects at once may classify some of them generically (`topology`)
rather than precisely.

## Exit codes

| Code | Meaning |
| ---- | ------- |
| `0` | LVS clean — layout matches reference (`status: "match"`). |
| `1` | Failed to run — bad request, unresolvable layout/reference input, unknown `--deck`, unparseable reference netlist, unsupported engine, or an engine error. |
| `2` | Usage error (missing argument, bad `--format` value) — from argparse. |
| `3` | Ran successfully; mismatches found (`status: "mismatch"`); the documented payload is on stdout. |

This is `klt lvs`'s direct analogue of `klt drc`'s `3` ("ran clean but found
findings") — LVS is a clean/dirty verdict like DRC, so it uses `drc`'s
two-outcome success split, **not** `klt sim`'s three-outcome `3`/`4` split
(which exists only because a corner sweep has a distinct "broken run"
outcome — a netlist comparer has no such third state).

On error (exit `1`), a concise message is written to **stderr** and nothing
is written to stdout. No Python traceback is printed.

- `--format text` (default): a plain-text line prefixed `klt lvs:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "lvs", "message": "reference netlist not found: missing.spice" } }
  ```

## Out of scope

`matched_group_id` (a geometric-matching check, deferred to a follow-up
epic per the phase 1 spike's section 4), any layout-vs-layout geometric
diffing, and loop closure through `klt sim` (Epic #153 phase 4) are all
explicitly out of scope for this command.
