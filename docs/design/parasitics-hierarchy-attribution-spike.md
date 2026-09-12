# Spike: attributing `--parasitics` R/C to top-cell-drawn vs. instance-inherited geometry

**Status:** design spike (issue #1702), decided. **Outcome:** implementation
is warranted, in the narrow shape — a **top-cell-only split of the ground
R/C terms**, carried by follow-up issue #1704. Nothing here authorises the
wider per-source-cell breakdown, which this spike explicitly rejects.

All file:line references and measurements below were verified against
`origin/main` @ `885ea4f` (2026-09-11). Re-check line numbers before relying
on them if this document is picked up much later — the surrounding function
is large and moves.

## 1. The problem

A top-level net that spans a hierarchy — part of its conductor drawn inside
a placed sub-block, part drawn at the top level to join blocks together — is
reported by `klt extract --parasitics` as a **single scalar** R and C
covering both. A caller who has already extracted the sub-blocks separately
(the composition use case in the parent friction report, #1699) cannot
subtract the shared portion back out, because nothing in the JSON identifies
which fraction of a net's R/C came from inside an instantiated cell versus
from geometry drawn directly in the top cell.

## 2. Why the netlist object cannot answer this

`_extract_netlist` builds every device-recognition `Region` by flattening
`top_cell.begin_shapes_rec()` (`_layout.region()`, `_layout.py:197-215`)
*before* `l2n.extract_netlist()` runs. Every instance transform is applied
and discarded at that point, and the result is exactly **one**
`kdb.Circuit` — there are no subcircuits to walk, so neither
`kdb.LayoutToNetlist` nor `kdb.Circuit` retains any handle back to the
instance a piece of geometry came from.

This is the same wall the device-side attribution hit. The only existing
cell-attribution primitive in the codebase,
`_instance_path_for_point()` / `_device_instance_paths()`
(`extract.py:8643` / `:8724`, issue #1666), solves it by re-deriving
instance membership **positionally and after the fact**, against the
original still-hierarchical `layout` / `top_cell`, via
`Cell.begin_instances_rec_touching`. Any hierarchy attribution for
parasitics must do the same: go back to `layout` / `top_cell`, never to
`l2n` / `circuit`.

Two things make that viable here:

- `top_cell` is **not** destructively flattened. `_extract_netlist`'s call
  site for `_compute_parasitics` (`extract.py:6671-6680`) still has both
  `layout` and `top_cell` live and hierarchical at that point.
- The "drawn directly in this cell vs. inherited from an instance" test is
  already an **established idiom in this file**, not a new invention:
  `_label_layer_strings()` (`extract.py:3331-3366`, issue #291) draws
  exactly this distinction for label layers by choosing between
  `cell.shapes(layer)` (non-recursive) and `cell.begin_shapes_rec(layer)`
  (recursive), and calls their difference "exactly the set of labels that
  live below an instance boundary."

The device precedent is **point-based** and the parasitics problem is
**region-based** — but, as §3 shows, the region form is *cheaper*, not more
expensive, because the whole instance tree collapses into one `Region`
before any net is considered.

## 3. Decision: the top-cell-only split (shape 1), ground terms only

### 3.1 Per-layer masks, built once

For each **drawn** layer that feeds a curated parasitics role, build two
regions once — independent of net count *and* of instance count:

```python
# layer_index_drawn = layout.find_layer(*drawn_layer)   # NOT the l2n index
own_drawn = kdb.Region(top_cell.shapes(layer_index_drawn)).merged()
all_drawn = kdb.Region(top_cell.begin_shapes_rec(layer_index_drawn)).merged()
instance_drawn = (all_drawn - own_drawn).merged()
```

> **Correction worth stating explicitly, because it is an easy and silent
> bug:** the index passed to `top_cell.shapes(...)` is a **`kdb.Layout`
> layer index** (`layout.find_layer(layer, datatype)`), *not* the
> `metal_index[i]` / `layer_index["poly"]` values `_compute_parasitics`
> already holds. Those are **`LayoutToNetlist` registration handles**
> returned by `l2n.register(region, name)` (`extract.py:5430`, `:5456`) and
> are only meaningful to `l2n.polygons_of_net`. The two index spaces are
> unrelated integers; mixing them reads a plausible-looking wrong layer
> rather than raising.

The role → drawn-layer map (verified at `extract.py:5084-5106`, `:7255-7281`):

| Parasitics role | Registered region built from | Drawn layer to mask against |
|---|---|---|
| `metals[i]` | `_region(layout, top_cell, deck.metals[i])` | `deck.metals[i]` |
| `poly` | `deck.poly`, minus gate regions | `deck.poly` |
| `diffusion` | `nfet_sd`/`pfet_sd` (derived from active ∩ implant) | `deck.active` (superset — safe: the derived SD region is a subset of drawn active, so the mask classifies it correctly) |

`diffusion` is left unset by the shipped decks, so in practice this is
poly + the metal stack.

### 3.2 The split, inside Pass 1's existing loop

Both masks come from the identical `begin_shapes_rec` flatten that produced
the registered regions in the first place, so they sit in the **same
coordinate frame and DBU** as `l2n.polygons_of_net`'s per-net regions —
directly comparable with a plain `Region &` / `Region -`, the same idiom
Pass 2's coupling loop already uses (`extract.py:7359+`).

Inside Pass 1's per-net/per-role loop (`extract.py:7326-7357`), where a
per-net-per-layer `Region` already exists before being reduced to
`area_um2` / `perim_um` scalars:

```python
top_cell_part = net_region - instance_drawn_for_this_layer
top_cell_area_um2 = top_cell_part.area() * dbu * dbu
top_cell_perim_um = top_cell_part.perimeter() * dbu
```

feeding `(top_cell_area_um2, top_cell_perim_um)` through the **exact same**
`cap_area_ff_um2` / `cap_perim_ff_um` / `sheet_res_ohm_sq` + `_n_squares`
formulas Pass 1 already applies, accumulating a second `top_cell_c_ff` /
`top_cell_r_ohm` per net alongside the existing `base_c_ff` / `base_r_ohm`.

### 3.3 The overlap-bias decision (subtract, do not intersect)

Top-cell-drawn geometry and instance-drawn geometry **can overlap** — a
top-level strap routed over a std cell's own metal1 pin is the normal case,
not a corner case. So `own_drawn` and `instance_drawn` are not a partition
of the *shapes*, and there are two inequivalent definitions of a spanning
net's top-cell share:

| Definition | Overlap charged to | Effect on the composition use case |
|---|---|---|
| `net_region & own_drawn` | the **top cell** | top-cell share + separately-extracted sub-block share **double-counts** the overlap |
| `net_region - instance_drawn` | the **instance** | overlap appears only in the sub-block's own extraction — subtraction stays clean |

**Choose the subtraction form** (`net_region - instance_drawn`). The whole
point of the feature is "let a caller subtract what they already extracted
separately," and only the second form makes that arithmetic sound. This
distinction is measurable, not hypothetical — see §4.

Note that defining `instance_drawn = all_drawn - own_drawn` (as above) and
then subtracting it is *not* the same as subtracting the raw union of the
instances' transformed shapes; the former silently hands the overlap back to
the top cell. Build `instance_drawn` as a merged union of the instance
subtree's own geometry, then subtract it from the net region.

## 4. Prototype measurements

A throwaway prototype (two-level hierarchy built in memory: `SUB` placed
twice under `TOP`, one net wholly inside `SUB`, one wholly drawn in `TOP`,
one spanning both with a deliberate top-strap-over-instance-shape overlap)
confirmed the three load-bearing claims against the installed KLayout
Python API:

| Claim | Result |
|---|---|
| Area of the own/instance split is exact | `own 4.8 µm² + instance 2.6 µm² = all 7.4 µm²` — **exact** |
| Perimeter is **not** additive across the split | `own 29.2 µm + instance 16.8 µm = 46.0 µm` vs. uncut `45.2 µm` — **+0.8 µm inflation** |
| The overlap case is real and the two bias definitions differ | overlap `0.2 µm²`; spanning net's top-cell share is `0.8 µm²` (overlap→top) vs. `0.6 µm²` (overlap→instance) |

The prototype validated the geometric primitive only and is deliberately not
committed; the fixture it describes belongs in the implementation issue's
test plan (§6), wired through the real `_compute_parasitics`.

## 5. Complexity and risk against the existing Pass 1 / Pass 2 structure

- **Cost is O(curated drawn layers)** — not O(nets), not O(instances). The
  instance tree collapses into one `Region` per layer *before* any net is
  considered, so a block with 4303 top-level placements
  (`tests/corpus/place_and_route/gcd.gds.gz`) costs the same as one with
  three. Each net then pays one extra `Region -` plus an `area()` /
  `perimeter()` call per role.
- **Contained to Pass 1; ground terms only.** No new state enters Pass 2's
  coupling bookkeeping (`deduction_regions`, the vertical-coupling loops).
  Attributing a *coupling* capacitor — splitting a coupled pair's charge
  between top-cell-drawn and instance-drawn plates — is a separable and
  harder follow-on, and deliberately out of scope. This mirrors this
  module's own history of shipping ground R/C first (#760) and coupling
  later (#976).
- **Two signature changes**, both small and local:
  - `_compute_parasitics` (`extract.py:7068`) must take `layout` and
    `top_cell`. Both are live at the sole call site (`extract.py:6671`).
  - `_net_area_perim_um` (`extract.py:7015`) returns only
    `(area_um2, perim_um)`, so the non-metal roles have no `Region` left to
    mask. Either give it a mask parameter or have it return the `Region`,
    taking care not to disturb the gate-subtraction it already performs for
    poly (issue #226). The metal roles already keep their per-net `Region`
    in `metal_regions` and need no such change.
- **Output is purely additive** — new fields behind a new opt-in CLI flag —
  so this is non-breaking under `docs/json-contract.md`; existing
  `--parasitics` callers see byte-identical output.
- **Known limitation, to be documented rather than fixed:** the split is
  exact in area but **not in perimeter** (§4). Cutting a shape at the
  instance boundary adds `2 × (cut-line length)` to the pieces' combined
  perimeter. Both the fringe-C term and the `_n_squares` sheet-R
  approximation are perimeter-sensitive, so for a net that genuinely crosses
  the boundary, `top_cell_R + instance_R` (and likewise C) comes out
  **slightly larger** than the existing scalar total, in proportion to the
  number and length of boundary crossings. Consequently the parent issue's
  wording — "confirm the attributed R/C sums to the existing scalar total" —
  holds **exactly** only for nets lying wholly on one side of the boundary;
  the spanning case needs a documented tolerance, not exact equality. Flagged
  here so a future reviewer does not read the discrepancy as a regression.
- **Risk: routine-to-moderate.** The real judgment calls are (a) threading
  the mask through `_net_area_perim_um` without perturbing gate subtraction,
  (b) using the layout layer index rather than the l2n index (§3.1), and
  (c) documenting the perimeter caveat clearly enough that it does not later
  read as a bug.

## 6. Why not the per-source-cell breakdown (shape 2)

Full attribution of a net's R/C to each contributing instance would need,
per contributing instance, its own transformed recursive region per curated
layer, then a boolean per (net, layer, instance) triple — genuinely
O(instances) in new boolean operations rather than O(layers). It also drags
in a much larger design surface: a nested per-instance breakdown on every
net entry, instance-path naming that stays consistent with
`_instance_path_for_point`'s array/tie-break rules, and recursive
sub-instance attribution. That is a new geometry subsystem, not an extension
of Pass 1.

No caller need described in #1699, #1701 or #1702 requires more than the
binary top-cell-vs-instance split, and #1699 itself names the narrow flag as
sufficient. Shape 2 is rejected for now; nothing in shape 1 forecloses it
later (the per-layer `instance_drawn` region is the natural place to refine
into per-instance regions if a concrete need ever appears).

## 7. Affected files for the implementation

- `src/klayout_tools/extract.py` — `_compute_parasitics` (~7068-7605,
  Pass 1 at ~7326-7357), `_net_area_perim_um` (~7015), call site (~6671)
- `src/klayout_tools/cli/parser.py` — the new opt-in flag
- `docs/cli/extract.md`, `docs/json-contract.md` — the additive fields and
  the perimeter caveat
- `tests/test_extract.py` — two-level hierarchy fixture: one net wholly
  inside a sub-block (top-cell share ≈ 0), one wholly top-drawn (share ==
  total), one spanning both (share strictly between, with the §5 tolerance)

## Related

- #1702 — this spike.
- #1704 — the implementation issue this spike authorises.
- #1699 — parent friction report; #1701 the sibling per-layer breakdown.
- #1666 (closed) — point-based device instance attribution, the precedent
  this document deliberately does *not* reuse (§2).
- #760 / #976 — ground R/C first, coupling later; the staging this spike
  mirrors.
