# `klt ring-check`

Assert that a caller-specified **layer set forms a single closed annulus** --
a guard ring, tap ring, or seam moat -- and report the break location when it
does not. Purely geometric: the shapes on the given layers are merged and the
result must be exactly one polygon with exactly one hole.

```
klt ring-check <file> --layers <json> [--region <json>] [--top <cell>] [--format text|json]
```

- `<file>` -- path to a GDSII (`.gds`) or OASIS (`.oas`) file. KLayout
  auto-detects the stream format on read; the extension is not authoritative.
- `--layers` -- **required**. The ring's layer set, as a path to a JSON file
  or an inline JSON array of `[layer, datatype]` pairs (e.g.
  `'[[22, 0], [34, 0]]'`) -- the same "path-or-inline-JSON array of
  `[layer, datatype]` pairs" convention `klt precheck --allowed-layers` uses.
  The shapes on **all** these layers are merged together, and their union must
  form the annulus (a ring drawn as diffusion + contact + metal is given as
  all three pairs). Not validated by argparse -- an empty or malformed value
  exits `1` with a clean error rather than argparse's usage-error exit `2`.
- `--region` -- optional. A clip window as an inline JSON array of four
  micrometre coordinates `[left, bottom, right, top]` (e.g. `'[0, 0, 100,
  100]'`), used to isolate one ring in a stream that contains other geometry
  on the same layers. Omit to check every shape on the layer set (the common
  case). `right > left` and `top > bottom` are enforced.
- `--top` -- optional. The top cell to check when the stream has more than
  one; omit to check every top cell. A named cell that is absent exits `1`.
- `--format` -- `text` (default, a human-readable summary) or `json`.

## Why this is a check `klt drc` and `klt lvs` cannot make

