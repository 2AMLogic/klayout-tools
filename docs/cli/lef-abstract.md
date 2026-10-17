# `klt lef-abstract`

Emit a LEF abstract — a `MACRO` block with `PIN` and `OBS` sections — from a
block's GDSII/OASIS layout plus its `klt socket-check` socket descriptor, so
OpenROAD can place it as a hard macro alongside standard cells (via
[`klt place-and-route`](place-and-route.md)'s `request.macros` field).
Capability A of [Epic #393](https://github.com/2AMLogic/klayout-tools/issues/393)
("mixed-signal as a first-class path"), issue #438.

```
klt lef-abstract <file> --socket <descriptor.json> --macro-name <name>
                  --cell-library <library> [--top <cell>] [--class <CLASS>]
                  [--symmetry "<axes>"] [--pdk <variant>] [--pdk-root <root>]
                  [-o/--output <path.lef>] [--require-power-pins]
                  [--format text|json]
```

- `<file>` — path to a GDSII (`.gds`) or OASIS (`.oas`) file. KLayout
  auto-detects the stream format on read.
- `--socket` — required. Path to a socket descriptor JSON file (see
  [`docs/schemas/socket.schema.json`](../schemas/socket.schema.json), the
  same descriptor `klt socket-check` validates a layout against). Not
  validated by argparse — a missing/malformed descriptor exits `1` with a
  clean error rather than argparse's usage-error exit `2`.
- `--macro-name` — required. Name for the emitted LEF `MACRO` block.
- `--cell-library` — required. **Not** the macro's own library — the
  standard-cell library (e.g. `sky130_fd_sc_hd`) whose tech LEF supplies the
  routing-layer/`SITE` header this command reads (see "Tech-LEF plumbing"
  below), resolved via the same PDK discovery `klt pdk find`/`klt
  synthesize`/`klt place-and-route` already use.
- `--top` — top cell to read when the stream has more than one; required in
  that case (mirrors `klt extract`'s own `--top`).
- `--class` — LEF `MACRO CLASS` value (default `BLOCK`, a hard macro).
- `--symmetry` — space-separated `SYMMETRY` axes (e.g. `"X Y"` or
  `"X Y R90"`); omit to emit no `SYMMETRY` statement.
- `--pdk` / `--pdk-root` — PDK variant/root overrides, same semantics as
  every other `klt` verb's PDK flags.
- `-o` / `--output` — output LEF path (default: `<macro-name>.lef`).
- `--require-power-pins` — exit `3` when the supply-pin verdict
  (`power_pins`, issue #2839) fails. Without it the verdict is reported only.
- `--format` — `text` (default, a human-readable summary) or `json`.

## Engine

Runs fully headless via the pip `klayout` package's native batch database
API (`klayout.db`) — no GUI, no Qt, no dependency on the standalone
`klayout` application binary. The tech-LEF header (`SITE`, routing-layer
`PITCH`/`OFFSET`/`DIRECTION`) is read by
[`klayout_tools.lef_header`](../design/sc-leflib-evaluation.md) — a small,
dependency-free text parser, **not** `klayout.db`'s own LEF importer (which
parses these same declarative attributes and then discards them, per that
document's survey) and **not** an external LEF-parsing library.

## Tech-LEF plumbing

`klt lef-abstract` resolves a tech LEF via `--cell-library` exactly as `klt
synthesize`/`klt place-and-route` resolve their liberty/LEF decks (the same
`find_pdk()`/`lef_files()` discovery, `src/klayout_tools/pdk.py`). Its
header — read via `klayout_tools.lef_header.read_lef_header` (issue #438's
tech-LEF header reader, the deferred-trigger resolution
[`docs/design/sc-leflib-evaluation.md`](../design/sc-leflib-evaluation.md)
called out) — supplies two things this command needs and `klayout.db`'s own
LEF importer does not expose:

- **The routing-layer set.** Only GDS shapes that map (via the open_pdks
  KLayout `.map` file, `assets.klayout/tech/<variant>.map` — the same file
  `klt place-and-route`'s own DEF→GDS merge resolves) to a tech-LEF layer
  whose `TYPE` is `ROUTING` become `OBS` geometry or a candidate `PIN`
  layer. Non-routing layers (wells, diffusion, poly) are not represented in
  the abstract at all — an *abstract* view describes what a router needs to
  avoid, not the full physical layout.
- **Each routing layer's own `WIDTH`.** The synthesized-pin-geometry
  fallback (see "Pins" below) uses it when a pin has no drawn geometry and
  no declared `width_um`/`height_um`.

An unresolvable PDK, tech LEF, or KLayout layer-map file is a clear
application error (exit `1`), matching `klt place-and-route`'s own posture.

## Socket descriptor to LEF MACRO translation

| Socket descriptor field | LEF translation |
| --- | --- |
| `outline` | The macro's `ORIGIN 0 0` + `SIZE` (`x1 - x0` BY `y1 - y0`). Every emitted coordinate is shifted by `(-x0, -y0)` into this macro-local frame — verified against a real sky130 standard-cell LEF and a real sky130 SRAM hard-macro LEF, both of which declare `ORIGIN 0.000 0.000` with already-macro-local coordinates. |
| `pins[]` | One `PIN <name> ... END <name>` per declared pin — see "Pins" below. |
| `reserved_layers[]` | **Not translated.** Describes layers reserved *for the integrator* (forbidden to this block) — the opposite concept from a macro's own `OBS` (this block's own obstruction geometry a router must avoid). |
| `budgets[]` | **Not translated.** Unverified interface budgets (resistance/capacitance/current) have no LEF representation. |
| (everything else drawn) | `OBS`, per routing-type LEF layer — see "Obstructions" below. |

### Pins

Each declared pin becomes a `PIN <name>` block:

- **`DIRECTION`/`USE`** — taken verbatim from the descriptor's own
  `pins[].direction`/`pins[].use` fields (added to
  [`docs/schemas/socket.schema.json`](../schemas/socket.schema.json) by this
  issue, LEF's own vocabulary: `direction` ∈ `INPUT`/`OUTPUT`/`INOUT`/
  `FEEDTHRU`, `use` ∈ `SIGNAL`/`ANALOG`/`POWER`/`GROUND`/`CLOCK`) when given.
  Otherwise a small, documented heuristic classifies by name: a name
  containing `VDD`/`VPWR`/`VCC`/`VPB` (whole alphanumeric token,
  case-insensitive) is `INOUT`/`POWER`; `VSS`/`VGND`/`GND`/`VNB` is
  `INOUT`/`GROUND`; anything else defaults to `INOUT`/`ANALOG` — `ANALOG` is
  LEF's own `USE` value for exactly this pin class, and this command's
  macros are analog blocks per Epic #393's own scope. There is no
  mechanical way to derive true signal direction from geometry alone.
- **`PORT` geometry** — real drawn shapes whose bounding box contains the
  declared `(x, y)` position (every such shape becomes its own `RECT`,
  mirroring how a real standard-cell LEF's multi-`RECT` pins look). The
  search covers **every** GDS `(layer, datatype)` pair that the resolved
  layer map maps to the same LEF layer *name* as the pin's declared
  `layer` — not just that single declared datatype (the declared datatype
  is always searched first). This matters because `klt socket-check` reads
  text labels off the pin's *exact* declared `layer`, which some PDKs
  (e.g. gf180mcu) put on a dedicated pin/label datatype distinct from the
  datatype the actual metal is drawn on (both resolving to the same LEF
  layer, e.g. `Metal1`) — searching every sibling datatype (mirroring how
  `OBS` geometry below is already unioned across every datatype sharing a
  LEF layer name) finds real geometry a single-datatype search would
  silently miss (issue #1614). Otherwise a **synthesized** placeholder
  box, centered at `(x, y)`, sized from the descriptor's own
  `width_um`/`height_um` when given, else the resolved tech-LEF routing
  layer's own `WIDTH`. Each pin's response entry reports which
  (`geometry_source`: `"drawn"` \| `"synthesized"` \| `"none"`) — never
  silently fabricating precision the layout doesn't actually have,
  mirroring `klt socket-check`'s own `"declared_unverified"` transparency
  convention for `budgets`. A pin whose declared `layer` does not resolve
  to a known routing LEF layer gets `"none"` (no `PORT` at all) and a
  `warnings[]` entry.

  A `geometry_source: "none"` pin is not itself an error here — this
  command still writes a structurally valid LEF — but if that pin is later
  wired into a real net and placed via `klt place-and-route`'s
  `request.macros`, OpenROAD's global router would fail with an opaque
  `GRT-0029` several stages into a real run. `klt place-and-route` itself
  now catches exactly that condition — a `PORT`-less macro pin the netlist
  actually wires up — with a clear, specific error before OpenROAD is
  invoked at all (see
  [that command's own "Hard-macro placement" section](place-and-route.md#hard-macro-placement-requestmacros)).
  This command's own `unroutable_pins[]` (below) is the same signal,
  promoted to a structured field so a caller composing `lef-abstract` ->
  `place-and-route` can check for it directly rather than relying on either
  `warnings[]` strings or `place-and-route`'s own downstream rejection.
  Discovered during Epic #393 Phase 3 (#456); see #464 for the full repro.

### Obstructions

Every shape drawn on a routing-type LEF layer, unioned per layer, clipped
to the outline, with the declared pin ports subtracted back out (so a
router is never told a declared connection point is also blocked). Emitted
as one `RECT` per resulting axis-aligned box, or `POLYGON` for anything
else (e.g. an L-shaped region left after subtracting a pin's port —
KLayout's own hole-resolution via `to_simple_polygon()` keeps this a single,
self-touching point list, an acceptable simplification for an abstract
view rather than a hole-aware multi-`RECT` decomposition).

## Response

```json
{
  "schema_version": 1,
  "file": "analog_block.gds",
  "socket": "analog_block.socket.json",
  "output": "analog_block.lef",
  "macro_name": "analog_block",
  "class": "BLOCK",
  "site": null,
  "symmetry": [],
  "size_um": { "width": 5.0, "height": 5.0 },
  "cell_library": "sky130_fd_sc_hd",
  "tech_lef": "/abs/path/sky130_fd_sc_hd__nom.tlef",
  "pins": [
    { "name": "VDD", "direction": "INOUT", "use": "POWER", "layer": "met1", "gds_layer": [68, 20], "geometry_source": "drawn", "rects_um": [[0.0, 2.0, 0.2, 3.0]] },
    { "name": "VOUT", "direction": "INOUT", "use": "ANALOG", "layer": "li1", "gds_layer": [67, 20], "geometry_source": "drawn", "rects_um": [[2.0, 0.0, 3.0, 0.2]] }
  ],
  "pin_count": 2,
  "unroutable_pins": [],
  "obs": [{ "layer": "met1", "shape_count": 1 }],
  "obs_shape_count": 1,
  "warnings": [],
  "self_check": { "status": "pass", "findings": [], "missing_macros": [], "unexpected_macros": [] },
  "power_pins": {
    "status": "fail",
    "findings": [{ "code": "no_ground_pin", "pin": null, "message": "no PIN with USE GROUND in the abstract" }],
    "pins": [{ "name": "VDD", "use": "POWER", "use_read_back": "POWER", "layer": "met1", "geometry_source": "drawn" }]
  },
  "provenance": {
    "klt_version": "0.1.0",
    "klayout_version": "0.30.10",
    "pdk": { "name": "sky130A", "source": "PDK_ROOT environment variable", "version": "<stamp>" },
    "deck": { "name": "sky130_fd_sc_hd", "content_hash": "sha256:<hex>" },
    "input": { "content_hash": "sha256:<hex>", "role": "layout" }
  }
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | integer | Per-command version, per `docs/json-contract.md`. |
| `file` / `socket` | string | Echo of the input paths exactly as provided. |
| `output` | string | The LEF path actually written. |
| `macro_name` | string | Echo of `--macro-name`. |
| `class` | string | Echo of `--class` (default `"BLOCK"`). |
| `site` | null | Always `null` in this version — this command never declares a `SITE` reference (real hard-macro LEFs, e.g. sky130's own SRAM macros, declare none either); reserved for a future explicit `--site` flag. |
| `symmetry` | array\<string\> | Echo of `--symmetry` (split on whitespace), or `[]`. |
| `size_um` | object | `{width, height}` — the outline's own extent. |
| `cell_library` | string | Echo of `--cell-library`. |
| `tech_lef` | string | The resolved tech LEF path this run's routing-layer/`SITE` header came from. |
| `pins` | array\<object\> | One entry per descriptor pin, in name-sorted order: `{name, direction, use, layer, gds_layer, geometry_source, rects_um}`. `layer` is the resolved LEF layer name, or `null` when unresolvable. `gds_layer` (issue #2839, additive) is the descriptor's declared `[layer, datatype]` for the pin, so a consumer can match the PIN to the same pin's `klt socket-check` port facts. `rects_um` is `[]` for `geometry_source: "none"`. |
| `pin_count` | integer | `len(pins)`. |
| `unroutable_pins` | array\<object\> | `{name, layer: [gds_layer, gds_datatype]}` for every pin with `geometry_source: "none"` — a programmatically-checkable echo of the same condition (issue #464), so a caller composing this command's output into `klt place-and-route`'s `request.macros` can check it directly instead of grepping `warnings[]`. `[]` on a run with no such pins. |
| `obs` | array\<object\> | `{layer, shape_count}` per LEF layer with obstruction geometry, layer-name sorted. |
| `obs_shape_count` | integer | Sum of every `obs[].shape_count`. |
| `warnings` | array\<string\> | Non-fatal notes: a pin fell back to synthesized geometry, or a pin's layer did not resolve to a known routing LEF layer. `[]` on a run with no such notes. |
| `self_check` | object | Structural self-check of the LEF file **read back from disk** after writing (issue #2769) — see "Structural self-check" below. `{status, findings, missing_macros, unexpected_macros}`; `status` is `"pass"` or `"fail"` and is the verdict of record for whether `output` may be consumed. |
| `power_pins` | object | Issue #2839 (additive): the supply-pin verdict over the same read-back bytes — see "Supply-pin verdict" below. Affects the exit code only with `--require-power-pins`. |
| `provenance` | object | The shared envelope block (`docs/json-contract.md`). `deck` names `--cell-library` and hashes the resolved tech LEF; `pdk` is `find_pdk()`'s resolved triple; `input` is the content hash of `<file>`. |

## Structural self-check

After writing the LEF, the command reads the file back from disk and runs a
small structural validator over **the bytes actually written** — not over
its own in-memory pin data — against exactly one expected macro
(`--macro-name`) whose expected pins are the socket descriptor's own
declared `pins[].name`s. The result is the additive `self_check` object:

```json
"self_check": {
  "status": "fail",
  "findings": [
    { "code": "missing_pin", "message": "declared pin VOUT is missing from MACRO analog_block", "line": 5, "macro": "analog_block", "pin": "VOUT" }
  ],
  "missing_macros": [],
  "unexpected_macros": []
}
```

| Field | Type | Description |
| --- | --- | --- |
| `status` | string | `"pass"` iff `findings` is empty, else `"fail"`. |
| `findings` | array\<object\> | Every defect found — all checks run, so one fault never hides another. Each entry always has the same five keys: `code` (stable, machine-readable; table below), `message` (prose, not part of the contract), `line` (1-based line in the emitted file, or `null`), `macro` / `pin` (the affected names, or `null` when not applicable). |
| `missing_macros` | array\<string\> | Expected macro names not declared in the file, sorted. |
| `unexpected_macros` | array\<string\> | Declared macro names that were not expected, sorted. |

| `code` | Meaning |
| --- | --- |
| `missing_version` | No library-level `VERSION <value> ;` statement. |
| `missing_end_library` | No `END LIBRARY`. |
| `content_after_end_library` | Non-comment content after `END LIBRARY`. |
| `unclosed_block` | A `MACRO`/`PIN`/`PORT`/`OBS` block never closed — at end of file (truncation), at `END LIBRARY`, or skipped over by an `END` naming an outer block. |
| `end_name_mismatch` | `END <name>` that does not match the innermost open block (a `MACRO`/`PIN` with a different name, or an anonymous `PORT`/`OBS`). |
| `unnamed_end` | Anonymous `END` where `END <name>` was required to close a `MACRO`/`PIN`. |
| `orphan_end` | `END` with no open block. |
| `misplaced_block` | Block in the wrong parent: a nested `MACRO`, a `PIN`/`OBS` not directly inside a `MACRO`, a `PORT` not directly inside a `PIN`. |
| `missing_name` | `MACRO`/`PIN` with no name on its line. |
| `unterminated_statement` | A statement with no `;` before end of file or before the next line starting with a block keyword. |
| `duplicate_macro` / `duplicate_pin` | A macro, or a pin within one macro, declared more than once. |
| `missing_macro` / `unexpected_macro` | Macro name set differs from the expected set (exact, case-sensitive comparison). |
| `missing_pin` | A declared pin is not inside its macro (`macro` names the containing macro; `line` is that macro's `MACRO` line). |
| `unexpected_pin` | A pin inside an expected macro that is not one of its declared pins (with `missing_pin`, this is how a pin placed in the wrong macro surfaces). |

Codes are additive: a consumer must tolerate codes it does not recognise
and gate on `status`.

**Scope and limits.** The validator understands only the LEF subset this
command emits: library-level `;`-terminated statements, `MACRO <name>` …
`END <name>`, `PIN <name>` … `END <name>`, anonymous `PORT` … `END` and
`OBS` … `END`, and a final `END LIBRARY`. `#` comments and ordinary
whitespace (indentation, tabs, blank lines, a `;` glued to a token) are
tolerated; identifiers are compared exactly, never case-folded. A name
after `MACRO`/`PIN`/`END` must sit on the keyword's line. It is not a
general LEF parser and checks no geometry, layer names or statement
semantics. **A structural pass does not prove routability or P&R
acceptance.** A `PORT`-less pin (`geometry_source: "none"`, listed in
`unroutable_pins`) and a synthesized pin are structurally present and keep
their `warnings` — neither is a structural failure.

On `"fail"` the LEF is **left on disk** for diagnosis and the normal report
is still emitted; callers must gate consumption of `output` on
`self_check.status` (or the exit code below). Failing to read the written
file back at all is an application error (exit `1`), not a finding.

## Supply-pin verdict (`power_pins`, issue #2839)

**Provisional** — the abstract half of the proposed T1 item 11
port-accessibility amendment (2AMLogic/product#134, unratified). Graded on
the LEF **read back from disk**, like `self_check`: each supply pin's `USE`
is taken from the written file's own `USE` statement, not from in-memory
data.

| Field | Type | Description |
| --- | --- | --- |
| `status` | string | `"pass"` iff `findings` is empty. |
| `findings` | array\<object\> | `{code, pin, message}`; `pin` is `null` for a set-level finding. |
| `pins` | array\<object\> | Every supply pin (`use` `POWER`/`GROUND`): `{name, use, use_read_back, layer, geometry_source}`. |

| `code` | Meaning |
| --- | --- |
| `no_power_pin` / `no_ground_pin` | No pin is `USE POWER` / `USE GROUND`. |
| `use_not_written` | The written file's `USE` for a supply pin is absent or differs. |
| `port_not_drawn` | A supply pin's `PORT` is a synthesized placeholder or missing (`geometry_source` not `"drawn"`) — the amendment requires the abstract's power `PORT` to be drawn layout geometry. |

`klt signoff`'s T1 item 11 consumes this envelope (with a `klt socket-check`
envelope for the GDS side) to check that every declared supply has a LEF
`PIN` that agrees with an edge-accessible GDS port — see
[`klt signoff`](signoff.md)'s "Item 11 port accessibility".

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The LEF abstract was written and `self_check.status` is `"pass"`. |
| `1` | Failed to run — bad layout/socket descriptor, an ambiguous/missing top cell, an unresolvable PDK/tech-LEF/layer-map for `--cell-library`, an unwritable output path, or the written file could not be read back for its self-check. Error envelope on stderr; no report. |
| `2` | Usage error (missing argument, bad `--format` value) — from argparse. |
| `3` | The LEF abstract was written and the full report emitted, but `self_check.status` is `"fail"` (issue #2769). Do not consume the file. Also returned, with `--require-power-pins` only, when `power_pins.status` is `"fail"` (issue #2839). |

A pin resolving to synthesized (rather than drawn) geometry, or to no
`PORT` at all, is reported in `warnings`/`unroutable_pins` and never by
itself produces exit `3`.

Per [`docs/json-contract.md`](../json-contract.md#exit-codes)'s additive
contract, a future release may still add a code above `2`; a consumer
should gate on the payload's own fields rather than assume the codes above
are exhaustive forever.

## Round-tripping through `klt place-and-route`

The emitted LEF is designed to be handed straight to
[`klt place-and-route`](place-and-route.md)'s `request.macros[].lef` field,
which `read_lef`s it before `read_verilog`/`link_design` and fixes it at a
caller-given location via OpenROAD's own `place_macro` — see that
document's "Hard-macro placement" section. Reading the resulting DEF back
(directly, or via this repo's own `klayout.db`) confirms the placer
respected the declared `OBS` regions: no other placed instance or routed
wire should overlap them.
