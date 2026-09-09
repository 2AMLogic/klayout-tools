# `klt clip`

Write a subset of an existing GDSII/OASIS stream back out as its own
top-cell stream -- either a micrometre bounding-box region, or a named
cell's full subtree.

```
klt clip <file> (--region <json> | --cell <name>) -o <output> [--top <cell>] [--format text|json]
```

- `<file>` -- path to a GDSII (`.gds`) or OASIS (`.oas`) file. KLayout
  auto-detects the stream format on read; the extension is not authoritative.
- `--region` / `--cell` -- **exactly one required** (argparse enforces this
  as a usage error, exit `2`, if both or neither are given). Selects one of
  the two modes below.
- `--top` -- optional, **region-clip mode only**. The top cell to clip
  `--region` against when the stream has more than one; omit to require
  exactly one top cell (a stream with more than one top cell and no `--top`
  exits `1`). Ignored (leave unset) with `--cell` -- cell-extraction mode
  resolves its source cell by name directly, regardless of top-cell status.
- `-o`/`--output` -- **required**. Output GDS/OASIS path for the
  clipped/extracted stream. KLayout picks the write format from the
  extension.
- `--format` -- `text` (default, a human-readable summary) or `json`.

## Region-clip mode (`--region`)

`--region` is an inline JSON array of four micrometre coordinates `[left,
bottom, right, top]` (e.g. `'[0, 0, 100, 100]'`) -- the same shape `klt
ring-check`/`klt components`'s own `--region` already accept. `right > left`
and `top > bottom` are enforced by the shared parser.

Every layer of the resolved top cell (`--top`, or the stream's sole top
cell) is **flattened** (the same `region()`/`texts()` idiom `klt
components` uses -- every instance's shapes are walked recursively via
`Cell.begin_shapes_rec`, so a shape several levels deep in the hierarchy is
included if its flattened position lands in the window) and intersected
with the clip window (`clip_box()`). The result -- shapes plus text labels,
per layer -- is written into a single fresh top cell named `CLIP` in the
output stream. Region-clip mode always produces exactly one flat cell; it
cannot preserve source hierarchy, since flattening is inherent to how it
walks the source.

```bash
klt clip design.gds --region '[0, 0, 50, 50]' -o device.gds
```

### Errors specific to region-clip mode

- **Zero-area bbox in micrometres** (`right <= left` or `top <= bottom`) --
  rejected by the shared `--region` parser before `klt clip` even runs
  (exit `1`, `"--region must have right > left and top > bottom, got [...]"`).
- **Degenerate at the input's own database unit** -- a region with positive
  area in micrometres can still round to zero width or height once converted
  to the layout's integer database units (e.g. a window narrower than one
  `dbu` on a coarse-grid stream). Writing a deliberately empty output would
  be indistinguishable from a caller's mistake, so this is a distinct error
  (exit `1`, `"...rounds to zero width or height at this layout's dbu=..."`).
- **Region matches no geometry** -- the window is well-formed and non-
  degenerate at the input's dbu, but no shape on any layer, in the resolved
  top cell's flattened hierarchy, falls inside it (exit `1`,
  `"--region [...] matches no geometry in '<file>' (top cell '<name>')"`).
  Distinctly worded from the degenerate-window case above, so a caller can
  tell "your window itself is broken" apart from "your window is fine but
  nothing is there."
