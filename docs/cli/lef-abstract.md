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
                  [-o/--output <path.lef>] [--format text|json]
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
- **`PORT` geometry** — real drawn shapes when the layout actually has
  metal on the pin's declared `layer` whose bounding box contains the
  declared `(x, y)` position (every such shape becomes its own `RECT`,
  mirroring how a real standard-cell LEF's multi-`RECT` pins look).
  Otherwise a **synthesized** placeholder box, centered at `(x, y)`, sized
  from the descriptor's own `width_um`/`height_um` when given, else the
  resolved tech-LEF routing layer's own `WIDTH`. Each pin's response entry
  reports which (`geometry_source`: `"drawn"` \| `"synthesized"` \|
  `"none"`) — never silently fabricating precision the layout doesn't
  actually have, mirroring `klt socket-check`'s own
  `"declared_unverified"` transparency convention for `budgets`. A pin
  whose declared `layer` does not resolve to a known routing LEF layer gets
  `"none"` (no `PORT` at all) and a `warnings[]` entry.

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
    { "name": "VDD", "direction": "INOUT", "use": "POWER", "layer": "met1", "geometry_source": "drawn", "rects_um": [[0.0, 2.0, 0.2, 3.0]] },
    { "name": "VOUT", "direction": "INOUT", "use": "ANALOG", "layer": "li1", "geometry_source": "drawn", "rects_um": [[2.0, 0.0, 3.0, 0.2]] }
  ],
  "pin_count": 2,
  "unroutable_pins": [],
  "obs": [{ "layer": "met1", "shape_count": 1 }],
  "obs_shape_count": 1,
  "warnings": [],
  "provenance": {
    "klt_version": "0.1.0",
    "klayout_version": "0.30.10",
    "pdk": { "name": "sky130A", "source": "PDK_ROOT environment variable", "version": "<stamp>" },
    "deck": { "name": "sky130_fd_sc_hd", "content_hash": "sha256:<hex>" },
    "input": { "content_hash": "sha256:<hex>" }
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
| `pins` | array\<object\> | One entry per descriptor pin, in name-sorted order: `{name, direction, use, layer, geometry_source, rects_um}`. `layer` is the resolved LEF layer name, or `null` when unresolvable. `rects_um` is `[]` for `geometry_source: "none"`. |
| `pin_count` | integer | `len(pins)`. |
| `unroutable_pins` | array\<object\> | `{name, layer: [gds_layer, gds_datatype]}` for every pin with `geometry_source: "none"` — a programmatically-checkable echo of the same condition (issue #464), so a caller composing this command's output into `klt place-and-route`'s `request.macros` can check it directly instead of grepping `warnings[]`. `[]` on a run with no such pins. |
| `obs` | array\<object\> | `{layer, shape_count}` per LEF layer with obstruction geometry, layer-name sorted. |
| `obs_shape_count` | integer | Sum of every `obs[].shape_count`. |
| `warnings` | array\<string\> | Non-fatal notes: a pin fell back to synthesized geometry, or a pin's layer did not resolve to a known routing LEF layer. `[]` on a run with no such notes. |
| `provenance` | object | The shared envelope block (`docs/json-contract.md`). `deck` names `--cell-library` and hashes the resolved tech LEF; `pdk` is `find_pdk()`'s resolved triple; `input` is the content hash of `<file>`. |

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The LEF abstract was written successfully. |
| `1` | Failed to run — bad layout/socket descriptor, an ambiguous/missing top cell, an unresolvable PDK/tech-LEF/layer-map for `--cell-library`, or an unwritable output path. |
| `2` | Usage error (missing argument, bad `--format` value) — from argparse. |

**No exit code `3`.** Unlike `klt socket-check`, this command has no
pass/fail concept of its own — a pin resolving to synthesized (rather than
drawn) geometry is reported in `warnings`, never treated as a failure.

## Round-tripping through `klt place-and-route`

The emitted LEF is designed to be handed straight to
[`klt place-and-route`](place-and-route.md)'s `request.macros[].lef` field,
which `read_lef`s it before `read_verilog`/`link_design` and fixes it at a
caller-given location via OpenROAD's own `place_macro` — see that
document's "Hard-macro placement" section. Reading the resulting DEF back
(directly, or via this repo's own `klayout.db`) confirms the placer
respected the declared `OBS` regions: no other placed instance or routed
wire should overlap them.