A guard/tap ring is a closed annulus of diffusion + contacts + metal tied to a
supply net, drawn to collect substrate current between a noisy and a quiet
domain. Its correctness is entirely geometric+connective: it must be
**continuous** all the way around. Neither `klt drc` nor `klt lvs` can verify
that (issue #303):

- **`klt drc` cannot see it.** The curated decks have no rule relating a ring's
  shape to anything. Cut a gap into one segment and every width, spacing,
  enclosure, and density rule still passes -- the remaining geometry is
  perfectly legal, it just is not a ring any more.
- **`klt lvs` cannot see it either.** A ring's taps are body/substrate
  contacts, and the curated decks synthesize body nets, so the ring's tie is
  never compared against a reference. Tied correctly, tied wrong, or tied to
  nothing -- LVS reports `match` regardless.

The load-bearing subtlety: **a plain connectivity/merge check does not catch a
single break.** A ring is redundant by construction, so one break still leaves
one connected group. The assertion has to be specifically "merges to exactly
one polygon with exactly one hole" (a proper annulus), not merely "the shapes
are connected". `klt ring-check` makes that annulus assertion mechanical and
records it on the same JSON stream the rest of the flow is checked against.

This check covers only ring **continuity** (item 1 of issue #303). The
complementary **tie** assertion (is the ring on the intended net?) needs
extracted connectivity and is a separate, larger piece deferred to follow-on
work -- see issue #303's "Suggested shapes" (2) and (3).

## Why a separate verb, not a new `klt drc` rule

`klt drc` is **deck-driven**: every rule is a fixed `(layer, other_layer,
check, threshold_dbu)` tuple sourced from a curated PDK deck and dispatched by
KLayout's pairwise `Region.*_check` primitives. A ring-continuity assertion is
neither deck-authored nor a pairwise threshold check -- it takes a
*caller-specified* layer set (and optional region) and asserts a *shape* (one
polygon, one hole). That is the same "geometric check driven by a
caller-supplied descriptor, not a PDK deck" shape `klt socket-check` and `klt
precheck` already have, so it lands as a sibling verb rather than a rule wedged
into the deck vocabulary. It emits the same `violations[]` envelope, so `klt
report` aggregates it with no ring-check-specific code.

## Engine

`klt ring-check` runs fully headless via the pip `klayout` package's native
batch database API (`klayout.db`) -- `Region` boolean/merge/size primitives
only, no GUI, no Qt, and no dependency on the standalone `klayout` application
binary. No PDK is resolved and no rule deck is read: the check is purely
geometric.

## How the annulus assertion works

For each checked top cell, the shapes on every `--layers` pair are unioned into
one `Region`, optionally clipped to `--region`, and merged. The merged region
**passes** (`status: "continuous"`) only when it is exactly **one polygon with
exactly one hole** -- a proper closed annulus. Every other outcome is a
`status: "broken"` violation:

| `kind`        | Geometry                                             | What it means |
| ------------- | ---------------------------------------------------- | ------------- |
| `empty`       | no geometry on the layer set (in the clip window)    | there is no ring to verify |
| `fragmented`  | more than one disjoint polygon                       | the ring is in pieces (e.g. two arcs) -- one violation per fragment |
| `gap`         | one polygon, no enclosed hole, closing re-forms one  | a break in one segment opened the annulus; the reported `bbox` is the gap |
| `no_hole`     | one polygon, no enclosed hole, no closing re-forms one | a solid, hole-less region (a filled plate drawn where a ring was meant to be), not a broken ring |
| `extra_holes` | one polygon with more than one hole                  | not a simple annulus (e.g. two separate holes) |

### Locating a gap

A gap cut into one segment merges the inner opening into the outside, so the
shapes still form **one connected polygon with no enclosed hole** -- exactly
the case a connectivity check misses. The gap is located by a morphological
*closing* (dilate by `r`, erode by `r`): the smallest `r` for which the
closing re-forms an enclosed hole has bridged the gap, and `closed - region` is
exactly the filled gap, whose bounding box is reported. Because a closing of
radius `r` fills only channels narrower than `2r` while preserving the outer
extent and any hole wider than `2r`, the narrowest missing channel (the gap) is
bridged first, before the ring's own -- necessarily wider -- central opening. A
genuinely solid region never re-forms a hole under any radius, so it is
reported as `no_hole` against the whole shape rather than a spurious gap
location.

## JSON schema (the contract)

**JSON is the API.** Human-readable text output is a courtesy; the JSON schema
below is the stable contract. Per the project's rules, **breaking (renaming,
removing, or retyping) a field is a breaking change**. New fields may be added
without breaking the contract, so consumers should ignore unknown fields. See
[`docs/json-contract.md`](../json-contract.md) for the envelope shared across
all `klt` commands (`schema_version`, error shape, exit codes).

Like `klt drc` (and unlike `klt socket-check`), `klt ring-check` reports
`bbox`/`polygon` coordinates in **database units** -- the `violations[]` entry
shape is deliberately identical to `klt drc`'s, so the same tooling renders
both.

```json
{
  "schema_version": 1,
  "file": "guard_ring.gds",
  "layers": [[22, 0], [34, 0]],
  "region_um": null,
  "dbu_um": 0.005,
  "status": "broken",
  "violation_count": 1,
  "violations": [
    {
      "rule": "ring.continuity",
      "description": "ring is broken: a gap in one segment opened the annulus, merging the inner opening into the outside (one polygon, no enclosed hole)",
      "check": "ring_continuity",
      "kind": "gap",
      "layer": "22/0+34/0",
      "cell": "GUARD_RING",
      "polygon_count": 1,
      "hole_count": 0,
      "bbox": { "left": 16000, "bottom": 8000, "right": 20000, "top": 12000 },
      "polygon": [[16000, 8000], [16000, 12000], [20000, 12000], [20000, 8000]]
    }
  ]
}
```

### Top-level fields

| Field             | Type                          | Description                                                                 |
| ----------------- | ----------------------------- | --------------------------------------------------------------------------- |
| `schema_version`  | integer                       | Version of this command's JSON shape (starts at `1`).                       |
| `file`            | string                        | The input layout path exactly as provided on the command line.              |
| `layers`          | array\<[int, int]\>           | The `--layers` set, as `[layer, datatype]` pairs, in the order given.       |
| `region_um`       | array\<number\> \| null       | The `--region` clip window `[left, bottom, right, top]` in micrometres, or `null` when omitted. |
| `dbu_um`          | number (float)                | The input layout's database unit in micrometres, same semantics as `klt layers`. |
| `status`          | `"continuous"` \| `"broken"`  | `"continuous"` iff every checked top cell's merged region is exactly one polygon with one hole. |
| `violation_count` | integer                       | `len(violations)`.                                                          |
| `violations`      | array\<object\>               | One entry per break; `[]` when `status` is `"continuous"`. Sorted by `(cell, kind, bbox.left, bbox.bottom, bbox.right, bbox.top)` for deterministic output. |

### `violations[]` entries

| Field           | Type                    | Description                                                                 |
| --------------- | ----------------------- | --------------------------------------------------------------------------- |
| `rule`          | string                  | Always `"ring.continuity"` -- a stable id, never renumbered once shipped.   |
| `description`   | string                  | Human-readable explanation of this break.                                   |
| `check`         | string                  | Always `"ring_continuity"` -- mirrors `klt drc`'s `check` field.            |
| `kind`          | string                  | `"empty"`, `"fragmented"`, `"gap"`, `"no_hole"`, or `"extra_holes"` -- see [the table above](#how-the-annulus-assertion-works). |
| `layer`         | string                  | The merged layer set, `"<l>/<d>+<l>/<d>..."`.                               |
| `cell`          | string                  | The top cell this break was found in.                                       |
| `polygon_count` | integer                 | Polygons in the merged region (`1` for a proper annulus).                   |
| `hole_count`    | integer                 | Holes across those polygons (`1` for a proper annulus).                     |
| `bbox`          | object                  | `{left, bottom, right, top}` in database units -- the break location (the gap for `kind: "gap"`, the fragment for `"fragmented"`, the whole shape otherwise). |
| `polygon`       | array\<[int, int]\> \| null | The break shape's hull points in database units, or `null` (the `empty` case). |

## Exit codes

| Code | Meaning                                                              |
| ---- | -------------------------------------------------------------------- |
| `0`  | Ran successfully -- the ring is continuous.                          |
| `1`  | Failed to run -- bad layout file, empty/invalid `--layers`, unknown `--top` cell, or malformed `--region`. |
| `2`  | Usage error (missing argument, bad `--format` value) -- from argparse. |
| `3`  | Ran successfully, the ring is broken.                               |

On error (exit `1`), a concise message is written to **stderr** and nothing is
written to stdout. No Python traceback is printed.

- `--format text` (default): a plain-text line prefixed `klt ring-check:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "ring-check", "message": "file not found: guard_ring.gds" } }
  ```