- **Ambiguous top cell** -- the stream has more than one top cell and no
  `--top` was given (exit `1`, same wording as `klt lef-abstract`/`klt
  economy`'s own ambiguous-top-cell error via `resolve_top_cell()`).

## Cell-extraction mode (`--cell`)

`--cell` names a cell **anywhere in the stream** -- a top cell or a nested
one, resolved by name only (`Layout.cell(name)`), independent of where (or
how many times) it happens to be instantiated. Its full subtree -- every
descendant cell definition it calls, at every depth, with its own
instance/array structure intact -- is copied verbatim
(`kdb.Cell.copy_tree()`, the same primitive `klt gen-compose`'s
block-placement step and the abstract-cell "shadow" duplication inside `klt
lef-abstract`'s netlist path already use) into a fresh top cell of the same
name in the output stream. Unlike region-clip, hierarchy is preserved
exactly: a macro with nested standard-cell instances comes out with the same
nesting, not flattened.

```bash
klt clip design.gds --cell SUBCELL_ADC -o subcell_adc.gds
```

### Errors specific to cell-extraction mode

- **Nonexistent cell name** -- `--cell` does not match any cell definition
  in the stream (exit `1`, `"cell '<name>' not found in '<file>'"`).

An existing cell with **no shapes anywhere in its subtree** is not an error
-- there is nothing ambiguous about "this cell is empty," unlike a region
matching no geometry (which could equally be a caller mistake about where
the window is).

## Determinism

Both modes write through the shared `write_layout()` helper -- the same
timestamp-suppressing `SaveLayoutOptions` every `klt` generator/drawing verb
uses (issue #320) -- so two runs against the same input with the same
`--region`/`--cell` produce byte-identical output.

## Why a separate verb, not a `--write` flag on `klt ring-check`/`klt components`

`klt ring-check --region` and `klt components --region` already accept the
same window shape, but only ever use it to *restrict analysis* -- neither
verb writes the clipped subset back out (issue #1603, item 2). Bolting a
write path onto either would conflate "check/report on this window" with
"extract this window as its own artifact," two genuinely different
operations with different failure modes (a ring-check's `--region` can be
empty and simply report "no ring here"; `klt clip`'s `--region` treats an
empty match as an error, since writing nothing was never the point of
running it). `klt clip` also adds the named-cell-subtree mode, which has no
analogue in either check verb at all.

## Engine

`klt clip` runs fully headless via the pip `klayout` package's native batch
database API (`klayout.db`) -- `Region`/`Texts` flattening and boolean
primitives for region-clip mode, `Cell.copy_tree()` for cell-extraction mode
-- no GUI, no Qt, and no dependency on the standalone `klayout` application
binary.

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
  "output": "device.gds",
  "mode": "region",
  "cell": null,
  "region_um": [0.0, 0.0, 50.0, 50.0],
  "top": "TOP",
  "dbu_um": 0.001,
  "cell_count": 1,
  "shape_count": 12
}
```

```json
{
  "schema_version": 1,
  "file": "design.gds",
  "output": "subcell_adc.gds",
  "mode": "cell",
  "cell": "SUBCELL_ADC",
  "region_um": null,
  "top": null,
  "dbu_um": 0.001,
  "cell_count": 4,
  "shape_count": 37
}
```

### Top-level fields

| Field            | Type                | Description                                                                 |
| ---------------- | ------------------- | ----------------------------------------------------------------------------- |
| `schema_version` | integer             | Version of this command's JSON shape (starts at `1`).                       |
| `file`           | string              | The input layout path exactly as provided on the command line.              |
| `output`         | string              | The output layout path exactly as provided on the command line (`-o`).      |
| `mode`           | `"region"` \| `"cell"` | Which mode produced this output.                                          |
| `cell`           | string \| null      | The `--cell` value, or `null` in region-clip mode.                          |
| `region_um`      | array\<number\> \| null | The `--region` window `[left, bottom, right, top]` in micrometres, or `null` in cell-extraction mode. |
| `top`            | string \| null      | The top cell region-clip actually clipped against, or `null` in cell-extraction mode (where `--top` is not used). |
| `dbu_um`         | number (float)      | The input layout's database unit in micrometres, same semantics as `klt layers`. |
| `cell_count`     | integer             | Cells written to the output stream: always `1` for region-clip (one flat `CLIP` cell); for cell-extraction, `1` plus every distinct descendant cell definition the copied subtree pulls in. |
| `shape_count`    | integer             | Total shapes (regions + text labels) written across every layer of the output stream. |

## Exit codes

| Code | Meaning                                                              |
| ---- | -------------------------------------------------------------------- |
| `0`  | Ran successfully -- the clipped/extracted stream was written.        |
| `1`  | Failed to run -- bad layout file, unknown `--cell`, malformed/degenerate `--region`, a `--region` matching no geometry, an ambiguous top cell with no `--top`, or an unwritable output path. |
| `2`  | Usage error (missing argument, both or neither of `--region`/`--cell`, missing `-o`, bad `--format` value) -- from argparse. |

On error (exit `1`), a concise message is written to **stderr** and nothing
is written to stdout, and no output file is written. No Python traceback is
printed.

- `--format text` (default): a plain-text line prefixed `klt clip:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "clip", "message": "cell 'NOPE' not found in 'design.gds'" } }
  ```
